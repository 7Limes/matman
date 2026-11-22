"""
Checks that a built matman executable works, by converting a small generated material for both
platforms:

    python ci/smoke_test.py PATH_TO_EXECUTABLE

Needs Pillow. Results are checked through the files written rather than printed output, since
the Windows build is a windowed app whose output can't be captured.
"""

import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image


def run(exe: str, *args: str):
    result = subprocess.run([exe, *args], capture_output=True, text=True, timeout=180)
    if result.returncode != 0:
        print(result.stdout, result.stderr, sep='\n')
        sys.exit(f'`matman {" ".join(args)}` failed with exit code {result.returncode}')


def make_material(folder: Path):
    folder.mkdir()
    Image.new('RGB', (64, 64), (180, 120, 80)).save(folder / 'smoke_albedo.png')
    Image.new('RGB', (64, 64), (128, 128, 255)).save(folder / 'smoke_normal-gl.png')
    Image.new('L', (64, 64), 200).save(folder / 'smoke_roughness.png')
    Image.new('L', (64, 64), 0).save(folder / 'smoke_metallic.png')


def main():
    exe = str(Path(sys.argv[1]).resolve())
    run(exe, '--version')

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        material = tmp / 'smoke-test'
        make_material(material)

        # (platform, material file, an image the conversion creates with Pillow)
        checks = [('godot', 'smoke-test.tres', 'smoke_albedo.png'),
                  ('unity', 'smoke-test.mat', 'smoke-test_metallic_smoothness.png')]
        for platform, material_file, image in checks:
            out = tmp / f'out-{platform}' / 'smoke-test'
            run(exe, 'convert', '-p', platform, '-s', '32', '-d', str(out.parent), str(material))

            if not (out / material_file).is_file():
                sys.exit(f'{platform}: {material_file} was not written')
            with Image.open(out / image) as img:
                if img.size != (32, 32):
                    sys.exit(f'{platform}: {image} is {img.size}, expected it resized to 32x32')
            print(f'{platform}: ok')


if __name__ == '__main__':
    main()
