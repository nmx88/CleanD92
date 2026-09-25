#!/usr/bin/env python3
"""
d92_app.py -- CleanD92, a native Windows front end for the StreamDock D92.

Double-click and it opens a window: live preview on the left, every control
on the right. No browser, no vendor software, no kernel driver.

    python d92_app.py              native window (default)
    python d92_app.py --web        also serve the browser UI on port 8092 (LAN)
    python d92_app.py --web-only   headless, browser UI only

Needs d92.py and d92_panel.py beside it, plus:
    pip install hidapi pillow psutil

Deliberately built on tkinter, which ships with Python, rather than on an
embedded browser: the point of this app is to be a single portable exe that
works on a machine where the user has installed nothing, and a WebView2
runtime dependency is exactly the kind of failure they cannot diagnose.
The browser UI in d92_panel.py still works and drives the same shared state,
so --web gives you remote control from a phone on the same Wi-Fi.

All device writes still happen on the one render thread in d92_panel. This
window only edits state, exactly like the HTTP handler does.
"""

import io
import os
import sys
import threading
import webbrowser
import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, ttk

from PIL import Image, ImageTk

import d92_layout as layout
import d92_panel as core
from d92 import D92

APP_NAME = "CleanD92"
# Bump with the release tag (CI also stamps version_info.txt from the tag so
# Windows file Properties cannot drift the way they did at 1.0.0 forever).
APP_VERSION = "1.5.0"
CREATOR = "nmx88"
REPO_URL = "https://github.com/nmx88/CleanD92"
REFRESH_MS = 700          # preview and status refresh
PREVIEW_MAX_HEIGHT = 320  # tallest the on-screen preview may get


def app_title():
    return "%s  %s" % (APP_NAME, APP_VERSION)


def open_repo(_event=None):
    """Open the GitHub repo. Never raise into the UI."""
    try:
        webbrowser.open(REPO_URL)
    except Exception:
        pass


def creator_link(parent, bg="#14161b", dim="#98a3b6", accent="#7eb8ff"):
    """'Creator  nmx88' with the name as a clickable repo link.

    Classic tk.Label, not ttk: need an explicit underline and hand cursor,
    and a disabled Text widget would swallow the click."""
    row = tk.Frame(parent, bg=bg)
    tk.Label(row, text="Creator  ", bg=bg, fg=dim,
             font=("Segoe UI", 9)).pack(side="left")
    link = tk.Label(row, text=CREATOR, bg=bg, fg=accent,
                    font=("Segoe UI", 9, "underline"), cursor="hand2")
    link.pack(side="left")
    link.bind("<Button-1>", open_repo)
    return row


# --------------------------------------------------------------------- app

