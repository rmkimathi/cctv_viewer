"""
cctv_core - GUI-independent logic for the CCTV Timeline viewer.

Layout expected on disk:  <root>/<Camera>/YYYY/MM/DD/CCTV1_00_20261021000349.mp4
"""
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

APP_NAME = "CCTV Timeline"
APP_VERSION = "2.0.0"

CONFIG = Path.home() / ".config" / "cctv_viewer.json"
CACHE_DIR = Path.home() / ".cache" / "cctv_viewer"
THUMB_DIR = CACHE_DIR / "thumbs"
MOTION_FILE = CACHE_DIR / "motion.json"

FILE_RE = re.compile(r"^(?P<cam>.+?)_(?P<ch>\d+)_(?P<ts>\d{14})\.mp4$", re.I)
DAY_SECONDS = 86400
THUMB_W = 480
MIN_SEG = 150          # min width (seconds) used for hit-testing so short clips stay clickable
MAX_DUR = 3600         # sanity cap on guessed durations
PALETTE = ["#3b82f6", "#22c55e", "#f59e0b", "#ec4899", "#8b5cf6", "#14b8a6"]
HEAVY_COLOR = "#ef4444"
MIN_SCENE = 0.02       # analysed clips below this score are never called busy


# --------------------------------------------------------------- resources ---
def resource_path(rel):
    """Path of a bundled resource.

    From source:  <folder of this file>/<rel>
    PyInstaller 6 (one-folder): sys._MEIPASS is <dist>/<app>/_internal, so
    "assets/cctv.ico" resolves to _internal/assets/cctv.ico.
    """
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / rel


ICON_PATH = resource_path("assets/cctv.ico")

# On Windows every console program (ffmpeg, mpv, ...) started from a GUI app would
# flash a console window; CREATE_NO_WINDOW suppresses it. No effect elsewhere.
NO_WINDOW = ({"creationflags": subprocess.CREATE_NO_WINDOW}
             if sys.platform == "win32" else {})


# ----------------------------------------------------------------- config ---
def load_config():
    try:
        return json.loads(CONFIG.read_text())
    except Exception:
        return {"root": "", "smb": ""}


def save_config(cfg):
    try:
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text(json.dumps(cfg, indent=2))
    except OSError:
        pass


# -------------------------------------------------------------------- smb ---
def gvfs_path_for(smb_url):
    """Map smb://server/share/sub/dir to its GVFS FUSE path, if mounted."""
    u = urlparse(smb_url)
    parts = [unquote(p) for p in u.path.split("/") if p]
    if not u.hostname or not parts:
        return None
    share, sub = parts[0], parts[1:]
    base = Path(f"/run/user/{os.getuid()}/gvfs")
    for d in base.glob("smb-share:*"):
        name = d.name.lower()
        if f"server={u.hostname.lower()}" in name and f"share={share.lower()}" in name:
            return str(d.joinpath(*sub))
    return None


def mount_smb(smb_url, user="", domain="", password=""):
    """Mount via gio. Returns (path or None, message)."""
    if not shutil.which("gio"):
        return None, "gio not found (sudo apt install libglib2.0-bin gvfs-backends)"
    u = urlparse(smb_url)
    parts = [p for p in u.path.split("/") if p]
    if not u.hostname or not parts:
        return None, "Use the form smb://server/share[/subfolder]"
    existing = gvfs_path_for(smb_url)
    if existing:
        return existing, "Already mounted"
    share_url = f"smb://{u.hostname}/{parts[0]}"
    stdin = None
    if user or password:
        stdin = f"{user}\n{domain}\n{password}\n"
    try:
        r = subprocess.run(["gio", "mount", share_url], input=stdin,
                           capture_output=True, text=True, timeout=30, **NO_WINDOW)
    except subprocess.TimeoutExpired:
        return None, "Mount timed out"
    path = gvfs_path_for(smb_url)
    if path:
        return path, "Mounted"
    return None, (r.stderr or r.stdout or "Mount failed").strip()


