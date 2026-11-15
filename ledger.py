"""Editable transaction ledger for the Qt page canvas."""

from copy import deepcopy
from decimal import Decimal, localcontext
from html import escape
import re

from PySide6.QtCore import QEvent, QObject, QRectF, QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QKeySequence, QPen, QTextDocument
from PySide6.QtWidgets import (
    QAbstractItemView,
    QGraphicsItem,
    QGraphicsObject,
    QGraphicsProxyWidget,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QPushButton,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


_COLUMNS = ("date", "description", "debit", "credit")
_HEADERS = ("Date", "Description", "Debit", "Credit", "Balance")
_BLANK_ROW = dict.fromkeys(_COLUMNS, "")
_BALANCE_HELP = "Running balance: add Debit and subtract Credit on each row."
_HEADER_HEIGHT = 30
_CURRENCY_ALIGN = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
_AMOUNT_PATTERN = re.compile(r"[+-]?(?:[0-9]{1,21}(?:\.[0-9]{0,2})?|\.[0-9]{1,2})")
_FLAGS = QGraphicsItem.GraphicsItemFlag
_CHANGE = QGraphicsItem.GraphicsItemChange


def _amount(text):
    stripped = text.strip()
    if not stripped:
        return Decimal(0)
    return Decimal(stripped) if _AMOUNT_PATTERN.fullmatch(stripped) else None


class _UndoFilter(QObject):
    def __init__(self, canvas, parent):
        super().__init__(parent)
        self.canvas = canvas

    def eventFilter(self, target, event):
        if event.type() in {QEvent.Type.ShortcutOverride, QEvent.Type.KeyPress} and event.matches(
            QKeySequence.StandardKey.Undo
        ):
            event.accept()
            if event.type() == QEvent.Type.KeyPress:
                QTimer.singleShot(0, self.canvas, self.canvas.undo)
            return True
        return super().eventFilter(target, event)


class _LiveCellDelegate(QStyledItemDelegate):
    edited = Signal(int, int, str)

    def createEditor(self, parent, option, index):
        editor = QLineEdit(parent)
        editor.installEventFilter(self.parent()._undo)
        if index.column() in (2, 3):
            editor.setAlignment(_CURRENCY_ALIGN)
        row, column = index.row(), index.column()
        editor.textEdited.connect(lambda text: self.edited.emit(row, column, text))
        return editor


class _LedgerTable(QTableWidget):
    draft_changed = Signal(int, int, str)

    def __init__(self, canvas, parent):
        super().__init__(0, len(_HEADERS), parent)
        self._undo = _UndoFilter(canvas, self)
        self.installEventFilter(self._undo)
        delegate = _LiveCellDelegate(self)
        delegate.edited.connect(self.draft_changed)
        self.setItemDelegate(delegate)
        self.setHorizontalHeaderLabels(_HEADERS)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setTabKeyNavigation(True)
        self.setAlternatingRowColors(True)
        self.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.AnyKeyPressed
        )
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(28)
        header = self.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for index, width in [(0, 101), (2, 96), (3, 96), (4, 107)]:
            self.setColumnWidth(index, width)
        self.horizontalHeaderItem(4).setToolTip(_BALANCE_HELP)

    def moveCursor(self, action, modifiers):
        index = super().moveCursor(action, modifiers)
        if index.isValid() and index.column() == 4:
            if action == QAbstractItemView.CursorAction.MoveNext and index.row() + 1 < self.rowCount():
                return self.model().index(index.row() + 1, 0)
            if action == QAbstractItemView.CursorAction.MovePrevious:
                return self.model().index(index.row(), 3)
        return index

