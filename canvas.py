"""Native Qt editor for positioned note content."""

import base64
import binascii
from copy import deepcopy
from html import escape
from io import BytesIO

from PySide6.QtCore import QByteArray, QBuffer, QEvent, QIODevice, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QImage,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QPixmap,
    QTextCharFormat,
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
    QMessageBox,
    QStyle,
)


_SELECTION = QColor("#3769d1")
_INK = "#29395b"
_EDITABLE = QGraphicsItem.GraphicsItemFlag
_CHANGE = QGraphicsItem.GraphicsItemChange


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
        self._setup(canvas, record)
        self.document().contentsChanged.connect(canvas._content_changed)

    def itemChange(self, change, value):
        return super().itemChange(change, self._item_changed(change, value))

    def to_record(self):
        record = super().to_record()
        record.update(
            html=self.document().toHtml(),
            w=self.textWidth(),
            h=self.document().size().height(),
        )
        return record

    def paint(self, painter, option, widget=None):
        selected = self.isSelected()
        option.state &= ~QStyle.StateFlag.State_Selected
        super().paint(painter, option, widget)
        if selected:
            _selection_outline(painter, self.boundingRect())


class _InkItem(_PageItem, QGraphicsPathItem):
    def __init__(self, canvas, record):
        super().__init__()
        self._setup(canvas, record)
        self.setPen(
            QPen(
                QColor(record.get("color", _INK)),
                max(0.25, float(record.get("width", 2.5))),
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
        path = self.path()
        if len(self.points) == 1:
            path.moveTo(point)
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
        self._stroke = None
        self._erase_previous = None
        self._editor = None
        self._items = []
        self._page_rect = QRectF()
        self._refresh_bounds()

    def set_page(self, objects):
        self._loading = True
        try:
            self._finish_text()
            self._stroke = None
            self._erase_previous = None
            self.scene().clear()
            self._items.clear()
            for record in objects:
                self._add_item(record)
            self._refresh_bounds()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().minimum())
            self.verticalScrollBar().setValue(self.verticalScrollBar().minimum())
        finally:
            self._loading = False

    def objects(self):
        return [item.to_record() for item in self._items]

    def _add_item(self, record):
        kind = record["type"]
        if kind == "text":
            item = _TextItem(self, record)
        elif kind == "ink":
            item = _InkItem(self, record)
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
        painter.fillRect(self._page_rect, Qt.GlobalColor.white)
        pen = QPen(QColor("#dce2ee"), 1)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.drawRect(self._page_rect)

    def _content_changed(self):
        if not self._loading:
            self._refresh_bounds()
            self.changed.emit()

    def _item_moved(self):
        if not self._loading:
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

    def _select(self, item):
        self.scene().clearSelection()
        item.setSelected(True)
        self.ensureVisible(item, 24, 24)
        self.setFocus()

    def _edit_text(self, item):
        if self._editor is not item:
            self._finish_text()
        self._select(item)
        item.setFlag(_EDITABLE.ItemIsMovable, False)
        item.setTextInteractionFlags(Qt.TextInteractionFlag.TextEditorInteraction)
        item.setFocus(Qt.FocusReason.MouseFocusReason)
        self._editor = item

    def _finish_text(self):
        if self._editor is not None:
            self._editor.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
            self._editor.setFlag(_EDITABLE.ItemIsMovable, True)
            self._editor.clearFocus()
            self._editor = None

    def set_tool(self, tool):
        if tool not in {"select", "text", "ink", "erase"}:
            raise ValueError(f"Unknown canvas tool {tool!r}")
        self._gesture_finished()
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
        return super().viewportEvent(event)

    def add_text(self, position=None, html=""):
        self.set_tool("select")
        position = self._insertion_point(420, 30) if position is None else position
        item = self._add_item(
            {"type": "text", "x": position.x(), "y": position.y(), "w": 420, "html": html}
        )
        self._content_changed()
        self._edit_text(item)

    def set_format(self, style):
        item = self._editor
        if item is None:
            item = next((i for i in self.scene().selectedItems() if isinstance(i, _TextItem)), None)
            if item is None:
                return
            self._edit_text(item)
        cursor = item.textCursor()
        current = cursor.charFormat()
        char_format = QTextCharFormat()
        if style == "bold":
            weight = QFont.Weight.Normal if current.fontWeight() >= QFont.Weight.Bold else QFont.Weight.Bold
            char_format.setFontWeight(weight)
        elif style == "italic":
            char_format.setFontItalic(not current.fontItalic())
        elif style == "underline":
            char_format.setFontUnderline(not current.fontUnderline())
        cursor.mergeCharFormat(char_format)
        item.setTextCursor(cursor)
        item.setFocus()

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
        if image.isNull():
            return False
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not image.save(buffer, "PNG"):
            return False
        self.set_tool("select")
        self._finish_text()
        width = min(620, image.width())
        height = width * image.height() / image.width()
        point = self._insertion_point(width, height)
        item = self._add_item({
            "type": "image",
            "x": point.x(),
            "y": point.y(),
            "w": width,
            "h": height,
            "data": base64.b64encode(bytes(buffer.data())).decode("ascii"),
        })
        self._content_changed()
        self._select(item)
        return True

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
            if item.record["type"] in {"ink", "svg"}
        ]
        if items:
            self._remove_items(items)
        self._erase_previous = point

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        point = self.mapToScene(event.position().toPoint())
        if self._tool == "text":
            self.add_text(point)
        elif self._tool == "ink":
            self._stroke = self._add_item({
                "type": "ink", "x": point.x(), "y": point.y(),
                "points": [[0, 0]], "width": 2.5, "color": _INK,
            })
        elif self._tool == "erase":
            self._erase(point)
        else:
            if self._editor is not None and self.itemAt(event.position().toPoint()) is not self._editor:
                self._finish_text()
            super().mousePressEvent(event)
            return
        event.accept()

    def mouseMoveEvent(self, event):
        point = self.mapToScene(event.position().toPoint())
        if self._stroke is not None:
            self._stroke.add_point(point - self._stroke.pos())
        elif self._erase_previous is not None:
            self._erase(point)
        else:
            super().mouseMoveEvent(event)
            return
        event.accept()

    def mouseReleaseEvent(self, event):
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

    def _gesture_finished(self):
        if self._stroke is not None:
            self._stroke = None
            self._content_changed()
        self._erase_previous = None

    def mouseDoubleClickEvent(self, event):
        if self._tool != "select" or event.button() != Qt.MouseButton.LeftButton:
            super().mouseDoubleClickEvent(event)
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
        if not isinstance(item, _PageItem):
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
