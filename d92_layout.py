#!/usr/bin/env python3
"""
d92_layout.py -- the layout model for the StreamDock D92 panel.

A layout is a flat list of items, each one a dict. Items carry their position
as FRACTIONS of the canvas, never pixels:

    {"id": "clock1", "type": "clock", "x": 0.03, "y": 0.10,
     "size": 0.40, "color": "#ebf0fa", "align": "left"}

Fractions are the whole point. The same saved preset then works on the wide
1920x462 canvas and the tall 462x1920 one without conversion, and it will
still work if a future panel model has different dimensions. `size` is a
fraction of the canvas's SHORT edge, so type scales with the strip rather
than stretching with it.

This module is deliberately free of device, sensor and UI code: it takes the
values to display as a plain dict and two font helpers, so it can be unit
tested on its own. render() also returns the bounding box of everything it
drew, which is what makes items draggable in the editor.
"""

import copy
import itertools
import json
import math
import os
import re
import time
from datetime import datetime, timezone

from PIL import ImageDraw

# type -> human label, shown in the editor
ITEM_TYPES = {
    "clock": "Clock",
    "date": "Date",
    "stat": "Readout",
    "text": "Fixed text",
    "filename": "Current file name",
    "weather": "Weather",
    "worldclock": "World clock",
}

# Weather item `format` chooses how much forecast to draw. The place and
# units live in shared state, not on the item, so one city drives every
# weather readout on the canvas (unless the item overrides via text).
WEATHER_FORMATS = ("now", "1", "2", "3", "4", "5", "6", "7")
# Older presets used these names; normalise() rewrites them.
_WEATHER_FORMAT_ALIASES = {"3day": "3", "week": "7"}

# A readout's source is a key into the value dict the host supplies. The set
# is dynamic -- one entry per fixed drive, one per GPU LibreHardwareMonitor
# reports -- so only the shape is validated here, not membership of a fixed
# list. d92_panel.available_sources() is what the editor lists.
SOURCE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_]{0,30}$")
FALLBACK_SOURCE = "cpu"

ALIGNMENTS = ("left", "center", "right")

DEFAULTS = {
    "type": "text",
    "source": "cpu",
    "text": "",
    "format": "",
    "x": 0.05,
    "y": 0.10,
    "size": 0.18,
    "width": 0.0,          # 0 means "to the right edge"
    "color": "#96d7ff",
    "label_color": "#6e7d96",
    "show_label": True,
    "align": "left",
}


_counter = itertools.count(1)


def new_item(kind="text", **overrides):
    # A monotonic counter, not a timestamp: items created inside the same
    # millisecond would otherwise share an id, and identical ids silently
    # collapse into one entry in the render box map -- which breaks both
    # selection and dragging.
    item = dict(DEFAULTS)
    item["type"] = kind
    item["id"] = "%s-%d" % (kind, next(_counter))
    item.update(overrides)
    return item


def normalise(item, index=0):
    """Coerce anything loaded from disk or sent over HTTP into a safe item.
    Never raises: a malformed preset should lose a field, not the app."""
    clean = dict(DEFAULTS)
    if isinstance(item, dict):
        clean.update({k: v for k, v in item.items() if k in DEFAULTS})
    clean["id"] = str(item.get("id") or "item-%d" % index)[:40] \
        if isinstance(item, dict) else "item-%d" % index

    if clean["type"] not in ITEM_TYPES:
        clean["type"] = "text"
    if not SOURCE_PATTERN.match(str(clean["source"] or "")):
        clean["source"] = FALLBACK_SOURCE
    if clean["align"] not in ALIGNMENTS:
        clean["align"] = "left"
    for key, low, high in (("x", -0.2, 1.2), ("y", -0.2, 1.2),
                           ("size", 0.02, 1.5), ("width", 0.0, 1.5)):
        try:
            clean[key] = max(low, min(high, float(clean[key])))
        except (TypeError, ValueError):
            clean[key] = DEFAULTS[key]
    for key in ("color", "label_color"):
        value = str(clean[key] or "")
        clean[key] = value if value.startswith("#") and len(value) in (4, 7) \
            else DEFAULTS[key]
    clean["show_label"] = bool(clean["show_label"])
    clean["text"] = str(clean["text"] or "")[:60]
    clean["format"] = str(clean["format"] or "")[:40]
    if clean["type"] == "weather":
        fmt = clean["format"].lower().strip()
        fmt = _WEATHER_FORMAT_ALIASES.get(fmt, fmt)
        if fmt not in WEATHER_FORMATS:
            clean["format"] = "now"
        else:
            clean["format"] = fmt
        # Per-item place override (text) is capped at now / 1 day so a second
        # city never silently eats a week of bandwidth on the strip.
        if (clean.get("text") or "").strip() and clean["format"] not in ("now", "1"):
            clean["format"] = "now"
    return clean


