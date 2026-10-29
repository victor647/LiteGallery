from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import rawpy
from PIL import Image, ImageOps
from psd_tools import PSDImage

from .converter import RAW_EXTENSIONS


@dataclass(frozen=True)
class ExposureCheck:
    underexposed: bool
    overexposed: bool
    black_pixels: int
    near_black_pixels: int
    white_pixels: int
    near_white_pixels: int
    total_pixels: int

    @property
    def issue(self) -> str | None:
        flags = []
        if self.underexposed:
            flags.append("暗部端点")
        if self.overexposed:
            flags.append("亮部端点")
        return "、".join(flags) if flags else None


def _read_luminance_image(path: Path) -> Image.Image:
    suffix = path.suffix.lower()
    if suffix in RAW_EXTENSIONS:
        with rawpy.imread(str(path)) as raw:
            pixels = raw.postprocess(
                use_camera_wb=True,
                no_auto_bright=True,
                half_size=True,
                output_color=rawpy.ColorSpace.sRGB,
                output_bps=8,
            )
        return Image.fromarray(pixels)
    if suffix == ".psd":
        image = PSDImage.open(path).composite()
        if image is None:
            raise RuntimeError("PSD 没有可用于曝光检查的合成图像。")
        return image
    with Image.open(path) as source:
        return ImageOps.exif_transpose(source).copy()


def analyze_exposure(path: Path) -> ExposureCheck:
    path = path.resolve()
    image = _read_luminance_image(path)
    histogram = image.convert("L").histogram()
    total = sum(histogram)
    black = histogram[0]
    near_black = histogram[1]
    white = histogram[255]
    near_white = histogram[254]
    return ExposureCheck(
        underexposed=black > 0 and black >= max(1, near_black * 2),
        overexposed=white > 0 and white >= max(1, near_white * 2),
        black_pixels=black,
        near_black_pixels=near_black,
        white_pixels=white,
        near_white_pixels=near_white,
        total_pixels=total,
    )
