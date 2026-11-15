"""Native desktop entry point for the engineering notebook prototype."""
import argparse
from hashlib import blake2s
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from uuid import uuid4

from PySide6.QtCore import (
    QDateTime, QEvent, QIODevice, QLocale, QProcess, QSaveFile, QSignalBlocker, QStandardPaths,
    Qt, QThread, QTimer, QUrl, Signal,
)
from PySide6.QtGui import (
    QAction, QActionGroup, QColor, QDesktopServices, QFont, QFontDatabase, QIcon, QImageReader, QKeyEvent, QKeySequence,
    QPageLayout, QPainter, QPalette, QPdfWriter, QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QColorDialog, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QInputDialog,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QPushButton, QSpinBox, QSplitter, QStatusBar, QTabBar, QToolBar, QToolButton, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from canvas import PAPER_STYLES, PageCanvas
from importer import IMPORTED_PAGE_HEADER, import_notebook, split_imported_header
from library import Library
from page_setup import PRINT_DPI, PageSetupDialog, page_setup_data, restore_page_setup
from page_templates import PAGE_TEMPLATES
from print_preview import PrintPreview
from shapes import SHAPE_NAMES
from text_styles import PAGE_TITLE_POINT_SIZE
from unicode_picker import UnicodePicker
from sync import exchange


NAVIGATION_COLORS = (
    "#e6194b", "#3cb44b", "#ffe119", "#4363d8", "#f58231", "#911eb4", "#42d4f4", "#f032e6",
    "#bfef45", "#fabed4", "#469990", "#dcbeff", "#9a6324", "#fffac8", "#800000", "#aaffc3",
    "#808000", "#ffd8b1", "#000075", "#737373", "#ff6f61", "#0077b6", "#ffb703", "#264653",
    "#c08497", "#8ac926", "#6d597a", "#ff924c", "#b56576", "#007f5f", "#a8dadc", "#5f0f40",
)


def navigation_color_icon(item_id):
    # Stable IDs give existing and synced notebooks the same random assignment.
    index = blake2s(item_id.encode("utf-8"), digest_size=1).digest()[0] % len(NAVIGATION_COLORS)
    swatch = QPixmap(12, 12)
    swatch.fill(QColor(NAVIGATION_COLORS[index]))
    return QIcon(swatch)


class BackgroundJob(QThread):
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, operation, parent):
        super().__init__(parent)
        self.operation = operation

    def run(self):
        try:
            result = self.operation()
        except Exception as error:
            self.failed.emit(str(error))
        else:
            self.succeeded.emit(result)


class PageList(QListWidget):
    reordered = Signal()

    def page_ids(self):
        return [self.item(row).data(Qt.ItemDataRole.UserRole) for row in range(self.count())]

    def dropEvent(self, event):
        previous = self.page_ids()
        super().dropEvent(event)
        if self.page_ids() != previous:
            self.reordered.emit()