def normalise_all(items):
    """Clean a whole list and guarantee unique ids. A preset saved by an
    older build, or hand-edited JSON, can easily contain duplicates; those
    would collapse in the render box map and break selection."""
    if not isinstance(items, (list, tuple)):
        return []
    cleaned = []
    seen = set()
    for index, item in enumerate(items[:24]):
        entry = normalise(item, index)
        if entry["id"] in seen:
            entry["id"] = "%s-%d" % (entry["type"], next(_counter))
        seen.add(entry["id"])
        cleaned.append(entry)
    return cleaned


# ----------------------------------------------------------------- presets

# Built-ins use only out-of-the-box sources (psutil). GPU / LHM readouts are
# easy to add once Poll LibreHardwareMonitor is on -- shipping them in the
# default layout left every new user staring at "no LHM" on first run.
# disk_c matches available_sources(); a bare "disk" key never resolves.
DEFAULT_PRESETS = {
    "Wide dashboard": [
        new_item("clock", x=0.03, y=0.08, size=0.42, color="#ebf0fa"),
        new_item("date", x=0.03, y=0.62, size=0.14, color="#6e7d96"),
        new_item("stat", source="cpu", x=0.50, y=0.06, size=0.19),
        new_item("stat", source="ram", x=0.50, y=0.40, size=0.19),
        new_item("stat", source="uptime", x=0.50, y=0.70, size=0.19),
        new_item("stat", source="net_down", x=0.76, y=0.06, size=0.19),
        new_item("stat", source="net_up", x=0.76, y=0.40, size=0.19),
        new_item("stat", source="disk_c", x=0.76, y=0.70, size=0.19),
    ],
    "Wide clock only": [
        new_item("clock", x=0.05, y=0.12, size=0.62, color="#ebf0fa"),
        new_item("date", x=0.06, y=0.78, size=0.14, color="#6e7d96"),
    ],
    "Tall clock only": [
        new_item("clock", x=0.08, y=0.18, size=0.42, color="#ebf0fa",
                 format="%H:%M"),
        new_item("date", x=0.08, y=0.32, size=0.14, color="#6e7d96",
                 format="%d %b"),
    ],
    "Media with caption": [
        new_item("clock", x=0.03, y=0.06, size=0.34, color="#ffffff"),
        new_item("filename", x=0.03, y=0.80, size=0.13, color="#ffffff",
                 show_label=False),
        new_item("stat", source="cpu", x=0.70, y=0.08, size=0.16),
        new_item("stat", source="ram", x=0.70, y=0.50, size=0.16),
    ],
    "Tall dashboard": [
        new_item("clock", x=0.07, y=0.03, size=0.34, color="#ebf0fa",
                 format="%H:%M"),
        new_item("date", x=0.07, y=0.09, size=0.13, color="#6e7d96",
                 format="%d %b"),
        new_item("stat", source="cpu", x=0.07, y=0.16, size=0.20),
        new_item("stat", source="ram", x=0.07, y=0.26, size=0.20),
        new_item("stat", source="uptime", x=0.07, y=0.36, size=0.20),
        new_item("stat", source="net_down", x=0.07, y=0.46, size=0.20),
        new_item("stat", source="net_up", x=0.07, y=0.56, size=0.20),
        new_item("filename", x=0.07, y=0.90, size=0.10, show_label=False,
                 color="#ffffff"),
    ],
}


def preset_dir(base):
    return os.path.join(base, "presets")


def list_presets(base):
    """Built-in names first, then whatever the user has saved."""
    names = list(DEFAULT_PRESETS)
    folder = preset_dir(base)
    if os.path.isdir(folder):
        for entry in sorted(os.listdir(folder)):
            if entry.lower().endswith(".json"):
                name = entry[:-5]
                if name not in names:
                    names.append(name)
    return names


# Keys stored beside `items` in a user preset. hold / canvas size stay out:
# hold is a draft toggle, canvas dimensions are fixed by the panel.
SCENE_KEYS = (
    "mode", "media", "brightness", "interval_ms",
    "layout", "flip", "fit", "quality", "color_bg",
    "slide_seconds", "slide_shuffle",
    "show_gpu", "lhm_url",
    "weather_place", "weather_country", "weather_units", "weather_lang",
)


