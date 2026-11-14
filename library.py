"""Local notebook storage and the durable queue for sync."""
from html.parser import HTMLParser
import json
import re
import sqlite3
from uuid import uuid4


class SearchableText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"head", "style", "script"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"head", "style", "script"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def search_text(payload):
    parser = SearchableText()
    for obj in payload["objects"]:
        if obj["type"] == "text":
            parser.feed(obj.get("html", ""))
        elif obj["type"] == "equation":
            parser.parts.append(obj.get("source", ""))
    return " ".join(parser.parts)


class Library:
    def __init__(self, path):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode = WAL;
            CREATE TABLE IF NOT EXISTS pages (
                id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                rev INTEGER NOT NULL DEFAULT 0,
                dirty INTEGER NOT NULL DEFAULT 1
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS page_search USING fts5(
                id UNINDEXED, title, body
            );
            CREATE TABLE IF NOT EXISTS settings (
                name TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)

    def _put(self, page_id, payload, rev=0, dirty=True):
        self.db.execute("""
            INSERT INTO pages (id, payload, rev, dirty) VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                payload=excluded.payload, rev=excluded.rev, dirty=excluded.dirty
        """, (page_id, json.dumps(payload, ensure_ascii=False), rev, dirty))
        self.db.execute("DELETE FROM page_search WHERE id=?", (page_id,))
        self.db.execute("INSERT INTO page_search VALUES (?, ?, ?)", (
            page_id, payload["title"], search_text(payload),
        ))

    def save(self, page_id, payload):
        row = self._get(page_id)
        if row and json.loads(row["payload"]) == payload:
            return
        with self.db:
            self._put(page_id, payload, row["rev"] if row else 0)

    def _get(self, page_id):
        return self.db.execute("SELECT * FROM pages WHERE id=?", (page_id,)).fetchone()

    def page(self, page_id):
        return json.loads(self._get(page_id)["payload"])

    def pages(self, query="", metadata_only=False):
        selection = "json_remove(pages.payload, '$.objects')" if metadata_only else "pages.payload"
        tokens = re.findall(r"\w+", query, flags=re.UNICODE)
        if tokens:
            expression = " AND ".join(f'"{token}"*' for token in tokens)
            rows = self.db.execute(f"""
                SELECT pages.id, {selection} AS payload
                FROM page_search JOIN pages ON pages.id=page_search.id
                WHERE page_search MATCH ? ORDER BY pages.rowid
            """, (expression,))
        elif query.strip():
            return []
        else:
            rows = self.db.execute(f"SELECT id, {selection} AS payload FROM pages ORDER BY rowid")
        return [(row["id"], json.loads(row["payload"])) for row in rows]

    def add_page(self, title, parent=None):
        if parent is None:
            parent = {
                "notebook_id": uuid4().hex, "notebook": "My Notebook",
                "section_id": uuid4().hex, "section": "Notes", "section_path": [],
            }
        payload = {**parent, "title": title, "objects": []}
        page_id = uuid4().hex
        self.save(page_id, payload)
        return page_id

    def import_notebook(self, notebook):
        first_id = None
        notebook_id = uuid4().hex
        with self.db:
            for section in notebook["sections"]:
                section_id = uuid4().hex
                for page in section["pages"]:
                    payload = {
                        "notebook_id": notebook_id, "notebook": notebook["title"],
                        "section_id": section_id, "section": section["title"],
                        "section_path": section["path"], "title": page["title"],
                        "objects": page["objects"],
                    }
                    page_id = uuid4().hex
                    self._put(page_id, payload)
                    first_id = first_id or page_id
        return first_id

    def import_report(self):
        row = self.db.execute("SELECT value FROM settings WHERE name='import_report'").fetchone()
        return row["value"] if row else ""

    def set_import_report(self, report):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES ('import_report', ?)", (report,))

    def bind_remote(self, remote):
        if not remote:
            return
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO settings VALUES ('sync_remote', ?)", (remote,))
            bound = self.db.execute("SELECT value FROM settings WHERE name='sync_remote'").fetchone()
            if bound["value"] != remote:
                raise ValueError(
                    "This library is paired with a different sync server URL. "
                    "Use the original URL or open a separate library with --database."
                )

    def sync_request(self, remote):
        row = self.db.execute(
            "SELECT value FROM settings WHERE name=?", ("cursor:" + remote,),
        ).fetchone()
        pending = [
            {"id": row["id"], "base_rev": row["rev"], "payload": json.loads(row["payload"])}
            for row in self.db.execute("SELECT * FROM pages WHERE dirty=1 ORDER BY rowid")
        ]
        return pending, int(row["value"]) if row else 0

    def apply_sync(self, remote, pending, result):
        sent = {page["id"]: page["payload"] for page in pending}
        accepted = set(result["accepted"])
        changes = {page["id"]: page for page in result["changes"]}
        conflicts = {page["id"]: page for page in result["conflicts"]}
        changes.update(conflicts)
        copied = 0
        with self.db:
            for page_id, change in changes.items():
                row = self._get(page_id)
                incoming = change["payload"]
                revision = change["rev"]
                if row and revision < row["rev"]:
                    continue
                local = json.loads(row["payload"]) if row else None
                if row and row["dirty"] and local != incoming:
                    if page_id in accepted and incoming == sent.get(page_id):
                        self._put(page_id, local, revision, dirty=True)
                        continue
                    if page_id in conflicts or revision > row["rev"]:
                        self._put(uuid4().hex, {
                            **local, "title": local["title"] + " (sync conflict)",
                        })
                        copied += 1
                    else:
                        continue
                self._put(page_id, incoming, revision, dirty=False)
            self.db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (
                "cursor:" + remote, str(result["cursor"]),
            ))
        return copied
