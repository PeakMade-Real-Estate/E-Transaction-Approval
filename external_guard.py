"""
Test-process isolation from every external service.

Every test module imports this FIRST (before app/db/sharepoint) so that:

  * the environment is deterministic and credential-free, regardless of the
    developer's .env (app.py's load_dotenv() never overrides variables that are
    already set, so the values forced below always win);
  * db.get_connection() returns an in-memory EMPTY database: SELECTs return no
    rows, any write statement raises ExternalAccessBlocked;
  * sharepoint._graph_request() is a fake transport: GETs return an empty
    collection, the Innovation Use Log POST is RECORDED in ACTIVITY_LOG_ROWS
    (never sent), every other write raises ExternalAccessBlocked;
  * lower-level safety nets (pyodbc.connect, Entra token acquisition,
    requests.Session.request, raw socket connects) raise ExternalAccessBlocked.

ExternalAccessBlocked derives from BaseException on purpose: the application's
own best-effort `except Exception` handlers (login logging, attachment
look-ups, etc.) cannot swallow it, so an accidental external call fails the
test loudly instead of silently "working".

Tests that mock a boundary themselves (patch.object(db, "get_connection", ...),
patch.object(sharepoint, "_graph_request", ...), etc.) override these defaults
exactly as before.

LIVE INTEGRATION TESTS (none exist today) must NOT live next to the default
suite: put them under integration_live/ (a plain directory with no
__init__.py, so `unittest discover` never recurses into it), do NOT import this
module there, and run them only on purpose, e.g.
    ETXN_LIVE_INTEGRATION=1 python -m unittest discover -s integration_live

Nothing in the application imports this module; it has no effect on
development or production behavior.
"""
import os
import re
import socket
import unittest

TEST_LOG_LIST_ID = "TEST-LOG-LIST"

_FORCED_ENV = {
    "DB_ENABLED": "true",
    "SHAREPOINT_ENABLED": "true",
    "DEV_LOGIN_ENABLED": "true",
    "MOCK_DATA_ENABLED": "false",
    "IDLE_TIMEOUT_MINUTES": "5",
    "SECRET_KEY": "test-only-secret-key",
    "DB_SERVER": "blocked.invalid",
    "DB_NAME": "blocked",
    "AZURE_TENANT_ID": "00000000-0000-0000-0000-000000000000",
    "AZURE_CLIENT_ID": "00000000-0000-0000-0000-000000000000",
    "AZURE_CLIENT_SECRET": "test-not-a-real-secret",
    "SHAREPOINT_SITE_HOSTNAME": "blocked.invalid",
    "SHAREPOINT_SITE_PATH": "/sites/blocked",
    "SHAREPOINT_LIBRARY_ID": "TEST-LIBRARY",
    "SHAREPOINT_PROPERTY_LIST_ID": "TEST-PROPERTY-LIST",
    "SHAREPOINT_LOG_LIST_ID": TEST_LOG_LIST_ID,
}
for _key, _value in _FORCED_ENV.items():
    os.environ[_key] = _value


class ExternalAccessBlocked(BaseException):
    """Raised when a test reaches a live external service (SQL, Graph, network)."""


class _State:
    current_test = "(import time)"
    blocked = []          # (test id, description) of every blocked attempt
    empty_db_reads = []   # (test id, first 80 chars of SQL) answered by the empty fake DB


state = _State()
ACTIVITY_LOG_ROWS = []    # Innovation Use Log rows the app TRIED to write (never sent)


def _block(what):
    state.blocked.append((state.current_test, what))
    raise ExternalAccessBlocked(
        f"BLOCKED external access during tests: {what} [test: {state.current_test}]. "
        "The default suite must never touch live SQL/SharePoint/Graph - mock this boundary in the test."
    )


# Track the running test so a block names the offender.
if not getattr(unittest.TestCase.run, "_external_guard", False):
    _original_run = unittest.TestCase.run

    def _tracking_run(self, result=None):
        state.current_test = self.id()
        try:
            return _original_run(self, result)
        finally:
            state.current_test = "(between tests)"

    _tracking_run._external_guard = True
    unittest.TestCase.run = _tracking_run


# ── SQL ──────────────────────────────────────────────────────────────────────
_WRITE_SQL = re.compile(
    r"^\s*(INSERT|UPDATE|DELETE|MERGE|ALTER|CREATE|DROP|TRUNCATE|EXEC(UTE)?|GRANT|REVOKE)\b"
    r"|\b(INSERT\s+INTO|DELETE\s+FROM|MERGE\s+INTO)\b|\bUPDATE\s+\S+\s+SET\b",
    re.IGNORECASE,
)


class _EmptyCursor:
    description = []   # a real cursor exposes an (empty) column list after a SELECT
    rowcount = 0

    def execute(self, sql, *args, **kwargs):
        if _WRITE_SQL.search(sql or ""):
            _block("SQL write: " + (sql or "").strip()[:80].replace("\n", " "))
        state.empty_db_reads.append((state.current_test, (sql or "").strip()[:80].replace("\n", " ")))
        return self

    def executemany(self, sql, *args, **kwargs):
        _block("SQL executemany: " + (sql or "").strip()[:80].replace("\n", " "))

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def fetchmany(self, *args):
        return []

    def __iter__(self):
        return iter(())

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _EmptyConnection:
    autocommit = False

    def cursor(self, *args, **kwargs):
        return _EmptyCursor()

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


import db  # noqa: E402  (after the environment is forced)

db.get_connection = lambda *args, **kwargs: _EmptyConnection()

import pyodbc  # noqa: E402

pyodbc.connect = lambda *args, **kwargs: _block("pyodbc.connect (real SQL connection)")

from azure.identity import ClientSecretCredential  # noqa: E402

ClientSecretCredential.get_token = lambda self, *args, **kwargs: _block("Entra ID token acquisition")

# ── SharePoint / Microsoft Graph ─────────────────────────────────────────────
import sharepoint  # noqa: E402


class _FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = ""
        self.content = b""
        self.headers = {}

    def json(self):
        return self._body

    def raise_for_status(self):
        return None


def _fake_graph_request(method, url, **kwargs):
    verb = str(method).upper()
    if verb == "POST" and str(url).endswith(f"/lists/{TEST_LOG_LIST_ID}/items"):
        ACTIVITY_LOG_ROWS.append(dict((kwargs.get("json") or {}).get("fields", {})))
        return _FakeResponse(201, {"id": "test-activity-row"})
    if verb == "GET":
        return _FakeResponse(200, {"value": []})
    return _block(f"Microsoft Graph {verb} {str(url)[:80]}")


sharepoint._graph_request = _fake_graph_request
sharepoint._get_access_token = lambda *args, **kwargs: _block("Microsoft Graph token acquisition")
sharepoint._site_id_cache = "TEST-SITE"
sharepoint._drive_id_cache = "TEST-DRIVE"
sharepoint._properties_cache = []

# ── Network safety nets (anything reaching these bypassed every boundary above) ──
import requests  # noqa: E402

requests.sessions.Session.request = (
    lambda self, method, url, *args, **kwargs: _block(f"HTTP {str(method).upper()} {str(url)[:80]}")
)


def _blocked_socket_connect(self, *args, **kwargs):
    return _block(f"raw socket connect {str(args)[:80]}")


socket.socket.connect = _blocked_socket_connect
socket.socket.connect_ex = _blocked_socket_connect
