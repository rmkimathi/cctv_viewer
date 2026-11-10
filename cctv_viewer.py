#!.venv/bin/python3
"""
CCTV Timeline - PySide6 viewer for Reolink FTP recordings.

Expected layout:  <root>/<Camera>/YYYY/MM/DD/CCTV1_00_20261021000349.mp4
(root = e.g. .../ftp/upload, Camera = a sub-folder such as CCTV)

Setup (Ubuntu):
    sudo apt install mpv ffmpeg gvfs-backends
    pip install PySide6

Run:
    python3 cctv_viewer.py

Build a one-folder app (icon ends up in _internal/assets/cctv.ico):
    pyinstaller --noconfirm --windowed --name cctv-viewer \
        --icon assets/cctv.ico --add-data "assets:assets" cctv_viewer.py

Features
  * Normal system window (snapping/tiling work) with a main menu; the About and
    Delete-old dialogs are frameless.
  * Day timeline per camera; click to play from that moment in mpv.
  * Hover the timeline (or select a row) for a thumbnail preview (ffmpeg).
  * Busy clips highlighted in red (estimated from file size, or accurately via
    Tools > Analyze motion).
  * Right-click a clip (in the list or on the timeline) to play, download,
    open its folder, copy its path, analyse it or delete it.
  * Tools > Delete old recordings... keeps the FTP folder from filling up.
"""
import datetime as dt
import functools
import os
import shutil
import sys
import threading
from pathlib import Path

from PySide6 import QtCore
from PySide6.QtCore import (QByteArray, QDate, QItemSelectionModel, QObject, QPoint,
                            QRect, QRunnable, Qt, QThreadPool, QTimer, QUrl, Signal)
from PySide6.QtGui import (QAction, QBrush, QColor, QDesktopServices, QFontMetrics,
                           QGuiApplication, QIcon, QPainter, QPalette, QPen, QPixmap)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox,
                               QDateEdit, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout,
                               QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
                               QMenu, QMenuBar, QMessageBox, QPlainTextEdit, QProgressBar,
                               QPushButton, QSizePolicy, QSpinBox, QSplitter, QTableWidget,
                               QTableWidgetItem, QToolButton, QVBoxLayout, QWidget)

import cctv_core as core
from cctv_core import (APP_NAME, APP_VERSION, DAY_SECONDS, HEAVY_COLOR, MIN_SEG, PALETTE,
                       Clip, clip_at, fmt_bytes, fmt_dur, fmt_size)

ROLE_CLIP = Qt.ItemDataRole.UserRole.value
ROLE_SORT = ROLE_CLIP + 1

STYLE = """
QWidget { color: #e5e7eb; }
QMainWindow, QDialog { background: #0b1220; }
#root { background: #0f172a; }
#dlgframe { background: #0f172a; border: 1px solid #334155; }
#titlebar { background: #111827; }
#titlebar QLabel { color: #cbd5e1; }
QMenuBar { background: transparent; }
QMenuBar::item { padding: 6px 10px; background: transparent; border-radius: 4px; }
QMenuBar::item:selected { background: #1f2937; }
QMenu { background: #111827; border: 1px solid #334155; padding: 4px; }
QMenu::item { padding: 6px 28px 6px 20px; border-radius: 3px; }
QMenu::item:selected { background: #1d4ed8; }
QMenu::item:disabled { color: #6b7280; }
QMenu::separator { height: 1px; background: #334155; margin: 4px 8px; }
QToolButton#winbtn { border: 0; padding: 0 14px; background: transparent; font-size: 11pt; }
QToolButton#winbtn:hover { background: #1f2937; }
QToolButton#winbtn_close { border: 0; padding: 0 14px; background: transparent; font-size: 11pt; }
QToolButton#winbtn_close:hover { background: #dc2626; }
QLineEdit { background: #111827; border: 1px solid #334155; border-radius: 4px; padding: 4px 6px; }
QLineEdit:focus { border-color: #3b82f6; }
QPushButton { background: #1f2937; border: 1px solid #334155; border-radius: 4px; padding: 5px 12px; }
QPushButton:hover { background: #273449; }
QPushButton:pressed { background: #1d4ed8; }
QPushButton:disabled { color: #6b7280; background: #111827; }
QPushButton#danger { background: #b91c1c; border-color: #b91c1c; }
QPushButton#danger:hover { background: #dc2626; }
QPushButton#danger:disabled { background: #3b1f23; border-color: #3b1f23; color: #7f6d6d; }
QTableWidget { background: #111827; alternate-background-color: #0f172a; border: 1px solid #334155;
               selection-background-color: #1d4ed8; selection-color: white; }
QHeaderView::section { background: #1f2937; color: #cbd5e1; border: 0; border-right: 1px solid #0f172a;
                       padding: 5px 6px; }
QPlainTextEdit { background: #111827; border: 1px solid #334155; border-radius: 4px; }
QProgressBar { background: #111827; border: 1px solid #334155; border-radius: 4px; text-align: center; height: 14px; }
QProgressBar::chunk { background: #3b82f6; border-radius: 3px; }
QSplitter::handle { background: #1f2937; }
QToolTip { background: #111827; color: #e5e7eb; border: 1px solid #334155; }
#preview { background: #111827; border: 1px solid #334155; }
#hint, #muted { color: #94a3b8; }
#daylbl { font-size: 13pt; font-weight: bold; }
#aboutname { font-size: 18pt; font-weight: bold; }
"""


def apply_dark_theme(app):
    app.setStyle("Fusion")
    pal = QPalette()
    for role, color in (
        (QPalette.ColorRole.Window, "#0f172a"), (QPalette.ColorRole.WindowText, "#e5e7eb"),
        (QPalette.ColorRole.Base, "#111827"), (QPalette.ColorRole.AlternateBase, "#0f172a"),
        (QPalette.ColorRole.Text, "#e5e7eb"), (QPalette.ColorRole.Button, "#1f2937"),
        (QPalette.ColorRole.ButtonText, "#e5e7eb"), (QPalette.ColorRole.ToolTipBase, "#111827"),
        (QPalette.ColorRole.ToolTipText, "#e5e7eb"), (QPalette.ColorRole.Highlight, "#1d4ed8"),
        (QPalette.ColorRole.HighlightedText, "#ffffff"), (QPalette.ColorRole.BrightText, "#ffffff"),
        (QPalette.ColorRole.Link, "#60a5fa"), (QPalette.ColorRole.PlaceholderText, "#6b7280"),
    ):
        pal.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText, QPalette.ColorRole.WindowText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, QColor("#6b7280"))
    app.setPalette(pal)
    app.setStyleSheet(STYLE)


