import sys
from pathlib import Path

from PIL import Image, UnidentifiedImageError


def warn(message: str):
    """Print a warning to stderr in yellow."""
    print(f'\033[33mWarning: {message}\033[0m', file=sys.stderr)


def has_alpha(path: Path) -> bool:
    """Whether the image has an alpha channel. False if the image can't be read."""
    try:
        with Image.open(path) as image:
            return 'A' in image.getbands()
    except (OSError, UnidentifiedImageError):
        return False


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