# ------------------------------------------------------------- recordings ---
class Clip:
    __slots__ = ("path", "cam", "ch", "start", "dur", "size", "mtime",
                 "motion", "ratio", "heavy", "src")

    def __init__(self, path, cam, ch, start, dur, size, mtime=0.0):
        self.path, self.cam, self.ch = path, cam, ch
        self.start, self.dur, self.size, self.mtime = start, dur, size, mtime
        self.motion = None      # ffmpeg scene score, once analysed
        self.ratio = 1.0        # activity relative to this camera's typical clip
        self.heavy = False
        self.src = "rate"       # "scene" (analysed) or "rate" (size/second estimate)

    @property
    def sec(self):  # seconds since midnight
        return self.start.hour * 3600 + self.start.minute * 60 + self.start.second

    @property
    def label(self):
        return self.cam

    @property
    def name(self):
        return os.path.basename(self.path)


def list_cameras(root):
    """Each sub-folder of the root is a camera: <root>/<Camera>/YYYY/MM/DD/."""
    try:
        return sorted(e.name for e in os.scandir(root)
                      if e.is_dir() and not e.name.startswith("."))
    except OSError:
        return []


def load_day(root, day):
    if not Path(root).is_dir():
        raise FileNotFoundError(f"Folder not found: {root}")
    clips = []
    for camera in list_cameras(root):
        folder = Path(root) / camera / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
        if not folder.is_dir():
            continue
        for e in os.scandir(folder):
            m = FILE_RE.match(e.name)
            if not m:
                continue
            try:
                start = dt.datetime.strptime(m["ts"], "%Y%m%d%H%M%S")
                st = e.stat()
            except (ValueError, OSError):
                continue
            # FTP upload finishes at the end of the clip, so mtime - start ~ duration
            dur = st.st_mtime - start.timestamp()
            if not 5 <= dur <= MAX_DUR:
                dur = 60
            clips.append(Clip(e.path, camera, m["ch"], start, dur, st.st_size, st.st_mtime))
    clips.sort(key=lambda c: (c.start, c.cam))
    return clips


def fmt_dur(s):
    s = int(s)
    return f"{s // 60}:{s % 60:02d}"


def fmt_size(b):
    return f"{b / 1048576:.1f} MB"


def fmt_bytes(b):
    return f"{b / 1073741824:.2f} GB" if b >= 1073741824 else f"{b / 1048576:.1f} MB"


def clip_at(clips, sec, strict=False):
    """Return (index, offset-in-clip) for a second of the day.

    A clip that really contains `sec` always wins. Otherwise the nearest clip is
    used if it is close enough (a clip can be under a pixel wide on the timeline):
    strict=True (hover preview) allows MIN_SEG/2 seconds, strict=False (click) 15 minutes.
    """
    best, best_d = None, None
    for i, c in enumerate(clips):
        end = c.sec + c.dur
        if c.sec <= sec <= end:
            return i, sec - c.sec
        d = c.sec - sec if sec < c.sec else sec - end
        if best_d is None or d < best_d:
            best, best_d = i, d
    if best is not None and best_d <= (MIN_SEG / 2 if strict else 900):
        return best, 0
    return None, 0


# ----------------------------------------------------------------- ffmpeg ---
_procs = set()
_procs_lock = threading.Lock()


def run_ffmpeg(cmd, timeout=60):
    """Run ffmpeg, tracking the process so it can be killed on exit."""
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, errors="replace", **NO_WINDOW)
    except OSError as e:
        return -1, "", str(e)
    with _procs_lock:
        _procs.add(p)
    try:
        out, err = p.communicate(timeout=timeout)
        return p.returncode, out, err
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        return -1, "", "timeout"
    finally:
        with _procs_lock:
            _procs.discard(p)


def kill_ffmpeg():
    with _procs_lock:
        procs = list(_procs)
    for p in procs:
        try:
            p.kill()
        except OSError:
            pass


# ------------------------------------------------------------- thumbnails ---
def thumb_path(clip, offset):
    key = hashlib.sha1(f"{clip.path}|{clip.size}|{int(clip.mtime)}|{offset}|{THUMB_W}".encode()).hexdigest()[:20]
    return THUMB_DIR / f"{key}.png"


