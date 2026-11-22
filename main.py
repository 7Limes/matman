from typing import Literal
import argparse
import time
import threading
from pathlib import Path
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor

from material import Material, MaterialConversionError, ConversionCancelled


@dataclass
class ResizeOptions:
    size: int  # Target length of each texture's longer side
    allow_upscale: bool = True
    allow_downscale: bool = True


def convert_material(file: str, platform: Literal['godot', 'unity'], output_dir: str,
                     resize: ResizeOptions | None=None,
                     cancel: threading.Event | None=None) -> Path:
    """
    Load and export a single material, returning the path of the exported material file.

    Raises MaterialConversionError if the material can't be converted, or ConversionCancelled
    if `cancel` is set partway through (any partial output is removed).
    """
    if cancel is not None and cancel.is_set():
        raise ConversionCancelled()

    mat = Material.from_path(file)
    mat.cancel = cancel
    try:
        if resize is not None:
            mat.resize_textures(resize.size, resize.allow_upscale, resize.allow_downscale)

        if platform == 'godot':
            return mat.export_godot(output_dir)
        return mat.export_unity(output_dir)
    finally:
        mat.cleanup()


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


def main():
    parser = argparse.ArgumentParser('mattra', description='Material transformer for game development')

    parser.add_argument('files', nargs='+', help='The files to convert')
    parser.add_argument('--platform', '-p', choices=['godot', 'unity'], required=True, help='The target platform')
    parser.add_argument('--dir', '-d', default='.', help='The directory to output to')
    parser.add_argument('--threaded', '-t', action='store_true', help='Convert each material in a separate thread')

    parser.add_argument('--size', '-s', type=int, help='Resize textures so their longer side is SIZE pixels')
    parser.add_argument('--no-upscale', action='store_true', help="With --size, don't enlarge textures smaller than SIZE")
    parser.add_argument('--no-downscale', action='store_true', help="With --size, don't shrink textures larger than SIZE")

    args = parser.parse_args()

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


if __name__ == '__main__':
    main()