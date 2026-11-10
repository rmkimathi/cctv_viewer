# CCTV Timeline - Code walkthrough Tutorial

This tutorial walks through a complete desktop application for browsing Reolink-style FTP camera recordings. We'll examine the architecture, design decisions, and key implementation details of the two-file codebase: `cctv_core.py` (GUI-independent logic) and `cctv_viewer.py` (PySide6 interface).

---

## 1. The Problem and the Data Layout

IP cameras upload video clips to an FTP server in a predictable hierarchy:

```
<root>/<Camera>/YYYY/MM/DD/CCTV1_00_20261021000349.mp4
```

Each filename encodes a channel (`00`) and a start timestamp (`20261021000349`). The application's job is to turn that pile of files into a browsable, clickable day timeline.

The key architectural decision is **separating the logic from the UI**. `cctv_core.py` has no PySide6 imports — it can be tested headlessly, and the GUI layer stays thin.

---

## 2. The Core Data Model

### The `Clip` class

```python
class Clip:
    __slots__ = ("path", "cam", "ch", "start", "dur", "size", "mtime",
                 "motion", "ratio", "heavy", "src")
```

`__slots__` avoids per-instance `__dict__`, which matters when loading thousands of clips per day. The class carries both raw facts (`start`, `dur`, `size`) and derived analysis state (`motion`, `ratio`, `heavy`, `src`).

The `sec` property converts the datetime to seconds-since-midnight — the coordinate system for the timeline:

```python
@property
def sec(self):  # seconds since midnight
    return self.start.hour * 3600 + self.start.minute * 60 + self.start.second
```

### Inferring duration without parsing the file

Opening every MP4 to read metadata is slow. Instead, `load_day` exploits an FTP behavior: **the file's modification time is when the upload finished**, i.e. the clip's end.

```python
# FTP upload finishes at the end of the clip, so mtime - start ~ duration
dur = st.st_mtime - start.timestamp()
if not 5 <= dur <= MAX_DUR:
    dur = 60
```

The sanity bounds (`5` seconds to `MAX_DUR = 3600`) guard against clock skew or odd files.

### Hit-testing: `clip_at`

On a 24-hour timeline, a 10-second clip is well under one pixel wide. Clicking exactly on it is impossible, so `clip_at` returns the *nearest* clip within a tolerance:

```python
def clip_at(clips, sec, strict=False):
    ...
    if c.sec <= sec <= end:
        return i, sec - c.sec          # exact hit always wins
    ...
    if best is not None and best_d <= (MIN_SEG / 2 if strict else 900):
        return best, 0
```

- `strict=True` (hover) allows `MIN_SEG/2 = 75` seconds.
- `strict=False` (click) allows up to 15 minutes of slack.

---

## 3. Running ffmpeg Safely

### Process tracking and cleanup

Every ffmpeg invocation goes through `run_ffmpeg`, which registers the process in a global set:

```python
_procs = set()
_procs_lock = threading.Lock()

def run_ffmpeg(cmd, timeout=60):
    p = subprocess.Popen(...)
    with _procs_lock:
        _procs.add(p)
    try:
        out, err = p.communicate(timeout=timeout)
        return p.returncode, out, err
    except subprocess.TimeoutExpired:
        p.kill()
        ...
    finally:
        with _procs_lock:
            _procs.discard(p)
```

`kill_ffmpeg()` then lets the GUI's `closeEvent` kill any stragglers. Without this, an abandoned thumbnail extraction could outlive the window.

### Suppressing console windows on Windows

```python
NO_WINDOW = ({"creationflags": subprocess.CREATE_NO_WINDOW}
             if sys.platform == "win32" else {})
```

On Windows, a GUI app spawning a console program flashes a black window. `CREATE_NO_WINDOW` suppresses it; on Linux the dict is empty.

---

## 4. Thumbnails: Content-Addressed Cache

Thumbnails are extracted with a single ffmpeg call:

```python
cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
       "-ss", str(off), "-i", clip.path, "-frames:v", "1",
       "-vf", f"scale={THUMB_W}:-2", "-y", str(tmp)]
```

Placing `-ss` **before** `-i` enables fast seeking (ffmpeg jumps to the nearest keyframe). The `-2` in the scale filter keeps the aspect ratio while forcing an even height (required by many codecs).

The cache key hashes the file's identity **and** its size/mtime:

```python
def thumb_path(clip, offset):
    key = hashlib.sha1(f"{clip.path}|{clip.size}|{int(clip.mtime)}|{offset}|{THUMB_W}".encode()).hexdigest()[:20]
    return THUMB_DIR / f"{key}.png"
```

