"""Native Qt editor for positioned note content."""

import base64
import binascii
from copy import deepcopy
from html import escape
from io import BytesIO
from math import ceil, floor
import os
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory

from PySide6.QtCore import (
    QByteArray, QBuffer, QEvent, QIODevice, QLineF, QPointF, QRectF, QSize, Qt, Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QImage,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QPixmap,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextListFormat,
    QTextTableFormat,
    QTransform,
)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsItem,
    QGraphicsObject,
    QGraphicsPathItem,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsTextItem,
    QGraphicsView,
    QInputDialog,
    QMenu,
    QMenuBar,
    QMessageBox,
    QStyle,
    QToolBar,
)

from ledger import LedgerItem
from page_setup import PRINT_DPI, PRINT_HEADING_INCHES
from shapes import ShapeItem
from text_styles import (
    TEXT_STYLES, apply_paragraph_style, paragraph_style, paragraph_style_marks,
    restore_paragraph_style_marks,
)


_SELECTION = QColor("#3769d1")
_INK = "#29395b"
_INK_WIDTH = 2.5
_EDITABLE = QGraphicsItem.GraphicsItemFlag
_CHANGE = QGraphicsItem.GraphicsItemChange
_PDF_MAX_PAGES = 25
_PDF_IMAGE_EDGE = 1600
_PDF_DATA_LIMIT = 32 * 1024 * 1024

_BULLET_STYLES = {
    "disc": QTextListFormat.Style.ListDisc,
    "circle": QTextListFormat.Style.ListCircle,
    "square": QTextListFormat.Style.ListSquare,
    "checkbox": QTextListFormat.Style.ListDisc,
}
_MARKER = QTextBlockFormat.MarkerType
_CHECKBOX_MARKERS = (_MARKER.Unchecked, _MARKER.Checked)
_MATH_POSITIONS = {
    "superscript": QTextCharFormat.VerticalAlignment.AlignSuperScript,
    "subscript": QTextCharFormat.VerticalAlignment.AlignSubScript,
}

PAPER_STYLES = {
    "white": "White Paper",
    "legal": "Legal Pad",
    "college": "College Ruled",
    "graph": "Graph Paper",
    "engineering": "Engineering Paper",
    "ledger": "Ledger Paper",
    "dot": "Dot Grid",
}

_PAPER_COLORS = {
    "legal": "#fff6c9",
    "engineering": "#f6fbf2",
    "ledger": "#fffdf2",
}


def _paper_positions(start, end, spacing):
    return range(ceil(start / spacing) * spacing, floor(end / spacing) * spacing + 1, spacing)


def _paper_rules(painter, visible, spacing, color, vertical=False, horizontal=True):
    pen = QPen(QColor(color), 1)
    pen.setCosmetic(True)
    painter.setPen(pen)
    lines = []
    if vertical:
        lines.extend(
            QLineF(x, visible.top(), x, visible.bottom())
            for x in _paper_positions(visible.left(), visible.right(), spacing)
        )
    if horizontal:
        lines.extend(
            QLineF(visible.left(), y, visible.right(), y)
            for y in _paper_positions(visible.top(), visible.bottom(), spacing)
        )
    if lines:
        painter.drawLines(lines)


def _paper_margins(painter, visible, positions, color):
    pen = QPen(QColor(color), 1)
    pen.setCosmetic(True)
    painter.setPen(pen)
    for x in positions:
        if visible.left() <= x <= visible.right():
            painter.drawLine(QLineF(x, visible.top(), x, visible.bottom()))


def _draw_paper_style(painter, visible, style, zoom):
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    match style:
        case "legal":
            _paper_rules(painter, visible, 30, "#b9c7bd")
            _paper_margins(painter, visible, (64, 68), "#d29496")
        case "college":
            _paper_rules(painter, visible, 26, "#c6d9ef")
            _paper_margins(painter, visible, (64,), "#e5a1a7")
        case "graph":
            _paper_rules(painter, visible, 20, "#dce8f3", vertical=True)
            _paper_rules(painter, visible, 100, "#b6cde3", vertical=True)
        case "engineering":
            _paper_rules(painter, visible, 16, "#dce9da", vertical=True)
            _paper_rules(painter, visible, 80, "#aecbaa", vertical=True)
        case "ledger":
            _paper_rules(painter, visible, 28, "#d1dfce")
            _paper_margins(painter, visible, (100, 352, 464, 576), "#a5bda3")
            _paper_margins(painter, visible, (48, 52), "#91b594")
        case "dot":
            spacing = 24 if zoom >= 0.5 else 48
            pen = QPen(QColor("#cad4df"), 2)
            pen.setCosmetic(True)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            xs = _paper_positions(visible.left(), visible.right(), spacing)
            for y in _paper_positions(visible.top(), visible.bottom(), spacing):
                painter.drawPoints([QPointF(x, y) for x in xs])


def _image_record(image):
    if image.isNull():
        raise ValueError("The image could not be read.")
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise ValueError("The image could not be saved to the note.")
    width = min(620, image.width())
    return {
        "type": "image",
        "w": width,
        "h": width * image.height() / image.width(),
        "data": base64.b64encode(bytes(buffer.data())).decode("ascii"),
    }


def _check_pdf_page_count(count):
    if not count:
        raise ValueError("The PDF has no pages to insert.")
    if count > _PDF_MAX_PAGES:
        raise ValueError(
            f"This PDF has {count} pages. Split it into files of up to {_PDF_MAX_PAGES} "
            "pages and insert each file separately."
        )


def _qt_pdf_images(path, document_type):
    document = document_type()
    error = document.load(str(path))
    if error != document_type.Error.None_:
        if error in {
            document_type.Error.IncorrectPassword,
            document_type.Error.UnsupportedSecurityScheme,
        }:
            raise ValueError("Open the PDF with its password and save an unlocked copy to insert.")
        raise ValueError("The PDF could not be opened. Check that it is a valid PDF file.")
    try:
        count = document.pageCount()
        _check_pdf_page_count(count)
        for index in range(count):
            print_size = document.pagePointSize(index)
            edge = max(print_size.width(), print_size.height())
            if min(print_size.width(), print_size.height()) <= 0:
                raise ValueError(f"PDF page {index + 1} has an invalid size.")
            scale = _PDF_IMAGE_EDGE / edge
            size = QSize(
                max(1, round(print_size.width() * scale)),
                max(1, round(print_size.height() * scale)),
            )
            yield document.render(index, size)
    finally:
        document.close()


