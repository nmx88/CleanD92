#!/usr/bin/env python3
"""
d92_panel.py -- control panel for the MiraBox StreamDock D92.

Run this, open http://127.0.0.1:8092 in a browser, and drive the panel from
there: pick an image or animated GIF, overlay a clock and CPU/RAM readout,
set brightness and refresh interval. Nothing is uploaded anywhere; media is
read from the ./media folder next to this script.

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
import os
import random
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
EXTENSIONS = (".gif", ".png", ".jpg", ".jpeg", ".bmp", ".webp")
# Soft ceiling for a drop / import. Longer GIFs are fine once they live in
# media/; this only stops someone dragging a multi-GB file onto the window.
MAX_IMPORT_BYTES = 80 * 1024 * 1024

HOST = "127.0.0.1"
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
    "color_bg": "#080a0e",
    # The layout: a list of positioned items. See d92_layout for the schema.
    "items": layout.normalise_all(layout.DEFAULT_PRESETS["Wide dashboard"]),
    "preset": "Wide dashboard",
    # Draft mode: keep building frames and refreshing the preview, but keep
    # re-sending the last frame the panel already has. Lets you audition a
    # file or drag a layout about without the panel changing, and without
    # letting the OUT endpoint go idle -- which is what blacks it out.
    "hold": False,
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
    return name


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
    img = Image.open(os.path.join(MEDIA_DIR, name))
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
        fit_frame(frame, width, height, fit).save(
            buf, format="JPEG", quality=quality)
        frames.append((buf.getvalue(), duration))

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
    """Ask the loader thread for `name`. Returns immediately."""
    with _loader["lock"]:
        if _loader["busy"] or _loader["want"] == name:
            return
        _loader["busy"] = True
        _loader["want"] = name

    def work():
        try:
            frames, error = cached_media(name, cfg, size), ""
        except Exception as exc:
            frames, error = None, str(exc)
        with _loader["lock"]:
            _loader["done"] = (name, frames, error)
            _loader["busy"] = False

    threading.Thread(target=work, daemon=True).start()


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
    re-decode and re-scale the same files on every pass. Canvas size and fit
    are part of the key, so changing either naturally invalidates entries.

    Eviction runs before the new file is admitted and is allowed to empty the
    cache completely -- the earlier version kept one entry back, which meant
    a single very long GIF could never be evicted."""
    width, height = size
    key = (name, width, height, cfg["fit"])
    with _cache_lock:
        if key in _cache:
            return _cache[key]

    frames = load_media(name, width, height, cfg["fit"])
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
                temp = pick(found, "\u00b0", ("hot spot", "hotspot", "gpu core",
                                              "core", "edge", "temperature"),
                            reject=("warning", "critical", "limit"))
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
                # Package first when present; Core Average / Max on modern
                # Intel trees that only expose per-core and aggregates.
                temp = pick(found, "\u00b0", ("tctl/tdie", "tctl", "package",
                                               "core average", "core max",
                                               "cpu", "temperature"),
                            reject=("warning", "critical", "limit", "distance"))
                if temp is not None:
                    values["cpu_temp"] = ("CPU TEMP", "%.0f\u00b0C" % temp)

            elif category == "storage":
                index = counts["storage"]
                counts["storage"] += 1
                # Composite / plain Temperature before Warning/Critical thresholds.
                temp = pick(found, "\u00b0", ("composite temperature",
                                               "temperature",),
                            reject=("warning", "critical", "limit"))
                if temp is not None:
                    values["disk%d_temp" % index] = (
                        "%s TEMP" % shorten(name, 10), "%.0f\u00b0C" % temp)
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

_weather_cache = {
    "t": 0.0,
    "place_key": "",
    "data": None,             # dict for layout.render_weather, or None
    "error": "",
}
_weather_lock = threading.Lock()
_weather_started = False
_weather_invalidate = False   # set from apply_patch without taking _weather_lock


def weather_snapshot():
    """Non-blocking view of the last forecast. Never raises."""
    with _weather_lock:
        data = dict(_weather_cache["data"]) if _weather_cache["data"] else None
        return data, _weather_cache["error"], _weather_cache["t"]


