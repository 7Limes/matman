"""Unity-specific helpers for writing URP Lit materials."""

import uuid
from dataclasses import dataclass
from pathlib import Path

from PIL import Image


# Shader reference for 'Universal Render Pipeline/Lit' (the same in Unity 6000.0 and 6000.3)
URP_LIT_SHADER = '{fileID: 4800000, guid: 933532a4fcc9baf4fa0491de14d08ed7, type: 3}'

# Default smoothness when no smoothness data is available
DEFAULT_SMOOTHNESS = 0.5

# Shader keywords URP Lit needs for each texture slot to have any effect.
# Unity does not add these itself when importing a material.
_SLOT_KEYWORDS = {
    '_BumpMap': '_NORMALMAP',
    '_MetallicGlossMap': '_METALLICSPECGLOSSMAP',
    '_OcclusionMap': '_OCCLUSIONMAP',
    '_ParallaxMap': '_PARALLAXMAP',
}


def new_guid() -> str:
    """Create a new random Unity asset guid (32 hex characters)."""
    return uuid.uuid4().hex


def write_texture_meta(image_path: Path, guid: str, normal_map: bool=False, srgb: bool=True,
                       flip_green: bool=False) -> None:
    """
    Write a minimal .meta file next to an image.

    Unity keeps the guid given here when it imports the image, so the material can reference the
    texture before Unity has seen it. Settings not given use Unity's defaults.
    """
    # serializedVersion must be present, or Unity treats the file as an old format and
    # misreads the other settings
    image_path.with_name(image_path.name + '.meta').write_text(
        'fileFormatVersion: 2\n'
        f'guid: {guid}\n'
        'TextureImporter:\n'
        '  serializedVersion: 13\n'
        '  mipmaps:\n'
        f'    sRGBTexture: {int(srgb)}\n'
        '  bumpmap:\n'
        f'    flipGreenChannel: {int(flip_green)}\n'
        f'  textureType: {1 if normal_map else 0}\n'
    )


def _load_grayscale(path: Path) -> Image.Image:
    """Load an image as 8-bit grayscale, scaling 16/32-bit images down instead of clipping."""
    image = Image.open(path)
    if image.mode in ('I', 'I;16', 'I;16B', 'I;16L'):
        image = image.convert('I').point(lambda v: v * (1 / 256))
    elif image.mode == 'F':
        image = image.point(lambda v: v * 255)
    return image.convert('L')


def pack_metallic_smoothness(metallic: Path | None, roughness: Path, out_path: Path) -> None:
    """
    Pack metallic (RGB) and smoothness (alpha, 1 - roughness) into one PNG, which is the
    layout URP Lit expects for _MetallicGlossMap. With no metallic map, metallic is 0.
    """
    rough = _load_grayscale(roughness)
    if metallic is not None:
        metal = _load_grayscale(metallic)
        rough = rough.resize(metal.size)
    else:
        metal = Image.new('L', rough.size, 0)

    smooth = rough.point(lambda v: 255 - v)
    Image.merge('RGBA', (metal, metal, metal, smooth)).save(out_path)


@dataclass
class MaterialSettings:
    smoothness: float = 1.0  # Multiplies the smoothness in _MetallicGlossMap's alpha, if present
    metallic: float = 0.0  # Only used when there is no _MetallicGlossMap
    parallax: float = 0.005  # Height map strength (URP's default)


def write_material(mat_path: Path, textures: dict[str, str], settings: MaterialSettings) -> None:
    """
    Write a URP Lit .mat file and its .meta.

    `textures` maps a URP Lit texture slot ('_BaseMap', '_BumpMap', '_MetallicGlossMap',
    '_OcclusionMap', '_ParallaxMap') to the guid of its texture.
    """
    keywords = sorted(_SLOT_KEYWORDS[slot] for slot in textures if slot in _SLOT_KEYWORDS)
    keyword_lines = ''.join(f'\n  - {keyword}' for keyword in keywords) if keywords else ' []'

    texture_lines = ''.join(
        f'    - {slot}:\n'
        f'        m_Texture: {{fileID: 2800000, guid: {guid}, type: 3}}\n'
        '        m_Scale: {x: 1, y: 1}\n'
        '        m_Offset: {x: 0, y: 0}\n'
        for slot, guid in textures.items()
    )

    mat_path.write_text(
        '%YAML 1.1\n'
        '%TAG !u! tag:unity3d.com,2011:\n'
        '--- !u!21 &2100000\n'
        'Material:\n'
        '  serializedVersion: 8\n'
        f'  m_Name: {mat_path.stem}\n'
        f'  m_Shader: {URP_LIT_SHADER}\n'
        f'  m_ValidKeywords:{keyword_lines}\n'
        '  m_InvalidKeywords: []\n'
        '  m_SavedProperties:\n'
        '    serializedVersion: 3\n'
        '    m_TexEnvs:\n'
        f'{texture_lines}'
        '    m_Ints: []\n'
        '    m_Floats:\n'
        '    - _WorkflowMode: 1\n'  # Metallic workflow
        f'    - _Smoothness: {settings.smoothness}\n'
        '    - _SmoothnessTextureChannel: 0\n'  # Smoothness from _MetallicGlossMap alpha
        f'    - _Metallic: {settings.metallic}\n'
        '    - _OcclusionStrength: 1\n'
        f'    - _Parallax: {settings.parallax}\n'
        '    - _BumpScale: 1\n'
        '    m_Colors:\n'
        '    - _BaseColor: {r: 1, g: 1, b: 1, a: 1}\n'
    )

    mat_path.with_name(mat_path.name + '.meta').write_text(
        'fileFormatVersion: 2\n'
        f'guid: {new_guid()}\n'
        'NativeFormatImporter:\n'
        '  mainObjectFileID: 2100000\n'
    )
