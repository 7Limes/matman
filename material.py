import os
import shutil
from pathlib import Path
from dataclasses import dataclass
import zipfile
from zipfile import ZipFile
import tempfile

from PIL import UnidentifiedImageError

import godot
import unity
from util import warn


# The maximum depth to search for image files in a directory
MAX_WALK_DEPTH = 3

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.exr', '.psd'}

DIFFUSE_KEYWORDS = {'diff', 'albedo', 'color'}
NORMAL_KEYWORDS = {'nor', 'norm', 'normal'}
ROUGHNESS_KEYWORDS = {'rough', 'roughness'}
DISPLACEMENT_KEYWORDS = {'disp', 'displacement', 'height'}
OCCLUSION_KEYWORDS = {'ao', 'occlusion', 'ambientocclusion'}
METALLIC_KEYWORDS = {'metal', 'metallic'}


def find_material_texture(directory: str, keywords: set[str], prefer_has: str='') -> Path | None:
    base_depth = directory.rstrip(os.sep).count(os.sep)

    candidate_result: Path | None = None

    for root, _, files in os.walk(directory):
        depth = root.rstrip(os.sep).count(os.sep) - base_depth
        if depth >= MAX_WALK_DEPTH:
            break

        for file in files:
            file_path = Path(root, file).resolve()
            if file_path.suffix.lower() not in IMAGE_EXTENSIONS:
                # Not an image
                continue

            file_path_str = str(file_path).lower()
            if any(kw in file_path_str for kw in keywords):
                if prefer_has:
                    if prefer_has in file_path_str:
                        return file_path
                    else:
                        candidate_result = file_path
                else:
                    return file_path
    
    if prefer_has:
        return candidate_result

    return None

class MaterialConversionError(Exception):
    pass

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
            find_material_texture(directory, DIFFUSE_KEYWORDS),
            find_material_texture(directory, NORMAL_KEYWORDS, prefer_has='gl'),
            find_material_texture(directory, ROUGHNESS_KEYWORDS),
            find_material_texture(directory, DISPLACEMENT_KEYWORDS),
            find_material_texture(directory, OCCLUSION_KEYWORDS),
            find_material_texture(directory, METALLIC_KEYWORDS),

            temp_dir = dir_path if is_temp else None
        )

    def export_godot(self, directory: str) -> Path:
        """
        Export as a Godot StandardMaterial3D.

        Creates a new folder named after the material inside `directory` containing the .tres
        and its images. Each image gets a .import file with a pre-assigned uid, so the material
        works as soon as Godot imports the images. Returns the path to the .tres file.
        """
        out_dir = Path(directory, self.name)
        try:
            out_dir.mkdir(parents=True)
        except FileExistsError:
            raise MaterialConversionError(f'Output folder already exists: {out_dir}')

        slots = {
            'albedo': self.diffuse_map,
            'normal': self.normal_map,
            'roughness': self.roughness_map,
            'metallic': self.metallic_map,
            'ao': self.occlusion_map,
            'heightmap': self.displacement_map,
        }

        textures: dict[str, godot.GodotTexture] = {}
        copied: dict[Path, godot.GodotTexture] = {}  # Source image -> texture, to copy each image once
        for slot, source in slots.items():
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
        out_dir = Path(directory, self.name)
        try:
            out_dir.mkdir(parents=True)
        except FileExistsError:
            raise MaterialConversionError(f'Output folder already exists: {out_dir}')

        textures: dict[str, str] = {}  # URP slot -> texture guid
        guids: dict[Path, str] = {}  # Image -> guid, so an image used in several slots is written once
        settings = unity.MaterialSettings()

        def add_texture(slot: str, image: Path, copy: bool=True, **meta_settings):
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
            try:
                has_smoothness = unity.has_alpha(self.metallic_map)
            except (OSError, UnidentifiedImageError):
                has_smoothness = False
            add_texture('_MetallicGlossMap', self.metallic_map, srgb=False)
            if not has_smoothness:
                settings.smoothness = unity.DEFAULT_SMOOTHNESS  # Multiplies the opaque alpha
        elif not packed:
            settings.smoothness = unity.DEFAULT_SMOOTHNESS

        mat_path = out_dir / f'{self.name}.mat'
        unity.write_material(mat_path, textures, settings)
        return mat_path

    def cleanup(self):
        if self.temp_dir is not None:
            shutil.rmtree(self.temp_dir)