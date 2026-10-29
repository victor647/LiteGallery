from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QPoint, QSettings, QSize, Qt, QThreadPool, QTimer
from PySide6.QtGui import QPixmap, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSlider,
    QSpinBox,
    QSplitter,
    QStyle,
    QVBoxLayout,
    QWidget,
)
from send2trash import send2trash

from .converter import RAW_EXTENSIONS, export_to_jpeg, image_dimensions
from .database import Catalog
from .export_workflow import ExportWorkflowMixin
from .exposure import ExposureCheck, analyze_exposure
from .gallery import (
    CARD_SIZE,
    DIRECTORY_ROLE,
    MAX_CARD_SIZE,
    PATH_ROLE,
    RECORD_ROLE,
    THUMBNAIL_ROLE,
    GalleryView,
    PhotoCardDelegate,
    PhotoViewerDialog,
    ZoneFilterProxyModel,
)
from .lightroom_bridge import LightroomBridge
from .models import ExportPreset, PhotoRecord, ScanResult
from .previews import create_preview
from .scanner import read_capture_time, scan_directory
from .workers import Worker

THUMBNAIL_EDGE = 320
ZONES = (
    ("retained", "保留区"),
    ("pending_delete", "待删除区"),
    ("pending_processing", "待处理区"),
)


def format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def capture_time_text(record: PhotoRecord) -> str:
    if record.captured_at is None:
        return "拍摄时间未知"
    return record.captured_at.strftime("%Y-%m-%d %H:%M")


def photo_label(record: PhotoRecord) -> str:
    rating = f"{record.rating}★" if record.rating else "未评分"
    issue = f" · {record.exposure_issue}" if record.exposure_issue else ""
    return f"{record.path.name}\n{capture_time_text(record)} · {rating}{issue}"