def _poppler_pdf_images(path):
    pdfinfo = shutil.which("pdfinfo")
    pdftoppm = shutil.which("pdftoppm")
    if not pdfinfo or not pdftoppm:
        raise ValueError("PDF insertion requires PySide6 QtPdf or the Poppler utilities.")
    environment = {**os.environ, "LC_ALL": "C"}
    with TemporaryDirectory(prefix="notebook-pdf-") as folder:
        prefix = Path(folder) / "page"
        try:
            result = subprocess.run(
                [pdfinfo, str(path)], capture_output=True, env=environment, timeout=15
            )
            if result.returncode:
                if b"password" in result.stderr.lower():
                    raise ValueError("Open the PDF with its password and save an unlocked copy to insert.")
                raise ValueError("The PDF could not be opened. Check that it is a valid PDF file.")
            match = re.search(rb"^Pages:\s+(\d+)$", result.stdout, re.MULTILINE)
            if not match:
                raise ValueError("The PDF page count could not be read.")
            count = int(match.group(1))
            _check_pdf_page_count(count)
            result = subprocess.run(
                [pdftoppm, "-png", "-cropbox", "-scale-to", str(_PDF_IMAGE_EDGE), str(path), str(prefix)],
                capture_output=True,
                env=environment,
                timeout=60,
            )
        except subprocess.TimeoutExpired as error:
            raise ValueError("The PDF took too long to render.") from error
        except OSError as error:
            raise ValueError("The PDF renderer could not be started.") from error
        images = sorted(prefix.parent.glob("page-*.png"), key=lambda image: int(image.stem[5:]))
        if result.returncode or len(images) != count:
            raise ValueError("The PDF renderer could not finish all pages.")
        for image in images:
            yield QImage(str(image))


def _pdf_records(path):
    source = Path(path).resolve()
    if not source.is_file():
        raise ValueError("The selected PDF file is unavailable.")
    try:
        from PySide6.QtPdf import QPdfDocument
    except ImportError:
        images = _poppler_pdf_images(source)
    else:
        images = _qt_pdf_images(source, QPdfDocument)
    records = []
    size = 0
    try:
        for index, image in enumerate(images, 1):
            try:
                record = _image_record(image)
            except ValueError as error:
                raise ValueError(f"PDF page {index} could not be rendered.") from error
            size += len(record["data"])
            if size > _PDF_DATA_LIMIT:
                raise ValueError("The rendered PDF is too large to insert in one operation.")
            records.append(record)
    finally:
        images.close()
    return records


def _selection_outline(painter, rectangle):
    pen = QPen(_SELECTION, 1, Qt.PenStyle.DashLine)
    pen.setCosmetic(True)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRect(rectangle)


class _PageItem:
    """Common serialization and movement handling for scene items."""

    def _setup(self, canvas, record):
        self.canvas = canvas
        self.record = deepcopy(record)
        self.setPos(float(record.get("x", 0)), float(record.get("y", 0)))
        self.setFlags(
            _EDITABLE.ItemIsMovable
            | _EDITABLE.ItemIsSelectable
            | _EDITABLE.ItemSendsGeometryChanges
        )

    def _item_changed(self, change, value):
        if change == _CHANGE.ItemPositionHasChanged:
            self.canvas._item_moved()
        return value

    def to_record(self):
        record = deepcopy(self.record)
        record.update(x=self.x(), y=self.y())
        return record


class _TextItem(_PageItem, QGraphicsTextItem):
    def __init__(self, canvas, record):
        super().__init__()
        self.setDefaultTextColor(QColor("#202a3d"))
        self.setFont(QFont("Sans Serif", 12))
        self.setTextWidth(max(40, float(record.get("w", 450))))
        self.setHtml(record.get("html", ""))
        restore_paragraph_style_marks(self.document(), record.get("paragraph_styles"))
        self._setup(canvas, record)
        self.document().contentsChanged.connect(canvas._content_changed)

    def itemChange(self, change, value):
        return super().itemChange(change, self._item_changed(change, value))

    def sceneEvent(self, event):
        handled = super().sceneEvent(event)
        # The document's cursor signal does not track caret motion in a graphics item.
        if self.canvas._editor is self and event.type() in {
            QEvent.Type.KeyPress, QEvent.Type.InputMethod, QEvent.Type.GraphicsSceneContextMenu,
            QEvent.Type.GraphicsSceneMousePress, QEvent.Type.GraphicsSceneMouseMove,
            QEvent.Type.GraphicsSceneMouseRelease, QEvent.Type.GraphicsSceneMouseDoubleClick,
        }:
            self.canvas._update_text_format()
        return handled

    def setTextCursor(self, cursor):
        super().setTextCursor(cursor)
        self.canvas._update_text_format()

    def is_empty(self):
        document = self.document()
        return document.isEmpty() and not _is_checkbox(document.firstBlock())

    def to_record(self):
        record = super().to_record()
        record.update(
            html=self.document().toHtml(),
            w=self.textWidth(),
            h=self.document().size().height(),
        )
        marks = paragraph_style_marks(self.document())
        if marks:
            record["paragraph_styles"] = marks
        else:
            record.pop("paragraph_styles", None)
        return record

    def paint(self, painter, option, widget=None):
        selected = self.isSelected()
        option.state &= ~QStyle.StateFlag.State_Selected
        super().paint(painter, option, widget)
        if selected:
            _selection_outline(painter, self.boundingRect())


def _is_checkbox(block):
    return block.textList() is not None and block.blockFormat().marker() in _CHECKBOX_MARKERS


def _set_marker(block, marker):
    block_format = block.blockFormat()
    if block_format.marker() != marker:
        block_format.setMarker(marker)
        QTextCursor(block).setBlockFormat(block_format)