class LedgerItem(QGraphicsObject):
    """A movable ledger whose editable cells and balance travel with a page."""

    def __init__(self, canvas, record):
        super().__init__()
        self.canvas = canvas
        self.record = deepcopy(record)
        self._width = max(610, float(record.get("w") or 680))
        self._height = max(230, float(record.get("h") or 276))
        self._print_document = None
        self.setPos(float(record.get("x", 0)), float(record.get("y", 0)))
        self.setFlags(_FLAGS.ItemIsSelectable | _FLAGS.ItemIsMovable | _FLAGS.ItemSendsGeometryChanges)

        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 4, 8, 8)
        layout.setSpacing(5)
        self._table = _LedgerTable(canvas, panel)
        self._drafts = {}
        self._table.draft_changed.connect(self._draft_changed)
        self._table.itemDelegate().closeEditor.connect(self._finish_drafts)
        self._table.setAccessibleName("Financial ledger")
        layout.addWidget(self._table)

        controls = QHBoxLayout()
        add = QPushButton("Add Row", panel)
        add.clicked.connect(self._add_row)
        self._remove = QPushButton("Remove Row", panel)
        self._remove.clicked.connect(self._remove_row)
        controls.addWidget(add)
        controls.addWidget(self._remove)
        controls.addStretch()
        layout.addLayout(controls)

        for row in record.get("rows") or [_BLANK_ROW] * 4:
            self._put_row(row)
        self._update_balances()
        self._table.itemChanged.connect(self._row_changed)
        self._table.currentCellChanged.connect(self._update_remove)
        self._update_remove()

        self._proxy = QGraphicsProxyWidget(self)
        self._proxy.setWidget(panel)
        self._proxy.setGeometry(QRectF(1, _HEADER_HEIGHT, self._width - 2, self._height - _HEADER_HEIGHT - 1))

    def boundingRect(self):
        if self._print_document is not None:
            return QRectF(0, 0, self._width, self._print_document.size().height())
        return QRectF(0, 0, self._width, self._height)

    def begin_print(self):
        # Draw the ledger as a table rather than printing the widget's scroll area and buttons.
        headings = "".join(
            f'<th width="{width}%">{name}</th>'
            for name, width in zip(_HEADERS, (15, 32, 17, 17, 19))
        )
        rows = [f'<tr bgcolor="#edf4fa">{headings}</tr>']
        for row in range(self._table.rowCount()):
            cells = [self._read_cell(row, column) for column in range(4)]
            cells.append(self._table.item(row, 4).text())
            data = "".join(
                f'<td align="{"right" if column >= 2 else "left"}">{escape(value) or "&nbsp;"}</td>'
                for column, value in enumerate(cells)
            )
            rows.append(f'<tr>{data}</tr>')
        document = QTextDocument()
        font = QFont("sans-serif")
        font.setPixelSize(14)
        document.setDefaultFont(font)
        document.setUseDesignMetrics(True)
        document.setHtml(
            '<p><b>Financial Ledger</b></p><table width="100%" border="1" '
            'cellpadding="5" cellspacing="0" bgcolor="#ffffff">' + "".join(rows) + '</table>'
        )
        document.setTextWidth(self._width)
        self.prepareGeometryChange()
        self._print_document = document
        self._proxy.hide()

    def end_print(self):
        self.prepareGeometryChange()
        self._print_document = None
        self._proxy.show()

    def paint(self, painter, option, widget=None):
        if self._print_document is not None:
            self._print_document.drawContents(painter)
            return
        bounds = self.boundingRect()
        painter.fillRect(bounds, QColor("#ffffff"))
        painter.fillRect(0, 0, int(bounds.width()), _HEADER_HEIGHT, QColor("#edf4fa"))
        painter.setPen(QColor("#254361"))
        font = QFont(painter.font())
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(
            QRectF(10, 0, self._width - 20, _HEADER_HEIGHT),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            "Financial Ledger",
        )
        pen = QPen(QColor("#3769d1" if self.isSelected() else "#c7d5e0"))
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.drawRect(bounds.adjusted(0, 0, -0.5, -0.5))

    def itemChange(self, change, value):
        if change == _CHANGE.ItemPositionHasChanged:
            self.canvas._item_moved()
        return super().itemChange(change, value)

    def _put_row(self, record):
        row = self._table.rowCount()
        self._table.insertRow(row)
        for column, key in enumerate(_COLUMNS):
            value = record.get(key)
            item = QTableWidgetItem(str(value) if value is not None else "")
            if key in {"debit", "credit"}:
                item.setTextAlignment(_CURRENCY_ALIGN)
            self._table.setItem(row, column, item)
        balance = QTableWidgetItem()
        balance.setFlags(balance.flags() & ~Qt.ItemFlag.ItemIsEditable)
        balance.setTextAlignment(_CURRENCY_ALIGN)
        balance.setToolTip(_BALANCE_HELP)
        self._table.setItem(row, 4, balance)

    def _update_balances(self):
        blocked = self._table.blockSignals(True)
        try:
            running = Decimal(0)
            tainted = False
            with localcontext() as context:
                context.prec = 40
                for row in range(self._table.rowCount()):
                    debit, credit = (self._read_amount(row, column) for column in (2, 3))
                    tainted |= debit is None or credit is None
                    if not tainted:
                        running += debit - credit
                    used = any(self._read_cell(row, column).strip() for column in range(4))
                    self._table.item(row, 4).setText(
                        "—" if tainted else format(running, ",.2f") if used else ""
                    )
        finally:
            self._table.blockSignals(blocked)

    def _read_amount(self, row, column):
        item = self._table.item(row, column)
        value = _amount(self._read_cell(row, column))
        item.setBackground(QBrush(QColor("#fbe5e2")) if value is None else QBrush())
        item.setToolTip(
            "Enter a plain decimal amount with up to two decimal places."
            if value is None else ""
        )
        return value

    def _update_remove(self, *unused):
        self._remove.setEnabled(self._table.currentRow() >= 0)

    def _row_changed(self, item):
        if item.column() < 4:
            self._drafts.pop((item.row(), item.column()), None)
            self._update_balances()
            self.canvas._content_changed()

    def _read_cell(self, row, column):
        return self._drafts.get((row, column), self._table.item(row, column).text())

    def _draft_changed(self, row, column, text):
        self._drafts[row, column] = text
        self._update_balances()
        self.canvas._content_changed()

    def _finish_drafts(self, *_):
        if self._drafts:
            # Keep edits already published to the page even if Escape closes the editor.
            with QSignalBlocker(self._table):
                for (row, column), value in self._drafts.items():
                    self._table.item(row, column).setText(value)
            self._drafts.clear()

    def _add_row(self):
        blocked = self._table.blockSignals(True)
        try:
            self._put_row(_BLANK_ROW)
        finally:
            self._table.blockSignals(blocked)
        row = self._table.rowCount() - 1
        self._table.setCurrentCell(row, 0)
        self._table.scrollToItem(self._table.item(row, 0))
        self.canvas._content_changed()

    def _remove_row(self):
        self._finish_drafts()
        row = self._table.currentRow()
        if row < 0:
            return
        blocked = self._table.blockSignals(True)
        try:
            self._table.removeRow(row)
            if not self._table.rowCount():
                self._put_row(_BLANK_ROW)
        finally:
            self._table.blockSignals(blocked)
        self._update_balances()
        self._update_remove()
        self.canvas._content_changed()

    def to_record(self):
        rows = [
            {
                key: self._read_cell(row, column)
                for column, key in enumerate(_COLUMNS)
            }
            for row in range(self._table.rowCount())
        ]
        return {**self.record, "x": self.x(), "y": self.y(), "w": self._width, "h": self._height, "rows": rows}