def scene_from_state(state):
    """Snapshot the tunable fields a preset should restore."""
    scene = {}
    for key in SCENE_KEYS:
        if key not in state:
            continue
        value = state[key]
        if key == "media" and value is not None:
            value = str(value)[:200]
        scene[key] = value
    return scene


def normalise_scene(raw):
    """Keep only known scene keys with basic type checks. Never raises."""
    if not isinstance(raw, dict):
        return None
    clean = {}
    for key in SCENE_KEYS:
        if key not in raw:
            continue
        value = raw[key]
        if key in ("flip", "slide_shuffle", "show_gpu"):
            clean[key] = bool(value)
        elif key in ("brightness", "interval_ms", "quality", "slide_seconds"):
            try:
                clean[key] = int(value)
            except (TypeError, ValueError):
                continue
        elif key == "mode" and value not in ("clock", "media", "slideshow"):
            continue
        elif key == "layout" and value not in ("horizontal", "vertical"):
            continue
        elif key == "fit" and value not in ("cover", "letterbox"):
            continue
        elif key == "weather_units" and value not in ("C", "F"):
            continue
        elif key == "weather_lang" and value not in ("el", "en"):
            continue
        elif key == "media":
            if value is None or value == "":
                clean[key] = None
            elif isinstance(value, str):
                clean[key] = value[:200]
        elif key in ("weather_place", "weather_country", "lhm_url",
                     "color_bg") and isinstance(value, str):
            clean[key] = value[:300] if key == "lhm_url" else value[:80]
        else:
            clean[key] = value
    return clean or None


def load_preset(base, name):
    """Return (items, scene_or_None). Built-ins and old files have no scene."""
    path = os.path.join(preset_dir(base), "%s.json" % name)
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            items = normalise_all(data.get("items"))
            scene = normalise_scene(data.get("scene"))
        else:
            items = normalise_all(data)
            scene = None
        return items, scene
    if name in DEFAULT_PRESETS:
        return normalise_all(copy.deepcopy(DEFAULT_PRESETS[name])), None
    raise KeyError(name)


def save_preset(base, name, items, scene=None):
    safe = "".join(c for c in name if c.isalnum() or c in " _-()").strip()
    if not safe:
        raise ValueError("preset name cannot be empty")
    folder = preset_dir(base)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "%s.json" % safe)
    payload = {"name": safe, "items": normalise_all(items)}
    cleaned = normalise_scene(scene) if scene is not None else None
    if cleaned:
        payload["scene"] = cleaned
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    return safe


def delete_preset(base, name):
    path = os.path.join(preset_dir(base), "%s.json" % name)
    if os.path.isfile(path):
        os.remove(path)
        return True
    return False       # built-ins cannot be deleted


# ------------------------------------------------------------------ render

def hex_rgb(value, fallback=(255, 255, 255)):
    text = (value or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    if len(text) != 6:
        return fallback
    try:
        return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return fallback


def item_strings(item, values):
    """(label, value) for an item, or (None, None) to draw nothing."""
    kind = item["type"]
    if kind == "clock":
        return None, time.strftime(item["format"] or "%H:%M:%S")
    if kind == "date":
        return None, time.strftime(item["format"] or "%a %d %b %Y")
    if kind == "text":
        return None, item["text"]
    if kind == "filename":
        name = values.get("filename") or ""
        if item["format"]:
            try:
                name = item["format"] % name
            except (TypeError, ValueError):
                pass
        return ("FILE" if item["show_label"] else None), name
    if kind == "stat":
        pair = values.get(item["source"])
        if not pair:
            return None, None
        label, value = pair
        return (label if item["show_label"] else None), value
    if kind == "weather":
        # Listbox / hit-test label only; render() draws the real glyph.
        weather = values.get("weather") or {}
        place = weather.get("place") or "weather"
        return None, "%s (%s)" % (place, item.get("format") or "now")
    return None, None


# Synodic month and a known new-moon epoch (UTC). Phase is a global
# property of the date -- no API, and the same icon worldwide at a given
# instant. Northern-hemisphere lighting (waxing lit on the right).
_MOON_SYNODIC = 29.530588853
_MOON_NEW_EPOCH = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)
_MOON_PHASES = (
    "moon_new",
    "moon_waxing_crescent",
    "moon_first_quarter",
    "moon_waxing_gibbous",
    "moon_full",
    "moon_waning_gibbous",
    "moon_last_quarter",
    "moon_waning_crescent",
)
_MOON_DARK = (8, 10, 14)


