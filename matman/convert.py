"""Converting a single material: the core shared by the GUI and the command line."""

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .material import ConversionCancelled, Material


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
