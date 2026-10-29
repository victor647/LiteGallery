from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QModelIndex, QRect, QSize, QSortFilterProxyModel, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QIcon,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListView,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
)

from .models import PhotoRecord
from .previews import create_preview, preview_cache_path
from .workers import Worker

PATH_ROLE = int(Qt.ItemDataRole.UserRole) + 1
DIRECTORY_ROLE = PATH_ROLE + 1
RECORD_ROLE = PATH_ROLE + 2
THUMBNAIL_ROLE = PATH_ROLE + 3
CARD_SIZE = QSize(170, 85)
MAX_CARD_SIZE = QSize(340, 170)


def _format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


class PhotoCardDelegate(QStyledItemDelegate):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.card_size = CARD_SIZE

    def set_card_size(self, size: QSize) -> None:
        self.card_size = QSize(size)

    def sizeHint(self, option, index) -> QSize:
        return self.card_size

    def paint(self, painter: QPainter, option, index) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        rect = option.rect.adjusted(2, 2, -2, -2)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        painter.setBrush(QColor("#dcecff") if selected else QColor("#f4f5f7"))
        painter.setPen(QPen(QColor("#3f7fc4") if selected else QColor("#d4d7dc"), 1))
        painter.drawRoundedRect(rect, 7, 7)

        thumbnail_rect = QRect(
            rect.x() + 5,
            rect.y() + 5,
            int((rect.width() - 15) * 0.4),
            rect.height() - 10,
        )
        pixmap = index.data(THUMBNAIL_ROLE)
        if not isinstance(pixmap, QPixmap):
            icon = index.data(Qt.ItemDataRole.DecorationRole)
            pixmap = (
                icon.pixmap(thumbnail_rect.size())
                if isinstance(icon, QIcon)
                else QPixmap()
            )
        painter.save()
        painter.setClipRect(thumbnail_rect)
        if not pixmap.isNull():
            scaled = pixmap.scaled(
                thumbnail_rect.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            target_rect = QRect(
                thumbnail_rect.x() + (thumbnail_rect.width() - scaled.width()) // 2,
                thumbnail_rect.y() + (thumbnail_rect.height() - scaled.height()) // 2,
                scaled.width(),
                scaled.height(),
            )
            painter.fillRect(thumbnail_rect, QColor("#d6d9de"))
            painter.drawPixmap(target_rect, scaled)
        else:
            painter.fillRect(thumbnail_rect, QColor("#d6d9de"))
            painter.setPen(QColor("#707781"))
            painter.drawText(
                thumbnail_rect,
                Qt.AlignmentFlag.AlignCenter,
                "文件夹" if index.data(DIRECTORY_ROLE) else "载入缩略图…",
            )
        painter.restore()

        if index.data(DIRECTORY_ROLE):
            path_text = index.data(PATH_ROLE)
            folder_name = str(path_text).replace("/", "\\").rsplit("\\", 1)[-1]
            rows = (folder_name, "子文件夹", "双击进入")
        else:
            record = index.data(RECORD_ROLE)
            captured = (
                record.captured_at.strftime("%Y-%m-%d %H:%M")
                if record and record.captured_at
                else "拍摄时间未知"
            )
            rating = f"{record.rating} 分" if record and record.rating else "未评分"
            if record and record.exposure_issue:
                rating += f" · {record.exposure_issue}"
            rows = (
                captured,
                rating,
                _format_bytes(record.size_bytes) if record else "",
            )

        right_left = thumbnail_rect.right() + 10
        text_rect = QRect(
            right_left,
            rect.y() + 10,
            rect.right() - right_left - 8,
            rect.height() - 20,
        )
        base_font = QFont(painter.font())
        base_font.setPointSize(max(8, base_font.pointSize()))
        painter.setFont(base_font)
        line_height = max(22, text_rect.height() // 3)
        for row, text in enumerate(rows):
            line_rect = QRect(
                text_rect.x(),
                text_rect.y() + row * line_height,
                text_rect.width(),
                line_height,
            )
            painter.setPen(QColor("#252a31") if row != 1 else QColor("#3f5f88"))
            painter.drawText(
                line_rect,
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                painter.fontMetrics().elidedText(
                    text, Qt.TextElideMode.ElideRight, line_rect.width()
                ),
            )
        painter.restore()


class ZoneFilterProxyModel(QSortFilterProxyModel):
    def __init__(self, collection_state: str, parent=None) -> None:
        super().__init__(parent)
        self.collection_state = collection_state

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        source_index = self.sourceModel().index(source_row, 0, source_parent)
        item = self.sourceModel().itemFromIndex(source_index)
        if item is None:
            return False
        if item.data(DIRECTORY_ROLE):
            in_zone = self.collection_state == "retained"
        else:
            record = item.data(RECORD_ROLE)
            in_zone = (
                record is not None
                and record.collection_state == self.collection_state
            )
        return in_zone and super().filterAcceptsRow(source_row, source_parent)


class GalleryView(QListView):
    viewportChanged = Signal()
    focusEntered = Signal()
    ratingRequested = Signal(int)
    processingRequested = Signal()
    deleteRequested = Signal()
    restoreRequested = Signal()

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        self.focusEntered.emit()

    def keyPressEvent(self, event) -> None:
        key = int(event.key())
        if int(Qt.Key.Key_1) <= key <= int(Qt.Key.Key_5):
            self.ratingRequested.emit(key - int(Qt.Key.Key_0))
            event.accept()
            return
        if event.key() == Qt.Key.Key_E:
            self.processingRequested.emit()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Delete:
            self.deleteRequested.emit()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Backspace:
            self.restoreRequested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.viewportChanged.emit()

    def scrollContentsBy(self, dx: int, dy: int) -> None:
        super().scrollContentsBy(dx, dy)
        self.viewportChanged.emit()


class PhotoViewerDialog(QDialog):
    PREVIEW_EDGE = 2200
    QUICK_PREVIEW_EDGE = 320
    ratingRequested = Signal(int)
    processingRequested = Signal()
    deleteRequested = Signal()
    restoreRequested = Signal()

    def __init__(
        self,
        records: list[PhotoRecord],
        start_index: int,
        thread_pool,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.records = records
        self.index = start_index
        self.thread_pool = thread_pool
        self._pending: set[Path] = set()
        self._pixmap = QPixmap()

        self.setWindowTitle("LiteGallery 照片查看器")
        self.resize(1280, 850)
        self.setMinimumSize(640, 480)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self.image = QLabel()
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setStyleSheet("background: #111; color: #ddd;")
        self.image.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.title = QLabel()
        self.position = QLabel()
        self.position.setAlignment(Qt.AlignmentFlag.AlignRight)

        previous = QPushButton("←")
        next_button = QPushButton("→")
        close_button = QPushButton("关闭")
        for button in (previous, next_button, close_button):
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        previous.setToolTip("上一张（←）")
        next_button.setToolTip("下一张（→）")
        previous.clicked.connect(self.show_previous)
        next_button.clicked.connect(self.show_next)
        close_button.clicked.connect(self.accept)

        controls = QHBoxLayout()
        controls.addWidget(previous)
        controls.addWidget(next_button)
        controls.addWidget(self.title, 1)
        controls.addWidget(self.position)
        controls.addWidget(close_button)

        layout = QVBoxLayout(self)
        layout.addLayout(controls)
        layout.addWidget(self.image, 1)
        QShortcut(QKeySequence(Qt.Key.Key_Left), self, activated=self.show_previous)
        QShortcut(QKeySequence(Qt.Key.Key_Right), self, activated=self.show_next)
        self.setFocus()
        self.show_current()

    def keyPressEvent(self, event) -> None:
        key = int(event.key())
        if int(Qt.Key.Key_1) <= key <= int(Qt.Key.Key_5):
            self.ratingRequested.emit(key - int(Qt.Key.Key_0))
            event.accept()
            return
        if event.key() == Qt.Key.Key_E:
            self.processingRequested.emit()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Delete:
            self.deleteRequested.emit()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Backspace:
            self.restoreRequested.emit()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Left:
            self.show_previous()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Right:
            self.show_next()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape:
            self.accept()
            event.accept()
            return
        super().keyPressEvent(event)

    def current_record(self) -> PhotoRecord:
        return self.records[self.index]

    def set_current_record(self, record: PhotoRecord) -> None:
        self.records[self.index] = record
        self._update_title()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.update_image()

    def show_previous(self) -> None:
        if self.records:
            self.index = (self.index - 1) % len(self.records)
            self.show_current()

    def show_next(self) -> None:
        if self.records:
            self.index = (self.index + 1) % len(self.records)
            self.show_current()

    def show_current(self) -> None:
        record = self.records[self.index]
        source = record.rendered_path or record.path
        self._update_title()
        self.position.setText(f"{self.index + 1} / {len(self.records)}")

        try:
            quick_preview = preview_cache_path(source, self.QUICK_PREVIEW_EDGE)
            if quick_preview.is_file():
                self.set_preview(quick_preview)
            else:
                self._pixmap = QPixmap()
                self.image.setText("正在载入预览…")
            cached = preview_cache_path(source, self.PREVIEW_EDGE)
            if cached.is_file():
                self.set_preview(cached)
                self.preload_neighbours()
                return
        except OSError:
            self._pixmap = QPixmap()
            self.image.setText("正在载入预览…")
        self.queue_preview(source)
        self.preload_neighbours()

    def _update_title(self) -> None:
        record = self.records[self.index]
        captured = (
            record.captured_at.strftime("%Y-%m-%d %H:%M")
            if record.captured_at
            else "拍摄时间未知"
        )
        rating = f"{record.rating}★" if record.rating else "未评分"
        issue = f"　待处理：{record.exposure_issue}" if record.exposure_issue else ""
        self.title.setText(f"{record.path.name}　{captured}　{rating}{issue}")

    def queue_preview(self, source: Path) -> None:
        if source in self._pending:
            return
        self._pending.add(source)

        def load() -> tuple[Path, Path | None, str | None]:
            try:
                return source, create_preview(source, self.PREVIEW_EDGE), None
            except Exception as error:
                return source, None, str(error)

        worker = Worker(load)
        worker.signals.finished.connect(self.preview_loaded)
        self.thread_pool.start(worker)

    def preview_loaded(self, result: tuple[Path, Path | None, str | None]) -> None:
        source, image_path, error = result
        self._pending.discard(source)
        if not self.records:
            return
        current = self.records[self.index]
        if (current.rendered_path or current.path) != source:
            return
        if image_path is None:
            self._pixmap = QPixmap()
            self.image.setText(f"无法载入预览：{error}")
            return
        self.set_preview(image_path)

    def preload_neighbours(self) -> None:
        if len(self.records) < 2:
            return
        for offset in (-1, 1):
            record = self.records[(self.index + offset) % len(self.records)]
            source = record.rendered_path or record.path
            if source in self._pending:
                continue
            try:
                cached = preview_cache_path(source, self.PREVIEW_EDGE)
                if cached.is_file():
                    continue
            except OSError:
                continue
            self.queue_preview(source)

    def set_preview(self, path: Path) -> None:
        self._pixmap = QPixmap(str(path))
        self.update_image()

    def update_image(self) -> None:
        if self._pixmap.isNull():
            return
        self.image.setPixmap(
            self._pixmap.scaled(
                self.image.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