# ------------------------------------------------------- background jobs ---
class _Ctx:
    """Handed to a job function: progress(payload) and the cancel Event."""

    def __init__(self, disp, jid, cancel):
        self._disp, self._jid, self.cancel = disp, jid, cancel

    def progress(self, payload):
        self._disp._prog.emit(self._jid, payload)


class _Job(QRunnable):
    def __init__(self, disp, jid, fn, args, cancel):
        super().__init__()
        self._disp, self._jid, self._fn, self._args = disp, jid, fn, args
        self._ctx = _Ctx(disp, jid, cancel)

    def run(self):
        result = error = None
        try:
            result = self._fn(self._ctx, *self._args)
        except BaseException as e:          # reported back to the GUI thread
            error = e
        try:
            self._disp._fin.emit(self._jid, result, error)
        except RuntimeError:                # dispatcher already destroyed (app closing)
            pass


class Dispatcher(QObject):
    """Runs functions on a QThreadPool and delivers results on the GUI thread.

    Callbacks run inside this object's slots, which live in the GUI thread, so
    they may touch widgets directly.
    """
    _prog = Signal(int, object)
    _fin = Signal(int, object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._next = 1
        self._cbs = {}
        self._prog.connect(self._on_prog)
        self._fin.connect(self._on_fin)

    def submit(self, pool, fn, *args, on_done=None, on_progress=None, cancel=None):
        """fn(ctx, *args) runs in a worker. Returns the job's cancel Event.
        Pass your own `cancel` Event if you need it stored before the job can finish."""
        jid = self._next
        self._next += 1
        cancel = cancel if cancel is not None else threading.Event()
        self._cbs[jid] = (on_done, on_progress)
        pool.start(_Job(self, jid, fn, args, cancel))
        return cancel

    def _on_prog(self, jid, payload):
        cb = self._cbs.get(jid)
        if cb and cb[1]:
            cb[1](payload)

    def _on_fin(self, jid, result, error):
        cb = self._cbs.pop(jid, None)
        if cb and cb[0]:
            cb[0](result, error)


def make_pool(n):
    pool = QThreadPool()
    pool.setMaxThreadCount(n)
    return pool


# ----------------------------------------------------------------- widgets ---
class SortItem(QTableWidgetItem):
    """Table item that sorts by a hidden key instead of its text."""

    def __lt__(self, other):
        a, b = self.data(ROLE_SORT), other.data(ROLE_SORT)
        try:
            return a < b
        except TypeError:
            return super().__lt__(other)


class PreviewLabel(QLabel):
    """Shows a pixmap scaled to fit, re-scaling when the panel is resized."""

    def __init__(self):
        super().__init__()
        self.setObjectName("preview")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(320, 180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._pm = None

    def set_image(self, pm):
        self._pm = pm
        self._fit()

    def _fit(self):
        if self._pm is None or self._pm.isNull():
            return
        self.setPixmap(self._pm.scaled(max(self.width() - 4, 16), max(self.height() - 4, 16),
                                       Qt.AspectRatioMode.KeepAspectRatio,
                                       Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit()


class TimelineWidget(QWidget):
    """24-hour bar with one block per clip. Busy clips are taller and red."""
    hovered = Signal(float)
    left = Signal()
    clicked = Signal(float)
    context = Signal(float, QPoint)

    def __init__(self):
        super().__init__()
        self.setMouseTracking(True)
        self.setFixedHeight(104)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.clips, self.colors, self.highlight = [], {}, True
        self._hover = None

    def set_data(self, clips, colors, highlight):
        self.clips, self.colors, self.highlight = clips, colors, highlight
        self.update()

    def _sec_at(self, x):
        return min(max(x / max(self.width(), 1) * DAY_SECONDS, 0), DAY_SECONDS - 1)

    def paintEvent(self, _e):
        p = QPainter(self)
        w, h = self.width(), self.height()
        label_h = 20
        top, bottom = 6, h - label_h
        p.fillRect(self.rect(), QColor("#111827"))
        p.fillRect(QRect(0, top, w, bottom - top), QColor("#1f2937"))
        fm = QFontMetrics(self.font())
        step = next((s for s in (1, 2, 3, 4, 6) if w / 24 * s >= fm.horizontalAdvance("00") + 12), 6)
        for hour in range(25):
            x = round(hour * w / 24)
            p.setPen(QPen(QColor("#374151")))
            p.drawLine(x, top, x, bottom)
            if hour < 24 and hour % step == 0:
                p.setPen(QColor("#9ca3af"))
                p.drawText(QRect(x, bottom + 2, round(w / 24 * step), label_h - 2),
                           Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, f" {hour:02d}")
        for heavy_pass in (False, True):            # busy clips last so they sit on top
            for c in self.clips:
                is_heavy = self.highlight and c.heavy
                if is_heavy != heavy_pass:
                    continue
                x0 = c.sec / DAY_SECONDS * w
                bw = max(c.dur / DAY_SECONDS * w, 3)
                if is_heavy:
                    r, col = QRect(round(x0), top - 4, round(bw), bottom - top + 8), QColor(HEAVY_COLOR)
                else:
                    r, col = QRect(round(x0), top + 6, round(bw), bottom - top - 12), \
                        QColor(self.colors.get(c.label, PALETTE[0]))
                p.fillRect(r, col)
        if self._hover is not None:
            x = round(self._hover / DAY_SECONDS * w)
            p.setPen(QPen(QColor("#f8fafc"), 1))
            p.drawLine(x, 0, x, bottom)
            s = int(self._hover)
            txt = f" {s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d} "
            tw = fm.horizontalAdvance(txt) + 4
            tx = min(x + 4, w - tw)
            p.fillRect(QRect(tx, 0, tw, fm.height() + 2), QColor("#0b1220"))
            p.setPen(QColor("#f8fafc"))
            p.drawText(QRect(tx, 0, tw, fm.height() + 2), Qt.AlignmentFlag.AlignCenter, txt)

    def mouseMoveEvent(self, e):
        self._hover = self._sec_at(e.position().x())
        self.update()
        self.hovered.emit(self._hover)

    def leaveEvent(self, _e):
        self._hover = None
        self.update()
        self.left.emit()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._sec_at(e.position().x()))
        elif e.button() == Qt.MouseButton.RightButton:
            self.context.emit(self._sec_at(e.position().x()), e.globalPosition().toPoint())


# --------------------------------------------------------- frameless chrome ---
def _start_move(widget):
    wh = widget.window().windowHandle()
    if wh is not None:
        wh.startSystemMove()


class DragBar(QWidget):
    """Title strip of a frameless dialog: drag to move."""

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            _start_move(self)


class FramelessDialog(QDialog):
    """Modal dialog without the system frame, with a small draggable title bar."""

    def __init__(self, parent, title):
        super().__init__(parent, Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setModal(True)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        frame = QFrame()
        frame.setObjectName("dlgframe")
        outer.addWidget(frame)
        v = QVBoxLayout(frame)
        v.setContentsMargins(1, 1, 1, 1)
        v.setSpacing(0)
        bar = DragBar()
        bar.setObjectName("titlebar")
        bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        bar.setFixedHeight(34)
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(12, 0, 0, 0)
        lbl = QLabel(title)
        lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        bl.addWidget(lbl)
        bl.addStretch(1)
        close = QToolButton()
        close.setObjectName("winbtn_close")
        close.setText("✕")
        close.setFixedHeight(34)
        close.clicked.connect(self.reject)
        bl.addWidget(close)
        v.addWidget(bar)
        body = QWidget()
        v.addWidget(body)
        self.body_layout = QVBoxLayout(body)
        self.body_layout.setContentsMargins(18, 16, 18, 16)
        self.body_layout.setSpacing(10)


class AboutDialog(FramelessDialog):
    def __init__(self, parent, icon):
        super().__init__(parent, f"About {APP_NAME}")
        self.setMinimumWidth(500)
        v = self.body_layout
        head = QHBoxLayout()
        head.setSpacing(16)
        pic = QLabel()
        pm = icon.pixmap(96, 96)
        if not pm.isNull():
            pic.setPixmap(pm)
        pic.setAlignment(Qt.AlignmentFlag.AlignTop)
        head.addWidget(pic)
        col = QVBoxLayout()
        name = QLabel(APP_NAME)
        name.setObjectName("aboutname")
        col.addWidget(name)
        ver = QLabel(f"Version {APP_VERSION}")
        ver.setObjectName("muted")
        col.addWidget(ver)
        desc = QLabel("Timeline viewer for Reolink FTP recordings.\n"
                      "Browse each day, preview frames, spot busy clips,\n"
                      "play in mpv, download or clean up old footage.")
        desc.setWordWrap(True)
        col.addSpacing(6)
        col.addWidget(desc)
        col.addStretch(1)
        head.addLayout(col, 1)
        v.addLayout(head)
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("color:#334155")
        v.addWidget(line)
        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(4)
        rows = [
            ("Qt", QtCore.qVersion()),
            ("PySide6", _pyside_version()),
            ("Python", sys.version.split()[0]),
            ("Display", QGuiApplication.platformName()),
            ("mpv", shutil.which("mpv") or "not found"),
            ("ffmpeg", shutil.which("ffmpeg") or "not found"),
            ("Settings", str(core.CONFIG)),
            ("Cache", str(core.CACHE_DIR)),
        ]
        for k, val in rows:
            key = QLabel(k)
            key.setObjectName("muted")
            val_l = QLabel(val)
            val_l.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            val_l.setWordWrap(True)
            form.addRow(key, val_l)
        v.addLayout(form)
        row = QHBoxLayout()
        row.addStretch(1)
        ok = QPushButton("Close")
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        row.addWidget(ok)
        v.addLayout(row)


def _pyside_version():
    try:
        import PySide6
        return PySide6.__version__
    except Exception:
        return "?"


class DeleteOldDialog(FramelessDialog):
    """Pick N days, scan, confirm, delete. Self.last_n holds the last N used."""

    def __init__(self, parent, disp, pool, root, days):
        super().__init__(parent, "Delete old recordings")
        self.setMinimumWidth(560)
        self.disp, self.pool, self.root = disp, pool, root
        self.items, self.busy, self.cancel, self._close_when_done = [], False, None, False
        self.last_n = days
        v = self.body_layout
        row = QHBoxLayout()
        row.addWidget(QLabel("Delete recordings older than"))
        self.spin = QSpinBox()
        self.spin.setRange(1, 3650)
        self.spin.setValue(days)
        self.spin.valueChanged.connect(self._reset)
        row.addWidget(self.spin)
        row.addWidget(QLabel("days"))
        row.addStretch(1)
        self.btn_scan = QPushButton("Scan")
        self.btn_scan.clicked.connect(self.scan)
        row.addWidget(self.btn_scan)
        v.addLayout(row)
        self.summary = QPlainTextEdit("Press Scan to see what would be deleted.")
        self.summary.setReadOnly(True)
        self.summary.setFixedHeight(110)
        v.addWidget(self.summary)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        v.addWidget(self.bar)
        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_del = QPushButton("Delete")
        self.btn_del.setObjectName("danger")
        self.btn_del.setEnabled(False)
        self.btn_del.clicked.connect(self.delete)
        self.btn_stop = QPushButton("Stop")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(lambda: self.cancel and self.cancel.set())
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.reject)
        for b in (self.btn_del, self.btn_stop, btn_close):
            row.addWidget(b)
        v.addLayout(row)

    def _reset(self):
        self.items = []
        self.btn_del.setEnabled(False)
        self.summary.setPlainText("Press Scan to see what would be deleted.")

    def scan(self):
        if self.busy:
            return
        n = self.spin.value()
        self.last_n = n
        cutoff = dt.date.today() - dt.timedelta(days=n)
        self.summary.setPlainText("Scanning…")
        self.btn_del.setEnabled(False)
        self.btn_scan.setEnabled(False)
        root = self.root
        self.disp.submit(self.pool, lambda ctx: core.scan_old(root, cutoff),
                         on_done=lambda res, err: self._scanned(cutoff, res, err))

    def _scanned(self, cutoff, items, err):
        self.btn_scan.setEnabled(True)
        if err or items is None:
            self.summary.setPlainText(f"Scan failed: {err}")
            return
        self.items = items
        if not items:
            self.summary.setPlainText(f"Nothing older than {cutoff} found under:\n{self.root}")
            return
        total = sum(i[1] for i in items)
        days = {i[2] for i in items}
        cams = sorted({i[3] for i in items})
        self.summary.setPlainText(
            f"{len(items)} files ({fmt_bytes(total)}) in {len(days)} day folders\n"
            f"Oldest: {min(days)}   Newest: {max(days)}\n"
            f"Cameras: {', '.join(cams)}\n"
            f"Everything dated before {cutoff} will be deleted; recent days are kept.")
        self.btn_del.setEnabled(True)

    def delete(self):
        if self.busy or not self.items:
            return
        total = sum(i[1] for i in self.items)
        ans = QMessageBox.question(
            self, "Confirm delete",
            f"Permanently delete {len(self.items)} files ({fmt_bytes(total)})?\n\n"
            "Files on the SMB share are NOT moved to a trash folder.\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ans != QMessageBox.StandardButton.Yes:
            return
        self.busy = True
        for w in (self.btn_del, self.btn_scan, self.spin):
            w.setEnabled(False)
        self.btn_stop.setEnabled(True)
        todo = list(self.items)
        self.cancel = threading.Event()
        self.disp.submit(
            self.pool, lambda ctx: core.delete_files(todo, ctx.progress, ctx.cancel),
            on_progress=self._progress, on_done=self._deleted, cancel=self.cancel)

    def _progress(self, payload):
        i, t = payload
        self.bar.setValue(int(i * 100 / max(t, 1)))
        self.summary.setPlainText(f"Deleting… {i}/{t}")

    def _deleted(self, res, err):
        self.busy, self.items = False, []
        self.btn_stop.setEnabled(False)
        self.btn_scan.setEnabled(True)
        self.spin.setEnabled(True)
        self.bar.setValue(100)
        if err or res is None:
            self.summary.setPlainText(f"Delete failed: {err}")
        else:
            deleted, errors, freed = res
            stopped = self.cancel is not None and self.cancel.is_set()
            self.summary.setPlainText(
                f"Deleted {deleted} files, freed {fmt_bytes(freed)}."
                + (f"\n{errors} files could not be deleted." if errors else "")
                + ("\nStopped early." if stopped else ""))
        if self._close_when_done:
            QDialog.reject(self)

    def reject(self):
        if self.busy:                       # let the running delete finish stopping first
            self._close_when_done = True
            if self.cancel:
                self.cancel.set()
            return
        super().reject()


# ------------------------------------------------------------- main window ---
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.icon = QIcon(str(core.ICON_PATH)) if core.ICON_PATH.exists() else QIcon()
        self.setWindowIcon(self.icon)
        self.resize(1280, 800)

        self.cfg = core.load_config()
        self.day = dt.date.today()
        self.all, self.shown, self.colors, self.labels = [], [], {}, []
        self.by_path = {}
        self.load_gen = self.an_gen = 0
        self.an_total = self.an_done = self.an_fail = 0
        self.thumb_want = None
        self.thumb_cap = ""
        self._pending_thumb = None
        self.xfer_cancel = None
        self.cam_chosen = False
        self.have_ffmpeg = shutil.which("ffmpeg") is not None
        self.motion_cache = core.load_motion_cache()
        core.prune_thumbs()

        self.disp = Dispatcher(self)
        self.pool_misc, self.pool_xfer = make_pool(2), make_pool(1)
        self.pool_thumb, self.pool_motion = make_pool(2), make_pool(2)
        self.thumb_timer = QTimer(self)
        self.thumb_timer.setSingleShot(True)
        self.thumb_timer.timeout.connect(self._fire_thumb)

        self._build_menus()
        self._build_ui()
        self.setMenuBar(self.menubar)

        geo = self.cfg.get("geometry")
        if geo:
            self.restoreGeometry(QByteArray.fromBase64(geo.encode()))
        if self.cfg.get("root"):
            QTimer.singleShot(0, self.refresh)

    # ----- menus
    def _action(self, menu, text, slot, shortcut=None, checkable=False, checked=False):
        a = QAction(text, self)
        if shortcut:
            a.setShortcut(shortcut)
        if checkable:
            a.setCheckable(True)
            a.setChecked(checked)
        a.triggered.connect(slot)
        menu.addAction(a)
        return a

    def _build_menus(self):
        mb = QMenuBar()
        self.menubar = mb
        m = mb.addMenu("&File")
        self._action(m, "Open folder…", self.browse_folder, "Ctrl+O")
        self._action(m, "Mount SMB share", self.mount, "Ctrl+M")
        self._action(m, "Reload", self.refresh, "F5")
        m.addSeparator()
        self._action(m, "Download selected…", lambda: self.download_clips(self.selected_clips()), "Ctrl+S")
        m.addSeparator()
        self._action(m, "Exit", self.close, "Ctrl+Q")

        m = mb.addMenu("&View")
        self._action(m, "Previous day", lambda: self.set_day(self.day - dt.timedelta(days=1)), "Alt+Left")
        self._action(m, "Next day", lambda: self.set_day(self.day + dt.timedelta(days=1)), "Alt+Right")
        self._action(m, "Today", lambda: self.set_day(dt.date.today()), "Ctrl+T")
        m.addSeparator()
        self.act_keep = self._action(m, "Keep playing next clips", lambda: None, checkable=True, checked=True)
        self.act_hilite = self._action(m, "Highlight busy clips", lambda: None, checkable=True, checked=True)
        self.act_busyonly = self._action(m, "Busy clips only", lambda: None, checkable=True, checked=False)

        m = mb.addMenu("&Tools")
        self._action(m, "Analyze motion", lambda: self.start_analysis(), "Ctrl+Shift+M")
        self._action(m, "Delete old recordings…", self.open_cleanup, "Ctrl+D")
        m.addSeparator()
        self._action(m, "Clear thumbnail cache", self.clear_cache)

        m = mb.addMenu("&Help")
        self._action(m, f"About {APP_NAME}", self.show_about, "F1")
        self._action(m, "About Qt", lambda: QApplication.aboutQt())

    # ----- body
    def _build_ui(self):
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        v = QVBoxLayout(root)
        v.setContentsMargins(12, 10, 12, 8)
        v.setSpacing(8)

        # connection
        row = QHBoxLayout()
        row.addWidget(QLabel("SMB URL"))
        self.smb_edit = QLineEdit(self.cfg.get("smb", ""))
        self.smb_edit.setPlaceholderText("smb://server/share[/subfolder]")
        self.smb_edit.setMinimumWidth(280)
        row.addWidget(self.smb_edit, 3)
        row.addWidget(QLabel("User"))
        self.user_edit = QLineEdit()
        self.user_edit.setMaximumWidth(140)
        row.addWidget(self.user_edit)
        row.addWidget(QLabel("Pass"))
        self.pass_edit = QLineEdit()
        self.pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.pass_edit.setMaximumWidth(140)
        row.addWidget(self.pass_edit)
        self.btn_mount = QPushButton("Mount")
        self.btn_mount.clicked.connect(self.mount)
        row.addWidget(self.btn_mount)
        v.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(QLabel("Folder"))
        self.root_edit = QLineEdit(self.cfg.get("root", ""))
        self.root_edit.setPlaceholderText("Root folder containing one sub-folder per camera")
        self.root_edit.returnPressed.connect(self.load_clicked)
        row.addWidget(self.root_edit, 1)
        b = QPushButton("Browse…")
        b.clicked.connect(self.browse_folder)
        row.addWidget(b)
        b = QPushButton("Load")
        b.clicked.connect(self.load_clicked)
        row.addWidget(b)
        v.addLayout(row)

        # navigation
        row = QHBoxLayout()
        b = QPushButton("◀ Prev")
        b.clicked.connect(lambda: self.set_day(self.day - dt.timedelta(days=1)))
        row.addWidget(b)
        self.day_lbl = QLabel()
        self.day_lbl.setObjectName("daylbl")
        self.day_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.day_lbl.setMinimumWidth(170)
        row.addWidget(self.day_lbl)
        b = QPushButton("Next ▶")
        b.clicked.connect(lambda: self.set_day(self.day + dt.timedelta(days=1)))
        row.addWidget(b)
        b = QPushButton("Today")
        b.clicked.connect(lambda: self.set_day(dt.date.today()))
        row.addWidget(b)
        self.date_edit = QDateEdit()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        self.date_edit.dateChanged.connect(self._date_picked)
        row.addWidget(self.date_edit)
        row.addSpacing(12)
        row.addWidget(QLabel("Camera"))
        self.cam_combo = QComboBox()
        self.cam_combo.addItem("All")
        self.cam_combo.setMinimumWidth(130)
        self.cam_combo.currentTextChanged.connect(self._camera_changed)
        row.addWidget(self.cam_combo)
        row.addStretch(1)
        b = QPushButton("Refresh")
        b.clicked.connect(self.refresh)
        row.addWidget(b)
        v.addLayout(row)

        # options
        row = QHBoxLayout()
        self.chk_keep = QCheckBox("Keep playing next clips")
        self.chk_keep.setChecked(True)
        row.addWidget(self.chk_keep)
        row.addSpacing(10)
        self.chk_hilite = QCheckBox("Highlight busy clips at")
        self.chk_hilite.setChecked(True)
        row.addWidget(self.chk_hilite)
        self.factor = QDoubleSpinBox()
        self.factor.setRange(1.0, 20.0)
        self.factor.setSingleStep(0.1)
        self.factor.setDecimals(1)
        self.factor.setValue(1.5)
        row.addWidget(self.factor)
        row.addWidget(QLabel("× typical"))
        self.chk_busyonly = QCheckBox("Busy only")
        row.addWidget(self.chk_busyonly)
        b = QPushButton("Analyze motion")
        b.clicked.connect(lambda: self.start_analysis())
        row.addWidget(b)
        legend = QLabel("★ busy · ~ estimated from file size")
        legend.setObjectName("hint")
        row.addWidget(legend)
        row.addStretch(1)
        v.addLayout(row)
        for act, chk in ((self.act_keep, self.chk_keep), (self.act_hilite, self.chk_hilite),
                         (self.act_busyonly, self.chk_busyonly)):
            act.toggled.connect(chk.setChecked)
            chk.toggled.connect(act.setChecked)
        self.chk_hilite.toggled.connect(self.apply_filter)
        self.chk_busyonly.toggled.connect(self.apply_filter)
        self.factor.valueChanged.connect(self.apply_filter)

        # timeline
        self.timeline = TimelineWidget()
        self.timeline.hovered.connect(self.on_tl_hover)
        self.timeline.clicked.connect(self.on_tl_click)
        self.timeline.context.connect(self.on_tl_context)
        v.addWidget(self.timeline)
        self.hint = QLabel("Click the timeline to play from that moment · right-click for more")
        self.hint.setObjectName("hint")
        v.addWidget(self.hint)

        # table + preview
        split = QSplitter(Qt.Orientation.Horizontal)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Time", "Camera", "Length", "Size", "Motion", "File"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(24)
        hh = self.table.horizontalHeader()
        hh.setStretchLastSection(True)
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for col, wd in enumerate((90, 100, 80, 90, 100)):
            self.table.setColumnWidth(col, wd)
        hh.setSortIndicator(0, Qt.SortOrder.AscendingOrder)
        self.table.setSortingEnabled(True)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.on_table_context)
        self.table.itemSelectionChanged.connect(self.on_selection)
        self.table.itemDoubleClicked.connect(lambda it: self.play_clip(self._clip_of_row(it.row())))
        split.addWidget(self.table)
        side = QWidget()
        sv = QVBoxLayout(side)
        sv.setContentsMargins(0, 0, 0, 0)
        self.preview = PreviewLabel()
        sv.addWidget(self.preview, 1)
        self.cap = QLabel("Hover the timeline for a preview" if self.have_ffmpeg
                          else "Previews need ffmpeg: sudo apt install ffmpeg")
        self.cap.setObjectName("hint")
        self.cap.setWordWrap(True)
        sv.addWidget(self.cap)
        split.addWidget(side)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([700, 460])
        v.addWidget(split, 1)

        # bottom row
        row = QHBoxLayout()
        b = QPushButton("▶ Play selected")
        b.clicked.connect(lambda: self.play_clip(self._first_selected()))
        row.addWidget(b)
        b = QPushButton("⬇ Download…")
        b.clicked.connect(lambda: self.download_clips(self.selected_clips()))
        row.addWidget(b)
        b = QPushButton("Delete old…")
        b.clicked.connect(self.open_cleanup)
        row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)

        # status bar
        self.status_lbl = QLabel("")
        self.xfer_bar = QProgressBar()
        self.xfer_bar.setRange(0, 1000)
        self.xfer_bar.setFixedWidth(200)
        self.xfer_bar.hide()
        self.xfer_cancel_btn = QPushButton("Cancel")
        self.xfer_cancel_btn.hide()
        self.xfer_cancel_btn.clicked.connect(lambda: self.xfer_cancel and self.xfer_cancel.set())
        sb = self.statusBar()
        sb.setSizeGripEnabled(False)
        sb.addWidget(self.status_lbl, 1)
        sb.addPermanentWidget(self.xfer_bar)
        sb.addPermanentWidget(self.xfer_cancel_btn)
        self._show_day()

    # ----- small helpers
    def status(self, msg):
        self.status_lbl.setText(msg)

    def persist(self):
        self.cfg["root"] = self.root_edit.text().strip()
        self.cfg["smb"] = self.smb_edit.text().strip()
        self.cfg["geometry"] = bytes(self.saveGeometry().toBase64()).decode()
        core.save_config(self.cfg)

    def _show_day(self):
        self.day_lbl.setText(f"{self.day:%Y-%m-%d  %a}")
        self.date_edit.blockSignals(True)
        self.date_edit.setDate(QDate(self.day.year, self.day.month, self.day.day))
        self.date_edit.blockSignals(False)

    def _date_picked(self, qd):
        d = qd.toPython()
        if d != self.day:
            self.set_day(d)

    def set_day(self, d):
        self.day = d
        self._show_day()
        self.refresh()

    def show_about(self):
        AboutDialog(self, self.icon).exec()

    # ----- connection
    def browse_folder(self):
        start = self.root_edit.text().strip() or str(Path.home())
        d = QFileDialog.getExistingDirectory(self, "Choose the recordings root folder", start)
        if d:
            self.root_edit.setText(d)
            self.load_clicked()

    def load_clicked(self):
        self.persist()
        self.refresh()

    def mount(self):
        url, user, pw = self.smb_edit.text().strip(), self.user_edit.text(), self.pass_edit.text()
        if not url:
            self.status("Enter an smb://server/share URL first")
            return
        self.status("Mounting…")
        self.btn_mount.setEnabled(False)
        self.disp.submit(self.pool_misc, lambda ctx: core.mount_smb(url, user, "", pw),
                         on_done=self._mounted)

    def _mounted(self, res, err):
        self.btn_mount.setEnabled(True)
        if err or not res:
            self.status(f"Mount failed: {err}")
            return
        path, msg = res
        self.status(msg)
        if path:
            self.root_edit.setText(path)
            self.persist()
            self.refresh()

    # ----- loading / filtering
    def refresh(self):
        root = self.root_edit.text().strip()
        if not root:
            self.status("Set a folder or mount an SMB share first")
            return
        self.load_gen += 1
        self.an_gen += 1                        # cancels any running analysis
        self.an_total = 0
        self.thumb_want = None
        gen, day = self.load_gen, self.day
        self.status("Loading…")

        def work(ctx):
            if self.load_gen != gen:            # superseded while queued
                return None
            return core.load_day(root, day), core.list_cameras(root)

        self.disp.submit(self.pool_misc, work,
                         on_done=lambda res, err: self._loaded(gen, root, day, res, err))

    def _loaded(self, gen, root, day, res, err):
        if gen != self.load_gen:
            return
        if err or res is None:
            self.all, self.by_path = [], {}
            self.apply_filter()
            self.status(str(err) if err else "Load cancelled")
            return
        self.all, self.labels = res
        self.by_path = {c.path: c for c in self.all}
        for c in self.all:                      # reuse earlier analysis
            ent = self.motion_cache.get(c.path)
            if ent and ent[0] == c.size and abs(ent[1] - c.mtime) < 1:
                c.motion = ent[2]
        labels = self.labels
        self.colors = {l: PALETTE[i % len(PALETTE)] for i, l in enumerate(labels)}
        cur = self.cam_combo.currentText()
        if self.cam_chosen and (cur == "All" or cur in labels):
            pick = cur
        elif "CCTV" in labels:
            pick = "CCTV"
        else:
            pick = labels[0] if labels else "All"
        self.cam_combo.blockSignals(True)
        self.cam_combo.clear()
        self.cam_combo.addItems(["All"] + labels)
        self.cam_combo.setCurrentText(pick)
        self.cam_combo.blockSignals(False)
        self.apply_filter()

    def _camera_changed(self, _text):
        self.cam_chosen = True
        self.apply_filter()

    def apply_filter(self, *_):
        core.score_clips(self.all, self.factor.value())
        cam = self.cam_combo.currentText() or "All"
        busy_only = self.chk_busyonly.isChecked()
        self.shown = [c for c in self.all
                      if cam in ("All", c.label) and (not busy_only or c.heavy)]
        self.redraw_timeline()
        self.populate_table()
        self.update_summary()

    def redraw_timeline(self):
        self.timeline.set_data(self.shown, self.colors, self.chk_hilite.isChecked())

    def update_summary(self):
        if not self.all:
            root = self.root_edit.text().strip()
            self.status(f"No recordings for {self.day} under {root} "
                        f"(cameras found: {', '.join(self.labels) or 'none'})")
            return
        total = sum(c.dur for c in self.shown)
        busy = sum(1 for c in self.shown if c.heavy)
        self.status(f"{len(self.shown)} clips, ~{total / 3600:.1f} h recorded, {busy} busy")

    @staticmethod
    def _motion_text(c):
        return f"{c.ratio:.1f}×{'' if c.src == 'scene' else '~'}{' ★' if c.heavy else ''}"

    def populate_table(self):
        t = self.table
        keep = {c.path for c in self.selected_clips()}
        t.blockSignals(True)
        t.setSortingEnabled(False)
        t.setRowCount(len(self.shown))
        for r, c in enumerate(self.shown):
            cells = [(f"{c.start:%H:%M:%S}", c.sec), (c.label, c.label), (fmt_dur(c.dur), c.dur),
                     (fmt_size(c.size), c.size), (self._motion_text(c), c.ratio), (c.name, c.name)]
            for col, (text, key) in enumerate(cells):
                it = SortItem(text)
                it.setData(ROLE_SORT, key)
                if col == 0:
                    it.setData(ROLE_CLIP, c)
                if col == 4 and c.heavy:
                    it.setForeground(QBrush(QColor(HEAVY_COLOR)))
                t.setItem(r, col, it)
        t.setSortingEnabled(True)
        if keep:
            sm = t.selectionModel()
            for r in range(t.rowCount()):
                it = t.item(r, 0)
                if it is not None and it.data(ROLE_CLIP).path in keep:
                    sm.select(t.model().index(r, 0),
                              QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
        t.blockSignals(False)

    # ----- selection helpers
    def _clip_of_row(self, row):
        it = self.table.item(row, 0)
        return it.data(ROLE_CLIP) if it is not None else None

    def selected_clips(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        out = [c for c in (self._clip_of_row(r) for r in rows) if c is not None]
        return sorted(out, key=lambda c: (c.start, c.cam))

    def _first_selected(self):
        s = self.selected_clips()
        return s[0] if s else None

    def select_clip(self, clip):
        for r in range(self.table.rowCount()):
            if self._clip_of_row(r) is clip:
                self.table.selectRow(r)
                return

    def on_selection(self):
        c = self._first_selected()
        if c is not None:
            self.hint.setText(f"{c.start:%H:%M:%S}  {c.name}")
            self.request_thumb(c, 0)

    # ----- playback
    def play_clip(self, clip, offset=0):
        if clip is None:
            self.status("Select a clip first")
            return
        try:
            idx = self.shown.index(clip)
        except ValueError:
            return
        try:
            core.play(self.shown, idx, offset, self.chk_keep.isChecked())
        except RuntimeError as e:
            QMessageBox.warning(self, "Cannot play", str(e))

    def on_tl_click(self, sec):
        idx, off = clip_at(self.shown, sec)
        h, m, s = int(sec) // 3600, int(sec) % 3600 // 60, int(sec) % 60
        if idx is None:
            self.hint.setText(f"{h:02d}:{m:02d}:{s:02d} – no recording near here")
            return
        c = self.shown[idx]
        self.hint.setText(f"Playing {c.name} from +{fmt_dur(off)}")
        self.play_clip(c, off)

    # ----- context menus
    def on_table_context(self, pos):
        idx = self.table.indexAt(pos)
        if not idx.isValid():
            menu = QMenu(self)
            menu.addAction("Reload", self.refresh)
            menu.exec(self.table.viewport().mapToGlobal(pos))
            return
        clips = self.selected_clips()
        if clips:
            self.clip_menu(clips, self.table.viewport().mapToGlobal(pos))

    def on_tl_context(self, sec, gpos):
        idx, off = clip_at(self.shown, sec)
        if idx is None:
            return
        c = self.shown[idx]
        self.select_clip(c)
        self.clip_menu([c], gpos, offset=off)

    def clip_menu(self, clips, gpos, offset=None):
        n = len(clips)
        first = clips[0]
        menu = QMenu(self)
        menu.addAction("▶  Play" if n == 1 else f"▶  Play (first of {n}, then continue)",
                       lambda: self.play_clip(first))
        if offset is not None and offset > 0:
            menu.addAction(f"▶  Play from here (+{fmt_dur(offset)})",
                           lambda: self.play_clip(first, offset))
        menu.addAction("Open containing folder",
                       lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(first.path))))
        menu.addSeparator()
        total = sum(c.size for c in clips)
        menu.addAction("⬇  Download…" if n == 1 else f"⬇  Download {n} files ({fmt_bytes(total)})…",
                       lambda: self.download_clips(clips))
        menu.addAction("Copy file path" if n == 1 else "Copy file paths",
                       lambda: QApplication.clipboard().setText("\n".join(c.path for c in clips)))
        menu.addSeparator()
        menu.addAction("Analyze motion" + ("" if n == 1 else f" ({n} clips)"),
                       lambda: self.start_analysis(clips))
        menu.addSeparator()
        menu.addAction("Delete…" if n == 1 else f"Delete {n} files…",
                       lambda: self.delete_clips(clips))
        menu.exec(gpos)

    # ----- download
    def download_clips(self, clips):
        if not clips:
            self.status("Select one or more clips first")
            return
        if self.xfer_cancel is not None:
            self.status("A download is already running")
            return
        default = self.cfg.get("download_dir") or str(Path.home() / "Downloads")
        if not Path(default).is_dir():
            default = str(Path.home())
        dest = QFileDialog.getExistingDirectory(self, "Download to…", default)
        if not dest:
            return
        self.cfg["download_dir"] = dest
        paths = [c.path for c in clips]
        self.xfer_bar.setValue(0)
        self.xfer_bar.show()
        self.xfer_cancel_btn.show()
        self.status(f"Downloading {len(paths)} file(s)…")
        self.xfer_cancel = threading.Event()
        self.disp.submit(
            self.pool_xfer, lambda ctx: core.copy_files(paths, dest, ctx.progress, ctx.cancel),
            on_progress=self._xfer_progress, on_done=lambda res, err: self._xfer_done(dest, res, err),
            cancel=self.xfer_cancel)

    def _xfer_progress(self, payload):
        done, total, name = payload
        self.xfer_bar.setValue(int(done * 1000 / total) if total else 0)
        self.status(f"Downloading {name}…  {fmt_bytes(done)} of {fmt_bytes(total)}")

    def _xfer_done(self, dest, res, err):
        self.xfer_cancel = None
        self.xfer_bar.hide()
        self.xfer_cancel_btn.hide()
        if err or res is None:
            self.status(f"Download failed: {err}")
            QMessageBox.warning(self, "Download failed", str(err))
            return
        copied, errors, cancelled = res
        if cancelled:
            self.status(f"Download cancelled ({len(copied)} file(s) finished)")
        else:
            self.status(f"Downloaded {len(copied)} file(s) to {dest}")
        if errors:
            QMessageBox.warning(self, "Some files failed",
                                "\n".join(f"{os.path.basename(p)}: {m}" for p, m in errors[:10]))

    # ----- delete selected
    def delete_clips(self, clips):
        if not clips:
            return
        total = sum(c.size for c in clips)
        ans = QMessageBox.question(
            self, "Delete recordings",
            f"Permanently delete {len(clips)} file(s) ({fmt_bytes(total)})?\n\n"
            "Files on the SMB share are NOT moved to a trash folder.\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ans != QMessageBox.StandardButton.Yes:
            return
        items = [(c.path, c.size, c.start.date(), c.cam) for c in clips]
        self.status("Deleting…")
        self.disp.submit(self.pool_misc, lambda ctx: core.delete_files(items),
                         on_done=self._deleted_selected)

    def _deleted_selected(self, res, err):
        if err or res is None:
            self.status(f"Delete failed: {err}")
        else:
            deleted, errors, freed = res
            self.status(f"Deleted {deleted} file(s), freed {fmt_bytes(freed)}"
                        + (f"; {errors} failed" if errors else ""))
        self.refresh()

    # ----- thumbnails
    def show_thumb(self, path, caption):
        pm = QPixmap(str(path))
        if not pm.isNull():
            self.preview.set_image(pm)
        self.cap.setText(caption)

    def on_tl_hover(self, sec):
        idx, off = clip_at(self.shown, sec, strict=True)
        if idx is None:
            self.thumb_want = None
            self._pending_thumb = None
            s = int(sec)
            self.cap.setText(f"{s // 3600:02d}:{s % 3600 // 60:02d}  – no recording")
        else:
            self.request_thumb(self.shown[idx], off)

    def request_thumb(self, c, off):
        off = int(off) // 10 * 10
        key = (c.path, off)
        if key == self.thumb_want:
            return
        self.thumb_want = key
        caption = f"{c.cam}  {c.start + dt.timedelta(seconds=off):%H:%M:%S}  ({c.name})"
        self.thumb_cap = caption
        if not self.have_ffmpeg:
            self.cap.setText(caption + "\n(install ffmpeg for previews)")
            return
        out = core.thumb_path(c, off)
        if out.exists():
            self._pending_thumb = None
            self.show_thumb(out, caption)
            return
        self.cap.setText(caption + "\nLoading preview…")
        self._pending_thumb = (c, off, key)
        self.thumb_timer.start(150)             # wait until the pointer settles

    def _fire_thumb(self):
        if not self._pending_thumb:
            return
        c, off, key = self._pending_thumb
        self._pending_thumb = None
        if key != self.thumb_want:
            return

        def work(ctx):
            if self.thumb_want != key:
                return key, None
            out = core.make_thumb(c, off)
            return key, str(out) if out else None

        self.disp.submit(self.pool_thumb, work, on_done=self._thumb_ready)

    def _thumb_ready(self, res, err):
        if err or res is None:
            return
        key, path = res
        if key != self.thumb_want:
            return
        if path:
            self.show_thumb(path, self.thumb_cap)
        else:
            self.cap.setText(self.thumb_cap + "\n(preview failed)")

    # ----- motion analysis
    def start_analysis(self, clips=None):
        if not self.have_ffmpeg:
            QMessageBox.warning(self, "ffmpeg not found", "Install it with:  sudo apt install ffmpeg")
            return
        pool = clips if isinstance(clips, list) else self.all
        todo = [c for c in pool if c.motion is None]
        if not todo:
            self.status("Nothing to analyse: those clips are already analysed")
            return
        self.an_gen += 1
        gen = self.an_gen
        self.an_total, self.an_done, self.an_fail = len(todo), 0, 0
        self.status(f"Analyzing motion 0/{len(todo)}…")
        for c in todo:
            self.disp.submit(self.pool_motion, functools.partial(self._motion_work, c, gen),
                             on_done=functools.partial(self._motion_done, c, gen))

    def _motion_work(self, c, gen, _ctx):
        if gen != self.an_gen:
            return None
        return core.analyze_motion(c.path)

    def _motion_done(self, c, gen, score, err):
        if gen != self.an_gen:
            return
        self.an_done += 1
        if err or score is None:
            self.an_fail += 1
        else:
            c.motion = score
            self.motion_cache[c.path] = [c.size, c.mtime, score]
        if self.an_done >= self.an_total:
            core.save_motion_cache(self.motion_cache)
            self.apply_filter()
            fails = f" ({self.an_fail} failed)" if self.an_fail else ""
            self.status(f"Motion analysis done{fails}. "
                        f"{sum(1 for x in self.shown if x.heavy)} busy clips")
        else:
            self.status(f"Analyzing motion {self.an_done}/{self.an_total}…")
            if self.an_done % 10 == 0:
                core.score_clips(self.all, self.factor.value())
                self.redraw_timeline()

    # ----- tools
    def open_cleanup(self):
        root = self.root_edit.text().strip()
        if not root or not Path(root).is_dir():
            self.status("Set a valid folder first")
            return
        dlg = DeleteOldDialog(self, self.disp, self.pool_misc, root, int(self.cfg.get("keep_days", 30)))
        dlg.exec()
        self.cfg["keep_days"] = dlg.last_n
        self.persist()
        self.refresh()

    def clear_cache(self):
        n, size = core.clear_thumbs()
        self.status(f"Cleared {n} cached thumbnails ({fmt_bytes(size)})")

    # ----- shutdown
    def closeEvent(self, e):
        self.an_gen += 1
        self.load_gen += 1
        self.thumb_want = None
        for p in (self.pool_motion, self.pool_thumb, self.pool_misc, self.pool_xfer):
            p.clear()
        if self.xfer_cancel:
            self.xfer_cancel.set()
        core.kill_ffmpeg()
        core.save_motion_cache(self.motion_cache)
        self.persist()
        for p in (self.pool_motion, self.pool_thumb, self.pool_misc, self.pool_xfer):
            p.waitForDone(3000)
        super().closeEvent(e)


def main():
    QGuiApplication.setDesktopFileName("cctv-viewer")
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    if core.ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(core.ICON_PATH)))
    apply_dark_theme(app)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
