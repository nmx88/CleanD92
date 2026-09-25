# AGENTS.md — working brief for CleanD92

Read this before changing anything. It exists because several of the rules
below look like bugs and invite "helpful" fixes that brick the hardware.

Repo: `github.com/nmx88/CleanD92` · licence MIT · owner `nmx88`
Local working copy: `C:\Users\John\mirabox`

---

## 1. What this is

A Windows desktop app that drives the **StreamDock D92**, a 1920×464 USB
monitoring panel (sold as MiraBox D92; firmware reports `HOTSPOTEKUSB HID
DEMO`, USB `5548:1011`). It shows a clock, system readouts, images, GIFs or a
slideshow on the panel.

The vendor utility does this by installing a kernel-mode **virtual display
driver**, creating a virtual monitor and capturing it. On the owner's machine
that driver failed to complete power IRPs and Windows bugchecked with
`0x9F DRIVER_POWER_STATE_FAILURE` — a reboot loop a few seconds after login.

CleanD92 replaces all of it from user mode. The panel is a plain USB HID
device; Windows binds its own `hidusb.sys`, and the app opens that interface
and pushes JPEG frames.

**The point of the project is that it installs nothing.** Any change that
introduces a driver, a service, elevation, or a kernel component defeats it.
LibreHardwareMonitor is the one exception and is optional, external, and the
user's own choice.

---

## 2. Hard invariants

Breaking any of these leaves the panel black until it is physically
unplugged. They are properties of the firmware, discovered the hard way.

### 2.1 Never reopen the HID handle within one physical connection

Opened once in `start_backend()`, held until exit. After a failed write
`D92._send` sets `self._dead` and refuses everything afterwards. There is
deliberately **no reopen path**. Do not add one, do not add reconnect logic,
do not "recover" from a write error. The correct behaviour on failure is to
stop and tell the user to replug.

### 2.2 Never let the OUT endpoint go idle

A frame goes out every `interval_ms` whether or not anything changed. Any
code path that can block the render thread is a bug, however short. This is
why media loading and sensor polling live on their own threads. If you add
anything that touches disk, network or a subprocess, it does **not** go in
`render_loop`.

### 2.3 The frame shape is fixed

The `DRA` header carries no dimensions, so the device infers geometry from
the JPEG and accepts only its own portrait shape (462×1920). A wrong-shaped
frame makes it tile the image and stop accepting writes.
`d92_panel.check_geometry()` computes the post-rotation size and refuses to
send if it does not match. Do not relax that check.

### 2.4 350 ms is the safe cadence

Lower works and looks better for GIFs, but raises the random-dropout rate.
The default stays 350; the user can lower it.

### 2.5 hidapi's Windows write has no timeout

`hid_write` blocks in `GetOverlappedResult` with no timeout. If the device
stalls, the render thread hangs with it and nothing can cancel it. This is
why 2.3 is enforced by refusal rather than by recovery.

---

## 3. Architecture

```
d92.py          transport. DEVICE profile (all model-specific literals),
                envelope building, chunking, wake sequence, D92 class.
                Also a small CLI: `probe`, `bars`, `dashboard`.
d92_layout.py   layout model. Items as dicts with fractional coordinates,
                presets, rendering, hit testing, clamping. Pure: no USB, no
                sensors, no UI. Takes font helpers and a values dict.
d92_panel.py    the engine. Shared state, render loop, media cache, async
                loader, sensor polling, geometry, and the browser UI.
d92_app.py      CleanD92, the tkinter window and layout editor.
```

### Threads

| Thread | Owns | Never does |
|---|---|---|
| render loop (`render_loop`) | **every device write** | disk, network, subprocess |
| media loader (`request_load`) | decoding, scaling, JPEG encoding | device writes |
| LHM poller (`lhm_poller`) | HTTP to LibreHardwareMonitor | device writes |
| tkinter main | UI, state edits | device writes |
| HTTP server (`--web`) | state edits | device writes |

Both front ends mutate shared state through **`apply_patch()`** and nothing
else. That is the single validation point; do not let a UI write `state`
directly.

### State