def moon_phase_kind(when=None):
    """Map a UTC instant to one of eight drawable moon glyphs."""
    when = when or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    else:
        when = when.astimezone(timezone.utc)
    age = ((when - _MOON_NEW_EPOCH).total_seconds() / 86400.0) % _MOON_SYNODIC
    # +0.5 centres each bucket on the named phase (new, quarters, full).
    index = int((age / _MOON_SYNODIC) * 8 + 0.5) % 8
    return _MOON_PHASES[index]


def weather_glyph_kind(code, is_day=True, when=None):
    """Map Open-Meteo WMO weather codes to drawable glyphs.

    `is_day` comes from current.is_day: clear / partly-clear at night become
    the current moon phase rather than a misleading sun."""
    try:
        code = int(code)
    except (TypeError, ValueError):
        code = 0
    day = bool(is_day)
    if code == 0:
        return "sun" if day else moon_phase_kind(when)
    if code in (1, 2):
        return "part" if day else "part_night"
    if code == 3:
        return "cloud"
    if code in (45, 48):
        return "fog"
    if 51 <= code <= 67 or 80 <= code <= 82:
        return "rain"
    if 71 <= code <= 77 or 85 <= code <= 86:
        return "snow"
    if code >= 95:
        return "storm"
    return "cloud"


# Per-kind colours so rain is not the same ink as the temperature text.
# Item `color` still paints the temp / place strings.
GLYPH_COLOURS = {
    "sun": (255, 196, 72),
    "moon": (196, 210, 235),
    "part": (255, 196, 72),
    "part_night": (196, 210, 235),
    "cloud": (160, 170, 185),
    "rain": (80, 160, 230),
    "snow": (210, 230, 255),
    "fog": (150, 155, 165),
    "storm": (180, 140, 255),
    # Sunrise stays with the day-sun yellow; sunset shifts coral so the
    # pair reads apart at the strip's small size without needing arrows.
    "sunrise": (255, 196, 72),
    "sunset": (255, 140, 80),
}
STORM_BOLT = (255, 220, 90)


def _draw_moon_phase(draw, cx, cy, radius, kind, colour):
    """Bright disc with a dark bite. Kind is one of _MOON_PHASES (or
    legacy "moon", which resolves to tonight's phase)."""
    if kind == "moon":
        kind = moon_phase_kind()
    rad = max(4, int(radius * 0.7))
    box = (cx - rad, cy - rad, cx + rad, cy + rad)
    if kind == "moon_new":
        # Dim disc rather than an outline -- outlines vanish at strip size.
        dim = tuple(max(0, int(c * 0.28)) for c in colour)
        draw.ellipse(box, fill=dim, outline=dim)
        return
    draw.ellipse(box, fill=colour, outline=colour)
    if kind == "moon_full":
        return
    if kind == "moon_first_quarter":
        # Lit on the right (Northern waxing).
        draw.rectangle((cx - rad - 1, cy - rad - 1, cx, cy + rad + 1),
                       fill=_MOON_DARK)
        return
    if kind == "moon_last_quarter":
        draw.rectangle((cx, cy - rad - 1, cx + rad + 1, cy + rad + 1),
                       fill=_MOON_DARK)
        return
    lit_right = kind in ("moon_waxing_crescent", "moon_waxing_gibbous")
    gibbous = kind in ("moon_waxing_gibbous", "moon_waning_gibbous")
    # Punch with near-black so phases read on the dark panel without
    # needing the true canvas colour (same trick as the old crescent).
    if gibbous:
        if lit_right:
            bite = (cx - int(rad * 1.55), cy - rad,
                    cx - int(rad * 0.05), cy + rad)
        else:
            bite = (cx + int(rad * 0.05), cy - rad,
                    cx + int(rad * 1.55), cy + rad)
    else:
        if lit_right:
            bite = (cx - int(rad * 1.25), cy - rad,
                    cx + int(rad * 0.15), cy + rad)
        else:
            bite = (cx - int(rad * 0.15), cy - rad,
                    cx + int(rad * 1.25), cy + rad)
    draw.ellipse(bite, fill=_MOON_DARK, outline=_MOON_DARK)


