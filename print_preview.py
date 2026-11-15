"""Native preview of the PDF pages produced by the notebook."""

import os
import re
import shutil
import subprocess

from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtGui import QImage, QPalette, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QSpinBox, QVBoxLayout,
)


_PREVIEW_EDGE = 1440


def _poppler(*args):
    try:
        return subprocess.run(
            args, capture_output=True, check=True, timeout=15, env={**os.environ, "LC_ALL": "C"},
        ).stdout
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError("The PDF renderer could not open the preview.") from error


class PrintPreview(QDialog):
    PRINT = 2
    SAVE_PDF = 3

    def __init__(self, path, parent):
        super().__init__(parent)
        self.setWindowTitle("Print Preview")
        self.resize(760, 820)
        self._path = str(path)
        self._document = None
        self._pixmap = QPixmap()
        count = self._open_document()

        layout = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.previous = QPushButton("Previous")
        self.next_page = QPushButton("Next")
        self.page_number = QSpinBox()
        self.page_number.setAccessibleName("Page number")
        self.page_number.setRange(1, count)
        self.page_number.setKeyboardTracking(False)
        self.zoom = QComboBox()
        self.zoom.setAccessibleName("Preview zoom")
        self.zoom.addItems(["Fit Page", "Fit Width"])
        bar.addWidget(self.previous)
        bar.addWidget(QLabel("Page"))
        bar.addWidget(self.page_number)
        bar.addWidget(QLabel(f"of {count}"))
        bar.addWidget(self.next_page)
        bar.addStretch()
        bar.addWidget(self.zoom)
        layout.addLayout(bar)

        self.sheet = QLabel()
        self.sheet.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scroll = QScrollArea()
        self.scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scroll.viewport().setBackgroundRole(QPalette.ColorRole.Mid)
        self.scroll.setWidget(self.sheet)
        self.scroll.viewport().installEventFilter(self)
        layout.addWidget(self.scroll, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.addButton("Print…", QDialogButtonBox.ButtonRole.ActionRole).clicked.connect(
            lambda: self.done(self.PRINT)
        )
        buttons.addButton("Save PDF…", QDialogButtonBox.ButtonRole.ActionRole).clicked.connect(
            lambda: self.done(self.SAVE_PDF)
        )
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.previous.clicked.connect(lambda: self.page_number.stepBy(-1))
        self.next_page.clicked.connect(lambda: self.page_number.stepBy(1))
        self.page_number.valueChanged.connect(self._show_page)
        self.zoom.currentIndexChanged.connect(self._fit_sheet)
        self._show_page(1)

    def _open_document(self):
        try:
            from PySide6.QtPdf import QPdfDocument
        except ImportError:
            pdfinfo, self._pdftoppm = shutil.which("pdfinfo"), shutil.which("pdftoppm")
            if not pdfinfo or not self._pdftoppm:
                raise RuntimeError("Print preview requires Qt PDF or the Poppler utilities (pdfinfo and pdftoppm).")
            match = re.search(rb"^Pages:\s+(\d+)$", _poppler(pdfinfo, self._path), re.MULTILINE)
            count = int(match.group(1)) if match else 0
        else:
            self._document = QPdfDocument(self)
            if self._document.load(self._path) != QPdfDocument.Error.None_:
                raise RuntimeError("The preview PDF could not be opened.")
            count = self._document.pageCount()
        if count < 1:
            raise RuntimeError("The preview PDF has no readable pages.")
        return count

    def _render_page(self, page):
        if self._document is None:
            return QImage.fromData(_poppler(
                self._pdftoppm, "-f", str(page), "-l", str(page), "-singlefile",
                "-scale-to", str(_PREVIEW_EDGE), "-png", self._path,
            ))
        points = self._document.pagePointSize(page - 1)
        if points.isEmpty():
            return QImage()
        factor = _PREVIEW_EDGE / max(points.width(), points.height())
        size = QSize(max(1, round(points.width() * factor)), max(1, round(points.height() * factor)))
        return self._document.render(page - 1, size)

    def _show_page(self, page):
        self.previous.setEnabled(page > 1)
        self.next_page.setEnabled(page < self.page_number.maximum())
        try:
            image = self._render_page(page)
        except RuntimeError:
            image = QImage()
        self._pixmap = QPixmap.fromImage(image)
        if self._pixmap.isNull():
            self.sheet.setText(f"Could not render page {page}.")
            self.sheet.adjustSize()
        else:
            self._fit_sheet()
        self.scroll.verticalScrollBar().setValue(0)

    def _fit_sheet(self):
        if self._pixmap.isNull():
            return
        view = self.scroll.viewport().size()
        width = max(1, view.width() - 24)
        transform = Qt.TransformationMode.SmoothTransformation
        if self.zoom.currentIndex() == 0:
            height = max(1, view.height() - 24)
            shown = self._pixmap.scaled(width, height, Qt.AspectRatioMode.KeepAspectRatio, transform)
        else:
            shown = self._pixmap.scaledToWidth(width, transform)
        self.sheet.setPixmap(shown)
        self.sheet.resize(shown.size())

    def eventFilter(self, source, event):
        if source is self.scroll.viewport() and event.type() == QEvent.Type.Resize:
            self._fit_sheet()
        return super().eventFilter(source, event)

    def done(self, result):
        if self._document is not None:
            self._document.close()
        super().done(result)
