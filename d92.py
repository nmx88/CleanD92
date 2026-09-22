#!/usr/bin/env python3
"""
d92.py -- MiraBox StreamDock D92 (VID 0x5548 / PID 0x1011)

Python port of the StreamDock D92 HID wire protocol. Needs no kernel driver
and no virtual display driver: frames are drawn straight into a Pillow image
and pushed over the device's vendor-defined HID interface.

Requires:  pip install hidapi pillow psutil

Wire protocol
  - Output report = 1025 bytes: 1 byte ReportID (0x00, a Windows HID API
    convention that never reaches the wire) + a 1024-byte envelope.
  - Image frames: 32-byte "CRT\\0\\0" + "DRA" header on the FIRST chunk only,
    then the JPEG bytes, everything chunked to 1024 and zero-padded at the end.
  - Control commands share the same envelope: "CRT\\0\\0" + 3-letter verb +
    big-endian parameter at a verb-specific offset.
      LIG -> brightness, 1 byte at offset 10
      DIS -> wake / display on, no payload
      SET (rotation) is NOT implemented: it sends correct-looking bytes but
      has no observed effect. Always rotate host-side before encoding.

Two operating rules -- breaking either one reliably blacks out the panel
until a PHYSICAL unplug/replug:

  1. Open the handle once and hold it. Never reopen it after a write failure
     within the same physical connection. This module enforces that: after a
     failed write the session is marked dead and refuses further writes.
  2. Never let the OUT endpoint go idle. Push a frame every interval whether
     or not the content changed. 350 ms is the verified-safe cadence.

On every successful open, replay the official app's connect sequence before
streaming: DIS -> ~450 ms -> LIG. That same sequence also revives a panel
that has already gone black.

Close the official MiraBox software before running this -- it opens the
device and this will not find it.
"""

import sys
import time

import hid
from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# Everything specific to this one panel model lives here.
#
# The code below this block is model-agnostic: it chunks an envelope, builds a
# header, pushes frames and holds the rules about never reopening the handle
# and never going idle. Those rules come from the firmware family, not from
# the D92, so they should hold for its siblings too.
#
# What is NOT shared is every literal in this dict. Devices on the same USB
# vendor id (0x5548 covers several StreamDock and Ajazz products) appear to
# share the vendor-defined HID usage page 0xFFA0 and the same general shape,
# but the verbs, the header layout and the geometry are per model and have to
# be captured from the real device. See "Adding another device" in the README.
#
# Do not aim these bytes at a different product id on a guess. A wrong-shaped
# frame already makes this panel tile the image and stop accepting writes, and
# firmware in this family carries update verbs as well -- a stray vendor
# report can leave a device in a state a replug does not fix.
# ---------------------------------------------------------------------------

DEVICE = {
    "name": "StreamDock D92",

    # -- identity
    "vid": 0x5548,
    "pid": 0x1011,
    "usage_page": 0xFFA0,           # vendor defined; informational
    "usage": 0x0001,

    # -- transport
    "report_id": 0x00,              # Windows HID convention, never on the wire
    "chunk": 1024,                  # output report payload, from the descriptor
    "in_report_len": 513,

    # -- frame envelope: magic + verb, then a 24-bit big-endian total length
    "magic": b"CRT\x00\x00",
    "frame_verb": b"DRA",
    "header_len": 32,
    "length_at": 9,                 # bytes [9..11] = header_len + len(jpeg)
    "tag_at": 12,
    "tag_value": 0xB1,

    # -- control verbs: name -> (verb, value offset or None, value length)
    "verbs": {
        "wake": ("DIS", None, 0),
        "brightness": ("LIG", 10, 1),
        # "SET" (rotation) exists and is accepted, but has no observed effect.
        # Rotate host-side instead.
    },
    "wake_delay": 0.450,            # between DIS and LIG, from the vendor app
    "default_brightness": 50,

    # -- geometry: author landscape, rotate, send portrait
    "canvas": (1920, 462),
    "rotate": 270,                  # 90 if the panel reads upside down

    # -- timing
    "frame_interval": 0.350,        # verified safe; lower raises dropouts
    "jpeg_quality": 85,
}

VID = DEVICE["vid"]
PID = DEVICE["pid"]

CHUNK = DEVICE["chunk"]
HDR_LEN = DEVICE["header_len"]
MAGIC = DEVICE["magic"]

