# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for CleanD92.

    pip install -r requirements-build.txt
    pyinstaller d92.spec

Produces dist/CleanD92.exe -- one file, no console window, nothing to
install. The media/ and presets/ folders are created next to the exe at
runtime, which is why d92_panel.py resolves them against sys.executable
rather than __file__: under one-file mode __file__ points inside the
temporary unpack directory, which is wiped on exit.
"""


analysis = Analysis(
    ["d92_app.py"],
    pathex=[],
    # hidapi ships as a single compiled extension, not a package, so there is
    # nothing to collect by hand: PyInstaller's binary dependency scan picks
    # up whatever DLLs the .pyd links against.
    binaries=[],
    # icon.ico is the window/exe icon; also shipped as data so the tray can
    # load it when frozen (the EXE version resource is not a filesystem file).
    datas=[
        ("icon.ico", "."),
        # GSMTC helper (stock PowerShell). Unpacked under _MEIPASS/tools/
        # when frozen -- see d92_panel._nowplaying_script.
        ("tools/nowplaying.ps1", "tools"),
    ],
    hiddenimports=[
        "hid",
        "psutil",
        "PIL.ImageTk",          # pulled in dynamically by the preview canvas
        "windnd",               # Windows drag-and-drop onto the preview
        "tzdata",               # zoneinfo database on Windows
        "pystray",
        "pystray._win32",       # backend selected at import time
        "tkinter",
        "tkinter.ttk",
        "tkinter.colorchooser",
        "tkinter.simpledialog",
        "tkinter.messagebox",
        "tkinter.filedialog",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Nothing here imports these; excluding them keeps the download small.
        "numpy",
        "scipy",
        "pandas",
        "matplotlib",
        "pytest",
        "setuptools",
        "PIL.ImageQt",
        "PyQt5",
        "PySide2",
        "unittest",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="CleanD92",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                  # UPX trips antivirus heuristics far too often
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,              # no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="icon.ico",
    version="version_info.txt",
)
