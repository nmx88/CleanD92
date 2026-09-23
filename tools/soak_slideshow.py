#!/usr/bin/env python3
"""Offline slideshow soak -- no HID. Measures last_ms and RSS over a run.

Usage:
    python tools/soak_slideshow.py [--minutes 10] [--slide 3]

Creates a few tiny PNGs in media/_soak/ if missing, drives render_loop with a
fake panel that discards JPEGs, and prints a summary. Does not open the real
device (so it can run while CleanD92.exe is using the panel).
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time

# Repo root on sys.path
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from PIL import Image  # noqa: E402

import d92_panel as core  # noqa: E402


class FakePanel:
    def __init__(self):
        self.sent = 0
        self.bytes = 0

    def wake(self, brightness=50):
        pass

    def set_brightness(self, value):
        pass

    def send_jpeg(self, jpeg):
        self.sent += 1
        self.bytes += len(jpeg)
        return max(1, (len(jpeg) + 1023) // 1024)

    def close(self):
        pass


def ensure_soak_media(folder, count=4):
    os.makedirs(folder, exist_ok=True)
    names = []
    for i in range(count):
        name = "soak_%d.png" % (i + 1)
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            colour = ((40 + i * 40) % 256, (80 + i * 30) % 256, 120)
            Image.new("RGB", (1920, 462), colour).save(path)
        names.append(name)
    return names


def rss_mb():
    try:
        import psutil
        return psutil.Process().memory_info().rss / (1024 * 1024.0)
    except Exception:
        return -1.0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--minutes", type=float, default=10.0)
    parser.add_argument("--slide", type=float, default=2.0)
    parser.add_argument("--interval-ms", type=int, default=350)
    args = parser.parse_args()

    soak_dir = os.path.join(core.MEDIA_DIR, "_soak")
    # Point MEDIA_DIR at soak folder for isolation
    core.MEDIA_DIR = soak_dir
    names = ensure_soak_media(soak_dir, 4)
    core.invalidate_media_list()

    panel = FakePanel()
    with core.state_lock:
        core.state["mode"] = "slideshow"
        core.state["slide_seconds"] = args.slide
        core.state["slide_shuffle"] = True
        core.state["interval_ms"] = args.interval_ms
        core.state["hold"] = False
        core.state["media"] = names[0]
        core.runtime["stop"] = False
        core.runtime["reload_media"] = True
        core.runtime["frames"] = 0
        core.runtime["last_ms"] = 0
        core.runtime["message"] = ""
        core.runtime["status"] = "starting"

    worker = threading.Thread(target=core.render_loop, args=(panel,),
                              daemon=True)
    worker.start()

    duration = max(30.0, args.minutes * 60.0)
    started = time.monotonic()
    samples = []
    hold_checked = False
    print("soak start: %.1f min, slide=%.1fs, files=%d" % (
        duration / 60.0, args.slide, len(names)))

    while time.monotonic() - started < duration:
        time.sleep(5.0)
        with core.state_lock:
            frames = core.runtime["frames"]
            last_ms = core.runtime["last_ms"]
            status = core.runtime["status"]
            message = core.runtime["message"]
            # Mid-run: exercise Hold + Apply + mode flip once.
            if (not hold_checked
                    and time.monotonic() - started > min(60.0, duration * 0.2)):
                core.state["hold"] = True
                hold_checked = True
                print("  [hold on]")
            elif hold_checked and core.state["hold"]:
                if time.monotonic() - started > min(90.0, duration * 0.3):
                    core.runtime["apply_once"] = True
                    core.state["mode"] = "clock"
                    core.runtime["reload_media"] = True
                    print("  [apply + mode clock under hold]")
                if time.monotonic() - started > min(120.0, duration * 0.35):
                    core.state["hold"] = False
                    core.state["mode"] = "slideshow"
                    core.runtime["reload_media"] = True
                    print("  [hold off, back to slideshow]")
        mem = rss_mb()
        samples.append((last_ms, mem, frames))
        print("t=%4.0fs  frames=%6d  last_ms=%3d  rss=%.1f MB  %s %s" % (
            time.monotonic() - started, frames, last_ms, mem, status,
            (message or "")[:40]))
        if status == "error":
            break

    with core.state_lock:
        core.runtime["stop"] = True
    worker.join(timeout=5.0)

    last_ms_vals = [s[0] for s in samples if s[0] > 0]
    mem_vals = [s[1] for s in samples if s[1] > 0]
    print("\n--- summary ---")
    print("sent_jpegs=%d  bytes=%.1f MB" % (
        panel.sent, panel.bytes / (1024 * 1024.0)))
    if last_ms_vals:
        print("last_ms: min=%d median=%d max=%d  (>100: %d samples)" % (
            min(last_ms_vals),
            sorted(last_ms_vals)[len(last_ms_vals) // 2],
            max(last_ms_vals),
            sum(1 for v in last_ms_vals if v > 100)))
    if mem_vals:
        print("rss_mb: start=%.1f end=%.1f max=%.1f delta=%.1f" % (
            mem_vals[0], mem_vals[-1], max(mem_vals),
            mem_vals[-1] - mem_vals[0]))
    ok = True
    if last_ms_vals and max(last_ms_vals) > 100:
        # Soft fail: report but still exit 0 if under STALL_WARN (3s)
        if max(last_ms_vals) >= int(core.STALL_WARN * 1000):
            ok = False
            print("FAIL: stall at or above STALL_WARN")
        else:
            print("WARN: some frames over 100 ms (decode spikes OK under 3s)")
    if mem_vals and (mem_vals[-1] - mem_vals[0]) > 80:
        ok = False
        print("FAIL: RSS grew more than 80 MB")
    print("result: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
