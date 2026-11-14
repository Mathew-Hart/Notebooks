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

The converter runs locally. The app reads its temporary output into native objects and stores those in the local database. Select a notebook on the left, a section tab across the top, and a page from the list on the right. Use **File → New Page** to add a page to the current section. Double-click a text frame or equation to edit it. Use **Select** to move objects, **Pen** to draw, **Eraser** to remove ink, and **Paste Image** to insert a screenshot from the clipboard. A title or body term can find a page through the sidebar search. For an isolated test library, pass `--database /path/to/sandbox.sqlite3`.

## Optional sync

Choose one host for a library. The DS223 can run the Python service in an ARM64 Python container using Synology Container Manager. Mount a folder **on the NAS volume** at `/data` and the project at `/app`, set the container working directory to `/app`, and publish port 8765 to the private LAN. The service uses the standard library and needs only `sync.py`. For a Linux server, point `--data-dir` at storage local to that server.

Set `NOTES_SYNC_TOKEN` to the same long random value on the host and both clients. Use the NAS or server's address for `NOTES_SYNC_URL`.

```sh
# On the host, after setting NOTES_SYNC_TOKEN
python3 sync.py --data-dir /data --bind 0.0.0.0 --port 8765

# On each desktop, after setting NOTES_SYNC_TOKEN
NOTES_SYNC_URL=http://nas.example.test:8765 python app.py
```

The prototype's HTTP mode is for a trusted private network. Use HTTPS through a reverse proxy if the network requires encrypted transport. Import the notebook on one client and let the other receive it through sync. The app syncs periodically and on **File → Sync Now**. Concurrent edits to the same page preserve the device's version as a separate conflict copy. Keep a separate local database for each device.

## Prototype scope

The importer reconstructs page objects from the converter's export. Compare complex layouts with OneNote before migrating a full library. Imported formatting and equation conversion can vary by source. Images and equations imported from inside a text frame can be moved independently, so editing the surrounding text does not automatically reposition them. Equation entry uses a subset of TeX supported by MathText. Importing the same notebook again creates a separate copy.
