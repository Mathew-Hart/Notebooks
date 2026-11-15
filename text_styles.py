"""Paragraph presets for the rich text on a notebook page."""

from PySide6.QtGui import QFont, QFontDatabase, QTextCharFormat, QTextCursor, QTextFormat


_PROPERTY = QTextFormat.Property
_STYLE = int(_PROPERTY.UserProperty) + 1
_HEADING_LEVELS = {f"heading{level}": level for level in range(1, 7)}
_HEADING_LEVELS["title"] = 1
PAGE_TITLE_POINT_SIZE = 30
_POINT_SIZES = dict(zip(
    ("title", "heading1", "heading2", "heading3", "heading4", "heading5", "heading6"),
    (PAGE_TITLE_POINT_SIZE, 24, 20, 17, 15, 13.5, 12.5),
))
_STORED_STYLES = {"title", "code", "quote"}
TEXT_STYLES = {*_HEADING_LEVELS, "body", "code", "quote"}


def paragraph_style(block):
    fmt = block.blockFormat()
    style = fmt.property(_STYLE)
    if style in {"code", "quote"}:
        return style
    level = fmt.headingLevel()
    if 1 <= level <= 6:
        return "title" if level == 1 and style == "title" else f"heading{level}"
    if fmt.nonBreakableLines() or fmt.hasProperty(_PROPERTY.BlockCodeLanguage):
        return "code"
    if fmt.hasProperty(_PROPERTY.BlockQuoteLevel):
        return "quote"
    return "body"


def _set_block_style(fmt, style):
    if style in _HEADING_LEVELS:
        fmt.setHeadingLevel(_HEADING_LEVELS[style])
    else:
        fmt.clearProperty(_PROPERTY.HeadingLevel)
    if style in _STORED_STYLES:
        fmt.setProperty(_STYLE, style)
    else:
        fmt.clearProperty(_STYLE)
    if style == "code":
        # Qt uses this property to continue a code block across blank lines.
        fmt.setProperty(_PROPERTY.BlockCodeLanguage, "")
    else:
        fmt.clearProperty(_PROPERTY.BlockCodeLanguage)
    if style == "quote":
        fmt.setProperty(_PROPERTY.BlockQuoteLevel, 1)
    else:
        fmt.clearProperty(_PROPERTY.BlockQuoteLevel)


def _character_style(fmt, style, previous, font):
    fmt = QTextCharFormat(fmt)
    fmt.clearProperty(_PROPERTY.FontSizeAdjustment)
    fmt.setFontFamilies(font.families())
    default_size = font.pointSizeF() if font.pointSizeF() > 0 else 12
    fmt.setFontPointSize(_POINT_SIZES.get(style, default_size))
    if style in _HEADING_LEVELS and fmt.fontWeight() < QFont.Weight.Bold:
        fmt.setFontWeight(QFont.Weight.Bold)
    elif previous in _HEADING_LEVELS and style not in _HEADING_LEVELS and fmt.fontWeight() == QFont.Weight.Bold:
        fmt.setFontWeight(font.weight())
    return fmt


def apply_paragraph_style(block, style):
    previous = paragraph_style(block)
    cursor = QTextCursor(block)
    if style in _HEADING_LEVELS and block.textList():
        # Qt exports list items as <li>, which cannot preserve their heading level.
        block.textList().remove(block)
    fmt = block.blockFormat()
    _set_block_style(fmt, style)
    fmt.setNonBreakableLines(False)
    fmt.setTopMargin(12 if style in _HEADING_LEVELS else 0)
    fmt.setBottomMargin(8 if style in _HEADING_LEVELS else 4)
    if previous == "quote" or style == "quote":
        fmt.setLeftMargin(22 if style == "quote" else 0)
        fmt.setRightMargin(8 if style == "quote" else 0)
    cursor.setBlockFormat(fmt)

    font = cursor.document().defaultFont()
    if style == "code":
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    block_char = _character_style(block.charFormat(), style, previous, font)
    cursor.setBlockCharFormat(block_char)
    # Work on a snapshot of fragment ranges so that formatting a run can merge
    # adjacent fragments without invalidating their iterators.
    fragments = []
    fragment = block.begin()
    while not fragment.atEnd():
        run = fragment.fragment()
        if not run.charFormat().isImageFormat():
            fragments.append((run.position(), run.length(), run.charFormat()))
        fragment += 1
    for position, length, char_format in fragments:
        cursor.setPosition(position)
        cursor.setPosition(position + length, QTextCursor.MoveMode.KeepAnchor)
        cursor.setCharFormat(_character_style(char_format, style, previous, font))


def paragraph_style_marks(document):
    """Store only styles Qt HTML cannot identify on its own."""
    marks = {}
    block = document.begin()
    while block.isValid():
        style = paragraph_style(block)
        if style in _STORED_STYLES or (
            style in _HEADING_LEVELS
            and (block.textList() is not None or (not block.text() and QTextCursor(block).currentTable() is not None))
        ):
            marks[str(block.blockNumber())] = style
        block = block.next()
    return marks


def restore_paragraph_style_marks(document, marks):
    if not isinstance(marks, dict):
        return
    for index, style in marks.items():
        if not isinstance(index, str) or not index.isascii() or not index.isdecimal() or len(index) > 9:
            continue
        if not isinstance(style, str) or style not in TEXT_STYLES:
            continue
        block = document.findBlockByNumber(int(index))
        if not block.isValid():
            continue
        cursor = QTextCursor(block)
        if not block.text() and cursor.currentTable() is not None:
            # Qt's HTML exporter drops the empty paragraph inside a table cell.
            apply_paragraph_style(block, style)
        else:
            fmt = block.blockFormat()
            _set_block_style(fmt, style)
            cursor.setBlockFormat(fmt)
