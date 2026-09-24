#!/usr/bin/env python3
"""Stamp APP_VERSION and version_info.txt from a release tag.

    python tools/stamp_version.py 1.4.0
    python tools/stamp_version.py v1.4.0

Used by the release workflow so Windows file Properties and the in-app
title cannot drift apart the way they did when version_info stayed at 1.0.0.
"""
from __future__ import annotations

import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def parse_version(raw):
    text = (raw or "").strip()
    if text.lower().startswith("v"):
        text = text[1:]
    parts = text.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise SystemExit("need major.minor.patch, got %r" % raw)
    major, minor, patch = (int(p) for p in parts)
    return major, minor, patch, "%d.%d.%d" % (major, minor, patch)


def stamp_app(path, version):
    with open(path, "r", encoding="utf-8") as fh:
        src = fh.read()
    updated, n = re.subn(
        r'^APP_VERSION = "[^"]*"',
        'APP_VERSION = "%s"' % version,
        src,
        count=1,
        flags=re.M,
    )
    if n != 1:
        raise SystemExit("APP_VERSION assignment not found in %s" % path)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(updated)


def stamp_version_info(path, major, minor, patch, version):
    # PyInstaller wants a 4-tuple; the fourth field stays 0.
    file_ver = "%d.%d.%d.0" % (major, minor, patch)
    text = """# UTF-8
# Windows file properties for CleanD92.exe. Keep the two version tuples and
# the two string versions in step when tagging a release. On tagged builds CI
# runs tools/stamp_version.py so this file matches the git tag.
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=(%d, %d, %d, 0),
    prodvers=(%d, %d, %d, 0),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [StringStruct('CompanyName', 'nmx88'),
         StringStruct('FileDescription', 'CleanD92 - driver-free control app for the StreamDock D92 panel'),
         StringStruct('FileVersion', '%s'),
         StringStruct('InternalName', 'CleanD92'),
         StringStruct('LegalCopyright', 'MIT Licence'),
         StringStruct('OriginalFilename', 'CleanD92.exe'),
         StringStruct('ProductName', 'CleanD92'),
         StringStruct('ProductVersion', '%s')])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""" % (major, minor, patch, major, minor, patch, file_ver, file_ver)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("version", help="1.4.0 or v1.4.0")
    args = parser.parse_args()
    major, minor, patch, version = parse_version(args.version)
    stamp_app(os.path.join(HERE, "d92_app.py"), version)
    stamp_version_info(os.path.join(HERE, "version_info.txt"),
                       major, minor, patch, version)
    print("stamped %s" % version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
