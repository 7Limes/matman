import os
import struct
import sys
from typing import IO, Callable
from pathlib import Path

from PIL import Image, UnidentifiedImageError


_warning_handler: Callable[[str], None] | None = None


def set_warning_handler(handler: Callable[[str], None] | None):
    """
    Send warnings to `handler` instead of printing them, e.g. to show them in a GUI.
    The handler may be called from conversion threads. Pass None to print again.
    """
    global _warning_handler
    _warning_handler = handler


def warn(message: str):
    """Print a warning to stderr in yellow, or pass it to the handler set with set_warning_handler."""
    if _warning_handler is not None:
        _warning_handler(message)
    else:
        print(f'\033[33mWarning: {message}\033[0m', file=sys.stderr)


def has_alpha(source: Path | IO[bytes]) -> bool:
    """
    Whether the image (a path, or an open seekable file) has an alpha channel, as Pillow would
    read it. False if it can't be read.
    """
    try:
        if isinstance(source, (str, os.PathLike)):
            with open(source, 'rb') as f:
                return _has_alpha(f)
        return _has_alpha(source)
    except (OSError, UnidentifiedImageError, struct.error):
        return False


def _has_alpha(f: IO[bytes]) -> bool:
    header = f.read(26)
    if header[:4] == b'8BPS':
        # Read PSDs' headers directly: Pillow parses the whole layer structure in Python to open
        # one, which is slow and holds the GIL, stalling the GUI while textures are scanned
        channels, _height, _width, depth, color_mode = struct.unpack('>HIIHH', header[12:26])
        # The same rule Pillow uses: only an 8-bit RGB PSD with a 4th channel opens as RGBA
        return color_mode == 3 and depth == 8 and channels == 4

    f.seek(0)
    with Image.open(f) as image:
        return 'A' in image.getbands()


def resize_image(source: Path, dest_dir: Path, size: int, allow_upscale: bool=True,
                 allow_downscale: bool=True) -> Path | None:
    """
    Resize an image so its longer side is `size` pixels, keeping its aspect ratio, and save it
    in `dest_dir` with the same file name. PSDs are saved as PNGs, since Pillow can't write PSDs.

    Returns the resized image's path, or None if the image was left alone because it is
    already the right size or the resize direction isn't allowed. Raises OSError or
    UnidentifiedImageError if the image can't be read.
    """
    with Image.open(source) as image:
        longest = max(image.size)
        if (longest == size
                or (longest < size and not allow_upscale)
                or (longest > size and not allow_downscale)):
            return None

        # Resampling needs a plain color mode
        if image.mode in ('P', 'PA'):
            image = image.convert('RGBA' if 'transparency' in image.info or image.mode == 'PA' else 'RGB')
        elif image.mode == 'CMYK':
            image = image.convert('RGB')

        scale = size / longest
        new_size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
        resized = image.resize(new_size, Image.Resampling.LANCZOS)
        icc_profile = image.info.get('icc_profile')

    suffix = '.png' if source.suffix.lower() == '.psd' else source.suffix
    dest = dest_dir / (source.stem + suffix)

    save_options = {'icc_profile': icc_profile} if icc_profile else {}
    if suffix.lower() in ('.jpg', '.jpeg'):
        save_options['quality'] = 95
    resized.save(dest, **save_options)
    return dest
