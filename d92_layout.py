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
import os
import re
import time

from PIL import ImageDraw

# type -> human label, shown in the editor
ITEM_TYPES = {
    "clock": "Clock",
    "date": "Date",
    "stat": "Readout",
    "text": "Fixed text",
    "filename": "Current file name",
}

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

DEFAULT_PRESETS = {
    "Wide dashboard": [
        new_item("clock", x=0.03, y=0.08, size=0.42, color="#ebf0fa"),
        new_item("date", x=0.03, y=0.62, size=0.14, color="#6e7d96"),
        new_item("stat", source="cpu", x=0.50, y=0.06, size=0.19),
        new_item("stat", source="ram", x=0.50, y=0.40, size=0.19),
        new_item("stat", source="gpu0", x=0.50, y=0.70, size=0.19),
        new_item("stat", source="net_down", x=0.76, y=0.06, size=0.19),
        new_item("stat", source="net_up", x=0.76, y=0.40, size=0.19),
        new_item("stat", source="disk", x=0.76, y=0.70, size=0.19),
    ],
    "Wide clock only": [
        new_item("clock", x=0.05, y=0.12, size=0.62, color="#ebf0fa"),
        new_item("date", x=0.06, y=0.78, size=0.14, color="#6e7d96"),
    ],
    "Media with caption": [
        new_item("clock", x=0.03, y=0.06, size=0.34, color="#ffffff"),
        new_item("filename", x=0.03, y=0.80, size=0.13, color="#ffffff",
                 show_label=False),
        new_item("stat", source="cpu", x=0.70, y=0.08, size=0.16),
        new_item("stat", source="gpu0", x=0.70, y=0.50, size=0.16),
    ],
    "Tall dashboard": [
        new_item("clock", x=0.07, y=0.03, size=0.34, color="#ebf0fa",
                 format="%H:%M"),
        new_item("date", x=0.07, y=0.09, size=0.13, color="#6e7d96",
                 format="%d %b"),
        new_item("stat", source="cpu", x=0.07, y=0.16, size=0.20),
        new_item("stat", source="ram", x=0.07, y=0.26, size=0.20),
        new_item("stat", source="gpu0", x=0.07, y=0.36, size=0.20),
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


def load_preset(base, name):
    """Saved file wins over a built-in of the same name."""
    path = os.path.join(preset_dir(base), "%s.json" % name)
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        items = data.get("items") if isinstance(data, dict) else data
        return normalise_all(items)
    if name in DEFAULT_PRESETS:
        return normalise_all(copy.deepcopy(DEFAULT_PRESETS[name]))
    raise KeyError(name)


def save_preset(base, name, items):
    safe = "".join(c for c in name if c.isalnum() or c in " _-()").strip()
    if not safe:
        raise ValueError("preset name cannot be empty")
    folder = preset_dir(base)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "%s.json" % safe)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"name": safe, "items": normalise_all(items)}, handle,
                  indent=2)
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
    return None, None


def render(img, items, values, fitted, text_height, translucent=False):
    """Draw every item onto img. Returns {item_id: (x0, y0, x1, y1)} in pixel
    coordinates, which the editor uses for hit testing and drag handles."""
    draw = ImageDraw.Draw(img)
    width, height = img.size
    short = min(width, height)
    halo = (0, 0, 0) if translucent else None
    boxes = {}

    for item in items:
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