def draw_weather_glyph(draw, cx, cy, radius, kind, colour=None):
    """Filled vector icons sized for the strip -- no image assets in the exe."""
    r = max(5, int(radius))
    w = max(1, r // 6)
    if colour is None:
        if kind == "moon" or kind.startswith("moon_"):
            colour = GLYPH_COLOURS["moon"]
        else:
            colour = GLYPH_COLOURS.get(kind, (200, 200, 200))

    def disc(x, y, rad, fill=True, ink=None):
        ink = ink or colour
        box = (x - rad, y - rad, x + rad, y + rad)
        if fill:
            draw.ellipse(box, fill=ink, outline=ink)
        else:
            draw.ellipse(box, outline=ink, width=max(2, w))

    if kind == "sun":
        disc(cx, cy, int(r * 0.55))
        for angle in range(0, 360, 45):
            rad = math.radians(angle)
            x0 = cx + int(r * 0.7 * math.cos(rad))
            y0 = cy + int(r * 0.7 * math.sin(rad))
            x1 = cx + int(r * 1.05 * math.cos(rad))
            y1 = cy + int(r * 1.05 * math.sin(rad))
            draw.line((x0, y0, x1, y1), fill=colour, width=max(2, w))
    elif kind == "moon" or kind.startswith("moon_"):
        _draw_moon_phase(draw, cx, cy, r, kind, colour)
    elif kind in ("part", "part_night"):
        # part_night uses tonight's phase, not a fixed crescent.
        body = "sun" if kind == "part" else moon_phase_kind()
        draw_weather_glyph(draw, cx - int(r * 0.25), cy - int(r * 0.35),
                           int(r * 0.55), body)
        cloud = GLYPH_COLOURS["cloud"]
        disc(cx + int(r * 0.15), cy + int(r * 0.2), int(r * 0.5), ink=cloud)
        disc(cx - int(r * 0.25), cy + int(r * 0.25), int(r * 0.38), ink=cloud)
        disc(cx + int(r * 0.45), cy + int(r * 0.3), int(r * 0.32), ink=cloud)
    elif kind == "cloud":
        disc(cx, cy + int(r * 0.15), int(r * 0.55))
        disc(cx - int(r * 0.45), cy + int(r * 0.2), int(r * 0.4))
        disc(cx + int(r * 0.45), cy + int(r * 0.25), int(r * 0.35))
        disc(cx - int(r * 0.1), cy - int(r * 0.25), int(r * 0.42))
    elif kind == "rain":
        draw_weather_glyph(draw, cx, cy - int(r * 0.15), int(r * 0.75), "cloud")
        for dx in (-int(r * 0.45), 0, int(r * 0.45)):
            draw.line((cx + dx, cy + int(r * 0.35),
                       cx + dx - int(r * 0.15), cy + int(r * 0.95)),
                      fill=colour, width=max(2, w))
    elif kind == "snow":
        draw_weather_glyph(draw, cx, cy - int(r * 0.15), int(r * 0.75), "cloud")
        for dx, dy in ((-int(r * 0.4), int(r * 0.45)),
                       (0, int(r * 0.7)),
                       (int(r * 0.4), int(r * 0.5))):
            x, y = cx + dx, cy + dy
            s = max(2, r // 5)
            draw.line((x - s, y, x + s, y), fill=colour, width=max(1, w))
            draw.line((x, y - s, x, y + s), fill=colour, width=max(1, w))
    elif kind == "fog":
        for i, dy in enumerate((-int(r * 0.55), -int(r * 0.15),
                                int(r * 0.25), int(r * 0.65))):
            inset = (i % 2) * int(r * 0.2)
            draw.line((cx - r + inset, cy + dy, cx + r - inset, cy + dy),
                      fill=colour, width=max(2, w))
    elif kind in ("sunrise", "sunset"):
        # Half-disc over a horizon. Pillow pieslice angles are clockwise
        # from 3 o'clock, so 180..360 is the upper half. Rays fan above
        # the horizon; sunrise reaches higher, sunset stays flatter.
        horizon_y = cy + int(r * 0.3)
        sun_r = max(3, int(r * 0.55))
        disc_box = (cx - sun_r, horizon_y - sun_r,
                    cx + sun_r, horizon_y + sun_r)
        draw.pieslice(disc_box, 180, 360, fill=colour, outline=colour)
        draw.line((cx - r, horizon_y, cx + r, horizon_y),
                  fill=colour, width=max(2, w))
        if kind == "sunrise":
            angles = (210, 240, 270, 300, 330)
            inner, outer = 0.72, 1.12
        else:
            angles = (220, 250, 270, 290, 320)
            inner, outer = 0.68, 0.98
        for angle in angles:
            rad = math.radians(angle)
            # Match the sun glyph: 0 right, 90 down, 270 up.
            x0 = cx + int(sun_r * inner * math.cos(rad))
            y0 = horizon_y + int(sun_r * inner * math.sin(rad))
            x1 = cx + int(r * outer * math.cos(rad))
            y1 = horizon_y + int(r * outer * math.sin(rad))
            if y1 > horizon_y:
                continue
            draw.line((x0, y0, x1, y1), fill=colour, width=max(2, w))
    else:  # storm
        draw_weather_glyph(draw, cx, cy - int(r * 0.2), int(r * 0.7), "cloud")
        bolt = [
            (cx - int(r * 0.1), cy + int(r * 0.05)),
            (cx + int(r * 0.35), cy + int(r * 0.05)),
            (cx - int(r * 0.05), cy + int(r * 0.45)),
            (cx + int(r * 0.15), cy + int(r * 0.45)),
            (cx - int(r * 0.35), cy + int(r * 1.05)),
            (cx - int(r * 0.05), cy + int(r * 0.55)),
            (cx - int(r * 0.25), cy + int(r * 0.55)),
        ]
        draw.polygon(bolt, fill=STORM_BOLT, outline=STORM_BOLT)



def render_weather(draw, item, weather, width, height, short, fitted,
                   text_height, translucent=False):
    """Draw a weather item. Returns the pixel box or None."""
    if not weather:
        return None
    left = item["x"] * width
    top = item["y"] * height
    span = (item["width"] or (1.0 - item["x"])) * width
    span = max(40.0, span - short * 0.02)
    colour = hex_rgb(item["color"], (150, 215, 255))
    label_colour = hex_rgb(item["label_color"], (110, 125, 150))
    halo = (0, 0, 0) if translucent else None
    fmt = item.get("format") or "now"
    rects = []

    def put(x, y, text, px, fill):
        font = fitted(text, max(20, span), px)
        box = font.getbbox(text) if hasattr(font, "getbbox") else (0, 0, 0, 0)
        drawn = box[2] - box[0]
        if halo:
            for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2)):
                draw.text((x + dx, y + dy), text, font=font, fill=halo)
        draw.text((x, y), text, font=font, fill=fill)
        return x, y, x + drawn, y + text_height(font, text)

    if fmt in ("now", "1"):
        # "1" is today's high/low column-style; for a single day with sun
        # times we still prefer the compact now layout plus sunrise/sunset.
        is_day = weather.get("is_day", True)
        glyph = weather_glyph_kind(weather.get("code"), is_day=is_day)
        radius = max(8, item["size"] * short * 0.35)
        cx = left + radius + 4
        cy = top + radius + 2
        draw_weather_glyph(draw, cx, cy, radius, glyph)
        rects.append((cx - radius - 4, cy - radius - 2,
                      cx + radius + 4, cy + radius + 4))
        text_x = cx + radius + 10
        temp = weather.get("temp") or "--"
        place = weather.get("place") or ""
        temp_px = max(10, item["size"] * short)
        cursor = top
        rects.append(put(text_x, cursor, temp, temp_px, colour))
        cursor += temp_px * 0.95
        if item["show_label"] and place:
            label_px = max(8, item["size"] * short * 0.45)
            rects.append(put(text_x, cursor, place, label_px, label_colour))
            cursor += label_px * 0.95
        sunrise = weather.get("sunrise") or ""
        sunset = weather.get("sunset") or ""
        if sunrise or sunset:
            # Vector sunrise/sunset glyphs beside the times -- replaces the
            # old ↑/↓ arrows which read as decoration rather than sun events.
            sun_px = max(7, item["size"] * short * 0.38)
            icon_r = max(5, int(sun_px * 0.55))
            gap = max(4, int(sun_px * 0.3))
            pair_gap = max(10, int(sun_px * 0.85))
            x = text_x
            icon_cy = cursor + max(icon_r, sun_px * 0.55)
            for kind, label in (("sunrise", sunrise or "--:--"),
                                ("sunset", sunset or "--:--")):
                draw_weather_glyph(draw, int(x + icon_r), int(icon_cy),
                                   icon_r, kind)
                rects.append((x, icon_cy - icon_r - 1,
                              x + 2 * icon_r, icon_cy + icon_r + 1))
                x += 2 * icon_r + gap
                box = put(x, cursor, label, sun_px, label_colour)
                rects.append(box)
                x = box[2] + pair_gap
    else:
        try:
            count = max(1, min(7, int(fmt)))
        except (TypeError, ValueError):
            count = 3
        days = (weather.get("daily") or [])[:count]
        if not days:
            return None
        # Pack columns to content size instead of stretching across the whole
        # item width -- a 2-day strip used to pin Wed left and Thu right with
        # a huge empty middle. 6-7 days still fill the span so a week fits.
        shrink = 1.0 if count <= 3 else (0.9 if count <= 5 else 0.7)
        natural = max(short * 0.13, item["size"] * short * 1.05) * shrink
        gap = short * (0.025 if count <= 5 else 0.008)
        if count <= 5:
            col_w = min(natural, span / count)
            block = count * col_w + (count - 1) * gap
            if item.get("align") == "center":
                origin = left + max(0.0, (span - block) / 2.0)
            elif item.get("align") == "right":
                origin = left + max(0.0, span - block)
            else:
                origin = left
        else:
            usable = max(40.0, span - gap * max(0, count - 1))
            col_w = usable / count
            origin = left
        glyph_r = max(5, min(col_w * 0.32, item["size"] * short * 0.28) * shrink)
        name_px = max(6, item["size"] * short * 0.32 * shrink)
        temp_px = max(7, item["size"] * short * 0.38 * shrink)
        for index, day in enumerate(days):
            col_left = origin + index * (col_w + gap)
            cx = col_left + col_w / 2.0
            name = day.get("name") or ""
            font = fitted(name, col_w * 0.95, name_px)
            box = font.getbbox(name) if hasattr(font, "getbbox") else (0, 0, 0, 0)
            nw = box[2] - box[0]
            nx = cx - nw / 2.0
            if halo:
                for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    draw.text((nx + dx, top + dy), name, font=font, fill=halo)
            draw.text((nx, top), name, font=font, fill=label_colour)
            rects.append((nx, top, nx + nw, top + text_height(font, name)))
            gy = top + name_px * 1.1 + glyph_r
            draw_weather_glyph(draw, cx, gy, glyph_r,
                               weather_glyph_kind(day.get("code"), is_day=True))
            rects.append((cx - glyph_r, gy - glyph_r,
                          cx + glyph_r, gy + glyph_r))
            temps = day.get("temps") or ""
            font = fitted(temps, col_w * 0.98, temp_px)
            box = font.getbbox(temps) if hasattr(font, "getbbox") else (0, 0, 0, 0)
            tw = box[2] - box[0]
            tx = cx - tw / 2.0
            ty = gy + glyph_r + 3
            if halo:
                for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    draw.text((tx + dx, ty + dy), temps, font=font, fill=halo)
            draw.text((tx, ty), temps, font=font, fill=colour)
            rects.append((tx, ty, tx + tw, ty + text_height(font, temps)))

    if not rects:
        return None
    return (min(r[0] for r in rects), min(r[1] for r in rects),
            max(r[2] for r in rects), max(r[3] for r in rects))


