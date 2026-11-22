import os
import re
import shutil
from pathlib import Path, PurePath, PurePosixPath
from dataclasses import dataclass
import zipfile
from zipfile import ZipFile
import tempfile
import threading
from typing import Callable

from PIL import UnidentifiedImageError

from . import godot, unity
from .util import has_alpha, resize_image, warn


# The maximum depth to search for image files in a directory
MAX_WALK_DEPTH = 3

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.exr', '.psd'}

DIFFUSE_KEYWORDS = {'diff', 'albedo', 'color'}
NORMAL_KEYWORDS = {'nor', 'norm', 'normal'}
ROUGHNESS_KEYWORDS = {'rough', 'roughness'}
DISPLACEMENT_KEYWORDS = {'disp', 'displacement', 'height'}
OCCLUSION_KEYWORDS = {'ao', 'occlusion', 'ambientocclusion'}
METALLIC_KEYWORDS = {'metal', 'metallic'}

# Material fields holding texture paths -> (keywords to search for, preferred label substring)
TEXTURE_SEARCH = {
    'diffuse_map': (DIFFUSE_KEYWORDS, ''),
    'normal_map': (NORMAL_KEYWORDS, 'gl'),
    'roughness_map': (ROUGHNESS_KEYWORDS, ''),
    'displacement_map': (DISPLACEMENT_KEYWORDS, ''),
    'occlusion_map': (OCCLUSION_KEYWORDS, ''),
    'metallic_map': (METALLIC_KEYWORDS, ''),
}
TEXTURE_FIELDS = tuple(TEXTURE_SEARCH)


_NAME_SEPARATORS = re.compile(r'[-_.\s]+')


def find_images(directory: str) -> list[Path]:
    """Find all images in `directory`, up to MAX_WALK_DEPTH levels deep, in a stable order."""
    base_depth = directory.rstrip(os.sep).count(os.sep)

    images: list[Path] = []
    for root, dirs, files in os.walk(directory):
        depth = root.rstrip(os.sep).count(os.sep) - base_depth
        if depth >= MAX_WALK_DEPTH - 1:
            dirs.clear()  # Don't descend any deeper
        dirs.sort()

        for file in sorted(files):
            file_path = Path(root, file).resolve()
            if file_path.suffix.lower() in IMAGE_EXTENSIONS:
                images.append(file_path)

    return images


def label_images(images: list[PurePath]) -> dict[PurePath, str]:
    """
    Label each image with the part of its file name that says which map it is, e.g.
    'worn-shiny-metal-Normal-ogl.png' -> 'normal ogl'.

    Leading words shared by every image (usually the material's name) are removed so they can't
    match keywords, e.g. 'metal' in 'worn-shiny-metal'. At least one word of each name is kept.
    """
    words = {
        image: [word for word in _NAME_SEPARATORS.split(image.stem.lower()) if word]
        for image in images
    }
    common_prefix = os.path.commonprefix(list(words.values()))
    return {
        image: ' '.join(image_words[min(len(common_prefix), len(image_words) - 1):])
        for image, image_words in words.items()
    }


def find_material_texture(labels: dict[PurePath, str], keywords: set[str],
                          prefer_has: str='') -> PurePath | None:
    """
    Find the first image whose label contains one of `keywords`. If `prefer_has` is given, an
    image whose label also contains it is preferred over other matches.
    """
    candidate_result: PurePath | None = None

    for image, label in labels.items():
        if any(kw in label for kw in keywords):
            if not prefer_has or prefer_has in label:
                return image
            if candidate_result is None:
                candidate_result = image

    return candidate_result


def detect_textures(images: list[PurePath]) -> dict[str, PurePath | None]:
    """Pick the image for each material field (see TEXTURE_SEARCH) from a material's images."""
    labels = label_images(images)
    return {
        field: find_material_texture(labels, keywords, prefer_has)
        for field, (keywords, prefer_has) in TEXTURE_SEARCH.items()
    }


@dataclass
class TextureScan:
    """Which textures a material has, found without extracting or converting it."""
    textures: dict[str, PurePath | None]  # Material field -> image (relative to a zip's root)
    # True if there is no roughness map, but the metallic map has an alpha channel that the
    # conversion uses as smoothness (Unity's layout), so the material still gets roughness
    smoothness_in_metallic: bool


def scan_textures(path: str) -> TextureScan:
    """
    Detect which textures the zip or folder at `path` has, using the same rules as a conversion.
    Zips are read without extracting them. Raises MaterialConversionError for other paths.
    """
    if zipfile.is_zipfile(path):
        with ZipFile(path) as zf:
            names = [
                PurePosixPath(name) for name in zf.namelist()
                if not name.endswith('/')
                and PurePosixPath(name).suffix.lower() in IMAGE_EXTENSIONS
                and len(PurePosixPath(name).parts) <= MAX_WALK_DEPTH
            ]
            # Same order as find_images: each folder's files, then its subfolders (top-down)
            names.sort(key=lambda name: (name.parent.parts, name.name))
            textures = detect_textures(names)

            metallic = textures['metallic_map']
            smoothness_in_metallic = False
            if textures['roughness_map'] is None and metallic is not None:
                with zf.open(str(metallic)) as f:
                    smoothness_in_metallic = has_alpha(f)
    elif os.path.isdir(path):
        textures = detect_textures(find_images(path))
        metallic = textures['metallic_map']
        smoothness_in_metallic = (textures['roughness_map'] is None and metallic is not None
                                  and has_alpha(metallic))
    else:
        raise MaterialConversionError('Path is not a zip or directory')

    return TextureScan(textures, smoothness_in_metallic)