def make_thumb(clip, offset):
    """Extract one frame at `offset` seconds as a small PNG. Returns Path or None."""
    out = thumb_path(clip, offset)
    if out.exists():
        return out
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".tmp.png")
    for off in dict.fromkeys((offset, 0)):      # retry from the start if offset is past the end
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
               "-ss", str(off), "-i", clip.path, "-frames:v", "1",
               "-vf", f"scale={THUMB_W}:-2", "-y", str(tmp)]
        rc, _, _ = run_ffmpeg(cmd, 30)
        if rc == 0 and tmp.exists() and tmp.stat().st_size > 0:
            tmp.replace(out)
            return out
        tmp.unlink(missing_ok=True)
    return None


def prune_thumbs(max_age_days=14):
    try:
        cutoff = time.time() - max_age_days * 86400
        for f in THUMB_DIR.glob("*.png"):
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
    except OSError:
        pass


def clear_thumbs():
    """Delete every cached thumbnail. Returns (files, bytes)."""
    n = size = 0
    try:
        for f in THUMB_DIR.glob("*.png"):
            try:
                size += f.stat().st_size
                f.unlink()
                n += 1
            except OSError:
                pass
    except OSError:
        pass
    return n, size


# ----------------------------------------------------------------- motion ---
def load_motion_cache():
    try:
        return json.loads(MOTION_FILE.read_text())
    except Exception:
        return {}


def save_motion_cache(cache):
    try:
        if len(cache) > 50000:
            cache = dict(list(cache.items())[-30000:])
        MOTION_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = MOTION_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache))
        tmp.replace(MOTION_FILE)
    except OSError:
        pass


