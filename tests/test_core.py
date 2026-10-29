from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image

from photo_manager.converter import convert_raw
from photo_manager.database import Catalog
from photo_manager.exposure import analyze_exposure
from photo_manager.lightroom_bridge import LightroomBridge
from photo_manager.models import ExportPreset
from photo_manager.scanner import read_capture_time, scan_directory


def test_scan_only_current_folder_and_returns_child_folders(tmp_path: Path) -> None:
    root = tmp_path / "photos"
    nested = root / "subfolder"
    nested.mkdir(parents=True)
    (root / "one.CR3").write_bytes(b"raw")
    (nested / "two.psd").write_bytes(b"psd data")
    (nested / "notes.txt").write_text("ignore", encoding="utf-8")

    result = scan_directory(root)

    assert len(result.records) == 1
    assert result.total_bytes == len(b"raw")
    assert result.records[0].extension == ".cr3"
    assert result.directories == (nested.resolve(),)


def test_scan_reads_photo_capture_date_and_time(tmp_path: Path) -> None:
    image_path = tmp_path / "capture.jpg"
    image = Image.new("RGB", (8, 8), "white")
    exif = Image.Exif()
    exif[306] = "2024:05:14 17:29:00"
    image.save(image_path, exif=exif)

    result = scan_directory(tmp_path)
    captured_at = read_capture_time(image_path)
    expected = datetime(2024, 5, 14, 17, 29, 0)
    assert captured_at == expected
    assert result.records[0].captured_at is None
    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.record_scan(result.root, result.records)
    catalog.set_capture_time(image_path.resolve(), captured_at)
    assert catalog.photos(root=tmp_path)[0].captured_at == expected


def test_catalog_preserves_labels_and_sync_state_after_rescan(
    tmp_path: Path,
) -> None:
    image = tmp_path / "image.nef"
    image.write_bytes(b"raw image")
    result = scan_directory(tmp_path)
    catalog = Catalog(tmp_path / "catalog.sqlite3")

    catalog.record_scan(result.root, result.records)
    catalog.set_importance(image.resolve(), "important")
    catalog.set_sync_state(image.resolve(), "synced")
    catalog.record_scan(result.root, result.records)

    record = catalog.photos()[0]
    assert record.importance == "important"
    assert record.lr_sync_state == "synced"


def test_catalog_persists_rating_and_collection_state(tmp_path: Path) -> None:
    image = tmp_path / "score.cr3"
    image.write_bytes(b"raw")
    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.record_scan(tmp_path, scan_directory(tmp_path).records)
    catalog.set_rating(image.resolve(), 4)
    catalog.set_collection_state(image.resolve(), "pending_processing")

    record = catalog.photo(image.resolve())
    assert record is not None
    assert record.rating == 4
    assert record.collection_state == "pending_processing"