def render_worldclock(draw, item, info, width, height, short, fitted,
                      text_height, translucent=False):
    """Draw city + local wall time + offset vs the host timezone."""
    if not info:
        return None
    left = item["x"] * width
    top = item["y"] * height
    span = (item["width"] or (1.0 - item["x"])) * width
    span = max(40.0, span - short * 0.02)
    colour = hex_rgb(item["color"], (150, 215, 255))
    label_colour = hex_rgb(item["label_color"], (110, 125, 150))
    halo = (0, 0, 0) if translucent else None
    rects = []

    def put(x, y, text, px, fill):
        font = fitted(text, max(20, span), px)
        box = font.getbbox(text) if hasattr(font, "getbbox") else (0, 0, 0, 0)
        drawn = box[2] - box[0]
        if halo:
            for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2)):
                draw.text((x + dx, y + dy), text, font=font, fill=halo)
        draw.text((x, y), text, font=font, fill=fill)
        return x, y, x + drawn, y + text_height(font, text)

    label = info.get("label") or item.get("text") or "—"
    tz_name = info.get("timezone") or ""
    clock_text = "--:--"
    delta_text = ""
    if tz_name:
        try:
            from zoneinfo import ZoneInfo
            here = datetime.now().astimezone()
            there = here.astimezone(ZoneInfo(tz_name))
            clock_text = there.strftime("%H:%M")
            off_here = here.utcoffset() or timezone.utc.utcoffset(None)
            off_there = there.utcoffset() or timezone.utc.utcoffset(None)
            # Keep half-hour / :45 zones honest (India +5.5, Nepal +5.75).
            minutes = int(round(
                (off_there - off_here).total_seconds() / 60.0))
            if minutes == 0:
                delta_text = "same"
            else:
                sign = "+" if minutes > 0 else "\u2212"
                abs_m = abs(minutes)
                hours, mins = divmod(abs_m, 60)
                if mins == 0:
                    delta_text = "%s%dh" % (sign, hours)
                elif hours == 0:
                    delta_text = "%s%dm" % (sign, mins)
                else:
                    delta_text = "%s%d:%02d" % (sign, hours, mins)
        except Exception:
            clock_text = info.get("error") or "no tz"
    elif info.get("error"):
        clock_text = info["error"]
    elif info.get("pending"):
        clock_text = "reading\u2026"

    time_px = max(10, item["size"] * short)
    label_px = max(8, item["size"] * short * 0.45)
    rects.append(put(left, top, clock_text, time_px, colour))
    if item.get("show_label", True):
        line = label
        if delta_text:
            line = "%s  %s" % (label, delta_text)
        rects.append(put(left, top + time_px * 0.95, line,
                         label_px, label_colour))
    return (min(r[0] for r in rects), min(r[1] for r in rects),
            max(r[2] for r in rects), max(r[3] for r in rects))


