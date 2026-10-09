"""
Proves the default test suite cannot reach shared SQL, SharePoint, Microsoft
Graph or the network (see external_guard.py), and that the application's
real login/usage logging code still runs - just into an in-memory recorder.

Run:  python -m unittest test_external_isolation -v
"""
import unittest
import external_guard  # noqa: F401  (must precede app/db/sharepoint imports)

import io
import os
import re
import socket
from pathlib import Path

import requests
from werkzeug.datastructures import FileStorage

import app as app_module
import db
import sharepoint

_ROOT = Path(__file__).resolve().parent


class _GuardTestCase(unittest.TestCase):
    """Deliberate probes append to guard.state.blocked; remove them afterwards."""

    def setUp(self):
        self._blocked_before = len(external_guard.state.blocked)
        self._reads_before = len(external_guard.state.empty_db_reads)

    def tearDown(self):
        del external_guard.state.blocked[self._blocked_before:]
        del external_guard.state.empty_db_reads[self._reads_before:]


class DeterministicEnvironmentTests(_GuardTestCase):
    def test_environment_is_forced_and_credential_free(self):
        self.assertEqual(os.environ["SHAREPOINT_LOG_LIST_ID"], external_guard.TEST_LOG_LIST_ID)
        self.assertEqual(os.environ["DB_SERVER"], "blocked.invalid")
        self.assertEqual(os.environ["AZURE_CLIENT_SECRET"], "test-not-a-real-secret")
        self.assertEqual(os.environ["SHAREPOINT_SITE_HOSTNAME"], "blocked.invalid")

    def test_feature_flags_match_what_the_tests_were_written_against(self):
        # Forced (not skipped/disabled): tests keep exercising the SQL- and SharePoint-backed code paths.
        self.assertTrue(app_module.database_enabled())
        self.assertTrue(app_module.sharepoint_enabled())
        self.assertEqual(app_module.app.config["IDLE_TIMEOUT_MINUTES"], 5)

    def test_blocked_exception_cannot_be_swallowed_by_except_exception(self):
        self.assertTrue(issubclass(external_guard.ExternalAccessBlocked, BaseException))
        self.assertFalse(issubclass(external_guard.ExternalAccessBlocked, Exception))


class SqlIsolationTests(_GuardTestCase):
    def test_default_connection_is_an_empty_in_memory_database(self):
        conn = db.get_connection()
        cur = conn.cursor()
        cur.execute("SELECT TOP (1) Transaction_Key FROM etransactions.ETransaction")
        self.assertIsNone(cur.fetchone())
        self.assertEqual(cur.fetchall(), [])
        conn.close()

    def test_sql_writes_fail_loudly(self):
        cur = db.get_connection().cursor()
        for statement in (
            "INSERT INTO etransactions.ETransaction (Request_ID) VALUES (?)",
            "UPDATE etransactions.ETransaction SET Current_Status = ? WHERE Transaction_Key = ?",
            "DELETE FROM etransactions.ETransaction WHERE Transaction_Key = ?",
            "MERGE etransactions.AppUser AS t USING x ON 1=1 WHEN MATCHED THEN DELETE;",
            "ALTER TABLE etransactions.ETransaction ADD x int",
            "EXEC etransactions.some_proc",
        ):
            with self.subTest(statement=statement), self.assertRaises(external_guard.ExternalAccessBlocked):
                cur.execute(statement, [1])

    def test_real_sql_drivers_are_blocked(self):
        import pyodbc
        with self.assertRaises(external_guard.ExternalAccessBlocked):
            pyodbc.connect("DRIVER={ODBC Driver 18 for SQL Server};SERVER=x;DATABASE=y")
        from azure.identity import ClientSecretCredential
        with self.assertRaises(external_guard.ExternalAccessBlocked):
            ClientSecretCredential("t", "c", "s").get_token("https://database.windows.net/.default")


class SharePointIsolationTests(_GuardTestCase):
    def test_attachment_upload_fails_loudly_instead_of_writing(self):
        upload = FileStorage(stream=io.BytesIO(b"data"), filename="e.pdf")
        with self.assertRaises(external_guard.ExternalAccessBlocked):
            sharepoint.upload_attachment(
                "TXN-2026-0001", upload,
                section=sharepoint.SECTION_TRANSACTION, doc_type=sharepoint.DOC_TYPE_PAYMENT_SUPPORT,
                uploaded_by_role="Submitter",
            )

    def test_graph_writes_other_than_the_activity_log_fail_loudly(self):
        for verb in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(verb=verb), self.assertRaises(external_guard.ExternalAccessBlocked):
                sharepoint._graph_request(verb, "https://graph.microsoft.com/v1.0/sites/x/lists/real-list/items")

    def test_graph_reads_return_empty_collections(self):
        resp = sharepoint._graph_request("GET", "https://graph.microsoft.com/v1.0/anything")
        self.assertEqual(resp.json(), {"value": []})
        self.assertEqual(sharepoint.list_attachments("TXN-2026-0001"), [])
        self.assertEqual(sharepoint.get_properties(), [])

    def test_token_acquisition_is_blocked(self):
        with self.assertRaises(external_guard.ExternalAccessBlocked):
            sharepoint._get_access_token()