class PanelApp:
    def __init__(self, root, panel, phone_url=None):
        self.root = root
        self.panel = panel
        self.phone_url = phone_url or ""
        self._photo = None
        self.source_labels = {}
        self._after = None
        self._suspend = False     # True while widgets are being repopulated
        self.selected = None      # id of the item being edited
        self._grab = None         # (dx, dy) offset while dragging
        self._nudge_job = None
        self._nudge_step = 0.005
        self._pending = None
        self._shot_box = None     # (ox, oy, w, h) of the image on the canvas
        self._tray = None         # pystray.Icon while withdrawn to the tray
        self._quitting = False

        root.title(app_title())
        root.minsize(980, 620)
        root.configure(bg="#14161b")
        root.protocol("WM_DELETE_WINDOW", self.quit)
        # Minimize (taskbar button) goes to the notification area; the X still
        # quits. The render thread keeps pushing frames while we are hidden.
        root.bind("<Unmap>", self._on_unmap)

        self._style()
        self._build()
        self.pull()
        self.tick()

    # -- chrome -----------------------------------------------------------

    def _style(self):
        root = self.root
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        bg, panel_bg, fg, dim = "#14161b", "#1b1f27", "#e6e9ef", "#98a3b6"
        style.configure(".", background=bg, foreground=fg,
                        fieldbackground=panel_bg, bordercolor="#2a3040")
        style.configure("TFrame", background=bg)
        style.configure("TLabelframe", background=bg, bordercolor="#2a3040")
        style.configure("TLabelframe.Label", background=bg, foreground="#8fa3c0")
        style.configure("TLabel", background=bg, foreground=fg)
        style.configure("Dim.TLabel", background=bg, foreground=dim)
        # Hover and indicator states. The clam theme paints a white box
        # behind check and radio indicators and turns the whole row white on
        # hover, which looked broken on a dark layout. Option names differ
        # between themes, so each one is applied defensively.
        for name in ("TCheckbutton", "TRadiobutton"):
            style.configure(name, background=bg, foreground=fg,
                            focuscolor=bg)
            style.map(name,
                      background=[("active", "#20252f"),
                                  ("pressed", "#20252f")],
                      foreground=[("active", "#ffffff"),
                                  ("disabled", "#5b6478")])
            for options in (
                    {"indicatorbackground": panel_bg,
                     "indicatorforeground": "#4da3ff"},
                    {"indicatorcolor": panel_bg},
            ):
                try:
                    style.configure(name, **options)
                except tk.TclError:
                    pass
            for spec in (
                    {"indicatorbackground": [("selected", "#2f5d94"),
                                             ("active", "#26303f"),
                                             ("!selected", panel_bg)]},
                    {"indicatorcolor": [("selected", "#4da3ff"),
                                        ("active", "#26303f"),
                                        ("!selected", panel_bg)]},
            ):
                try:
                    style.map(name, **spec)
                except tk.TclError:
                    pass
        style.configure("TButton", background=panel_bg, foreground=fg,
                        bordercolor="#2a3040", focuscolor=bg)
        style.map("TButton",
                  background=[("active", "#26303f"), ("pressed", "#1a212b")],
                  foreground=[("active", "#ffffff"),
                              ("disabled", "#5b6478")])
        style.configure("Danger.TButton", foreground="#ffb3bd")
        style.configure("TScale", background=bg)
        # Readonly comboboxes ignore fieldbackground on Windows and render
        # dark text on a light selection, which made the file name unreadable.
        style.configure("TCombobox", fieldbackground=panel_bg,
                        background=panel_bg, foreground=fg,
                        arrowcolor=fg, selectbackground=panel_bg,
                        selectforeground=fg)
        style.map("TCombobox",
                  fieldbackground=[("readonly", panel_bg),
                                   ("disabled", panel_bg)],
                  foreground=[("readonly", fg), ("disabled", dim)],
                  selectbackground=[("readonly", panel_bg)],
                  selectforeground=[("readonly", fg)],
                  background=[("active", "#26303f")],
                  arrowcolor=[("active", "#ffffff")])
        style.map("TScale", background=[("active", bg)])
        style.map("Danger.TButton",
                  foreground=[("active", "#ffd7dd")],
                  background=[("active", "#3a1b20")])
        style.configure("TEntry", fieldbackground=panel_bg, foreground=fg,
                        insertcolor=fg)
        style.configure("TSpinbox", fieldbackground=panel_bg, foreground=fg,
                        arrowcolor=fg)
        root.option_add("*TCombobox*Listbox.background", panel_bg)
        root.option_add("*TCombobox*Listbox.foreground", fg)
        root.option_add("*TCombobox*Listbox.selectBackground", "#2f5d94")
        root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")

    def _build(self):
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        # ---- left column: preview + status
        left = ttk.Frame(outer)
        left.pack(side="left", fill="both", expand=True, padx=(0, 12))

        box = ttk.Labelframe(left, text="Preview \u2014 drag items to move "
                                       "them, or drop a file here", padding=8)
        box.pack(fill="x")
        self.preview = tk.Canvas(box, bg="#000000", bd=0,
                                 highlightthickness=0, height=200)
        self.preview.pack(fill="both", expand=True)
        self.preview.bind("<Button-1>", self.on_press)
        self.preview.bind("<B1-Motion>", self.on_drag)
        self.preview.bind("<ButtonRelease-1>", self.on_release)
        self._hook_file_drop(self.preview)
        ttk.Label(left, text="The authored canvas \u2014 what lands on the "
                             "panel. Click an item to select it, drag to "
                             "reposition. Drop a .png .jpg .gif onto the "
                             "preview to copy it into media/ and show it.",
                  style="Dim.TLabel", wraplength=740).pack(anchor="w",
                                                           pady=(6, 0))

        self.status = tk.Label(left, bg="#12261a", fg="#9fe3b6", anchor="w",
                               justify="left", padx=10, pady=8,
                               text="starting\u2026")
        self.status.pack(fill="x", pady=(12, 0))

        help_row = ttk.Frame(left)
        help_row.pack(fill="x", pady=(8, 0))
        ttk.Button(help_row, text="Help",
                   command=self.show_help).pack(side="left")
        ttk.Label(help_row, text=app_title(),
                  style="Dim.TLabel").pack(side="left", padx=(12, 0))
        creator_link(help_row).pack(side="right")

        self.sensors = tk.Text(left, height=11, bg="#12151c", fg="#9aa5b8",
                               bd=0, padx=10, pady=8, wrap="word",
                               font=("Consolas", 9))
        self.sensors.pack(fill="both", expand=True, pady=(12, 0))
        self.sensors.insert("1.0", "GPU sensors: press \u201cTest GPU "
                                   "sensors\u201d.\n")
        self.sensors.configure(state="disabled")

        # ---- right column: scrollable controls
        right = ttk.Frame(outer)
        right.pack(side="right", fill="y")
        canvas = tk.Canvas(right, width=430, bg="#14161b", bd=0,
                           highlightthickness=0)
        bar = ttk.Scrollbar(right, orient="vertical", command=canvas.yview)
        holder = ttk.Frame(canvas, padding=(0, 0, 10, 0))
        canvas.create_window((0, 0), window=holder, anchor="nw")
        canvas.configure(yscrollcommand=bar.set)
        canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        holder.bind("__nothing__", lambda e: None)
        holder.bind("<Configure>",
                    lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(
            -1 * (e.delta // 120), "units"))

        self._source(holder)
        self._layout_group(holder)
        self._items_group(holder)
        self._weather_group(holder)
        self._gpu_group(holder)
        self._panel(holder)

    # -- control groups ---------------------------------------------------

    def _source(self, parent):
        group = ttk.Labelframe(parent, text="Source", padding=10)
        group.pack(fill="x", pady=(0, 10))

        self.mode = tk.StringVar()
        row = ttk.Frame(group)
        row.pack(fill="x")
        for label, value in (("Info screen", "clock"),
                             ("Image / GIF", "media"),
                             ("Slideshow", "slideshow")):
            self._radio(row, label, value, self.mode,
                        lambda: self._set_mode(self.mode.get()))

        ttk.Label(group, text="File", style="Dim.TLabel").pack(anchor="w",
                                                               pady=(8, 2))
        self.media = tk.StringVar()
        self.media_box = ttk.Combobox(group, textvariable=self.media,
                                      state="readonly")
        self.media_box.pack(fill="x")
        self.media_box.bind("<<ComboboxSelected>>", lambda e: self.push(
            media=self.media.get(), mode="media"))
        ttk.Button(group, text="Rescan folder",
                   command=self.rescan).pack(fill="x", pady=(6, 0))
        ttk.Button(group, text="Open media folder",
                   command=self.open_folder).pack(fill="x", pady=(6, 0))
        ttk.Button(group, text="Convert short clip\u2026",
                   command=self.convert_clip).pack(fill="x", pady=(6, 0))
        ttk.Label(group, style="Dim.TLabel", wraplength=380,
                  text="Needs ffmpeg on PATH. Takes the first 12 seconds, "
                       "crops to 1920\u00d7462, writes a GIF into media/."
                  ).pack(anchor="w", pady=(4, 0))

        # Fit / slideshow controls are packed only when the mode needs them,
        # so first-run Info screen is not buried under GIF knobs.
        self.fit_frame = ttk.Frame(group)
        self.fit = tk.StringVar()
        ttk.Label(self.fit_frame, text="Fit", style="Dim.TLabel").pack(
            anchor="w", pady=(8, 2))
        fitrow = ttk.Frame(self.fit_frame)
        fitrow.pack(fill="x")
        for label, value in (("Cover (crop)", "cover"),
                             ("Letterbox", "letterbox")):
            self._radio(fitrow, label, value, self.fit,
                        lambda: self.push(fit=self.fit.get()), side="left")

        self.slide_frame = ttk.Frame(group)
        self.slide_seconds = self._spin(self.slide_frame, "Seconds per file",
                                        2, 600, "slide_seconds")
        self.slide_shuffle = self._check(self.slide_frame,
                                         "Shuffle slideshow order",
                                         "slide_shuffle")

    def _layout_group(self, parent):
        group = ttk.Labelframe(parent, text="Preset", padding=10)
        group.pack(fill="x", pady=(0, 10))
        self.hold = self._check(group, "Hold panel output while I edit",
                                "hold")
        ttk.Button(group, text="Apply now",
                   command=self.apply_now).pack(fill="x", pady=(2, 0))
        ttk.Label(group, style="Dim.TLabel", wraplength=380,
                  text="With Hold on, the preview keeps updating while the "
                       "panel goes on showing the frame it already has: pick "
                       "a file, drag things about, then press Apply now to "
                       "push one frame. The stream never stops either way."
                  ).pack(anchor="w", pady=(4, 8))
        self.preset = tk.StringVar()
        self.preset_box = ttk.Combobox(group, textvariable=self.preset,
                                       state="readonly")
        self.preset_box.pack(fill="x")
        self.preset_box.bind("<<ComboboxSelected>>",
                             lambda e: self.load_preset())
        row = ttk.Frame(group)
        row.pack(fill="x", pady=(6, 0))
        ttk.Button(row, text="Save as\u2026",
                   command=self.save_preset).pack(side="left",
                                                  expand=True, fill="x")
        ttk.Button(row, text="Delete",
                   command=self.delete_preset).pack(side="left",
                                                    expand=True, fill="x",
                                                    padx=(6, 0))
        ttk.Label(group, style="Dim.TLabel", wraplength=380,
                  text="Saves the layout and current settings (mode, quality, "
                       "interval, weather, orientation, \u2026). Positions are "
                       "fractions of the canvas so they work in both "
                       "orientations."
                  ).pack(anchor="w", pady=(6, 0))

    def _items_group(self, parent):
        group = ttk.Labelframe(parent, text="Items", padding=10)
        group.pack(fill="x", pady=(0, 10))

        self.item_list = tk.Listbox(group, height=7, bg="#1b1f27",
                                    fg="#e6e9ef", bd=0,
                                    selectbackground="#2f5d94",
                                    selectforeground="#ffffff",
                                    exportselection=False,
                                    activestyle="none")
        self.item_list.pack(fill="x")
        self.item_list.bind("<<ListboxSelect>>", lambda e: self.on_pick())

        row = ttk.Frame(group)
        row.pack(fill="x", pady=(6, 0))
        self.new_type = tk.StringVar(value="stat")
        ttk.Combobox(row, textvariable=self.new_type, state="readonly",
                     width=12,
                     values=list(layout.ITEM_TYPES)).pack(side="left")
        ttk.Button(row, text="Add", command=self.add_item
                   ).pack(side="left", expand=True, fill="x", padx=(6, 0))
        ttk.Button(row, text="Remove", command=self.remove_item
                   ).pack(side="left", expand=True, fill="x", padx=(6, 0))

        # ---- properties of the selected item
        props = ttk.Labelframe(parent, text="Selected item", padding=10)
        props.pack(fill="x", pady=(0, 10))
        self.props = props

        # Field frames are packed into fields_host so they stay above the
        # nudge row when shown/hidden per item type.
        self.fields_host = ttk.Frame(props)
        self.fields_host.pack(fill="x")

        self.prop_source = ttk.Frame(self.fields_host)
        ttk.Label(self.prop_source, text="Readout source", style="Dim.TLabel"
                  ).pack(anchor="w")
        self.item_source = tk.StringVar()
        self.source_box = ttk.Combobox(self.prop_source,
                                       textvariable=self.item_source,
                                       state="readonly", values=[])
        self.source_box.pack(fill="x")
        self.source_box.bind("<<ComboboxSelected>>", lambda e: self.edit(
            source=self.source_labels.get(self.item_source.get(),
                                          self.item_source.get())))

        self.prop_text = ttk.Frame(self.fields_host)
        self.prop_text_label = ttk.Label(
            self.prop_text, text="Fixed text / name prefix", style="Dim.TLabel")
        self.prop_text_label.pack(anchor="w")
        self.item_text = tk.StringVar()
        text_entry = ttk.Entry(self.prop_text, textvariable=self.item_text)
        text_entry.pack(fill="x")
        text_entry.bind("<Return>",
                        lambda e: self.edit(text=self.item_text.get()))
        text_entry.bind("<FocusOut>",
                        lambda e: self.edit(text=self.item_text.get()))
        self.prop_text_hint = ttk.Label(
            self.prop_text, wraplength=340, style="Dim.TLabel", text="")
        self.prop_text_hint.pack(anchor="w", pady=(2, 0))

        self.prop_format = ttk.Frame(self.fields_host)
        ttk.Label(self.prop_format, text="Time format (clock and date)",
                  style="Dim.TLabel").pack(anchor="w")
        self.item_format = tk.StringVar()
        fmt = ttk.Entry(self.prop_format, textvariable=self.item_format)
        fmt.pack(fill="x")
        fmt.bind("<Return>", lambda e: self.edit(format=self.item_format.get()))
        fmt.bind("<FocusOut>",
                 lambda e: self.edit(format=self.item_format.get()))

        self.prop_horizon = ttk.Frame(self.fields_host)
        ttk.Label(self.prop_horizon, text="Forecast horizon",
                  style="Dim.TLabel").pack(anchor="w")
        self.item_horizon = tk.StringVar()
        self.horizon_labels = {
            "now": "Now",
            "1": "1 day", "2": "2 days", "3": "3 days", "4": "4 days",
            "5": "5 days", "6": "6 days", "7": "7 days",
        }
        self.horizon_values = {v: k for k, v in self.horizon_labels.items()}
        self.horizon_box = ttk.Combobox(
            self.prop_horizon, textvariable=self.item_horizon, state="readonly",
            values=[self.horizon_labels[k] for k in layout.WEATHER_FORMATS])
        self.horizon_box.pack(fill="x")
        self.horizon_box.bind("<<ComboboxSelected>>", lambda e: self.edit(
            format=self.horizon_values.get(self.item_horizon.get(), "now")))

        self.prop_size = ttk.Frame(self.fields_host)
        self.item_size = self._item_slider(self.prop_size, "Size", 2, 150,
                                           "size", scale=0.01)

        self.prop_align = ttk.Frame(self.fields_host)
        self.item_align = tk.StringVar()
        ttk.Label(self.prop_align, text="Align", style="Dim.TLabel").pack(
            anchor="w")
        arow = ttk.Frame(self.prop_align)
        arow.pack(fill="x")
        for name in layout.ALIGNMENTS:
            self._radio(arow, name.title(), name, self.item_align,
                        lambda: self.edit(align=self.item_align.get()),
                        side="left")

        self.prop_label = ttk.Frame(self.fields_host)
        self.item_show_label = tk.BooleanVar()
        tk.Checkbutton(self.prop_label, text="Show label",
                       variable=self.item_show_label,
                       command=lambda: self.edit(
                           show_label=self.item_show_label.get()),
                       **self.WIDGET_COLOURS).pack(fill="x", anchor="w")

        self.prop_colours = ttk.Frame(self.fields_host)
        self.item_swatches = {}
        for key, label in (("color", "Value colour"),
                           ("label_color", "Label colour")):
            row = ttk.Frame(self.prop_colours)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=label, width=14).pack(side="left")
            swatch = tk.Button(row, text="", width=6, bd=1, relief="solid",
                               command=lambda k=key: self.pick_item_colour(k))
            swatch.pack(side="left")
            self.item_swatches[key] = swatch

        nudge = ttk.Frame(props)
        nudge.pack(fill="x", pady=(8, 0))
        for label, dx, dy in (("\u2190", -1, 0), ("\u2192", 1, 0),
                              ("\u2191", 0, -1), ("\u2193", 0, 1)):
            button = ttk.Button(nudge, text=label, width=3)
            button.pack(side="left", expand=True, fill="x")
            button.bind("<ButtonPress-1>",
                        lambda e, a=dx, b=dy: self.start_nudge(a, b))
            button.bind("<ButtonRelease-1>", lambda e: self.stop_nudge())
            button.bind("<Leave>", lambda e: self.stop_nudge())
        ttk.Label(props, style="Dim.TLabel", wraplength=380,
                  text="One press moves the item by 0.5% of the canvas; hold "
                       "an arrow down and it accelerates. Items stop at the "
                       "canvas edge rather than sliding off it."
                  ).pack(anchor="w", pady=(6, 0))

    def _weather_group(self, parent):
        group = ttk.Labelframe(parent, text="Weather", padding=10)
        group.pack(fill="x", pady=(0, 10))
        ttk.Label(group, text="Place (city, optional country)",
                  style="Dim.TLabel").pack(anchor="w")
        self.weather_place = tk.StringVar()
        place = ttk.Entry(group, textvariable=self.weather_place)
        place.pack(fill="x")
        place.bind("<Return>", lambda e: self.push(
            weather_place=self.weather_place.get()))
        place.bind("<FocusOut>", lambda e: self.push(
            weather_place=self.weather_place.get()))

        ttk.Label(group, text="Country bias (blank = anywhere)",
                  style="Dim.TLabel").pack(anchor="w", pady=(8, 0))
        self.weather_country = tk.StringVar()
        country = ttk.Entry(group, textvariable=self.weather_country, width=6)
        country.pack(anchor="w")
        country.bind("<Return>", lambda e: self.push(
            weather_country=self.weather_country.get()))
        country.bind("<FocusOut>", lambda e: self.push(
            weather_country=self.weather_country.get()))

        self.weather_lang = tk.StringVar()
        ttk.Label(group, text="Language", style="Dim.TLabel").pack(
            anchor="w", pady=(8, 0))
        lrow = ttk.Frame(group)
        lrow.pack(fill="x")
        for label, value in (("Greek", "el"), ("English", "en")):
            self._radio(lrow, label, value, self.weather_lang,
                        lambda: self.push(weather_lang=self.weather_lang.get()),
                        side="left")

        self.weather_units = tk.StringVar()
        ttk.Label(group, text="Units", style="Dim.TLabel").pack(anchor="w",
                                                                pady=(8, 0))
        urow = ttk.Frame(group)
        urow.pack(fill="x")
        for label, value in (("Celsius", "C"), ("Fahrenheit", "F")):
            self._radio(urow, label, value, self.weather_units,
                        lambda: self.push(weather_units=self.weather_units.get()),
                        side="left")
        ttk.Button(group, text="Refresh weather now",
                   command=self.refresh_weather).pack(fill="x", pady=(8, 0))
        ttk.Label(group, wraplength=380, style="Dim.TLabel",
                  text="Uses Open-Meteo (no API key). Add a Weather item under "
                       "Items. Language picks Greek or Latin place names "
                       "(Γαλάτσι / Galatsi). Leave country blank to search "
                       "worldwide. Weather prefs are saved in settings.json "
                       "for the next launch, and also inside any preset you "
                       "Save as\u2026."
                  ).pack(anchor="w", pady=(6, 0))

    def _gpu_group(self, parent):
        group = ttk.Labelframe(parent, text="GPU sensors", padding=10)
        group.pack(fill="x", pady=(0, 10))
        self.show_gpu = self._check(group, "Poll LibreHardwareMonitor",
                                    "show_gpu")
        ttk.Label(group, text="LibreHardwareMonitor JSON URL",
                  style="Dim.TLabel").pack(anchor="w", pady=(8, 2))
        self.lhm = tk.StringVar()
        entry = ttk.Entry(group, textvariable=self.lhm)
        entry.pack(fill="x")
        entry.bind("<FocusOut>", lambda e: self.push(lhm_url=self.lhm.get()))
        entry.bind("<Return>", lambda e: self.push(lhm_url=self.lhm.get()))
        ttk.Button(group, text="Test GPU sensors",
                   command=self.probe).pack(fill="x", pady=(6, 0))
        ttk.Label(group, wraplength=380, style="Dim.TLabel",
                  text="Needs LibreHardwareMonitor running AS ADMINISTRATOR "
                       "with Options \u2192 Remote Web Server \u2192 Run. "
                       "Without admin it serves an empty sensor tree. Windows "
                       "exposes no vendor-neutral way to read GPU temperature "
                       "from user mode without a kernel driver, and this app "
                       "installs none."
                  ).pack(anchor="w", pady=(6, 0))

    def _panel(self, parent):
        group = ttk.Labelframe(parent, text="Panel", padding=10)
        group.pack(fill="x", pady=(0, 10))
        self.brightness = self._slider(group, "Brightness", 0, 100,
                                       "brightness")

        self.layout = tk.StringVar()
        ttk.Label(group, text="Content orientation",
                  style="Dim.TLabel").pack(anchor="w", pady=(8, 2))
        row = ttk.Frame(group)
        row.pack(fill="x")
        for label, value in (("Horizontal", "horizontal"),
                             ("Vertical", "vertical")):
            self._radio(row, label, value, self.layout,
                        lambda: self._set_orientation(self.layout.get()),
                        side="left")
        self.flip = self._check(group, "Flip 180\u00b0", "flip")
        ttk.Label(group, wraplength=380, style="Dim.TLabel",
                  text="Pick Vertical if the panel is mounted on its end. The "
                       "frame always reaches the device in its portrait "
                       "shape \u2014 anything else makes it tile the image "
                       "and stop accepting writes."
                  ).pack(anchor="w", pady=(4, 0))

        row = ttk.Frame(group)
        row.pack(fill="x", pady=(8, 0))
        ttk.Label(row, text="Info background", width=16).pack(side="left")
        self.bg_swatch = tk.Button(row, text="", width=6, bd=1,
                                   relief="solid",
                                   command=lambda: self.pick_colour("color_bg"))
        self.bg_swatch.pack(side="left")

        self.interval = self._spin(group, "Interval (ms)", 100, 2000,
                                   "interval_ms", step=10)
        self.quality = self._spin(group, "JPEG quality", 40, 95, "quality")
        ttk.Label(group, wraplength=380, style="Dim.TLabel",
                  text="350 ms is the cadence verified safe upstream. Lower "
                       "is smoother for GIFs but increases USB dropouts."
                  ).pack(anchor="w", pady=(4, 0))
        ttk.Button(group, text="Wake panel (DIS \u2192 LIG)",
                   command=self.wake).pack(fill="x", pady=(8, 0))
        if self.phone_url:
            ttk.Label(group, text="Phone UI (same Wi-Fi)",
                      style="Dim.TLabel").pack(anchor="w", pady=(10, 0))
            self.phone_url_var = tk.StringVar(value=self.phone_url)
            ttk.Entry(group, textvariable=self.phone_url_var,
                      state="readonly").pack(fill="x")
            ttk.Button(group, text="Copy phone URL",
                       command=self.copy_phone_url).pack(fill="x", pady=(4, 0))
            ttk.Label(group, wraplength=380, style="Dim.TLabel",
                      text="Open that address in the phone browser. Only on "
                           "your LAN \u2014 do not forward the port to the "
                           "internet; there is no login."
                      ).pack(anchor="w", pady=(4, 0))

    # -- widget helpers ---------------------------------------------------

    # Classic tk widgets, not ttk, for anything with an indicator. The clam
    # theme paints its own hover background behind check and radio labels and
    # ignores parts of what style.map sets for the active state, which came
    # out as white-on-white. tk.Checkbutton and tk.Radiobutton take explicit
    # colours for every state and honour them directly.

    WIDGET_COLOURS = dict(bg="#14161b", fg="#e6e9ef",
                          activebackground="#20252f",
                          activeforeground="#ffffff",
                          selectcolor="#1b1f27",
                          disabledforeground="#5b6478",
                          highlightthickness=0, bd=0, anchor="w",
                          takefocus=False)

    def _check(self, parent, label, key, command=None):
        var = tk.BooleanVar()
        action = command or (lambda: self.push(**{key: var.get()}))
        tk.Checkbutton(parent, text=label, variable=var, command=action,
                       **self.WIDGET_COLOURS).pack(fill="x", anchor="w")
        return var

    def _radio(self, parent, label, value, var, command, side=None):
        widget = tk.Radiobutton(parent, text=label, value=value,
                                variable=var, command=command,
                                **self.WIDGET_COLOURS)
        if side:
            widget.pack(side=side, padx=(0, 10))
        else:
            widget.pack(fill="x", anchor="w")
        return widget

    def _slider(self, parent, label, low, high, key):
        ttk.Label(parent, text=label, style="Dim.TLabel").pack(anchor="w",
                                                               pady=(8, 0))
        var = tk.IntVar()
        scale = ttk.Scale(parent, from_=low, to=high, orient="horizontal")
        scale.pack(fill="x")
        readout = ttk.Label(parent, text="", style="Dim.TLabel")
        readout.pack(anchor="e")

        def moved(value):
            var.set(int(float(value)))
            readout.configure(text=str(var.get()))
        scale.configure(command=moved)
        # push on release only: dragging would otherwise fire a write per pixel
        scale.bind("<ButtonRelease-1>",
                   lambda e: self.push(**{key: var.get()}))
        return (var, scale, readout)

    def _spin(self, parent, label, low, high, key, step=1):
        ttk.Label(parent, text=label, style="Dim.TLabel").pack(anchor="w",
                                                               pady=(8, 0))
        var = tk.IntVar()
        spin = ttk.Spinbox(parent, from_=low, to=high, increment=step,
                           textvariable=var, width=10,
                           command=lambda: self.push(**{key: var.get()}))
        spin.pack(fill="x")
        spin.bind("<Return>", lambda e: self.push(**{key: var.get()}))
        spin.bind("<FocusOut>", lambda e: self.push(**{key: var.get()}))
        return (var, spin)

    def _item_slider(self, parent, label, low, high, key, scale=1.0):
        ttk.Label(parent, text=label, style="Dim.TLabel").pack(anchor="w",
                                                               pady=(8, 0))
        var = tk.IntVar()
        widget = ttk.Scale(parent, from_=low, to=high, orient="horizontal")
        widget.pack(fill="x")
        readout = ttk.Label(parent, text="", style="Dim.TLabel")
        readout.pack(anchor="e")

        def moved(value):
            var.set(int(float(value)))
            readout.configure(text=str(var.get()))
        widget.configure(command=moved)
        widget.bind("<ButtonRelease-1>",
                    lambda e: self.edit(**{key: var.get() * scale}))
        return (var, widget, readout, scale)

    # -- layout editing ---------------------------------------------------

    def items(self):
        with core.state_lock:
            return [dict(i) for i in core.state["items"]]

    def selected_item(self):
        for item in self.items():
            if item["id"] == self.selected:
                return item
        return None

    def edit(self, **changes):
        """Change fields on the selected item and push the whole list."""
        if self._suspend or not self.selected:
            return
        items = self.items()
        for item in items:
            if item["id"] == self.selected:
                item.update(changes)
                break
        else:
            return
        core.apply_patch({"items": items})
        self.pull()

    NUDGE_BASE = 0.005        # fraction of the canvas per step
    NUDGE_MAX = 0.05          # ceiling once the button is held down
    NUDGE_FIRST_MS = 350      # pause before repeating, as with a key repeat
    NUDGE_EVERY_MS = 45

    def nudge(self, dx, dy, step=None):
        item = self.selected_item()
        if not item:
            return
        step = self.NUDGE_BASE if step is None else step
        moved = dict(item)
        moved["x"] = item["x"] + dx * step
        moved["y"] = item["y"] + dy * step
        x, y = layout.clamp(moved, core.runtime.get("boxes"),
                            core.runtime.get("box_canvas"))
        if (x, y) != (item["x"], item["y"]):
            self.edit(x=x, y=y)

    def start_nudge(self, dx, dy):
        """First press, then accelerate while the button is held."""
        self.stop_nudge()
        self._nudge_step = self.NUDGE_BASE
        self.nudge(dx, dy, self._nudge_step)
        self._nudge_job = self.root.after(
            self.NUDGE_FIRST_MS, self._repeat_nudge, dx, dy)

    def _repeat_nudge(self, dx, dy):
        self._nudge_step = min(self.NUDGE_MAX, self._nudge_step * 1.25)
        self.nudge(dx, dy, self._nudge_step)
        self._nudge_job = self.root.after(
            self.NUDGE_EVERY_MS, self._repeat_nudge, dx, dy)

    def stop_nudge(self):
        if self._nudge_job is not None:
            try:
                self.root.after_cancel(self._nudge_job)
            except Exception:
                pass
            self._nudge_job = None

    def add_item(self):
        items = self.items()
        kind = self.new_type.get()
        fresh = layout.new_item(kind, x=0.05, y=0.05)
        if fresh["type"] == "text":
            fresh["text"] = "Text"
        if fresh["type"] == "weather":
            fresh["format"] = "now"
            fresh["size"] = 0.22
            fresh["color"] = "#ebe6d5"
            fresh["label_color"] = "#8a9bb0"
        if fresh["type"] == "worldclock":
            fresh["text"] = "New York, US"
            fresh["size"] = 0.16
            fresh["color"] = "#ebe6d5"
            fresh["label_color"] = "#8a9bb0"
        if fresh["type"] == "nowplaying":
            fresh["size"] = 0.14
            fresh["width"] = 0.55
            fresh["color"] = "#ebe6d5"
            fresh["label_color"] = "#8a9bb0"
            fresh["show_label"] = True
        items.append(fresh)
        core.apply_patch({"items": items})
        self.selected = fresh["id"]
        self.pull()
        if fresh["type"] == "weather":
            self.refresh_weather()

    def remove_item(self):
        if not self.selected:
            return
        items = [i for i in self.items() if i["id"] != self.selected]
        core.apply_patch({"items": items})
        self.selected = items[-1]["id"] if items else None
        self.pull()

    def on_pick(self):
        picked = self.item_list.curselection()
        if picked:
            order = self.items()
            if picked[0] < len(order):
                self.selected = order[picked[0]]["id"]
                self.pull()

    def pick_item_colour(self, key):
        item = self.selected_item()
        if not item:
            return
        chosen = colorchooser.askcolor(color=item[key], parent=self.root)[1]
        if chosen:
            self.edit(**{key: chosen})

    # -- dragging on the preview -----------------------------------------

    def canvas_point(self, event):
        """Mouse position in authored-canvas fractions, or None if outside."""
        if not self._shot_box:
            return None
        ox, oy, sw, sh = self._shot_box
        if sw <= 0 or sh <= 0:
            return None
        fx = (event.x - ox) / float(sw)
        fy = (event.y - oy) / float(sh)
        if not (0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0):
            return None
        return fx, fy

    def on_press(self, event):
        point = self.canvas_point(event)
        if not point:
            return
        boxes = dict(core.runtime.get("boxes") or {})
        cw, ch = core.runtime.get("box_canvas") or (0, 0)
        if not boxes or not cw:
            return
        hit = layout.hit_test(boxes, point[0] * cw, point[1] * ch,
                              slack=max(6, cw * 0.01))
        if hit:
            self.selected = hit
            item = self.selected_item()
            # remember the grab offset so the item does not jump to the cursor
            self._grab = (point[0] - item["x"], point[1] - item["y"])
            self.pull()
        else:
            self._grab = None

    def on_drag(self, event):
        if not self._grab or not self.selected:
            return
        point = self.canvas_point(event)
        if not point:
            return
        # Apply immediately but skip the widget repopulate: doing a full pull
        # on every motion event makes the drag stutter.
        items = self.items()
        for item in items:
            if item["id"] == self.selected:
                item["x"] = point[0] - self._grab[0]
                item["y"] = point[1] - self._grab[1]
                item["x"], item["y"] = layout.clamp(
                    item, core.runtime.get("boxes"),
                    core.runtime.get("box_canvas"))
                break
        core.apply_patch({"items": items})

    def on_release(self, event):
        if self._grab:
            self._grab = None
            self.pull()

    # -- presets ----------------------------------------------------------

    def load_preset(self):
        name = self.preset.get()
        try:
            items, scene = layout.load_preset(core.HERE, name)
        except Exception as exc:
            messagebox.showerror("Preset", "Cannot load %r:\n%s" % (name, exc),
                                 parent=self.root)
            return
        patch = {"items": items, "preset": name}
        if scene:
            patch.update(scene)
        core.apply_patch(patch)
        self.selected = items[0]["id"] if items else None
        self.pull()

    def save_preset(self):
        from tkinter import simpledialog
        current = self.preset.get()
        name = simpledialog.askstring("Save preset", "Preset name:",
                                      initialvalue=current, parent=self.root)
        if not name:
            return
        with core.state_lock:
            scene = layout.scene_from_state(core.state)
        try:
            saved = layout.save_preset(core.HERE, name, self.items(),
                                       scene=scene)
        except Exception as exc:
            messagebox.showerror("Preset", "Cannot save:\n%s" % exc,
                                 parent=self.root)
            return
        core.apply_patch({"preset": saved})
        self.pull()

    def delete_preset(self):
        name = self.preset.get()
        if name in layout.DEFAULT_PRESETS:
            messagebox.showinfo("Preset", "Built-in presets cannot be "
                                          "deleted.", parent=self.root)
            return
        if not messagebox.askyesno("Preset", "Delete preset %r?" % name,
                                   parent=self.root):
            return
        layout.delete_preset(core.HERE, name)
        self.pull()

    # -- state ------------------------------------------------------------

    # Built-in presets that pair with content orientation. Switching
    # Horizontal <-> Vertical loads the matching dashboard when the user is
    # still on one of these names -- custom presets are left alone.
    ORIENT_PRESETS = {
        ("horizontal", "vertical"): {
            "Wide dashboard": "Tall dashboard",
            "Wide clock only": "Tall clock only",
            "Media with caption": "Tall dashboard",
        },
        ("vertical", "horizontal"): {
            "Tall dashboard": "Wide dashboard",
            "Tall clock only": "Wide clock only",
        },
    }

    def _set_mode(self, mode):
        self.push(mode=mode)
        self._sync_mode_widgets(mode)

    def _set_orientation(self, orientation):
        """Change layout; swap to a matching built-in preset when appropriate."""
        with core.state_lock:
            old = core.state["layout"]
            preset = core.state["preset"]
        patch = {"layout": orientation}
        target = self.ORIENT_PRESETS.get((old, orientation), {}).get(preset)
        if target and old != orientation:
            try:
                # Orient pairs are layout swaps only. Never apply a shadowed
                # user scene here -- that would wipe mode / weather / quality
                # when someone Save as…'d over a built-in name.
                items, _scene = layout.load_preset(core.HERE, target)
                patch["items"] = items
                patch["preset"] = target
            except Exception:
                pass
        self.push(**patch)

    def _sync_mode_widgets(self, mode=None):
        if mode is None:
            mode = self.mode.get()
        if mode in ("media", "slideshow"):
            self.fit_frame.pack(fill="x", pady=(0, 0))
        else:
            self.fit_frame.pack_forget()
        if mode == "slideshow":
            self.slide_frame.pack(fill="x", pady=(0, 0))
        else:
            self.slide_frame.pack_forget()

    def _sync_prop_widgets(self, item_type):
        """Show only the fields that the selected item type uses."""
        for frame in (self.prop_source, self.prop_text, self.prop_format,
                      self.prop_horizon, self.prop_size, self.prop_align,
                      self.prop_label, self.prop_colours):
            frame.pack_forget()
        if not item_type:
            return
        order = []
        if item_type == "stat":
            order = [self.prop_source, self.prop_size, self.prop_align,
                     self.prop_label, self.prop_colours]
        elif item_type in ("clock", "date"):
            order = [self.prop_format, self.prop_size, self.prop_align,
                     self.prop_colours]
        elif item_type == "weather":
            order = [self.prop_text, self.prop_horizon, self.prop_size,
                     self.prop_align, self.prop_label, self.prop_colours]
            self.prop_text_label.configure(text="Place override")
            self.prop_text_hint.configure(
                text="Blank uses the Weather place above. Override is limited "
                     "to Now or 1 day.")
        elif item_type == "worldclock":
            order = [self.prop_text, self.prop_size, self.prop_align,
                     self.prop_label, self.prop_colours]
            self.prop_text_label.configure(text="City")
            self.prop_text_hint.configure(
                text="e.g. New York, US or Tokyo. Shows local time and the "
                     "offset from this PC.")
        elif item_type == "nowplaying":
            order = [self.prop_size, self.prop_label, self.prop_colours]
        elif item_type == "text":
            order = [self.prop_text, self.prop_size, self.prop_align,
                     self.prop_colours]
            self.prop_text_label.configure(text="Fixed text / name prefix")
            self.prop_text_hint.configure(text="")
        elif item_type == "filename":
            order = [self.prop_text, self.prop_size, self.prop_align,
                     self.prop_label, self.prop_colours]
            self.prop_text_label.configure(text="Fixed text / name prefix")
            self.prop_text_hint.configure(text="")
        for frame in order:
            frame.pack(fill="x", pady=(8, 0) if frame is not order[0] else (0, 0))

    def show_help(self):
        win = tk.Toplevel(self.root)
        win.title("%s \u2014 Help" % app_title())
        win.configure(bg="#14161b")
        win.minsize(420, 360)
        text = (
            "Getting started\n"
            "\n"
            "\u2022 Close the official MiraBox software before opening "
            "CleanD92 \u2014 it holds the USB device open.\n"
            "\u2022 Put images and GIFs in the media folder beside the exe "
            "(Open media folder), or drop a file onto the preview. "
            "Ultrawide / 32:9 sources crop cleanly; phone portraits do not.\n"
            "\u2022 Short MP4/MOV clips can become GIFs via Convert short "
            "clip\u2026 if ffmpeg is on PATH. The exe does not bundle ffmpeg.\n"
            "\u2022 Info screen works with an empty media folder. Image / "
            "GIF and Slideshow need files there.\n"
            "\u2022 Save as\u2026 stores a full scene: layout items plus mode, "
            "quality, interval, orientation, weather, slideshow options and "
            "LHM settings. Hold is not saved. Built-in presets are layout "
            "only. settings.json still remembers the last weather place "
            "across restarts.\n"
            "\n"
            "Temperatures and GPU\n"
            "\n"
            "\u2022 CPU load, RAM, disk usage, network and uptime need "
            "nothing else.\n"
            "\u2022 CPU / GPU / disk temperatures need LibreHardwareMonitor "
            "running as administrator with Options \u2192 Remote Web Server "
            "\u2192 Run, then tick Poll LibreHardwareMonitor here.\n"
            "\n"
            "Weather\n"
            "\n"
            "\u2022 Add a Weather item, set Place (e.g. Galatsi) and optional "
            "country bias (blank = anywhere). Horizon is Now or 1\u20137 days. "
            "Language Greek/English picks place names and weekday labels. "
            "A Place override on the item can show a second city (Now / 1 day). "
            "World clock items show another city's time and the offset from "
            "this PC. Data comes from Open-Meteo; nothing is uploaded except "
            "the place name lookup.\n"
            "\n"
            "Now playing\n"
            "\n"
            "\u2022 Add a Now playing item to show the title, artist and a "
            "small album thumbnail from whatever this PC is playing "
            "(Spotify, browser, Groove, \u2026). It reads the Windows media "
            "session; phone or watch players are not visible here. Tick "
            "Show label for the artist line.\n"
            "\n"
            "If the panel goes black\n"
            "\n"
            "\u2022 Unplug and replug the panel, then quit and open "
            "CleanD92 again. There is no reconnect button on purpose: "
            "reopening the HID handle within one plug leaves the panel "
            "black until a physical replug.\n"
            "\u2022 350 ms between frames is the safe cadence. Lower is "
            "smoother for GIFs and raises the chance of random USB "
            "dropouts.\n"
            "\n"
            "Hold keeps the panel on its last frame while you edit; Apply "
            "now pushes one fresh frame.\n"
            "\n"
            "Minimise sends the window to the notification area (tray); the "
            "panel keeps updating. Double-click the tray icon or choose Show "
            "to bring the window back. The window close button still quits."
        )
        body = tk.Text(win, wrap="word", bg="#1b1f27", fg="#e6e9ef",
                       bd=0, padx=14, pady=12, font=("Segoe UI", 10))
        body.pack(fill="both", expand=True, padx=12, pady=12)
        body.insert("1.0", text)
        body.configure(state="disabled")
        # Version + creator sit outside the scroll text so the link stays
        # clickable (a disabled Text swallows Button-1).
        foot = tk.Frame(win, bg="#14161b")
        foot.pack(fill="x", padx=12, pady=(0, 8))
        tk.Label(foot, text=app_title(), bg="#14161b", fg="#98a3b6",
                 font=("Segoe UI", 9)).pack(side="left")
        creator_link(foot).pack(side="right")
        ttk.Button(win, text="Close", command=win.destroy).pack(pady=(0, 12))

    def push(self, **patch):
        """Validate through the same code path the browser UI uses."""
        if self._suspend:
            return
        core.apply_patch(patch)
        self.pull()

    def pull(self):
        """Repopulate every widget from shared state."""
        self._suspend = True
        try:
            with core.state_lock:
                st = dict(core.state)
                items = [dict(i) for i in st["items"]]
            self.mode.set(st["mode"])
            self.fit.set(st["fit"])
            self.layout.set(st["layout"])
            self.flip.set(st["flip"])
            self.lhm.set(st["lhm_url"])
            self.show_gpu.set(st["show_gpu"])
            self.hold.set(st["hold"])
            self.slide_shuffle.set(st["slide_shuffle"])
            self.weather_place.set(st.get("weather_place", ""))
            self.weather_country.set(st.get("weather_country", "GR"))
            self.weather_units.set(st.get("weather_units", "C"))
            self.weather_lang.set(st.get("weather_lang", "el"))
            var, scale, readout = self.brightness
            var.set(st["brightness"])
            scale.set(st["brightness"])
            readout.configure(text=str(st["brightness"]))
            for holder, key in ((self.slide_seconds, "slide_seconds"),
                                (self.interval, "interval_ms"),
                                (self.quality, "quality")):
                holder[0].set(st[key])
            self.bg_swatch.configure(bg=st["color_bg"],
                                     activebackground=st["color_bg"])

            names = layout.list_presets(core.HERE)
            self.preset_box.configure(values=names or ["(none)"])
            self.preset.set(st["preset"] if st["preset"] in names
                            else (names[0] if names else ""))

            if self.selected not in {i["id"] for i in items}:
                self.selected = items[0]["id"] if items else None
            self.item_list.delete(0, "end")
            chosen = None
            for index, item in enumerate(items):
                kind = layout.ITEM_TYPES[item["type"]]
                if item["type"] == "stat":
                    detail = item.get("source") or ""
                elif item["type"] == "weather":
                    detail = item.get("format") or "now"
                elif item["type"] == "nowplaying":
                    detail = "media"
                else:
                    label, value = layout.item_strings(
                        item, {"filename": "\u2026"})
                    detail = (value or label or "")[:20]
                self.item_list.insert("end", "%-9s %s" % (kind, detail))
                if item["id"] == self.selected:
                    chosen = index
            if chosen is not None:
                self.item_list.selection_clear(0, "end")
                self.item_list.selection_set(chosen)

            item = self.selected_item()
            sources = core.available_sources()
            self.source_labels = {"%s \u2014 %s" % (k, v): k
                                  for k, v in sources.items()}
            self.source_box.configure(values=sorted(self.source_labels))
            if item:
                shown = [t for t, k in self.source_labels.items()
                         if k == item["source"]]
                self.item_source.set(shown[0] if shown else item["source"])
                self.item_text.set(item["text"])
                self.item_format.set(item["format"])
                # Override places only offer now / 1 day in the combobox.
                if item["type"] == "weather" and (item.get("text") or "").strip():
                    allowed = ("now", "1")
                else:
                    allowed = layout.WEATHER_FORMATS
                self.horizon_box.configure(
                    values=[self.horizon_labels[k] for k in allowed])
                self.item_horizon.set(
                    self.horizon_labels.get(item["format"], "Now"))
                self.item_align.set(item["align"])
                self.item_show_label.set(item["show_label"])
                var, widget, readout, factor = self.item_size
                percent = int(round(item["size"] / factor))
                var.set(percent)
                widget.set(percent)
                readout.configure(text=str(percent))
                for key, swatch in self.item_swatches.items():
                    swatch.configure(bg=item[key], activebackground=item[key])
                self.props.configure(text="Selected item \u2014 %s" %
                                          layout.ITEM_TYPES[item["type"]])
                self._sync_prop_widgets(item["type"])
            else:
                self.props.configure(text="Selected item \u2014 none")
                self._sync_prop_widgets(None)

            files = core.list_media()
            self.media_box.configure(values=files or ["(media folder empty)"])
            if st["media"] in files:
                self.media.set(st["media"])
            elif files:
                self.media.set(files[0])
            else:
                self.media.set("(media folder empty)")
            self._sync_mode_widgets(st["mode"])
        finally:
            self._suspend = False

    def rescan(self):
        self.pull()

    def _hook_file_drop(self, widget):
        """Accept Explorer drops on the preview. windnd is Windows-only, which
        matches this app; if it is missing the drop path is simply absent."""
        try:
            import windnd
        except ImportError:
            return

        def dropped(files):
            # windnd delivers bytes paths on a non-UI thread; hop to Tk.
            paths = []
            for entry in files or []:
                if isinstance(entry, bytes):
                    paths.append(entry.decode(sys.getfilesystemencoding(),
                                              "surrogateescape"))
                else:
                    paths.append(str(entry))
            if paths:
                self.root.after(0, self._import_dropped, paths)

        try:
            windnd.hook_dropfiles(widget, func=dropped)
        except Exception:
            pass

    def _import_dropped(self, paths):
        """Copy the first usable dropped file into media/ and show it."""
        errors = []
        for path in paths:
            try:
                name = core.import_media_file(path)
            except ValueError as exc:
                errors.append("%s: %s" % (os.path.basename(path), exc))
                continue
            except OSError as exc:
                errors.append("%s: %s" % (os.path.basename(path), exc))
                continue
            self.push(media=name, mode="media")
            with core.state_lock:
                core.runtime["message"] = "imported %s" % name
            return
        if errors:
            messagebox.showerror(
                "Drop file",
                "Could not import:\n" + "\n".join(errors[:6]),
                parent=self.root)

    def convert_clip(self):
        """Optional MP4/MOV/... -> GIF via system ffmpeg. Nothing is bundled."""
        if not core.find_ffmpeg():
            messagebox.showinfo(
                "Convert clip",
                "ffmpeg was not found on PATH.\n\n"
                "Install it from https://ffmpeg.org, make sure `ffmpeg` "
                "works in a terminal, then reopen CleanD92.\n\n"
                "Alternatively convert the clip yourself and drop the GIF "
                "onto the preview.",
                parent=self.root)
            return
        path = filedialog.askopenfilename(
            parent=self.root,
            title="Short clip to convert",
            filetypes=[
                ("Video", "*.mp4 *.mov *.mkv *.webm *.avi"),
                ("All files", "*.*"),
            ])
        if not path:
            return
        with core.state_lock:
            core.runtime["message"] = "converting %s \u2026" % (
                os.path.basename(path),)

        def work():
            try:
                name = core.convert_clip_to_gif(path)
            except (ValueError, RuntimeError, OSError) as exc:
                self.root.after(0, lambda: messagebox.showerror(
                    "Convert clip", str(exc), parent=self.root))
                with core.state_lock:
                    core.runtime["message"] = "convert failed"
                return
            self.root.after(0, lambda: self.push(media=name, mode="media"))
            with core.state_lock:
                core.runtime["message"] = "converted %s" % name

        threading.Thread(target=work, daemon=True).start()

    def open_folder(self):
        os.makedirs(core.MEDIA_DIR, exist_ok=True)
        try:
            os.startfile(core.MEDIA_DIR)          # Windows only, by design
        except AttributeError:
            messagebox.showinfo("Media folder", core.MEDIA_DIR)

    def pick_colour(self, key):
        with core.state_lock:
            current = core.state[key]
        chosen = colorchooser.askcolor(color=current, parent=self.root)[1]
        if chosen:
            self.push(**{key: chosen})

    def refresh_weather(self):
        """Kick the weather poller immediately (still off the render thread)."""
        with core.state_lock:
            place = core.state.get("weather_place", "")
            country = core.state.get("weather_country", "GR")
            units = core.state.get("weather_units", "C")
            lang = core.state.get("weather_lang", "el")

        def work():
            core._weather_invalidate = True
            data = core.fetch_weather(place, country, units, lang)
            if data:
                msg = "weather: %s %s" % (data.get("place"), data.get("temp"))
            else:
                _, err, _ = core.weather_snapshot()
                msg = "weather: %s" % (err or "failed")
            with core.state_lock:
                core.runtime["message"] = msg

        threading.Thread(target=work, daemon=True).start()

    def wake(self):
        with core.state_lock:
            core.runtime["wake"] = True

    def copy_phone_url(self):
        url = self.phone_url or format_phone_urls()
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(url)
            with core.state_lock:
                core.runtime["message"] = "phone URL copied: %s" % url
        except Exception as exc:
            messagebox.showerror("Phone URL", "Cannot copy:\n%s" % exc,
                                 parent=self.root)

    def apply_now(self):
        """Push exactly one fresh frame, even while Hold is on."""
        with core.state_lock:
            core.runtime["apply_once"] = True

    def probe(self):
        with core.state_lock:
            url = core.state["lhm_url"]
        self.sensors.configure(state="normal")
        self.sensors.delete("1.0", "end")
        self.sensors.insert("end", "reading %s \u2026\n" % url)
        self.sensors.configure(state="disabled")
        self.root.update_idletasks()

        def work():
            values = core.fetch_lhm(url)
            with core._lhm_lock:
                error = core._lhm_cache["error"]
                hardware = list(core._lhm_cache["hardware"])
            text = []
            if error:
                text.append("error: %s\n" % error)
            text.append("sensor sources available to the layout:")
            text.extend("  %-14s %-12s %s" % (key, label, value)
                        for key, (label, value) in sorted(values.items()))
            if not values:
                text.append("  (none)")
            text.append("")
            text.append("hardware LibreHardwareMonitor reports:")
            text.extend("  %s" % name for name in hardware)
            if not hardware:
                text.append("  (none)")
            self.root.after(0, self._show_sensors, "\n".join(text))

        threading.Thread(target=work, daemon=True).start()

    def _show_sensors(self, text):
        self.sensors.configure(state="normal")
        self.sensors.delete("1.0", "end")
        self.sensors.insert("1.0", text)
        self.sensors.configure(state="disabled")

    # -- periodic ---------------------------------------------------------

    def tick(self):
        with core.preview_lock:
            jpeg = core.preview["jpeg"]
        if jpeg:
            try:
                shot = Image.open(io.BytesIO(jpeg))
                canvas = self.preview
                # Fit the widget, not a fixed size. The old version scaled to
                # a constant 760 px and drew it at x=0, so on a narrower
                # window everything past the right edge was simply cut off --
                # items looked as if they had vanished while the panel itself
                # was showing them correctly.
                room = max(120, canvas.winfo_width() - 8)
                shot.thumbnail((room, PREVIEW_MAX_HEIGHT), Image.LANCZOS)
                self._photo = ImageTk.PhotoImage(shot)
                canvas.delete("all")
                width = max(canvas.winfo_width(), shot.width)
                ox = (width - shot.width) // 2
                oy = 2
                canvas.configure(height=shot.height + 4)
                canvas.create_image(ox, oy, anchor="nw", image=self._photo)
                self._shot_box = (ox, oy, shot.width, shot.height)

                # outline the selected item so it is obvious what drags
                boxes = dict(core.runtime.get("boxes") or {})
                cw, ch = core.runtime.get("box_canvas") or (0, 0)
                if self.selected in boxes and cw and ch:
                    x0, y0, x1, y1 = boxes[self.selected]
                    sx = shot.width / float(cw)
                    sy = shot.height / float(ch)
                    canvas.create_rectangle(
                        ox + x0 * sx - 3, oy + y0 * sy - 3,
                        ox + x1 * sx + 3, oy + y1 * sy + 3,
                        outline="#4da3ff", width=2, dash=(4, 3))
            except Exception:
                pass

        rt = dict(core.runtime)
        with core.state_lock:
            holding = core.state["hold"]
            show_gpu = core.state["show_gpu"]
        bad = rt["status"] in ("error", "stopped")
        parts = [rt["status"]]
        if holding and not bad:
            parts.append("holding")
        line = "%s \u00b7 %d frames \u00b7 %d ms" % (
            " \u00b7 ".join(parts), rt["frames"], rt["last_ms"])
        message = rt["message"] or ""
        if show_gpu and not bad:
            _, lhm_error, stamp = core.lhm_values()
            if stamp and lhm_error and "refused" in lhm_error.lower():
                hint = ("LibreHardwareMonitor is not reachable \u2014 run it "
                        "as administrator with the remote web server on")
                message = ("%s\n%s" % (message, hint)).strip() if message \
                    else hint
        self.status.configure(
            bg="#2a1417" if bad else ("#2a2412" if holding else "#12261a"),
            fg="#ffb3bd" if bad else ("#e6d29f" if holding else "#9fe3b6"),
            text="%s%s" % (line, "\n" + message if message else ""))
        self._after = self.root.after(REFRESH_MS, self.tick)

    def quit(self):
        self._quitting = True
        self._stop_tray()
        self.stop_nudge()
        # Cancel the pending refresh first: without this the callback fires
        # after destroy() and Tk prints "invalid command name" on exit.
        if self._after is not None:
            try:
                self.root.after_cancel(self._after)
            except Exception:
                pass
            self._after = None
        with core.state_lock:
            core.runtime["stop"] = True
        self.root.after(400, self._finish)

    def _finish(self):
        try:
            self.panel.close()
        except Exception:
            pass
        self.root.destroy()

    # -- system tray (minimise) -------------------------------------------

    def _on_unmap(self, event):
        # Only the toplevel's own minimise, not a child Unmap.
        if event.widget is not self.root or self._quitting:
            return
        try:
            state = self.root.state()
        except tk.TclError:
            return
        if state == "iconic":
            self.root.after_idle(self._minimize_to_tray)

    def _minimize_to_tray(self):
        if self._quitting:
            return
        # Start the tray icon before withdraw so a missing pystray leaves the
        # window as a normal taskbar minimise instead of vanishing.
        if self._tray is None and not self._start_tray():
            return
        try:
            self.root.withdraw()
        except tk.TclError:
            pass

    def _tray_image(self):
        """Load icon.ico for the tray; fall back to a tiny drawn mark."""
        candidates = []
        if getattr(sys, "frozen", False):
            meipass = getattr(sys, "_MEIPASS", None)
            if meipass:
                candidates.append(os.path.join(meipass, "icon.ico"))
            candidates.append(os.path.join(core.HERE, "icon.ico"))
        here = os.path.dirname(os.path.abspath(__file__))
        candidates.append(os.path.join(here, "icon.ico"))
        candidates.append(os.path.join(core.HERE, "icon.ico"))
        for path in candidates:
            if not os.path.isfile(path):
                continue
            try:
                img = Image.open(path)
                img = img.convert("RGBA")
                img.thumbnail((64, 64), Image.Resampling.LANCZOS)
                return img
            except Exception:
                continue
        img = Image.new("RGBA", (64, 64), (20, 22, 27, 255))
        # Simple mark so a missing icon file still shows something visible.
        from PIL import ImageDraw
        draw = ImageDraw.Draw(img)
        draw.ellipse((8, 8, 56, 56), fill=(77, 163, 255, 255))
        return img

    def _start_tray(self):
        try:
            import pystray
        except ImportError:
            return False
        try:
            icon = pystray.Icon(
                "CleanD92",
                self._tray_image(),
                app_title(),
                menu=pystray.Menu(
                    pystray.MenuItem(
                        "Show", self._restore_from_tray, default=True),
                    pystray.MenuItem("Quit", self._quit_from_tray),
                ),
            )
        except Exception:
            return False
        self._tray = icon
        # pystray.run blocks; keep it off the Tk thread.
        threading.Thread(target=icon.run, name="tray", daemon=True).start()
        return True

    def _stop_tray(self):
        icon = self._tray
        self._tray = None
        if icon is None:
            return
        try:
            icon.stop()
        except Exception:
            pass

    def _restore_from_tray(self, _icon=None, _item=None):
        # Tray callbacks arrive on pystray's thread -- hop back to Tk.
        def show():
            self._stop_tray()
            try:
                self.root.deiconify()
                self.root.state("normal")
                self.root.lift()
                self.root.focus_force()
            except tk.TclError:
                pass
        try:
            self.root.after(0, show)
        except tk.TclError:
            pass

    def _quit_from_tray(self, _icon=None, _item=None):
        try:
            self.root.after(0, self.quit)
        except tk.TclError:
            pass


# -------------------------------------------------------------------- boot

def start_backend():
    """Open the device, replay the wake sequence, start the render thread."""
    os.makedirs(core.MEDIA_DIR, exist_ok=True)
    # README promises both folders on first run; presets/ was only created
    # when the user saved one.
    os.makedirs(layout.preset_dir(core.HERE), exist_ok=True)
    first_run = not os.path.isfile(core.SETTINGS_PATH)
    core.load_settings()
    panel = D92().open()
    with core.state_lock:
        brightness = core.state["brightness"]
        if first_run:
            # One-shot coaching until the render loop has something newer to
            # say (empty media, wake, etc.). Settings.json appears when the
            # user first edits a weather preference.
            core.runtime["message"] = (
                "first run -- Info screen is on the panel. Drop images onto "
                "the preview or into media/ when you want them; Help has the "
                "rest")
    panel.wake(brightness)
    threading.Thread(target=core.render_loop, args=(panel,),
                     daemon=True).start()
    return panel


def start_web():
    """Serve the browser UI on all interfaces so a phone on the LAN can open it."""
    server = core.ThreadingHTTPServer((core.WEB_HOST, core.PORT), core.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def format_phone_urls():
    urls = core.phone_urls()
    if not urls:
        return "http://127.0.0.1:%d" % core.PORT
    return urls[0]


DEVICE_HELP = (
    "Could not open the StreamDock D92.\n\n"
    "\u2022 Check the panel is plugged in.\n"
    "\u2022 Close the official MiraBox software \u2014 it holds the device "
    "open.\n"
    "\u2022 Close any other copy of this app.\n"
    "\u2022 If the panel has gone black and nothing else helps, unplug and "
    "replug it.\n\n"
    "Details: %s"
)


def no_device_dialog(exc):
    """Stay-open window so the user can plug in and Retry without relaunching
    from Explorer. Retry starts a fresh open attempt; we never reopen a handle
    that already succeeded (that path is what blacks the panel)."""
    result = {"retry": False}
    root = tk.Tk()
    root.title(app_title())
    root.configure(bg="#14161b")
    root.minsize(440, 280)
    frame = ttk.Frame(root, padding=16)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="No panel found",
              font=("Segoe UI", 12, "bold")).pack(anchor="w")
    body = tk.Text(frame, wrap="word", height=12, bg="#1b1f27", fg="#e6e9ef",
                   bd=0, padx=10, pady=8, font=("Segoe UI", 10))
    body.pack(fill="both", expand=True, pady=(10, 12))
    body.insert("1.0", DEVICE_HELP % exc)
    body.configure(state="disabled")
    meta = tk.Frame(frame, bg="#14161b")
    meta.pack(fill="x", pady=(0, 10))
    tk.Label(meta, text=app_title(), bg="#14161b", fg="#98a3b6",
             font=("Segoe UI", 9)).pack(side="left")
    creator_link(meta).pack(side="right")
    row = ttk.Frame(frame)
    row.pack(fill="x")

    def retry():
        result["retry"] = True
        root.destroy()

    def quit_app():
        result["retry"] = False
        root.destroy()

    ttk.Button(row, text="Retry", command=retry).pack(side="left",
                                                      expand=True, fill="x")
    ttk.Button(row, text="Quit", command=quit_app).pack(
        side="left", expand=True, fill="x", padx=(8, 0))
    root.protocol("WM_DELETE_WINDOW", quit_app)
    root.mainloop()
    return result["retry"]


def main():
    flags = set(sys.argv[1:])
    web = "--web" in flags or "--web-only" in flags

    panel = None
    while panel is None:
        try:
            panel = start_backend()
        except Exception as exc:
            if "--web-only" in flags:
                print("Cannot open the device: %s" % exc)
                return 1
            if not no_device_dialog(exc):
                return 1

    server = start_web() if web else None
    phone_url = ""
    if server:
        urls = core.phone_urls()
        phone_url = urls[0] if urls else "http://127.0.0.1:%d" % core.PORT
        print("Browser UI (this PC): http://127.0.0.1:%d" % core.PORT)
        print("Phone on same Wi-Fi:  %s" % phone_url)
        for extra in urls[1:]:
            print("                      %s" % extra)

    if "--web-only" in flags:
        print("Media folder: %s" % core.MEDIA_DIR)
        print("Ctrl+C to stop.")
        try:
            while True:
                threading.Event().wait(1)
        except KeyboardInterrupt:
            pass
        with core.state_lock:
            core.runtime["stop"] = True
        panel.close()
        return 0

    root = tk.Tk()
    PanelApp(root, panel, phone_url=phone_url if server else None)
    root.mainloop()
    if server:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