CANVAS_W, CANVAS_H = DEVICE["canvas"]
ROTATE = DEVICE["rotate"]

FRAME_INTERVAL = DEVICE["frame_interval"]
JPEG_QUALITY = DEVICE["jpeg_quality"]


class D92Error(Exception):
    pass


# ---------------------------------------------------------------- protocol

def build_dra_header(jpeg_len, device=DEVICE):
    """Frame header. The length field covers header + JPEG, 24-bit big
    endian. Every literal comes from the device profile."""
    total = device["header_len"] + jpeg_len
    hdr = bytearray(device["header_len"])
    magic = device["magic"]
    verb = device["frame_verb"]
    hdr[0:len(magic)] = magic
    hdr[len(magic):len(magic) + len(verb)] = verb
    at = device["length_at"]
    hdr[at] = (total >> 16) & 0xFF
    hdr[at + 1] = (total >> 8) & 0xFF
    hdr[at + 2] = total & 0xFF
    hdr[device["tag_at"]] = device["tag_value"]
    return bytes(hdr)


def build_ctrl(verb, value_offset=None, value=0, value_len=1, device=DEVICE):
    """Control command envelope: magic + 3-letter verb + optional big-endian
    value at a verb-specific offset."""
    if len(verb) != 3:
        raise ValueError("verb must be exactly 3 characters")
    magic = device["magic"]
    base = len(magic) + 3
    size = base if value_offset is None else max(base,
                                                 value_offset + value_len)
    buf = bytearray(size)
    buf[0:len(magic)] = magic
    buf[len(magic):base] = verb.encode("ascii")
    if value_offset is not None:
        for i in range(value_len):
            buf[value_offset + value_len - 1 - i] = (value >> (8 * i)) & 0xFF
    return bytes(buf)


def control(name, value=0, device=DEVICE):
    """Build a control envelope by profile name ("wake", "brightness")."""
    verb, offset, length = device["verbs"][name]
    return build_ctrl(verb, value_offset=offset, value=value,
                      value_len=length or 1, device=device)


# ------------------------------------------------------------------ device

class D92:
    def __init__(self):
        self._dev = None
        self._dead = False

    def open(self):
        if self._dev is not None:
            raise D92Error("already open -- never reopen within one session")
        dev = hid.device()
        dev.open(VID, PID)          # raises if absent or held by another app
        self._dev = dev
        return self

    def close(self):
        if self._dev is not None:
            self._dev.close()
            self._dev = None

    @property
    def alive(self):
        return self._dev is not None and not self._dead

    def _send(self, wire, retries=3, retry_delay=0.05):
        """Write one <=1024-byte envelope. In-place retries only, no reopen."""
        if self._dev is None:
            raise D92Error("device not open")
        if self._dead:
            raise D92Error(
                "session is dead -- physically replug the panel, then restart"
            )
        if len(wire) > CHUNK:
            raise ValueError("envelope must be <= %d bytes" % CHUNK)

        packet = (bytes([DEVICE["report_id"]]) + wire
                  + b"\x00" * (CHUNK - len(wire)))
        last = None
        for attempt in range(retries):
            try:
                written = self._dev.write(packet)
                if written < 0:
                    raise D92Error("write returned %d" % written)
                return
            except Exception as exc:
                last = exc
                if attempt < retries - 1:
                    time.sleep(retry_delay)

        self._dead = True
        raise D92Error("write failed after %d retries: %s" % (retries, last))

    # -- control ----------------------------------------------------------

    def screen_on(self):
        """Wake / display on."""
        self._send(control("wake"))

    def set_brightness(self, value):
        """Brightness, 0..100."""
        value = max(0, min(100, int(value)))
        self._send(control("brightness", value))

    def wake(self, brightness=DEVICE["default_brightness"],
             delay=DEVICE["wake_delay"]):
        """The official connect sequence. Call right after open(), then start
        pushing frames immediately. Also revives an already-black panel."""
        self.screen_on()
        time.sleep(delay)
        self.set_brightness(brightness)

    # -- frames -----------------------------------------------------------

    def send_jpeg(self, jpeg):
        """Push one JPEG frame. Returns the number of chunks written."""
        payload = build_dra_header(len(jpeg)) + jpeg
        chunks = 0
        for off in range(0, len(payload), CHUNK):
            self._send(payload[off:off + CHUNK])
            chunks += 1
        return chunks

    def send_image(self, img, quality=JPEG_QUALITY):
        """Letterbox `img` into the authoring canvas, rotate, encode, push."""
        import io
        canvas = letterbox(img, CANVAS_W, CANVAS_H)
        rotated = canvas.rotate(ROTATE, expand=True)
        buf = io.BytesIO()
        rotated.convert("RGB").save(buf, format="JPEG", quality=quality)
        return self.send_jpeg(buf.getvalue())


