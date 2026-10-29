from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path

import rawpy
from PIL import Image, ImageOps
from psd_tools import PSDImage

from .converter import RAW_EXTENSIONS
from .paths import thumbnail_dir


def preview_cache_path(source: Path, max_edge: int = 900) -> Path:
    source = source.resolve()
    stat = source.stat()
    identity = f"{source}:{stat.st_size}:{stat.st_mtime_ns}:{max_edge}"
    key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return thumbnail_dir() / f"{key}.jpg"


def create_preview(source: Path, max_edge: int = 900) -> Path:
    source = source.resolve()
    target = preview_cache_path(source, max_edge)
    if target.exists():
        return target

    suffix = source.suffix.lower()
    if suffix in RAW_EXTENSIONS:
        with rawpy.imread(str(source)) as raw:
            thumbnail = raw.extract_thumb()
        if thumbnail.format == rawpy.ThumbFormat.JPEG:
            image = Image.open(BytesIO(thumbnail.data))
            image.load()
        else:
            image = Image.fromarray(thumbnail.data)
    elif suffix == ".psd":
        image = PSDImage.open(source).composite()
        if image is None:
            raise RuntimeError("PSD 文件不含可用的合成预览。")
    else:
        image = Image.open(source)
        if suffix in {".jpg", ".jpeg"}:
            image.draft("RGB", (max_edge, max_edge))
        image = ImageOps.exif_transpose(image)
        image.load()

    image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    image.convert("RGB").save(target, "JPEG", quality=88, optimize=True)
    return target
