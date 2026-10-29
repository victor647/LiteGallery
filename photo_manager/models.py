from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class ExportPreset:
    name: str
    max_edge: int | None
    quality: int

    def validate(self) -> None:
        if not self.name.strip():
            raise ValueError("预设名称不能为空。")
        if self.max_edge is not None and self.max_edge < 64:
            raise ValueError("最长边至少为 64 像素。")
        if not 1 <= self.quality <= 100:
            raise ValueError("JPEG 质量必须介于 1 到 100。")


@dataclass(frozen=True)
class PhotoRecord:
    path: Path
    root: Path
    extension: str
    size_bytes: int
    modified_ns: int
    captured_at: datetime | None = None
    importance: str = "unrated"
    lr_sync_state: str = "not_sent"
    rating: int = 0
    collection_state: str = "retained"
    rendered_path: Path | None = None
    exposure_issue: str | None = None


@dataclass(frozen=True)
class ScanResult:
    root: Path
    records: tuple[PhotoRecord, ...]
    errors: tuple[str, ...]
    directories: tuple[Path, ...] = ()

    @property
    def total_bytes(self) -> int:
        return sum(record.size_bytes for record in self.records)
