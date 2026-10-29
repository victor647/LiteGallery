from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import ExportPreset, PhotoRecord
from .paths import app_data_dir


class Catalog:
    def __init__(self, database_path: Path | None = None) -> None:
        self.database_path = database_path or app_data_dir() / "catalog.sqlite3"
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 20000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS photos (
                    path TEXT PRIMARY KEY COLLATE NOCASE,
                    root TEXT NOT NULL,
                    extension TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    modified_ns INTEGER NOT NULL,
                    captured_at TEXT,
                    rating INTEGER NOT NULL DEFAULT 0,
                    collection_state TEXT NOT NULL DEFAULT 'retained',
                    rendered_path TEXT,
                    exposure_issue TEXT,
                    importance TEXT NOT NULL DEFAULT 'unrated',
                    lr_sync_state TEXT NOT NULL DEFAULT 'not_sent',
                    exists_on_disk INTEGER NOT NULL DEFAULT 1,
                    indexed_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS photos_root_idx ON photos(root);

                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    path TEXT NOT NULL,
                    followup TEXT NOT NULL DEFAULT 'none',
                    output_path TEXT,
                    state TEXT NOT NULL DEFAULT 'pending',
                    error TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS presets (
                    name TEXT PRIMARY KEY COLLATE NOCASE,
                    max_edge INTEGER NOT NULL,
                    quality INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS app_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                INSERT OR IGNORE INTO presets(name, max_edge, quality)
                VALUES ('低分辨率', 2048, 85);

                CREATE TABLE IF NOT EXISTS rating_exports (
                    rating INTEGER PRIMARY KEY,
                    max_edge INTEGER
                );
                INSERT OR IGNORE INTO rating_exports(rating, max_edge) VALUES
                    (1, 1280), (2, 1920), (3, 2560), (4, 3840), (5, NULL);
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(photos)").fetchall()
            }
            if "captured_at" not in columns:
                connection.execute("ALTER TABLE photos ADD COLUMN captured_at TEXT")
            if "rating" not in columns:
                connection.execute(
                    "ALTER TABLE photos ADD COLUMN rating INTEGER NOT NULL DEFAULT 0"
                )
                connection.execute(
                    """
                    UPDATE photos
                    SET rating = CASE importance
                        WHEN 'important' THEN 5
                        WHEN 'unimportant' THEN 1
                        ELSE 0
                    END
                    """
                )
            if "collection_state" not in columns:
                connection.execute(
                    """
                    ALTER TABLE photos ADD COLUMN collection_state TEXT
                    NOT NULL DEFAULT 'retained'
                    """
                )
            if "rendered_path" not in columns:
                connection.execute("ALTER TABLE photos ADD COLUMN rendered_path TEXT")
            if "exposure_issue" not in columns:
                connection.execute("ALTER TABLE photos ADD COLUMN exposure_issue TEXT")
            migrated = connection.execute(
                "SELECT 1 FROM app_meta WHERE key = 'binary-importance-to-rating-v1'"
            ).fetchone()
            if not migrated:
                connection.execute(
                    """
                    UPDATE photos SET rating = 5
                    WHERE rating = 0 AND importance = 'important'
                    """
                )
                connection.execute(
                    """
                    UPDATE photos SET rating = 1
                    WHERE rating = 0 AND importance = 'unimportant'
                    """
                )
                connection.execute(
                    """
                    INSERT INTO app_meta(key, value)
                    VALUES ('binary-importance-to-rating-v1', 'done')
                    """
                )

    def record_scan(self, root: Path, records: tuple[PhotoRecord, ...]) -> None:
        root_text = str(root.resolve())
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                "UPDATE photos SET exists_on_disk = 0 WHERE root = ? COLLATE NOCASE",
                (root_text,),
            )
            connection.executemany(
                """
                INSERT INTO photos(
                    path, root, extension, size_bytes, modified_ns, captured_at,
                    rating, collection_state, rendered_path, exposure_issue,
                    exists_on_disk, indexed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(path) DO UPDATE SET
                    root = excluded.root,
                    extension = excluded.extension,
                    size_bytes = excluded.size_bytes,
                    modified_ns = excluded.modified_ns,
                    captured_at = excluded.captured_at,
                    rating = excluded.rating,
                    collection_state = excluded.collection_state,
                    rendered_path = excluded.rendered_path,
                    exposure_issue = excluded.exposure_issue,
                    exists_on_disk = 1,
                    indexed_at = excluded.indexed_at
                """,
                [
                    (
                        str(record.path),
                        root_text,
                        record.extension,
                        record.size_bytes,
                        record.modified_ns,
                        record.captured_at.isoformat() if record.captured_at else None,
                        record.rating,
                        record.collection_state,
                        str(record.rendered_path) if record.rendered_path else None,
                        record.exposure_issue,
                        now,
                    )
                    for record in records
                ],
            )

    def upsert_record(self, record: PhotoRecord) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO photos(
                    path, root, extension, size_bytes, modified_ns, captured_at,
                    rating, collection_state, rendered_path, exposure_issue,
                    exists_on_disk, indexed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(path) DO UPDATE SET
                    root = excluded.root,
                    extension = excluded.extension,
                    size_bytes = excluded.size_bytes,
                    modified_ns = excluded.modified_ns,
                    captured_at = excluded.captured_at,
                    rating = excluded.rating,
                    collection_state = excluded.collection_state,
                    rendered_path = excluded.rendered_path,
                    exposure_issue = excluded.exposure_issue,
                    exists_on_disk = 1,
                    indexed_at = excluded.indexed_at
                """,
                (
                    str(record.path),
                    str(record.root),
                    record.extension,
                    record.size_bytes,
                    record.modified_ns,
                    record.captured_at.isoformat() if record.captured_at else None,
                    record.rating,
                    record.collection_state,
                    str(record.rendered_path) if record.rendered_path else None,
                    record.exposure_issue,
                    now,
                ),
            )

    def photos(
        self,
        root: Path | None = None,
        include_missing: bool = False,
        collection_state: str | None = None,
    ) -> list[PhotoRecord]:
        conditions: list[str] = []
        parameters: list[str] = []
        if root is not None:
            conditions.append("root = ? COLLATE NOCASE")
            parameters.append(str(root.resolve()))
        if not include_missing:
            conditions.append("exists_on_disk = 1")
        if collection_state is not None:
            conditions.append("collection_state = ?")
            parameters.append(collection_state)
        predicate = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM photos {predicate} ORDER BY path COLLATE NOCASE",
                parameters,
            ).fetchall()
        return [
            PhotoRecord(
                path=Path(row["path"]),
                root=Path(row["root"]),
                extension=row["extension"],
                size_bytes=row["size_bytes"],
                modified_ns=row["modified_ns"],
                captured_at=(
                    datetime.fromisoformat(row["captured_at"])
                    if row["captured_at"]
                    else None
                ),
                importance=row["importance"],
                lr_sync_state=row["lr_sync_state"],
                rating=row["rating"],
                collection_state=row["collection_state"],
                rendered_path=(
                    Path(row["rendered_path"]) if row["rendered_path"] else None
                ),
                exposure_issue=row["exposure_issue"],
            )
            for row in rows
        ]

    def photo(self, path: Path) -> PhotoRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM photos WHERE path = ?", (str(path),)
            ).fetchone()
        if row is None:
            return None
        return PhotoRecord(
            path=Path(row["path"]),
            root=Path(row["root"]),
            extension=row["extension"],
            size_bytes=row["size_bytes"],
            modified_ns=row["modified_ns"],
            captured_at=(
                datetime.fromisoformat(row["captured_at"])
                if row["captured_at"]
                else None
            ),
            importance=row["importance"],
            lr_sync_state=row["lr_sync_state"],
            rating=row["rating"],
            collection_state=row["collection_state"],
            rendered_path=(
                Path(row["rendered_path"]) if row["rendered_path"] else None
            ),
            exposure_issue=row["exposure_issue"],
        )

    def folder_stats(self, root: Path) -> tuple[int, int]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS photo_count, COALESCE(SUM(size_bytes), 0) AS total_bytes
                FROM photos
                WHERE root = ? COLLATE NOCASE AND exists_on_disk = 1
                """,
                (str(root.resolve()),),
            ).fetchone()
        return int(row["photo_count"]), int(row["total_bytes"])

    def set_importance(self, path: Path, importance: str) -> None:
        if importance not in {"important", "unimportant", "unrated"}:
            raise ValueError("无效的重要性标记。")
        with self._connect() as connection:
            connection.execute(
                "UPDATE photos SET importance = ? WHERE path = ?",
                (importance, str(path)),
            )

    def set_rating(self, path: Path, rating: int) -> None:
        if rating not in range(0, 6):
            raise ValueError("评分必须是 0 到 5。")
        with self._connect() as connection:
            connection.execute(
                "UPDATE photos SET rating = ? WHERE path = ?",
                (rating, str(path)),
            )

    def set_collection_state(self, path: Path, state: str) -> None:
        if state not in {"retained", "pending_delete", "pending_processing"}:
            raise ValueError("无效的照片分区状态。")
        with self._connect() as connection:
            connection.execute(
                "UPDATE photos SET collection_state = ? WHERE path = ?",
                (state, str(path)),
            )

    def set_rendered_path(self, path: Path, rendered_path: Path | None) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE photos SET rendered_path = ? WHERE path = ?",
                (str(rendered_path) if rendered_path else None, str(path)),
            )

    def set_exposure_issue(self, path: Path, issue: str | None) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE photos SET exposure_issue = ? WHERE path = ?",
                (issue, str(path)),
            )

    def set_sync_state(self, path: Path, state: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE photos SET lr_sync_state = ? WHERE path = ?",
                (state, str(path)),
            )

    def set_capture_time(self, path: Path, captured_at: datetime) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE photos SET captured_at = ? WHERE path = ?",
                (captured_at.isoformat(), str(path)),
            )

    def remove_photo(self, path: Path) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM photos WHERE path = ?", (str(path),))

    def add_operation(
        self,
        operation_id: str,
        action: str,
        path: Path,
        followup: str = "none",
        output_path: Path | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO operations(
                    operation_id, action, path, followup, output_path, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    operation_id,
                    action,
                    str(path),
                    followup,
                    str(output_path) if output_path else None,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def operation(self, operation_id: str) -> sqlite3.Row | None:
        with self._connect() as connection:
            return connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()

    def pending_operations(self) -> list[sqlite3.Row]:
        with self._connect() as connection:
            return connection.execute(
                "SELECT * FROM operations WHERE state = 'pending' ORDER BY created_at"
            ).fetchall()

    def finish_operation(
        self, operation_id: str, state: str, error: str | None = None
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE operations SET state = ?, error = ? WHERE operation_id = ?",
                (state, error, operation_id),
            )

    def presets(self) -> list[ExportPreset]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT name, max_edge, quality FROM presets ORDER BY name COLLATE NOCASE"
            ).fetchall()
        return [
            ExportPreset(row["name"], row["max_edge"], row["quality"])
            for row in rows
        ]

    def rating_export_limits(self) -> dict[int, int | None]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT rating, max_edge FROM rating_exports ORDER BY rating"
            ).fetchall()
        return {row["rating"]: row["max_edge"] for row in rows}

    def set_rating_export_limit(self, rating: int, max_edge: int | None) -> None:
        if rating not in range(1, 6):
            raise ValueError("评分档位必须是 1 到 5。")
        if max_edge is not None and max_edge < 64:
            raise ValueError("最大分辨率至少为 64 像素。")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO rating_exports(rating, max_edge) VALUES (?, ?)
                ON CONFLICT(rating) DO UPDATE SET max_edge = excluded.max_edge
                """,
                (rating, max_edge),
            )

    def save_preset(self, preset: ExportPreset) -> None:
        preset.validate()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO presets(name, max_edge, quality) VALUES (?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    max_edge = excluded.max_edge,
                    quality = excluded.quality
                """,
                (preset.name, preset.max_edge, preset.quality),
            )
