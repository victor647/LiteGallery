from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PySide6.QtWidgets import QMessageBox

from .converter import RAW_EXTENSIONS, export_to_jpeg, image_dimensions
from .exposure import analyze_exposure
from .models import ExportPreset, PhotoRecord
from .workers import Worker


class ExportWorkflowMixin:
    def export_selected(self) -> None:
        record = self.selected_record()
        if record is None:
            return
        key = self.path_key(record.path)
        if key in self.busy_paths:
            return
        if record.rating not in range(1, 6):
            QMessageBox.information(self, "先评分", "请先按 1–5 为照片评分。")
            return
        if record.collection_state == "pending_delete":
            QMessageBox.warning(self, "照片待删除", "请先恢复照片再导出。")
            return
        if (
            record.collection_state == "pending_processing"
            and (
                record.rendered_path is None
                or not record.rendered_path.is_file()
            )
        ):
            QMessageBox.information(
                self,
                "等待 Lightroom 编辑版",
                "请先在 Lightroom 完成调整，再点击“获取 Lightroom 编辑版”。",
            )
            return
        source = (
            record.rendered_path
            if record.rendered_path and record.rendered_path.is_file()
            else record.path
        )
        if record.rating < 5 and record.lr_sync_state in {
            "pending_import",
            "pending_remove",
            "error",
        }:
            QMessageBox.warning(
                self,
                "Lightroom 同步待处理",
                "请先完成 Lightroom Catalog 同步，再执行会回收原件的导出。",
            )
            return
        if record.rating == 4:
            self.busy_paths.add(key)
            self.status.setText("正在检查 4 分照片的曝光直方图…")
            worker = Worker(lambda: analyze_exposure(source))
            worker.signals.finished.connect(
                lambda result: self.exposure_checked(record, source, result)
            )
            worker.signals.failed.connect(
                lambda message: self.export_failed(record.path, message)
            )
            self.pool.start(worker)
            return
        self.begin_export(record, source)

    def exposure_checked(
        self, record: PhotoRecord, source: Path, check
    ) -> None:
        key = self.path_key(record.path)
        current = self.catalog.photo(record.path) or record
        if (
            current.rating != record.rating
            or current.collection_state == "pending_delete"
        ):
            self.busy_paths.discard(key)
            self.status.setText("照片状态已变化，本次曝光检查已取消。")
            return
        if check.issue:
            self.busy_paths.discard(key)
            self.catalog.set_collection_state(record.path, "pending_processing")
            self.catalog.set_exposure_issue(record.path, check.issue)
            self.reload_gallery()
            self.select_in_zone(record.path, "pending_processing")
            self.status.setText(
                f"{record.path.name} 检测到{check.issue}直方图尖峰，"
                "已转入待处理区，未导出。"
            )
            return
        self.catalog.set_exposure_issue(record.path, None)
        self.begin_export(replace(current, exposure_issue=None), source)

    def begin_export(self, record: PhotoRecord, source: Path) -> None:
        key = self.path_key(record.path)
        limits = self.catalog.rating_export_limits()
        max_edge = limits.get(record.rating)
        quality = int(self.settings.value("jpeg_quality", 85))
        preset = ExportPreset(f"{record.rating} 分", max_edge, quality)
        uses_render = source != record.path

        if (
            not uses_render
            and record.extension in {".jpg", ".jpeg"}
            and max_edge is not None
            and record.rating < 5
        ):
            width, height = image_dimensions(record.path)
            if max(width, height) <= max_edge:
                self.busy_paths.discard(key)
                QMessageBox.information(
                    self,
                    "尺寸已符合",
                    "这张 JPG 已不超过该评分档的最大边长，无需重新导出。",
                )
                return
        if (
            not uses_render
            and record.extension in {".jpg", ".jpeg"}
            and max_edge is None
        ):
            self.busy_paths.discard(key)
            QMessageBox.information(
                self, "已是原始尺寸", "这张 JPG 已是原始像素尺寸，无需重复导出。"
            )
            return

        destination = record.path.with_name(
            f"{record.path.stem}_score{record.rating}.jpg"
        )
        if destination.exists():
            self.busy_paths.discard(key)
            QMessageBox.warning(
                self, "目标已存在", f"不会覆盖已有文件：{destination.name}"
            )
            return
        recycle_source = record.rating < 5 and (
            record.extension in RAW_EXTENSIONS
            or (
                record.extension in {".jpg", ".jpeg"}
                and max_edge is not None
                and max(image_dimensions(record.path)) > max_edge
            )
        )
        prompt = (
            f"将按 {max_edge or '原始尺寸'} 像素长边导出 JPG。"
            + (
                "\n\n导出验证成功后，原始 RAW/JPG 将移入回收站。"
                if recycle_source
                else ""
            )
            + "\n\n继续？"
        )
        if QMessageBox.question(self, "导出照片", prompt) != QMessageBox.StandardButton.Yes:
            self.busy_paths.discard(key)
            return

        exiftool = str(self.settings.value("exiftool_path", "")).strip() or None
        self.busy_paths.add(key)
        self.status.setText(f"正在导出 {record.path.name}…")
        worker = Worker(
            lambda: export_to_jpeg(
                source,
                destination,
                preset,
                exiftool,
                metadata_source=record.path,
            )
        )
        worker.signals.finished.connect(
            lambda output: self.export_finished(record, output, recycle_source)
        )
        worker.signals.failed.connect(
            lambda message: self.export_failed(record.path, message)
        )
        self.pool.start(worker)

    def export_failed(self, path: Path, message: str) -> None:
        self.busy_paths.discard(self.path_key(path))
        self.show_error(message)

    def export_finished(
        self, source_record: PhotoRecord, output: Path, recycle_source: bool
    ) -> None:
        key = self.path_key(source_record.path)
        try:
            current = self.catalog.photo(source_record.path)
            if (
                current is None
                or current.rating != source_record.rating
                or current.collection_state == "pending_delete"
            ):
                raise RuntimeError("导出期间照片状态发生变化，原件未删除。")

            output_record = self.record_for(
                output,
                source_record.root,
                source_record.captured_at,
                rating=source_record.rating,
            )
            self.catalog.upsert_record(output_record)
            self._cleanup_rendered(source_record.rendered_path)
            if source_record.rendered_path:
                self.catalog.set_rendered_path(source_record.path, None)

            if recycle_source and current.lr_sync_state == "synced":
                self.queue_operation(
                    "remove",
                    source_record.path,
                    followup="trash_export",
                    output_path=output,
                )
                self.status.setText(
                    f"JPG 已验证；等待 Lightroom 移除原件后回收：{source_record.path.name}"
                )
            elif recycle_source:
                if source_record.path.exists():
                    from send2trash import send2trash

                    send2trash(str(source_record.path))
                self.catalog.remove_photo(source_record.path)
                self.status.setText(
                    f"导出完成，原件已移入回收站：{output.name}"
                )
            else:
                if source_record.collection_state == "pending_processing":
                    self.catalog.set_collection_state(source_record.path, "retained")
                self.catalog.set_exposure_issue(source_record.path, None)
                self.status.setText(f"导出完成：{output.name}")
            self.reload_gallery()
        except Exception as error:
            self.status.setText(f"JPG 已生成，但原件处置失败，原件仍保留：{error}")
            QMessageBox.warning(self, "原件仍保留", self.status.text())
        finally:
            self.busy_paths.discard(key)