class MainWindow(ExportWorkflowMixin, QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("LiteGallery")
        self.resize(1400, 900)
        self.catalog = Catalog()
        self.bridge = LightroomBridge()
        self.settings = QSettings("LiteGallery", "LiteGallery")
        self.pool = QThreadPool.globalInstance()
        self.pool.setMaxThreadCount(4)
        self.current_root: Path | None = None
        self.current_directories: tuple[Path, ...] = ()
        self.active_zone = "retained"
        self.gallery_items: dict[str, QStandardItem] = {}
        self.thumbnail_pending: set[str] = set()
        self.thumbnail_ready: set[str] = set()
        self.busy_paths: set[str] = set()

        self.folder = QLineEdit(str(self.settings.value("last_folder", "")))
        self.folder.setPlaceholderText("当前文件夹")
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索文件名或拍摄时间…")
        self.card_size = CARD_SIZE
        self.status = QLabel(
            "评分按 1–5；E 加入待处理区；Delete 暂存删除；Backspace 恢复。"
        )

        self.gallery_model = QStandardItemModel(self)
        self.views: dict[str, GalleryView] = {}
        self.proxies: dict[str, ZoneFilterProxyModel] = {}
        self.zone_counts: dict[str, QLabel] = {}
        for state, _ in ZONES:
            proxy = ZoneFilterProxyModel(state, self)
            proxy.setSourceModel(self.gallery_model)
            proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            proxy.setFilterKeyColumn(0)
            view = GalleryView()
            view.setModel(proxy)
            view.setViewMode(GalleryView.ViewMode.IconMode)
            view.setFlow(GalleryView.Flow.LeftToRight)
            view.setWrapping(True)
            view.setWordWrap(True)
            view.setResizeMode(GalleryView.ResizeMode.Adjust)
            view.setMovement(GalleryView.Movement.Static)
            view.setLayoutMode(GalleryView.LayoutMode.Batched)
            view.setBatchSize(96)
            view.setSelectionMode(GalleryView.SelectionMode.SingleSelection)
            view.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            view.setItemDelegate(PhotoCardDelegate(view))
            view.setIconSize(QSize(132, 150))
            view.setGridSize(CARD_SIZE)
            view.setSpacing(8)
            view.setUniformItemSizes(True)
            view.setTextElideMode(Qt.TextElideMode.ElideMiddle)
            view.activated.connect(
                lambda index, zone=state: self.activate_entry(index, zone)
            )
            view.ratingRequested.connect(
                lambda rating, zone=state: self.rate_selected(rating, zone)
            )
            view.processingRequested.connect(
                lambda zone=state: self.mark_processing_selected(zone)
            )
            view.deleteRequested.connect(
                lambda zone=state: self.move_selected_to_delete(zone)
            )
            view.restoreRequested.connect(
                lambda zone=state: self.restore_selected(zone)
            )
            view.viewportChanged.connect(
                lambda zone=state: self.schedule_visible_thumbnails(zone)
            )
            view.focusEntered.connect(
                lambda zone=state: self.set_active_zone(zone)
            )
            self.views[state] = view
            self.proxies[state] = proxy

        panels: dict[str, QFrame] = {}
        for state, title in ZONES:
            panel = QFrame()
            panel.setFrameShape(QFrame.Shape.StyledPanel)
            panel_layout = QVBoxLayout(panel)
            panel_layout.setContentsMargins(8, 6, 8, 8)
            heading = QHBoxLayout()
            title_label = QLabel(title)
            title_label.setStyleSheet("font-size: 14px; font-weight: 600;")
            count_label = QLabel("0 项")
            count_label.setAlignment(Qt.AlignmentFlag.AlignRight)
            count_label.setStyleSheet("color: #68717d;")
            heading.addWidget(title_label)
            heading.addWidget(count_label, 1)
            panel_layout.addLayout(heading)
            panel_layout.addWidget(self.views[state], 1)
            panels[state] = panel
            self.zone_counts[state] = count_label

        right_splitter = QSplitter(Qt.Orientation.Vertical)
        right_splitter.setChildrenCollapsible(False)
        right_splitter.addWidget(panels["pending_processing"])
        right_splitter.addWidget(panels["pending_delete"])
        right_splitter.setStretchFactor(0, 1)
        right_splitter.setStretchFactor(1, 1)
        right_splitter.setSizes([420, 420])

        gallery_splitter = QSplitter(Qt.Orientation.Horizontal)
        gallery_splitter.setChildrenCollapsible(False)
        gallery_splitter.addWidget(panels["retained"])
        gallery_splitter.addWidget(right_splitter)
        gallery_splitter.setStretchFactor(0, 1)
        gallery_splitter.setStretchFactor(1, 1)
        gallery_splitter.setSizes([700, 700])

        browse_button = QPushButton("选择目录")
        parent_button = QPushButton("上一级")
        scan_button = QPushButton("刷新")
        rating_hint = QLabel("评分：1–5　E：待处理　Delete：待删除　Backspace：恢复")
        send_lightroom_button = QPushButton("发送到 Lightroom")
        get_render_button = QPushButton("获取 Lightroom 编辑版")
        photoshop_button = QPushButton("在 Photoshop 打开")
        export_button = QPushButton("导出 JPG")
        clear_pending_button = QPushButton("清空待删除区")
        settings_button = QPushButton("设置")
        plugin_button = QPushButton("准备 Lightroom 插件")

        browse_button.clicked.connect(self.choose_folder)
        parent_button.clicked.connect(self.go_parent)
        scan_button.clicked.connect(self.scan_folder)
        self.folder.returnPressed.connect(self.navigate_from_address)
        self.search.textChanged.connect(self.filter_all_zones)
        send_lightroom_button.clicked.connect(self.send_to_lightroom)
        get_render_button.clicked.connect(self.get_lightroom_render)
        photoshop_button.clicked.connect(self.open_in_photoshop)
        export_button.clicked.connect(self.export_selected)
        clear_pending_button.clicked.connect(self.clear_pending_deletions)
        settings_button.clicked.connect(self.configure_settings)
        plugin_button.clicked.connect(self.prepare_plugin)

        path_row = QHBoxLayout()
        path_row.addWidget(parent_button)
        path_row.addWidget(self.folder, 1)
        path_row.addWidget(browse_button)
        path_row.addWidget(scan_button)
        path_row.addWidget(self.search, 1)

        action_row = QHBoxLayout()
        for widget in (
            rating_hint,
            send_lightroom_button,
            get_render_button,
            photoshop_button,
            export_button,
            clear_pending_button,
            settings_button,
            plugin_button,
        ):
            action_row.addWidget(widget)

        layout = QVBoxLayout()
        layout.addLayout(path_row)
        layout.addLayout(action_row)
        size_row = QHBoxLayout()
        size_row.addWidget(QLabel("缩略图大小"))
        size_row.addWidget(QLabel("小"))
        self.card_size_slider = QSlider(Qt.Orientation.Horizontal)
        self.card_size_slider.setRange(CARD_SIZE.width(), MAX_CARD_SIZE.width())
        self.card_size_slider.setSingleStep(2)
        self.card_size_slider.setPageStep(20)
        self.card_size_slider.setTickInterval(34)
        self.card_size_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        saved_card_width = int(
            self.settings.value("thumbnail_card_width", CARD_SIZE.width())
        )
        saved_card_width = max(
            CARD_SIZE.width(), min(MAX_CARD_SIZE.width(), saved_card_width)
        )
        saved_card_width -= saved_card_width % 2
        self.card_size_slider.setValue(saved_card_width)
        self.card_size_slider.valueChanged.connect(self.set_card_width)
        size_row.addWidget(self.card_size_slider, 1)
        size_row.addWidget(QLabel("大"))
        layout.addLayout(size_row)
        self.set_card_width(saved_card_width)
        layout.addWidget(gallery_splitter, 1)
        layout.addWidget(self.status)
        central = QWidget()
        central.setLayout(layout)
        self.setCentralWidget(central)

        self.ack_timer = QTimer(self)
        self.ack_timer.timeout.connect(self.process_acks)
        self.ack_timer.start(1000)

        initial_folder = (
            Path(self.folder.text().strip()) if self.folder.text().strip() else None
        )
        if initial_folder and initial_folder.is_dir():
            self.navigate_to(initial_folder, scan=True)

    def current_view(self, zone: str | None = None) -> GalleryView:
        return self.views[zone or self.active_zone]

    def current_proxy(self, zone: str | None = None) -> ZoneFilterProxyModel:
        return self.proxies[zone or self.active_zone]

    def set_active_zone(self, zone: str) -> None:
        self.active_zone = zone
        self.schedule_visible_thumbnails(zone)

    def filter_all_zones(self, text: str) -> None:
        for proxy in self.proxies.values():
            proxy.setFilterFixedString(text)
        self.update_zone_counts()
        self.schedule_all_visible_thumbnails()

    def update_zone_counts(self) -> None:
        for zone, proxy in self.proxies.items():
            self.zone_counts[zone].setText(f"{proxy.rowCount()} 项")

    def schedule_all_visible_thumbnails(self) -> None:
        for zone in self.views:
            self.schedule_visible_thumbnails(zone)

    def set_card_width(self, width: int) -> None:
        width = max(CARD_SIZE.width(), min(MAX_CARD_SIZE.width(), width))
        width -= width % 2
        self.card_size = QSize(width, width // 2)
        self.settings.setValue("thumbnail_card_width", width)
        for view in self.views.values():
            view.setGridSize(self.card_size)
            view.itemDelegate().set_card_size(self.card_size)
            view.setIconSize(
                QSize(max(1, int((width - 15) * 0.4)), max(1, width // 2 - 10))
            )
            view.viewport().update()

    def selected_item(self, zone: str | None = None, index=None) -> QStandardItem | None:
        view = self.current_view(zone)
        proxy = self.current_proxy(zone)
        if index is None:
            index = view.currentIndex()
        if not index.isValid():
            return None
        source_index = proxy.mapToSource(index)
        if not source_index.isValid():
            return None
        return self.gallery_model.itemFromIndex(source_index)

    def selected_record(self, zone: str | None = None) -> PhotoRecord | None:
        item = self.selected_item(zone)
        if item is None or item.data(DIRECTORY_ROLE):
            return None
        return item.data(RECORD_ROLE)

    @staticmethod
    def path_key(path: Path) -> str:
        return str(path.resolve()).casefold()

    def is_busy(self, path: Path) -> bool:
        return self.path_key(path) in self.busy_paths

    def choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "选择照片文件夹", self.folder.text()
        )
        if folder:
            self.navigate_to(Path(folder), scan=True)

    def navigate_from_address(self) -> None:
        path = Path(self.folder.text().strip()).expanduser()
        if path.is_dir():
            self.navigate_to(path, scan=True)
        else:
            QMessageBox.warning(self, "目录无效", "请输入一个存在的文件夹。")

    def navigate_to(self, root: Path, scan: bool = True) -> None:
        root = root.expanduser().resolve()
        if not root.is_dir():
            QMessageBox.warning(self, "目录无效", "所选文件夹不存在或无法访问。")
            return
        self.current_root = root
        self.folder.setText(str(root))
        self.settings.setValue("last_folder", str(root))
        self.current_directories = ()
        self.populate_gallery((), self.catalog.photos(root=root))
        self.show_cached_stats()
        if scan:
            self.scan_folder()

    def go_parent(self) -> None:
        if self.current_root is None:
            return
        parent = self.current_root.parent
        if parent != self.current_root:
            self.navigate_to(parent, scan=True)

    def scan_folder(self) -> None:
        folder_text = self.folder.text().strip()
        if not folder_text or not Path(folder_text).is_dir():
            QMessageBox.warning(self, "目录无效", "请选择一个存在的文件夹。")
            return
        root = Path(folder_text).expanduser().resolve()
        self.current_root = root
        self.settings.setValue("last_folder", str(root))
        self.status.setText("正在扫描当前文件夹…")

        def task() -> tuple[ScanResult, list[PhotoRecord]]:
            cached = self.catalog.photos(root=root, include_missing=True)
            result = scan_directory(root, cached)
            self.catalog.record_scan(result.root, result.records)
            return result, self.catalog.photos(root=result.root)

        worker = Worker(task)
        worker.signals.finished.connect(self.scan_finished)
        worker.signals.failed.connect(self.show_error)
        self.pool.start(worker)

    def scan_finished(
        self, value: tuple[ScanResult, list[PhotoRecord]]
    ) -> None:
        result, records = value
        if self.current_root is None or result.root != self.current_root:
            return
        self.current_directories = result.directories
        self.populate_gallery(result.directories, records)
        total = sum(record.size_bytes for record in records)
        self.status.setText(
            f"{len(records):,} 张照片 · {format_bytes(total)} · "
            f"{len(result.directories)} 个子文件夹"
            + " · 双击照片预览，←/→切换"
            + (f" · {len(result.errors)} 项无法读取" if result.errors else "")
        )
        self.update_zone_counts()
        QTimer.singleShot(0, self.schedule_all_visible_thumbnails)

    def show_cached_stats(self) -> None:
        if self.current_root is None:
            return
        count, size = self.catalog.folder_stats(self.current_root)
        self.status.setText(
            f"缓存统计（上次扫描）：{count:,} 张照片 · {format_bytes(size)}；"
            "正在刷新当前文件夹…"
        )

    def populate_gallery(
        self,
        directories: tuple[Path, ...] | list[Path],
        records: list[PhotoRecord],
    ) -> None:
        self.gallery_model.clear()
        self.gallery_items = {}
        self.thumbnail_ready = set()
        folder_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon)
        for directory in directories:
            item = QStandardItem()
            item.setText(f"{directory.name}\n子文件夹")
            item.setIcon(folder_icon)
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
            item.setEditable(False)
            item.setData(str(directory), PATH_ROLE)
            item.setData(True, DIRECTORY_ROLE)
            self.gallery_model.appendRow(item)
            self.gallery_items[str(directory).casefold()] = item

        records.sort(key=lambda record: record.path.name.casefold())
        for record in records:
            item = QStandardItem()
            item.setText(photo_label(record))
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
            item.setEditable(False)
            item.setToolTip(f"{record.path.name}\n拍摄时间：{capture_time_text(record)}")
            item.setData(str(record.path), PATH_ROLE)
            item.setData(False, DIRECTORY_ROLE)
            item.setData(record, RECORD_ROLE)
            self.gallery_model.appendRow(item)
            self.gallery_items[str(record.path).casefold()] = item

        self.update_zone_counts()

    def schedule_visible_thumbnails(self, zone: str) -> None:
        view = self.current_view(zone)
        proxy = self.current_proxy(zone)
        if proxy.rowCount() == 0:
            return
        viewport = view.viewport().rect()
        sample_points = (
            QPoint(8, 8),
            QPoint(viewport.width() // 2, viewport.height() // 2),
            QPoint(max(8, viewport.width() - 8), max(8, viewport.height() - 8)),
            QPoint(8, max(8, viewport.height() - 8)),
        )
        visible_rows = [
            index.row()
            for point in sample_points
            if (index := view.indexAt(point)).isValid()
        ]
        if not visible_rows:
            start = 0
            end = min(proxy.rowCount(), 48)
        else:
            start = max(0, min(visible_rows) - 8)
            columns = max(1, viewport.width() // view.gridSize().width())
            visible_rows_count = (
                max(1, viewport.height() // view.gridSize().height()) + 3
            )
            end = min(
                proxy.rowCount(),
                start + columns * visible_rows_count + columns * 2,
            )

        for row in range(start, end):
            proxy_index = proxy.index(row, 0)
            item = self.selected_item(zone, proxy_index)
            if item is None or item.data(DIRECTORY_ROLE):
                continue
            record: PhotoRecord = item.data(RECORD_ROLE)
            key = str(record.path).casefold()
            if key in self.thumbnail_pending or key in self.thumbnail_ready:
                continue
            self.thumbnail_pending.add(key)
            preview_source = (
                record.rendered_path
                if record.rendered_path and record.rendered_path.is_file()
                else record.path
            )

            def load(
                original=record.path,
                source=preview_source,
                known_date=record.captured_at,
            ):
                preview = create_preview(source, THUMBNAIL_EDGE)
                captured = known_date or read_capture_time(original)
                return original, preview, captured

            worker = Worker(load)
            worker.signals.finished.connect(self.thumbnail_finished)
            worker.signals.failed.connect(
                lambda _message, source_path=record.path: self.thumbnail_failed(
                    source_path
                )
            )
            self.pool.start(worker)

    def thumbnail_finished(
        self, result: tuple[Path, Path, object]
    ) -> None:
        source, preview, captured = result
        key = str(source).casefold()
        self.thumbnail_pending.discard(key)
        self.thumbnail_ready.add(key)
        item = self.gallery_items.get(key)
        if item is None:
            return
        record: PhotoRecord = item.data(RECORD_ROLE)
        if captured and record.captured_at is None:
            self.catalog.set_capture_time(source, captured)
            record = replace(record, captured_at=captured)
            item.setData(record, RECORD_ROLE)
            item.setToolTip(
                f"{record.path.name}\n拍摄时间：{capture_time_text(record)}"
            )
            item.setText(photo_label(record))
        item.setData(QPixmap(str(preview)), THUMBNAIL_ROLE)

    def thumbnail_failed(self, source: Path) -> None:
        key = str(source).casefold()
        self.thumbnail_pending.discard(key)
        self.thumbnail_ready.add(key)

    def activate_entry(self, index, zone: str) -> None:
        item = self.selected_item(zone, index)
        if item is None:
            return
        path = Path(item.data(PATH_ROLE))
        if item.data(DIRECTORY_ROLE):
            self.navigate_to(path, scan=True)
            return
        self.open_viewer(index, zone)

    def open_viewer(self, selected_index, zone: str | None = None) -> None:
        zone = zone or self.active_zone
        proxy = self.current_proxy(zone)
        records: list[PhotoRecord] = []
        selected_item = self.selected_item(zone, selected_index)
        if selected_item is None or selected_item.data(DIRECTORY_ROLE):
            return
        selected_path = Path(selected_item.data(PATH_ROLE))
        selected_position = 0
        for row in range(proxy.rowCount()):
            item = self.selected_item(zone, proxy.index(row, 0))
            if item is None or item.data(DIRECTORY_ROLE):
                continue
            record = item.data(RECORD_ROLE)
            if record.path == selected_path:
                selected_position = len(records)
            records.append(record)
        if not records:
            return
        viewer = PhotoViewerDialog(records, selected_position, self.pool, self)
        viewer.ratingRequested.connect(
            lambda rating: self.rate_record(
                viewer.current_record(), rating, viewer
            )
        )
        viewer.processingRequested.connect(
            lambda: self._change_viewer_zone(viewer, self.mark_processing)
        )
        viewer.deleteRequested.connect(
            lambda: self._change_viewer_zone(viewer, self.mark_pending_delete)
        )
        viewer.restoreRequested.connect(
            lambda: self._change_viewer_zone(viewer, self.restore_photo)
        )
        viewer.exec()

    @staticmethod
    def _change_viewer_zone(viewer: PhotoViewerDialog, callback) -> None:
        callback(viewer.current_record().path)
        viewer.accept()

    def open_viewer_for_path(self, path: Path, zone: str = "pending_processing") -> None:
        proxy = self.current_proxy(zone)
        for row in range(proxy.rowCount()):
            index = proxy.index(row, 0)
            item = self.selected_item(zone, index)
            if item is not None and not item.data(DIRECTORY_ROLE):
                record: PhotoRecord = item.data(RECORD_ROLE)
                if record.path == path:
                    self.set_active_zone(zone)
                    self.views[zone].setFocus()
                    self.open_viewer(index, zone)
                    return

    def reload_gallery(self) -> None:
        if self.current_root is not None:
            self.populate_gallery(
                self.current_directories,
                self.catalog.photos(root=self.current_root),
            )
            QTimer.singleShot(0, self.schedule_all_visible_thumbnails)

    def select_in_zone(self, path: Path, zone: str) -> None:
        self.set_active_zone(zone)
        proxy = self.current_proxy(zone)
        view = self.current_view(zone)
        view.setFocus()
        target = str(path).casefold()
        for row in range(proxy.rowCount()):
            index = proxy.index(row, 0)
            item = self.selected_item(zone, index)
            if (
                item is not None
                and not item.data(DIRECTORY_ROLE)
                and str(item.data(PATH_ROLE)).casefold() == target
            ):
                view.setCurrentIndex(index)
                view.scrollTo(index, GalleryView.ScrollHint.PositionAtCenter)
                return

    def rate_selected(self, rating: int, zone: str) -> None:
        record = self.selected_record(zone)
        if record is not None:
            self.rate_record(record, rating)

    def rate_record(
        self, record: PhotoRecord, rating: int, viewer: PhotoViewerDialog | None = None
    ) -> None:
        if self.is_busy(record.path):
            self.status.setText("该照片正在导出，暂不能修改评分。")
            return
        self.catalog.set_rating(record.path, rating)
        updated = replace(record, rating=rating)
        item = self.gallery_items.get(str(record.path).casefold())
        if item is not None:
            item.setData(updated, RECORD_ROLE)
            item.setText(photo_label(updated))
        if viewer is not None:
            viewer.set_current_record(updated)

    def mark_processing_selected(self, zone: str) -> None:
        record = self.selected_record(zone)
        if record is not None:
            self.mark_processing(record.path)

    def mark_processing(self, path: Path) -> None:
        if self.is_busy(path):
            self.status.setText("该照片正在导出，暂不能修改分区。")
            return
        record = self.catalog.photo(path)
        if record is None:
            return
        self._cleanup_rendered(record.rendered_path)
        self.catalog.set_rendered_path(path, None)
        self.catalog.set_collection_state(path, "pending_processing")
        self.reload_gallery()
        self.select_in_zone(path, "pending_processing")
        self.status.setText(
            f"{path.name} 已加入待处理区；完成 Lightroom 调整后获取编辑版。"
        )

    def move_selected_to_delete(self, zone: str) -> None:
        record = self.selected_record(zone)
        if record is not None:
            self.mark_pending_delete(record.path)

    def mark_pending_delete(self, path: Path) -> None:
        if self.is_busy(path):
            self.status.setText("该照片正在导出，暂不能移入待删除区。")
            return
        record = self.catalog.photo(path)
        if record is None or record.collection_state == "pending_delete":
            return
        self.catalog.set_collection_state(path, "pending_delete")
        self.reload_gallery()
        self.select_in_zone(path, "pending_delete")
        self.status.setText(f"{path.name} 已暂存到待删除区，文件尚未删除。")

    def restore_selected(self, zone: str) -> None:
        record = self.selected_record(zone)
        if record is not None:
            self.restore_photo(record.path)

    def restore_photo(self, path: Path) -> None:
        if self.is_busy(path):
            self.status.setText("该照片正在导出，暂不能恢复分区。")
            return
        record = self.catalog.photo(path)
        if record is None or record.collection_state == "retained":
            return
        if record.collection_state == "pending_processing":
            self._cleanup_rendered(record.rendered_path)
            self.catalog.set_rendered_path(path, None)
            self.catalog.set_exposure_issue(path, None)
        self.catalog.set_collection_state(path, "retained")
        self.reload_gallery()
        self.select_in_zone(path, "retained")
        self.status.setText(f"{path.name} 已恢复到保留区。")

    def send_to_lightroom(self) -> None:
        record = self.selected_record()
        if record is None:
            return
        if record.lr_sync_state in {"pending_import", "pending_remove"}:
            QMessageBox.information(self, "操作待处理", "该照片已有 Lightroom 操作待处理。")
            return
        if record.lr_sync_state == "synced":
            QMessageBox.information(self, "已同步", "该照片已经在 Lightroom Catalog 中。")
            return
        try:
            self.queue_operation("import", record.path)
            self.status.setText(
                "已排队；请启动 Lightroom Classic 并启用 LiteGallery 插件。"
            )
            self.reload_gallery()
        except Exception as error:
            self.show_error(str(error))

    def get_lightroom_render(self) -> None:
        record = self.selected_record()
        if record is None:
            return
        if self.is_busy(record.path):
            self.status.setText("该照片正在导出，请稍后再请求 Lightroom 渲染。")
            return
        if record.collection_state == "pending_delete":
            QMessageBox.warning(self, "照片待删除", "请先恢复照片或取消待删除状态。")
            return
        if record.lr_sync_state != "synced":
            if record.lr_sync_state != "pending_import":
                self.send_to_lightroom()
            self.status.setText(
                "照片正在发送或尚未进入 Lightroom；导入后完成调整，再次获取编辑版。"
            )
            return
        if any(
            operation["action"] == "render"
            and Path(operation["path"]) == record.path
            and operation["state"] == "pending"
            for operation in self.catalog.pending_operations()
        ):
            self.status.setText("该照片的 Lightroom 渲染请求正在处理中。")
            return
        operation_id = uuid.uuid4().hex
        output_dir = self.bridge.root / "renders" / operation_id
        self.queue_operation(
            "render",
            record.path,
            followup="render",
            output_path=output_dir,
            operation_id=operation_id,
        )
        self.status.setText("已请求 Lightroom 输出 16-bit TIFF 编辑预览。")

    def open_in_photoshop(self) -> None:
        record = self.selected_record()
        if record is None:
            return
        executable = str(self.settings.value("photoshop_path", "")).strip()
        if not executable or not Path(executable).is_file():
            QMessageBox.warning(
                self, "未设置 Photoshop", "请在设置中选择 Photoshop.exe。"
            )
            return
        try:
            subprocess.Popen([executable, str(record.path)], close_fds=True)
        except OSError as error:
            self.show_error(str(error))

    def configure_settings(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("应用设置")
        photoshop = QLineEdit(str(self.settings.value("photoshop_path", "")))
        exiftool = QLineEdit(str(self.settings.value("exiftool_path", "")))
        ps_browse = QPushButton("浏览…")
        exif_browse = QPushButton("浏览…")
        save = QPushButton("保存")

        def browse_to(field: QLineEdit, caption: str) -> None:
            selected, _ = QFileDialog.getOpenFileName(dialog, caption)
            if selected:
                field.setText(selected)

        ps_browse.clicked.connect(lambda: browse_to(photoshop, "选择 Photoshop.exe"))
        exif_browse.clicked.connect(lambda: browse_to(exiftool, "选择 ExifTool"))
        form = QFormLayout(dialog)
        ps_row = QHBoxLayout()
        ps_row.addWidget(photoshop, 1)
        ps_row.addWidget(ps_browse)
        exif_row = QHBoxLayout()
        exif_row.addWidget(exiftool, 1)
        exif_row.addWidget(exif_browse)
        form.addRow("Photoshop.exe", ps_row)
        form.addRow("ExifTool（可选）", exif_row)

        limits = self.catalog.rating_export_limits()
        rating_spins: dict[int, QSpinBox] = {}
        form.addRow(QLabel("各评分档最大边长（像素，保持宽高比）"))
        for rating, default in ((1, 1280), (2, 1920), (3, 2560), (4, 3840)):
            spin = QSpinBox()
            spin.setRange(64, 50000)
            spin.setValue(limits.get(rating) or default)
            rating_spins[rating] = spin
            form.addRow(f"{rating} 分", spin)
        native_5 = QCheckBox("5 分保留原始尺寸")
        native_5.setChecked(limits.get(5) is None)
        size_5 = QSpinBox()
        size_5.setRange(64, 50000)
        size_5.setValue(limits.get(5) or 12000)
        size_5.setEnabled(not native_5.isChecked())
        native_5.toggled.connect(lambda checked: size_5.setEnabled(not checked))
        row_5 = QHBoxLayout()
        row_5.addWidget(native_5)
        row_5.addWidget(size_5)
        form.addRow("5 分", row_5)

        quality = QSpinBox()
        quality.setRange(1, 100)
        quality.setValue(int(self.settings.value("jpeg_quality", 85)))
        form.addRow("JPEG 质量", quality)
        form.addRow(
            QLabel("4 分曝光检查：灰度直方图 0/255 端点柱高达到相邻柱 2 倍时待处理。")
        )
        form.addRow(save)

        def save_settings() -> None:
            self.settings.setValue("photoshop_path", photoshop.text().strip())
            self.settings.setValue("exiftool_path", exiftool.text().strip())
            self.settings.setValue("jpeg_quality", quality.value())
            for rating, spin in rating_spins.items():
                self.catalog.set_rating_export_limit(rating, spin.value())
            self.catalog.set_rating_export_limit(
                5, None if native_5.isChecked() else size_5.value()
            )
            dialog.accept()

        save.clicked.connect(save_settings)
        dialog.exec()

    def prepare_plugin(self) -> None:
        source = (
            Path(__file__).resolve().parent.parent
            / "lightroom_plugin"
            / "PhotoManager.lrplugin"
        )
        if not source.is_dir():
            QMessageBox.warning(self, "找不到插件源码", f"插件目录不存在：{source}")
            return
        destination = (
            Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
            / "LiteGallery"
            / "LightroomPlugin"
            / "PhotoManager.lrplugin"
        )
        try:
            shutil.copytree(source, destination, dirs_exist_ok=True)
            os.startfile(str(destination.parent))
        except OSError as error:
            self.show_error(str(error))
            return
        QMessageBox.information(
            self,
            "Lightroom 插件已准备",
            "在 Lightroom Classic 的 Plug-in Manager 中点击 Add，"
            "选择 LiteGallery\\LightroomPlugin\\PhotoManager.lrplugin。",
        )

    def _legacy_export_selected(self) -> None:
        record = self.selected_record()
        if record is None:
            return
        if record.rating not in range(1, 6):
            QMessageBox.information(self, "先评分", "请先按 1–5 为照片评分。")
            return
        if record.collection_state == "pending_delete":
            QMessageBox.warning(self, "照片待删除", "请先恢复照片再导出。")
            return
        if (
            record.collection_state == "pending_processing"
            and (record.rendered_path is None or not record.rendered_path.is_file())
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
        if record.rating == 4:
            self.status.setText("正在检查 4 分照片的曝光直方图…")
            worker = Worker(lambda: analyze_exposure(source))
            worker.signals.finished.connect(
                lambda check: self.exposure_checked(record, source, check)
            )
            worker.signals.failed.connect(self.show_error)
            self.pool.start(worker)
            return
        self.begin_export(record, source)

    def _legacy_exposure_checked(
        self, record: PhotoRecord, source: Path, check: ExposureCheck
    ) -> None:
        current = self.catalog.photo(record.path) or record
        if check.issue:
            self.catalog.set_collection_state(record.path, "pending_processing")
            self.catalog.set_exposure_issue(record.path, check.issue)
            self.reload_gallery()
            self.select_in_zone(record.path, "pending_processing")
            self.status.setText(
                f"{record.path.name} 检测到{check.issue}直方图尖峰，已转入待处理区，未导出。"
            )
            return
        self.catalog.set_exposure_issue(record.path, None)
        self.begin_export(replace(current, exposure_issue=None), source)

    def _legacy_begin_export(self, record: PhotoRecord, source: Path) -> None:
        limits = self.catalog.rating_export_limits()
        max_edge = limits.get(record.rating)
        quality = int(self.settings.value("jpeg_quality", 85))
        preset = ExportPreset(f"{record.rating} 分", max_edge, quality)

        if (
            source == record.path
            and record.extension in {".jpg", ".jpeg"}
            and record.rating < 5
        ):
            width, height = image_dimensions(record.path)
            if max_edge is not None and max(width, height) <= max_edge:
                QMessageBox.information(
                    self,
                    "尺寸已符合",
                    "这张 JPG 已不超过该评分档的最大边长，无需重新导出。",
                )
                return
        if (
            source == record.path
            and record.extension in {".jpg", ".jpeg"}
            and max_edge is None
        ):
            QMessageBox.information(
                self, "已是原始尺寸", "这张 JPG 已是原始像素尺寸，无需重复导出。"
            )
            return

        destination = record.path.with_name(
            f"{record.path.stem}_score{record.rating}.jpg"
        )
        if destination.exists():
            QMessageBox.warning(
                self, "目标已存在", f"不会覆盖已有文件：{destination.name}"
            )
            return
        recycle_source = record.rating < 5 and (
            record.extension in RAW_EXTENSIONS
            or record.extension in {".jpg", ".jpeg"}
            and max_edge is not None
            and max(image_dimensions(record.path)) > max_edge
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
            return

        exiftool = str(self.settings.value("exiftool_path", "")).strip() or None
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
        worker.signals.failed.connect(self.show_error)
        self.pool.start(worker)

    def _legacy_export_finished(
        self, source_record: PhotoRecord, output: Path, recycle_source: bool
    ) -> None:
        output_record = self.record_for(
            output,
            source_record.root,
            source_record.captured_at,
            rating=source_record.rating,
        )
        try:
            self.catalog.upsert_record(output_record)
            self._cleanup_rendered(source_record.rendered_path)
            if source_record.rendered_path:
                self.catalog.set_rendered_path(source_record.path, None)
            if recycle_source and source_record.lr_sync_state == "synced":
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

    def clear_pending_deletions(self) -> None:
        if self.current_root is None:
            return
        records = self.catalog.photos(
            root=self.current_root, collection_state="pending_delete"
        )
        if not records:
            QMessageBox.information(self, "待删除区为空", "当前文件夹没有待删除照片。")
            return
        answer = QMessageBox.question(
            self,
            "清空待删除区",
            f"将处理当前文件夹中的 {len(records)} 个待删除文件。"
            "已同步的文件会先从 Lightroom Catalog 移除，再进入回收站。继续？",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        pending = 0
        for record in records:
            if self.is_busy(record.path):
                pending += 1
                continue
            if record.lr_sync_state in {"pending_import", "pending_remove"}:
                pending += 1
                continue
            try:
                self._cleanup_rendered(record.rendered_path)
                if record.lr_sync_state == "synced":
                    self.queue_operation("remove", record.path, followup="trash")
                    pending += 1
                    continue
                if record.path.exists():
                    send2trash(str(record.path))
                self.catalog.remove_photo(record.path)
            except Exception as error:
                pending += 1
                self.status.setText(
                    f"回收失败，文件仍保留：{record.path.name}：{error}"
                )
        self.reload_gallery()
        self.status.setText(
            f"已处理待删除项目；{pending} 个等待 Lightroom 确认或需要重试。"
        )

    def queue_operation(
        self,
        action: str,
        path: Path,
        followup: str = "none",
        output_path: Path | None = None,
        operation_id: str | None = None,
    ) -> str:
        operation_id = operation_id or uuid.uuid4().hex
        self.catalog.add_operation(
            operation_id, action, path, followup, output_path
        )
        if action == "import":
            self.catalog.set_sync_state(path, "pending_import")
        elif action == "remove":
            self.catalog.set_sync_state(path, "pending_remove")
        self.bridge.enqueue(action, path, operation_id, output_path)
        return operation_id

    @staticmethod
    def record_for(
        path: Path,
        root: Path,
        captured_at=None,
        rating: int = 0,
    ) -> PhotoRecord:
        stat = path.stat()
        return PhotoRecord(
            path=path.resolve(),
            root=root.resolve(),
            extension=path.suffix.lower(),
            size_bytes=stat.st_size,
            modified_ns=stat.st_mtime_ns,
            captured_at=captured_at or read_capture_time(path),
            rating=rating,
        )

    def _cleanup_rendered(self, path: Path | None) -> None:
        if path is None:
            return
        render_root = (self.bridge.root / "renders").resolve()
        try:
            resolved = path.resolve()
            if not resolved.is_relative_to(render_root) or not resolved.is_file():
                return
            resolved.unlink(missing_ok=True)
            parent = resolved.parent
            if parent != render_root and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            pass

    def process_acks(self) -> None:
        self.dispatch_pending_operations()
        for ack_path, ack in self.bridge.pending_acks():
            operation_id = str(ack.get("operation_id", ""))
            operation = self.catalog.operation(operation_id)
            if operation is None or operation["state"] != "pending":
                self.bridge.acknowledge(ack_path)
                continue
            source = Path(operation["path"])
            action = str(operation["action"])
            if ack.get("status") != "success":
                error = str(ack.get("error", "Lightroom 插件报告操作失败。"))
                try:
                    if action == "import":
                        self.catalog.set_sync_state(source, "error")
                    elif action == "remove":
                        self.catalog.set_sync_state(source, "synced")
                    self.catalog.finish_operation(operation_id, "error", error)
                except Exception:
                    self.status.setText("保存 Lightroom 失败回执时失败；回执保留稍后重试。")
                    continue
                self.status.setText(f"Lightroom 操作失败：{error}")
                self.bridge.acknowledge(ack_path)
                self.reload_gallery()
                continue
            try:
                if action == "import":
                    self.catalog.set_sync_state(source, "synced")
                    self.catalog.finish_operation(operation_id, "success")
                    self.status.setText(f"已同步到 Lightroom：{source.name}")
                elif action == "render":
                    rendered_text = ack.get("rendered_path")
                    rendered = Path(rendered_text) if rendered_text else None
                    if rendered is None or not rendered.is_file():
                        raise FileNotFoundError("Lightroom 没有生成可用的 TIFF。")
                    current = self.catalog.photo(source)
                    if current and current.rendered_path != rendered:
                        self._cleanup_rendered(current.rendered_path)
                    self.catalog.set_rendered_path(source, rendered)
                    self.catalog.set_collection_state(source, "pending_processing")
                    self.catalog.set_exposure_issue(source, None)
                    self.catalog.finish_operation(operation_id, "success")
                    self.status.setText(
                        f"已取得 Lightroom 编辑预览：{source.name}；双击预览后可导出。"
                    )
                    self.bridge.acknowledge(ack_path)
                    self.reload_gallery()
                    self.set_active_zone("pending_processing")
                    QTimer.singleShot(
                        100,
                        lambda source_path=source: self.open_viewer_for_path(
                            source_path, "pending_processing"
                        ),
                    )
                    continue
                elif operation["followup"] in {"trash", "trash_export"}:
                    current = self.catalog.photo(source)
                    if current:
                        self._cleanup_rendered(current.rendered_path)
                    if source.exists():
                        send2trash(str(source))
                    self.catalog.remove_photo(source)
                    self.catalog.finish_operation(operation_id, "success")
                    self.status.setText(f"Lightroom 已移除并回收：{source.name}")
                else:
                    self.catalog.set_sync_state(source, "not_sent")
                    self.catalog.finish_operation(operation_id, "success")
                    self.status.setText(f"已从 Lightroom 移除：{source.name}")
            except Exception as error:
                try:
                    self.catalog.set_sync_state(source, "error")
                    self.catalog.finish_operation(operation_id, "error", str(error))
                except Exception:
                    self.status.setText("保存回执失败；回执保留，稍后重试。")
                    continue
                self.status.setText(f"Lightroom 已处理，但后续文件处理失败：{error}")
            self.bridge.acknowledge(ack_path)
            self.reload_gallery()

    def dispatch_pending_operations(self) -> None:
        for operation in self.catalog.pending_operations():
            try:
                self.bridge.enqueue(
                    operation["action"],
                    Path(operation["path"]),
                    operation["operation_id"],
                    Path(operation["output_path"]) if operation["output_path"] else None,
                )
            except OSError as error:
                self.status.setText(f"Lightroom 请求等待写入：{error}")

    def show_error(self, message: str) -> None:
        self.status.setText(f"操作失败：{message}")
        QMessageBox.critical(self, "操作失败", message)


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("LiteGallery")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
