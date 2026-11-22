"""Godot-specific helpers for writing StandardMaterial3D resources."""

import secrets
import struct
from dataclasses import dataclass
from pathlib import Path


# Godot's ResourceUID text encoding: base 34 using a-y and 0-8 (no 'z' or '9')
_UID_CHARS = 'abcdefghijklmnopqrstuvwxy012345678'


def new_uid() -> str:
    """Create a new random Godot resource uid, e.g. 'uid://c2e2rxlry7e3j'."""
    value = secrets.randbits(63)  # Godot uids are non-negative 64-bit ints
    text = ''
    while True:
        text = _UID_CHARS[value % len(_UID_CHARS)] + text
        value //= len(_UID_CHARS)
        if value == 0:
            return 'uid://' + text


# EXR compression types (from the OpenEXR spec) that Godot's EXR importer can't decode
_UNSUPPORTED_EXR_COMPRESSION = {8: 'DWAA', 9: 'DWAB'}


def unsupported_exr_compression(image_path: Path) -> str | None:
    """
    Return the compression name if `image_path` is an EXR that Godot can't import, else None.

    Reads only the EXR header. Returns None for non-EXR files or headers that can't be parsed.
    """
    try:
        with open(image_path, 'rb') as f:
            if f.read(4) != b'\x76\x2f\x31\x01':  # EXR magic number
                return None
            f.read(4)  # Version and flags

            # Header is a list of (name\0, type\0, int32 size, value) ending with an empty name
            while True:
                name = _read_null_terminated(f)
                if not name:
                    return None
                _read_null_terminated(f)  # Attribute type
                size = struct.unpack('<i', f.read(4))[0]
                value = f.read(size)
                if name == b'compression':
                    return _UNSUPPORTED_EXR_COMPRESSION.get(value[0])
    except (OSError, struct.error, IndexError):
        return None


def _read_null_terminated(f, max_length: int=256) -> bytes:
    data = b''
    while len(data) < max_length:
        char = f.read(1)
        if char in (b'', b'\0'):
            break
        data += char
    return data


@dataclass
class GodotTexture:
    """A texture assigned to a StandardMaterial3D property."""
    file_name: str  # Image file name, relative to the .tres file
    uid: str
    import_params: dict[str, str | int]


def write_import_file(image_path: Path, uid: str, params: dict[str, str | int]) -> None:
    """
    Write a minimal .import file next to an image.

    Godot keeps the uid given here when it imports the image, so the material can reference
    the texture before Godot has ever seen it. Any params not given use Godot's defaults, and
    Godot fills in the rest of the file when it imports.
    """
    lines = [
        '[remap]',
        '',
        'importer="texture"',
        'type="CompressedTexture2D"',
        f'uid="{uid}"',
        '',
        '[params]',
        '',
    ]
    lines += [f'{key}={value}' for key, value in params.items()]

    image_path.with_name(image_path.name + '.import').write_text('\n'.join(lines) + '\n')


def write_material(tres_path: Path, textures: dict[str, GodotTexture]) -> None:
    """
    Write a StandardMaterial3D .tres file.

    `textures` maps a material slot ('albedo', 'normal', 'roughness', 'metallic', 'ao',
    'heightmap') to its texture. Texture paths are written relative to the .tres file, so the
    material folder can be placed anywhere in a project.
    """
    lines = [f'[gd_resource type="StandardMaterial3D" format=3 uid="{new_uid()}"]', '']

    # Several slots may share one image, so only declare each image once
    resource_ids: dict[str, str] = {}
    for texture in textures.values():
        if texture.file_name not in resource_ids:
            resource_id = str(len(resource_ids) + 1)
            resource_ids[texture.file_name] = resource_id
            lines.append(
                f'[ext_resource type="Texture2D" uid="{texture.uid}" '
                f'path="{texture.file_name}" id="{resource_id}"]'
            )

    lines += ['', '[resource]']
    for slot, texture in textures.items():
        # These slots are disabled by default and must be switched on
        if slot in ('normal', 'ao', 'heightmap'):
            lines.append(f'{slot}_enabled = true')
        # metallic defaults to 0, which would multiply the texture out entirely
        if slot == 'metallic':
            lines.append('metallic = 1.0')
        lines.append(f'{slot}_texture = ExtResource("{resource_ids[texture.file_name]}")')

    tres_path.write_text('\n'.join(lines) + '\n')