`d92_panel.state` — a plain dict under `state_lock`. Keys: `mode`, `media`,
`brightness`, `interval_ms`, `layout`, `flip`, `canvas_w/h`, `fit`,
`quality`, `slide_seconds`, `slide_shuffle`, `show_gpu`, `lhm_url`,
`weather_place` / `weather_country` / `weather_units` / `weather_lang`,
`color_bg`, `items`, `preset`, `hold`.

User presets (`presets/*.json`) store `items` plus a `scene` object with every
tunable above except `hold` and `canvas_w/h`. Built-ins are items-only.
`settings.json` persists the four weather keys across restarts (also snapshotted
into a preset when the user saves one).

`d92_panel.runtime` — status, message, frames, last_ms, plus one-shot flags
the render loop consumes: `wake`, `apply_once`, `reload_media`,
`apply_brightness`, `stop`. Also `boxes` and `box_canvas`, the pixel boxes of
the last render, which the editor uses for hit testing and clamping.

### Layout items

```json
{"id": "stat-3", "type": "stat", "source": "cpu_temp",
 "x": 0.50, "y": 0.06, "size": 0.19, "width": 0.0,
 "color": "#96d7ff", "label_color": "#6e7d96",
 "show_label": true, "align": "left", "text": "", "format": ""}
```

Coordinates and sizes are **fractions of the canvas**, never pixels — that is
what lets one preset work in both orientations and on a future panel with
different dimensions. `size` is a fraction of the canvas short edge. Types:
`clock`, `date`, `stat`, `text`, `filename`. Sources are dynamic (one per
fixed drive, one per GPU LHM reports); `available_sources()` is the list, and
`normalise()` validates the shape rather than membership.

Item ids must be unique. They were once generated from a millisecond
timestamp, which silently collapsed eight items into three in the render box
map and broke selection and dragging. `new_item` now uses a counter and
`normalise_all` repairs duplicates.

### Geometry

Content is authored on a landscape 1920×462 canvas and rotated 270° before
encoding, or authored on a portrait 462×1920 canvas and sent unrotated. Either
way the wire frame is 462×1920. `author_size()`, `wire_rotation()`,
`wire_size()` and `check_geometry()` in `d92_panel.py` are the whole story.

### Media

`load_media` returns `[(jpeg_bytes, duration_seconds), ...]` — frames are
cached **JPEG-compressed**, not as bitmaps. A 1920×462 frame is 2.66 MB
decoded and about 60 KB encoded; the raw version exhausted memory on long
GIFs. Animations over `MAX_FRAMES` (150) are subsampled with the skipped
durations folded into the kept frames, so timing stays honest.

---

## 4. Protocol reference

All literals live in `DEVICE` at the top of `d92.py`.

```
output report  1025 bytes = report id 0x00 + 1024 byte envelope
frame header   32 bytes: "CRT\0\0" + "DRA", 24-bit BE total length at [9..11],
               0xB1 at [12]; then the JPEG, chunked to 1024
control        "CRT\0\0" + 3-letter verb + big-endian arg at a verb offset
               DIS  wake, no arg
               LIG  brightness 0..100, 1 byte at offset 10
               SET  rotation: accepted, no observed effect. Rotate host-side.
connect        DIS -> ~450 ms -> LIG, then stream. Also revives a black panel.
```

Use `WriteFile` (what hidapi does), **not** `HidD_SetOutputReport` — the
latter fails with error 87 on this device.

