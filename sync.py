"""Page revision exchange for the desktop app and its private sync server."""

import argparse
from contextlib import closing
import hmac
from http import HTTPStatus
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


MAX_REQUEST_BYTES = 64 * 1024 * 1024
MAX_REVISION = 2**63 - 1
SYNC_PATH = "/v1/sync"
TOKEN_VARIABLE = "NOTES_SYNC_TOKEN"


class SyncError(Exception):
    """An actionable error from the sync client."""


class _StaleCursor(Exception):
    """The client is ahead of this server's database."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _valid_token(token):
    return (
        isinstance(token, str)
        and bool(token)
        and token.isascii()
        and token.isprintable()
        and " " not in token
    )


def _valid_revision(value):
    return type(value) is int and 0 <= value <= MAX_REVISION


def _valid_id(value):
    return isinstance(value, str) and 0 < len(value) <= 256 and value.isascii()


def _encode(value):
    return json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _upload_batches(pending):
    # Reserve the longest possible cursor and the JSON punctuation around the page list.
    envelope_size = len(_encode({"pending": [], "cursor": MAX_REVISION}))
    batches = [[]]
    size = envelope_size
    for page in pending:
        encoded = _encode(page).encode("utf-8")
        if len(encoded) + envelope_size > MAX_REQUEST_BYTES:
            title = page["payload"].get("title", "Untitled")
            raise SyncError(f"The page {title!r} exceeds the sync service's 64 MiB request limit.")
        if size + len(encoded) + bool(batches[-1]) > MAX_REQUEST_BYTES:
            batches.append([])
            size = envelope_size
        size += len(encoded) + bool(batches[-1])
        batches[-1].append(encoded)
    return batches


def exchange(url, token, pending, cursor):
    """Upload pending page snapshots and read the server's current revisions."""
    try:
        server = urlsplit(url)
    except ValueError as error:
        raise SyncError("Enter a valid sync server URL.") from error
    if (
        server.scheme not in {"http", "https"}
        or not server.hostname
        or server.username is not None
        or server.query
        or server.fragment
    ):
        raise SyncError("Enter an HTTP or HTTPS sync server URL.")
    if not _valid_token(token):
        raise SyncError("Enter the sync token configured on the server.")

    # Connect directly and keep the bearer token on the configured origin.
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    changes = {}
    conflicts = {}
    accepted = []
    for batch in _upload_batches(pending):
        body = b'{"cursor":' + str(cursor).encode("ascii") + b',"pending":[' + b",".join(batch) + b"]}"
        result = _exchange_request(opener, url.rstrip("/") + SYNC_PATH, token, body)
        cursor = result["cursor"]
        changes.update((page["id"], page) for page in result["changes"])
        conflicts.update((page["id"], page) for page in result["conflicts"])
        accepted.extend(result["accepted"])
    return {
        "cursor": cursor,
        "accepted": accepted,
        "changes": list(changes.values()),
        "conflicts": [changes.get(page_id, page) for page_id, page in conflicts.items()],
    }


def _exchange_request(opener, url, token, body):
    request = Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with opener.open(request, timeout=30) as response:
            result = json.load(response)
    except HTTPError as error:
        with error:
            try:
                detail = json.loads(error.read(4096)).get("error")
            except (ValueError, AttributeError):
                detail = None
        raise SyncError(
            detail if isinstance(detail, str) else f"Sync server returned HTTP {error.code}."
        ) from error
    except (URLError, HTTPException, OSError, ValueError, RecursionError) as error:
        raise SyncError(f"Could not sync with the server. {error}") from error

    if not isinstance(result, dict) or not _valid_revision(result.get("cursor")):
        raise SyncError("The sync server returned an invalid revision cursor.")
    for field in ("changes", "conflicts"):
        revisions = result.get(field)
        if not isinstance(revisions, list) or any(
            not isinstance(revision, dict)
            or not _valid_id(revision.get("id"))
            or not _valid_revision(revision.get("rev"))
            or not isinstance(revision.get("payload"), dict)
            for revision in revisions
        ):
            raise SyncError(f"The sync server returned invalid {field}.")
    accepted = result.get("accepted")
    if not isinstance(accepted, list) or any(not _valid_id(page_id) for page_id in accepted):
        raise SyncError("The sync server returned invalid upload acknowledgments.")
    return result


def _validate_request(body):
    if not isinstance(body, dict) or not _valid_revision(body.get("cursor")):
        raise ValueError("A valid revision cursor is required.")
    pending = body.get("pending")
    if not isinstance(pending, list):
        raise ValueError("Pending changes must be a list.")
    seen = set()
    for page in pending:
        if (
            not isinstance(page, dict)
            or not _valid_id(page.get("id"))
            or not _valid_revision(page.get("base_rev"))
            or not isinstance(page.get("payload"), dict)
        ):
            raise ValueError("Each pending change needs a page ID, base revision and payload.")
        if page["id"] in seen:
            raise ValueError("A page can occur only once in an exchange.")
        seen.add(page["id"])
        # Validate and serialize before holding the database write lock.
        page["payload"] = _encode(page["payload"])
    return pending, body["cursor"]