If a file is re-uploaded or changed, the key changes automatically. A `.tmp.png` intermediate plus `tmp.replace(out)` makes the write atomic — a half-written PNG is never served.

Note the retry logic: if the requested offset is past the end of a clip (common when duration is estimated), it falls back to offset `0`:

```python
for off in dict.fromkeys((offset, 0)):
```

`dict.fromkeys` both deduplicates and preserves order.

---

## 5. Motion Analysis Without Decoding Everything

### The heuristic

To detect "busy" clips, the core asks ffmpeg for **scene-change scores**, but only from keyframes:

```python
cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
       "-skip_frame", "nokey", "-i", path, "-an",
       "-vf", "scale=160:-2,select='gte(scene,0)',metadata=print:file=-",
       "-f", "null", "-"]
```

- `-skip_frame nokey` decodes only keyframes — typically a few dozen frames per clip instead of thousands.
- `scale=160:-2` shrinks frames so scene analysis is cheap.
- `metadata=print:file=-` writes the scores to stdout, where a regex picks them out.

### Aggregating scores

```python
scores = sorted(scores[1:], reverse=True)       # first frame has nothing to compare with
top = scores[:max(1, len(scores) // 4)]         # busiest quarter: short events count
return sum(top) / len(top)
```

The first frame is dropped (there's no previous frame to diff against). Averaging the top quartile — rather than the max — avoids a single global lighting change dominating the score, while still letting brief bursts register.

### Relative scoring

Motion scores aren't comparable across cameras. `score_clips` normalizes per camera:

```python
for group in by_cam.values():
    med_rate = statistics.median(rates) or 1.0
    med_motion = max(statistics.median(analysed), 0.005) if analysed else None
    for c, rate in zip(group, rates):
        if c.motion is not None and med_motion:
            c.ratio, c.src = c.motion / med_motion, "scene"
            c.heavy = c.ratio >= factor and c.motion >= MIN_SCENE
        else:
            c.ratio, c.src = rate / med_rate, "rate"
            c.heavy = c.ratio >= factor
```

The fallback is **bytes per second**: `c.size / max(c.dur, 1.0)`. A static scene compresses well; an active scene doesn't. This lets the UI flag busy clips immediately, before any analysis, with `src = "rate"` marking it as an estimate. The UI displays this distinction:

```python
return f"{c.ratio:.1f}×{'' if c.src == 'scene' else '~'}{' ★' if c.heavy else ''}"
```

The tilde (`~`) means "estimated from file size."

### Caching analysis results

Results persist in `motion.json`, keyed by path with size/mtime validation:

```python
ent = self.motion_cache.get(c.path)
if ent and ent[0] == c.size and abs(ent[1] - c.mtime) < 1:
    c.motion = ent[2]
```

`save_motion_cache` trims the dict before writing and uses an atomic `.tmp` + `replace` — important because this file is written on every app close and could be corrupted by a crash.

---

## 6. The GUI Layer: Threading Model

### The Dispatcher

PySide6 worker threads must not touch widgets. The `Dispatcher` solves this with signals that cross back to the GUI thread:

```python
class Dispatcher(QObject):
    _prog = Signal(int, object)
    _fin = Signal(int, object, object)

    def submit(self, pool, fn, *args, on_done=None, on_progress=None, cancel=None):
        jid = self._next
        self._next += 1
        self._cbs[jid] = (on_done, on_progress)
        pool.start(_Job(self, jid, fn, args, cancel))
        return cancel
```

Because `Dispatcher` lives in the GUI thread, its slots (`_on_prog`, `_on_fin`) execute there — so callbacks can safely modify widgets. Each job gets an ID so results are matched to the right callback.

The worker wrapper catches everything:

```python
class _Job(QRunnable):
    def run(self):
        result = error = None
        try:
            result = self._fn(self._ctx, *self._args)
        except BaseException as e:
            error = e
        try:
            self._disp._fin.emit(self._jid, result, error)
        except RuntimeError:                # dispatcher already destroyed
            pass
```

The inner `RuntimeError` guard handles the case where the app is closing while a job finishes.

### The context object

Every job receives a `_Ctx` giving it two things: `progress(payload)` and `cancel` (a `threading.Event`):

```python
def __init__(self, disp, jid, cancel):
    self._disp, self._jid, self.cancel = disp, jid, cancel
```

This is a clean contract: a job can report progress and check for cancellation, and nothing else.

### Pool separation

The window creates four thread pools with different sizes:

```python
self.pool_misc, self.pool_xfer = make_pool(2), make_pool(1)
self.pool_thumb, self.pool_motion = make_pool(2), make_pool(2)
```

- `pool_xfer` has **one** thread so downloads are serialized (and cancellation is clean).
- `pool_motion` allows parallel ffmpeg analyses.
- `pool_thumb` is separate so previews stay responsive during a long analysis.
- `pool_misc` handles loads, scans, and deletes.

---

## 7. The Timeline Widget

### Custom painting

`TimelineWidget.paintEvent` draws the 24-hour bar manually:

```python
for heavy_pass in (False, True):            # busy clips last so they sit on top
    for c in self.clips:
        is_heavy = self.highlight and c.heavy
        if is_heavy != heavy_pass:
            continue
        x0 = c.sec / DAY_SECONDS * w
        bw = max(c.dur / DAY_SECONDS * w, 3)
```

The two-pass loop is a z-ordering trick: busy clips are drawn in the second pass, so a one-pixel red line isn't buried under neighboring normal clips.

`max(..., 3)` guarantees every clip is at least 3 pixels wide — clickable even if technically invisible in time.

### Adaptive hour labels

The label density adapts to width:

```python
step = next((s for s in (1, 2, 3, 4, 6) if w / 24 * s >= fm.horizontalAdvance("00") + 12), 6)
```

Pick the smallest hour-step whose spacing still fits two digits plus padding.

### Hover and click signals

The widget emits pure `float` seconds — it knows nothing about clips:

```python
hovered = Signal(float)
clicked = Signal(float)
context = Signal(float, QPoint)
```

The window then resolves those seconds to a clip via `clip_at`. This keeps the widget generic and testable.

### Debounced hover previews

Hovering fires `hovered` on every pixel move. Extracting a thumbnail each time would spawn hundreds of ffmpeg calls. Instead, a single-shot timer debounces:

```python
self.thumb_timer.start(150)             # wait until the pointer settles
```

And `request_thumb` snaps the offset to a 10-second grid:

```python
off = int(off) // 10 * 10
```

Together these mean at most one extraction per settled hover position. A generation counter guards against stale results:

```python
key = (c.path, off)
if key == self.thumb_want:
    return
```

When the async result arrives, it's discarded unless it still matches `thumb_want`.

---

## 8. Loading and Cancellation via Generations

Every `refresh()` increments a generation counter:

```python
def refresh(self):
    ...
    self.load_gen += 1
    self.an_gen += 1
    gen, day = self.load_gen, self.day
    ...
    self.disp.submit(self.pool_misc, work,
                     on_done=lambda res, err: self._loaded(gen, root, day, res, err))
```

The completion handler bails if the generation has moved on:

```python
def _loaded(self, gen, root, day, res, err):
    if gen != self.load_gen:
        return
```

This is a lightweight alternative to cancellation events for loads — flipping a day repeatedly just orphans the earlier jobs, whose results are ignored. Analysis uses the same pattern (`an_gen`) so a refresh cancels a running motion analysis without killing the ffmpeg processes (they finish, and their results are dropped).

---

## 9. Safe File Operations

### Atomic copies

`copy_file` writes to a `.part` sibling and renames on success:

```python
part = dst.with_name(dst.name + ".part")
try:
    with open(src, "rb") as fi, open(part, "wb") as fo:
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            buf = fi.read(chunk)
            ...
    st = os.stat(src)
    part.replace(dst)
    os.utime(dst, (st.st_atime, st.st_mtime))
except BaseException:
    try:
        part.unlink()
    except OSError:
        pass
    raise
```

Three things happen here:

1. **No overwrites**: `unique_dest` generates `name (1).ext`, `name (2).ext`, etc.
2. **No partial files that look complete**: an interrupted copy leaves only a `.part`, which any consumer can recognize and ignore.
3. **Preserved timestamps**: `os.utime` keeps the original mtime — critical, since the app relies on mtime for duration inference.

The `except BaseException` (not just `Exception`) ensures cleanup even on `Cancelled` or `KeyboardInterrupt`.

### Throttled progress

Copying every 1 MB chunk and reporting each time would flood the signal queue. `copy_files` throttles to ~10 updates/second:

```python
def on_bytes(n):
    state["done"] += n
    now = time.monotonic()
    if progress and now - state["t"] >= 0.1:
        state["t"] = now
        progress((state["done"], total, state["name"]))
```

`time.monotonic()` (not `time.time()`) is immune to system clock changes.

### Empty-folder cleanup

After deleting files, `delete_files` removes now-empty day/month/year folders — but stops before the camera folder:

```python
for d in sorted(dirs):
    p = Path(d)
    for _ in range(3):                  # day, month, year (never the camera folder)
        try:
            p.rmdir()
        except OSError:
            break
        p = p.parent
```

At most three levels up, and `rmdir` fails silently if the folder isn't empty.

---

## 10. Frameless Dialogs with System Move

The About and Delete dialogs use `Qt.FramelessWindowHint`, but dragging must still work. Instead of manually tracking mouse offsets, the app asks the window manager:

```python
def _start_move(widget):
    wh = widget.window().windowHandle()
    if wh is not None:
        wh.startSystemMove()
```

`startSystemMove()` hands the drag to the compositor, so snapping and multi-monitor behavior work as expected. The title bar's label is made click-through so it doesn't eat the drag:

```python
lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
```

---

## 11. Sorting a Table by Hidden Keys

The clips table sorts by `HH:MM:SS` text, which would sort fine lexicographically — but the Motion column ("1.5× ★") would not. `SortItem` stores a numeric sort key separately:

```python
class SortItem(QTableWidgetItem):
    def __lt__(self, other):
        a, b = self.data(ROLE_SORT), other.data(ROLE_SORT)
        try:
            return a < b
        except TypeError:
            return super().__lt__(other)
```

The `TypeError` fallback covers comparing unlike types (e.g. `None` with a number), avoiding a crash mid-sort.

Repopulation must temporarily disable sorting, and selection is preserved by path:

```python
keep = {c.path for c in self.selected_clips()}
t.setSortingEnabled(False)
...
t.setSortingEnabled(True)
if keep:
    ...select rows whose path is in keep...
```

Without this, re-sorting after a repopulate would scramble the visible selection.

---

## 12. The `clip_at` + Play flow

The timeline click handler demonstrates the layering nicely:

```python
def on_tl_click(self, sec):
    idx, off = clip_at(self.shown, sec)
    ...
    c = self.shown[idx]
    self.hint.setText(f"Playing {c.name} from +{fmt_dur(off)}")
    self.play_clip(c, off)
```

The widget reported seconds; the core resolved them to `(index, offset)`; the window looked up the clip and handed it to `play_clip`, which builds an mpv command:

```python
def build_mpv_cmd(clips, index, offset=0, continuous=True):
    cmd = ["mpv", "--force-window=yes", "--osd-level=1", "--keep-open=no"]
    first = clips[index].path
    rest = [c.path for c in clips[index + 1:]] if continuous else []
    if offset > 0:
        cmd += ["--{", f"--start={int(offset)}", first, "--}"]
    else:
        cmd += [first]
    return cmd + rest
```

The `--{ ... --}` syntax scopes an option to a single file — here, `--start` applies only to the clicked clip. The remaining clips are passed as bare arguments, so mpv plays them in sequence (the "keep playing next clips" option).

---

## 13. Shutdown Order

The `closeEvent` teardown shows the care needed with background work:

```python
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
    for p in (...):
        p.waitForDone(3000)
    super().closeEvent(e)
```

Order matters:

1. Bump generations so late results are ignored.
2. `pool.clear()` drops queued (not yet started) jobs.
3. Signal cancellation so running jobs stop cleanly.
4. `kill_ffmpeg()` kills external processes the cancel flags can't reach.
5. Save state.
6. `waitForDone(3000)` — but only for 3 seconds, so a hung job doesn't block quitting.

---

## 14. Putting It Together

The design principles worth carrying into your own projects:

| Principle | Implementation |
|---|---|
| Keep logic GUI-free | `cctv_core.py` imports no PySide6 |
| Never block the UI thread | `Dispatcher` + `QThreadPool` + signal handoff |
| Make stale results harmless | Generation counters (`load_gen`, `an_gen`) |
| Make cancellation possible everywhere | `threading.Event` in every job's context |
| Fail atomically on disk | `.part` files, atomic `replace`, no overwrites |
| Cache keyed by content identity | SHA-1 of path + size + mtime |
| Estimate first, analyze later | Bytes/second heuristic, then ffmpeg scene scores |
| Let the compositor do window moves | `startSystemMove()` |

### Try it out

```bash
sudo apt install mpv ffmpeg gvfs-backends
pip install PySide6
python3 cctv_viewer.py
```

Point the Folder field at the directory containing your camera subfolders, and the timeline should populate for today. Add `Tools → Delete old recordings…` to your workflow and the FTP share will never fill up again.