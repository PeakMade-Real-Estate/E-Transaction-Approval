"""
Refinement tests for Item 6 (Corporate bulk recipients): dashboard / CSV identification, feature-gate
read vs. create behavior, unmigrated-schema compatibility, spreadsheet validation details, and
file / validation integrity.

Mock-only: `external_guard` (imported first) makes SQL / SharePoint / Graph inert.

Run:  python -m unittest test_bulk_visibility -v
"""
import csv
import hashlib
import io
import logging
import os
import re
import unittest
import zipfile
import external_guard  # noqa: F401  (must precede app/db/sharepoint imports)
from unittest.mock import MagicMock, patch

import app as app_module
import bulk_listing
import db
import export
import sharepoint
import test_bulk_recipients as tb
import test_internal_recipient as ir
import workflow as wf

ACCT, RTN = tb.ACCT, tb.RTN


def _no_numbers(testcase, text):
    tb._no_numbers(testcase, text)


# ── helpers ──────────────────────────────────────────────────────────────────

def make_xlsx_shared(rows, headers=("Account Number", "Routing Number")):
    """A workbook the way Excel writes text: sharedStrings.xml plus t="s" cells."""
    strings, index = [], {}

    def sid(text):
        if text not in index:
            index[text] = len(strings)
            strings.append(text)
        return index[text]

    body = []
    for r, values in enumerate([headers] + list(rows), start=1):
        cells = "".join(f'<c r="{col}{r}" t="s"><v>{sid(v)}</v></c>' for col, v in zip("AB", values) if v is not None)
        body.append(f'<row r="{r}">{cells}</row>')
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    sheet = f'<?xml version="1.0"?><worksheet xmlns="{ns}"><sheetData>{"".join(body)}</sheetData></worksheet>'
    sst = (f'<?xml version="1.0"?><sst xmlns="{ns}">'
           + "".join(f"<si><t>{s}</t></si>" for s in strings) + "</sst>")
    workbook = (f'<?xml version="1.0"?><workbook xmlns="{ns}" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets><sheet name="S" sheetId="1" r:id="rId1"/></sheets></workbook>')
    rels = ('<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="x" Target="worksheets/sheet1.xml"/></Relationships>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("xl/workbook.xml", workbook)
        zf.writestr("xl/_rels/workbook.xml.rels", rels)
        zf.writestr("xl/worksheets/sheet1.xml", sheet)
        zf.writestr("xl/sharedStrings.xml", sst)
    return buf.getvalue()


class _Recorder:
    """A matcher that records exactly what it was asked to look up."""

    def __init__(self, book=None, status="Open"):
        self.calls, self.inner = [], tb.matcher(book, status)

    def __call__(self, pairs):
        self.calls.append(list(pairs))
        return self.inner(pairs)


# ── 1. Dashboard / CSV identification ────────────────────────────────────────

class _DashCursor:
    def __init__(self, columns, rows):
        self.description = [(c,) for c in columns]
        self._rows, self.sql = rows, None

    def execute(self, sql, params=None):
        self.sql = sql

    def fetchall(self):
        return self._rows


BASE_COLS = ["request_id", "request_type", "urgent", "amount", "status", "property_dept", "days_pending"]


class DashboardQueryCompatibilityTests(unittest.TestCase):
    def _run(self, ready, mode_value="Bulk"):
        cols = BASE_COLS + (["recipient_mode"] if ready else [])
        row = ("TXN-1", "ACH", 1, 100, "Pending Approver", "P", 2) + ((mode_value,) if ready else ())
        cur = _DashCursor(cols, [row])
        conn = MagicMock()
        conn.cursor.return_value = cur
        with patch.object(db, "bulk_schema_ready", return_value=ready), \
             patch.object(db, "get_connection", return_value=conn):
            records = db.get_dashboard_records(role="business_admin", user_key=1)
        return cur.sql, records

    def test_recipient_mode_is_read_only_when_the_schema_is_present(self):
        sql, records = self._run(True)
        self.assertIn("t.Recipient_Mode", sql)
        self.assertEqual(records[0]["recipient_mode"], "bulk")

    def test_an_unmigrated_database_is_never_asked_for_the_new_column(self):
        sql, records = self._run(False)
        self.assertNotIn("Recipient_Mode", sql)
        self.assertEqual(records[0]["recipient_mode"], "")
        self.assertIs(db._dashboard_sql(False), db._DASHBOARD_SQL_BASE)

    def test_historical_null_mode_is_blank(self):
        _, records = self._run(True, mode_value=None)
        self.assertEqual(records[0]["recipient_mode"], "")

    def test_the_read_does_not_depend_on_the_create_switch(self):
        with patch.dict(os.environ, {"BULK_RECIPIENT_ENABLED": "false"}):
            sql, records = self._run(True)
        self.assertIn("t.Recipient_Mode", sql)
        self.assertEqual(records[0]["recipient_mode"], "bulk")

    def test_scope_filtering_is_unchanged_by_the_new_column(self):
        # The authorization WHERE clause is built independently of the select list.
        where, params = db._dashboard_scope_where({"submitter"}, 11, None)
        self.assertEqual((where, params), (["t.PreparedBy_User_Key = ?"], [11]))
        self.assertTrue(db._dashboard_sql(True).count("FROM [etransactions].[ETransaction] t") == 1)


def _dash_records():
    base = {"status": wf.STATUS_PENDING_APPROVER, "property_dept": "Corporate", "assigned_approver": "Alice",
            "amount": 500.0, "submitted_date": "2026-01-01", "days_pending": 1, "treasury_service_date": "2026-10-01"}
    return [
        dict(base, request_id="TXN-BULK-SDA", request_type="Same Day ACH", urgent=True, urgency_reason="Same Day ACH",
             recipient_mode="bulk"),
        dict(base, request_id="TXN-BULK-WIRE", request_type="Wire", urgent=False, urgency_reason="", recipient_mode="bulk"),
        dict(base, request_id="TXN-PLAIN", request_type="ACH", urgent=False, urgency_reason="", recipient_mode=""),
    ]


class DashboardPageTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as sess:
            sess["role"] = "sam"

    def _get(self, query=""):
        with patch.object(app_module, "current_app_user_key", return_value=200), \
             patch.object(app_module.db, "get_dashboard_records", return_value=_dash_records()) as mock_get:
            resp = self.client.get(f"/dashboard{query}")
        return resp.get_data(as_text=True), mock_get

    def test_bulk_transactions_are_identified_and_keep_their_type_and_urgency(self):
        body, _ = self._get()
        self.assertEqual(body.count("bulk-recipients-badge"), 2)
        self.assertIn("Same Day ACH", body)
        self.assertIn("row-urgent", body)
        self.assertIn("Urgent", body)

    def test_a_single_recipient_row_carries_no_bulk_badge(self):
        body, _ = self._get("?request_type=ACH")
        self.assertIn("TXN-PLAIN", body)
        self.assertNotIn("bulk-recipients-badge", body)

    def test_filters_still_narrow_bulk_rows(self):
        body, _ = self._get("?request_type=Wire")
        self.assertIn("TXN-BULK-WIRE", body)
        self.assertNotIn("TXN-BULK-SDA", body)
        body, _ = self._get("?urgent_only=1")
        self.assertIn("TXN-BULK-SDA", body)
        self.assertNotIn("TXN-BULK-WIRE", body)

    def test_role_scope_inputs_are_unchanged(self):
        _, mock_get = self._get()
        mock_get.assert_called_once_with(role={"sam"}, user_key=200, accounting_group_key=None)


class ExportBulkTests(unittest.TestCase):
    def _rows(self, records):
        text = export.build_dashboard_export_csv(records).decode("utf-8-sig")
        keys = [k for _, k in export.EXPORT_COLUMNS]
        rows = list(csv.reader(io.StringIO(text)))
        return {r[keys.index("request_id")]: dict(zip(keys, r)) for r in rows[1:]}, rows[0]

    def test_bulk_is_identified_and_the_original_type_and_urgency_are_preserved(self):
        by_id, header = self._rows(_dash_records())
        self.assertIn("Recipient Type", header)
        self.assertEqual(by_id["TXN-BULK-SDA"]["recipient_mode"], "Bulk Recipients")
        self.assertEqual(by_id["TXN-BULK-SDA"]["request_type"], "Same Day ACH")
        self.assertEqual(by_id["TXN-BULK-SDA"]["urgent"], "Yes")
        self.assertEqual(by_id["TXN-BULK-WIRE"]["request_type"], "Wire")
        self.assertEqual(by_id["TXN-PLAIN"]["recipient_mode"], "")

    def test_no_account_routing_or_listing_content_is_ever_exported(self):
        poisoned = dict(_dash_records()[0], recv_account_number=ACCT[1], recv_routing_number=RTN,
                        orig_account_number=ACCT[2], bulk_recipients=[{"line": 2, "bank_account_key": 31}],
                        bulk_listing_sha256="a" * 64, bulk_listing_rows=[(ACCT[3], RTN)])
        text = export.build_dashboard_export_csv([poisoned]).decode("utf-8-sig")
        _no_numbers(self, text)
        self.assertNotIn("a" * 64, text)
        self.assertNotIn("bank_account_key", text)

    def test_the_export_route_reuses_the_dashboard_scope_and_filters_for_bulk_rows(self):
        client = app_module.app.test_client()
        with client.session_transaction() as sess:
            sess["role"] = "sam"
        with patch.object(app_module, "current_app_user_key", return_value=200), \
             patch.object(app_module.db, "get_dashboard_records", return_value=_dash_records()) as mock_get:
            resp = client.get("/dashboard/export?request_type=Wire")
        mock_get.assert_called_once_with(role={"sam"}, user_key=200, accounting_group_key=None)
        text = resp.data.decode("utf-8-sig")
        self.assertIn("TXN-BULK-WIRE", text)
        self.assertNotIn("TXN-BULK-SDA", text)
        self.assertIn("Bulk Recipients", text)

    def test_the_new_export_key_is_not_a_banking_or_surrogate_field(self):
        keys = [k for _, k in export.EXPORT_COLUMNS]
        self.assertIn("recipient_mode", keys)
        self.assertNotIn("bulk_recipients", keys)
        self.assertNotIn("bank_account_key", keys)


# ── 2. Feature gate: create vs. read ─────────────────────────────────────────

class _DetailCursor:
    COLS = ["request_id", "request_type", "status", "amount", "recipient_bank_account_key", "_instruction_key",
            "recv_payee_name", "recv_account_number", "recv_routing_number"]

    def __init__(self, recipient_key=None, instruction_key=None):
        self.description = [(c,) for c in self.COLS]
        self.row = ("TXN-1", "ACH", "Pending Approver", 10, recipient_key, instruction_key, "", "", "")
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append(sql)

    def fetchone(self):
        return self.row

    def fetchall(self):
        return []


class GateReadBehaviorTests(unittest.TestCase):
    STATE = {"mode": "Bulk", "listing_sha256": "a" * 64, "recipient_rows": 3, "matched_rows": 3, "validated_at": "x"}

    def _detail(self, *, ready, env="false", recipient_key=None, instruction_key=None, state=None):
        cur = _DetailCursor(recipient_key, instruction_key)
        conn = MagicMock()
        conn.cursor.return_value = cur
        with patch.dict(os.environ, {"BULK_RECIPIENT_ENABLED": env}), \
             patch.object(db, "get_connection", return_value=conn), \
             patch.object(db, "bulk_schema_ready", return_value=ready), \
             patch.object(db, "get_bulk_state", return_value=state or self.STATE) as get_state:
            record = db.get_request_detail("TXN-1")
        return record, get_state

    def test_existing_bulk_transactions_stay_accurate_when_the_create_switch_is_off(self):
        record, get_state = self._detail(ready=True, env="false")
        self.assertEqual(record["recipient_mode"], "bulk")
        self.assertEqual((record["bulk_recipient_rows"], record["bulk_matched_rows"]), (3, 3))
        get_state.assert_called_once()

    def test_an_unmigrated_schema_never_reads_the_new_columns(self):
        record, get_state = self._detail(ready=False, instruction_key=7)
        get_state.assert_not_called()
        self.assertEqual(record["recipient_mode"], "external")  # a real single-beneficiary row

    def test_a_row_with_no_beneficiary_and_no_readable_mode_is_unavailable_not_external(self):
        record, _ = self._detail(ready=False, instruction_key=None)
        self.assertEqual(record["recipient_mode"], "unavailable")

    def test_internal_recipient_rows_are_unchanged(self):
        record, _ = self._detail(ready=False, recipient_key=9, instruction_key=7)
        self.assertEqual(record["recipient_mode"], "internal")

    def test_a_stored_non_bulk_mode_wins_over_the_derivation(self):
        record, _ = self._detail(ready=True, instruction_key=7, state=dict(self.STATE, mode="External"))
        self.assertEqual(record["recipient_mode"], "external")

    def test_effective_mode_never_guesses_external_for_a_row_without_a_beneficiary(self):
        self.assertEqual(db.effective_recipient_mode(None, None, has_instruction=False), "unavailable")
        self.assertEqual(db.effective_recipient_mode("", 9, has_instruction=False), "internal")
        self.assertEqual(db.effective_recipient_mode("Bulk", None, has_instruction=False), "bulk")


class GateRenderTests(unittest.TestCase):
    def setUp(self):
        self.case = ir.DetailPageTests("test_24_legacy_transaction_without_recipient_fields_renders_as_external")
        self.case.setUp()

    def _body(self, **overrides):
        return self.case._render(self.case._record(**overrides)).get_data(as_text=True)

    def test_an_unavailable_row_shows_a_notice_and_no_beneficiary_details(self):
        body = self._body(recipient_mode="unavailable", recv_payee_name="", recv_bank_name="",
                          recv_account_number="", recv_routing_number="")
        self.assertIn("recipient-details-unavailable", body)
        self.assertIn("Recipient details unavailable", body)
        self.assertNotIn("External Payee", body)
        self.assertNotIn("recv-acct-masked", body)

    def test_bulk_detail_renders_while_the_create_switch_is_off(self):
        with patch.object(app_module, "bulk_recipient_enabled", return_value=False):
            body = self._body(recipient_mode="bulk", bulk_recipient_rows=2, bulk_matched_rows=2,
                              recv_account_number="", recv_routing_number="")
        self.assertIn("Bulk Recipients", body)
        self.assertNotIn("External Payee", body)

    def test_approval_actions_do_not_depend_on_beneficiary_rows_or_recipient_mode(self):
        import inspect
        for fn in (db.get_transaction_for_workflow, db.get_transaction_banking_keys):
            src = inspect.getsource(fn)
            self.assertNotIn("Recipient_Mode", src)
        self.assertNotIn("JOIN [etransactions].[Beneficiary", inspect.getsource(db.get_transaction_for_workflow))
        # Every single-beneficiary join other than the prior-use history match is a LEFT JOIN.
        src = inspect.getsource(db)
        all_joins = src.count("JOIN [etransactions].[Beneficiary")
        left_joins = src.count("LEFT JOIN [etransactions].[Beneficiary")
        prior_joins = inspect.getsource(db.find_prior_completed_beneficiary_match).count("JOIN [etransactions].[Beneficiary")
        self.assertEqual(all_joins - left_joins, prior_joins)

    def test_reveal_of_a_bulk_receiving_side_fails_safely(self):
        client = app_module.app.test_client()
        with client.session_transaction() as sess:
            sess["role"] = "treasury"
        with patch.object(app_module, "current_app_user_key", return_value=1), \
             patch.object(app_module.db, "get_transaction_for_workflow",
                          return_value={"status": wf.STATUS_PENDING_APPROVER, "accounting_group_key": None}), \
             patch.object(app_module.authorization, "can_view_transaction", return_value=True), \
             patch.object(app_module, "_authorized_group_keys_by_role", return_value={}), \
             patch.object(app_module.db, "get_transaction_banking_keys",
                          return_value={"originating_bank_account_key": 5, "beneficiary_instruction_key": None}), \
             patch.object(app_module.banking_security, "audit_banking_data_reveal"):
            resp = client.post("/dashboard/request/TXN-1/reveal-banking/receiving", data={"field": "account_number"})
        self.assertEqual(resp.status_code, 404)


class _BulkGateOff(tb._BulkRouteBase):
    gate = False


class DraftEditGateTests(unittest.TestCase):
    def _edit(self, enabled):
        client = app_module.app.test_client()
        with client.session_transaction() as sess:
            sess["role"] = "submitter"
        draft = {"transaction_key": 55, "request_id": "TXN-2026-D055", "recipient_mode": "bulk"}
        with patch.object(app_module, "current_app_user", return_value={"user_key": 11, "display_name": "U", "email": ""}), \
             patch.object(app_module.db, "get_draft_for_edit", return_value=draft), \
             patch.object(app_module, "render_template", return_value="DRAFT-FORM"), \
             patch.object(app_module, "bulk_recipient_enabled", return_value=enabled):
            return client.get("/intake/draft/55", follow_redirects=False)

    def test_a_bulk_draft_cannot_be_edited_while_the_feature_is_off(self):
        resp = self._edit(False)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/dashboard"))

    def test_a_bulk_draft_opens_when_the_feature_is_on(self):
        resp = self._edit(True)
        self.assertEqual((resp.status_code, resp.get_data(as_text=True)), (200, "DRAFT-FORM"))


class FeatureOffRejectsNewBulkTests(_BulkGateOff):
    def test_new_bulk_submission_and_draft_are_rejected_and_nothing_is_written(self):
        for form in (tb._bulk_form(), tb._draft_form()):
            resp, mk = self._bulk_post(form)
            self.assertEqual(resp.status_code, 200)
            self.assertIn("Bulk recipient payments are not available.", resp.get_data(as_text=True))
            for name in ("insert_transaction", "create_draft", "update_draft", "finalize_draft_submission"):
                mk[name].assert_not_called()

    def test_finalizing_an_existing_bulk_draft_is_also_rejected(self):
        resp, mk = self._bulk_post(tb._bulk_form(transaction_key="55"), files={})
        self.assertEqual(resp.status_code, 200)
        mk["finalize_draft_submission"].assert_not_called()

    def test_single_recipient_requests_still_submit_normally_while_off(self):
        resp, mk = self._post(ir._external_form())
        self.assertEqual(resp.status_code, 302)
        mk["insert_transaction"].assert_called_once()
        resp, mk = self._post(ir._form(), summary=ir._summary())
        self.assertEqual(resp.status_code, 302)


# ── 3. Spreadsheet validation ────────────────────────────────────────────────

def _validate(content, matcher=None, origin=5, name="r.xlsx"):
    return bulk_listing.validate_bulk_listing(
        content, name, originating_bank_account_key=origin, matcher=matcher or tb.matcher())


class SpreadsheetBehaviorTests(unittest.TestCase):
    def test_leading_zeros_reach_the_matcher_exactly_as_typed(self):
        rec = _Recorder(book={"0012345678": 31, "000987": 32})
        result = _validate(tb.make_xlsx([("0012345678", "021000089"), ("000987", "011000015")]), rec)
        self.assertTrue(result.ok)
        self.assertEqual(rec.calls[0], [("0012345678", "021000089"), ("000987", "011000015")])

    def test_excel_style_shared_string_text_cells_are_supported(self):
        rec = _Recorder()
        result = _validate(make_xlsx_shared([(ACCT[1], RTN), (ACCT[2], RTN)]), rec)
        self.assertTrue(result.ok)
        self.assertEqual(rec.calls[0][0], (ACCT[1], RTN))

    def test_numeric_cells_can_never_become_a_silently_wrong_account_number(self):
        # 0012345678 typed into a General-format cell is stored as the number 12345678.
        for numeric in ({(0, 0)}, {(0, 1)}):
            rec = _Recorder()
            result = _validate(tb.make_xlsx([("12345678", "21000089")], numeric=numeric), rec)
            self.assertFalse(result.ok)
            self.assertEqual(rec.calls, [])  # rejected before any lookup
            self.assertTrue(any("not text" in e for e in result.errors))

    def test_fully_blank_rows_are_skipped_but_partially_blank_rows_are_errors(self):
        blank = tb.make_xlsx([(ACCT[1], RTN), (None, None), (ACCT[2], RTN)])
        result = _validate(blank)
        self.assertTrue(result.ok)
        self.assertEqual([r["line"] for r in result.recipients], [2, 4])
        partial = _validate(tb.make_xlsx([(ACCT[1], RTN), (ACCT[2], None)]))
        self.assertFalse(partial.ok)
        self.assertIn("Row 3: Routing Number is blank.", partial.errors)

    def test_every_actual_row_is_looked_up_and_a_miss_blocks_the_whole_listing(self):
        rec = _Recorder(book={ACCT[1]: 31, ACCT[3]: 33})
        result = _validate(tb.good_xlsx(), rec)
        self.assertEqual(len(rec.calls[0]), 3)
        self.assertFalse(result.ok)
        self.assertEqual(result.recipients, [])
        self.assertEqual((result.row_count, result.matched_count), (3, 0))

    def test_an_ambiguous_match_blocks_the_whole_listing(self):
        rec = _Recorder(book={ACCT[2]: 32, ACCT[3]: 33})
        rec.inner = tb.matcher(extra={ACCT[1]: [{"bank_account_key": 31, "status": "Open"},
                                                {"bank_account_key": 34, "status": "Open"}]})
        result = _validate(tb.good_xlsx(), rec)
        self.assertFalse(result.ok)
        self.assertEqual(result.recipients, [])

    def test_matching_uses_full_numbers_never_last_four(self):
        cur = MagicMock()
        cur.fetchall.return_value = []
        conn = MagicMock()
        conn.cursor.return_value = cur
        with patch.object(db, "get_connection", return_value=conn):
            db.match_bulk_recipient_accounts([(ACCT[1], RTN)])
        sql, params = cur.execute.call_args[0]
        self.assertIn("AccountNumber = ? AND RoutingNumber = ?", sql)
        for forbidden in ("LIKE", "RIGHT(", "SUBSTRING", "Last4", "last4"):
            self.assertNotIn(forbidden, sql)
        self.assertEqual(params, [ACCT[1], RTN])
        self.assertEqual(len(params[0]), 10)

    def test_numbers_never_appear_in_messages_or_logs(self):
        records = []

        class _H(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage() + (record.exc_text or ""))

        handler = _H(level=logging.DEBUG)
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        for content, m in ((tb.good_xlsx(), tb.matcher(book={})), (tb.good_xlsx(), tb.matcher(status="Closed")),
                           (tb.make_xlsx([(ACCT[1], RTN), (ACCT[1], RTN)]), tb.matcher())):
            result = _validate(content, m)
            _no_numbers(self, " ".join(result.errors))
        client = app_module.app.test_client()
        with client.session_transaction() as sess:
            sess["role"] = "submitter"
        with patch.object(app_module, "bulk_recipient_enabled", return_value=True), \
             patch.object(app_module.db, "match_bulk_recipient_accounts", side_effect=tb.matcher(book={})):
            resp = client.post("/intake/bulk-listing/check", content_type="multipart/form-data",
                               data={"file_bulk_recipient_listing": (io.BytesIO(tb.good_xlsx()), "r.xlsx")})
        _no_numbers(self, resp.get_data(as_text=True))
        _no_numbers(self, " ".join(records))


class ConfigurableLimitTests(unittest.TestCase):
    def test_defaults_are_the_documented_operational_restrictions(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BULK_LISTING_MAX_ROWS", None)
            os.environ.pop("BULK_LISTING_MAX_FILE_MB", None)
            self.assertEqual((bulk_listing.max_rows(), bulk_listing.max_file_bytes()), (500, 5 * 1024 * 1024))

    def test_limits_are_configurable_with_safe_fallbacks_and_ceilings(self):
        with patch.dict(os.environ, {"BULK_LISTING_MAX_ROWS": "2", "BULK_LISTING_MAX_FILE_MB": "1"}):
            self.assertEqual((bulk_listing.max_rows(), bulk_listing.max_file_bytes()), (2, 1024 * 1024))
            with self.assertRaises(bulk_listing.ListingError):
                bulk_listing.parse_listing(tb.good_xlsx(), "r.xlsx")
        with patch.dict(os.environ, {"BULK_LISTING_MAX_ROWS": "abc", "BULK_LISTING_MAX_FILE_MB": "-4"}):
            self.assertEqual((bulk_listing.max_rows(), bulk_listing.max_file_bytes()), (500, 5 * 1024 * 1024))
        with patch.dict(os.environ, {"BULK_LISTING_MAX_ROWS": "999999", "BULK_LISTING_MAX_FILE_MB": "999"}):
            self.assertEqual(bulk_listing.max_rows(), bulk_listing.CEILING_MAX_ROWS)
            self.assertEqual(bulk_listing.max_file_bytes(), bulk_listing.CEILING_MAX_FILE_BYTES)

    def test_the_posted_file_read_honors_the_configured_size(self):
        with patch.dict(os.environ, {"BULK_LISTING_MAX_FILE_MB": "1"}):
            with self.assertRaises(bulk_listing.ListingError):
                bulk_listing.check_file_basics("r.xlsx", 2 * 1024 * 1024)
            bulk_listing.check_file_basics("r.xlsx", 1024)


class ListingTemplateTests(unittest.TestCase):
    PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "templates",
                        "Bulk_Recipient_Listing_Template.xlsx")

    def test_the_template_has_the_expected_headers_and_text_formatted_columns(self):
        with open(self.PATH, "rb") as handle:
            content = handle.read()
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            sheet = zf.read("xl/worksheets/sheet1.xml").decode()
            styles = zf.read("xl/styles.xml").decode()
        self.assertIn("Account Number", sheet)
        self.assertIn("Routing Number", sheet)
        self.assertIn('style="1"', sheet)
        self.assertIn('numFmtId="49"', styles)  # "@" Text
        # The application's reader accepts the headers and then reports there are no recipients yet.
        with self.assertRaises(bulk_listing.ListingError) as ctx:
            bulk_listing.parse_listing(content, "template.xlsx")
        self.assertIn("no recipient rows", str(ctx.exception))

    def test_the_intake_page_links_the_template(self):
        client = app_module.app.test_client()
        with client.session_transaction() as sess:
            sess["role"] = "submitter"
        accounts = [{"bank_account_key": 5, "bank_name": "W", "account_title": "O", "account_last4": "1111",
                     "entity_name": "A", "classification": "Corporate"}]
        with patch.object(app_module, "current_app_user", return_value={"user_key": 11, "display_name": "U", "email": ""}), \
             patch.object(app_module.db, "get_bank_accounts", return_value=accounts), \
             patch.object(app_module.db, "get_user_list", return_value=[]), \
             patch.object(app_module.db, "get_reassignment_candidates", return_value=[]), \
             patch.object(app_module, "bulk_recipient_enabled", return_value=True):
            body = client.get("/intake").get_data(as_text=True)
        self.assertIn("Bulk_Recipient_Listing_Template.xlsx", body)


# ── 4. Controls preserved ────────────────────────────────────────────────────

class ControlsPreservedTests(tb._BulkRouteBase):
    def test_every_protective_control_still_blocks(self):
        cases = {
            "unmatched": (tb.matcher(book={ACCT[1]: 31}), {}),
            "closed": (tb.matcher(status="Closed"), {}),
            "ambiguous": (tb.matcher(extra={ACCT[1]: [{"bank_account_key": 1, "status": "Open"},
                                                      {"bank_account_key": 2, "status": "Open"}]}), {}),
            "originating": (tb.matcher(), {"bank_account_key": "31"}),
            "duplicate": (tb.matcher(), {}),
        }
        for label, (m, overrides) in cases.items():
            content = tb.make_xlsx([(ACCT[1], RTN), (ACCT[1], RTN)]) if label == "duplicate" else None
            with self.subTest(label):
                resp, mk = self._bulk_post(tb._bulk_form(**overrides), files=tb._bulk_files(content), m=m)
                self.assertEqual(resp.status_code, 200)
                mk["insert_transaction"].assert_not_called()

    def test_verbal_confirmation_and_payment_support_are_still_required(self):
        resp, mk = self._bulk_post(tb._bulk_form(verbal_contact_name=""))
        self.assertIn("Verbal Confirmation Contact Name is required", resp.get_data(as_text=True))
        resp, mk = self._bulk_post(tb._bulk_form(), files=tb._bulk_files(file_payment_support=True))
        self.assertIn("Payment Support attachment is required.", resp.get_data(as_text=True))

    def test_single_recipient_rules_are_unchanged_when_the_feature_is_on(self):
        resp, mk = self._bulk_post(ir._external_form(), files=ir._files(file_wire_ach_instructions=True))
        self.assertIn("External ACH/Wire Instructions attachment is required.", resp.get_data(as_text=True))
        resp, mk = self._bulk_post(ir._external_form(avs_score="150"), files=ir._files())
        self.assertIn("AVS Score must be between 0 and 100.", resp.get_data(as_text=True))
        resp, mk = self._bulk_post(ir._external_form(instructions_previously_used="yes", last_used_date="2026-01-01"),
                                   files=ir._files(), prior_match=None)
        self.assertIn("could not be verified as previously used", resp.get_data(as_text=True))

    def test_eligibility_that_changes_after_the_check_blocks_the_submission(self):
        check = self._check()
        self.assertTrue(check.get_json()["ok"])
        resp, mk = self._bulk_post(tb._bulk_form(), m=tb.matcher(status="Closed"))
        self.assertEqual(resp.status_code, 200)
        mk["insert_transaction"].assert_not_called()


# ── 5. Persistence and file integrity ────────────────────────────────────────

class StoredFileIntegrityTests(tb._BulkRouteBase):
    def _stored(self, content):
        return ("recipients.xlsx", content)

    def test_a_stored_file_that_differs_from_the_verified_file_blocks_a_new_submission(self):
        other = tb.make_xlsx([(ACCT[1], RTN)])
        with patch.object(app_module, "sharepoint_enabled", return_value=True), \
             patch.object(app_module.sharepoint, "read_latest_bulk_listing", return_value=self._stored(other)):
            resp, mk = self._bulk_post(tb._bulk_form(), confirm=False)
        self.assertEqual(resp.status_code, 200)
        mk["insert_transaction"].assert_not_called()
        self.assertIn("could not be confirmed", resp.get_data(as_text=True))

    def test_a_matching_stored_file_allows_the_submission_and_the_hash_is_the_stored_files(self):
        content = tb.good_xlsx()
        with patch.object(app_module, "sharepoint_enabled", return_value=True), \
             patch.object(app_module.sharepoint, "read_latest_bulk_listing", return_value=self._stored(content)):
            resp, mk = self._bulk_post(tb._bulk_form(), confirm=False)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(mk["insert_transaction"].call_args[0][0]["bulk_listing_sha256"],
                         hashlib.sha256(content).hexdigest())

    def test_a_missing_or_unreadable_stored_file_counts_as_a_mismatch(self):
        for effect in ({"return_value": None}, {"side_effect": RuntimeError("graph")}):
            with patch.object(app_module, "sharepoint_enabled", return_value=True), \
                 patch.object(app_module.sharepoint, "read_latest_bulk_listing", **effect):
                resp, mk = self._bulk_post(tb._bulk_form(), confirm=False)
            self.assertEqual(resp.status_code, 200)
            mk["insert_transaction"].assert_not_called()

    def test_finalizing_a_draft_whose_stored_file_changed_is_blocked(self):
        item = {"doc_type": sharepoint.DOC_TYPE_BULK_RECIPIENT_LISTING, "filename": "x"}
        pay = {"doc_type": sharepoint.DOC_TYPE_PAYMENT_SUPPORT, "filename": "y"}
        reads = iter([self._stored(tb.good_xlsx()), self._stored(tb.make_xlsx([(ACCT[1], RTN)]))])
        with patch.object(app_module, "sharepoint_enabled", return_value=True), \
             patch.object(app_module.sharepoint, "list_attachments", return_value=[item, pay]), \
             patch.object(app_module.sharepoint, "read_latest_bulk_listing", side_effect=lambda *a: next(reads)):
            resp, mk = self._bulk_post(tb._bulk_form(transaction_key="55"), files={}, confirm=False)
        self.assertEqual(resp.status_code, 200)  # verified file != file re-read just before finalize
        mk["finalize_draft_submission"].assert_not_called()

    def test_replacing_the_spreadsheet_replaces_the_earlier_verification(self):
        new = tb.make_xlsx([(ACCT[1], RTN)])
        stored = ("old.xlsx", tb.good_xlsx())
        with patch.object(app_module, "sharepoint_enabled", return_value=True), \
             patch.object(app_module.sharepoint, "read_latest_bulk_listing", return_value=stored) as read:
            resp, mk = self._bulk_post(tb._draft_form(transaction_key="55"), files=tb._bulk_files(new))
        self.assertEqual(resp.status_code, 302)
        data = mk["update_draft"].call_args[0][1]
        self.assertEqual(data["bulk_listing_sha256"], hashlib.sha256(new).hexdigest())
        self.assertEqual([r["bank_account_key"] for r in data["bulk_recipients"]], [31])
        read.assert_not_called()  # the old stored file is not what was verified

    def test_a_listing_that_could_not_be_stored_leaves_no_verified_recipients_on_the_draft(self):
        def failing_upload(request_id):
            raise app_module.AttachmentUploadError(["Bulk Recipient Listing"])

        with patch.object(app_module.db, "set_draft_bulk_recipients") as clear:
            resp, mk = self._bulk_post(tb._draft_form(), extra=(patch.object(
                app_module, "_upload_intake_attachments", side_effect=failing_upload),))
        self.assertEqual(resp.status_code, 302)
        clear.assert_called_once_with(55, 11, [], None)

    def test_a_stored_file_that_does_not_match_after_a_draft_save_clears_the_recipients(self):
        with patch.object(app_module, "sharepoint_enabled", return_value=True), \
             patch.object(app_module.sharepoint, "read_latest_bulk_listing",
                          return_value=self._stored(tb.make_xlsx([(ACCT[3], RTN)]))), \
             patch.object(app_module.db, "set_draft_bulk_recipients") as clear:
            resp, mk = self._bulk_post(tb._draft_form(), confirm=False)
        self.assertEqual(resp.status_code, 302)
        clear.assert_called_once_with(55, 11, [], None)


class DbIntegrityTests(unittest.TestCase):
    DATA = dict(ir._INSERT_DATA, recipient_bank_account_key=None, recv_payee_name="", recipient_mode="Bulk",
                bulk_recipients=[{"line": 2, "bank_account_key": 31}, {"line": 3, "bank_account_key": 32}],
                bulk_listing_sha256="c" * 64)

    def _insert(self, data=None, closed=False):
        cur = tb._EligibilityCursor(bank_row=ir._bank_row(), closed=closed)
        conn = ir._conn(cur)
        with patch.object(db, "get_connection", return_value=conn), \
             patch.object(db, "resolve_approval_rule", return_value=ir._RULE), \
             patch.object(db, "resolve_entity_key", return_value=3):
            try:
                db.insert_transaction(dict(data or self.DATA))
                error = None
            except db.InternalRecipientError as exc:
                error = exc
        return cur, conn, error

    def test_recipients_that_became_ineligible_cannot_create_a_transaction_or_workflow_event(self):
        cur, conn, error = self._insert(closed=True)
        self.assertIsNotNone(error)
        conn.rollback.assert_called()
        conn.commit.assert_not_called()
        self.assertFalse(cur.statements("INSERT INTO [etransactions].[WorkflowEvent]"))
        self.assertFalse(cur.statements("INSERT INTO [etransactions].[WorkflowAssignment]"))
        self.assertFalse(cur.statements("INSERT INTO [etransactions].[BulkRecipientAccount]"))
        _no_numbers(self, str(error))

    def test_the_eligibility_check_names_only_keys_and_the_originating_account(self):
        cur, _, _ = self._insert()
        sql, params = cur.statements("SELECT COUNT(*) FROM [etransactions].[BankAccount]")[0]
        self.assertIn("Status = 'Open'", sql)
        self.assertIn("BankAccount_Key <> ?", sql)
        self.assertEqual(params, [31, 32, 5])  # recipient keys, then the originating account key

    def test_empty_unhashed_or_duplicate_verifications_are_refused(self):
        for patch_data in ({"bulk_recipients": []}, {"bulk_listing_sha256": None},
                           {"bulk_recipients": [{"line": 2, "bank_account_key": 31}, {"line": 3, "bank_account_key": 31}]}):
            with self.subTest(patch_data):
                _, conn, error = self._insert(dict(self.DATA, **patch_data))
                self.assertIsNotNone(error)
                conn.commit.assert_not_called()

    def test_recipient_rows_belong_to_the_transaction_just_created_and_carry_no_banking_numbers(self):
        cur, conn, error = self._insert()
        self.assertIsNone(error)
        conn.commit.assert_called_once()
        children = cur.statements("INSERT INTO [etransactions].[BulkRecipientAccount]")
        self.assertEqual({p[0] for _, p in children}, {99})  # the surrogate key the fake returns for ETransaction
        for sql, _ in children:
            for forbidden in ("AccountNumber", "RoutingNumber", "Account_Number", "Routing_Number"):
                self.assertNotIn(forbidden, sql)
        self.assertTrue(all(p[5] == "c" * 64 for _, p in children))

    def test_finalizing_a_draft_with_an_ineligible_recipient_rolls_back_before_any_workflow_row(self):
        cur = tb._EligibilityCursor(guard_row=(10, 20), closed=True)
        conn = ir._conn(cur)
        data = dict(self.DATA, approval_rule_key=7, requires_vp=False, requires_cfo=False,
                    approval_tier="Sr. Accounting Manager", treasury_service_date="2026-10-01")
        with patch.object(db, "get_connection", return_value=conn), patch.object(db, "resolve_entity_key", return_value=3):
            with self.assertRaises(db.InternalRecipientError):
                db.finalize_draft_submission(55, data, prepared_by_user_key=11)
        conn.commit.assert_not_called()
        self.assertFalse(cur.statements("INSERT INTO [etransactions].[WorkflowEvent]"))

    def test_draft_saves_do_not_run_the_submission_eligibility_check(self):
        cur = tb._EligibilityCursor()
        db._save_recipient_mode_state(cur, 55, self.DATA, None)
        self.assertFalse(cur.statements("SELECT COUNT(*)"))
        self.assertEqual(len(cur.statements("INSERT INTO [etransactions].[BulkRecipientAccount]")), 2)

    def test_the_submission_error_is_reported_to_the_requester_without_banking_values(self):
        client = app_module.app.test_client()
        with client.session_transaction() as sess:
            sess["role"] = "submitter"
        form = tb._bulk_form()
        extra = (patch.object(app_module, "bulk_recipient_enabled", return_value=True),
                 patch.object(app_module, "_stored_listing_matches", return_value=True),
                 patch.object(app_module.db, "match_bulk_recipient_accounts", side_effect=tb.matcher()),
                 patch.object(app_module.db, "insert_transaction",
                              side_effect=db.InternalRecipientError("One or more bulk recipient accounts are no longer eligible.")))
        base = ir._RouteBase("setUp")
        base.client = client
        resp, mk = base._post(form, files=tb._bulk_files(), extra=extra)
        body = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("no longer eligible", body)
        _no_numbers(self, body)


class ContractMatchesCodeTests(unittest.TestCase):
    """The schema contract must describe the SQL the code actually issues."""

    @classmethod
    def setUpClass(cls):
        root = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(root, "BULK_RECIPIENT_SCHEMA_CONTRACT.md"), encoding="utf-8") as handle:
            cls.contract = handle.read()

    def test_every_column_the_schema_check_requires_is_documented(self):
        for column in sorted(db._BULK_REQUIRED_TRANSACTION_COLUMNS | db._BULK_REQUIRED_CHILD_COLUMNS):
            self.assertIn(f"`{column}`", self.contract)
        self.assertIn("etransactions.BulkRecipientAccount", self.contract)

    def test_the_documented_statements_match_the_code(self):
        import inspect
        src = inspect.getsource(db)
        shared = (
            "(Transaction_Key, Line_Number, BankAccount_Key, Verification_Outcome, Verified_DateTime, Listing_SHA256)",
            "SET Recipient_Mode = ? WHERE Transaction_Key = ?",
            "AccountNumber = ? AND RoutingNumber = ?",
            "Status = 'Open' AND BankAccount_Key <> ?",
            "Bulk_Listing_SHA256 = ?, Bulk_Recipient_Count = ?",
            "Verification_Outcome = 'Matched'",
        )
        for fragment in shared:
            self.assertIn(fragment, self.contract)
            self.assertIn(fragment, src)
        for fragment in ("t.Recipient_Mode", "AS recipient_mode", "AS _instruction_key"):
            self.assertIn(fragment, src)
            self.assertIn(fragment, self.contract)

    def test_both_gates_and_the_unavailable_state_are_documented(self):
        for text in ("Read gate", "Create gate", "bulk_schema_ready", "bulk_recipient_enabled",
                     "Recipient details unavailable", "Recipient Type", "snapshot isolation"):
            self.assertIn(text, self.contract)


class IsolationTests(unittest.TestCase):
    def test_no_external_access_was_attempted(self):
        self.assertEqual(external_guard.state.blocked, [])


if __name__ == "__main__":
    unittest.main()
