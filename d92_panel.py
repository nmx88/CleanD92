#!/usr/bin/env python3
"""
d92_panel.py -- control panel for the MiraBox StreamDock D92.

Run this (or `python d92_app.py --web`), then open http://127.0.0.1:8092 on
this PC or the printed LAN URL on a phone on the same Wi-Fi. The server
listens on all interfaces when started this way; there is no login, so keep
it on a trusted network.

Requires d92.py in the same folder, plus:  pip install hidapi pillow psutil

ARCHITECTURE -- this is not decoration, it is what keeps the panel alive:

  One process owns the device. The HID handle is opened exactly once at
  startup and held until exit. A single render thread does every write. The
  HTTP server only mutates shared state; it never touches the device.

  Reopening the handle within one physical USB connection is the confirmed
  #1 cause of a permanently black panel, so there is deliberately no reopen
  path anywhere in here. If a write fails the stream stops, the UI says so,
  and you physically replug then quit and open CleanD92 again.

  The render thread also never goes idle -- it pushes a frame every interval
  whether or not anything changed, which is the second hard requirement of
  this device.

GEOMETRY -- why rotation is restricted to 90/270:

  The DRA frame header carries no width/height fields, so the device infers
  geometry from the JPEG itself, and it only accepts its own portrait shape.
  Authoring happens on a landscape canvas (1920x462) which becomes 462x1920
  after a 90 or 270 degree rotation; sending an unrotated landscape frame
  instead makes the device stop accepting writes.

  That matters more than it sounds, because hidapi's Windows write blocks in
  GetOverlappedResult with no timeout -- if the device stalls, the write
  never returns and the render thread hangs with it. (The C# reference this
  was ported from used a 2000 ms WaitForSingleObject; hidapi exposes no
  equivalent.) So rather than recover from a stall, check_geometry below
  refuses to send anything that could cause one.

Animation note: frame advance follows the wall clock, so a GIF plays at its
real speed but only as smoothly as the push interval allows. At the default
350 ms that is ~3 fps. Lowering the interval helps, at the cost of more USB
dropouts -- 350 ms is the value verified safe upstream.
"""

import io
import json
import math
import os
import random
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image, ImageDraw, ImageSequence

import d92_layout as layout
from d92 import D92, D92Error, load_font

if getattr(sys, "frozen", False):
    # PyInstaller: __file__ points inside the temporary unpack directory, so
    # the media folder has to be resolved against the exe the user launched.
    HERE = os.path.dirname(os.path.abspath(sys.executable))
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
MEDIA_DIR = os.path.join(HERE, "media")
SETTINGS_PATH = os.path.join(HERE, "settings.json")
# Survives restart. Kept small on purpose -- layouts already live in presets/.
SETTINGS_KEYS = ("weather_place", "weather_country", "weather_units",
                 "weather_lang", "notify_mirror")
EXTENSIONS = (".gif", ".png", ".jpg", ".jpeg", ".bmp", ".webp")
# Soft ceiling for a drop / import. Longer GIFs are fine once they live in
# media/; this only stops someone dragging a multi-GB file onto the window.
MAX_IMPORT_BYTES = 80 * 1024 * 1024

HOST = "127.0.0.1"         # loopback print address / legacy
WEB_HOST = "0.0.0.0"       # --web listens on all interfaces (phone on LAN)
PORT = 8092

STALL_WARN = 3.0          # seconds; a frame slower than this is suspicious
FRAME_BUDGET = 400        # JPEG-compressed frames kept across all files
MAX_FRAMES = 150          # per file; longer animations are subsampled

state_lock = threading.Lock()

state = {
    "mode": "clock",          # "clock" | "media" | "slideshow"
    "media": None,            # filename inside ./media, for "media" mode
    "brightness": 50,         # 0..100
    "interval_ms": 350,       # >=100; 350 is the verified-safe cadence
    "layout": "horizontal",   # "horizontal" | "vertical" -- see GEOMETRY
    "flip": False,            # rotate the content 180 degrees
    "canvas_w": 1920,         # panel long edge
    "canvas_h": 462,          # panel short edge
    "fit": "cover",           # "cover" | "letterbox"
    "quality": 85,
    "slide_seconds": 8,       # dwell time per file in slideshow mode
    "slide_shuffle": False,
    "show_gpu": False,        # opt-in: LHM is external and often not running
    "lhm_url": "http://localhost:8085/data.json",
    # Open-Meteo, no API key. Place is free text; country biases geocoding
    # toward Greece when the name is ambiguous (e.g. "Athens").
    "weather_place": "Galatsi",
    "weather_country": "GR",
    "weather_units": "C",     # "C" | "F"
    "weather_lang": "el",     # "el" | "en" -- geocode + weekday labels
    "color_bg": "#080a0e",
    # The layout: a list of positioned items. See d92_layout for the schema.
    "items": layout.normalise_all(layout.DEFAULT_PRESETS["Wide dashboard"]),
    "preset": "Wide dashboard",
    # Draft mode: keep building frames and refreshing the preview, but keep
    # re-sending the last frame the panel already has. Lets you audition a
    # file or drag a layout about without the panel changing, and without
    # letting the OUT endpoint go idle -- which is what blacks it out.
    "hold": False,
    # Toast mirroring needs sparse package identity + user consent.
    "notify_mirror": False,
}

runtime = {
    "status": "starting",     # starting | streaming | error
    "message": "",
    "frames": 0,
    "last_ms": 0,
    "reload_media": True,
    "apply_brightness": True,
    "wake": False,            # ask the render thread to replay DIS -> LIG
    "stop": False,            # ask the render thread to finish and return
    "apply_once": False,      # push one fresh frame even while holding
    "boxes": {},              # item id -> pixel box, for the layout editor
    "box_canvas": (0, 0),     # canvas size those boxes were measured on
}


# ------------------------------------------------------------------ media

def list_media():
    if not os.path.isdir(MEDIA_DIR):
        return []
    return sorted(n for n in os.listdir(MEDIA_DIR)
                  if n.lower().endswith(EXTENSIONS))


# Render loop must not hit the disk every tick (AGENTS 2.2). A short TTL
# listing is refreshed from the loop when a slideshow needs a playlist and
# when coaching the empty-folder case -- not 3 times a second.
_media_list = {"t": 0.0, "names": []}
_MEDIA_LIST_TTL = 2.0


def list_media_cached(force=False):
    now = time.monotonic()
    if (not force and _media_list["names"] is not None
            and now - _media_list["t"] < _MEDIA_LIST_TTL):
        return list(_media_list["names"])
    names = list_media()
    _media_list["t"] = now
    _media_list["names"] = names
    return list(names)


def invalidate_media_list():
    _media_list["t"] = 0.0