def test_legacy_importance_migrates_to_five_point_rating(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        CREATE TABLE photos (
            path TEXT PRIMARY KEY COLLATE NOCASE,
            root TEXT NOT NULL,
            extension TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            modified_ns INTEGER NOT NULL,
            captured_at TEXT,
            importance TEXT NOT NULL DEFAULT 'unrated',
            lr_sync_state TEXT NOT NULL DEFAULT 'not_sent',
            exists_on_disk INTEGER NOT NULL DEFAULT 1,
            indexed_at TEXT NOT NULL
        );
        """
    )
    source = tmp_path / "important.jpg"
    source.write_bytes(b"image")
    connection.execute(
        """
        INSERT INTO photos(path, root, extension, size_bytes, modified_ns, importance, indexed_at)
        VALUES (?, ?, '.jpg', 5, 1, 'important', 'legacy')
        """,
        (str(source.resolve()), str(tmp_path.resolve())),
    )
    connection.commit()
    connection.close()

    record = Catalog(database_path).photo(source.resolve())

    assert record is not None
    assert record.rating == 5


def test_rating_export_limits_have_expected_defaults(tmp_path: Path) -> None:
    catalog = Catalog(tmp_path / "catalog.sqlite3")

    assert catalog.rating_export_limits() == {
        1: 1280,
        2: 1920,
        3: 2560,
        4: 3840,
        5: None,
    }
    catalog.set_rating_export_limit(4, 3000)
    assert catalog.rating_export_limits()[4] == 3000


def test_rescan_marks_removed_files_missing_without_erasing_sync_metadata(
    tmp_path: Path,
) -> None:
    image = tmp_path / "image.cr2"
    image.write_bytes(b"raw image")
    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.record_scan(tmp_path, scan_directory(tmp_path).records)
    catalog.set_sync_state(image.resolve(), "synced")

    image.unlink()
    catalog.record_scan(tmp_path, scan_directory(tmp_path).records)

    assert catalog.photos() == []
    assert catalog.photos(include_missing=True)[0].lr_sync_state == "synced"


def test_cached_folder_statistics_are_aggregated_in_sqlite(tmp_path: Path) -> None:
    (tmp_path / "one.jpg").write_bytes(b"one")
    (tmp_path / "two.psd").write_bytes(b"second")
    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.record_scan(tmp_path, scan_directory(tmp_path).records)

    assert catalog.folder_stats(tmp_path) == (2, len(b"one") + len(b"second"))


def test_lightroom_bridge_writes_request_and_reads_ack(
    tmp_path: Path,
) -> None:
    bridge = LightroomBridge(tmp_path / "bridge")
    photo = tmp_path / "photo.cr3"
    operation_id = bridge.enqueue("import", photo)
    request = json.loads(
        (tmp_path / "bridge" / "requests" / f"{operation_id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert request["action"] == "import"
    assert request["path"] == str(photo.resolve())

    ack_path = tmp_path / "bridge" / "acks" / f"{operation_id}.json"
    ack_path.write_text(
        json.dumps({"operation_id": operation_id, "status": "success"}),
        encoding="utf-8",
    )
    acknowledgements = bridge.pending_acks()
    assert acknowledgements[0][1]["operation_id"] == operation_id
    bridge.acknowledge(acknowledgements[0][0])
    assert not ack_path.exists()


def test_lightroom_bridge_reuses_persisted_operation_id(tmp_path: Path) -> None:
    bridge = LightroomBridge(tmp_path / "bridge")
    photo = tmp_path / "photo.nef"

    first = bridge.enqueue("remove", photo, "stable-operation")
    second = bridge.enqueue("remove", photo, "stable-operation")

    assert first == second == "stable-operation"
    requests = list((tmp_path / "bridge" / "requests").glob("*.json"))
    assert len(requests) == 1


def test_lightroom_bridge_render_request_carries_staging_directory(
    tmp_path: Path,
) -> None:
    bridge = LightroomBridge(tmp_path / "bridge")
    photo = tmp_path / "photo.raw"
    render_directory = tmp_path / "bridge" / "renders" / "render-operation"
    operation_id = bridge.enqueue(
        "render", photo, "render-operation", render_directory
    )

    request = json.loads(
        (bridge.requests_dir / f"{operation_id}.json").read_text(encoding="utf-8")
    )
    assert request["action"] == "render"
    assert request["output_path"] == str(render_directory.resolve())


def test_export_preset_validation() -> None:
    ExportPreset("Web", 1600, 90).validate()
    with pytest.raises(ValueError):
        ExportPreset("Bad", 32, 90).validate()
    with pytest.raises(ValueError):
        ExportPreset("Bad", 1600, 101).validate()
    ExportPreset("Native", None, 90).validate()


def test_histogram_endpoint_spikes_flag_exposure(tmp_path: Path) -> None:
    path = tmp_path / "clipped.png"
    image = Image.new("L", (100, 100), 128)
    values = [0] * 8 + [1] * 3 + [254] * 2 + [255] * 6
    image.putdata(values + [128] * (10000 - len(values)))
    image.save(path)

    result = analyze_exposure(path)

    assert result.underexposed
    assert result.overexposed


def test_export_jpeg_resizes_and_native_preset_preserves_dimensions(
    tmp_path: Path,
) -> None:
    from photo_manager.converter import export_to_jpeg

    source = tmp_path / "large.png"
    Image.new("RGB", (1200, 600), "purple").save(source)
    resized = export_to_jpeg(
        source,
        tmp_path / "small.jpg",
        ExportPreset("Small", 400, 85),
    )
    native = export_to_jpeg(
        source,
        tmp_path / "native.jpg",
        ExportPreset("Native", None, 90),
    )

    with Image.open(resized) as image:
        assert image.size == (400, 200)
    with Image.open(native) as image:
        assert image.size == (1200, 600)


def test_existing_jpg_is_never_overwritten(tmp_path: Path) -> None:
    raw = tmp_path / "photo.cr3"
    output = tmp_path / "photo_lowres.jpg"
    raw.write_bytes(b"raw source")
    output.write_bytes(b"existing jpeg")

    with pytest.raises(FileExistsError):
        convert_raw(raw, ExportPreset("Low", 2048, 85))

    assert raw.read_bytes() == b"raw source"
    assert output.read_bytes() == b"existing jpeg"
