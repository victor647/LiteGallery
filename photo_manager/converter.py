from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

import rawpy
from PIL import Image, ImageOps
from psd_tools import PSDImage

from .models import ExportPreset


RAW_EXTENSIONS = {".cr2", ".cr3", ".dng", ".nef"}
IMAGE_EXTENSIONS = {
    ".cr2",
    ".cr3",
    ".dng",
    ".jpg",
    ".jpeg",
    ".nef",
    ".png",
    ".psd",
    ".tif",
    ".tiff",
}


def _read_rgb(source: Path, preview_edge: int | None) -> Image.Image:
    suffix = source.suffix.lower()
    if suffix in RAW_EXTENSIONS:
        with rawpy.imread(str(source)) as raw:
            pixels = raw.postprocess(
                use_camera_wb=True,
                output_color=rawpy.ColorSpace.sRGB,
                output_bps=8,
            )
        return Image.fromarray(pixels)
    if suffix == ".psd":
        image = PSDImage.open(source).composite()
        if image is None:
            raise RuntimeError("PSD 没有可用于导出的合成图像。")
        return image

    with Image.open(source) as opened:
        if suffix in {".jpg", ".jpeg"} and preview_edge:
            opened.draft("RGB", (preview_edge, preview_edge))
        image = ImageOps.exif_transpose(opened)
        image.load()
        return image.copy()


def image_dimensions(source: Path) -> tuple[int, int]:
    source = source.resolve()
    if source.suffix.lower() in RAW_EXTENSIONS:
        with rawpy.imread(str(source)) as raw:
            return raw.sizes.width, raw.sizes.height
    if source.suffix.lower() == ".psd":
        image = PSDImage.open(source).composite()
        if image is None:
            raise RuntimeError("PSD 没有可用于读取尺寸的合成图像。")
        return image.size
    with Image.open(source) as image:
        return ImageOps.exif_transpose(image).size


def export_to_jpeg(
    source: Path,
    destination: Path,
    preset: ExportPreset,
    exiftool_path: str | None = None,
    metadata_source: Path | None = None,
) -> Path:
    source = source.resolve()
    destination = destination.resolve()
    preset.validate()
    if source.suffix.lower() not in IMAGE_EXTENSIONS:
        raise ValueError(f"不支持的图片格式：{source.suffix}")
    if destination.exists():
        raise FileExistsError(f"目标文件已存在，不会覆盖：{destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.stem}.{uuid.uuid4().hex}.tmp.jpg"
    )

    try:
        image = _read_rgb(source, preset.max_edge)
        if preset.max_edge is not None:
            image.thumbnail(
                (preset.max_edge, preset.max_edge), Image.Resampling.LANCZOS
            )
        image.convert("RGB").save(
            temporary, "JPEG", quality=preset.quality, optimize=True
        )

        if exiftool_path:
            executable = Path(exiftool_path)
            if not executable.is_file():
                raise FileNotFoundError(f"找不到 ExifTool：{executable}")
            result = subprocess.run(
                [
                    str(executable),
                    "-TagsFromFile",
                    str((metadata_source or source).resolve()),
                    "-all:all",
                    "-overwrite_original",
                    str(temporary),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode:
                raise RuntimeError(result.stderr.strip() or "ExifTool 执行失败。")

        with Image.open(temporary) as rendered:
            rendered.verify()
        with Image.open(temporary) as rendered:
            rendered.load()
            if preset.max_edge is not None and max(rendered.size) > preset.max_edge:
                raise RuntimeError("生成的 JPG 尺寸超过选定档位。")

        if destination.exists():
            raise FileExistsError(f"目标文件已存在，不会覆盖：{destination}")
        os.rename(temporary, destination)
        return destination
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def convert_raw(
    source: Path, preset: ExportPreset, exiftool_path: str | None = None
) -> Path:
    if source.suffix.lower() not in RAW_EXTENSIONS:
        raise ValueError(f"仅支持 RAW 文件：{source.suffix}")
    destination = source.with_name(f"{source.stem}_lowres.jpg")
    return export_to_jpeg(source, destination, preset, exiftool_path)
