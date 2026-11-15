"""Editable vector shapes for the Qt page canvas."""

from copy import deepcopy
from math import atan2, degrees, hypot

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor, QPainterPath, QPainterPathStroker, QPen, QTransform,
)
from PySide6.QtWidgets import QGraphicsItem


SHAPE_NAMES = ("square", "rectangle", "circle", "ellipse", "triangle", "line", "arrow", "axes")
_EQUAL_SIDES = {"square", "circle"}
_SEGMENTS = {"line", "arrow"}
_CLOSED = {"square", "rectangle", "circle", "ellipse", "triangle"}
_HANDLE_CORNERS = ((0, 0), (1, 0), (1, 1), (0, 1))
_SELECTION = QColor("#3769d1")
_CHANGE = QGraphicsItem.GraphicsItemChange
_FLAG = QGraphicsItem.GraphicsItemFlag


class ShapeItem(QGraphicsItem):
    """A single positioned shape with resize and rotate handles in Select mode."""

    def __init__(self, canvas, record):
        super().__init__()
        self.canvas = canvas
        self.record = deepcopy(record)
        self._kind = record["shape"]
        if self._kind not in SHAPE_NAMES:
            raise ValueError(f"Unknown shape {self._kind!r}")
        self._width = 0
        self._height = 0
        self._gesture = None
        self._pen = QPen(
            QColor(record.get("color", "#29395b")),
            max(0.25, float(record.get("width", 2.5))),
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )
        self._reverse_x = bool(record.get("reverse_x"))
        self._reverse_y = bool(record.get("reverse_y"))
        self.set_size(float(record["w"]), float(record["h"]))
        self.setRotation(float(record.get("rotation", 0)))
        self.setPos(float(record["x"]), float(record["y"]))
        self.setFlags(
            _FLAG.ItemIsSelectable | _FLAG.ItemIsMovable | _FLAG.ItemSendsGeometryChanges
        )

    def set_size(self, width, height):
        """Set dimensions in canvas units before applying the transform."""
        minimum = 0 if self._kind in _SEGMENTS else 1
        width, height = max(minimum, width), max(minimum, height)
        if self._kind in _EQUAL_SIDES:
            width = height = max(width, height)
        if (width, height) != (self._width, self._height):
            self.prepareGeometryChange()
            self._width, self._height = width, height
            self.setTransformOriginPoint(width / 2, height / 2)
            self.update()

    def set_direction(self, reverse_x, reverse_y):
        if (reverse_x, reverse_y) != (self._reverse_x, self._reverse_y):
            self._reverse_x, self._reverse_y = reverse_x, reverse_y
            self.update()

    def to_record(self):
        record = deepcopy(self.record)
        record.update(
            x=self.x(),
            y=self.y(),
            w=self._width,
            h=self._height,
            rotation=self.rotation(),
        )
        if self._kind in _SEGMENTS:
            record.update(reverse_x=self._reverse_x, reverse_y=self._reverse_y)
        return record

    def _scale(self):
        return self.canvas.transform().m11()

    def _corners(self):
        return [
            QPointF(right * self._width, bottom * self._height)
            for right, bottom in _HANDLE_CORNERS
        ]

    def _rotation_handle(self):
        return QPointF(self._width / 2, -24 / self._scale())

    def _resize_handles(self):
        corners = self._corners()
        if self._kind in _SEGMENTS:
            first = _HANDLE_CORNERS.index((int(self._reverse_x), int(self._reverse_y)))
            return [(first, corners[first]), ((first + 2) % 4, corners[(first + 2) % 4])]
        return list(enumerate(corners))

    def boundingRect(self):
        # Reserve handles at the canvas's minimum zoom without reindexing on every zoom.
        margin = max(26, self._pen.widthF() / 2 + 2)
        bounds = QRectF(0, 0, self._width, self._height).adjusted(
            -margin, -max(margin, 96), margin, margin
        )
        if self._kind in {"arrow", "axes"}:
            arrow_margin = self._pen.widthF() * 2.2
            bounds = bounds.united(QRectF(0, 0, self._width, self._height).adjusted(
                -arrow_margin, -arrow_margin, arrow_margin, arrow_margin
            ))
        return bounds

    def _line(self, path, start, end, arrow=False):
        path.moveTo(start)
        path.lineTo(end)
        if arrow:
            delta = end - start
            length = hypot(delta.x(), delta.y())
            if length:
                unit = QPointF(delta.x() / length, delta.y() / length)
                normal = QPointF(-unit.y(), unit.x())
                size = min(max(10, self._pen.widthF() * 3), length / 2)
                base = end - unit * size
                path.moveTo(base + normal * (size * 0.55))
                path.lineTo(end)
                path.lineTo(base - normal * (size * 0.55))

    def _outline(self):
        width, height = self._width, self._height
        bounds = QRectF(0, 0, width, height)
        path = QPainterPath()
        if self._kind in {"square", "rectangle"}:
            path.addRect(bounds)
        elif self._kind in {"circle", "ellipse"}:
            path.addEllipse(bounds)
        elif self._kind == "triangle":
            path.moveTo(width / 2, 0)
            path.lineTo(width, height)
            path.lineTo(0, height)
            path.closeSubpath()
        elif self._kind == "axes":
            origin = QPointF(0, height)
            self._line(path, origin, QPointF(width, height), arrow=True)
            self._line(path, origin, QPointF(0, 0), arrow=True)
        else:
            start = QPointF(width if self._reverse_x else 0, height if self._reverse_y else 0)
            end = QPointF(width, height) - start
            self._line(path, start, end, arrow=self._kind == "arrow")
        return path

    def shape(self):
        outline = self._outline()
        stroker = QPainterPathStroker()
        stroker.setWidth(max(8 / self._scale(), self._pen.widthF()))
        shape = stroker.createStroke(outline)
        if self._kind in _CLOSED:
            shape = shape.united(outline)
        if self.isSelected():
            radius = 6 / self._scale()
            handles = QPainterPath()
            handles.setFillRule(Qt.FillRule.WindingFill)
            for _, position in self._resize_handles():
                handles.addRect(QRectF(
                    position.x() - radius, position.y() - radius, radius * 2, radius * 2
                ))
            handles.addEllipse(self._rotation_handle(), radius, radius)
            shape = shape.united(handles)
        return shape

    def paint(self, painter, option, widget=None):
        painter.setPen(self._pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(self._outline())
        if not self.isSelected():
            return
        outline = QPen(_SELECTION, 1, Qt.PenStyle.DashLine)
        outline.setCosmetic(True)
        painter.setPen(outline)
        painter.drawRect(QRectF(0, 0, self._width, self._height))
        rotate = self._rotation_handle()
        painter.drawLine(QPointF(self._width / 2, 0), rotate)
        handle_pen = QPen(_SELECTION, 1.2)
        handle_pen.setCosmetic(True)
        painter.setPen(handle_pen)
        painter.setBrush(Qt.GlobalColor.white)
        radius = 4 / self._scale()
        for _, position in self._resize_handles():
            painter.drawRect(QRectF(
                position.x() - radius, position.y() - radius, radius * 2, radius * 2
            ))
        painter.drawEllipse(rotate, radius, radius)

    def itemChange(self, change, value):
        if change == _CHANGE.ItemPositionHasChanged and self._gesture is None:
            self.canvas._item_moved()
        return super().itemChange(change, value)

    def mousePressEvent(self, event):
        if not self.isSelected() or event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        pos = event.pos()
        radius = 6 / self._scale()
        rotate = pos - self._rotation_handle()
        if hypot(rotate.x(), rotate.y()) <= radius:
            center = self.mapToScene(self.transformOriginPoint())
            delta = event.scenePos() - center
            self._gesture = (
                "rotate", center, degrees(atan2(delta.y(), delta.x())) - self.rotation()
            )
        else:
            for corner, position in self._resize_handles():
                hit = pos - position
                if max(abs(hit.x()), abs(hit.y())) <= radius:
                    anchor = self.mapToScene(self._corners()[(corner + 2) % 4])
                    offset = event.scenePos() - self.mapToScene(position)
                    moving_start = _HANDLE_CORNERS[corner] == (self._reverse_x, self._reverse_y)
                    self._gesture = ("resize", corner, anchor, offset, moving_start)
                    break
        if self._gesture is None:
            super().mousePressEvent(event)
        else:
            event.accept()

    def _apply_gesture(self, point):
        kind, *data = self._gesture
        if kind == "rotate":
            center, offset = data
            delta = point - center
            self.setRotation((degrees(atan2(delta.y(), delta.x())) - offset) % 360)
            return

        corner, anchor, offset, moving_start = data
        right, bottom = _HANDLE_CORNERS[corner]
        delta = QTransform().rotate(-self.rotation()).map(point - offset - anchor)
        if self._kind in _SEGMENTS:
            right, bottom = delta.x() >= 0, delta.y() >= 0
            self.set_size(abs(delta.x()), abs(delta.y()))
            self.set_direction(
                right if moving_start else not right, bottom if moving_start else not bottom
            )
        else:
            self.set_size(max(0, delta.x() if right else -delta.x()),
                          max(0, delta.y() if bottom else -delta.y()))
        opposite = QPointF((not right) * self._width, (not bottom) * self._height)
        self.setPos(self.pos() + anchor - self.mapToScene(opposite))

    def mouseMoveEvent(self, event):
        if self._gesture is None:
            super().mouseMoveEvent(event)
        else:
            self._apply_gesture(event.scenePos())
            event.accept()

    def mouseReleaseEvent(self, event):
        if self._gesture is None:
            super().mouseReleaseEvent(event)
        else:
            self._apply_gesture(event.scenePos())
            self._gesture = None
            self.canvas._content_changed()
            event.accept()