def letterbox(img, width, height, background=(0, 0, 0)):
    """Fit img inside width x height, preserving aspect, black bars."""
    out = Image.new("RGB", (width, height), background)
    src = img.convert("RGB")
    scale = min(width / src.width, height / src.height)
    size = (max(1, int(src.width * scale)), max(1, int(src.height * scale)))
    resized = src.resize(size, Image.LANCZOS)
    out.paste(resized, ((width - size[0]) // 2, (height - size[1]) // 2))
    return out


# ----------------------------------------------------------------- drawing

def load_font(size):
    for name in ("consola.ttf", "segoeui.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def draw_dashboard():
    """A plain dashboard on the 1920x462 canvas. Replace with your own."""
    img = Image.new("RGB", (CANVAS_W, CANVAS_H), (8, 10, 14))
    d = ImageDraw.Draw(img)

    big = load_font(200)
    small = load_font(64)

    d.text((60, 90), time.strftime("%H:%M:%S"), font=big, fill=(235, 240, 250))

    lines = []
    try:
        import psutil
        lines.append("CPU  %5.1f %%" % psutil.cpu_percent(interval=None))
        lines.append("RAM  %5.1f %%" % psutil.virtual_memory().percent)
    except ImportError:
        lines.append("pip install psutil")

    y = 110
    for line in lines:
        d.text((900, y), line, font=small, fill=(120, 200, 255))
        y += 90

    d.text((60, 360), time.strftime("%a %d %b %Y"), font=small,
           fill=(110, 120, 140))
    return img


# --------------------------------------------------------------------- cli

def cmd_probe():
    """Open, read identity, close. No writes -- safe."""
    dev = hid.device()
    dev.open(VID, PID)
    print("Manufacturer:", dev.get_manufacturer_string())
    print("Product     :", dev.get_product_string())
    print("Serial      :", dev.get_serial_number_string())
    dev.close()
    print("OK -- device reachable")


def cmd_stream(draw_fn):
    panel = D92().open()
    print("Opened. Sending wake sequence (DIS -> 450ms -> LIG)...")
    panel.wake()
    print("Streaming every %.0f ms. Ctrl+C to stop." % (FRAME_INTERVAL * 1000))

    frames = 0
    try:
        while True:
            started = time.monotonic()
            panel.send_image(draw_fn())
            frames += 1
            if frames % 20 == 0:
                print("\r%d frames" % frames, end="", flush=True)
            slack = FRAME_INTERVAL - (time.monotonic() - started)
            if slack > 0:
                time.sleep(slack)
    except KeyboardInterrupt:
        print("\nStopped by user after %d frames." % frames)
    except D92Error as exc:
        print("\nSession ended: %s" % exc)
        print("The panel needs a physical unplug/replug before restarting.")
    finally:
        panel.close()


def cmd_bars():
    """Static colour bars -- for checking orientation and geometry."""
    img = Image.new("RGB", (CANVAS_W, CANVAS_H), (0, 0, 0))
    d = ImageDraw.Draw(img)
    colours = [(255, 0, 0), (0, 255, 0), (0, 0, 255),
               (255, 255, 0), (255, 255, 255)]
    step = CANVAS_W // len(colours)
    for i, colour in enumerate(colours):
        d.rectangle([i * step, 0, (i + 1) * step, CANVAS_H], fill=colour)
    font = load_font(120)
    d.text((40, 40), "TOP LEFT", font=font, fill=(0, 0, 0))
    return lambda: img


USAGE = """usage: d92.py {probe | bars | dashboard}

  probe      open the device and print its identity, write nothing
  bars       stream static colour bars (check rotation and geometry)
  dashboard  stream a clock + CPU/RAM dashboard
"""

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "probe":
        cmd_probe()
    elif cmd == "bars":
        cmd_stream(cmd_bars())
    elif cmd == "dashboard":
        cmd_stream(draw_dashboard)
    else:
        print(USAGE)
        sys.exit(2)
