"""The command line interface: `matman convert ...`."""

import argparse
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

from .convert import ResizeOptions, convert_material
from .material import MaterialConversionError


def _convert_and_report(file: str, platform: Literal['godot', 'unity'], output_dir: str,
                        resize: ResizeOptions | None=None):
    """Convert a material, printing the result for the CLI."""
    try:
        print(f'Exported {convert_material(file, platform, output_dir, resize)}')
    except MaterialConversionError as e:
        print(f'Failed to convert {file}: {e}')


def convert_materials(files: list[str], platform: Literal['godot', 'unity'], output_dir: str,
                      threaded: bool=False, resize: ResizeOptions | None=None):
    if not threaded:
        for file in files:
            _convert_and_report(file, platform, output_dir, resize)
        return

    # One thread per material
    with ThreadPoolExecutor(max_workers=len(files)) as executor:
        futures = [executor.submit(_convert_and_report, file, platform, output_dir, resize) for file in files]
        for future in futures:
            future.result()  # Re-raise any unexpected exception from the thread


def main(argv: list[str] | None=None):
    """Run `matman convert` with `argv` (the arguments after 'convert')."""
    parser = argparse.ArgumentParser(
        'matman convert', description='Convert materials into Godot or Unity materials.')

    parser.add_argument('files', nargs='+', help='Zip files or folders of materials to convert')
    parser.add_argument('--platform', '-p', choices=['godot', 'unity'], required=True, help='The target platform')
    parser.add_argument('--dir', '-d', default='.', help='The directory to output to')
    parser.add_argument('--threaded', '-t', action='store_true', help='Convert each material in a separate thread')

    parser.add_argument('--size', '-s', type=int, help='Resize textures so their longer side is SIZE pixels')
    parser.add_argument('--no-upscale', action='store_true', help="With --size, don't enlarge textures smaller than SIZE")
    parser.add_argument('--no-downscale', action='store_true', help="With --size, don't shrink textures larger than SIZE")

    args = parser.parse_args(argv)

    resize = None
    if args.size is not None:
        if args.size <= 0:
            parser.error('--size must be a positive number')
        if args.no_upscale and args.no_downscale:
            parser.error('--no-upscale and --no-downscale together would leave every texture unchanged')
        resize = ResizeOptions(args.size, not args.no_upscale, not args.no_downscale)
    elif args.no_upscale or args.no_downscale:
        parser.error('--no-upscale and --no-downscale require --size')

    start = time.perf_counter()
    convert_materials(args.files, args.platform, args.dir, args.threaded, resize)
    print(f'Conversion completed in {time.perf_counter() - start:.2f}s')