def _revision(row):
    page_id, rev, payload = row
    return {"id": page_id, "rev": rev, "payload": json.loads(payload)}


class _SyncStore:
    def __init__(self, directory):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = directory / "sync.sqlite3"
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS pages (
                    id TEXT PRIMARY KEY,
                    rev INTEGER NOT NULL UNIQUE,
                    payload TEXT NOT NULL
                )
                """
            )

    def exchange(self, pending, cursor):
        accepted = []
        conflicts = []
        changes = {}
        with closing(sqlite3.connect(self.path, timeout=15)) as connection, connection:
            # Serialize compare-and-update and take the outgoing snapshot in the same transaction.
            connection.execute("BEGIN IMMEDIATE")
            latest = connection.execute("SELECT COALESCE(MAX(rev), 0) FROM pages").fetchone()[0]
            if cursor > latest:
                raise _StaleCursor

            for page in pending:
                page_id = page["id"]
                row = connection.execute(
                    "SELECT id, rev, payload FROM pages WHERE id = ?", (page_id,)
                ).fetchone()
                current_rev = row[1] if row else 0

                if row and row[2] == page["payload"]:
                    # The current content already matches, including after a dropped response.
                    accepted.append(page_id)
                    if current_rev <= cursor:
                        changes[page_id] = row
                    continue
                if page["base_rev"] != current_rev:
                    if row is None:
                        raise _StaleCursor
                    conflicts.append(row)
                    continue

                latest += 1
                connection.execute(
                    """
                    INSERT INTO pages (id, rev, payload) VALUES (?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET rev = excluded.rev, payload = excluded.payload
                    """,
                    (page_id, latest, page["payload"]),
                )
                accepted.append(page_id)

            active = connection.execute(
                "SELECT id, rev, payload FROM pages WHERE rev > ? ORDER BY rev", (cursor,)
            )
            changes.update((row[0], row) for row in active)

        return {
            "accepted": accepted,
            "changes": [_revision(row) for row in changes.values()],
            "conflicts": [_revision(row) for row in conflicts],
            "cursor": latest,
        }


class _Handler(BaseHTTPRequestHandler):
    timeout = 30

    def do_POST(self):
        if self.path != SYNC_PATH:
            self._send(HTTPStatus.NOT_FOUND, {"error": "Unknown sync endpoint."})
            return
        supplied = self.headers.get("Authorization", "").encode("utf-8")
        if not hmac.compare_digest(supplied, self.server.authorization):
            self._send(HTTPStatus.UNAUTHORIZED, {"error": "The server rejected the sync token."})
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            length = 0
        if length <= 0:
            self._send(HTTPStatus.BAD_REQUEST, {"error": "A JSON request body is required."})
            return
        if length > MAX_REQUEST_BYTES:
            self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "Sync request is too large."})
            return
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError
            request = json.loads(raw)
            pending, cursor = _validate_request(request)
        except (ValueError, RecursionError):
            self._send(HTTPStatus.BAD_REQUEST, {"error": "Invalid sync request."})
            return
        except OSError:
            return

        try:
            result = self.server.store.exchange(pending, cursor)
        except _StaleCursor:
            self._send(
                HTTPStatus.CONFLICT,
                {"error": "This device has revisions that are missing from the sync server."},
            )
            return
        except sqlite3.Error:
            self.log_error("Sync database transaction failed.")
            self._send(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Sync storage is unavailable."})
            return
        self._send(HTTPStatus.OK, result)

    def _send(self, status, payload):
        body = _encode(payload).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            # The desktop will retry if the connection drops before it receives the response.
            pass


def main():
    parser = argparse.ArgumentParser(
        description=f"Serve the private desktop sync endpoint. Set {TOKEN_VARIABLE} before launch."
    )
    parser.add_argument("--data-dir", type=Path, required=True, help="A directory local to this host")
    parser.add_argument("--bind", default="127.0.0.1", help="Interface to listen on")
    parser.add_argument("--port", type=int, default=8765, help="Port to listen on")
    args = parser.parse_args()
    token = os.environ.get(TOKEN_VARIABLE)
    if not _valid_token(token):
        parser.error(f"{TOKEN_VARIABLE} must be a nonempty ASCII token without whitespace.")
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535.")

    store = _SyncStore(args.data_dir.expanduser())
    with ThreadingHTTPServer((args.bind, args.port), _Handler) as server:
        server.store = store
        server.authorization = f"Bearer {token}".encode("ascii")
        print(f"Sync service listening on {args.bind}:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