class NetworkSafetyNetTests(_GuardTestCase):
    def test_raw_http_is_blocked(self):
        for call in (lambda: requests.get("https://example.com"), lambda: requests.post("https://example.com", data={})):
            with self.assertRaises(external_guard.ExternalAccessBlocked):
                call()

    def test_raw_socket_connect_is_blocked(self):
        sock = socket.socket()
        try:
            with self.assertRaises(external_guard.ExternalAccessBlocked):
                sock.connect(("example.com", 443))
        finally:
            sock.close()


class UsageLoggingStillExercisedTests(_GuardTestCase):
    """The REAL login/logout logging code runs; only its SharePoint POST is captured in memory."""

    def setUp(self):
        super().setUp()
        self._rows_before = len(external_guard.ACTIVITY_LOG_ROWS)
        self.client = app_module.app.test_client()

    def tearDown(self):
        del external_guard.ACTIVITY_LOG_ROWS[self._rows_before:]
        super().tearDown()

    def _new_rows(self):
        return external_guard.ACTIVITY_LOG_ROWS[self._rows_before:]

    def test_first_request_in_a_session_records_one_login_without_touching_sharepoint(self):
        with self.client.session_transaction() as sess:
            sess["role"] = "submitter"
        self.client.get("/dashboard")
        rows = self._new_rows()
        self.assertEqual([r["ActivityType"] for r in rows], ["Login"])
        self.assertEqual(rows[0]["Application"], "E-Transaction Approval Dashboard")
        self.assertEqual(rows[0]["Env"], "Development")
        self.client.get("/dashboard")
        self.assertEqual(len(self._new_rows()), 1)  # later requests in the session do not re-log

    def test_logout_records_one_logout(self):
        with self.client.session_transaction() as sess:
            sess["role"] = "submitter"
        self.client.get("/logout")
        self.assertIn("Logout", [r["ActivityType"] for r in self._new_rows()])

    def test_real_log_activity_builds_the_row_and_never_reaches_the_network(self):
        sharepoint.log_activity(
            activity_type=sharepoint.LOG_ACTIVITY_LOGIN,
            user_email="test@example.com", user_name="Test User", user_role="Submitter",
        )
        row = self._new_rows()[0]
        self.assertEqual(row["UserEmail"], "test@example.com")
        self.assertEqual(row["ActivityType"], "Login")
        self.assertEqual(len(external_guard.state.blocked), self._blocked_before)


class IsolationCannotBeForgottenTests(unittest.TestCase):
    _FIRST_PARTY = re.compile(
        r"^\s*(?:import|from)\s+(?:app|db|sharepoint|workflow|authorization|banking_security|auth|export|mock_data|seed_data)\b"
    )
    _GUARD = re.compile(r"^\s*import\s+external_guard\b")

    def test_every_test_module_imports_the_guard_before_any_application_module(self):
        offenders = []
        for path in sorted(_ROOT.glob("test_*.py")):
            guard_line = first_party_line = None
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if guard_line is None and self._GUARD.match(line):
                    guard_line = number
                if first_party_line is None and self._FIRST_PARTY.match(line):
                    first_party_line = number
            if guard_line is None or (first_party_line is not None and guard_line > first_party_line):
                offenders.append(path.name)
        self.assertEqual(offenders, [], "These test modules must `import external_guard` before importing app/db/sharepoint")

    def test_application_modules_never_import_the_test_guard(self):
        for name in ("app.py", "db.py", "sharepoint.py", "auth.py", "workflow.py", "authorization.py", "banking_security.py"):
            self.assertNotIn("external_guard", (_ROOT / name).read_text(encoding="utf-8"), name)

    def test_live_integration_tests_cannot_be_collected_by_default_discovery(self):
        live_dir = _ROOT / "integration_live"
        if live_dir.exists():
            self.assertFalse((live_dir / "__init__.py").exists(),
                             "integration_live must not be a package, or default discovery would run live tests")


if __name__ == "__main__":
    unittest.main()
