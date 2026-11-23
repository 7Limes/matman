# PyInstaller build for matman: a single executable file on Windows and Linux, and matman.app
# on macOS.
#
# Build in a separate virtual environment holding only what the app needs, since anything
# installed there can end up in the executable:
#
#     python -m venv .venv-build
#     .venv-build/bin/pip install -r requirements-build.txt      (Windows: .venv-build\Scripts\pip)
#     .venv-build/bin/pyinstaller matman.spec
#
# The result is written to dist/. PyInstaller builds for the system it runs on, so build on
# Windows for a Windows .exe, on macOS for a .app, and on Linux for a Linux executable.
# .github/workflows/build.yml builds all of them on GitHub Actions.
#
# On Linux, Python's shared library must be installed (e.g. `apt install libpython3.13` for a
# deadsnakes Python 3.13); the system's own python3 usually has it already.

import sys

a = Analysis(
    ['matman/__main__.py'],
    pathex=['.'],
    datas=[('assets', 'assets')],  # Found through sys._MEIPASS at runtime (see gui.py)
)
pyz = PYZ(a.pure)

if sys.platform == 'darwin':
    # A .app is already a single item to the user, so its contents stay unpacked: that starts
    # faster, and PyInstaller is phasing out single-file executables inside .app bundles
    exe = EXE(
        pyz,
        a.scripts,
        exclude_binaries=True,
        name='matman',
        console=False,
        upx=False,
    )
    coll = COLLECT(exe, a.binaries, a.datas, name='matman', upx=False)
    app = BUNDLE(
        coll,
        name='matman.app',
        bundle_identifier='io.github.matman',
        info_plist={'NSHighResolutionCapable': True},
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        name='matman',
        # The GUI is the default, so don't open a console window with it on Windows. The
        # command line attaches to the terminal it was started from instead (see __main__.py).
        console=False,
        upx=False,
    )
