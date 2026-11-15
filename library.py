"""Local notebook storage and the durable queue for sync."""
from collections import defaultdict
from datetime import datetime, timezone
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
        elif obj["type"] == "ledger":
            for row in obj.get("rows", []):
                parser.parts.extend(row.get(field, "") for field in ("date", "description", "debit", "credit"))
    return " ".join(parser.parts)


def _reordered(current, desired):
    """Replace the slots of the supplied siblings while leaving other slots in place."""
    valid = set(current)
    desired = dict.fromkeys(item for item in desired if item in valid)
    replacements = iter(desired)
    return [next(replacements) if item in desired else item for item in current]


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
        if not payload.get("deleted"):
            self.db.execute("INSERT INTO page_search VALUES (?, ?, ?)", (
                page_id, payload["title"], search_text(payload),
            ))

    def save(self, page_id, payload):
        return self.save_pages([(page_id, payload)])[page_id]

    def save_pages(self, records):
        modified_times = {}
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            for page_id, payload in records:
                row = self._get(page_id)
                previous = json.loads(row["payload"]) if row else None
                payload = dict(payload)
                # Navigation changes and sync downloads don't edit the page itself.
                if previous is None or self._content_changed(previous, payload):
                    payload["modified_at"] = datetime.now(timezone.utc).isoformat()
                elif "modified_at" in previous:
                    payload["modified_at"] = previous["modified_at"]
                else:
                    payload.pop("modified_at", None)
                if payload != previous:
                    self._put(page_id, payload, row["rev"] if row else 0)
                modified_times[page_id] = payload.get("modified_at")
        return modified_times

    @staticmethod
    def _content_changed(previous, payload):
        return any(
            previous.get(key, default) != payload.get(key, default)
            for key, default in (("title", ""), ("objects", []), ("paper_style", "white"))
        )

    def _get(self, page_id):
        return self.db.execute("SELECT * FROM pages WHERE id=?", (page_id,)).fetchone()

    def page(self, page_id):
        return json.loads(self._get(page_id)["payload"])

    def _ordered_metadata(self):
        rows = self.db.execute("""
            SELECT id, json_remove(payload, '$.objects') AS metadata
            FROM pages
            WHERE NOT COALESCE(json_extract(payload, '$.deleted'), 0)
            ORDER BY rowid
        """)
        notebooks = defaultdict(dict)
        for row in rows:
            page = json.loads(row["metadata"])
            sections = notebooks[page["notebook_id"]]
            sections.setdefault(page["section_id"], []).append((row["id"], page))
        for sections in notebooks.values():
            section_positions = {}
            for index, (section_id, records) in enumerate(sections.items()):
                explicit = [
                    page["section_order"] for _, page in records
                    if type(page.get("section_order")) is int
                ]
                section_positions[section_id] = min(explicit, default=index)
            for section_id in sorted(sections, key=lambda key: (section_positions[key], key)):
                records = sections[section_id]
                for index, (_, page) in enumerate(records):
                    page["section_order"] = section_positions[section_id]
                    if type(page.get("page_order")) is not int:
                        page["page_order"] = index
                yield from sorted(records, key=lambda record: (record[1]["page_order"], record[0]))

    def pages(self, query="", metadata_only=False):
        results = None
        tokens = re.findall(r"\w+", query, flags=re.UNICODE)
        if tokens:
            expression = " AND ".join(f'"{token}"*' for token in tokens)
            results = {
                row["id"] for row in self.db.execute(
                    "SELECT id FROM page_search WHERE page_search MATCH ?", (expression,),
                )
            }
        elif query.strip():
            return []
        return [
            (page_id, metadata if metadata_only else self.page(page_id))
            for page_id, metadata in self._ordered_metadata()
            if results is None or page_id in results
        ]

    def reorder_sections(self, notebook_id, ids):
        rows = [
            (page_id, page) for page_id, page in self._ordered_metadata()
            if page["notebook_id"] == notebook_id
        ]
        current = list(dict.fromkeys(page["section_id"] for _, page in rows))
        desired = _reordered(current, ids)
        if desired == current:
            return
        positions = {section_id: rank for rank, section_id in enumerate(desired)}
        self._update_pages(
            (page_id, {"section_order": positions[page["section_id"]]})
            for page_id, page in rows
        )

    def reorder_pages(self, section_id, ids):
        current = [
            page_id for page_id, page in self._ordered_metadata()
            if page["section_id"] == section_id
        ]
        desired = _reordered(current, ids)
        if desired != current:
            self._update_pages(
                (page_id, {"page_order": rank}) for rank, page_id in enumerate(desired)
            )

    def rename_section(self, section_id, title):
        self._update_pages(
            (page_id, {"section": title})
            for page_id, page in self._ordered_metadata() if page["section_id"] == section_id
        )

    def delete_pages(self, ids):
        ids = set(ids)
        self._update_pages(
            (page_id, {"deleted": True})
            for page_id, _ in self._ordered_metadata() if page_id in ids
        )

    def _update_pages(self, changes):
        self.save_pages(
            (page_id, {**self.page(page_id), **updates}) for page_id, updates in changes
        )

    def add_page(self, title, parent=None, *, objects=None):
        if parent is None:
            parent = {
                "notebook_id": uuid4().hex, "notebook": "My Notebook",
                "section_id": uuid4().hex, "section": "Notes", "section_path": [],
            }
        siblings = [
            page for _, page in self._ordered_metadata()
            if page["section_id"] == parent["section_id"]
        ]
        payload = {
            key: parent[key] for key in ("notebook_id", "notebook", "section_id", "section", "section_path")
        }
        section_order = siblings[0]["section_order"] if siblings else parent.get("section_order")
        payload.update(
            title=title, objects=[] if objects is None else objects,
            section_order=section_order if type(section_order) is int else 0,
            page_order=max((page["page_order"] for page in siblings), default=-1) + 1,
        )
        page_id = uuid4().hex
        self.save(page_id, payload)
        return page_id

    def import_notebook(self, notebook):
        first_id = None
        notebook_id = uuid4().hex
        with self.db:
            for section_order, section in enumerate(notebook["sections"]):
                section_id = uuid4().hex
                for page_order, page in enumerate(section["pages"]):
                    payload = {
                        "notebook_id": notebook_id, "notebook": notebook["title"],
                        "section_id": section_id, "section": section["title"],
                        "section_path": section["path"], "section_order": section_order,
                        "title": page["title"], "page_order": page_order,
                        "objects": page["objects"],
                    }
                    if modified_at := page.get("modified_at"):
                        payload["modified_at"] = modified_at
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

    def page_setup(self):
        row = self.db.execute("SELECT value FROM settings WHERE name='page_setup'").fetchone()
        try:
            return json.loads(row["value"]) if row else None
        except json.JSONDecodeError:
            return None

    def save_page_setup(self, setup):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES ('page_setup', ?)", (json.dumps(setup),))

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
                # Identical edits can have different timestamps on different devices.
                if row and row["dirty"] and (
                    {**local, "modified_at": None} != {**incoming, "modified_at": None}
                ):
                    if page_id in accepted and incoming == sent.get(page_id):
                        self._put(page_id, local, revision, dirty=True)
                        continue
                    if page_id not in conflicts and revision <= row["rev"]:
                        continue
                    if local.get("deleted"):
                        if not incoming.get("deleted"):
                            self._copy_conflict(incoming)
                            copied += 1
                            self._put(page_id, local, revision, dirty=True)
                            continue
                    else:
                        self._copy_conflict(local)
                        copied += 1
                self._put(page_id, incoming, revision, dirty=False)
            self.db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (
                "cursor:" + remote, str(result["cursor"]),
            ))
        return copied

    def _copy_conflict(self, payload):
        self._put(uuid4().hex, {**payload, "title": payload["title"] + " (sync conflict)"})
