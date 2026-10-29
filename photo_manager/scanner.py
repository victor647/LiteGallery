from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import rawpy
from PIL import ExifTags, Image

from .models import PhotoRecord, ScanResult


SUPPORTED_EXTENSIONS = {
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
RAW_EXTENSIONS = {".cr2", ".cr3", ".dng", ".nef"}


def read_capture_time(path: Path) -> datetime | None:
    if path.suffix.lower() in RAW_EXTENSIONS:
        try:
            with rawpy.imread(str(path)) as raw:
                timestamp = raw.metadata.timestamp
            return datetime.fromtimestamp(timestamp) if timestamp else None
        except Exception:
            return None

    try:
        with Image.open(path) as image:
            exif = image.getexif()
            exif_ifd = exif.get_ifd(ExifTags.IFD.Exif)
            for value in (
                exif_ifd.get(36867),
                exif_ifd.get(36868),
                exif.get(306),
            ):
                if not value:
                    continue
                try:
                    return datetime.strptime(str(value), "%Y:%m:%d %H:%M:%S")
                except ValueError:
                    continue
    except (OSError, ValueError, TypeError):
        return None
    return None


def scan_directory(
    root: Path,
    known_records: tuple[PhotoRecord, ...] | list[PhotoRecord] = (),
) -> ScanResult:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"目录不存在：{root}")

    records: list[PhotoRecord] = []
    directories: list[Path] = []
    errors: list[str] = []
    known = {str(record.path).casefold(): record for record in known_records}

    try:
        with os.scandir(root) as entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        directories.append(Path(entry.path).resolve())
                    elif (
                        entry.is_file(follow_symlinks=False)
                        and Path(entry.name).suffix.lower() in SUPPORTED_EXTENSIONS
                    ):
                        stat = entry.stat(follow_symlinks=False)
                        path = Path(entry.path).resolve()
                        old_record = known.get(str(path).casefold())
                        unchanged = (
                            old_record is not None
                            and old_record.size_bytes == stat.st_size
                            and old_record.modified_ns == stat.st_mtime_ns
                        )
                        records.append(
                            PhotoRecord(
                                path=path,
                                root=root,
                                extension=path.suffix.lower(),
                                size_bytes=stat.st_size,
                                modified_ns=stat.st_mtime_ns,
                                captured_at=(
                                    old_record.captured_at
                                    if unchanged and old_record
                                    else None
                                ),
                                importance=old_record.importance if old_record else "unrated",
                                lr_sync_state=(
                                    old_record.lr_sync_state if old_record else "not_sent"
                                ),
                                rating=old_record.rating if old_record else 0,
                                collection_state=(
                                    old_record.collection_state if old_record else "retained"
                                ),
                                rendered_path=(
                                    old_record.rendered_path if unchanged and old_record else None
                                ),
                                exposure_issue=(
                                    old_record.exposure_issue if unchanged and old_record else None
                                ),
                            )
                        )
                except OSError as error:
                    errors.append(f"{entry.path}: {error}")
    except OSError as error:
        errors.append(f"{root}: {error}")

    records.sort(key=lambda record: record.path.name.casefold())
    directories.sort(key=lambda path: path.name.casefold())
    return ScanResult(
        root=root,
        records=tuple(records),
        errors=tuple(errors),
        directories=tuple(directories),
    )