Credit: the wire protocol was reverse-engineered by
[chainsaid/U-Sidecar](https://github.com/chainsaid/U-Sidecar). Keep that
attribution in the README.

---

## 5. Build and release

```
pip install -r requirements.txt          run from source
python d92_app.py

pip install -r requirements-build.txt    build the exe
pyinstaller --noconfirm --clean CleanD92.spec
```

`dist\CleanD92.exe`, one file, no console, 25–45 MB. Tagging `v*` triggers
`.github/workflows/release.yml`, which stamps `APP_VERSION` /
`version_info.txt` from the tag, builds on `windows-latest`, and attaches
the exe to a GitHub release. Bump `APP_VERSION` in `d92_app.py` in the same
commit when you cut a release so source runs match the tag before CI runs.

When frozen, `d92_panel.HERE` resolves against `sys.executable`, not
`__file__` — under one-file mode `__file__` points inside a temp directory
that is wiped on exit, so `media/` and `presets/` would vanish every run.

---

## 6. Known state and open items

- `v1.0.0` is released. The first CI run failed because the workflow still
  referenced the pre-rename `CleaneD92.spec`; that is fixed, the tag was
  moved, and the re-run succeeded with the exe attached to the release.
- The built exe is **in daily use** by the owner, running from its own folder
  and driving the panel. Treat the current build as working: a change that
  breaks it is a regression, not a discovery.
- GPU, CPU and disk temperatures need LibreHardwareMonitor running as
  administrator with Options → Remote Web Server → Run. On the owner's
  machine it was not running (WinError 10061), so the LHM parser has been
  written against the documented JSON shape but **never exercised against a
  live tree**. Expect the sensor-name matching in `fetch_lhm` to need
  adjusting once real data is seen; the app's "Test GPU sensors" button dumps
  what it found and every hardware node LHM reports.
- MP4 was considered and rejected: the panel only ever receives JPEG, so it
  would mean bundling ffmpeg and roughly doubling the download.
- The panel has **random USB dropouts**, anywhere from seconds to minutes,
  not root-caused by anyone. Recovery is a physical replug. Report it, do not
  paper over it.
- No plugin interface for other devices in the family, on purpose: an
  abstraction designed with one device on the bench puts its seams in the
  wrong places. See "Adding another device" in the README.

---

## 7. Conventions

- Comments explain **why**, especially where the code looks wrong. Every
  invariant above should be defended by a comment at the place it is enforced.
- No new runtime dependencies without a reason that survives "this has to be
  one portable exe". Current set: `hidapi`, `pillow`, `psutil`, `pystray`,
  `windnd`, `tzdata`, plus stdlib tkinter.
- UI colours are explicit. The window uses classic `tk.Radiobutton` and
  `tk.Checkbutton` rather than `ttk` for anything with an indicator, because
  the clam theme paints its own hover background and produced white-on-white.
- British spelling in user-facing text, plain wording, no exclamation marks.
- The README's tone about the vendor is deliberately factual, not accusatory.

---

## 8. Suggested next phases

Each phase stands alone. **Stop at the end of each and report before
continuing.**

The app is already released and in daily use, so every change from here is
maintenance on working software. Prefer small, reversible edits; run
`python d92_app.py` against the real panel after each one.

### Phase 1 — Make the sensors real
1. Run LibreHardwareMonitor elevated with the web server on.
2. Press "Test GPU sensors", capture the full dump.
3. Adjust the keyword matching in `fetch_lhm` against that real tree, for
   CPU package temperature, per-GPU load and temperature, and per-drive
   temperature.
**Done (this machine):** live tree against i7-12700K + RX 7700 XT + UHD 770
+ four drives. `cpu_temp` ← CPU Package; `gpu0` ← GPU Core load/temp (hot
spot fallback); Intel iGPU has load only (no temp leaves in LHM); disks use
Composite/Temperature with distinct model labels. Matching comments in
`fetch_lhm` name the live sensor strings.
**Done when:** every readout the layout offers shows a real number.
**Stop and report.**

### Phase 2 — First-run experience
Nobody who downloads this knows what to do first. Consider: a default preset
chosen by orientation, a short "no panel found" screen that explains the
likely causes, and a one-line status when the media folder is empty.
**Done:** tip to the live Info screen, Vertical swaps in Tall clock only,
Help/README cover weather and world clocks, empty-media coaching stays in the
render status line.
**Done when:** a new user with an empty folder gets something sensible on the
panel within a minute. **Stop and report.**

### Phase 3 — Robustness pass
Long-run soak with the slideshow, watch `last_ms` and memory. Confirm the
dropout path reports cleanly rather than hanging. Confirm Hold plus Apply
behaves across mode changes.
Harness: `python tools/soak_slideshow.py --minutes 60 --slide 3` (fake
panel, no HID).
**Done:** 60 min PASS — 10279 frames, last_ms min/med/max 10/23/55 (0
samples >100), RSS 35.7→37.1 MB (delta +1.4, max 46.2). Earlier 20 min
partial was also PASS; Hold/Apply/mode flip exercised mid-run.
**Done when:** an hour of slideshow shows flat memory and no stalls over
100 ms. **Stop and report.**

### Phase 4 — Only if a second device appears
Do not build the device abstraction speculatively. When someone turns up with
another panel in the `5548` family and a packet capture, restructure around
both at once.
