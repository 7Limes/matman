"""
The entry point, run by `python -m matman` (or a packaged executable):

    matman                       open the app
    matman FILE...               open the app with these zip files or folders in the list
    matman convert [options]     convert materials from the command line
"""

import sys
from pathlib import Path

# Run as a plain script (e.g. as a packaging tool's entry point) there's no parent package, so
# make the folder containing the package importable. Imports below are absolute for the same reason.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from matman import __version__

USAGE = f"""matman {__version__}: a material manager for game development

usage:
  matman                       open the app
  matman FILE...               open the app with these zip files or folders in the list
  matman convert [options]     convert materials from the command line
                               (see `matman convert --help`)
"""


def _attach_windows_console():
    """
    The Windows build is a windowed app so the GUI doesn't open a console window, which leaves
    it nowhere to print. When run from a terminal, print to that terminal instead.
    """
    if sys.platform != 'win32' or not getattr(sys, 'frozen', False):
        return
    import ctypes
    ATTACH_PARENT_PROCESS = -1
    if ctypes.windll.kernel32.AttachConsole(ATTACH_PARENT_PROCESS):
        sys.stdout = open('CONOUT$', 'w', buffering=1)
        sys.stderr = open('CONOUT$', 'w', buffering=1)
        print()  # The terminal has already printed its next prompt; start on a fresh line


def main(argv: list[str] | None=None) -> int:
    args = sys.argv[1:] if argv is None else argv

    if args and args[0] in ('-h', '--help', '--version', 'convert'):
        _attach_windows_console()

    if args and args[0] in ('-h', '--help'):
        print(USAGE)
        return 0
    if args and args[0] == '--version':
        print(f'matman {__version__}')
        return 0
    if args and args[0] == 'convert':
        from matman import cli
        cli.main(args[1:])
        return 0

    # The GUI is only imported when needed, so the command line doesn't have to load Qt
    from matman import gui
    return gui.main(args)


if __name__ == '__main__':
    sys.exit(main())