class NotebookWindow(QMainWindow):
    def __init__(self, library, converter=None, sync_url="", sync_token=""):
        super().__init__()
        self.library = library
        self.converter = converter
        self.sync_url = sync_url.rstrip("/")
        self.sync_token = sync_token
        self.job = None
        self.page_id = None
        self.loading = False
        self.navigation_clipboard = None
        self._imported_header = None
        self._original_page_date = None
        self._saved_objects = []
        self._focus_layout = None
        self.print_layout = restore_page_setup(self.library.page_setup())

        self.setWindowTitle("Engineering Notebook")
        self.resize(1210, 820)
        splitter = QSplitter()
        self.setCentralWidget(splitter)

        navigation = QWidget()
        navigation.setMinimumWidth(145)
        nav_layout = QVBoxLayout(navigation)
        nav_layout.setContentsMargins(14, 16, 12, 12)
        heading = QLabel("Notebooks")
        heading.setStyleSheet("font-size: 17px; font-weight: 600;")
        nav_layout.addWidget(heading)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search notes")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.filter_pages)
        nav_layout.addWidget(self.search)
        self.notebooks = QListWidget()
        self.notebooks.setAccessibleName("Notebooks")
        self.notebooks.setUniformItemSizes(True)
        self.notebooks.currentItemChanged.connect(self.select_notebook)
        nav_layout.addWidget(self.notebooks)
        splitter.addWidget(navigation)

        editor = QWidget()
        editor.setMinimumWidth(300)
        editor_layout = QVBoxLayout(editor)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(0)
        self.sections = QTabBar()
        self.sections.setAccessibleName("Sections")
        self.sections.setExpanding(False)
        self.sections.setUsesScrollButtons(True)
        self.sections.setMovable(True)
        self.sections.tabMoved.connect(self.reorder_sections)
        self.sections.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.sections.customContextMenuRequested.connect(self.section_menu)
        self.sections.currentChanged.connect(self.select_section)
        editor_layout.addWidget(self.sections)

        self.page_header = QWidget()
        header_layout = QVBoxLayout(self.page_header)
        header_layout.setContentsMargins(24, 12, 24, 10)
        header_layout.setSpacing(2)
        self.title = QLineEdit()
        self.title.setPlaceholderText("Page title")
        self.title.setAccessibleName("Page title")
        self.title.setFont(QFont("Sans Serif", PAGE_TITLE_POINT_SIZE, QFont.Weight.Bold))
        self.title.setFrame(False)
        self.title.setStyleSheet("QLineEdit {background: transparent; padding: 0;}")
        self.title.textEdited.connect(self.queue_save)
        header_layout.addWidget(self.title)
        self.last_edited = QLabel()
        self.last_edited.setTextFormat(Qt.TextFormat.PlainText)
        self.last_edited.setWordWrap(True)
        self.last_edited.setIndent(2)
        self.last_edited.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        header_layout.addWidget(self.last_edited)
        editor_layout.addWidget(self.page_header)

        self.canvas = PageCanvas()
        self.canvas.changed.connect(self.queue_save)
        self.canvas.link_requested.connect(self.open_link)
        editor_layout.addWidget(self.canvas, 1)
        splitter.addWidget(editor)
        pages = QWidget()
        pages.setMinimumWidth(150)
        pages_layout = QVBoxLayout(pages)
        pages_layout.setContentsMargins(12, 16, 14, 12)
        heading = QLabel("Pages")
        heading.setStyleSheet("font-size: 17px; font-weight: 600;")
        pages_layout.addWidget(heading)
        self.page_list = PageList()
        self.page_list.setAccessibleName("Pages")
        self.page_list.setUniformItemSizes(True)
        self.page_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.page_list.reordered.connect(self.reorder_pages)
        self.page_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.page_list.customContextMenuRequested.connect(self.page_menu)
        self.page_list.currentItemChanged.connect(self.select_page)
        pages_layout.addWidget(self.page_list)
        add_page = QPushButton("Add Page")
        add_page.clicked.connect(self.new_page)
        pages_layout.addWidget(add_page, alignment=Qt.AlignmentFlag.AlignLeft)
        splitter.addWidget(pages)
        splitter.setChildrenCollapsible(False)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([190, 795, 225])

        self.save_timer = QTimer(self, singleShot=True, interval=450)
        self.save_timer.timeout.connect(self.commit_page)
        self.sync_timer = QTimer(self, interval=15000)
        self.sync_timer.timeout.connect(self.sync_now)
        self.build_actions()
        self.refresh_navigation()
        if self.page_id is None:
            self.load_page(None)
        if self.page_id is None and not self.sync_url:
            self.statusBar().showMessage("Import a OneNote notebook or create a page.")
        if self.sync_url:
            self.sync_timer.start()

    def build_actions(self):
        file_menu = self.menuBar().addMenu("&File")

        def action(text, callback, shortcut=None):
            item = QAction(text, self)
            if shortcut:
                item.setShortcut(shortcut)
            item.triggered.connect(callback)
            return item

        self.import_action = action("&Import OneNote…", self.choose_import)
        self.import_report_action = action("Last Import Report…", self.show_import_report)
        self.import_report_action.setEnabled(bool(self.library.import_report()))
        self.new_action = action("&New Notebook…", self.new_notebook, QKeySequence.StandardKey.New)
        file_menu.addAction(self.new_action)
        file_menu.addAction(action("&Find", self.search.setFocus, QKeySequence.StandardKey.Find))
        self.sync_action = action("&Sync", self.sync_now)
        file_menu.addAction(self.sync_action)
        file_menu.addSeparator()
        setup_action = action("Page Set&up…", self.choose_page_setup)
        preview_action = action("Print Pre&view…", self.print_preview)
        print_action = action("&Print…", self.print_note, QKeySequence.StandardKey.Print)
        pdf_print_action = action("Print to &PDF…", self.print_to_pdf)
        file_menu.addActions([setup_action, preview_action, print_action, pdf_print_action])
        file_menu.addSeparator()
        file_menu.addAction(action("E&xit", self.close, QKeySequence.StandardKey.Quit))

        edit_menu = self.menuBar().addMenu("&Edit")
        undo_key = QKeySequence(QKeySequence.StandardKey.Undo).toString(QKeySequence.SequenceFormat.NativeText)
        edit_menu.addAction(action(f"&Undo\t{undo_key}", self.undo))
        edit_menu.addSeparator()
        for label, command in [
            ("Cu&t", QKeySequence.StandardKey.Cut),
            ("&Copy", QKeySequence.StandardKey.Copy),
            ("&Paste", QKeySequence.StandardKey.Paste),
        ]:
            # The focused editor already handles the shortcut itself.
            shortcut = QKeySequence(command).toString(QKeySequence.SequenceFormat.NativeText)
            edit_menu.addAction(action(
                f"{label}\t{shortcut}",
                lambda checked=False, command=command: self.edit_clipboard(command),
            ))

        insert_menu = self.menuBar().addMenu("&Insert")
        insert_menu.addAction(self.import_action)
        insert_menu.addAction(self.import_report_action)
        pdf_action = action("Import &PDF…", self.choose_pdf)
        picture_action = action("P&icture…", self.choose_picture)
        camera_action = action("&Camera…", self.capture_camera)
        screenshot_action = action("Screens&hot…", self.open_screenshot_tool)
        equation_action = action("&Equation…", self.add_equation)
        symbol_action = action("&Symbol…", self.choose_symbol)
        emoticon_action = action("E&moticon…", self.choose_emoticon)
        table_action = action("&Table…", self.choose_table)
        link_action = action("&Link…", self.choose_link)
        page_link_action = action("Link to &Page…", self.choose_page_link)
        insert_menu.addActions([
            pdf_action, picture_action, camera_action, screenshot_action, equation_action, symbol_action, table_action,
            emoticon_action, link_action, page_link_action,
        ])

        draw_menu = self.menuBar().addMenu("&Draw")
        format_menu = self.menuBar().addMenu("F&ormat")
        template_menu = format_menu.addMenu("&Template")
        for title, html in PAGE_TEMPLATES.items():
            template_menu.addAction(action(
                title, lambda checked=False, name=title, content=html: self.add_template(name, content),
            ))
        template_menu.addAction(action("Financial Ledger", self.add_ledger_template))
        self.background_menu = format_menu.addMenu("Page &Background")
        paper_group = QActionGroup(self.background_menu)
        self.paper_actions = {}
        for style, label in PAPER_STYLES.items():
            item = action(label, lambda checked=False, key=style: self.change_paper_style(key))
            item.setCheckable(True)
            paper_group.addAction(item)
            self.background_menu.addAction(item)
            self.paper_actions[style] = item

        view_menu = self.menuBar().addMenu("&View")
        self.focus_action = action("&Focus", self.set_focus_mode)
        self.focus_action.setCheckable(True)
        view_menu.addAction(self.focus_action)
        toolbars_menu = view_menu.addMenu("&Toolbars")

        def add_toolbar(title):
            toolbar = QToolBar(title, self)
            toolbar.setMovable(False)
            toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            self.addToolBar(toolbar)
            toolbars_menu.addAction(toolbar.toggleViewAction())
            return toolbar

        def add_button(toolbar, item, theme_name, glyph):
            icon = QIcon.fromTheme(theme_name)
            if icon.isNull():
                pixmap = QPixmap(24, 24)
                pixmap.fill(Qt.GlobalColor.transparent)
                painter = QPainter(pixmap)
                font = painter.font()
                font.setPixelSize(17 if len(glyph) == 1 else 12)
                font.setBold(True)
                painter.setFont(font)
                painter.setPen(toolbar.palette().color(QPalette.ColorRole.ButtonText))
                painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, glyph)
                painter.end()
                icon = QIcon(pixmap)
            item.setIcon(icon)
            toolbar.addAction(item)
            if item.menu() is not None:
                toolbar.widgetForAction(item).setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)

        add_button(add_toolbar("Notebook"), self.new_action, "document-new", "+")
        editing_toolbar = add_toolbar("Page Editing")
        insert_toolbar = add_toolbar("Insert")
        draw_toolbar = add_toolbar("Draw")
        formatting_toolbar = add_toolbar("Text Formatting")
        zoom_toolbar = add_toolbar("Zoom")

        for item, icon, glyph in [
            (picture_action, "insert-image", "▧"),
            (screenshot_action, "camera-photo", "◉"),
            (table_action, "insert-table", "▦"),
            (equation_action, "insert-math-expression", "∑"),
            (symbol_action, "insert-text-symbol", "Ω"),
        ]:
            add_button(insert_toolbar, item, icon, glyph)

        self.tool_group = QActionGroup(self)
        self.editor_actions = [
            pdf_action, picture_action, camera_action, symbol_action, emoticon_action, table_action, link_action,
            page_link_action,
            preview_action, print_action, pdf_print_action, self.focus_action,
        ]
        tools = {}
        for label, tool, icon, glyph in [
            ("Select", "select", "edit-select", "↖"),
            ("Pen", "ink", "draw-freehand", "✎"),
            ("Eraser", "erase", "draw-eraser", "▱"),
        ]:
            item = action(label, lambda checked=False, value=tool: self.canvas.set_tool(value))
            item.setCheckable(True)
            self.tool_group.addAction(item)
            add_button(editing_toolbar, item, icon, glyph)
            if tool != "select":
                draw_toolbar.addAction(item)
            self.editor_actions.append(item)
            tools[tool] = item
            if tool == "select":
                self.select_tool = item
                item.setChecked(True)
        def mark_tool(tool):
            if tool in tools:
                tools[tool].setChecked(True)

        self.canvas.tool_changed.connect(mark_tool)
        draw_menu.addActions([tools["ink"], tools["erase"]])
        draw_menu.addSeparator()
        pen_color = action("Pen &Color…", self.choose_pen_color)
        pen_width = action("Pen &Thickness…", self.choose_pen_width)
        draw_menu.addActions([pen_color, pen_width])
        add_button(draw_toolbar, pen_color, "color-picker", "●")
        add_button(draw_toolbar, pen_width, "draw-line", "━")
        self.editor_actions.extend([pen_color, pen_width])
        shapes_menu = draw_menu.addMenu("&Basic Shapes")
        for shape in SHAPE_NAMES:
            item = action(shape.title(), lambda checked=False, kind=shape: self.canvas.add_shape(kind))
            shapes_menu.addAction(item)
            self.editor_actions.append(item)

        editing_toolbar.addSeparator()
        paste_image_action = action("Paste Image", self.paste_image)
        for item, icon, glyph in [
            (action("Add Text", self.add_text), "insert-text", "T+"),
            (paste_image_action, "edit-paste", "▤"),
            (action("Remove", self.canvas.delete_selected), "edit-delete", "×"),
        ]:
            add_button(editing_toolbar, item, icon, glyph)
            self.editor_actions.append(item)

        editing_toolbar.insertAction(paste_image_action, equation_action)
        self.editor_actions.append(equation_action)

        self.style_selector = QComboBox()
        self.style_selector.setAccessibleName("Paragraph style")
        self.style_selector.setPlaceholderText("Style")
        for label, style in [
            ("Body Text", "body"),
            ("Page Title", "title"),
            ("Heading 1", "heading1"),
            ("Heading 2", "heading2"),
            ("Heading 3", "heading3"),
            ("Heading 4", "heading4"),
            ("Heading 5", "heading5"),
            ("Heading 6", "heading6"),
            ("Code Block", "code"),
            ("Quote", "quote"),
        ]:
            self.style_selector.addItem(label, style)
        self.style_selector.activated.connect(self.apply_text_style, Qt.ConnectionType.QueuedConnection)
        self.canvas.text_style_changed.connect(self.update_style_selector)
        self.update_style_selector(self.canvas.text_style_at_cursor())
        formatting_toolbar.addWidget(self.style_selector)

        self.font_selector = QComboBox()
        self.font_selector.setAccessibleName("Font family")
        self.font_selector.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.font_selector.setMinimumContentsLength(15)
        self.font_selector.addItems(QFontDatabase.families())
        # Let the popup release focus before returning it to the text editor.
        self.font_selector.textActivated.connect(self.apply_font_family, Qt.ConnectionType.QueuedConnection)
        self.canvas.text_font_changed.connect(self.update_font_selector)
        self.update_font_selector(self.canvas.text_font_family())
        formatting_toolbar.addWidget(self.font_selector)

        for label, callback, icon, glyph in [
            ("Font Size…", self.choose_font_size, "format-font-size", "pt"),
            ("Font Color…", lambda: self.choose_text_color("color"), "format-text-color", "A▂"),
            ("Highlight Color…", lambda: self.choose_text_color("highlight"), "format-fill-color", "▨"),
        ]:
            item = action(label, callback)
            add_button(formatting_toolbar, item, icon, glyph)
            self.editor_actions.append(item)
        formatting_toolbar.addSeparator()

        bullet_list = QAction("Bulleted List", self)
        bullet_menu = QMenu("Bullet Style", self)
        for label, bullet in [
            ("•  Solid Bullet", "disc"),
            ("◦  Hollow Bullet", "circle"),
            ("▪  Square Bullet", "square"),
            ("☐  To-Do Checkbox", "checkbox"),
        ]:
            bullet_menu.addAction(action(
                label, lambda checked=False, value=bullet: self.canvas.set_format("bullet", value),
            ))
        bullet_list.setMenu(bullet_menu)

        for label, style, icon, glyph in [
            ("Bold", "bold", "format-text-bold", "B"),
            ("Italic", "italic", "format-text-italic", "I"),
            ("Underline", "underline", "format-text-underline", "U"),
            ("Strikethrough", "strike", "format-text-strikethrough", "S̶"),
            ("Superscript", "superscript", "format-text-superscript", "x²"),
            ("Subscript", "subscript", "format-text-subscript", "x₂"),
            ("Bulleted List", "bullet", "format-list-unordered", "•≡"),
            ("Numbered List", "number", "format-list-ordered", "1≡"),
            ("Increase Indent", "indent", "format-indent-more", "⇥"),
            ("Decrease Indent", "outdent", "format-indent-less", "⇤"),
        ]:
            item = bullet_list if style == "bullet" else action(
                label, lambda checked=False, value=style: self.canvas.set_format(value),
            )
            add_button(formatting_toolbar, item, icon, glyph)
            self.editor_actions.append(item)

        alignment = QAction("Justification", self)
        alignment_menu = QMenu("Justification", self)
        for label, flag in [
            ("Align Left", Qt.AlignmentFlag.AlignLeft),
            ("Center", Qt.AlignmentFlag.AlignHCenter),
            ("Align Right", Qt.AlignmentFlag.AlignRight),
            ("Justify", Qt.AlignmentFlag.AlignJustify),
        ]:
            alignment_menu.addAction(action(
                label, lambda checked=False, value=flag: self.canvas.set_format("alignment", value),
            ))
        alignment.setMenu(alignment_menu)
        add_button(formatting_toolbar, alignment, "format-justify-fill", "≡")
        self.editor_actions.append(alignment)
        for label, callback, icon, glyph in [
            ("Zoom In", lambda: self.canvas.zoom_by(1.2), "zoom-in", "+"),
            ("Zoom Out", lambda: self.canvas.zoom_by(1 / 1.2), "zoom-out", "−"),
            ("Actual Size", lambda: self.canvas.set_zoom(1.0), "zoom-original", "1:1"),
        ]:
            add_button(zoom_toolbar, action(label, callback), icon, glyph)

    def set_focus_mode(self, enabled):
        enabled = enabled and self.page_id is not None
        self.focus_action.setChecked(enabled)
        splitter = self.centralWidget()
        app = QApplication.instance()
        if enabled and self._focus_layout is None:
            direct_children = Qt.FindChildOption.FindDirectChildrenOnly
            had_status_bar = self.findChild(QStatusBar, options=direct_children) is not None
            status_bar = self.statusBar()
            widgets = [
                splitter.widget(0), splitter.widget(2), self.menuBar(),
                *self.findChildren(QToolBar, options=direct_children),
            ]
            visible = [widget for widget in widgets if not widget.isHidden()]
            if not had_status_bar or not status_bar.isHidden():
                visible.append(status_bar)
            self._focus_layout = (splitter.sizes(), visible, None if had_status_bar else status_bar)
            for widget in visible:
                widget.hide()
            self.sections.hide()
            editor = splitter.widget(1)
            focus = self.focusWidget()
            if focus is None or not editor.isAncestorOf(focus):
                self.canvas.setFocus()
            app.installEventFilter(self)
        elif not enabled and self._focus_layout is not None:
            app.removeEventFilter(self)
            sizes, visible, temporary_status_bar = self._focus_layout
            self._focus_layout = None
            if temporary_status_bar is not None and not temporary_status_bar.currentMessage():
                visible = [widget for widget in visible if widget is not temporary_status_bar]
                self.setStatusBar(None)
            for widget in visible:
                widget.show()
            self.sections.setVisible(self.sections.count() > 0)
            splitter.setSizes(sizes)

    def eventFilter(self, watched, event):
        if (
            self._focus_layout is not None
            and event.type() == QEvent.Type.KeyPress
            and event.key() == Qt.Key.Key_Escape
            and event.modifiers() == Qt.KeyboardModifier.NoModifier
            and isinstance(watched, QWidget)
            and watched.window() is self
            and QApplication.activePopupWidget() is None
            and QApplication.activeModalWidget() is None
        ):
            self.set_focus_mode(False)
            return True
        return super().eventFilter(watched, event)

    def choose_page_setup(self):
        dialog = PageSetupDialog(self.print_layout, self)
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted:
                layout = dialog.page_layout()
                self.library.save_page_setup(page_setup_data(layout))
                self.print_layout = layout
        finally:
            dialog.deleteLater()

    def print_preview(self):
        title = self.title.text().strip() or "Untitled"
        layout = QPageLayout(self.print_layout)
        try:
            with TemporaryDirectory(prefix="notebook-preview-") as folder:
                path = Path(folder) / "preview.pdf"
                self.write_note_pdf(path)
                dialog = PrintPreview(path, self)
                dialog.setWindowTitle(f"Print Preview — {layout.pageSize().name()}, {layout.orientation().name}")
                try:
                    result = dialog.exec()
                finally:
                    dialog.deleteLater()
                if result == PrintPreview.PRINT:
                    self.print_note(preview=path, title=title, layout=layout)
                elif result == PrintPreview.SAVE_PDF:
                    self.print_to_pdf(preview=path, title=title)
        except (OSError, RuntimeError) as error:
            QMessageBox.warning(self, "Print Preview", str(error))

    def print_note(self, *, preview=None, title=None, layout=None):
        title = title or self.title.text().strip() or "Untitled"
        layout = QPageLayout(self.print_layout) if layout is None else layout
        try:
            from PySide6.QtPrintSupport import QPrintDialog, QPrinter
        except ImportError:
            self.print_with_cups(preview=preview, title=title, layout=layout)
            return
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        printer.setDocName(title)
        if printer.isValid():
            printer.setPageLayout(layout)
        dialog = QPrintDialog(printer, self)
        dialog.setOption(QPrintDialog.PrintDialogOption.PrintShowPageSize, False)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                if not printer.setPageLayout(layout):
                    raise RuntimeError(
                        "The selected printer cannot use these Page Setup settings. "
                        "Choose a supported paper size and margins in File → Page Setup."
                    )
                self.save_page()
                self.canvas.print_page(printer, title, self.last_edited.text())
            except RuntimeError as error:
                QMessageBox.warning(self, "Print", str(error))

    def print_to_pdf(self, *, preview=None, title=None):
        title = title or self.title.text().strip() or "Untitled"
        filename = title.replace("/", "_").replace("\\", "_") + ".pdf"
        path, _ = QFileDialog.getSaveFileName(self, "Print to PDF", filename, "PDF files (*.pdf)")
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
            if Path(path).exists() and QMessageBox.question(
                self, "Replace PDF", f"Replace {Path(path).name}?"
            ) != QMessageBox.StandardButton.Yes:
                return
        try:
            self.write_note_pdf(path, preview=preview)
        except (OSError, RuntimeError) as error:
            QMessageBox.warning(self, "Print to PDF", str(error))
        else:
            self.statusBar().showMessage(f"Saved {Path(path).name}", 5000)

    def write_note_pdf(self, path, *, preview=None):
        if preview is None:
            self.save_page()
        output = QSaveFile(str(path))
        if not output.open(QIODevice.OpenModeFlag.WriteOnly):
            raise RuntimeError(output.errorString())
        if preview is None:
            title = self.title.text().strip() or "Untitled"
            writer = QPdfWriter(output)
            writer.setResolution(PRINT_DPI)
            writer.setTitle(title)
            try:
                if not writer.setPageLayout(self.print_layout):
                    raise RuntimeError("The PDF could not use these Page Setup settings.")
                self.canvas.print_page(writer, title, self.last_edited.text())
            finally:
                del writer
        else:
            with preview.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    if output.write(chunk) != len(chunk):
                        raise RuntimeError(output.errorString())
        if not output.commit():
            raise RuntimeError(output.errorString())

    def print_with_cups(self, *, preview=None, title=None, layout=None):
        lpstat, lp = shutil.which("lpstat"), shutil.which("lp")
        if not lpstat or not lp:
            QMessageBox.warning(self, "Print", "Install Qt Print Support to use system printers.")
            return
        try:
            result = subprocess.run([lpstat, "-a"], capture_output=True, text=True, timeout=5)
            queues = [line.split(maxsplit=1)[0] for line in result.stdout.splitlines() if line.strip()]
            if not queues:
                QMessageBox.information(self, "Print", "No printers are available. Use Print to PDF to save a copy.")
                return
            queue, accepted = QInputDialog.getItem(self, "Print", "Printer", queues, editable=False)
            if not accepted:
                return
            title = title or self.title.text().strip() or "Untitled"
            layout = self.print_layout if layout is None else layout
            orientation = 4 if layout.orientation() == QPageLayout.Orientation.Landscape else 3
            options = [
                "-o", f"media={layout.pageSize().key()}",
                "-o", f"orientation-requested={orientation}",
                "-o", "print-scaling=none",
            ]

            def submit(path):
                subprocess.run(
                    [lp, "-d", queue, "-t", title, *options, str(path)],
                    check=True, capture_output=True, timeout=15,
                )

            if preview is None:
                with TemporaryDirectory() as folder:
                    path = Path(folder) / "note.pdf"
                    self.write_note_pdf(path)
                    submit(path)
            else:
                submit(preview)
        except (OSError, subprocess.SubprocessError, RuntimeError) as error:
            QMessageBox.warning(self, "Print", f"Could not print the page. {error}")
        else:
            self.statusBar().showMessage(f"Sent to {queue}", 5000)

    def update_style_selector(self, style):
        self.style_selector.setCurrentIndex(self.style_selector.findData(style))
        self.style_selector.setEnabled(bool(style))

    def apply_text_style(self, index):
        self.canvas.apply_text_style(self.style_selector.itemData(index))

    def update_font_selector(self, family):
        # Preserve imported font names even when the family is absent locally.
        self.font_selector.setPlaceholderText(family or "Font")
        self.font_selector.setCurrentIndex(self.font_selector.findText(family))
        self.font_selector.setEnabled(bool(family))

    def apply_font_family(self, family):
        self.canvas.set_format("font", family)

    def choose_font_size(self):
        size, accepted = QInputDialog.getDouble(self, "Font Size", "Size (pt)", 12, 1, 512, 1)
        if accepted:
            self.canvas.set_format("size", size)

    def choose_text_color(self, style):
        title, default = ("Font Color", "#202a3d") if style == "color" else ("Highlight Color", "#fff59d")
        color = QColorDialog.getColor(QColor(default), self, title)
        if color.isValid():
            self.canvas.set_format(style, color)

    def choose_pen_color(self):
        color = QColorDialog.getColor(self.canvas.pen_color, self, "Pen Color")
        if color.isValid():
            self.canvas.pen_color = color
            self.canvas.set_tool("ink")

    def choose_pen_width(self):
        width, accepted = QInputDialog.getDouble(
            self, "Pen Thickness", "Width (px)", self.canvas.pen_width, 0.25, 100, 2,
        )
        if accepted:
            self.canvas.pen_width = width
            self.canvas.set_tool("ink")

    def change_paper_style(self, style):
        if self.page_id and style != self.canvas.paper_style:
            self.canvas.set_paper_style(style)
            self.queue_save()

    def undo(self):
        target = self.focusWidget()
        if isinstance(target, QLineEdit):
            target.undo()
        elif self.page_id:
            self.canvas.undo()

    def edit_clipboard(self, command):
        target = self.focusWidget()
        if target is None or not target.isEnabled():
            return
        key = QKeySequence(command)[0]
        QApplication.sendEvent(target, QKeyEvent(
            QEvent.Type.KeyPress, key.key(), key.keyboardModifiers(),
        ))

    def add_text(self):
        self.select_tool.setChecked(True)
        self.canvas.set_tool("select")
        self.canvas.add_text()

    def add_equation(self):
        self.select_tool.setChecked(True)
        self.canvas.set_tool("select")
        self.canvas.add_equation()

    def choose_link(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Insert Link")
        form = QFormLayout(dialog)
        address = QLineEdit(dialog)
        address.setPlaceholderText("https://example.com")
        label = QLineEdit(self.canvas.selected_text(), dialog)
        label.setPlaceholderText("Use the link address")
        form.addRow("&Address", address)
        form.addRow("&Display text", label)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            dialog,
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        address.setFocus()
        while dialog.exec() == QDialog.DialogCode.Accepted:
            url = QUrl.fromUserInput(address.text().strip())
            if self.is_external_link(url):
                self.canvas.insert_link(url.toString(QUrl.ComponentFormattingOption.FullyEncoded), label.text().strip())
                break
            QMessageBox.warning(dialog, "Invalid Link", "Enter a web address or a mailto: link.")

    def choose_page_link(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Link to Page")
        layout = QVBoxLayout(dialog)
        tree = QTreeWidget(dialog)
        tree.setHeaderLabel("Choose a notebook page")
        tree.setMinimumSize(400, 300)
        layout.addWidget(tree)
        notebooks = {}
        sections = {}
        for page_id, page in self.library.pages(metadata_only=True):
            notebook_id = page["notebook_id"]
            if notebook_id not in notebooks:
                notebook = QTreeWidgetItem(tree, [page["notebook"]])
                notebook.setFlags(notebook.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                notebooks[notebook_id] = notebook
            section_id = page["section_id"]
            if section_id not in sections:
                section_name = " / ".join([*page["section_path"], page["section"]])
                section = QTreeWidgetItem(notebooks[notebook_id], [section_name])
                section.setFlags(section.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                sections[section_id] = section
            item = QTreeWidgetItem(sections[section_id], [page["title"]])
            item.setData(0, Qt.ItemDataRole.UserRole, page_id)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            dialog,
        )
        ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setEnabled(False)
        tree.currentItemChanged.connect(
            lambda item, previous: ok.setEnabled(bool(item and item.data(0, Qt.ItemDataRole.UserRole)))
        )
        tree.itemDoubleClicked.connect(lambda item, column: dialog.accept() if ok.isEnabled() else None)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        tree.expandAll()
        if dialog.exec() == QDialog.DialogCode.Accepted:
            item = tree.currentItem()
            if item and (page_id := item.data(0, Qt.ItemDataRole.UserRole)):
                label = self.canvas.selected_text() or item.text(0)
                self.canvas.insert_link(f"notebook-page:{page_id}", label)

    @staticmethod
    def is_external_link(url):
        return url.isValid() and (
            (url.scheme() in {"http", "https"} and bool(url.host()))
            or (url.scheme() == "mailto" and bool(url.path()))
        )

    def open_link(self, address):
        url = QUrl(address)
        if url.scheme() == "notebook-page":
            target = url.path()
            if any(page_id == target for page_id, _ in self.library.pages(metadata_only=True)):
                self.save_page()
                with QSignalBlocker(self.search):
                    self.search.clear()
                self.refresh_navigation(target)
            else:
                self.statusBar().showMessage("The linked page is unavailable.", 5000)
        elif self.is_external_link(url) and not QDesktopServices.openUrl(url):
            self.statusBar().showMessage("Could not open the link.", 5000)

    def choose_table(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Insert Table")
        form = QFormLayout(dialog)
        rows = QSpinBox(dialog)
        rows.setRange(1, 100)
        rows.setValue(3)
        columns = QSpinBox(dialog)
        columns.setRange(1, 50)
        columns.setValue(3)
        form.addRow("&Rows", rows)
        form.addRow("&Columns", columns)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            dialog,
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.canvas.insert_table(rows.value(), columns.value())

    def choose_symbol(self):
        picker = UnicodePicker(self)
        if picker.exec() == QDialog.DialogCode.Accepted and picker.character:
            self.canvas.insert_symbol(picker.character)

    def choose_emoticon(self):
        emoticon, accepted = QInputDialog.getItem(
            self, "Insert Emoticon", "Emoticon",
            ["🙂", "😀", "😂", "😉", "😍", "😎", "🤔", "😢", "😠", "👍", "❤️",
             ":-)", ":-D", ";-)", ":-P", ":-(", "¯\\_(ツ)_/¯", "(＾▽＾)"],
            editable=False,
        )
        if accepted:
            self.canvas.insert_symbol(emoticon)

    def choose_pdf(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import PDF", "", "PDF files (*.pdf)")
        if not path:
            return
        try:
            count = self.canvas.insert_pdf(path)
        except (ValueError, RuntimeError) as error:
            QMessageBox.warning(self, "PDF import", str(error))
            return
        self.save_page()
        self.statusBar().showMessage(f"Inserted and saved {count} PDF page(s)", 5000)

    def choose_picture(self):
        formats = sorted(bytes(fmt).decode("ascii") for fmt in QImageReader.supportedImageFormats())
        patterns = " ".join(f"*.{fmt}" for fmt in formats)
        path, _ = QFileDialog.getOpenFileName(
            self, "Insert Picture", "", f"Image files ({patterns});;All files (*)",
        )
        if not path:
            return
        reader = QImageReader(path)
        reader.setAutoTransform(True)
        image = reader.read()
        if image.isNull():
            QMessageBox.warning(self, "Insert Picture", reader.errorString())
        elif not self.canvas.paste_image(image):
            QMessageBox.warning(self, "Insert Picture", "The image could not be added to the page.")

    def capture_camera(self):
        page_id = self.page_id
        if page_id is None:
            return
        try:
            from camera_capture import CameraCaptureDialog

            dialog = CameraCaptureDialog(self)
        except ImportError:
            QMessageBox.information(
                self, "Insert Camera Image", "Camera capture requires Qt Multimedia and Qt Multimedia Widgets.",
            )
            return
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            if self.page_id != page_id:
                QMessageBox.information(self, "Insert Camera Image", "The page changed. Open it and try again.")
            elif not self.canvas.paste_image(dialog.image):
                QMessageBox.warning(self, "Insert Camera Image", "The photo could not be added to the page.")
        finally:
            dialog.deleteLater()

    def open_screenshot_tool(self):
        if sys.platform == "darwin":
            commands = [("/usr/bin/open", ["-b", "com.apple.screencaptureui"])]
        elif sys.platform.startswith("linux"):
            desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower().split(":")
            tools = {
                "xfce": ("xfce4-screenshooter", []),
                "kde": ("spectacle", ["--gui"]),
                "gnome": ("gnome-screenshot", ["--interactive"]),
                "cinnamon": ("cinnamon-screenshot", ["--interactive"]),
                "mate": ("mate-screenshot", ["--interactive"]),
            }
            commands = [tools[name] for name in desktop if name in tools]
            commands.extend(tool for tool in tools.values() if tool not in commands)
        else:
            commands = []
        for program, arguments in commands:
            executable = QStandardPaths.findExecutable(program)
            if executable and QProcess.startDetached(executable, arguments)[0]:
                self.statusBar().showMessage("Paste the capture or insert the saved picture.", 7000)
                return
        QMessageBox.information(
            self, "Screenshot", "Use your system’s screenshot shortcut, then paste the capture "
            "or insert the saved picture.",
        )

    def paste_image(self):
        image = QApplication.clipboard().image()
        if image.isNull():
            self.statusBar().showMessage("Copy a screenshot or image first.", 5000)
        else:
            self.select_tool.setChecked(True)
            self.canvas.set_tool("select")
            self.canvas.paste_image(image)

    def filter_pages(self):
        self.save_page()
        self.refresh_navigation()

    @staticmethod
    def populate_list(widget, entries, selected, *, colored=False):
        row = 0
        with QSignalBlocker(widget):
            widget.clear()
            for index, (entry_id, title) in enumerate(entries):
                item = QListWidgetItem(title, widget)
                if colored:
                    item.setIcon(navigation_color_icon(entry_id))
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsDropEnabled)
                item.setData(Qt.ItemDataRole.UserRole, entry_id)
                item.setToolTip(title)
                if entry_id == selected:
                    row = index
            widget.setCurrentRow(row if widget.count() else -1)
        item = widget.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def refresh_navigation(self, target=None, reload=False):
        rows = self.library.pages(self.search.text(), metadata_only=True)
        target = target or self.page_id
        selected = next((page for page_id, page in rows if page_id == target), None)
        notebook = self.notebooks.currentItem()
        notebook_id = selected["notebook_id"] if selected else (
            notebook.data(Qt.ItemDataRole.UserRole) if notebook else None
        )
        section_id = selected["section_id"] if selected else self.sections.tabData(
            self.sections.currentIndex()
        )
        # Use the saved notebook, section, and page order from the library.
        self.navigation_rows = rows
        self.sections.setMovable(not self.search.text().strip())
        self.page_list.setDragEnabled(not self.search.text().strip())
        notebooks = {page["notebook_id"]: page["notebook"] for _, page in rows}
        notebook_id = self.populate_list(self.notebooks, notebooks.items(), notebook_id, colored=True)
        self.show_sections(notebook_id, section_id, target, reload)

    def show_sections(self, notebook_id, selected=None, target=None, reload=False):
        sections = {
            page["section_id"]: " / ".join([*page["section_path"], page["section"]])
            for _, page in self.navigation_rows if page["notebook_id"] == notebook_id
        }
        tab = 0
        with QSignalBlocker(self.sections):
            while self.sections.count():
                self.sections.removeTab(self.sections.count() - 1)
            for section_id, title in sections.items():
                index = self.sections.addTab(navigation_color_icon(section_id), title)
                self.sections.setTabData(index, section_id)
                self.sections.setTabToolTip(index, title)
                if section_id == selected:
                    tab = index
            if sections:
                plus = self.sections.addTab("+")
                self.sections.setTabToolTip(plus, "New Section")
                self.sections.setAccessibleTabName(plus, "New Section")
            self.sections.setCurrentIndex(tab if sections else -1)
        self.sections.setVisible(bool(sections) and self._focus_layout is None)
        self.show_pages(self.sections.tabData(self.sections.currentIndex()), target, reload)

    def show_pages(self, section_id, target=None, reload=False):
        entries = (
            (page_id, page["title"]) for page_id, page in self.navigation_rows
            if page["section_id"] == section_id
        )
        selected = self.populate_list(self.page_list, entries, target)
        if selected != self.page_id or reload:
            self.load_page(selected)

    def select_notebook(self, item, previous):
        if item is not None:
            self.save_page()
            self.show_sections(item.data(Qt.ItemDataRole.UserRole))

    def select_section(self, index):
        if index < 0:
            return
        section_id = self.sections.tabData(index)
        if section_id is None:
            current_section = self.library.page(self.page_id)["section_id"] if self.page_id else None
            for tab in range(self.sections.count()):
                if self.sections.tabData(tab) == current_section and current_section is not None:
                    with QSignalBlocker(self.sections):
                        self.sections.setCurrentIndex(tab)
                    break
            self.new_section()
            return
        self.save_page()
        self.show_pages(section_id, self.page_id)

    def select_page(self, item, previous):
        if item is not None:
            page_id = item.data(Qt.ItemDataRole.UserRole)
            if page_id != self.page_id:
                self.save_page()
                self.load_page(page_id)

    def reorder_sections(self, source, destination):
        self.save_page()
        notebook = self.notebooks.currentItem()
        if notebook is not None:
            last = self.sections.count() - 1
            plus = last
            order = []
            for index in range(self.sections.count()):
                section_id = self.sections.tabData(index)
                if section_id is None:
                    plus = index
                else:
                    order.append(section_id)
            if plus != last:
                with QSignalBlocker(self.sections):
                    self.sections.moveTab(plus, last)
            self.library.reorder_sections(notebook.data(Qt.ItemDataRole.UserRole), order)
            self.navigation_rows = self.library.pages(metadata_only=True)

    def reorder_pages(self):
        self.save_page()
        section_id = self.sections.tabData(self.sections.currentIndex())
        self.library.reorder_pages(section_id, self.page_list.page_ids())
        self.refresh_navigation()

    def section_menu(self, point):
        index = self.sections.tabAt(point)
        if index >= 0 and (section_id := self.sections.tabData(index)) is not None:
            self.navigation_menu("section", section_id, self.sections.mapToGlobal(point))

    def page_menu(self, point):
        item = self.page_list.itemAt(point)
        if item is not None:
            self.navigation_menu("page", item.data(Qt.ItemDataRole.UserRole),
                                 self.page_list.viewport().mapToGlobal(point))

    def navigation_menu(self, kind, item_id, position):
        menu = QMenu(self)
        rename = menu.addAction("Rename…")
        delete = menu.addAction("Delete…")
        menu.addSeparator()
        cut = menu.addAction("Cut")
        copy = menu.addAction("Copy")
        paste = menu.addAction("Paste")
        paste.setEnabled(self.navigation_clipboard is not None and (
            kind == "section" or self.navigation_clipboard[0] == "page"
        ))
        choice = menu.exec(position)
        if choice is None:
            return
        self.save_page()
        if choice == paste:
            self.paste_navigation(kind, item_id)
            return
        rows = self.navigation_records(kind, item_id)
        if not rows:
            return
        title = rows[0][1]["section" if kind == "section" else "title"]
        if choice == rename:
            name, accepted = QInputDialog.getText(self, "Rename " + kind, "Name", text=title)
            if accepted and name.strip():
                if kind == "section":
                    self.library.rename_section(item_id, name.strip())
                else:
                    current = self.library.page(item_id)
                    if not current.get("deleted"):
                        self.set_page_title(current, name.strip())
                        self.library.save(item_id, current)
                self.refresh_navigation(reload=True)
        elif choice == delete:
            detail = " and all its pages" if kind == "section" else ""
            answer = QMessageBox.question(self, "Delete " + kind, f'Delete “{title}”{detail}?')
            if answer == QMessageBox.StandardButton.Yes:
                self.library.delete_pages([page_id for page_id, _ in rows])
                self.refresh_navigation()
        elif choice in (cut, copy):
            self.navigation_clipboard = (kind, item_id, choice == cut, rows)
            message = "Right-click a destination and choose Paste to move it." if choice == cut else "Copied."
            self.statusBar().showMessage(message, 5000)

    def navigation_records(self, kind, item_id):
        if kind == "page":
            page = self.library.page(item_id)
            return [] if page.get("deleted") else [(item_id, page)]
        return [(page_id, self.library.page(page_id)) for page_id, page in self.library.pages(metadata_only=True)
                if page["section_id"] == item_id]

    def paste_navigation(self, target_kind, target_id):
        if self.navigation_clipboard is None:
            return
        kind, source_id, moving, records = self.navigation_clipboard
        if kind == "section" and target_kind != "section":
            return
        if moving:
            records = self.navigation_records(kind, source_id)
            if not records:
                self.navigation_clipboard = None
                return
        destination = self.navigation_records(target_kind, target_id)
        if not destination:
            return
        target = destination[0][1]
        self.paste_navigation_records(kind, moving, records, target, target_kind, target_id)

    def paste_navigation_records(self, kind, moving, records, target, target_kind, target_id):
        target_section = target["section_id"]
        source_id = records[0][0] if kind == "page" else records[0][1]["section_id"]
        if moving and kind == target_kind and source_id == target_id:
            return
        changes = []
        existing = self.library.pages(metadata_only=True)
        if kind == "section":
            section_id = source_id if moving else uuid4().hex
            notebook_id = target["notebook_id"]
            siblings = dict.fromkeys(page["section_id"] for _, page in existing
                                     if page["notebook_id"] == notebook_id and page["section_id"] != section_id)
            order = list(siblings)
            order.insert(order.index(target_section) + 1, section_id)
            ranks = {id_: rank for rank, id_ in enumerate(order)}
            for page_id, page in existing:
                if page["section_id"] in siblings:
                    changes.append((page_id, {**self.library.page(page_id),
                                              "section_order": ranks[page["section_id"]]}))
            parent = {"notebook_id": notebook_id, "notebook": target["notebook"],
                      "section_id": section_id, "section": records[0][1]["section"],
                      "section_path": target["section_path"], "section_order": ranks[section_id]}
            inserted = [(page_id if moving else uuid4().hex, {**page, **parent, "page_order": rank})
                        for rank, (page_id, page) in enumerate(records)]
        else:
            page_id = source_id if moving else uuid4().hex
            siblings = [id_ for id_, page in existing if page["section_id"] == target_section and id_ != page_id]
            position = siblings.index(target_id) + 1 if target_kind == "page" else len(siblings)
            siblings.insert(position, page_id)
            parent = {key: target[key] for key in ("notebook_id", "notebook", "section_id", "section", "section_path")}
            target_order = next(page["section_order"] for _, page in existing
                                if page["section_id"] == target_section)
            parent["section_order"] = target_order
            for rank, sibling_id in enumerate(siblings):
                if sibling_id != page_id:
                    payload = self.library.page(sibling_id)
                    changes.append((sibling_id, {**payload, "page_order": rank, "section_order": target_order}))
            inserted = [(page_id, {**records[0][1], **parent, "page_order": position})]
        self.library.save_pages([*changes, *inserted])
        if moving:
            self.navigation_clipboard = None
        with QSignalBlocker(self.search):
            self.search.clear()
        self.refresh_navigation(inserted[0][0])

    @staticmethod
    def set_page_title(page, title):
        if title != page["title"]:
            header, _ = split_imported_header(page["objects"], page["title"])
            if header is not None and not header.get(IMPORTED_PAGE_HEADER):
                page["objects"] = [{**header, IMPORTED_PAGE_HEADER: True}, *page["objects"][1:]]
            page["title"] = title

    def update_last_edited(self, modified_at):
        timestamp = QDateTime.fromString(modified_at, Qt.DateFormat.ISODate) if isinstance(modified_at, str) else QDateTime()
        if timestamp.isValid():
            local = timestamp.toLocalTime()
            locale = QLocale()
            day = locale.toString(local.date(), QLocale.FormatType.LongFormat)
            time = locale.toString(local.time(), QLocale.FormatType.ShortFormat)
            self.last_edited.setText(f"Last edited {day} at {time}")
        elif self._original_page_date:
            self.last_edited.setText(f"Original page date {self._original_page_date}")
        else:
            self.last_edited.setText("Last edit time unavailable")

    def load_page(self, page_id):
        self.loading = True
        self.page_id = page_id
        if page_id is None and self._focus_layout is not None:
            self.set_focus_mode(False)
        try:
            page = self.library.page(page_id) if page_id else {"title": "", "objects": []}
            self.title.setText(page["title"])
            objects = page["objects"]
            self._imported_header, self._original_page_date = split_imported_header(objects, page["title"])
            self.canvas.set_page(objects[1:] if self._imported_header is not None else objects)
            self._saved_objects = self.canvas.objects()
            self.update_last_edited(page.get("modified_at"))
            self.page_header.setVisible(bool(page_id))
            style = page.get("paper_style", "white")
            if not isinstance(style, str) or style not in PAPER_STYLES:
                style = "white"
            self.canvas.set_paper_style(style)
            self.paper_actions[style].setChecked(True)
            self.background_menu.setEnabled(bool(page_id))
            self.select_tool.setChecked(True)
            self.canvas.set_tool("select")
            self.title.setEnabled(bool(page_id))
            self.canvas.setEnabled(bool(page_id))
            for item in self.editor_actions:
                item.setEnabled(bool(page_id))
        finally:
            self.loading = False

    def queue_save(self):
        if self.page_id and not self.loading:
            self.statusBar().showMessage("Saving locally…")
            self.save_timer.start()

    def save_page(self):
        if not self.save_timer.isActive():
            return
        self.save_timer.stop()
        self.commit_page()

    def commit_page(self):
        if self.page_id and not self.loading:
            payload = self.library.page(self.page_id)
            self.set_page_title(payload, self.title.text().strip() or "Untitled")
            objects = self.canvas.objects()
            if objects != self._saved_objects:
                if self._imported_header is not None:
                    original = {**self._imported_header, IMPORTED_PAGE_HEADER: True}
                    payload["objects"] = [original, *objects]
                else:
                    payload["objects"] = objects
            if self.canvas.paper_style != "white" or "paper_style" in payload:
                payload["paper_style"] = self.canvas.paper_style
            modified_at = self.library.save(self.page_id, payload)
            self._saved_objects = objects
            self.update_last_edited(modified_at)
            for page_id, metadata in self.navigation_rows:
                if page_id == self.page_id:
                    metadata["title"] = payload["title"]
                    break
            for row in range(self.page_list.count()):
                item = self.page_list.item(row)
                if item.data(Qt.ItemDataRole.UserRole) == self.page_id:
                    item.setText(payload["title"])
                    item.setToolTip(payload["title"])
                    break
            self.statusBar().showMessage("Saved on this device")

    def new_section(self):
        notebook = self.notebooks.currentItem()
        if notebook is None:
            return
        notebook_id = notebook.data(Qt.ItemDataRole.UserRole)
        notebook_title = notebook.text()
        name, accepted = QInputDialog.getText(self, "New Section", "Section name")
        if not accepted or not name.strip():
            return
        self.save_page()
        last_order = max(
            (page["section_order"] for _, page in self.library.pages(metadata_only=True)
             if page["notebook_id"] == notebook_id),
            default=-1,
        )
        page_id = self.library.add_page("Untitled", {
            "notebook_id": notebook_id, "notebook": notebook_title,
            "section_id": uuid4().hex, "section": name.strip(), "section_path": [],
            "section_order": last_order + 1,
        })
        with QSignalBlocker(self.search):
            self.search.clear()
        self.refresh_navigation(page_id)

    def new_notebook(self):
        name, accepted = QInputDialog.getText(self, "New Notebook", "Notebook name")
        if not accepted or not name.strip():
            return
        self.save_page()
        page_id = self.library.add_page("Untitled", {
            "notebook_id": uuid4().hex, "notebook": name.strip(),
            "section_id": uuid4().hex, "section": "Notes", "section_path": [],
        })
        with QSignalBlocker(self.search):
            self.search.clear()
        self.refresh_navigation(page_id)

    def new_page(self):
        title, ok = QInputDialog.getText(self, "New page", "Page title")
        if not ok:
            return
        self.create_page(title.strip() or "Untitled")

    def add_template(self, title, html):
        self.create_page(title, objects=[
            {"type": "text", "html": html, "x": 48, "y": 40, "w": 540},
        ])

    def add_ledger_template(self):
        self.create_page("Financial Ledger", objects=[{
            "type": "ledger", "x": 48, "y": 40, "w": 620,
            "rows": [dict(date="", description="", debit="", credit="") for _ in range(4)],
        }])

    def create_page(self, title, *, objects=None):
        self.save_page()
        parent = self.library.page(self.page_id) if self.page_id else None
        page_id = self.library.add_page(title, parent, objects=objects)
        with QSignalBlocker(self.search):
            self.search.clear()
        self.refresh_navigation(page_id)

    def choose_import(self):
        result, _ = QFileDialog.getOpenFileName(
            self, "Import OneNote", "", "OneNote archives (*.onepkg *.one *.onetoc2);;All files (*)",
        )
        if result:
            self.start_import(Path(result))

    def start_import(self, path):
        if self.job:
            self.statusBar().showMessage("Wait for the current operation and try importing again.", 5000)
            return
        self.statusBar().showMessage(f"Reading {path.name}…")
        self.start_job(lambda: import_notebook(path, self.converter), self.finish_import)

    def finish_import(self, notebook):
        self.save_page()
        first = self.library.import_notebook(notebook)
        if not first:
            self.statusBar().showMessage("The imported notebook contained no readable pages.")
            return
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.refresh_navigation(first)
        count = sum(len(section["pages"]) for section in notebook["sections"])
        summary = f"Imported {count} pages from {notebook['title']}"
        warnings = notebook.get("warnings", [])
        lines = [summary, *warnings]
        self.library.set_import_report("\n\n".join(lines))
        self.import_report_action.setEnabled(True)
        self.statusBar().showMessage(
            summary + (" · See Insert → Last Import Report" if warnings else "")
        )

    def show_import_report(self):
        report = self.library.import_report()
        if report:
            QMessageBox.information(self, "OneNote import report", report)

    def sync_now(self):
        if self.job:
            return
        if not self.sync_url:
            QMessageBox.information(
                self, "Sync",
                "To connect to your sync server, set NOTES_SYNC_URL and NOTES_SYNC_TOKEN before starting the app.",
            )
            return
        self.save_page()
        pending, cursor = self.library.sync_request(self.sync_url)
        self.statusBar().showMessage("Syncing…")
        self.start_job(
            lambda: exchange(self.sync_url, self.sync_token, pending, cursor),
            lambda result: self.finish_sync(pending, result),
            report_error=False,
        )

    def finish_sync(self, pending, result):
        self.save_page()
        previous = self.library.page(self.page_id) if self.page_id else None
        copies = self.library.apply_sync(self.sync_url, pending, result)
        reload = bool(self.page_id and previous != self.library.page(self.page_id))
        if reload or copies or result["changes"] or result["conflicts"]:
            self.refresh_navigation(reload=reload)
        queued, _ = self.library.sync_request(self.sync_url)
        if copies:
            self.statusBar().showMessage(f"Kept {copies} local sync conflict copies.")
        elif queued:
            self.statusBar().showMessage("Saved on this device · changes waiting to sync")
        else:
            self.statusBar().showMessage("Synced")

    def start_job(self, operation, done, report_error=True):
        job = BackgroundJob(operation, self)
        self.job = job
        self.import_action.setEnabled(False)
        self.sync_action.setEnabled(False)

        def failed(message):
            self.statusBar().showMessage(message)
            if report_error:
                QMessageBox.warning(self, "OneNote import", message)

        def finished():
            self.job = None
            self.import_action.setEnabled(True)
            self.sync_action.setEnabled(True)
            job.deleteLater()

        job.succeeded.connect(done, Qt.ConnectionType.QueuedConnection)
        job.failed.connect(failed, Qt.ConnectionType.QueuedConnection)
        job.finished.connect(finished, Qt.ConnectionType.QueuedConnection)
        job.start()

    def closeEvent(self, event):
        if self.job:
            self.statusBar().showMessage("Please wait for the current operation to finish.")
            event.ignore()
            return
        self.save_page()
        self.library.db.close()
        super().closeEvent(event)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--import-notebook", type=Path)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--converter", help="Path to the one2html executable")
    parser.add_argument("--sync-url", default=os.getenv("NOTES_SYNC_URL", ""))
    args = parser.parse_args()
    token = os.getenv("NOTES_SYNC_TOKEN", "")
    if args.sync_url and not token:
        parser.error("Set NOTES_SYNC_TOKEN when using sync.")

    app = QApplication([sys.argv[0]])
    app.setApplicationName("Engineering Notebook")
    path = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation))
    library = Library(args.database or path / "notes.sqlite3")
    try:
        library.bind_remote(args.sync_url.rstrip("/"))
    except ValueError as error:
        parser.error(str(error))
    window = NotebookWindow(library, args.converter, args.sync_url, token)
    window.show()
    if args.import_notebook:
        QTimer.singleShot(0, lambda: window.start_import(args.import_notebook))
    elif args.sync_url:
        QTimer.singleShot(0, window.sync_now)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