class MaterialConversionError(Exception):
    pass


class ConversionCancelled(Exception):
    """Raised inside a conversion when its cancel event is set."""

@dataclass
class Material:
    name: str
    diffuse_map: Path | None
    normal_map: Path | None
    roughness_map: Path | None
    displacement_map: Path | None
    occlusion_map: Path | None
    metallic_map: Path | None

    temp_dir: Path | None
    resize_dir: Path | None = None  # Temporary folder holding resized textures
    cancel: threading.Event | None = None  # When set, the conversion stops at the next texture

    @staticmethod
    def from_path(path: str) -> 'Material':
        if zipfile.is_zipfile(path):
            return Material.from_zip(path)
        elif os.path.isdir(path):
            return Material.from_dir(path)
        else:
            raise MaterialConversionError('Path is not a zip or directory')

    @staticmethod
    def from_zip(zip_path: str) -> 'Material':
        extract_path = tempfile.mkdtemp()
        with ZipFile(zip_path) as zf:
            zf.extractall(extract_path)

        return Material.from_dir(extract_path, name=Path(zip_path).stem, is_temp=True)

    @staticmethod
    def from_dir(directory: str, name: str | None=None, is_temp: bool=False) -> 'Material':
        dir_path = Path(directory).resolve()

        return Material(
            name or dir_path.name,
            **detect_textures(find_images(directory)),
            temp_dir=dir_path if is_temp else None,
        )

    def check_cancelled(self):
        """Raise ConversionCancelled if this material's cancel event is set."""
        if self.cancel is not None and self.cancel.is_set():
            raise ConversionCancelled()

    def _export(self, directory: str, write: Callable[[Path], Path]) -> Path:
        """
        Create this material's output folder inside `directory`, then fill it with `write`.
        If the conversion is cancelled partway through, the partial folder is removed.
        """
        out_dir = Path(directory, self.name)
        try:
            out_dir.mkdir(parents=True)
        except FileExistsError:
            raise MaterialConversionError(f'Output folder already exists: {out_dir}')

        try:
            return write(out_dir)
        except ConversionCancelled:
            shutil.rmtree(out_dir, ignore_errors=True)
            raise

    def resize_textures(self, size: int, allow_upscale: bool=True, allow_downscale: bool=True):
        """
        Resize every texture so its longer side is `size` pixels. Resized copies are written to a
        temporary folder and replace this material's texture paths; the originals are untouched.
        """
        self.resize_dir = Path(tempfile.mkdtemp())

        resized: dict[Path, Path] = {}  # Source -> resized image, so shared images resize once
        for field in TEXTURE_FIELDS:
            self.check_cancelled()
            source = getattr(self, field)
            if source is None:
                continue

            if source not in resized:
                try:
                    resized[source] = resize_image(source, self.resize_dir, size, allow_upscale,
                                                   allow_downscale) or source
                except (OSError, UnidentifiedImageError):
                    warn(f'{self.name}: could not read {source.name} to resize it, so it will keep '
                         'its original size.')
                    resized[source] = source

            setattr(self, field, resized[source])

    def export_godot(self, directory: str) -> Path:
        """
        Export as a Godot StandardMaterial3D.

        Creates a new folder named after the material inside `directory` containing the .tres
        and its images. Each image gets a .import file with a pre-assigned uid, so the material
        works as soon as Godot imports the images. Returns the path to the .tres file.
        """
        return self._export(directory, self._write_godot)

    def _write_godot(self, out_dir: Path) -> Path:
        metallic_map = self.metallic_map
        roughness_map = self.roughness_map
        generated: set[Path] = set()  # Images written into out_dir instead of copied

        # A metallic map in Unity's layout (metallic in RGB, smoothness in alpha) is split into
        # the separate metallic and roughness images Godot uses. PSDs are always converted, since
        # Godot can't import them.
        if metallic_map is not None and (metallic_map.suffix.lower() == '.psd'
                                         or (roughness_map is None and has_alpha(metallic_map))):
            self.check_cancelled()
            metallic_out = out_dir / f'{self.name}_metallic.png'
            roughness_out = out_dir / f'{self.name}_roughness.png' if roughness_map is None else None
            try:
                if godot.split_metallic_smoothness(metallic_map, metallic_out, roughness_out):
                    roughness_map = roughness_out
                    generated.add(roughness_out)
                metallic_map = metallic_out
                generated.add(metallic_out)
            except (OSError, UnidentifiedImageError):
                warn(f'{self.name}: could not read {metallic_map.name}, so metallic and roughness '
                     'will not be used.')
                metallic_map = None

        slots = {
            'albedo': self.diffuse_map,
            'normal': self.normal_map,
            'roughness': roughness_map,
            'metallic': metallic_map,
            'ao': self.occlusion_map,
            'heightmap': self.displacement_map,
        }

        textures: dict[str, godot.GodotTexture] = {}
        copied: dict[Path, godot.GodotTexture] = {}  # Source image -> texture, to copy each image once
        for slot, source in slots.items():
            self.check_cancelled()
            if source is None:
                continue

            if source.suffix.lower() == '.psd':
                warn(f'{self.name}: skipping {source.name}, Godot cannot import PSD files.')
                continue

            if source not in copied:
                compression = godot.unsupported_exr_compression(source)
                if compression is not None:
                    warn(f'{self.name}: {source.name} uses {compression} compression, which Godot '
                         'cannot import, so this texture will fail to load.')

                params: dict[str, str | int] = {}
                if slot == 'normal':
                    params['compress/normal_map'] = 1
                    # Godot expects OpenGL-style normal maps, so flip DirectX ones
                    if 'dx' in source.name.lower():
                        params['process/normal_map_invert_y'] = 'true'

                if source not in generated:
                    shutil.copy2(source, out_dir / source.name)
                uid = godot.new_uid()
                godot.write_import_file(out_dir / source.name, uid, params)
                copied[source] = godot.GodotTexture(source.name, uid, params)

            textures[slot] = copied[source]

        tres_path = out_dir / f'{self.name}.tres'
        godot.write_material(tres_path, textures)
        return tres_path

    def export_unity(self, directory: str) -> Path:
        """
        Export as a Unity URP Lit material.

        Creates a new folder named after the material inside `directory` containing the .mat and
        its images. Each image gets a .meta file with a pre-assigned guid, so the material works
        as soon as Unity imports the images. Returns the path to the .mat file.
        """
        return self._export(directory, self._write_unity)

    def _write_unity(self, out_dir: Path) -> Path:
        textures: dict[str, str] = {}  # URP slot -> texture guid
        guids: dict[Path, str] = {}  # Image -> guid, so an image used in several slots is written once
        settings = unity.MaterialSettings()

        def add_texture(slot: str, image: Path, copy: bool=True, **meta_settings):
            self.check_cancelled()
            if image not in guids:
                if copy:
                    shutil.copy2(image, out_dir / image.name)
                guids[image] = unity.new_guid()
                unity.write_texture_meta(out_dir / image.name, guids[image], **meta_settings)
            textures[slot] = guids[image]

        if self.diffuse_map is not None:
            add_texture('_BaseMap', self.diffuse_map)
        if self.normal_map is not None:
            # Unity expects OpenGL-style normal maps, so flip DirectX ones
            add_texture('_BumpMap', self.normal_map, normal_map=True, srgb=False,
                        flip_green='dx' in self.normal_map.name.lower())
        if self.occlusion_map is not None:
            add_texture('_OcclusionMap', self.occlusion_map, srgb=False)
        if self.displacement_map is not None:
            add_texture('_ParallaxMap', self.displacement_map, srgb=False)

        # URP Lit has no roughness slot: it reads metallic from RGB and smoothness from alpha
        # of a single texture, so separate metallic and roughness maps get packed together
        packed = False
        if self.roughness_map is not None:
            self.check_cancelled()
            packed_path = out_dir / f'{self.name}_metallic_smoothness.png'
            try:
                unity.pack_metallic_smoothness(self.metallic_map, self.roughness_map, packed_path)
                add_texture('_MetallicGlossMap', packed_path, copy=False, srgb=False)
                packed = True
            except (OSError, UnidentifiedImageError):
                packed_path.unlink(missing_ok=True)
                sources = [m.name for m in (self.metallic_map, self.roughness_map) if m is not None]
                warn(f'{self.name}: could not read {" / ".join(sources)} to make a metallic/smoothness '
                     'map, so roughness will not be used.')

        if not packed and self.metallic_map is not None:
            # Assume a metallic map with alpha already has smoothness in it (Unity's layout)
            has_smoothness = has_alpha(self.metallic_map)
            add_texture('_MetallicGlossMap', self.metallic_map, srgb=False)
            if not has_smoothness:
                settings.smoothness = unity.DEFAULT_SMOOTHNESS  # Multiplies the opaque alpha
        elif not packed:
            settings.smoothness = unity.DEFAULT_SMOOTHNESS

        mat_path = out_dir / f'{self.name}.mat'
        unity.write_material(mat_path, textures, settings)
        return mat_path

    def cleanup(self):
        for temp_dir in (self.temp_dir, self.resize_dir):
            if temp_dir is not None:
                shutil.rmtree(temp_dir)