def import_media_file(path):
    """Copy an external image/GIF into MEDIA_DIR. Returns the stored filename.

    Runs wherever the caller puts it -- never from the render thread. Used by
    drag-and-drop and by the optional ffmpeg converter. Refuses unknown
    extensions and oversized files rather than filling the disk."""
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise ValueError("not a file: %s" % path)
    ext = os.path.splitext(path)[1].lower()
    if ext not in EXTENSIONS:
        raise ValueError("unsupported type %s (want %s)" % (
            ext or "(none)", ", ".join(EXTENSIONS)))
    size = os.path.getsize(path)
    if size > MAX_IMPORT_BYTES:
        raise ValueError("file is %.0f MB; limit is %d MB" % (
            size / (1024 * 1024), MAX_IMPORT_BYTES // (1024 * 1024)))
    os.makedirs(MEDIA_DIR, exist_ok=True)
    base = os.path.basename(path)
    stem, extension = os.path.splitext(base)
    # Keep names filesystem-safe and short enough for the filename readout.
    safe = "".join(c for c in stem if c.isalnum() or c in " ._-+()[]")
    safe = (safe or "media").strip(" .")[:80]
    name = safe + extension.lower()
    dest = os.path.join(MEDIA_DIR, name)
    n = 1
    while os.path.exists(dest):
        # Same path dropped twice: reuse rather than cloning duplicates.
        try:
            if os.path.samefile(path, dest):
                return name
        except OSError:
            pass
        name = "%s_%d%s" % (safe, n, extension.lower())
        dest = os.path.join(MEDIA_DIR, name)
        n += 1
        if n > 999:
            raise ValueError("too many copies of %s already in media/" % safe)
    import shutil
    shutil.copy2(path, dest)
    invalidate_media_list()
    return name


CLIP_EXTENSIONS = (".mp4", ".mov", ".mkv", ".webm", ".avi")
MAX_CLIP_SECONDS = 12
MAX_CLIP_BYTES = 100 * 1024 * 1024
# Cover-fit to the panel strip, 12 fps, 128-colour palette -- matches the
# recipe in the README. Anything longer is truncated; the panel only shows
# ~3 fps at the default interval anyway.
_FFMPEG_VF = (
    "fps=12,"
    "scale=1920:462:force_original_aspect_ratio=increase,"
    "crop=1920:462,"
    "split[a][b];[a]palettegen=max_colors=128[p];"
    "[b][p]paletteuse=dither=bayer"
)


def find_ffmpeg():
    """Path to a system ffmpeg, or None. Never bundled with the exe."""
    import shutil
    return shutil.which("ffmpeg")


def convert_clip_to_gif(path, seconds=MAX_CLIP_SECONDS):
    """Transcode a short video into media/*.gif via system ffmpeg.

    Returns the stored GIF filename. Raises ValueError/RuntimeError with a
    plain message the UI can show. Must not run on the render thread."""
    import shutil
    import subprocess
    import tempfile

    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise ValueError("not a file: %s" % path)
    ext = os.path.splitext(path)[1].lower()
    if ext not in CLIP_EXTENSIONS:
        raise ValueError("unsupported clip type %s (want %s)" % (
            ext or "(none)", ", ".join(CLIP_EXTENSIONS)))
    size = os.path.getsize(path)
    if size > MAX_CLIP_BYTES:
        raise ValueError("clip is %.0f MB; limit is %d MB" % (
            size / (1024 * 1024), MAX_CLIP_BYTES // (1024 * 1024)))
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError(
            "ffmpeg was not found on PATH. Install it from "
            "https://ffmpeg.org and reopen CleanD92, or convert the clip "
            "yourself and drop the GIF onto the preview.")

    try:
        seconds = max(1, min(MAX_CLIP_SECONDS, int(seconds)))
    except (TypeError, ValueError):
        seconds = MAX_CLIP_SECONDS

    stem = os.path.splitext(os.path.basename(path))[0]
    safe = "".join(c for c in stem if c.isalnum() or c in " ._-+()[]")
    safe = (safe or "clip").strip(" .")[:60]
    os.makedirs(MEDIA_DIR, exist_ok=True)
    # Write to a temp file first so a failed ffmpeg run cannot leave a
    # truncated GIF that the media loader would then try to decode.
    fd, tmp = tempfile.mkstemp(prefix="cleand92_", suffix=".gif")
    os.close(fd)
    try:
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-y", "-ss", "0", "-t", str(seconds),
            "-i", path,
            "-vf", _FFMPEG_VF,
            "-loop", "0",
            tmp,
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, timeout=180, check=False)
        except subprocess.TimeoutExpired:
            raise RuntimeError("ffmpeg timed out after 180s")
        if proc.returncode != 0 or not os.path.isfile(tmp) \
                or os.path.getsize(tmp) < 100:
            err = (proc.stderr or b"").decode("utf-8", "replace").strip()
            raise RuntimeError(err or "ffmpeg failed (exit %s)" % proc.returncode)
        # Land it under media/ through the same naming rules as a drop.
        name = import_media_file(tmp)
        # import_media_file keeps the temp basename; rename to the clip stem.
        wanted = safe + ".gif"
        if name != wanted:
            src = os.path.join(MEDIA_DIR, name)
            dest = os.path.join(MEDIA_DIR, wanted)
            n = 1
            while os.path.exists(dest) and dest != src:
                wanted = "%s_%d.gif" % (safe, n)
                dest = os.path.join(MEDIA_DIR, wanted)
                n += 1
            if dest != src:
                shutil.move(src, dest)
                name = wanted
        return name
    finally:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass


def fit_frame(src, width, height, mode):
    """Scale src into width x height. 'cover' crops, 'letterbox' pads black."""
    out = Image.new("RGB", (width, height), (0, 0, 0))
    src = src.convert("RGB")
    if mode == "cover":
        scale = max(width / src.width, height / src.height)
    else:
        scale = min(width / src.width, height / src.height)
    size = (max(1, round(src.width * scale)), max(1, round(src.height * scale)))
    resized = src.resize(size, Image.LANCZOS)
    out.paste(resized, ((width - size[0]) // 2, (height - size[1]) // 2))
    return out


def load_media(name, width, height, fit, quality=80):
    """Return [(jpeg_bytes, duration_seconds), ...] fitted to the canvas.

    Frames are kept JPEG-compressed, not as raw RGB. A 1920x462 frame is
    2.66 MB decoded but roughly 60 KB encoded, so a long GIF no longer eats
    hundreds of megabytes and take the process down with it. Decoding one
    frame per push costs a few milliseconds, which the 350 ms cadence has to
    spare.

    Very long animations are subsampled to MAX_FRAMES: the skipped frames'
    durations are folded into the frame that is kept, so the animation still
    runs at the right wall-clock speed."""
    path = os.path.join(MEDIA_DIR, name)
    img = Image.open(path)
    try:
        total = getattr(img, "n_frames", 1) or 1
        step = max(1, -(-total // MAX_FRAMES))

        frames = []
        for position, frame in enumerate(ImageSequence.Iterator(img)):
            duration = max(0.02, (frame.info.get("duration")
                                  or img.info.get("duration") or 100) / 1000.0)
            if position % step and frames:
                # folded into the frame we kept, so timing stays honest
                frames[-1] = (frames[-1][0], frames[-1][1] + duration)
                continue
            buf = io.BytesIO()
            fitted = fit_frame(frame, width, height, fit)
            try:
                fitted.save(buf, format="JPEG", quality=quality)
            finally:
                fitted.close()
            frames.append((buf.getvalue(), duration))
    finally:
        img.close()

    if not frames:
        raise ValueError("no frames in %s" % name)
    return frames


_cache = {}
_cache_order = []
_cache_lock = threading.Lock()

# Loading a file means decoding, scaling and re-encoding every frame, which
# for a long GIF takes seconds. Doing that inside the render loop stops the
# stream dead, and an idle OUT endpoint is what blacks the panel out -- so
# loads happen on their own thread while the loop keeps pushing the frame it
# already has.
_loader = {
    "lock": threading.Lock(),
    "busy": False,
    "want": None,          # file the loader was asked for
    "done": None,          # (name, frames_or_None, error) waiting to be taken
}


def request_load(name, cfg, size):
    """Ask the loader thread for `name`. Returns immediately.

    Always records the latest want. If a load is already in flight for a
    different file (short slideshow slots), the finishing worker starts the
    pending one so the loop never sits on a stale want forever."""
    with _loader["lock"]:
        _loader["want"] = name
        if _loader["busy"]:
            return
        _loader["busy"] = True
        pending = name

    def work(target):
        while True:
            try:
                frames, error = cached_media(target, cfg, size), ""
            except Exception as exc:
                frames, error = None, str(exc)
            with _loader["lock"]:
                _loader["done"] = (target, frames, error)
                nxt = _loader["want"]
                if nxt and nxt != target:
                    # Slideshow moved on while we were decoding -- catch up.
                    target = nxt
                    continue
                _loader["busy"] = False
                return

    threading.Thread(target=work, args=(pending,), daemon=True).start()


def collect_load():
    """(name, frames, error) if a load finished since the last call."""
    with _loader["lock"]:
        done, _loader["done"] = _loader["done"], None
    return done


def reset_loader():
    with _loader["lock"]:
        _loader["want"] = None
        _loader["done"] = None

preview_lock = threading.Lock()
preview = {"jpeg": b"", "t": 0.0}      # last authored frame, for the browser
PREVIEW_EVERY = 0.7                    # seconds between preview refreshes
PREVIEW_LONG_EDGE = 900                # downscale before sending to browser


def cached_media(name, cfg, size):
    """load_media with a frame-budgeted cache, so a slideshow does not
    re-decode and re-scale the same files on every pass. Canvas size, fit and
    JPEG quality are part of the key, so changing any of them naturally
    invalidates entries.

    Eviction runs before the new file is admitted and is allowed to empty the
    cache completely -- the earlier version kept one entry back, which meant
    a single very long GIF could never be evicted."""
    width, height = size
    quality = int(cfg.get("quality") or 80)
    key = (name, width, height, cfg["fit"], quality)
    with _cache_lock:
        if key in _cache:
            return _cache[key]

    frames = load_media(name, width, height, cfg["fit"], quality=quality)
    with _cache_lock:
        while _cache_order and (
                sum(len(_cache[k]) for k in _cache_order) + len(frames)
                > FRAME_BUDGET):
            _cache.pop(_cache_order.pop(0), None)
        if len(frames) <= FRAME_BUDGET and key not in _cache:
            _cache[key] = frames
            _cache_order.append(key)
    return frames


def clear_cache():
    with _cache_lock:
        _cache.clear()
        del _cache_order[:]


# ----------------------------------------------------------------- drawing

_font_cache = {}


def font(size):
    if size not in _font_cache:
        _font_cache[size] = load_font(size)
    return _font_cache[size]


def outlined(draw, xy, text, fnt, fill=(255, 255, 255), halo=(0, 0, 0)):
    x, y = xy
    for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2)):
        draw.text((x + dx, y + dy), text, font=fnt, fill=halo)
    draw.text((x, y), text, font=fnt, fill=fill)


# ------------------------------------------------------------------ stats

_drives = None
_net_prev = {"t": 0.0, "sent": 0, "recv": 0, "up": 0.0, "down": 0.0}
_lhm_cache = {"t": 0.0, "values": {}, "error": "", "hardware": []}
_lhm_lock = threading.Lock()
_lhm_started = False


def human_rate(bytes_per_second):
    value = bytes_per_second
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if value < 1024 or unit == "GB/s":
            return "%.0f %s" % (value, unit) if unit == "B/s" \
                else "%.1f %s" % (value, unit)
        value /= 1024.0


def net_rates():
    """Up/down bytes per second, from the delta since the last call."""
    try:
        import psutil
    except ImportError:
        return None, None
    counters = psutil.net_io_counters()
    now = time.monotonic()
    if _net_prev["t"]:
        span = max(0.001, now - _net_prev["t"])
        _net_prev["up"] = (counters.bytes_sent - _net_prev["sent"]) / span
        _net_prev["down"] = (counters.bytes_recv - _net_prev["recv"]) / span
    _net_prev.update({"t": now, "sent": counters.bytes_sent,
                      "recv": counters.bytes_recv})
    return _net_prev["up"], _net_prev["down"]

LHM_POLL = 2.0            # seconds between sensor reads
LHM_TIMEOUT = 2.5         # generous: this runs off the render thread

# LibreHardwareMonitor tags each hardware node with an icon path, which is a
# far more reliable category hint than guessing from the device name.
ICON_CATEGORIES = {
    "cpu": "cpu", "amd": "gpu", "ati": "gpu", "nvidia": "gpu",
    # LHM uses intel.png for Intel iGPU (UHD/Arc), intelgpu.png on older builds.
    "intelgpu": "gpu", "intel": "gpu",
    "hdd": "storage", "ssd": "storage",
    "nvme": "storage", "ram": "memory", "mainboard": "board",
}
NAME_HINTS = (
    ("gpu", ("radeon", "geforce", "nvidia", "arc ", "iris", "vega",
             "quadro", "graphics", "uhd graphics")),
    ("cpu", ("ryzen", "core i", "core ultra", "threadripper", "xeon",
             "athlon", "pentium", "celeron", "cpu")),
    ("storage", ("ssd", "nvme", "hdd", "hard disk", "st1", "st2", "wdc",
                 "crucial", "sandisk", "kingston", "toshiba", "seagate")),
)


def categorise(name, icon):
    stem = os.path.basename(icon or "").rsplit(".", 1)[0].lower()
    if stem in ICON_CATEGORIES:
        return ICON_CATEGORIES[stem]
    low = (name or "").lower()
    for category, hints in NAME_HINTS:
        if any(hint in low for hint in hints):
            return category
    return "other"


def shorten(name, limit=16):
    for prefix in ("AMD ", "NVIDIA ", "Intel(R) ", "Intel ", "Radeon ",
                   "GeForce ", "Generic "):
        if name.startswith(prefix):
            name = name[len(prefix):]
    return name[:limit].strip()


def shorten_disk(name, limit=14):
    """Keep model tokens that distinguish drives (980 PRO vs 840 EVO).

    Verified against a live LHM tree where four Samsungs otherwise all
    collapsed to 'Samsung SS' under the generic 10-char shorten."""
    text = name or ""
    for prefix in ("Samsung SSD ", "Samsung HDD ", "Samsung ",
                   "WDC ", "WD ", "Crucial ", "Kingston ", "NVMe "):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    text = re.sub(r"\s+\d+\s*(TB|GB)\s*$", "", text, flags=re.IGNORECASE)
    return text[:limit].strip() or shorten(name, limit)


def fetch_lhm(url):
    """Read sensors from a running LibreHardwareMonitor. BLOCKING.

    Never call this from the render thread: a dead or firewalled LHM leaves
    urlopen sitting on its timeout, and a render thread that pauses stops
    feeding the panel, which is what blacks it out. The poller below owns
    this call; the render thread reads lhm_values() instead.

    Returns {source_key: (label, value)} covering CPU temperature, every GPU
    (load and temperature) and every storage device's temperature. These need
    LibreHardwareMonitor because Windows exposes no vendor-neutral way to
    read any of them from user mode without a kernel driver -- and this app
    installs none. Run LibreHardwareMonitor as administrator, then
    Options -> Remote Web Server -> Run."""
    values, hardware, error = {}, [], ""
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=LHM_TIMEOUT) as response:
            tree = json.loads(response.read().decode("utf-8", "replace"))
    except Exception as exc:
        with _lhm_lock:
            _lhm_cache.update({"t": time.monotonic(), "values": {},
                               "error": str(exc), "hardware": []})
        return {}

    def number(text):
        try:
            return float((text or "").split()[0].replace(",", "."))
        except (ValueError, IndexError):
            return None

    def leaves(node, found):
        value = (node.get("Value") or "").strip()
        if value:
            found.append(((node.get("Text") or "").lower(), value))
        for child in node.get("Children") or []:
            leaves(child, found)

    def pick(found, unit, keywords, reject=()):
        """First sensor carrying `unit` whose name matches any keyword.

        Keywords are tried in order so a preferred name (e.g. 'gpu core')
        wins over a looser one (e.g. '3d'). `reject` skips threshold sensors
        such as Warning Temperature that also carry a degree value."""
        for word in keywords:
            for text, value in found:
                if any(bad in text for bad in reject):
                    continue
                if unit in value and word in text:
                    got = number(value)
                    if got is not None:
                        return got
        return None

    counts = {"gpu": 0, "storage": 0, "cpu": 0}

    def walk(node, depth=0):
        name = (node.get("Text") or "").strip()
        children = node.get("Children") or []
        if depth >= 2 and name and children:
            hardware.append(name)
            category = categorise(name, node.get("ImageURL"))
            found = []
            leaves(node, found)

            if category == "gpu":
                index = counts["gpu"]
                counts["gpu"] += 1
                # Prefer GPU Core % over the many D3D engine counters LHM lists.
                load = pick(found, "%", ("gpu core", "core load", "d3d 3d",
                                          "3d", "load", "usage"))
                # Core / edge before hot spot -- the strip's single GPU temp
                # should match the conventional "GPU Temperature" people know
                # from HWInfo; hotspot stays as fallback (AMD junction).
                # Live tree (RX 7700 XT): GPU Core, GPU Memory, GPU Hot Spot.
                # Intel UHD often has load only and no temperature leaves.
                temp = pick(found, "\u00b0", ("gpu core", "core", "edge",
                                              "hot spot", "hotspot",
                                              "temperature"),
                            reject=("warning", "critical", "limit", "memory"))
                parts = []
                if load is not None:
                    parts.append("%.0f%%" % load)
                if temp is not None:
                    parts.append("%.0f\u00b0C" % temp)
                if parts:
                    values["gpu%d" % index] = (shorten(name), " ".join(parts))
                if temp is not None:
                    values["gpu%d_temp" % index] = ("%s TEMP" % shorten(name, 9),
                                                    "%.0f\u00b0C" % temp)
                if load is not None:
                    values["gpu%d_load" % index] = ("%s LOAD" % shorten(name, 9),
                                                    "%.0f%%" % load)

            elif category == "cpu" and not counts["cpu"]:
                counts["cpu"] += 1
                # Live Intel tree: CPU Package, Core Average, Core Max,
                # per-P/E-core, plus Distance to TjMax (rejected). AMD uses
                # Tctl/Tdie. Package / Tctl first when present.
                temp = pick(found, "\u00b0", ("tctl/tdie", "tctl", "tdie",
                                               "cpu package", "package",
                                               "core average", "core max",
                                               "cpu", "temperature"),
                            reject=("warning", "critical", "limit", "distance"))
                if temp is not None:
                    values["cpu_temp"] = ("CPU TEMP", "%.0f\u00b0C" % temp)

            elif category == "storage":
                index = counts["storage"]
                counts["storage"] += 1
                # Live NVMe: Composite Temperature + Temperature #N +
                # Warning/Critical thresholds. SATA often just Temperature.
                temp = pick(found, "\u00b0", ("composite temperature",
                                               "temperature",),
                            reject=("warning", "critical", "limit"))
                if temp is not None:
                    values["disk%d_temp" % index] = (
                        "%s TEMP" % shorten_disk(name), "%.0f\u00b0C" % temp)
            return
        for child in children:
            walk(child, depth + 1)

    walk(tree)
    if not values:
        error = "connected, but no usable sensors in the tree"
    with _lhm_lock:
        _lhm_cache.update({"t": time.monotonic(), "values": values,
                           "error": error, "hardware": hardware})
    return values


def lhm_values():
    """Whatever the poller last saw. Never blocks, never raises."""
    with _lhm_lock:
        return dict(_lhm_cache["values"]), _lhm_cache["error"], _lhm_cache["t"]


def lhm_placeholder():
    """What to show for an LHM-backed readout that has no data yet."""
    _, error, stamp = lhm_values()
    if not stamp:
        return "reading\u2026"
    low = error.lower()
    if ("timed out" in low or "refused" in low or "unreachable" in low
            or "no route" in low or "10061" in low):
        # Short: the panel strip has no room for a how-to. The GPU sensors
        # group in the window explains LibreHardwareMonitor elevation.
        return "no LHM"
    if "no usable sensors" in low:
        return "empty LHM"
    return (error or "no data")[:20]


def lhm_poller():
    while True:
        with state_lock:
            wanted = state["show_gpu"]
            url = state["lhm_url"]
            stopping = runtime["stop"]
        if stopping:
            return
        if wanted:
            fetch_lhm(url)
        time.sleep(LHM_POLL)


def start_lhm_poller():
    global _lhm_started
    if _lhm_started:
        return
    _lhm_started = True
    threading.Thread(target=lhm_poller, daemon=True).start()


# ---------------------------------------------------------------- weather
# Open-Meteo needs no API key. The poller owns every HTTP call; the render
# thread only reads weather_snapshot(). Poll slowly -- forecasts do not
# change every frame, and a hung urlopen on the render thread blacks the panel.

WEATHER_POLL = 20 * 60      # seconds between refreshes
WEATHER_TIMEOUT = 8.0

_weather_entries = {}          # place_key -> {data, error, t}
_geo_cache = {}                # query_key -> {label, timezone, error, t}
_weather_lock = threading.Lock()
_weather_started = False
_weather_invalidate = False   # set from apply_patch without taking _weather_lock


def _weather_key(place, country, units, lang):
    name, code = _parse_weather_query(place, country)
    lang = "en" if lang == "en" else "el"
    return "%s|%s|%s|%s" % (name.lower(), code, units, lang), name, code


def weather_snapshot(place_key=None):
    """Non-blocking view of a forecast entry. Never raises.

    With no key, returns the most recently updated entry (handy for the
    Refresh button). With a key, returns that place only."""
    with _weather_lock:
        if place_key:
            entry = _weather_entries.get(place_key) or {}
            data = dict(entry["data"]) if entry.get("data") else None
            return data, entry.get("error", ""), entry.get("t", 0.0)
        if not _weather_entries:
            return None, "", 0.0
        key = max(_weather_entries,
                  key=lambda k: _weather_entries[k].get("t", 0.0))
        entry = _weather_entries[key]
        data = dict(entry["data"]) if entry.get("data") else None
        return data, entry.get("error", ""), entry.get("t", 0.0)


def _parse_weather_query(place, country):
    """'Galatsi, GR' -> ('Galatsi', 'GR'). Empty country means worldwide."""
    text = (place or "").strip()
    country = (country or "").strip().upper()[:2]
    if country and not (len(country) == 2 and country.isalpha()):
        country = ""
    if "," in text:
        name, _, rest = text.partition(",")
        code = rest.strip().upper()[:2]
        if len(code) == 2 and code.isalpha():
            return name.strip(), code
        return name.strip(), country
    return text, country


def _format_temp(value, units):
    try:
        celsius = float(value)
    except (TypeError, ValueError):
        return "--"
    if units == "F":
        return "%.0f\u00b0F" % (celsius * 9.0 / 5.0 + 32.0)
    return "%.0f\u00b0C" % celsius


# Weekday labels for the forecast strip. Chosen by weather_lang rather than
# the Windows locale, so English and Greek stay stable across machines.
WEEKDAYS = {
    "el": ("Δευ", "Τρι", "Τετ", "Πεμ", "Παρ", "Σαβ", "Κυρ"),
    "en": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
}


def _weekday_label(day_iso, lang="el"):
    """YYYY-MM-DD -> short weekday in `lang`, or the MM-DD fallback."""
    names = WEEKDAYS.get(lang) or WEEKDAYS["el"]
    try:
        stamp = time.strptime(day_iso, "%Y-%m-%d")
        return names[stamp.tm_wday]
    except (TypeError, ValueError):
        return (day_iso or "?")[-5:]


def fetch_weather(place, country="GR", units="C", lang="el"):
    """Resolve a place name and pull current + 7-day forecast. BLOCKING."""
    import urllib.parse
    import urllib.request

    name, country = _parse_weather_query(place, country)
    lang = "en" if lang == "en" else "el"
    if not name:
        key, _, _ = _weather_key(place, country, units, lang)
        with _weather_lock:
            _weather_entries[key] = {
                "t": time.monotonic(), "data": None, "error": "no place set"}
        return None

    place_key = "%s|%s|%s|%s" % (name.lower(), country, units, lang)
    try:
        query = {"name": name, "count": 5, "language": lang, "format": "json"}
        if country:
            query["countryCode"] = country
        geo_url = ("https://geocoding-api.open-meteo.com/v1/search?"
                   + urllib.parse.urlencode(query))
        with urllib.request.urlopen(geo_url, timeout=WEATHER_TIMEOUT) as resp:
            geo = json.loads(resp.read().decode("utf-8", "replace"))
        results = geo.get("results") or []
        if not results and country:
            # Retry without the country filter so a mistyped code still resolves.
            query.pop("countryCode", None)
            geo_url = ("https://geocoding-api.open-meteo.com/v1/search?"
                       + urllib.parse.urlencode(query))
            with urllib.request.urlopen(geo_url, timeout=WEATHER_TIMEOUT) as resp:
                geo = json.loads(resp.read().decode("utf-8", "replace"))
            results = geo.get("results") or []
        if not results:
            raise ValueError("place not found")

        # Prefer an exact country match when several names collide.
        chosen = results[0]
        if country:
            for row in results:
                if (row.get("country_code") or "").upper() == country:
                    chosen = row
                    break

        lat = chosen["latitude"]
        lon = chosen["longitude"]
        label_bits = [chosen.get("name") or name]
        code = (chosen.get("country_code") or country or "").upper()
        if code:
            label_bits.append(code)
        place_label = ", ".join(label_bits)

        forecast_url = (
            "https://api.open-meteo.com/v1/forecast?"
            + urllib.parse.urlencode({
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,weather_code,is_day",
                "daily": ("weather_code,temperature_2m_max,temperature_2m_min,"
                          "sunrise,sunset"),
                "timezone": "auto",
                "forecast_days": 7,
            }))
        with urllib.request.urlopen(forecast_url, timeout=WEATHER_TIMEOUT) as resp:
            forecast = json.loads(resp.read().decode("utf-8", "replace"))

        current = forecast.get("current") or {}
        daily = forecast.get("daily") or {}
        days = []
        times = daily.get("time") or []
        codes = daily.get("weather_code") or []
        highs = daily.get("temperature_2m_max") or []
        lows = daily.get("temperature_2m_min") or []
        sunrises = daily.get("sunrise") or []
        sunsets = daily.get("sunset") or []
        for index, day in enumerate(times):
            day_name = _weekday_label(day, lang)
            code_val = codes[index] if index < len(codes) else 0
            high = highs[index] if index < len(highs) else None
            low = lows[index] if index < len(lows) else None
            days.append({
                "name": day_name,
                "code": code_val,
                "temps": "%s/%s" % (_format_temp(low, units),
                                    _format_temp(high, units)),
            })

        def _hhmm(iso):
            # Open-Meteo returns "2026-09-23T07:13" in the place timezone.
            if not iso or "T" not in str(iso):
                return ""
            try:
                return str(iso).split("T", 1)[1][:5]
            except Exception:
                return ""

        sunrise = _hhmm(sunrises[0]) if sunrises else ""
        sunset = _hhmm(sunsets[0]) if sunsets else ""

        data = {
            "place": place_label[:28],
            "temp": _format_temp(current.get("temperature_2m"), units),
            "code": current.get("weather_code", 0),
            "is_day": bool(current.get("is_day", 1)),
            "timezone": chosen.get("timezone") or "",
            "sunrise": sunrise,
            "sunset": sunset,
            "daily": days,
        }
        with _weather_lock:
            _weather_entries[place_key] = {
                "t": time.monotonic(), "data": data, "error": ""}
        return data
    except Exception as exc:
        with _weather_lock:
            _weather_entries[place_key] = {
                "t": time.monotonic(), "data": None,
                "error": str(exc)[:120]}
        return None


def resolve_geo(place, country="", lang="el"):
    """Geocode a place to an IANA timezone. BLOCKING; cached under _geo_cache.

    Used by world clocks (and available to weather). HTTP stays off the
    render thread -- the poller calls this."""
    import urllib.parse
    import urllib.request

    name, country = _parse_weather_query(place, country)
    lang = "en" if lang == "en" else "el"
    if not name:
        return None
    key = "%s|%s|%s" % (name.lower(), country, lang)
    with _weather_lock:
        hit = _geo_cache.get(key)
        if hit and hit.get("timezone") and time.monotonic() - hit.get("t", 0) < WEATHER_POLL * 6:
            return dict(hit)
    try:
        query = {"name": name, "count": 5, "language": lang, "format": "json"}
        if country:
            query["countryCode"] = country
        geo_url = ("https://geocoding-api.open-meteo.com/v1/search?"
                   + urllib.parse.urlencode(query))
        with urllib.request.urlopen(geo_url, timeout=WEATHER_TIMEOUT) as resp:
            geo = json.loads(resp.read().decode("utf-8", "replace"))
        results = geo.get("results") or []
        if not results and country:
            query.pop("countryCode", None)
            geo_url = ("https://geocoding-api.open-meteo.com/v1/search?"
                       + urllib.parse.urlencode(query))
            with urllib.request.urlopen(geo_url, timeout=WEATHER_TIMEOUT) as resp:
                geo = json.loads(resp.read().decode("utf-8", "replace"))
            results = geo.get("results") or []
        if not results:
            raise ValueError("place not found")
        chosen = results[0]
        if country:
            for row in results:
                if (row.get("country_code") or "").upper() == country:
                    chosen = row
                    break
        bits = [chosen.get("name") or name]
        code = (chosen.get("country_code") or country or "").upper()
        if code:
            bits.append(code)
        entry = {
            "t": time.monotonic(),
            "label": ", ".join(bits)[:28],
            "timezone": chosen.get("timezone") or "",
            "error": "",
        }
        if not entry["timezone"]:
            entry["error"] = "no tz"
        with _weather_lock:
            _geo_cache[key] = entry
        return dict(entry)
    except Exception as exc:
        entry = {
            "t": time.monotonic(),
            "label": name[:28],
            "timezone": "",
            "error": str(exc)[:40],
        }
        with _weather_lock:
            _geo_cache[key] = entry
        return dict(entry)


def geo_snapshot(place, country="", lang="el"):
    """Non-blocking look-up of a cached geocode. Never raises."""
    name, country = _parse_weather_query(place, country)
    lang = "en" if lang == "en" else "el"
    if not name:
        return None
    key = "%s|%s|%s" % (name.lower(), country, lang)
    with _weather_lock:
        hit = _geo_cache.get(key)
        return dict(hit) if hit else None


def weather_poller():
    while True:
        with state_lock:
            place = state.get("weather_place", "")
            country = state.get("weather_country", "GR")
            units = state.get("weather_units", "C")
            lang = state.get("weather_lang", "el")
            items = list(state.get("items") or [])
            stopping = runtime["stop"]
        if stopping:
            return
        global _weather_invalidate
        force = _weather_invalidate
        if force:
            _weather_invalidate = False

        weather_items = [i for i in items if i.get("type") == "weather"]
        if weather_items:
            # Primary place plus every per-item override (item.text).
            targets = []
            if (place or "").strip():
                targets.append((place, country))
            for item in weather_items:
                override = (item.get("text") or "").strip()
                if override:
                    # Override may embed its own ", CC"; empty country bias.
                    targets.append((override, ""))
            seen = set()
            for target_place, target_country in targets:
                key, name, _ = _weather_key(
                    target_place, target_country, units, lang)
                if not name or key in seen:
                    continue
                seen.add(key)
                with _weather_lock:
                    entry = _weather_entries.get(key) or {}
                    fresh = (not force
                             and entry.get("data")
                             and time.monotonic() - entry.get("t", 0) < WEATHER_POLL)
                if not fresh:
                    fetch_weather(target_place, target_country, units, lang)

        # World clocks only need a timezone from geocode -- no forecast.
        for item in items:
            if item.get("type") != "worldclock":
                continue
            query = (item.get("text") or "").strip()
            if not query:
                continue
            hit = geo_snapshot(query, "", lang)
            stale = (force or not hit or not hit.get("timezone")
                     or time.monotonic() - hit.get("t", 0) > WEATHER_POLL * 6)
            if stale:
                resolve_geo(query, "", lang)

        time.sleep(5)    # notice place / item edits quickly; fetch stays gated


def start_weather_poller():
    global _weather_started
    if _weather_started:
        return
    _weather_started = True
    threading.Thread(target=weather_poller, daemon=True).start()


# -------------------------------------------------------------- now playing
# Windows GSMTC via stock PowerShell. Avoids bundling winsdk (large, and the
# WinRT awaits hung in early probes). tools/nowplaying.ps1 owns the WinRT
# calls. The poller owns every subprocess; the render thread only reads
# nowplaying_snapshot(). Never call this from render_loop -- the script can
# take several seconds and hid_write has no timeout.

NOWPLAYING_POLL = 3.0       # idle gap after a fetch when an item is on canvas
NOWPLAYING_TIMEOUT = 14.0   # hard cap on the helper process
NOWPLAYING_THUMB_SIDE = 96  # square JPEG prepared off the render thread
NOWPLAYING_IDLE_HIDE = 5.0  # seconds of empty session before the strip vanishes

_nowplaying_cache = {
    "t": 0.0,
    "title": "",
    "artist": "",
    "album": "",
    "status": "none",
    "app": "",
    "thumb": None,          # small JPEG bytes, or None
    "error": "",
    "idle_since": 0.0,      # monotonic when title went empty; 0 = active
    "sessions": (),         # ((app, status, title, artist, album), ...)
}
_nowplaying_lock = threading.Lock()
_nowplaying_started = False


def _prepare_nowplaying_thumb(raw):
    """Downscale session art to a small JPEG. Runs on the poller thread so
    the render loop never decodes a multi-megabyte album PNG."""
    if not raw:
        return None
    try:
        with Image.open(io.BytesIO(raw)) as art:
            rgb = art.convert("RGB")
            rgb.thumbnail((NOWPLAYING_THUMB_SIDE, NOWPLAYING_THUMB_SIDE),
                          Image.LANCZOS)
            buf = io.BytesIO()
            rgb.save(buf, format="JPEG", quality=85)
            rgb.close()
            return buf.getvalue()
    except Exception:
        return None


def fetch_nowplaying(prefer_app=""):
    """Run the helper once and update the cache. Blocking; off render thread.

    prefer_app: optional AUMID substring from a nowplaying item's text field."""
    import base64
    import subprocess

    script = _nowplaying_script()
    if not script:
        with _nowplaying_lock:
            now = time.monotonic()
            idle = _nowplaying_cache.get("idle_since") or now
            _nowplaying_cache.update({
                "t": now, "error": "helper missing",
                "status": "none", "title": "", "artist": "", "album": "",
                "app": "", "thumb": None, "idle_since": idle, "sessions": (),
            })
        return

    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    cmd = [
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", script,
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, timeout=NOWPLAYING_TIMEOUT,
            check=False, creationflags=creationflags)
        text = (proc.stdout or b"").decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        with _nowplaying_lock:
            _nowplaying_cache.update({
                "t": time.monotonic(), "error": "timed out",
            })
        return
    except OSError as exc:
        with _nowplaying_lock:
            _nowplaying_cache.update({
                "t": time.monotonic(), "error": str(exc)[:60],
            })
        return

    fields = {}
    sessions = []
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().upper()
        value = value.strip()
        if key == "SESSION":
            parts = (value.split("|") + ["", "", "", "", ""])[:5]
            sessions.append(tuple(p.strip() for p in parts))
        else:
            fields[key] = value

    prefer = (prefer_app or "").strip().lower()
    title = fields.get("TITLE", "")
    artist = fields.get("ARTIST", "")
    album = fields.get("ALBUM", "")
    status = (fields.get("STATUS") or "none").strip()
    app = fields.get("APP", "")
    error = fields.get("ERROR", "")

    # Optional AUMID filter: re-pick from SESSION lines when the helper's
    # default choice does not match the item's text override.
    if prefer and sessions:
        ranked = []
        for row in sessions:
            sapp, sstatus, stitle, sartist, salbum = row
            if prefer not in sapp.lower():
                continue
            score = 0
            if sstatus.lower() in ("playing", "opened"):
                score += 2
            if stitle:
                score += 1
            ranked.append((score, sapp, sstatus, stitle, sartist, salbum))
        if ranked:
            ranked.sort(key=lambda r: -r[0])
            _, app, status, title, artist, album = ranked[0]

    thumb = None
    b64 = fields.get("THUMB_B64") or ""
    # Only keep the helper thumb when we did not override the pick.
    keep_thumb = not prefer or prefer in (app or "").lower()
    if keep_thumb and b64:
        try:
            raw = base64.b64decode(b64, validate=False)
            if 64 < len(raw) < 4 * 1024 * 1024:
                thumb = _prepare_nowplaying_thumb(raw)
        except Exception:
            thumb = None

    with _nowplaying_lock:
        prev = _nowplaying_cache
        if (thumb is None
                and title and title == prev.get("title")
                and artist == prev.get("artist")
                and prev.get("thumb")):
            thumb = prev["thumb"]
        now = time.monotonic()
        idle = (not (title or "").strip()
                or (status or "").lower() in ("none", ""))
        if idle:
            idle_since = prev.get("idle_since") or now
        else:
            idle_since = 0.0
        _nowplaying_cache.update({
            "t": now,
            "title": title[:80],
            "artist": artist[:80],
            "album": album[:80],
            "status": status[:40],
            "app": app[:120],
            "thumb": thumb,
            "error": error[:80],
            "idle_since": idle_since,
            "sessions": tuple(sessions),
        })


def nowplaying_snapshot():
    """Non-blocking view of the last media session. Never raises."""
    with _nowplaying_lock:
        return {
            "title": _nowplaying_cache["title"],
            "artist": _nowplaying_cache["artist"],
            "album": _nowplaying_cache["album"],
            "status": _nowplaying_cache["status"],
            "app": _nowplaying_cache.get("app", ""),
            "thumb": _nowplaying_cache["thumb"],
            "error": _nowplaying_cache["error"],
            "t": _nowplaying_cache["t"],
            "idle_since": _nowplaying_cache.get("idle_since", 0.0),
            "sessions": _nowplaying_cache.get("sessions", ()),
        }


def nowplaying_poller():
    while True:
        with state_lock:
            items = list(state.get("items") or [])
            stopping = runtime["stop"]
        if stopping:
            return
        np_items = [i for i in items if i.get("type") == "nowplaying"]
        if np_items:
            # First non-empty text field is an AUMID substring filter.
            prefer = ""
            for item in np_items:
                prefer = (item.get("text") or "").strip()
                if prefer:
                    break
            fetch_nowplaying(prefer_app=prefer)
        else:
            with _nowplaying_lock:
                if (_nowplaying_cache.get("thumb")
                        or _nowplaying_cache.get("title")
                        or _nowplaying_cache.get("idle_since")):
                    _nowplaying_cache.update({
                        "title": "", "artist": "", "album": "",
                        "status": "none", "app": "", "thumb": None,
                        "error": "", "t": 0.0, "idle_since": 0.0,
                        "sessions": (),
                    })
        time.sleep(NOWPLAYING_POLL)


def start_nowplaying_poller():
    global _nowplaying_started
    if _nowplaying_started:
        return
    _nowplaying_started = True
    threading.Thread(target=nowplaying_poller, daemon=True).start()


# ----------------------------------------------------------------- volume
# Default render endpoint via pycaw (Core Audio). Poller owns the COM calls;
# metric_values only reads the cache. No device writes here.

VOLUME_POLL = 1.0

_volume_cache = {"t": 0.0, "percent": None, "muted": False, "error": ""}
_volume_lock = threading.Lock()
_volume_started = False


def fetch_volume():
    try:
        from pycaw.pycaw import AudioUtilities
        device = AudioUtilities.GetSpeakers()
        endpoint = device.EndpointVolume
        percent = max(0, min(100, int(round(
            endpoint.GetMasterVolumeLevelScalar() * 100))))
        muted = bool(endpoint.GetMute())
        with _volume_lock:
            _volume_cache.update({
                "t": time.monotonic(), "percent": percent,
                "muted": muted, "error": "",
            })
    except Exception as exc:
        with _volume_lock:
            _volume_cache.update({
                "t": time.monotonic(), "error": str(exc)[:60],
            })


def volume_snapshot():
    with _volume_lock:
        return {
            "percent": _volume_cache["percent"],
            "muted": _volume_cache["muted"],
            "error": _volume_cache["error"],
            "t": _volume_cache["t"],
        }


def volume_poller():
    while True:
        with state_lock:
            items = list(state.get("items") or [])
            stopping = runtime["stop"]
        if stopping:
            return
        wanted = any(
            i.get("type") == "stat"
            and i.get("source") in ("volume", "volume_mute")
            for i in items)
        if wanted:
            fetch_volume()
        time.sleep(VOLUME_POLL)


def start_volume_poller():
    global _volume_started
    if _volume_started:
        return
    _volume_started = True
    threading.Thread(target=volume_poller, daemon=True).start()


# ------------------------------------------------------------------- timer
# Live end times keyed by item id. Not persisted in presets -- Reset in the
# editor (or adding a new timer) starts a fresh countdown.

_timer_ends = {}          # item_id -> end monotonic
_timer_lock = threading.Lock()


def timer_arm(item_id, duration_seconds):
    with _timer_lock:
        _timer_ends[item_id] = time.monotonic() + max(1, int(duration_seconds))


def timer_clear(item_id=None):
    with _timer_lock:
        if item_id is None:
            _timer_ends.clear()
        else:
            _timer_ends.pop(item_id, None)


def timer_display_map(items):
    """{item_id: {display, remaining}} for every timer item on the canvas."""
    now = time.monotonic()
    result = {}
    with _timer_lock:
        ends = dict(_timer_ends)
    live_ids = set()
    for item in items or []:
        if item.get("type") != "timer":
            continue
        iid = item["id"]
        live_ids.add(iid)
        seconds = layout.parse_timer_duration(item.get("format"))
        end = ends.get(iid)
        if end is None:
            # First sighting: arm from now so a loaded preset starts ticking.
            end = now + seconds
            with _timer_lock:
                _timer_ends[iid] = end
        remaining = int(math.ceil(end - now))
        if remaining <= 0:
            result[iid] = {"display": "done", "remaining": 0}
        else:
            result[iid] = {
                "display": "%d:%02d" % (remaining // 60, remaining % 60),
                "remaining": remaining,
            }
    # Drop ends for timers removed from the layout.
    stale = [k for k in ends if k not in live_ids]
    if stale:
        with _timer_lock:
            for k in stale:
                _timer_ends.pop(k, None)
    return result


# ----------------------------------------------------------- notifications
# Windows toast mirroring via UserNotificationListener. Requires sparse
# package identity (packaging/AppxManifest.xml + register_identity.ps1) and
# user consent. Opt-in via state["notify_mirror"].

NOTIFY_POLL = 4.0
NOTIFY_TIMEOUT = 20.0
# Keep a toast on the strip while Windows still lists it. Cap age so a
# never-dismissed Action Center entry cannot stick forever.
NOTIFY_MAX_AGE = 15 * 60
# Native apps matched on AUMID / display name. Browser toasts are allowed
# only when the title/body also looks like Gmail (see _notify_allowed).
NOTIFY_ALLOW_APPS = (
    "viber", "telegram", "outlook", "microsoft.outlook", "hxmail",
    "hxoutlook", "mail", "gmail",
)
NOTIFY_BROWSER_TOKENS = ("chrome", "msedge", "googlechrome", "firefox", "brave")
NOTIFY_GMAIL_TOKENS = ("gmail", "google mail", "mail.google")

_notify_cache = {
    "t": 0.0,
    "access": "",
    "error": "",
    "toasts": [],           # [{id, aumid, app, title, body, seen}]
}
_notify_lock = threading.Lock()
_notify_started = False


def _tool_script(*parts):
    """Resolve a tools/… script for source and frozen runs."""
    rel = os.path.join(*parts)
    candidates = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(os.path.join(meipass, rel))
        candidates.append(os.path.join(HERE, rel))
    else:
        candidates.append(os.path.join(HERE, rel))
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def _nowplaying_script():
    return _tool_script("tools", "nowplaying.ps1")


def _notify_allowed(aumid, app, title, body):
    """Keep chat/mail apps; browsers only when the toast looks like Gmail."""
    blob = (" ".join((aumid or "", app or "", title or "", body or ""))).lower()
    if "cleand92" in blob:
        return False
    if any(token in blob for token in NOTIFY_ALLOW_APPS):
        return True
    if any(token in blob for token in NOTIFY_BROWSER_TOKENS):
        return any(token in blob for token in NOTIFY_GMAIL_TOKENS)
    return False


def register_notify_identity():
    """Ensure packaging/ sits beside the app, then register the sparse package."""
    import shutil
    import subprocess

    script = _tool_script("tools", "register_identity.ps1")
    if not script:
        return False, "register script missing"

    dest_dir = os.path.join(HERE, "packaging")
    os.makedirs(dest_dir, exist_ok=True)
    src_manifest = None
    for candidate in (
            os.path.join(HERE, "packaging", "AppxManifest.xml"),
            _tool_script("packaging", "AppxManifest.xml"),
    ):
        if candidate and os.path.isfile(candidate):
            src_manifest = candidate
            break
    if not src_manifest:
        return False, "AppxManifest.xml missing"
    dest_manifest = os.path.join(dest_dir, "AppxManifest.xml")
    if os.path.abspath(src_manifest) != os.path.abspath(dest_manifest):
        shutil.copy2(src_manifest, dest_manifest)

    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    cmd = [
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", script, "-AppDir", HERE,
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, timeout=60, check=False,
            creationflags=creationflags)
        text = (proc.stdout or b"").decode("utf-8", "replace")
    except Exception as exc:
        return False, str(exc)[:120]
    if "STATUS=registered" in text:
        return True, "registered"
    err = ""
    for line in text.splitlines():
        if line.startswith("ERROR="):
            err = line[6:]
            break
    return False, err or text[:200] or "register failed"


def fetch_notifications():
    """Poll toasts. Blocking; off render thread."""
    import subprocess
    script = _tool_script("tools", "notifications.ps1")
    if not script:
        with _notify_lock:
            _notify_cache.update({
                "t": time.monotonic(), "error": "helper missing",
            })
        return
    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    cmd = [
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", script,
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, timeout=NOTIFY_TIMEOUT,
            check=False, creationflags=creationflags)
        text = (proc.stdout or b"").decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        with _notify_lock:
            _notify_cache.update({"t": time.monotonic(), "error": "timed out"})
        return
    except OSError as exc:
        with _notify_lock:
            _notify_cache.update({
                "t": time.monotonic(), "error": str(exc)[:60],
            })
        return

    access = ""
    error = ""
    fresh = []
    now = time.monotonic()
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().upper()
        value = value.strip()
        if key == "ACCESS":
            access = value
        elif key == "ERROR":
            error = value
        elif key == "TOAST":
            parts = (value.split("|") + ["", "", "", "", ""])[:5]
            tid, aumid, app, title, body = [p.strip() for p in parts]
            if not _notify_allowed(aumid, app, title, body):
                continue
            fresh.append({
                "id": tid[:80],
                "aumid": aumid[:120],
                "app": (app or aumid.split(".")[-1] or "App")[:40],
                "title": title[:80],
                "body": body[:120],
                # Helper emits Windows order; lower index is treated as newer.
                "ord": len(fresh),
            })

    with _notify_lock:
        prev = {t["id"]: t for t in _notify_cache.get("toasts") or []}
        merged = []
        for row in fresh:
            old = prev.get(row["id"])
            row["seen"] = old["seen"] if old else now
            merged.append(row)
        # Prefer chat/mail, then Gmail-in-browser; within a tier keep Windows
        # order so the newest toast wins (Cursor spam must not bury Viber).
        def _rank(row):
            blob = (" ".join((
                row.get("aumid") or "", row.get("app") or "",
                row.get("title") or "", row.get("body") or "",
            ))).lower()
            if any(t in blob for t in NOTIFY_ALLOW_APPS):
                return 0
            if any(t in blob for t in NOTIFY_BROWSER_TOKENS):
                return 1
            return 2
        merged.sort(key=lambda row: (_rank(row), row.get("ord", 9999)))
        _notify_cache.update({
            "t": now,
            "access": access,
            "error": error,
            "toasts": merged,
        })


def notify_snapshot():
    """Newest allowed toast still listed by Windows, or None. Never raises.

    When mirroring is off the strip stays blank -- no sticky coaching from an
    earlier failed poll. Age-capped so sticky Action Center entries expire.
    """
    with state_lock:
        mirroring = bool(state.get("notify_mirror"))
    if not mirroring:
        return None
    now = time.monotonic()
    with _notify_lock:
        toasts = list(_notify_cache.get("toasts") or [])
        access = _notify_cache.get("access") or ""
        error = _notify_cache.get("error") or ""
    if access and access != "Allowed":
        return {
            "app": "Notify",
            "title": "access needed",
            "body": "Allow notification access in Windows Settings",
        }
    if error and not toasts:
        low = error.lower()
        if ("identity" in low or "not registered" in low or "class not" in low
                or "denied" in low or "capability" in low):
            return {
                "app": "Notify",
                "title": "identity needed",
                "body": "Use Register sparse identity under Notifications",
            }
        return None
    live = [t for t in toasts
            if (t.get("title") or t.get("body"))
            and now - t.get("seen", now) <= NOTIFY_MAX_AGE]
    if not live:
        return None
    top = live[0]
    return {
        "app": top.get("app") or "App",
        "title": top.get("title") or "",
        "body": top.get("body") or "",
    }


def notify_poller():
    while True:
        with state_lock:
            wanted = state.get("notify_mirror")
            items = list(state.get("items") or [])
            stopping = runtime["stop"]
        if stopping:
            return
        if wanted and any(i.get("type") == "notify" for i in items):
            fetch_notifications()
        time.sleep(NOTIFY_POLL)


def start_notify_poller():
    global _notify_started
    if _notify_started:
        return
    _notify_started = True
    threading.Thread(target=notify_poller, daemon=True).start()


def load_settings():
    """Merge settings.json into state. Missing or broken file is a no-op."""
    if not os.path.isfile(SETTINGS_PATH):
        return
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    with state_lock:
        place = data.get("weather_place")
        if isinstance(place, str) and place.strip():
            state["weather_place"] = place.strip()[:80]
        country = data.get("weather_country")
        if isinstance(country, str):
            # Empty string is allowed: worldwide search, no country bias.
            code = country.strip().upper()[:2]
            if not code:
                state["weather_country"] = ""
            elif code.isalpha():
                state["weather_country"] = code
        units = data.get("weather_units")
        if units in ("C", "F"):
            state["weather_units"] = units
        lang = data.get("weather_lang")
        if lang in ("el", "en"):
            state["weather_lang"] = lang
        if "notify_mirror" in data:
            state["notify_mirror"] = bool(data.get("notify_mirror"))


def save_settings():
    """Write weather prefs beside the exe. Never raises into the UI."""
    with state_lock:
        payload = {key: state.get(key) for key in SETTINGS_KEYS}
    try:
        tmp = SETTINGS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp, SETTINGS_PATH)
    except OSError:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass


def human_bytes(value):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return "%.0f %s" % (value, unit) if unit in ("B", "KB") \
                else "%.1f %s" % (value, unit)
        value /= 1024.0


def drive_letters():
    """Fixed drives, as single letters. Cached: enumerating partitions on
    every frame would hit the disk."""
    global _drives
    if _drives is None:
        letters = []
        try:
            import psutil
            for part in psutil.disk_partitions(all=False):
                letter = (part.mountpoint or "").strip("\\/").rstrip(":")
                if len(letter) == 1 and letter.isalpha():
                    letters.append(letter.upper())
        except Exception:
            pass
        _drives = letters or ["C"]
    return _drives


def available_sources():
    """{source_key: label} for every readout that can be placed on the
    layout, including one entry per fixed drive and whatever LibreHardware-
    Monitor is currently reporting. The editor lists these."""
    sources = {
        "cpu": "CPU load",
        "cpu_temp": "CPU temperature (LHM)",
        "ram": "RAM used, percent",
        "ram_abs": "RAM used, absolute",
        "net_down": "Network down",
        "net_up": "Network up",
        "uptime": "Uptime",
        "volume": "Speaker volume",
        "volume_mute": "Speaker mute",
        "battery": "Battery",
    }
    for letter in drive_letters():
        sources["disk_%s" % letter.lower()] = "Disk %s: used" % letter
    from_lhm, _, _ = lhm_values()
    for key in sorted(from_lhm):
        if key.startswith("gpu") and key.count("_") == 0:
            sources[key] = "GPU %s, load + temp (LHM)" % key[3:]
        elif key.endswith("_load"):
            sources[key] = "GPU %s load (LHM)" % key[3:-5]
        elif key.startswith("gpu") and key.endswith("_temp"):
            sources[key] = "GPU %s temperature (LHM)" % key[3:-5]
        elif key.startswith("disk") and key.endswith("_temp"):
            sources[key] = "Disk %s temperature (LHM)" % key[4:-5]
    # Keep two generic GPU slots selectable even before LHM has answered, so
    # a preset referring to them is editable on a machine without LHM running.
    sources.setdefault("gpu0", "GPU 0, load + temp (LHM)")
    sources.setdefault("gpu1", "GPU 1, load + temp (LHM)")
    sources.setdefault("gpu0_temp", "GPU 0 temperature (LHM)")
    return sources


def metric_values(cfg, filename=None):
    """{source: (label, value_string)} for the layout renderer, plus the
    current file name. Cheap and non-blocking: LibreHardwareMonitor numbers
    come from the poller's cache, never from a live HTTP call."""
    values = {"filename": filename or ""}
    try:
        import psutil
    except ImportError:
        values["cpu"] = ("CPU", "no psutil")
        return values

    values["cpu"] = ("CPU", "%.0f%%" % psutil.cpu_percent(interval=None))
    memory = psutil.virtual_memory()
    values["ram"] = ("RAM", "%.0f%%" % memory.percent)
    values["ram_abs"] = ("RAM", "%s / %s" % (
        human_bytes(memory.used), human_bytes(memory.total)))

    for letter in drive_letters():
        try:
            usage = psutil.disk_usage("%s:\\" % letter)
            values["disk_%s" % letter.lower()] = (
                "%s:" % letter, "%.0f%%" % usage.percent)
        except Exception:
            values["disk_%s" % letter.lower()] = ("%s:" % letter, "n/a")

    try:
        seconds = int(time.time() - psutil.boot_time())
        values["uptime"] = ("UP", "%dd %dh" % (seconds // 86400,
                                               (seconds % 86400) // 3600)
                            if seconds >= 86400
                            else "%dh %dm" % (seconds // 3600,
                                              (seconds % 3600) // 60))
    except Exception:
        pass

    up, down = net_rates()
    if down is not None:
        values["net_down"] = ("DOWN", human_rate(down))
        values["net_up"] = ("UP", human_rate(up))

    vol = volume_snapshot()
    if vol.get("percent") is not None:
        if vol.get("muted"):
            values["volume"] = ("VOL", "MUTE")
            values["volume_mute"] = ("MUTE", "on")
        else:
            values["volume"] = ("VOL", "%d%%" % vol["percent"])
            values["volume_mute"] = ("MUTE", "off")
    elif vol.get("error"):
        values["volume"] = ("VOL", "n/a")
        values["volume_mute"] = ("MUTE", "n/a")
    else:
        values["volume"] = ("VOL", "\u2026")
        values["volume_mute"] = ("MUTE", "\u2026")

    try:
        bat = psutil.sensors_battery()
    except Exception:
        bat = None
    if bat is None:
        values["battery"] = ("BAT", "n/a")
    else:
        pct = int(round(bat.percent))
        if bat.power_plugged:
            values["battery"] = ("BAT", "%d%% AC" % pct)
        else:
            values["battery"] = ("BAT", "%d%%" % pct)

    if cfg["show_gpu"]:
        from_lhm, _, _ = lhm_values()
        values.update(from_lhm)
        # Any LHM-backed source a layout asks for but LHM has not supplied
        # gets a short status instead of vanishing from the panel.
        placeholder = lhm_placeholder()
        for item in cfg["items"]:
            key = item.get("source", "")
            if (key.startswith("gpu") or key.endswith("_temp")) \
                    and key not in values:
                values[key] = (key.upper().replace("_", " ")[:12], placeholder)

    # Weather: per-item blob so a second place override can share the canvas
    # with the primary city. Placeholder keeps the item selectable while the
    # poller is catching up or offline.
    weather_items = [i for i in (cfg.get("items") or [])
                     if i.get("type") == "weather"]
    if weather_items:
        units = cfg.get("weather_units", "C")
        lang = cfg.get("weather_lang", "el")
        primary = (cfg.get("weather_place") or "").strip()
        primary_country = cfg.get("weather_country", "GR")
        weather_map = {}

        def _placeholder(label, err, stamp):
            if not stamp:
                note = "reading\u2026"
            elif err:
                low = err.lower()
                if "not found" in low:
                    note = "no place"
                elif "timed out" in low or "refused" in low:
                    note = "offline"
                else:
                    note = "no data"
            else:
                note = "no data"
            return {
                "place": (label or "weather")[:28],
                "temp": note,
                "code": 0,
                "is_day": True,
                "daily": [{"name": "\u2014", "code": 0, "temps": note}] * 3,
            }

        for item in weather_items:
            override = (item.get("text") or "").strip()
            if override:
                key, _, _ = _weather_key(override, "", units, lang)
                label = override
            else:
                key, _, _ = _weather_key(primary, primary_country, units, lang)
                label = primary or "weather"
                if "," not in label and primary_country:
                    label = "%s, %s" % (label, primary_country)
            snap, err, stamp = weather_snapshot(key)
            weather_map[item["id"]] = (snap if snap
                                       else _placeholder(label, err, stamp))
        values["weather_map"] = weather_map
        # Keep a primary `weather` key for any older caller / first item.
        values["weather"] = weather_map[weather_items[0]["id"]]

    # World clocks: timezone comes from the geo cache; wall time is computed
    # in layout.render_worldclock so it ticks every frame without HTTP.
    clock_items = [i for i in (cfg.get("items") or [])
                   if i.get("type") == "worldclock"]
    if clock_items:
        lang = cfg.get("weather_lang", "el")
        wmap = {}
        for item in clock_items:
            query = (item.get("text") or "").strip()
            if not query:
                wmap[item["id"]] = {
                    "label": "set city", "timezone": "", "error": "set city"}
                continue
            hit = geo_snapshot(query, "", lang)
            if hit and hit.get("timezone"):
                wmap[item["id"]] = {
                    "label": hit.get("label") or query,
                    "timezone": hit["timezone"],
                }
            elif hit and hit.get("error"):
                wmap[item["id"]] = {
                    "label": hit.get("label") or query,
                    "timezone": "",
                    "error": hit["error"][:20],
                }
            else:
                wmap[item["id"]] = {
                    "label": query[:28], "timezone": "", "pending": True}
        values["worldclock_map"] = wmap

    # Now playing: GSMTC cache is filled by the poller; nothing blocks here.
    if any(i.get("type") == "nowplaying" for i in (cfg.get("items") or [])):
        snap = nowplaying_snapshot()
        status = (snap.get("status") or "").lower()
        title = (snap.get("title") or "").strip()
        idle_since = snap.get("idle_since") or 0.0
        if not snap.get("t"):
            values["nowplaying"] = {
                "title": "", "artist": "", "status": "reading", "thumb": None}
        elif ((not title or status in ("none", ""))
              and idle_since
              and (time.monotonic() - idle_since) >= NOWPLAYING_IDLE_HIDE):
            # Strip gone after a short "Nothing playing" grace period.
            values["nowplaying"] = None
        elif not title or status in ("none", ""):
            values["nowplaying"] = {
                "title": "", "artist": "", "status": "none", "thumb": None}
        else:
            values["nowplaying"] = {
                "title": snap.get("title") or "",
                "artist": snap.get("artist") or "",
                "status": snap.get("status") or "",
                "thumb": snap.get("thumb"),
            }

    timer_items = [i for i in (cfg.get("items") or [])
                   if i.get("type") == "timer"]
    if timer_items:
        values["timer_map"] = timer_display_map(timer_items)

    if any(i.get("type") == "notify" for i in (cfg.get("items") or [])):
        note = notify_snapshot()
        if note:
            values["notify"] = note
    return values


# ----------------------------------------------------------- info surfaces

def hex_rgb(value, fallback=(255, 255, 255)):
    """'#96d7ff' -> (150, 215, 255). Bad input falls back rather than raising
    mid-render."""
    text = (value or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    if len(text) != 6:
        return fallback
    try:
        return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return fallback


def fitted(text, max_width, base_size, min_size=10):
    """Largest cached font at or below base_size whose rendering of `text`
    fits max_width. This is what stops a long clock from running off the
    edge of the narrow vertical canvas."""
    size = max(min_size, int(base_size))
    while size > min_size:
        chosen = font(size)
        try:
            width = chosen.getbbox(text)[2]
        except Exception:
            return chosen
        if width <= max_width:
            return chosen
        size = int(size * 0.92) if size > 20 else size - 1
    return font(min_size)


def text_height(fnt, sample="0123456789:%"):
    try:
        box = fnt.getbbox(sample)
        return box[3] - box[1]
    except Exception:
        return fnt.size if hasattr(fnt, "size") else 12


def draw_info(img, cfg, translucent=False, filename=None):
    """Draw the configured layout onto img. Records the pixel boxes of
    everything drawn in runtime["boxes"] so the editor can hit-test and drag
    items against exactly what the panel is showing."""
    values = metric_values(cfg, filename)
    boxes = layout.render(img, cfg["items"], values, fitted, text_height,
                          translucent=translucent)
    runtime["boxes"] = boxes
    runtime["box_canvas"] = img.size
    return img


def info_canvas(cfg, filename=None):
    width, height = author_size(cfg)
    img = Image.new("RGB", (width, height),
                    hex_rgb(cfg["color_bg"], (8, 10, 14)))
    return draw_info(img, cfg, filename=filename)


# --------------------------------------------------------------- geometry

def author_size(cfg):
    """The canvas content is composed on, before the wire rotation."""
    if cfg["layout"] == "vertical":
        return cfg["canvas_h"], cfg["canvas_w"]     # 462 x 1920
    return cfg["canvas_w"], cfg["canvas_h"]         # 1920 x 462


def wire_rotation(cfg):
    """Degrees to rotate the authored canvas so it reaches the device in the
    panel's portrait shape. Horizontal content needs a quarter turn;
    vertical content is already the right way round."""
    if cfg["layout"] == "vertical":
        return 180 if cfg["flip"] else 0
    return 90 if cfg["flip"] else 270


def wire_size(cfg):
    """What the device insists on receiving: its own portrait shape."""
    return cfg["canvas_h"], cfg["canvas_w"]         # 462 x 1920


def check_geometry(cfg):
    """Reject any configuration that would put a wrong-shaped frame on the
    wire. Returns None if safe, or a message explaining the refusal."""
    if cfg["canvas_w"] <= cfg["canvas_h"]:
        return ("canvas_w must be the panel's long edge (width > height), "
                "got %dx%d" % (cfg["canvas_w"], cfg["canvas_h"]))
    width, height = author_size(cfg)
    if wire_rotation(cfg) % 180:
        width, height = height, width
    if (width, height) != wire_size(cfg):
        return ("computed frame %dx%d does not match the panel shape %dx%d "
                "-- refusing to send" % ((width, height) + wire_size(cfg)))
    return None


def encode(img, quality):
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


# ------------------------------------------------------------ render loop

def render_loop(panel):
    last_jpeg = None       # last frame actually sent, for draft mode
    frames = None          # frames of the file currently on screen
    loaded = None          # which file those frames belong to
    index = 0              # animation frame index within `frames`
    due = 0.0              # when to advance the animation frame
    playlist = []          # slideshow file order
    pos = 0                # position in the playlist
    slot_end = 0.0         # when the current slideshow file's turn ends

    start_lhm_poller()
    start_weather_poller()
    start_nowplaying_poller()
    start_volume_poller()
    start_notify_poller()
    runtime["status"] = "streaming"

    while True:
        started = time.monotonic()
        with state_lock:
            if runtime["stop"]:
                runtime["status"] = "stopped"
                return
            cfg = dict(state)
            reload_media = runtime["reload_media"]
            apply_brightness = runtime["apply_brightness"]
            wake_now = runtime["wake"]
            apply_once = runtime["apply_once"]
            runtime["reload_media"] = False
            runtime["apply_brightness"] = False
            runtime["wake"] = False
            runtime["apply_once"] = False

        refusal = check_geometry(cfg)
        if refusal:
            runtime["message"] = refusal
            time.sleep(0.3)
            continue

        try:
            if wake_now:
                panel.wake(cfg["brightness"])
                runtime["message"] = "wake sequence re-sent"
            elif apply_brightness:
                panel.set_brightness(cfg["brightness"])

            if reload_media:
                frames, loaded, index, due = None, None, 0, 0.0
                playlist, pos, slot_end = [], 0, 0.0
                clear_cache()
                reset_loader()
                invalidate_media_list()

            now = time.monotonic()

            # -- a load that finished on the loader thread ---------------
            finished = collect_load()
            if finished:
                done_name, done_frames, done_error = finished
                # Accept only if this finish is still what the loop wants.
                # Short slideshow slots otherwise paint a stale decode.
                if cfg["mode"] == "media":
                    accept = cfg.get("media") == done_name
                elif cfg["mode"] == "slideshow":
                    accept = (bool(playlist) and pos < len(playlist)
                              and playlist[pos] == done_name)
                else:
                    accept = False
                if accept:
                    loaded = done_name    # even on failure, so we stop asking
                    if done_frames:
                        frames, index, due = done_frames, 0, 0.0
                        runtime["message"] = "%s -- %d frame(s)%s" % (
                            done_name, len(done_frames),
                            "" if cfg["mode"] != "slideshow"
                            else "  [%d/%d]" % (
                                pos + 1, max(1, len(playlist))))
                    else:
                        frames = None
                        runtime["message"] = "cannot load %s: %s" % (
                            done_name, done_error)

            # -- decide which file should be on screen -------------------
            wanted = None
            if cfg["mode"] == "media":
                wanted = cfg["media"]
            elif cfg["mode"] == "slideshow":
                if not playlist:
                    playlist = list_media_cached()
                    if cfg["slide_shuffle"]:
                        random.shuffle(playlist)
                    pos, slot_end = 0, 0.0
                if playlist:
                    if slot_end == 0.0:
                        slot_end = now + cfg["slide_seconds"]
                    elif now >= slot_end:
                        pos = (pos + 1) % len(playlist)
                        slot_end = now + cfg["slide_seconds"]
                        if pos == 0 and cfg["slide_shuffle"]:
                            random.shuffle(playlist)
                    wanted = playlist[pos]
                else:
                    # Keep coaching on the status line every empty tick so a
                    # later successful load does not leave a stale hint.
                    runtime["message"] = (
                        "media folder is empty -- drop ultrawide .png .jpg "
                        ".gif files in media/, or use Open media folder")

            if cfg["mode"] == "media" and not wanted:
                if not list_media_cached():
                    runtime["message"] = (
                        "media folder is empty -- drop ultrawide .png .jpg "
                        ".gif files in media/, or use Open media folder")
                else:
                    runtime["message"] = (
                        "no file selected -- pick one under Source")

            # -- ask for whatever should be on screen, without waiting ---
            if wanted != loaded:
                if wanted:
                    request_load(wanted, cfg, author_size(cfg))
                else:
                    frames, loaded, index, due = None, None, 0, 0.0

            # -- build the frame ----------------------------------------
            if frames:
                if due == 0.0:
                    due = now + frames[index][1]
                while now >= due:
                    index = (index + 1) % len(frames)
                    due += frames[index][1]
                    if due < now - 5.0:      # very stale, resynchronise
                        due = now + frames[index][1]
                with Image.open(io.BytesIO(frames[index][0])) as decoded:
                    canvas = decoded.convert("RGB")
                canvas = draw_info(canvas, cfg, translucent=True,
                                   filename=loaded)
            else:
                canvas = info_canvas(cfg, filename=loaded)

            rotation = wire_rotation(cfg)

            now = time.monotonic()
            if now - preview["t"] >= PREVIEW_EVERY:
                # The browser sees the authored canvas, not the wire frame:
                # that is what actually appears on the panel once mounted.
                shot = canvas.copy()
                try:
                    scale = PREVIEW_LONG_EDGE / max(shot.size)
                    if scale < 1:
                        resized = shot.resize(
                            (max(1, int(shot.width * scale)),
                             max(1, int(shot.height * scale))), Image.LANCZOS)
                        shot.close()
                        shot = resized
                    buf = io.BytesIO()
                    shot.save(buf, format="JPEG", quality=70)
                    with preview_lock:
                        preview["jpeg"] = buf.getvalue()
                        preview["t"] = now
                finally:
                    shot.close()

            if rotation:
                rotated = canvas.rotate(rotation, expand=True)
                canvas.close()
                canvas = rotated

            try:
                if cfg["hold"] and last_jpeg is not None and not apply_once:
                    # Draft mode: the preview above is live, the panel is not.
                    # Still a real push, so the endpoint never goes idle.
                    panel.send_jpeg(last_jpeg)
                else:
                    last_jpeg = encode(canvas, cfg["quality"])
                    panel.send_jpeg(last_jpeg)
            finally:
                canvas.close()

            elapsed = time.monotonic() - started
            runtime["frames"] += 1
            runtime["last_ms"] = int(elapsed * 1000)
            if elapsed > STALL_WARN:
                runtime["message"] = ("last frame took %.1fs -- the device may "
                                      "be stalling; try Wake, or unplug and "
                                      "replug if it stays dark" % elapsed)

        except D92Error as exc:
            runtime["status"] = "error"
            # Exe users do not "restart this script" -- quit and open again.
            # Deliberately no reopen: that blacks the panel until a replug.
            runtime["message"] = (
                "%s -- unplug and replug the panel, then quit and open "
                "CleanD92 again" % exc)
            print("\n[device] %s" % runtime["message"])
            return
        except Exception as exc:
            runtime["message"] = "render error: %s" % exc

        slack = (cfg["interval_ms"] / 1000.0) - (time.monotonic() - started)
        if slack > 0:
            time.sleep(slack)


# ------------------------------------------------------------------- http

def lan_addresses():
    """Non-loopback IPv4 addresses, Wi-Fi first when we can tell.

    VirtualBox / APIPA / some Hyper-V adapters often appear beside the real
    LAN address; rank them last so the phone URL is usually useful."""
    import socket
    found = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            addr = info[4][0]
            if addr and not addr.startswith("127.") and addr not in found:
                found.append(addr)
    except OSError:
        pass
    if not found:
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                probe.connect(("8.8.8.8", 80))
                addr = probe.getsockname()[0]
                if addr and not addr.startswith("127."):
                    found.append(addr)
            finally:
                probe.close()
        except OSError:
            pass

    def rank(addr):
        if addr.startswith("169.254."):
            return 90
        if addr.startswith("192.168.56."):
            return 50          # VirtualBox host-only
        parts = addr.split(".")
        if len(parts) >= 2 and parts[0] == "172" and parts[1].isdigit():
            second = int(parts[1])
            if 16 <= second <= 31:
                return 15      # RFC1918, often WSL/Hyper-V
        if addr.startswith("192.168.") or addr.startswith("10."):
            return 0
        return 20

    return sorted(found, key=rank)


def phone_urls(port=None):
    """URLs a phone on the same Wi-Fi can open."""
    port = PORT if port is None else port
    return ["http://%s:%d" % (addr, port) for addr in lan_addresses()]

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>D92 panel</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{color-scheme:dark}
body{margin:0;padding:24px;background:#0d0f13;color:#e6e9ef;
 font:15px/1.5 system-ui,Segoe UI,sans-serif}
h1{font-size:19px;margin:0 0 4px}
.sub{color:#7c869b;font-size:13px;margin-bottom:22px}
fieldset{border:1px solid #232733;border-radius:10px;margin:0 0 16px;
 padding:14px 16px 18px}
legend{color:#8fa3c0;font-size:12px;letter-spacing:.08em;
 text-transform:uppercase;padding:0 6px}
label{display:block;margin:10px 0 4px;color:#9aa5b8;font-size:13px}
select,input[type=number]{width:100%;background:#151922;color:#e6e9ef;
 border:1px solid #2a3040;border-radius:7px;padding:8px}
input[type=range]{width:100%}
.row{display:flex;gap:14px;flex-wrap:wrap}
.row>div{flex:1;min-width:130px}
.seg{display:flex;gap:8px;margin-top:6px}
.seg button{flex:1;background:#151922;color:#9aa5b8;border:1px solid #2a3040;
 border-radius:7px;padding:9px;cursor:pointer;font:inherit}
.seg button.on{background:#1e3a5f;color:#dceaff;border-color:#2f5d94}
.seg button.danger{border-color:#5c2027;color:#ffb3bd}
.chk{display:flex;align-items:center;gap:9px;margin:9px 0;color:#c7cedd}
.chk input{width:17px;height:17px}
#status{border-radius:10px;padding:11px 14px;font-size:13px;
 background:#12261a;border:1px solid #1f4a30;color:#9fe3b6}
#status.error{background:#2a1417;border-color:#5c2027;color:#ffb3bd}
#status.holding{background:#2a2412;border-color:#5c4a20;color:#e6d29f}
.hint{color:#6b7488;font-size:12px;margin-top:6px}
#pvwrap{background:#000;border:1px solid #2a3040;border-radius:8px;
 padding:8px;display:flex;justify-content:center;align-items:center;
 min-height:60px}
#pv{max-width:100%;max-height:340px;display:block}
input[type=color]{width:100%;height:38px;background:#151922;
 border:1px solid #2a3040;border-radius:7px;padding:3px;cursor:pointer}
input[type=text]{width:100%;background:#151922;color:#e6e9ef;
 border:1px solid #2a3040;border-radius:7px;padding:8px}
#sensors{white-space:pre-wrap;font-family:ui-monospace,Consolas,monospace;
 font-size:12px;color:#9aa5b8;background:#12151c;border:1px solid #232733;
 border-radius:8px;padding:10px;margin-top:10px;display:none}
/* Phone / narrow screens: bigger taps, full-width segments. */
@media (max-width:640px){
 body{padding:14px}
 h1{font-size:18px}
 .seg button,.chk input,select,input[type=number],input[type=text],
 input[type=range],input[type=color]{min-height:44px;font-size:16px}
 .seg{flex-direction:column}
 .row>div{min-width:100%}
 #pv{max-height:220px}
}
</style></head><body>
<h1>StreamDock D92</h1>
<div class="sub">Media is read from the <code>media</code> folder next to
the script. Drop files in, then Rescan.</div>

<div id="status">connecting&hellip;</div>

<fieldset><legend>Preview</legend>
<div id="pvwrap"><img id="pv" alt="panel preview"></div>
<div class="hint">The authored canvas, refreshed about once a second &mdash;
this is what lands on the panel.</div>
</fieldset>

<fieldset><legend>Source</legend>
<div class="seg">
  <button id="m-clock">Clock</button>
  <button id="m-media">Image / GIF</button>
  <button id="m-slideshow">Slideshow</button>
</div>
<label for="media">File (single-file mode)</label>
<select id="media"></select>
<div class="seg"><button id="rescan">Rescan folder</button></div>
<label>Fit</label>
<div class="seg">
  <button id="f-cover">Cover (crop)</button>
  <button id="f-letterbox">Letterbox</button>
</div>
<div class="row" style="margin-top:14px">
  <div><label for="slide_seconds">Slideshow: seconds per file</label>
    <input type="number" id="slide_seconds" min="2" max="600"></div>
</div>
<div class="chk"><input type="checkbox" id="slide_shuffle"><span>Shuffle
 order</span></div>
<div class="hint">Slideshow plays every file in the folder in turn. Animated
GIFs keep animating during their slot.</div>
</fieldset>

<fieldset><legend>Layout</legend>
<div class="chk"><input type="checkbox" id="hold"><span>Hold panel output
 (edit without pushing)</span></div>
<div class="seg" style="margin-top:6px">
  <button id="apply">Apply now</button>
</div>
<label for="preset">Preset</label>
<select id="preset"></select>
<div class="hint">Positions, sizes and colours of individual items are edited
in the desktop app (<code>d92_app.py</code>), which can drag them on a live
preview. Here you can switch between saved presets.</div>
</fieldset>

<fieldset><legend>GPU sensors</legend>
<div class="chk"><input type="checkbox" id="show_gpu"><span>Poll
 LibreHardwareMonitor</span></div>
<label for="lhm_url">LibreHardwareMonitor JSON URL</label>
<input type="text" id="lhm_url">
<div class="seg" style="margin-top:8px">
  <button id="probe">Test GPU sensors</button>
</div>
<div id="sensors"></div>
<div class="hint">GPU readings need LibreHardwareMonitor running <b>as
administrator</b> with <b>Options &rarr; Remote Web Server &rarr; Run</b>.
Windows exposes no vendor-neutral way to read GPU temperature from user mode
without a kernel driver, and this app installs none.</div>
</fieldset>

<fieldset><legend>Background</legend>
<div class="row"><div><label for="color_bg">Info screen background</label>
  <input type="color" id="color_bg"></div></div>
</fieldset>

<fieldset><legend>Panel</legend>
<label for="brightness">Brightness <span id="bv"></span></label>
<input type="range" id="brightness" min="0" max="100">
<label>Content orientation</label>
<div class="seg">
  <button id="l-horizontal">Horizontal</button>
  <button id="l-vertical">Vertical</button>
</div>
<div class="chk"><input type="checkbox" id="flip"><span>Flip 180&deg;</span></div>
<div class="hint">Pick <b>Vertical</b> if the panel is mounted on its end: the
content is then composed on a tall 462&times;1920 canvas instead of a wide
one. The frame always reaches the device in its portrait shape either way
&mdash; anything else makes it tile the image and stop accepting writes.</div>
<div class="seg" style="margin-top:12px">
  <button id="wake">Wake panel (DIS &rarr; LIG)</button>
</div>
<div class="hint">Re-sends the official connect sequence. Try this first if
the panel has gone dark &mdash; it does not reopen the handle.</div>
<div class="row" style="margin-top:12px">
  <div><label for="interval_ms">Interval (ms)</label>
    <input type="number" id="interval_ms" min="100" max="2000" step="10"></div>
  <div><label for="quality">JPEG quality</label>
    <input type="number" id="quality" min="40" max="95"></div>
</div>
<div class="hint">350 ms is the cadence verified safe upstream. Lower is
smoother but increases USB dropouts.</div>
<div class="row" style="margin-top:12px">
  <div><label for="canvas_w">Canvas width</label>
    <input type="number" id="canvas_w" min="64" max="4096"></div>
  <div><label for="canvas_h">Canvas height</label>
    <input type="number" id="canvas_h" min="64" max="4096"></div>
</div>
<div class="hint">Pre-rotation authoring size, width &gt; height. Try 464 if
the image looks slightly squashed.</div>
</fieldset>

<script>
let st = {};
const $ = id => document.getElementById(id);

function paint(){
  $('m-clock').className = st.mode==='clock' ? 'on':'';
  $('m-media').className = st.mode==='media' ? 'on':'';
  $('m-slideshow').className = st.mode==='slideshow' ? 'on':'';
  $('slide_seconds').value = st.slide_seconds;
  $('slide_shuffle').checked = st.slide_shuffle;
  $('f-cover').className = st.fit==='cover' ? 'on':'';
  $('f-letterbox').className = st.fit==='letterbox' ? 'on':'';
  $('l-horizontal').className = st.layout==='horizontal' ? 'on':'';
  $('l-vertical').className = st.layout==='vertical' ? 'on':'';
  $('flip').checked = st.flip;
  $('brightness').value = st.brightness; $('bv').textContent = st.brightness;
  $('interval_ms').value = st.interval_ms;
  $('quality').value = st.quality;
  $('canvas_w').value = st.canvas_w;
  $('canvas_h').value = st.canvas_h;
  $('lhm_url').value = st.lhm_url;
  $('color_bg').value = st.color_bg;
  $('show_gpu').checked = st.show_gpu;
  $('hold').checked = st.hold;
}

function fillPresets(names, current){
  const sel = $('preset');
  sel.innerHTML = '';
  for(const n of (names && names.length ? names : ['(none)'])){
    const o = document.createElement('option');
    o.textContent = n; o.value = n;
    if(n === current) o.selected = true;
    sel.appendChild(o);
  }
}

function fillMedia(files){
  const sel = $('media');
  sel.innerHTML = '';
  if(!files.length){
    const o = document.createElement('option');
    o.textContent = '(media folder is empty)'; o.value = '';
    sel.appendChild(o); return;
  }
  for(const f of files){
    const o = document.createElement('option');
    o.textContent = f; o.value = f;
    if(f===st.media) o.selected = true;
    sel.appendChild(o);
  }
}

async function refresh(){
  const r = await fetch('/api/state');
  const j = await r.json();
  st = j.state;
  fillMedia(j.media);
  fillPresets(j.presets, st.preset);
  paint();
  const s = $('status');
  let cls = '';
  if(j.runtime.status==='error') cls = 'error';
  else if(st.hold) cls = 'holding';
  s.className = cls;
  const head = st.hold && j.runtime.status!=='error'
    ? j.runtime.status+' \\u00b7 holding' : j.runtime.status;
  s.innerHTML = '<b>'+head+'</b> \\u00b7 '+j.runtime.frames+
    ' frames \\u00b7 '+j.runtime.last_ms+' ms' +
    (j.runtime.message ? ' \\u00b7 '+j.runtime.message : '');
}

async function patch(p){
  const r = await fetch('/api/state', {method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify(p)});
  const j = await r.json();
  st = j.state; paint();
}


$('m-clock').onclick = () => patch({mode:'clock'});
$('m-media').onclick = () => patch({mode:'media'});
$('m-slideshow').onclick = () => patch({mode:'slideshow'});
$('f-cover').onclick = () => patch({fit:'cover'});
$('f-letterbox').onclick = () => patch({fit:'letterbox'});
$('l-horizontal').onclick = () => patch({layout:'horizontal'});
$('l-vertical').onclick = () => patch({layout:'vertical'});
$('media').onchange = e => patch({media:e.target.value, mode:'media'});
$('rescan').onclick = refresh;
$('brightness').oninput = e => { $('bv').textContent = e.target.value; };
$('brightness').onchange = e => patch({brightness:+e.target.value});
$('lhm_url').onchange = e => patch({lhm_url:e.target.value});
$('color_bg').onchange = e => patch({color_bg: e.target.value});
$('apply').onclick = async () => {
  await fetch('/api/apply', {method:'POST',
    headers:{'Content-Type':'application/json'}, body:'{}'});
};
$('preset').onchange = async e => {
  const r = await fetch('/api/preset', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name:e.target.value})});
  const j = await r.json();
  if(j.error){ alert(j.error); }
  refresh();
};
$('probe').onclick = async () => {
  const box = $('sensors');
  box.style.display = 'block';
  box.textContent = 'reading ' + st.lhm_url + ' \\u2026';
  const j = await (await fetch('/api/sensors')).json();
  let out = '';
  if(j.error) out += 'error: ' + j.error + '\\n\\n';
  out += 'GPU rows sent to the panel:\\n';
  out += (j.rows.length ? j.rows.map(r => '  ' + r[0] + '  ' + r[1]).join('\\n')
                        : '  (none)') + '\\n\\n';
  out += 'hardware LHM reports:\\n';
  out += (j.hardware.length ? j.hardware.map(h => '  ' + h).join('\\n')
                            : '  (none)');
  box.textContent = out;
};

function bumpPreview(){
  $('pv').src = '/api/preview.jpg?t=' + Date.now();
}
$('pv').onerror = () => { $('pv').removeAttribute('src'); };
setInterval(bumpPreview, 1000);
bumpPreview();
for(const id of ['interval_ms','quality','canvas_w','canvas_h','slide_seconds'])
  $(id).onchange = e => patch({[id]: +e.target.value});
for(const id of ['slide_shuffle','flip','show_gpu','hold'])
  $(id).onchange = e => patch({[id]: e.target.checked});
$('wake').onclick = async () => {
  await fetch('/api/wake', {method:'POST',
    headers:{'Content-Type':'application/json'}, body:'{}'});
  refresh(); };


refresh();
setInterval(refresh, 2000);
</script></body></html>
"""

INT_FIELDS = {
    "brightness": (0, 100),
    "interval_ms": (100, 2000),
    "canvas_w": (64, 4096),
    "canvas_h": (64, 4096),
    "quality": (40, 95),
    "slide_seconds": (2, 600),
}

COLOR_FIELDS = ("color_bg",)


def apply_patch(patch):
    """Validate and merge a patch into shared state. Returns the new state."""
    global _weather_invalidate
    settings_dirty = False
    with state_lock:
        for key, value in patch.items():
            if key in INT_FIELDS:
                low, high = INT_FIELDS[key]
                try:
                    value = max(low, min(high, int(value)))
                except (TypeError, ValueError):
                    continue
                if key == "brightness":
                    runtime["apply_brightness"] = True
                if key in ("canvas_w", "canvas_h"):
                    runtime["reload_media"] = True
                state[key] = value
            elif key == "items":
                state["items"] = layout.normalise_all(value)
            elif key == "preset" and isinstance(value, str):
                state["preset"] = value[:60]
            elif key in COLOR_FIELDS and isinstance(value, str):
                state[key] = value.strip()[:9]
            elif key == "layout" and value in ("horizontal", "vertical"):
                state["layout"] = value
                runtime["reload_media"] = True   # authoring canvas changed
            elif key == "flip":
                state["flip"] = bool(value)
            elif key == "lhm_url" and isinstance(value, str):
                state["lhm_url"] = value.strip()[:300]
            elif key == "show_gpu":
                state["show_gpu"] = bool(value)
            elif key == "notify_mirror":
                state["notify_mirror"] = bool(value)
                settings_dirty = True
            elif key == "weather_place" and isinstance(value, str):
                state["weather_place"] = value.strip()[:80]
                _weather_invalidate = True
                settings_dirty = True
            elif key == "weather_country" and isinstance(value, str):
                code = value.strip().upper()[:2]
                # Blank = no country filter (search worldwide).
                state["weather_country"] = code if code.isalpha() else ""
                _weather_invalidate = True
                settings_dirty = True
            elif key == "weather_units" and value in ("C", "F"):
                state["weather_units"] = value
                _weather_invalidate = True
                settings_dirty = True
            elif key == "weather_lang" and value in ("el", "en"):
                state["weather_lang"] = value
                _weather_invalidate = True
                settings_dirty = True
            elif key == "hold":
                state["hold"] = bool(value)
            elif key == "mode" and value in ("clock", "media", "slideshow"):
                state["mode"] = value
                runtime["reload_media"] = True
                # Hold keeps last_jpeg on the wire until Apply; mode change
                # alone must not force a push (that would defeat Hold).
            elif key == "fit" and value in ("cover", "letterbox"):
                state["fit"] = value
                runtime["reload_media"] = True
            elif key == "media":
                # Basename only so a crafted path cannot leave media/.
                if value is None or value == "":
                    state["media"] = None
                elif isinstance(value, str):
                    base = os.path.basename(value.strip())[:200]
                    state["media"] = None if (not base or base in (".", "..")) \
                        else base
                else:
                    state["media"] = None
                runtime["reload_media"] = True
            elif key == "slide_shuffle":
                state["slide_shuffle"] = bool(value)
                runtime["reload_media"] = True
        snapshot = dict(state)
    if settings_dirty:
        # Persist outside the lock: disk I/O must not stall apply_patch callers.
        save_settings()
    return snapshot


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # keep the console clean for device messages

    def _json(self, payload, code=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return None

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/state":
            with state_lock:
                snapshot = dict(state)
            rt = {k: runtime[k] for k in
                  ("status", "message", "frames", "last_ms")}
            self._json({"state": snapshot, "runtime": rt,
                        "media": list_media(),
                        "presets": layout.list_presets(HERE)})
        elif self.path.startswith("/api/preview.jpg"):
            with preview_lock:
                body = preview["jpeg"]
            if not body:
                self._json({"error": "no frame yet"}, 503)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/sensors":
            with state_lock:
                url = state["lhm_url"]
            values = fetch_lhm(url)        # blocking, but off the render loop
            with _lhm_lock:
                self._json({"url": url,
                            "rows": sorted([k] + list(v)
                                           for k, v in values.items()),
                            "hardware": list(_lhm_cache["hardware"]),
                            "error": _lhm_cache["error"]})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        payload = self._read_json()
        if payload is None:
            self._json({"error": "bad json"}, 400)
            return
        if self.path == "/api/state":
            self._json({"state": apply_patch(payload)})
        elif self.path == "/api/apply":
            with state_lock:
                runtime["apply_once"] = True
            self._json({"result": "queued"})
        elif self.path == "/api/wake":
            # The render thread owns every device write, so just ask it.
            with state_lock:
                runtime["wake"] = True
            self._json({"result": "queued"})
        elif self.path == "/api/preset":
            name = str(payload.get("name") or "")
            try:
                items, scene = layout.load_preset(HERE, name)
            except Exception as exc:
                self._json({"error": "cannot load preset: %s" % exc}, 400)
                return
            patch = {"items": items, "preset": name}
            if scene:
                patch.update(scene)
            self._json({"state": apply_patch(patch)})

        else:
            self._json({"error": "not found"}, 404)


# ------------------------------------------------------------------- main

def main():
    os.makedirs(MEDIA_DIR, exist_ok=True)
    os.makedirs(layout.preset_dir(HERE), exist_ok=True)
    load_settings()

    print("Opening the D92 (close the official MiraBox software first)...")
    try:
        panel = D92().open()
    except Exception as exc:
        print("Cannot open the device: %s" % exc)
        print("Check it is plugged in and that no other app holds it.")
        return 1

    print("Sending wake sequence (DIS -> 450 ms -> LIG)...")
    panel.wake(state["brightness"])

    worker = threading.Thread(target=render_loop, args=(panel,), daemon=True)
    worker.start()

    server = ThreadingHTTPServer((WEB_HOST, PORT), Handler)
    urls = phone_urls() or ["http://127.0.0.1:%d" % PORT]
    print("\nControl panel (this PC):  http://127.0.0.1:%d" % PORT)
    print("Phone on same Wi-Fi:      %s" % urls[0])
    if len(urls) > 1:
        for extra in urls[1:]:
            print("                         %s" % extra)
    print("Media folder :  %s" % MEDIA_DIR)
    print("Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        server.server_close()
        panel.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