def render(img, items, values, fitted, text_height, translucent=False):
    """Draw every item onto img. Returns {item_id: (x0, y0, x1, y1)} in pixel
    coordinates, which the editor uses for hit testing and drag handles."""
    draw = ImageDraw.Draw(img)
    width, height = img.size
    short = min(width, height)
    halo = (0, 0, 0) if translucent else None
    boxes = {}

    for item in items:
        if item["type"] == "weather":
            wmap = values.get("weather_map") or {}
            weather = wmap.get(item["id"]) or values.get("weather")
            box = render_weather(draw, item, weather,
                                 width, height, short, fitted, text_height,
                                 translucent=translucent)
            if box:
                boxes[item["id"]] = box
            continue

        if item["type"] == "worldclock":
            wmap = values.get("worldclock_map") or {}
            info = wmap.get(item["id"])
            box = render_worldclock(draw, item, info,
                                    width, height, short, fitted, text_height,
                                    translucent=translucent)
            if box:
                boxes[item["id"]] = box
            continue

        label, value = item_strings(item, values)
        if not value and not label:
            continue

        left = item["x"] * width
        top = item["y"] * height
        span = (item["width"] or (1.0 - item["x"])) * width
        span = max(20.0, span - short * 0.02)

        def put(y, text, px, colour):
            font = fitted(text, span, px)
            box = font.getbbox(text) if hasattr(font, "getbbox") else (0, 0, 0, 0)
            drawn = box[2] - box[0]
            x = left
            if item["align"] == "center":
                x = left + (span - drawn) / 2.0
            elif item["align"] == "right":
                x = left + span - drawn
            if halo:
                for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2)):
                    draw.text((x + dx, y + dy), text, font=font, fill=halo)
            draw.text((x, y), text, font=font, fill=colour)
            return x, y, x + drawn, y + text_height(font, text)

        rects = []
        cursor = top
        if label:
            px = max(6.0, item["size"] * short * 0.55)
            rect = put(cursor, label, px, hex_rgb(item["label_color"],
                                                  (110, 125, 150)))
            rects.append(rect)
            cursor = rect[3] + short * 0.015
        if value:
            px = max(6.0, item["size"] * short)
            rects.append(put(cursor, value, px,
                             hex_rgb(item["color"], (150, 215, 255))))

        if rects:
            boxes[item["id"]] = (min(r[0] for r in rects),
                                 min(r[1] for r in rects),
                                 max(r[2] for r in rects),
                                 max(r[3] for r in rects))
    return boxes


def clamp(item, boxes, canvas, margin=0.004):
    """Keep an item inside the canvas, using the box it actually rendered to
    so a wide readout stops at its own right edge rather than at its anchor.
    Falls back to a plain 0..1 clamp when nothing has been measured yet."""
    x = float(item["x"])
    y = float(item["y"])
    width = height = 0.0
    box = (boxes or {}).get(item["id"])
    cw, ch = canvas or (0, 0)
    if box and cw and ch:
        width = (box[2] - box[0]) / float(cw)
        height = (box[3] - box[1]) / float(ch)
    x = max(margin, min(1.0 - margin - width, x))
    y = max(margin, min(1.0 - margin - height, y))
    # A single item wider than the canvas would invert the bounds above.
    return round(max(0.0, x), 4), round(max(0.0, y), 4)


def hit_test(boxes, px, py, slack=6):
    """Topmost item whose box contains the point. Later items win, matching
    the draw order."""
    for item_id in reversed(list(boxes)):
        x0, y0, x1, y1 = boxes[item_id]
        if x0 - slack <= px <= x1 + slack and y0 - slack <= py <= y1 + slack:
            return item_id
    return None