def analyze_motion(path):
    """Scene-change score from key frames only (fast). Returns float or None."""
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
           "-skip_frame", "nokey", "-i", path, "-an",
           "-vf", "scale=160:-2,select='gte(scene,0)',metadata=print:file=-",
           "-f", "null", "-"]
    rc, out, _ = run_ffmpeg(cmd, 300)
    scores = [float(m.group(1)) for m in re.finditer(r"lavfi\.scene_score=([0-9.eE+-]+)", out)]
    if rc != 0 or len(scores) < 2:
        return None
    scores = sorted(scores[1:], reverse=True)       # first frame has nothing to compare with
    top = scores[:max(1, len(scores) // 4)]         # busiest quarter: short events count
    return sum(top) / len(top)


def score_clips(clips, factor):
    """Set ratio/heavy on every clip, relative to each camera's typical clip."""
    by_cam = {}
    for c in clips:
        by_cam.setdefault(c.cam, []).append(c)
    for group in by_cam.values():
        rates = [c.size / max(c.dur, 1.0) for c in group]
        med_rate = statistics.median(rates) or 1.0
        analysed = [c.motion for c in group if c.motion is not None]
        med_motion = max(statistics.median(analysed), 0.005) if analysed else None
        for c, rate in zip(group, rates):
            if c.motion is not None and med_motion:
                c.ratio, c.src = c.motion / med_motion, "scene"
                c.heavy = c.ratio >= factor and c.motion >= MIN_SCENE
            else:
                c.ratio, c.src = rate / med_rate, "rate"
                c.heavy = c.ratio >= factor


# ---------------------------------------------------------------- cleanup ---
def _numeric_dirs(path, width):
    try:
        return sorted(Path(e.path) for e in os.scandir(path)
                      if e.is_dir() and e.name.isdigit() and len(e.name) == width)
    except OSError:
        return []


def scan_old(root, cutoff):
    """Recordings in <root>/<Camera>/YYYY/MM/DD folders dated before `cutoff`.
    Returns [(path, size, day, camera)]. Only files matching the camera's
    naming pattern are listed."""
    items = []
    for cam in list_cameras(root):
        for y in _numeric_dirs(Path(root) / cam, 4):
            for m in _numeric_dirs(y, 2):
                for d in _numeric_dirs(m, 2):
                    try:
                        day = dt.date(int(y.name), int(m.name), int(d.name))
                    except ValueError:
                        continue
                    if day >= cutoff:
                        continue
                    try:
                        for e in os.scandir(d):
                            if e.is_file() and FILE_RE.match(e.name):
                                items.append((e.path, e.stat().st_size, day, cam))
                    except OSError:
                        continue
    return items


def delete_files(items, progress=None, stop=None):
    """Delete the listed (path, size, day, camera) tuples, then remove
    day/month/year folders left empty. Returns (deleted, errors, bytes_freed)."""
    deleted = errors = freed = 0
    dirs = set()
    total = len(items)
    for i, (path, size, _day, _cam) in enumerate(items, 1):
        if stop is not None and stop.is_set():
            break
        try:
            os.remove(path)
            deleted += 1
            freed += size
            dirs.add(os.path.dirname(path))
        except OSError:
            errors += 1
        if progress and (i % 10 == 0 or i == total):
            progress((i, total))
    for d in sorted(dirs):
        p = Path(d)
        for _ in range(3):                  # day, month, year (never the camera folder)
            try:
                p.rmdir()
            except OSError:
                break
            p = p.parent
    return deleted, errors, freed


# --------------------------------------------------------------- download ---
class Cancelled(Exception):
    pass


def unique_dest(dest_dir, name):
    """dest_dir/name, or 'name (1).ext', 'name (2).ext'... if it already exists."""
    p = Path(dest_dir) / name
    if not p.exists():
        return p
    i = 1
    while True:
        q = p.with_name(f"{p.stem} ({i}){p.suffix}")
        if not q.exists():
            return q
        i += 1


def copy_file(src, dest_dir, on_bytes=None, cancel=None, chunk=1 << 20):
    """Copy src into dest_dir without overwriting. Writes name.part first and
    renames when complete, so an interrupted copy never looks like a real
    recording. Keeps the original modification time. Returns the new Path."""
    dst = unique_dest(dest_dir, os.path.basename(src))
    part = dst.with_name(dst.name + ".part")
    try:
        with open(src, "rb") as fi, open(part, "wb") as fo:
            while True:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                buf = fi.read(chunk)
                if not buf:
                    break
                fo.write(buf)
                if on_bytes:
                    on_bytes(len(buf))
        st = os.stat(src)
        part.replace(dst)
        try:
            os.utime(dst, (st.st_atime, st.st_mtime))
        except OSError:
            pass
    except BaseException:
        try:
            part.unlink()
        except OSError:
            pass
        raise
    return dst


def copy_files(paths, dest_dir, progress=None, cancel=None):
    """Copy several files. progress((done_bytes, total_bytes, filename)) is
    throttled to ~10 calls/second. Returns (copied_paths, errors, cancelled)
    where errors is [(path, message)]."""
    sizes = []
    for p in paths:
        try:
            sizes.append(os.path.getsize(p))
        except OSError:
            sizes.append(0)
    total = sum(sizes)
    state = {"done": 0, "name": "", "t": 0.0}

    def on_bytes(n):
        state["done"] += n
        now = time.monotonic()
        if progress and now - state["t"] >= 0.1:
            state["t"] = now
            progress((state["done"], total, state["name"]))

    copied, errors, cancelled = [], [], False
    for p in paths:
        if cancel is not None and cancel.is_set():
            cancelled = True
            break
        state["name"] = os.path.basename(p)
        try:
            copied.append(str(copy_file(p, dest_dir, on_bytes, cancel)))
        except Cancelled:
            cancelled = True
            break
        except OSError as e:
            errors.append((p, str(e)))
    if progress:
        progress((state["done"], total, state["name"]))
    return copied, errors, cancelled


# --------------------------------------------------------------- playback ---
def build_mpv_cmd(clips, index, offset=0, continuous=True):
    cmd = ["mpv", "--force-window=yes", "--osd-level=1", "--keep-open=no"]
    first = clips[index].path
    rest = [c.path for c in clips[index + 1:]] if continuous else []
    if offset > 0:
        cmd += ["--{", f"--start={int(offset)}", first, "--}"]
    else:
        cmd += [first]
    return cmd + rest


def play(clips, index, offset=0, continuous=True):
    """Start mpv. Raises RuntimeError if mpv is not installed."""
    if not shutil.which("mpv"):
        raise RuntimeError("mpv not found.\nInstall it with:  sudo apt install mpv")
    subprocess.Popen(build_mpv_cmd(clips, index, offset, continuous),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **NO_WINDOW)