def _checkbox_hit(block, layout, point):
    bounds = layout.blockBoundingRect(block)
    if not bounds.top() - 2 <= point.y() <= bounds.bottom() + 2:
        return False
    line = block.layout().lineAt(0)
    if not line.isValid():
        return False
    # Follow the laid-out first line so centered and right-to-left tasks also work.
    text = line.naturalTextRect().translated(bounds.topLeft())
    metrics = QFontMetrics(block.charFormat().font(), layout.paintDevice())
    text_list = block.textList()
    if text_list.format().style() in _BULLET_STYLES.values():
        width = metrics.lineSpacing() // 3
    else:
        width = metrics.horizontalAdvance(text_list.itemText(block))
    padding = 2 * (metrics.lineSpacing() // 6)
    space = metrics.horizontalAdvance(" ")
    if block.textDirection() == Qt.LayoutDirection.RightToLeft:
        left = text.right() + space - padding
    else:
        left = text.left() - space - width - padding
    area = QRectF(left, text.top(), width + padding, line.height())
    return area.adjusted(-2, -2, 2, 2).contains(point)


class _InkItem(_PageItem, QGraphicsPathItem):
    def __init__(self, canvas, record):
        super().__init__()
        self._setup(canvas, record)
        self.setPen(
            QPen(
                QColor(record.get("color", _INK)),
                max(0.25, float(record.get("width", _INK_WIDTH))),
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
        )
        self.points = [QPointF(*point) for point in record.get("points", [])]
        if self.points:
            path = QPainterPath(self.points[0])
            if len(self.points) == 1:
                path.lineTo(self.points[0] + QPointF(0.01, 0))
            for point in self.points[1:]:
                path.lineTo(point)
            self.setPath(path)

    def add_point(self, point):
        if self.points and point == self.points[-1]:
            return
        self.points.append(point)
        path = QPainterPath(self.points[0]) if len(self.points) <= 2 else self.path()
        if len(self.points) == 1:
            path.lineTo(point + QPointF(0.01, 0))
        else:
            path.lineTo(point)
        self.setPath(path)

    def shape(self):
        stroker = QPainterPathStroker()
        stroker.setWidth(max(6, self.pen().widthF()))
        stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
        stroker.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        return stroker.createStroke(self.path())

    def itemChange(self, change, value):
        return super().itemChange(change, self._item_changed(change, value))

    def to_record(self):
        record = super().to_record()
        record.update(
            points=[[point.x(), point.y()] for point in self.points],
            w=self.boundingRect().width(),
            h=self.boundingRect().height(),
        )
        return record

    def paint(self, painter, option, widget=None):
        selected = self.isSelected()
        option.state &= ~QStyle.StateFlag.State_Selected
        super().paint(painter, option, widget)
        if selected:
            _selection_outline(painter, self.boundingRect())


def _render_equation(source):
    if not source.strip():
        raise ValueError("Enter a TeX expression.")
    from matplotlib import rc_context
    from matplotlib.mathtext import math_to_image

    expression = source.strip()
    if expression.startswith("$$") and expression.endswith("$$"):
        expression = expression[1:-1]
    elif not (expression.startswith("$") and expression.endswith("$")):
        expression = f"${expression}$"
    output = BytesIO()
    with rc_context({"savefig.transparent": True, "text.usetex": False}):
        math_to_image(expression, output, dpi=180, format="png", color="#202a3d")
    pixmap = QPixmap()
    pixmap.loadFromData(output.getvalue(), "PNG")
    if pixmap.isNull():
        raise ValueError("The equation could not be rendered.")
    return pixmap


class _MediaItem(_PageItem, QGraphicsObject):
    def __init__(self, canvas, record):
        super().__init__()
        self.pixmap = QPixmap()
        self.renderer = None
        self._content_shape = None
        self.width = max(1, float(record.get("w") or 100))
        self.height = max(1, float(record.get("h") or 40))
        self._setup(canvas, record)
        match record["type"]:
            case "image":
                try:
                    self.pixmap.loadFromData(base64.b64decode(record.get("data", "")))
                except (binascii.Error, ValueError):
                    pass
            case "svg":
                self.renderer = QSvgRenderer(QByteArray(record.get("svg", "").encode()), self)
                if self.renderer.isValid():
                    self._content_shape = self._svg_shape()
            case "equation":
                try:
                    self.pixmap = _render_equation(record.get("source", ""))
                except (ValueError, ImportError, RuntimeError):
                    pass
                else:
                    if not record.get("w") or not record.get("h"):
                        self._size_to_equation()

    def _size_to_equation(self):
        self.prepareGeometryChange()
        self.width = max(20, self.pixmap.width() / 1.65)
        self.height = max(10, self.pixmap.height() / 1.65)

    def _svg_shape(self):
        # The alpha mask anchors hit-testing to the ink, even if the source SVG
        # uses a viewBox containing blank space around the visible paths.
        image = QImage(
            max(1, min(900, round(self.width))),
            max(1, min(900, round(self.height))),
            QImage.Format.Format_ARGB32_Premultiplied,
        )
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        self.renderer.render(painter, QRectF(image.rect()))
        painter.end()
        pixmap = QPixmap.fromImage(image)
        image_item = QGraphicsPixmapItem(pixmap)
        image_item.setShapeMode(QGraphicsPixmapItem.ShapeMode.MaskShape)
        shape = image_item.shape()
        if shape.isEmpty():
            return None
        result = QTransform.fromScale(
            self.width / image.width(), self.height / image.height()
        ).map(shape)
        stroker = QPainterPathStroker()
        stroker.setWidth(4.0)
        return result.united(stroker.createStroke(result))

    def set_equation(self, source, pixmap):
        self.record["source"] = source
        self.pixmap = pixmap
        self._size_to_equation()
        self.update()
        self.canvas._content_changed()

    def boundingRect(self):
        return QRectF(0, 0, self.width, self.height)

    def shape(self):
        if self._content_shape is not None:
            return self._content_shape
        return super().shape()

    def itemChange(self, change, value):
        return super().itemChange(change, self._item_changed(change, value))

    def to_record(self):
        record = super().to_record()
        record.update(w=self.width, h=self.height)
        return record

    def paint(self, painter, option, widget=None):
        bounds = self.boundingRect()
        if self.renderer is not None and self.renderer.isValid():
            self.renderer.render(painter, bounds)
        elif not self.pixmap.isNull():
            source = QRectF(self.pixmap.rect())
            target = bounds
            if self.record["type"] == "equation":
                scale = min(bounds.width() / source.width(), bounds.height() / source.height())
                width, height = source.width() * scale, source.height() * scale
                target = QRectF(0, (bounds.height() - height) / 2, width, height)
            painter.drawPixmap(target, self.pixmap, source)
        elif self.record["type"] == "equation":
            painter.setPen(QColor("#29395b"))
            painter.drawText(bounds, Qt.AlignmentFlag.AlignVCenter, self.record.get("source", ""))
        else:
            painter.fillRect(bounds, QColor("#f2f4f8"))
            painter.setPen(QColor("#48556b"))
            painter.drawText(bounds, Qt.AlignmentFlag.AlignCenter, "Image unavailable")
        if self.isSelected():
            _selection_outline(painter, bounds)


class PageCanvas(QGraphicsView):
    changed = Signal()
    tool_changed = Signal(str)
    zoom_changed = Signal(float)
    link_requested = Signal(str)
    text_font_changed = Signal(str)
    text_style_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setMinimumSize(240, 220)
        self.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
        )
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self._loading = False
        self._tool = "select"
        self._zoom = 1.0
        self.pen_color = QColor(_INK)
        self.pen_width = _INK_WIDTH
        self._stroke = None
        self._erase_previous = None
        self._pressed_link = None
        self._pressed_checkbox = None
        self._text_click = None
        self._editor = None
        self._draft_text = None
        self._text_font = ""
        self._text_style = ""
        self._items = []
        self._undo_stack = []
        self._snapshot = []
        self._undo_gesture = False
        self.paper_style = "white"
        self._page_rect = QRectF()
        self.scene().selectionChanged.connect(self._update_text_format)
        QApplication.instance().focusChanged.connect(self._check_draft_focus, Qt.ConnectionType.QueuedConnection)
        self._refresh_bounds()

    def set_page(self, objects):
        self._restore_objects(objects)
        self._undo_stack.clear()
        self._undo_gesture = False
        self._snapshot = self.objects()
        self.horizontalScrollBar().setValue(self.horizontalScrollBar().minimum())
        self.verticalScrollBar().setValue(self.verticalScrollBar().minimum())

    def _restore_objects(self, objects):
        self._loading = True
        try:
            self._finish_text()
            self._stroke = None
            self._erase_previous = None
            self._pressed_link = None
            self._pressed_checkbox = None
            self._text_click = None
            self.scene().clear()
            self._items.clear()
            for record in objects:
                self._add_item(record)
            self._refresh_bounds()
        finally:
            self._loading = False

    def objects(self):
        return [item.to_record() for item in self._items if not self._is_empty_draft(item)]

    def _record_undo(self):
        if self._undo_gesture:
            return
        current = self.objects()
        if current != self._snapshot:
            self._undo_stack.append(self._snapshot)
            if len(self._undo_stack) > 20:
                self._undo_stack.pop(0)
            self._snapshot = current

    def undo(self):
        if self._undo_gesture or not self._undo_stack:
            return
        horizontal = self.horizontalScrollBar()
        vertical = self.verticalScrollBar()
        scroll = horizontal.value(), vertical.value()
        editor_index = self._items.index(self._editor) if self._editor is not None else None
        cursor_position = self._editor.textCursor().position() if self._editor is not None else 0
        self._restore_objects(self._undo_stack.pop())
        self._snapshot = self.objects()
        if editor_index is not None and editor_index < len(self._items):
            item = self._items[editor_index]
            if isinstance(item, _TextItem):
                self._edit_text(item)
                cursor = item.textCursor()
                cursor.setPosition(min(cursor_position, item.document().characterCount() - 1))
                item.setTextCursor(cursor)
        horizontal.setValue(scroll[0])
        vertical.setValue(scroll[1])
        self.changed.emit()

    def _add_item(self, record):
        kind = record["type"]
        if kind == "text":
            item = _TextItem(self, record)
        elif kind == "ink":
            item = _InkItem(self, record)
        elif kind == "shape":
            item = ShapeItem(self, record)
        elif kind == "ledger":
            item = LedgerItem(self, record)
        elif kind in {"image", "equation", "svg"}:
            item = _MediaItem(self, record)
        else:
            raise ValueError(f"Unknown page object {kind!r}")
        item.setZValue(len(self._items))
        self.scene().addItem(item)
        self._items.append(item)
        return item

    def _refresh_bounds(self):
        content = self.scene().itemsBoundingRect()
        min_width = max(690, self.viewport().width() - 48)
        self._page_rect = QRectF(
            min(0, content.left() - 24),
            min(0, content.top() - 24),
            1,
            1,
        ).united(QRectF(0, 0, max(min_width, content.right() + 90), max(900, content.bottom() + 90)))
        self.setSceneRect(self._page_rect.adjusted(-20, -20, 24, 36))
        self.viewport().update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh_bounds()

    def drawBackground(self, painter, rectangle):
        painter.fillRect(rectangle, QColor("#eff2f7"))
        paper = self._page_rect.intersected(rectangle)
        if paper.isEmpty():
            return
        style = self.paper_style
        painter.fillRect(paper, QColor(_PAPER_COLORS.get(style, "#ffffff")))
        if style != "white":
            painter.save()
            painter.setClipRect(self._page_rect, Qt.ClipOperation.IntersectClip)
            _draw_paper_style(painter, paper, style, self._zoom)
            painter.restore()
        pen = QPen(QColor("#dce2ee"), 1)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.drawRect(self._page_rect)

    def set_paper_style(self, style):
        if style not in PAPER_STYLES:
            raise ValueError(f"Unknown paper style {style!r}")
        if self.paper_style != style:
            self.paper_style = style
            self.viewport().update()

    def print_page(self, printer, title, timestamp=""):
        """Paint the whole note, including offscreen content, to a paged device."""
        scene = self.scene()
        focused = scene.focusItem()
        selected = scene.selectedItems()
        ledgers = [item for item in self._items if isinstance(item, LedgerItem)]
        prepared = []
        try:
            for ledger in ledgers:
                ledger.begin_print()
                prepared.append(ledger)
            return self._paint_print_pages(printer, title, timestamp)
        finally:
            for ledger in reversed(prepared):
                ledger.end_print()
            for item in selected:
                item.setSelected(True)
            if focused is not None:
                scene.setFocusItem(focused)

    def _paint_print_pages(self, printer, title, timestamp):
        dpi = printer.resolution()
        layout = printer.pageLayout()
        origin = QPointF()
        if layout.mode() == layout.Mode.FullPageMode:
            # In full-page mode the device origin is the physical paper corner.
            layout.setMode(layout.Mode.StandardMode)
            origin = QPointF(layout.paintRectPixels(dpi).topLeft())
        # Keep page breaks independent of the output device's resolution.
        paint_area = QRectF(layout.paintRectPixels(PRINT_DPI))
        paint_area.moveTopLeft(QPointF())
        heading_height = PRINT_DPI * PRINT_HEADING_INCHES
        body = paint_area.adjusted(0, heading_height, 0, 0)
        if body.isEmpty() or dpi <= 0:
            raise RuntimeError("The selected paper size has no space for the note.")

        scene = self.scene()
        bounds = scene.itemsBoundingRect()
        if self._is_empty_draft(self._draft_text):
            bounds = QRectF()
            for item in scene.items():
                if item is not self._draft_text:
                    bounds = bounds.united(item.sceneBoundingRect())
        left = min(0, bounds.left() - 24)
        top = min(0, bounds.top() - 24)
        width = max(690, bounds.right() + 24) - left
        bottom = max(top + 1, bounds.bottom() + 24)
        scale = body.width() / width
        page_height = body.height() / scale
        count = ceil((bottom - top) / page_height)
        ranges = printer.pageRanges()
        pages = range(1, count + 1)
        if not ranges.isEmpty():
            pages = [
                number
                for selection in ranges.toRangeList()
                for number in range(max(1, selection.from_), min(count, selection.to) + 1)
            ]
            if not pages:
                raise RuntimeError("The selected print range does not contain any note pages.")

        painter = QPainter()
        if not painter.begin(printer):
            raise RuntimeError("The printer or PDF destination could not be opened.")
        editor = self._editor
        cursor = editor.textCursor() if editor is not None else None
        try:
            scene.clearSelection()
            scene.clearFocus()
            if cursor is not None and cursor.hasSelection():
                preview_cursor = QTextCursor(cursor)
                preview_cursor.clearSelection()
                editor.setTextCursor(preview_cursor)
            painter.setRenderHints(
                QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
            )
            for sheet, page in enumerate(pages):
                if sheet and not printer.newPage():
                    raise RuntimeError("The printer could not start the next page.")
                source = QRectF(left, top + page_height * (page - 1), width, page_height)
                painter.save()
                painter.translate(origin)
                painter.scale(dpi / PRINT_DPI, dpi / PRINT_DPI)
                self._paint_print_heading(painter, title, timestamp, page, count, heading_height, paint_area)
                painter.save()
                painter.setClipRect(body, Qt.ClipOperation.IntersectClip)
                painter.translate(body.topLeft())
                painter.scale(scale, scale)
                painter.translate(-source.topLeft())
                painter.fillRect(source, QColor(_PAPER_COLORS.get(self.paper_style, "#ffffff")))
                _draw_paper_style(painter, source, self.paper_style, 1.0)
                painter.restore()
                painter.save()
                painter.setClipRect(body, Qt.ClipOperation.IntersectClip)
                scene.render(painter, body, source)
                painter.restore()
                painter.restore()
        finally:
            finished = painter.end()
            if cursor is not None:
                editor.setTextCursor(cursor)
        if not finished:
            raise RuntimeError("The print job could not be completed.")
        return True

    @staticmethod
    def _paint_print_heading(painter, title, timestamp, page, count, height, area):
        painter.save()
        painter.setPen(QColor("#202a3d"))
        page_text = f"{page} / {count}" if count > 1 else ""
        font = QFont(painter.font())
        font.setPixelSize(round(10 / 72 * PRINT_DPI))
        painter.setFont(font)
        number_width = painter.fontMetrics().horizontalAdvance(page_text)
        title_height = height * (0.65 if timestamp else 0.8)
        rectangle = QRectF(area.left(), area.top(), area.width(), title_height)
        painter.drawText(rectangle, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                         page_text)
        font.setPixelSize(round(16 / 72 * PRINT_DPI))
        font.setBold(True)
        painter.setFont(font)
        title_width = max(0, int(area.width() - number_width - height / 2))
        title = painter.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, title_width)
        painter.drawText(rectangle, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         title)
        if timestamp:
            font.setPixelSize(round(8 / 72 * PRINT_DPI))
            font.setBold(False)
            painter.setFont(font)
            painter.setPen(QColor("#526178"))
            date_area = QRectF(area.left(), area.top() + title_height, area.width(), height - title_height)
            date = painter.fontMetrics().elidedText(timestamp, Qt.TextElideMode.ElideRight, int(area.width()))
            painter.drawText(date_area, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, date)
        painter.restore()

    def _content_changed(self):
        if not self._loading:
            self._record_undo()
            self._refresh_bounds()
            self.changed.emit()

    def _item_moved(self):
        if not self._loading:
            self._record_undo()
            # Keep the coordinate system stationary during the drag.
            self.changed.emit()

    def _insertion_point(self, width, height):
        visible = self.mapToScene(self.viewport().rect()).boundingRect()
        x = max(48, visible.left() + 44)
        y = max(54, visible.top() + 46)
        for item in sorted(self._items, key=lambda item: item.sceneBoundingRect().top()):
            bounds = item.sceneBoundingRect()
            if bounds.intersects(QRectF(x, y, width, height)):
                y = bounds.bottom() + 18
        return QPointF(x, y)

    def _select(self, item, *, reveal=True):
        self.scene().clearSelection()
        item.setSelected(True)
        if reveal:
            self.ensureVisible(item, 24, 24)
        self.setFocus()

    def _text_format_item(self):
        if self._editor is not None:
            return self._editor
        return next((item for item in self.scene().selectedItems() if isinstance(item, _TextItem)), None)

    def text_font_family(self):
        item = self._text_format_item()
        if item is None:
            return ""
        return item.textCursor().charFormat().font().family()

    def text_style_at_cursor(self):
        item = self._text_format_item()
        return paragraph_style(item.textCursor().block()) if item is not None else ""

    def _update_text_format(self):
        family = self.text_font_family()
        if family != self._text_font:
            self._text_font = family
            self.text_font_changed.emit(family)
        style = self.text_style_at_cursor()
        if style != self._text_style:
            self._text_style = style
            self.text_style_changed.emit(style)

    def _edit_text(self, item, *, reveal=True):
        if self._editor is not item:
            self._finish_text()
        self._select(item, reveal=reveal)
        item.setFlag(_EDITABLE.ItemIsMovable, False)
        item.setTextInteractionFlags(Qt.TextInteractionFlag.TextEditorInteraction)
        item.setFocus(Qt.FocusReason.MouseFocusReason)
        self._editor = item
        self._update_text_format()

    def _finish_text(self):
        item = self._editor
        if item is None:
            return
        self._editor = None
        discard = self._is_empty_draft(item)
        if self._draft_text is item:
            self._draft_text = None
        item.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        item.setFlag(_EDITABLE.ItemIsMovable, True)
        item.clearFocus()
        if discard:
            self._items.remove(item)
            self.scene().removeItem(item)
            self._refresh_bounds()
        self._update_text_format()

    def _is_empty_draft(self, item):
        return item is not None and item is self._draft_text and item.is_empty()

    def _check_draft_focus(self):
        if not self._is_empty_draft(self._editor) or self._editor.hasFocus():
            return
        # Formatting and insert controls act on the current text cursor.
        if QApplication.activePopupWidget() or QApplication.activeModalWidget():
            return
        focus = QApplication.focusWidget()
        while focus is not None:
            if isinstance(focus, (QToolBar, QMenuBar)):
                return
            focus = focus.parentWidget()
        self._finish_text()

    def set_tool(self, tool):
        if tool not in {"select", "text", "ink", "erase"}:
            raise ValueError(f"Unknown canvas tool {tool!r}")
        self._gesture_finished()
        self._pressed_link = None
        self._pressed_checkbox = None
        if tool != "select":
            self._finish_text()
            self.scene().clearSelection()
        if tool != self._tool:
            self._tool = tool
            self.tool_changed.emit(tool)
        cursors = {
            "select": Qt.CursorShape.ArrowCursor,
            "text": Qt.CursorShape.IBeamCursor,
            "ink": Qt.CursorShape.CrossCursor,
            "erase": Qt.CursorShape.PointingHandCursor,
        }
        self.viewport().setCursor(cursors[tool])
        self.setFocus()

    def viewportEvent(self, event):
        if event.type() == QEvent.Type.UngrabMouse and hasattr(self, "_stroke"):
            self._gesture_finished()
            self._pressed_link = None
            self._pressed_checkbox = None
            self._text_click = None
            self._undo_gesture = False
            self._record_undo()
        return super().viewportEvent(event)

    def add_text(self, position=None, html=""):
        self.set_tool("select")
        self._finish_text()
        reveal = position is None
        position = self._insertion_point(420, 30) if reveal else position
        item = self._add_item(
            {"type": "text", "x": position.x(), "y": position.y(), "w": 420, "html": html}
        )
        if item.is_empty():
            self._draft_text = item
            self._refresh_bounds()
        else:
            self._content_changed()
        self._edit_text(item, reveal=reveal)

    def insert_symbol(self, symbol):
        if self._editor is None:
            self.add_text()
        cursor = self._editor.textCursor()
        cursor.insertText(symbol)
        self._editor.setTextCursor(cursor)
        self.setFocus()
        self._editor.setFocus()

    def selected_text(self):
        if self._editor is None:
            return ""
        return self._editor.textCursor().selectedText()

    def insert_link(self, url, label=""):
        label = label.strip()
        was_grouped = self._undo_gesture
        self._undo_gesture = True
        try:
            if self._editor is None:
                item = next((item for item in self.scene().selectedItems()
                             if isinstance(item, _TextItem)), None)
                if item is None:
                    self.add_text()
                else:
                    self._edit_text(item)
            cursor = self._editor.textCursor()
            selected = cursor.selectedText()
            normal = QTextCharFormat(cursor.charFormat())
            if normal.isAnchor():
                normal.setAnchor(False)
                normal.setAnchorHref("")
                normal.setAnchorNames([])
                normal.setFontUnderline(False)
                normal.clearForeground()

            link = QTextCharFormat()
            link.setAnchor(True)
            link.setAnchorHref(url)
            link.setFontUnderline(True)
            link.setForeground(QColor("#275fc2"))
            if selected and label in ("", selected):
                cursor.mergeCharFormat(link)
                cursor.setPosition(cursor.selectionEnd())
            else:
                replacement = QTextCharFormat(normal)
                replacement.merge(link)
                cursor.insertText(label or url, replacement)
            cursor.setCharFormat(normal)
            self._editor.setTextCursor(cursor)
            self.setFocus()
            self._editor.setFocus()
        finally:
            self._undo_gesture = was_grouped
            self._record_undo()

    def insert_table(self, rows, columns):
        if self._editor is None:
            self.add_text()
        cursor = self._editor.textCursor()
        table_format = QTextTableFormat()
        table_format.setBorder(1)
        table_format.setBorderCollapse(True)
        table_format.setCellPadding(6)
        table_format.setCellSpacing(0)
        cursor.beginEditBlock()
        cursor.clearSelection()
        table = cursor.insertTable(rows, columns, table_format)
        cursor.endEditBlock()
        self._editor.setTextCursor(table.cellAt(0, 0).firstCursorPosition())
        self.setFocus()
        self._editor.setFocus()

    def set_format(self, style, value=None):
        item = self._text_format_item()
        if item is None:
            return
        if self._editor is None:
            self._edit_text(item)
        cursor = item.textCursor()
        cursor.beginEditBlock()
        try:
            if style == "alignment":
                block_format = QTextBlockFormat()
                block_format.setAlignment(value)
                cursor.mergeBlockFormat(block_format)
            elif style == "bullet":
                self._set_list_style(cursor, _BULLET_STYLES[value or "disc"], checkbox=value == "checkbox")
            elif style == "number":
                self._set_list_style(cursor, QTextListFormat.Style.ListDecimal)
            elif style in {"indent", "outdent"}:
                self._change_indent(cursor, 1 if style == "indent" else -1)
            else:
                self._set_char_format(cursor, style, value)
        finally:
            cursor.endEditBlock()
        item.setTextCursor(cursor)
        self.setFocus()
        item.setFocus()

    def apply_text_style(self, style):
        if style not in TEXT_STYLES:
            raise ValueError(f"Unknown text style {style!r}")
        item = self._text_format_item()
        if item is None:
            return
        if self._editor is None:
            self._edit_text(item)
        cursor = item.textCursor()
        position, anchor = cursor.position(), cursor.anchor()
        blocks = self._style_blocks(cursor)
        cursor.beginEditBlock()
        try:
            for block in blocks:
                apply_paragraph_style(block, style)
        finally:
            cursor.endEditBlock()
        # Start a fresh cursor so a pending inline format cannot obscure the
        # applied paragraph style at an unchanged caret position.
        cursor = QTextCursor(item.document())
        cursor.setPosition(anchor)
        cursor.setPosition(position, QTextCursor.MoveMode.KeepAnchor)
        if not cursor.hasSelection() and not cursor.block().text():
            cursor.setCharFormat(cursor.block().charFormat())
        item.setTextCursor(cursor)
        self.setFocus()
        item.setFocus()

    def _style_blocks(self, cursor):
        if not cursor.hasComplexSelection():
            return self._selected_blocks(cursor)
        row, row_count, column, column_count = cursor.selectedTableCells()
        table = cursor.currentTable()
        blocks = {}
        for r in range(row, row + row_count):
            for c in range(column, column + column_count):
                cell = table.cellAt(r, c)
                block = cell.firstCursorPosition().block()
                last = cell.lastCursorPosition().position()
                while block.isValid() and block.position() <= last:
                    blocks[block.position()] = block
                    block = block.next()
        return blocks.values()

    @staticmethod
    def _set_char_format(cursor, style, value):
        current = cursor.charFormat()
        char_format = QTextCharFormat()
        if style == "bold":
            weight = QFont.Weight.Normal if current.fontWeight() >= QFont.Weight.Bold else QFont.Weight.Bold
            char_format.setFontWeight(weight)
        elif style == "italic":
            char_format.setFontItalic(not current.fontItalic())
        elif style == "underline":
            char_format.setFontUnderline(not current.fontUnderline())
        elif style == "strike":
            char_format.setFontStrikeOut(not current.fontStrikeOut())
        elif style == "font":
            char_format.setFontFamilies([value])
        elif style == "size":
            char_format.setFontPointSize(float(value))
        elif style == "color":
            char_format.setForeground(value)
        elif style == "highlight":
            char_format.setBackground(value)
        elif style in _MATH_POSITIONS:
            target = _MATH_POSITIONS[style]
            char_format.setVerticalAlignment(
                QTextCharFormat.VerticalAlignment.AlignNormal
                if current.verticalAlignment() == target else target
            )
        cursor.mergeCharFormat(char_format)

    @staticmethod
    def _selected_blocks(cursor):
        block = cursor.document().findBlock(cursor.selectionStart())
        blocks = [block]
        block = block.next()
        while block.isValid() and block.position() < cursor.selectionEnd():
            blocks.append(block)
            block = block.next()
        return blocks

    def _set_list_style(self, cursor, style, *, checkbox=False):
        blocks = list(self._style_blocks(cursor))
        lists = [block.textList() for block in blocks]
        if all(
            text_list is not None and text_list.format().style() == style and _is_checkbox(block) == checkbox
            for block, text_list in zip(blocks, lists)
        ):
            for block in blocks:
                self._remove_from_list(block)
            return

        indents = [
            text_list.format().indent() if text_list else block.blockFormat().indent() + 1
            for block, text_list in zip(blocks, lists)
        ]
        active = None
        previous_indent = None
        for block, indent in zip(blocks, indents):
            marker = _MARKER.NoMarker
            if checkbox:
                marker = block.blockFormat().marker() if _is_checkbox(block) else _MARKER.Unchecked
                legacy = re.match(r"[ \t\u00a0]*([☐☑☒])(?:[ \t\u00a0]+|$)", block.text())
                if legacy:
                    if legacy[1] in ("☑", "☒"):
                        marker = _MARKER.Checked
                    edit = QTextCursor(block)
                    edit.setPosition(block.position() + legacy.end(), QTextCursor.MoveMode.KeepAnchor)
                    edit.removeSelectedText()
            if indent != previous_indent:
                list_format = QTextListFormat()
                list_format.setStyle(style)
                list_format.setIndent(indent)
                active = QTextCursor(block).createList(list_format)
                previous_indent = indent
            else:
                active.add(block)
            _set_marker(block, marker)

    @staticmethod
    def _remove_from_list(block):
        text_list = block.textList()
        indent = max(0, text_list.format().indent() - 1)
        text_list.remove(block)
        block_format = block.blockFormat()
        block_format.setIndent(indent)
        block_format.setMarker(_MARKER.NoMarker)
        QTextCursor(block).setBlockFormat(block_format)

    @staticmethod
    def _set_block_indent(block, indent):
        block_format = QTextBlockFormat()
        block_format.setIndent(max(0, indent))
        QTextCursor(block).mergeBlockFormat(block_format)

    def _change_indent(self, cursor, step):
        blocks = self._selected_blocks(cursor)
        # Capture formats before moving any blocks, which may empty existing lists.
        formats = [
            block.textList().format() if block.textList() else None for block in blocks
        ]
        active = None
        active_format = None
        for block, list_format in zip(blocks, formats):
            if list_format is None:
                self._set_block_indent(block, block.blockFormat().indent() + step)
                active = None
                continue
            list_format.setIndent(list_format.indent() + step)
            if list_format.indent() <= 0:
                self._remove_from_list(block)
                self._set_block_indent(block, 0)
                active = None
            elif active is not None and active_format == list_format:
                active.add(block)
            else:
                previous = block.previous().textList()
                if previous is not None and previous.format() == list_format:
                    previous.add(block)
                    active = previous
                else:
                    active = QTextCursor(block).createList(list_format)
                active_format = list_format

    def _equation_prompt(self, existing=""):
        source = existing
        while True:
            source, accepted = QInputDialog.getText(
                self, "Equation", "TeX expression", text=source
            )
            if not accepted:
                return None
            try:
                return source, _render_equation(source)
            except (ValueError, ImportError, RuntimeError) as error:
                QMessageBox.warning(self, "Equation could not be rendered", str(error))

    def add_equation(self):
        result = self._equation_prompt()
        if result is None:
            return
        source, pixmap = result
        self.set_tool("select")
        self._finish_text()
        width = max(20, pixmap.width() / 1.65)
        height = max(10, pixmap.height() / 1.65)
        point = self._insertion_point(width, height)
        item = self._add_item({
            "type": "equation",
            "x": point.x(),
            "y": point.y(),
            "w": width,
            "h": height,
            "source": source,
        })
        self._content_changed()
        self._select(item)

    def paste_image(self, image=None):
        image = QApplication.clipboard().image() if image is None else image
        try:
            record = _image_record(image)
        except ValueError:
            return False
        self._insert_images([record])
        return True

    def insert_pdf(self, path):
        """Render all PDF pages before inserting any images into the note."""
        records = _pdf_records(path)
        self._insert_images(records)
        return len(records)

    def _insert_images(self, records):
        width = max(record["w"] for record in records)
        height = sum(record["h"] for record in records) + 24 * (len(records) - 1)
        self.set_tool("select")
        self._finish_text()
        point = self._insertion_point(width, height)
        items = []
        y = point.y()
        for record in records:
            items.append(self._add_item({**record, "x": point.x(), "y": y}))
            y += record["h"] + 24
        self._content_changed()
        self._select(items[0])

    def add_shape(self, shape):
        self.set_tool("select")
        self._finish_text()
        width, height = {
            "square": (100, 100), "circle": (100, 100),
            "line": (140, 20), "arrow": (140, 20), "axes": (120, 120),
        }.get(shape, (140, 90))
        point = self._insertion_point(width, height)
        item = self._add_item({
            "type": "shape", "shape": shape, "x": point.x(), "y": point.y(),
            "w": width, "h": height, "rotation": 0,
            "color": self.pen_color.name(QColor.NameFormat.HexArgb),
            "width": self.pen_width,
        })
        self._content_changed()
        self._select(item)

    def delete_selected(self):
        if self._editor is not None:
            return False
        items = self.scene().selectedItems()
        if not items:
            return False
        self._remove_items(items)
        return True

    def _remove_items(self, items):
        for item in items:
            self.scene().removeItem(item)
            self._items.remove(item)
        self._content_changed()

    def _erase(self, point):
        if self._erase_previous is None or point == self._erase_previous:
            area = QPainterPath()
            area.addEllipse(point, 5 / self._zoom, 5 / self._zoom)
        else:
            path = QPainterPath(self._erase_previous)
            path.lineTo(point)
            stroker = QPainterPathStroker()
            stroker.setWidth(10 / self._zoom)
            stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
            area = stroker.createStroke(path)
        items = [
            item for item in self.scene().items(area)
            if isinstance(item, (_InkItem, _MediaItem)) and item.record["type"] in {"ink", "svg"}
        ]
        if items:
            self._remove_items(items)
        self._erase_previous = point

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        self._text_click = None
        self._pressed_checkbox = None
        position = event.position().toPoint()
        modifiers = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier
        if self._tool == "select" and event.modifiers() & modifiers:
            url = self._link_at(position)
            if url:
                self._pressed_link = (url, position)
                event.accept()
                return
        if self._tool == "select" and (checkbox := self._checkbox_at(position, include_native=True)) is not None:
            self._pressed_checkbox = (checkbox, position)
            event.accept()
            return
        self._undo_gesture = True
        point = self.mapToScene(position)
        if self._tool == "text":
            self.add_text(point)
        elif self._tool == "ink":
            self._stroke = self._add_item({
                "type": "ink", "x": point.x(), "y": point.y(),
                "points": [[0, 0]],
                "width": self.pen_width,
                "color": self.pen_color.name(QColor.NameFormat.HexArgb),
            })
        elif self._tool == "erase":
            self._erase(point)
        else:
            item = self.itemAt(position)
            if self._editor is not None and item is not self._editor:
                self._finish_text()
            if item is None and self._page_rect.contains(point) and not event.modifiers():
                self._text_click = point
            super().mousePressEvent(event)
            return
        event.accept()

    def mouseMoveEvent(self, event):
        if self._pressed_link is not None or self._pressed_checkbox is not None:
            event.accept()
            return
        point = self.mapToScene(event.position().toPoint())
        if self._text_click is not None:
            distance = (event.position().toPoint() - self.mapFromScene(self._text_click)).manhattanLength()
            if distance >= QApplication.startDragDistance():
                self._text_click = None
        if self._stroke is not None:
            self._stroke.add_point(point - self._stroke.pos())
        elif self._erase_previous is not None:
            self._erase(point)
        else:
            super().mouseMoveEvent(event)
            return
        event.accept()

    def mouseReleaseEvent(self, event):
        if self._pressed_checkbox is not None and event.button() == Qt.MouseButton.LeftButton:
            checkbox, start = self._pressed_checkbox
            self._pressed_checkbox = None
            position = event.position().toPoint()
            if (position - start).manhattanLength() < QApplication.startDragDistance():
                if self._checkbox_at(position) == checkbox:
                    item, block_position = checkbox
                    block = item.document().findBlock(block_position)
                    checked = block.blockFormat().marker() == _MARKER.Checked
                    _set_marker(block, _MARKER.Unchecked if checked else _MARKER.Checked)
            event.accept()
            return
        if self._pressed_link is not None and event.button() == Qt.MouseButton.LeftButton:
            url, start = self._pressed_link
            self._pressed_link = None
            position = event.position().toPoint()
            if (position - start).manhattanLength() < QApplication.startDragDistance():
                if self._link_at(position) == url:
                    self.link_requested.emit(url)
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self._text_click is not None:
            point, self._text_click = self._text_click, None
            distance = (event.position().toPoint() - self.mapFromScene(point)).manhattanLength()
            if distance < QApplication.startDragDistance() and self._tool == "select":
                super().mouseReleaseEvent(event)
                self._undo_gesture = False
                self.add_text(point)
                event.accept()
                return
        if self._stroke is not None:
            point = self.mapToScene(event.position().toPoint())
            self._stroke.add_point(point - self._stroke.pos())
            self._gesture_finished()
            event.accept()
        elif self._erase_previous is not None:
            self._erase_previous = None
            event.accept()
        else:
            super().mouseReleaseEvent(event)
            self._refresh_bounds()
        if event.button() == Qt.MouseButton.LeftButton:
            self._undo_gesture = False
            self._record_undo()

    def _gesture_finished(self):
        if self._stroke is not None:
            self._stroke = None
            self._content_changed()
        self._erase_previous = None

    def _link_at(self, position):
        item = self.itemAt(position)
        if isinstance(item, _TextItem):
            scene_position = self.mapToScene(position)
            return item.document().documentLayout().anchorAt(item.mapFromScene(scene_position))
        return ""

    def _checkbox_at(self, position, *, include_native=False):
        item = self.itemAt(position)
        if isinstance(item, _TextItem):
            point = item.mapFromScene(self.mapToScene(position))
            document = item.document()
            layout = document.documentLayout()
            block = document.firstBlock()
            while block.isValid():
                if _is_checkbox(block) and _checkbox_hit(block, layout, point):
                    return item, block.position()
                block = block.next()
            if include_native and item is self._editor:
                # Consume Qt's native checkbox click when its hit box misses the painted marker.
                block = layout.blockWithMarkerAt(point)
                if block.isValid() and _is_checkbox(block):
                    return item, None
        return None

    def mouseDoubleClickEvent(self, event):
        if self._tool != "select" or event.button() != Qt.MouseButton.LeftButton:
            super().mouseDoubleClickEvent(event)
            return
        if self._checkbox_at(event.position().toPoint(), include_native=True) is not None:
            event.accept()
            return
        item = self.itemAt(event.position().toPoint())
        if isinstance(item, _TextItem):
            self._edit_text(item)
            super().mouseDoubleClickEvent(event)
        elif isinstance(item, _MediaItem) and item.record["type"] == "equation":
            result = self._equation_prompt(item.record.get("source", ""))
            if result is not None:
                item.set_equation(*result)
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event):
        if self._tool != "select":
            return
        item = self.itemAt(event.pos())
        if not isinstance(item, (_PageItem, ShapeItem, LedgerItem)):
            return
        if item is self._editor:
            super().contextMenuEvent(event)
            return
        self._finish_text()
        self._select(item)
        menu = QMenu(self)
        resize = None
        if item.record["type"] in {"text", "image"}:
            resize = menu.addAction("Set Width…")
        remove = menu.addAction("Remove")
        selected = menu.exec(event.globalPos())
        if selected is remove:
            self._remove_items([item])
        elif resize is not None and selected is resize:
            self._resize_item(item)

    def _resize_item(self, item):
        width, accepted = QInputDialog.getDouble(
            self, "Set Width", "Width (px)", item.boundingRect().width(), 24, 100000, 1
        )
        if not accepted:
            return
        if isinstance(item, _TextItem):
            item.setTextWidth(width)
        else:
            item.prepareGeometryChange()
            item.height *= width / item.width
            item.width = width
            item.update()
        self._content_changed()

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.StandardKey.Undo):
            self.undo()
            event.accept()
            return
        if event.matches(QKeySequence.StandardKey.Paste):
            mime = QApplication.clipboard().mimeData()
            if mime is not None and mime.hasImage() and self.paste_image():
                event.accept()
                return
            if self._editor is None and mime is not None and mime.hasText():
                self.add_text(html=escape(mime.text()).replace("\n", "<br>"))
                event.accept()
                return
        if event.key() == Qt.Key.Key_Escape and self._editor is not None:
            self._finish_text()
            event.accept()
        elif event.key() in {Qt.Key.Key_Delete, Qt.Key.Key_Backspace} and self.delete_selected():
            event.accept()
        else:
            super().keyPressEvent(event)

    def set_zoom(self, factor):
        factor = min(3, max(0.35, factor))
        if factor != self._zoom:
            multiplier = factor / self._zoom
            self.scale(multiplier, multiplier)
            self._zoom = factor
            self.zoom_changed.emit(factor)

    def zoom_by(self, factor):
        self.set_zoom(self._zoom * factor)

    def wheelEvent(self, event):
        modifiers = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier
        if event.modifiers() & modifiers and event.angleDelta().y():
            self.zoom_by(1.15 ** (event.angleDelta().y() / 120))
            event.accept()
        else:
            super().wheelEvent(event)