def _parse_weather_query(place, country):
    """'Galatsi, GR' -> ('Galatsi', 'GR'). Bare names keep the country bias."""
    text = (place or "").strip()
    country = (country or "").strip().upper()[:2]
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


def fetch_weather(place, country="GR", units="C"):
    """Resolve a place name and pull current + 7-day forecast. BLOCKING."""
    import urllib.parse
    import urllib.request

    name, country = _parse_weather_query(place, country)
    if not name:
        with _weather_lock:
            _weather_cache.update({"t": time.monotonic(), "data": None,
                                   "error": "no place set", "place_key": ""})
        return None

    place_key = "%s|%s|%s" % (name.lower(), country, units)
    try:
        query = {"name": name, "count": 5, "language": "el", "format": "json"}
        if country:
            query["countryCode"] = country
        geo_url = ("https://geocoding-api.open-meteo.com/v1/search?"
                   + urllib.parse.urlencode(query))
        with urllib.request.urlopen(geo_url, timeout=WEATHER_TIMEOUT) as resp:
            geo = json.loads(resp.read().decode("utf-8", "replace"))
        results = geo.get("results") or []
        if not results and country:
            # Retry without the country filter so a mistyped GR still resolves.
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
        admin = chosen.get("admin1") or ""
        code = (chosen.get("country_code") or country or "").upper()
        if code:
            label_bits.append(code)
        place_label = ", ".join(label_bits)
        if admin and admin.lower() not in place_label.lower():
            # Keep strip labels short: "Galatsi, GR" not the full admin chain.
            pass

        forecast_url = (
            "https://api.open-meteo.com/v1/forecast?"
            + urllib.parse.urlencode({
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,weather_code",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min",
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
        for index, day in enumerate(times):
            try:
                # daily.time is YYYY-MM-DD; weekday in local English short form
                # is fine on the panel -- Greek locales vary by Windows install.
                stamp = time.strptime(day, "%Y-%m-%d")
                day_name = time.strftime("%a", stamp)
            except (TypeError, ValueError):
                day_name = day[-5:] if day else "?"
            code_val = codes[index] if index < len(codes) else 0
            high = highs[index] if index < len(highs) else None
            low = lows[index] if index < len(lows) else None
            days.append({
                "name": day_name,
                "code": code_val,
                "temps": "%s/%s" % (_format_temp(low, units),
                                    _format_temp(high, units)),
            })

        data = {
            "place": place_label[:28],
            "temp": _format_temp(current.get("temperature_2m"), units),
            "code": current.get("weather_code", 0),
            "daily": days,
        }
        with _weather_lock:
            _weather_cache.update({"t": time.monotonic(), "data": data,
                                   "error": "", "place_key": place_key})
        return data
    except Exception as exc:
        with _weather_lock:
            _weather_cache.update({"t": time.monotonic(), "data": None,
                                   "error": str(exc)[:120],
                                   "place_key": place_key})
        return None


def weather_poller():
    while True:
        with state_lock:
            place = state.get("weather_place", "")
            country = state.get("weather_country", "GR")
            units = state.get("weather_units", "C")
            items = list(state.get("items") or [])
            stopping = runtime["stop"]
        if stopping:
            return
        # Only hit the network when a layout actually shows weather -- keeps
        # offline / first-run machines from paying for a feature they unused.
        needed = any(i.get("type") == "weather" for i in items)
        if needed and (place or "").strip():
            global _weather_invalidate
            force = _weather_invalidate
            if force:
                _weather_invalidate = False
            key = "%s|%s|%s" % (
                _parse_weather_query(place, country)[0].lower(),
                _parse_weather_query(place, country)[1], units)
            with _weather_lock:
                fresh = (not force
                         and _weather_cache["place_key"] == key
                         and _weather_cache["data"]
                         and time.monotonic() - _weather_cache["t"] < WEATHER_POLL)
            if not fresh:
                fetch_weather(place, country, units)
        time.sleep(5)    # notice place / item edits quickly; fetch stays gated


def start_weather_poller():
    global _weather_started
    if _weather_started:
        return
    _weather_started = True
    threading.Thread(target=weather_poller, daemon=True).start()


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

    # Weather: structured blob for layout.render_weather. Placeholder keeps
    # the item selectable while the poller is catching up or offline.
    if any(i.get("type") == "weather" for i in cfg.get("items") or []):
        snap, err, stamp = weather_snapshot()
        if snap:
            values["weather"] = snap
        else:
            place = (cfg.get("weather_place") or "weather").strip() or "weather"
            if "," not in place and cfg.get("weather_country"):
                place = "%s, %s" % (place, cfg["weather_country"])
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
            values["weather"] = {
                "place": place[:28],
                "temp": note,
                "code": 0,
                "daily": [{"name": "\u2014", "code": 0, "temps": note}] * 3,
            }
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

            now = time.monotonic()

            # -- a load that finished on the loader thread ---------------
            finished = collect_load()
            if finished:
                done_name, done_frames, done_error = finished
                loaded = done_name          # even on failure, so we stop asking
                if done_frames:
                    frames, index, due = done_frames, 0, 0.0
                    runtime["message"] = "%s -- %d frame(s)%s" % (
                        done_name, len(done_frames),
                        "" if cfg["mode"] != "slideshow"
                        else "  [%d/%d]" % (pos + 1, max(1, len(playlist))))
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
                    playlist = list_media()
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
                if not list_media():
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
                canvas = Image.open(io.BytesIO(frames[index][0]))
                canvas = canvas.convert("RGB")
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
                scale = PREVIEW_LONG_EDGE / max(shot.size)
                if scale < 1:
                    shot = shot.resize(
                        (max(1, int(shot.width * scale)),
                         max(1, int(shot.height * scale))), Image.LANCZOS)
                buf = io.BytesIO()
                shot.save(buf, format="JPEG", quality=70)
                with preview_lock:
                    preview["jpeg"] = buf.getvalue()
                    preview["t"] = now

            if rotation:
                canvas = canvas.rotate(rotation, expand=True)

            if cfg["hold"] and last_jpeg is not None and not apply_once:
                # Draft mode: the preview above is live, the panel is not.
                # Still a real push, so the endpoint never goes idle.
                panel.send_jpeg(last_jpeg)
            else:
                last_jpeg = encode(canvas, cfg["quality"])
                panel.send_jpeg(last_jpeg)

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
            elif key == "weather_place" and isinstance(value, str):
                state["weather_place"] = value.strip()[:80]
                _weather_invalidate = True
            elif key == "weather_country" and isinstance(value, str):
                code = value.strip().upper()[:2]
                state["weather_country"] = code if code.isalpha() else "GR"
                _weather_invalidate = True
            elif key == "weather_units" and value in ("C", "F"):
                state["weather_units"] = value
                _weather_invalidate = True
            elif key == "hold":
                state["hold"] = bool(value)
            elif key == "mode" and value in ("clock", "media", "slideshow"):
                state["mode"] = value
                runtime["reload_media"] = True
            elif key == "fit" and value in ("cover", "letterbox"):
                state["fit"] = value
                runtime["reload_media"] = True
            elif key == "media":
                state["media"] = value or None
                runtime["reload_media"] = True
            elif key == "slide_shuffle":
                state["slide_shuffle"] = bool(value)
                runtime["reload_media"] = True
        return dict(state)


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
                items = layout.load_preset(HERE, name)
            except Exception as exc:
                self._json({"error": "cannot load preset: %s" % exc}, 400)
                return
            self._json({"state": apply_patch({"items": items,
                                              "preset": name})})

        else:
            self._json({"error": "not found"}, 404)


# ------------------------------------------------------------------- main

def main():
    os.makedirs(MEDIA_DIR, exist_ok=True)
    os.makedirs(layout.preset_dir(HERE), exist_ok=True)

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

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("\nControl panel:  http://%s:%d" % (HOST, PORT))
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
