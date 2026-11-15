"""Search Unicode names or insert text by character or code point."""

from functools import lru_cache
from itertools import islice
import re
import unicodedata

from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QListWidget, QVBoxLayout,
)


@lru_cache(maxsize=1)
def _named_characters():
    return tuple(
        (character, name)
        for number in range(0x110000)
        if (name := unicodedata.name(character := chr(number), ""))
    )


class UnicodePicker(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Insert Symbol")
        self.resize(550, 420)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("e.g. GREEK CAPITAL LETTER OMEGA")
        form.addRow("&Search names", self.search)
        self.results = QListWidget()
        self.results.setAccessibleName("Unicode search results")
        self.entry = QLineEdit()
        self.entry.setPlaceholderText("Paste a character or enter U+1F680")
        layout.addLayout(form)
        layout.addWidget(self.results)
        entry_form = QFormLayout()
        entry_form.addRow("&Character or code point", self.entry)
        layout.addLayout(entry_form)
        self.message = QLabel()
        layout.addWidget(self.message)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
        )
        self.insert = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.insert.setText("Insert")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.search.textChanged.connect(self.refresh_results)
        self.results.currentTextChanged.connect(self.select_result)
        self.results.itemDoubleClicked.connect(lambda item: self.accept())
        self.entry.textChanged.connect(self.validate_entry)
        self.validate_entry()
        self.search.setFocus()

    def refresh_results(self, query):
        self.results.clear()
        terms = query.upper().split()
        if not terms:
            return
        matches = (
            f"U+{ord(character):04X}  {character}  {name}"
            for character, name in _named_characters()
            if all(term in name for term in terms)
        )
        self.results.addItems(list(islice(matches, 200)))

    def select_result(self, result):
        if result:
            self.entry.setText(result.split(maxsplit=1)[0])

    @property
    def character(self):
        value = self.entry.text()
        if not value.upper().startswith("U+"):
            return value
        if not re.fullmatch(r"U\+[0-9A-F]{1,6}", value, re.IGNORECASE):
            return ""
        point = int(value[2:], 16)
        if point > 0x10FFFF or 0xD800 <= point <= 0xDFFF:
            return ""
        return chr(point)

    def validate_entry(self):
        valid = bool(self.character)
        self.insert.setEnabled(valid)
        self.message.setText("Enter a valid Unicode code point." if self.entry.text() and not valid else "")
