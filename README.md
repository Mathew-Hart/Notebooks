# Engineering Notebook

A native Qt desktop prototype for macOS and Linux. Pages support movable rich-text frames, pasted screenshots, equations, and pen strokes. Notes autosave to a local SQLite database and can be searched offline.

## Run locally

From the project folder, with Python 3.10 or later:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python app.py
```

Use **File → Import OneNote** to open a `.onepkg`, `.one`, or `.onetoc2` file. Import requires the [one2html converter](https://github.com/msiemens/one2html/releases), version 2.0.1 or later. Put it on your path, set `ONE2HTML`, or pass `--converter /path/to/one2html`. For Linux ARM64, build the converter from source with Rust:

```sh
cargo install --locked --no-default-features --features bin,onepkg one2html --version 2.0.1
```

You can also import during launch:

```sh
python app.py --import-notebook '/path/to/Example.onepkg'
```

The converter runs locally. The app reads its temporary output into native objects and stores those in the local database. Select a notebook on the left, a section tab across the top, and a page from the list on the right. Use **File → New Notebook** to create a notebook or **Add Page** to add a page to the current section. Drag section tabs or pages to reorder them. Right-click a tab or page to rename, delete, cut, copy, or paste. Clear the search to drag items. Double-click a text frame or equation to edit it. Use **Select** to move objects, **Pen** to draw, **Eraser** to remove ink, and **Paste Image** to insert a screenshot from the clipboard. A title or body term can find a page through the sidebar search. For an isolated test library, pass `--database /path/to/sandbox.sqlite3`.

On a touchscreen, drag a finger on the page to pan in any tool mode. A pen writes directly on the page (or erases when **Eraser** is selected). Flip a pen with a top eraser to erase without changing tools, then flip back to resume writing; this requires the device driver to report the eraser end to Qt. Touch movement is ignored during a pen stroke so a resting palm does not move the page. Hold the pen side button and trace a lasso to select page objects inside or crossing the loop. Release the button or lift the tip to finish; the next normal pen stroke writes again. Press Escape to cancel the lasso. Side buttons reported as right or middle mouse buttons are supported. Mouse input still follows the selected tool.

Use **File → Page Setup** to choose the paper size, orientation, and margins for preview, printing, and PDF export. These settings are saved with the local library. Use **File → Print Preview** to review the current note one sheet at a time. Preview requires Qt PDF or the Poppler utilities (`pdfinfo` and `pdftoppm`).

Use **File → Print** to send the current note to a system printer, or **File → Print to PDF** to save a PDF without a printer. Both include the page background and content outside the visible canvas. Long notes continue across multiple sheets.

Use **Insert → Camera** to preview a connected camera and capture a photo directly into the current note. For Debian-based systems using the distribution's PySide6, install `python3-pyside6.qtmultimedia` and `python3-pyside6.qtmultimediawidgets`. macOS app bundles need a camera usage description in [Info.plist](https://doc.qt.io/qtforpython-6/PySide6/QtCore/QCameraPermission.html).

## Optional sync

Choose one host for a library. To run the Python service in a container on a NAS, mount a folder **on the NAS volume** at `/data` and the project at `/app`, set the container working directory to `/app`, and publish port 8765 to the private LAN. The service uses the standard library and needs only `sync.py`. For a Linux server, point `--data-dir` at storage local to that server.

Set `NOTES_SYNC_TOKEN` to the same long random value on the host and both clients. Use the NAS or server's address for `NOTES_SYNC_URL`.

```sh
# On the host, after setting NOTES_SYNC_TOKEN
python3 sync.py --data-dir /data --bind 0.0.0.0 --port 8765

# On each desktop, after setting NOTES_SYNC_TOKEN
NOTES_SYNC_URL=http://nas.example.test:8765 python app.py
```

The prototype's HTTP mode is for a trusted private network. Use HTTPS through a reverse proxy if the network requires encrypted transport. Import the notebook on one client and let the other receive it through sync. The app syncs periodically and on **File → Sync**. Concurrent edits to the same page preserve the device's version as a separate conflict copy. Keep a separate local database for each device.

## Prototype scope

The importer reconstructs page objects from the converter's export. Compare complex layouts with OneNote before migrating a full library. Imported formatting and equation conversion can vary by source. Images and equations imported from inside a text frame can be moved independently, so editing the surrounding text does not automatically reposition them. Equation entry uses a subset of TeX supported by MathText. Importing the same notebook again creates a separate copy.
