"""Paper settings for note printing and PDF export."""

from math import isfinite

from PySide6.QtCore import QMarginsF, QSizeF
from PySide6.QtGui import QPageLayout, QPageSize
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QGroupBox,
    QLabel, QVBoxLayout,
)

PRINT_DPI = 144
PRINT_HEADING_INCHES = 0.43


_MM = QPageLayout.Unit.Millimeter
_SIZE_MM = QPageSize.Unit.Millimeter
_MIN_BODY_INCHES = 0.25
_PAPER_SIZES = (
    QPageSize.PageSizeId.Letter,
    QPageSize.PageSizeId.Legal,
    QPageSize.PageSizeId.Tabloid,
    QPageSize.PageSizeId.Executive,
    QPageSize.PageSizeId.A3,
    QPageSize.PageSizeId.A4,
    QPageSize.PageSizeId.A5,
    QPageSize.PageSizeId.B5,
)


def _has_print_area(layout):
    if not layout.isValid():
        return False
    area = layout.paintRectPixels(PRINT_DPI)
    return (
        area.width() >= _MIN_BODY_INCHES * PRINT_DPI
        and area.height() >= (PRINT_HEADING_INCHES + _MIN_BODY_INCHES) * PRINT_DPI
    )


def page_setup_data(layout):
    size = layout.pageSize()
    margins = layout.margins()
    data = {
        "paper_size": size.id().name,
        "orientation": layout.orientation().name,
        "unit": layout.units().name,
        "margins": [margins.left(), margins.top(), margins.right(), margins.bottom()],
    }
    if size != QPageSize(size.id()):
        dimensions = size.definitionSize()
        data["paper_definition"] = {
            "size": [dimensions.width(), dimensions.height()],
            "unit": size.definitionUnits().name,
            "name": size.name(),
        }
    return data


def restore_page_setup(data):
    default = QPageLayout(
        QPageSize(QPageSize.PageSizeId.A4), QPageLayout.Orientation.Portrait,
        QMarginsF(10, 10, 10, 10), QPageLayout.Unit.Point,
    )
    try:
        size_id = QPageSize.PageSizeId[data["paper_size"]]
        definition = data.get("paper_definition")
        if definition is not None:
            width, height = definition["size"]
            if not (0 < width <= 32000 and 0 < height <= 32000):
                return default
            size = QPageSize(
                QSizeF(width, height), QPageSize.Unit[definition["unit"]], definition["name"],
                QPageSize.SizeMatchPolicy.ExactMatch,
            )
        else:
            size = QPageSize(size_id)
        orientation = QPageLayout.Orientation[data["orientation"]]
        unit = QPageLayout.Unit[data["unit"]]
        values = data["margins"]
        if not all(isfinite(value) and value >= 0 for value in values):
            return default
        margins = QMarginsF(*values)
        layout = QPageLayout(size, orientation, QMarginsF(), unit)
        if layout.setMargins(margins) and _has_print_area(layout):
            return layout
    except (KeyError, TypeError, ValueError, OverflowError):
        pass
    return default


class PageSetupDialog(QDialog):
    def __init__(self, page_layout, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Page Setup")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)

        current_size = page_layout.pageSize()
        sizes = [QPageSize(size_id) for size_id in _PAPER_SIZES]
        selected = next((index for index, size in enumerate(sizes) if size == current_size), None)
        if selected is None:
            selected = len(sizes)
            sizes.append(current_size)

        self.paper_size = QComboBox()
        largest_edge = 0
        for size in sizes:
            dimensions = size.size(_SIZE_MM)
            largest_edge = max(largest_edge, dimensions.width(), dimensions.height())
            self.paper_size.addItem(size.name(), size)
        self.paper_size.setCurrentIndex(selected)
        form.addRow("Paper &size:", self.paper_size)

        self.orientation = QComboBox()
        self.orientation.addItem("Portrait", QPageLayout.Orientation.Portrait)
        self.orientation.addItem("Landscape", QPageLayout.Orientation.Landscape)
        self.orientation.setCurrentIndex(self.orientation.findData(page_layout.orientation()))
        form.addRow("&Orientation:", self.orientation)

        margins = page_layout.margins(_MM)
        group = QGroupBox("Margins")
        fields = QFormLayout(group)
        self.margins = {}
        for side, value in (
            ("Top", margins.top()),
            ("Bottom", margins.bottom()),
            ("Left", margins.left()),
            ("Right", margins.right()),
        ):
            field = QDoubleSpinBox()
            field.setRange(0, largest_edge)
            field.setSuffix(" mm")
            field.setValue(value)
            fields.addRow(f"{side}:", field)
            self.margins[side] = field
        layout.addWidget(group)

        self.error = QLabel("Reduce the margins to leave room for the note.")
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.paper_size.currentIndexChanged.connect(self._update_validation)
        self.orientation.currentIndexChanged.connect(self._update_validation)
        for field in self.margins.values():
            field.valueChanged.connect(self._update_validation)
        self._update_validation()

    def _margins(self):
        return QMarginsF(*(self.margins[side].value() for side in ("Left", "Top", "Right", "Bottom")))

    def page_layout(self):
        layout = QPageLayout(self.paper_size.currentData(), self.orientation.currentData(), QMarginsF(), _MM)
        if layout.setMargins(self._margins()) and _has_print_area(layout):
            return layout
        return None

    def _update_validation(self):
        valid = self.page_layout() is not None
        self.error.setHidden(valid)
        self.ok.setEnabled(valid)

    def accept(self):
        self._update_validation()
        if self.ok.isEnabled():
            super().accept()
