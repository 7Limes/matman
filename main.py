from typing import Literal
import argparse
import time
from concurrent.futures import ThreadPoolExecutor

from material import Material, MaterialConversionError


def convert_material(file: str, platform: Literal['godot', 'unity'], output_dir: str):
    """Load and export a single material."""
    try:
        mat = Material.from_path(file)
    except MaterialConversionError:
        return

    try:
        if platform == 'godot':
            print(f'Exported {mat.export_godot(output_dir)}')
    except MaterialConversionError as e:
        print(f'Failed to export {mat.name}: {e}')
    finally:
        mat.cleanup()


def convert_materials(files: list[str], platform: Literal['godot', 'unity'], output_dir: str,
                      threaded: bool=False):
    if not threaded:
        for file in files:
            convert_material(file, platform, output_dir)
        return

    # One thread per material
    with ThreadPoolExecutor(max_workers=len(files)) as executor:
        futures = [executor.submit(convert_material, file, platform, output_dir) for file in files]
        for future in futures:
            future.result()  # Re-raise any unexpected exception from the thread


def main():
    parser = argparse.ArgumentParser('mattra', description='Material transformer for game development')

    parser.add_argument('files', nargs='+', help='The files to convert')
    parser.add_argument('--platform', '-p', choices=['godot', 'unity'], required=True, help='The target platform')
    parser.add_argument('--dir', '-d', default='.', help='The directory to output to')
    parser.add_argument('--threaded', '-t', action='store_true', help='Convert each material in a separate thread')

    args = parser.parse_args()

    start = time.perf_counter()
    convert_materials(args.files, args.platform, args.dir, args.threaded)
    print(f'Conversion completed in {time.perf_counter() - start:.2f}s')


if __name__ == '__main__':
    main()