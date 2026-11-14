"""Native desktop entry point for the engineering notebook prototype."""
import argparse
import os
from pathlib import Path
import sys

from PySide6.QtCore import QSignalBlocker, QStandardPaths, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QInputDialog, QLabel, QLineEdit, QMainWindow,
    QListWidget, QListWidgetItem, QMessageBox, QSplitter, QTabBar, QToolBar,
    QVBoxLayout, QWidget,
)

from canvas import PageCanvas
from importer import import_notebook
from library import Library
from sync import exchange


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
        self.sections.currentChanged.connect(self.select_section)
        editor_layout.addWidget(self.sections)
        self.title = QLineEdit()
        self.title.setPlaceholderText("Page title")
        self.title.setStyleSheet("QLineEdit {padding: 11px 16px; font-size: 20px; font-weight: 600;}")
        self.title.textEdited.connect(self.queue_save)
        editor_layout.addWidget(self.title)
        self.canvas = PageCanvas()
        self.canvas.changed.connect(self.queue_save)
        editor_layout.addWidget(self.canvas, 1)
        splitter.addWidget(editor)
        pages = QWidget()
        pages.setMinimumWidth(150)
        pages_layout = QVBoxLayout(pages)
        pages_layout.setContentsMargins(12, 16, 14, 12)
        heading = QLabel("Pages")
        heading.setStyleSheet("font-size: 17px; font-weight: 600;")
        pages_layout.addWidget(heading)
        self.page_list = QListWidget()
        self.page_list.setAccessibleName("Pages")
        self.page_list.setUniformItemSizes(True)
        self.page_list.currentItemChanged.connect(self.select_page)
        pages_layout.addWidget(self.page_list)
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
        toolbar = QToolBar("Page editing", self)
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        def action(text, callback, shortcut=None):
            item = QAction(text, self)
            if shortcut:
                item.setShortcut(shortcut)
            item.triggered.connect(callback)
            return item

        self.import_action = action("&Import OneNote…", self.choose_import)
        file_menu.addAction(self.import_action)
        self.import_report_action = action("Last Import Report…", self.show_import_report)
        self.import_report_action.setEnabled(bool(self.library.import_report()))
        file_menu.addAction(self.import_report_action)
        self.new_action = action("&New Page", self.new_page, QKeySequence.StandardKey.New)
        file_menu.addAction(self.new_action)
        toolbar.addAction(self.new_action)
        file_menu.addAction(action("&Find", self.search.setFocus, QKeySequence.StandardKey.Find))
        self.sync_action = action("&Sync Now", self.sync_now)
        self.sync_action.setEnabled(bool(self.sync_url))
        file_menu.addAction(self.sync_action)

        toolbar.addSeparator()
        self.tool_group = QActionGroup(self)
        self.editor_actions = []
        tools = {}
        for label, tool in [("Select", "select"), ("Pen", "ink"), ("Eraser", "erase")]:
            item = action(label, lambda checked=False, value=tool: self.canvas.set_tool(value))
            item.setCheckable(True)
            self.tool_group.addAction(item)
            toolbar.addAction(item)
            self.editor_actions.append(item)
            tools[tool] = item
            if tool == "select":
                self.select_tool = item
                item.setChecked(True)
        def mark_tool(tool):
            if tool in tools:
                tools[tool].setChecked(True)

        self.canvas.tool_changed.connect(mark_tool)

        toolbar.addSeparator()
        for label, callback in [
            ("Add Text", self.add_text), ("Equation", self.add_equation),
            ("Paste Image", self.paste_image), ("Remove", self.canvas.delete_selected),
        ]:
            item = action(label, callback)
            toolbar.addAction(item)
            self.editor_actions.append(item)

        toolbar.addSeparator()
        for label, style in [("Bold", "bold"), ("Italic", "italic"), ("Underline", "underline")]:
            item = action(label, lambda checked=False, value=style: self.canvas.set_format(value))
            toolbar.addAction(item)
            self.editor_actions.append(item)
        toolbar.addSeparator()
        for label, callback in [
            ("Zoom In", lambda: self.canvas.zoom_by(1.2)),
            ("Zoom Out", lambda: self.canvas.zoom_by(1 / 1.2)),
            ("Actual Size", lambda: self.canvas.set_zoom(1.0)),
        ]:
            toolbar.addAction(action(label, callback))

    def add_text(self):
        self.select_tool.setChecked(True)
        self.canvas.set_tool("select")
        self.canvas.add_text()

    def add_equation(self):
        self.select_tool.setChecked(True)
        self.canvas.set_tool("select")
        self.canvas.add_equation()

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
    def populate_list(widget, entries, selected):
        row = 0
        with QSignalBlocker(widget):
            widget.clear()
            for index, (entry_id, title) in enumerate(entries):
                item = QListWidgetItem(title, widget)
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
        # Keep the library order returned by SQLite.
        self.navigation_rows = rows
        notebooks = {page["notebook_id"]: page["notebook"] for _, page in rows}
        notebook_id = self.populate_list(self.notebooks, notebooks.items(), notebook_id)
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
                index = self.sections.addTab(title)
                self.sections.setTabData(index, section_id)
                self.sections.setTabToolTip(index, title)
                if section_id == selected:
                    tab = index
            self.sections.setCurrentIndex(tab if sections else -1)
        self.sections.setVisible(bool(sections))
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
        self.save_page()
        self.show_pages(self.sections.tabData(index))

    def select_page(self, item, previous):
        if item is not None:
            self.save_page()
            self.load_page(item.data(Qt.ItemDataRole.UserRole))

    def load_page(self, page_id):
        self.loading = True
        self.page_id = page_id
        try:
            page = self.library.page(page_id) if page_id else {"title": "", "objects": []}
            self.title.setText(page["title"])
            self.canvas.set_page(page["objects"])
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
            payload["title"] = self.title.text().strip() or "Untitled"
            payload["objects"] = self.canvas.objects()
            self.library.save(self.page_id, payload)
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

    def new_page(self):
        title, ok = QInputDialog.getText(self, "New page", "Page title")
        if not ok:
            return
        self.save_page()
        parent = self.library.page(self.page_id) if self.page_id else None
        page_id = self.library.add_page(title.strip() or "Untitled", parent)
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
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
            summary + (" · See File → Last Import Report" if warnings else "")
        )

    def show_import_report(self):
        report = self.library.import_report()
        if report:
            QMessageBox.information(self, "OneNote import report", report)

    def sync_now(self):
        if self.job or not self.sync_url:
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
            self.sync_action.setEnabled(bool(self.sync_url))
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
