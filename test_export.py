"""
Tests for Batch 9: filtered dashboard export.

Export is NOT a separate authorization model — the export route reuses
app._resolve_dashboard_authorized_scope() / app._apply_dashboard_filters(),
the exact same functions dashboard() uses. These tests therefore focus on:
  (a) the export module's pure formatting/sanitization (export.py), and
  (b) the export ROUTE never trusting browser-supplied role/user/group
      values, always narrowing (never widening) the authorized scope, and
      never invoking Reveal or leaking sensitive banking data.

Run:  python -m unittest test_export -v
"""
import csv
import io
import re
import unittest
from unittest.mock import patch

import app as app_module
import export
import workflow as wf


def _record(**overrides):
    base = {
        "transaction_key": 1, "request_id": "TXN-2026-001",
        "property_dept": "Sunset Ridge", "property_code": "E100",
        "request_type": "ACH", "treasury_service_date": "2026-10-01",
        "prepared_date": "2026-09-01", "submitted_date": "2026-09-02",
        "last_modified_date": "2026-09-03",
        "amount": 1500.5, "currency": "USD", "payment_purpose": "Vendor payment",
        "urgent": False, "urgency_reason": "",
        "status": wf.STATUS_PENDING_APPROVER, "current_workflow_stage": "Approver",
        "approval_tier": "Senior Accounting Manager / Assistant Controller",
        "requires_vp": False, "over_1m": False,
        "prepared_by": "Prep One", "assigned_approver": "Owner One",
        "approver": "Approver One", "controller": "Controller One",
        "vp_approver": "", "cfo_approver": "", "accounting_group_name": "Group A",
        "days_pending": 1,
    }
    base.update(overrides)
    return base


class ExportCsvFormatTests(unittest.TestCase):
    """Pure formatting/sanitization tests for export.py — no Flask involved."""

    def _rows(self, csv_bytes):
        text = csv_bytes.decode("utf-8-sig")
        return list(csv.reader(io.StringIO(text)))

    def test_header_row_matches_export_columns(self):
        rows = self._rows(export.build_dashboard_export_csv([]))
        self.assertEqual(rows[0], [h for h, _ in export.EXPORT_COLUMNS])

    def test_amount_stays_numeric_two_decimals(self):
        rows = self._rows(export.build_dashboard_export_csv([_record(amount=1234.5)]))
        amount_idx = [k for _, k in export.EXPORT_COLUMNS].index("amount")
        self.assertEqual(rows[1][amount_idx], "1234.50")

    def test_urgent_renders_as_yes_no(self):
        rows = self._rows(export.build_dashboard_export_csv([_record(urgent=True), _record(urgent=False)]))
        urgent_idx = [k for _, k in export.EXPORT_COLUMNS].index("urgent")
        self.assertEqual(rows[1][urgent_idx], "Yes")
        self.assertEqual(rows[2][urgent_idx], "No")

    def test_dates_pass_through_iso_format(self):
        rows = self._rows(export.build_dashboard_export_csv([_record(prepared_date="2026-09-01")]))
        idx = [k for _, k in export.EXPORT_COLUMNS].index("prepared_date")
        self.assertEqual(rows[1][idx], "2026-09-01")

    def test_formula_injection_text_is_sanitized(self):
        malicious = _record(payment_purpose="=cmd|'/c calc'!A1")
        rows = self._rows(export.build_dashboard_export_csv([malicious]))
        idx = [k for _, k in export.EXPORT_COLUMNS].index("payment_purpose")
        self.assertTrue(rows[1][idx].startswith("'="))

    def test_plus_minus_at_prefixes_are_sanitized(self):
        for bad in ("+1+1", "-2+3", "@SUM(A1)"):
            rows = self._rows(export.build_dashboard_export_csv([_record(payment_purpose=bad)]))
            idx = [k for _, k in export.EXPORT_COLUMNS].index("payment_purpose")
            self.assertTrue(rows[1][idx].startswith("'"))

    def test_ordinary_text_is_not_modified(self):
        rows = self._rows(export.build_dashboard_export_csv([_record(payment_purpose="Vendor payment")]))
        idx = [k for _, k in export.EXPORT_COLUMNS].index("payment_purpose")
        self.assertEqual(rows[1][idx], "Vendor payment")

    def test_no_internal_surrogate_keys_in_columns(self):
        keys = [k for _, k in export.EXPORT_COLUMNS]
        self.assertNotIn("transaction_key", keys)
        self.assertNotIn("current_owner_user_key", keys)

    def test_no_banking_fields_in_columns(self):
        keys = [k for _, k in export.EXPORT_COLUMNS]
        for banking_key in (
            "orig_account_number", "orig_routing_number", "recv_account_number",
            "recv_routing_number", "taxidnumber", "recv_bank_name", "orig_bank_name",
        ):
            self.assertNotIn(banking_key, keys)

    def test_extra_sensitive_keys_on_record_never_leak(self):
        # Even if a record dict happened to carry sensitive extra keys not in
        # EXPORT_COLUMNS, only allow-listed columns are ever written out.
        poisoned = _record()
        poisoned["orig_account_number"] = "999999999999"
        poisoned["recv_routing_number"] = "021000021"
        csv_bytes = export.build_dashboard_export_csv([poisoned])
        text = csv_bytes.decode("utf-8-sig")
        self.assertNotIn("999999999999", text)
        self.assertNotIn("021000021", text)

    def test_filename_pattern_is_safe_and_content_free(self):
        name = export.export_filename()
        self.assertRegex(name, r"^ETransaction_Export_\d{8}_\d{6}\.csv$")
        self.assertNotIn("@", name)  # no email
        self.assertNotIn("TXN", name)  # no transaction id


class ExportRouteAuthorizationTests(unittest.TestCase):
    """
    Route-level: the export endpoint must resolve role/user_key from the
    server session ONLY, reuse the exact dashboard scoping function, never
    broaden on an unauthorized accounting_group_key, and never call Reveal.
    """

    def setUp(self):
        self.client = app_module.app.test_client()

    def _set_role(self, role):
        with self.client.session_transaction() as sess:
            sess["role"] = role

    def test_requester_export_contains_own_transaction(self):
        self._set_role("submitter")
        records = [_record(request_id="TXN-OWN-1")]
        with patch.object(app_module, "current_app_user_key", return_value=11), \
             patch.object(app_module.db, "get_dashboard_records", return_value=records) as mock_get:
            resp = self.client.get("/dashboard/export")
        self.assertEqual(resp.status_code, 200)
        mock_get.assert_called_once_with(role={"submitter"}, user_key=11, accounting_group_key=None)
        self.assertIn(b"TXN-OWN-1", resp.data)

    def test_requester_export_may_include_own_draft(self):
        self._set_role("submitter")
        records = [_record(request_id="TXN-DRAFT-1", status=wf.STATUS_DRAFT)]
        with patch.object(app_module, "current_app_user_key", return_value=11), \
             patch.object(app_module.db, "get_dashboard_records", return_value=records):
            resp = self.client.get("/dashboard/export")
        self.assertIn(b"TXN-DRAFT-1", resp.data)

    def test_requester_cannot_broaden_via_user_supplied_role_or_user_key(self):
        # Query-string role/user_key must be completely ignored — the server
        # session's role and current_app_user_key() are the only inputs used.
        self._set_role("submitter")
        with patch.object(app_module, "current_app_user_key", return_value=11), \
             patch.object(app_module.db, "get_dashboard_records", return_value=[]) as mock_get:
            self.client.get("/dashboard/export?role=business_admin&user_key=999&show_all=true")
        mock_get.assert_called_once_with(role={"submitter"}, user_key=11, accounting_group_key=None)

    def test_approver_export_uses_same_scope_call_as_dashboard(self):
        self._set_role("sam")
        with patch.object(app_module, "current_app_user_key", return_value=200), \
             patch.object(app_module.db, "get_dashboard_records", return_value=[]) as mock_get:
            self.client.get("/dashboard/export")
        mock_get.assert_called_once_with(role={"sam"}, user_key=200, accounting_group_key=None)

    def test_controller_default_export_uses_personal_scope(self):
        self._set_role("controller")
        with patch.object(app_module, "current_app_user_key", return_value=300), \
             patch.object(app_module.db, "get_user_accounting_group_keys", return_value=[55]), \
             patch.object(app_module.db, "get_dashboard_records", return_value=[]) as mock_get:
            self.client.get("/dashboard/export")
        mock_get.assert_called_once_with(role={"controller"}, user_key=300, accounting_group_key=None)

    def test_controller_authorized_accounting_group_export_works(self):
        self._set_role("controller")
        with patch.object(app_module, "current_app_user_key", return_value=300), \
             patch.object(app_module.db, "get_user_accounting_group_keys", return_value=[55]), \
             patch.object(app_module.db, "get_dashboard_records", return_value=[]) as mock_get:
            self.client.get("/dashboard/export?accounting_group_key=55")
        mock_get.assert_called_once_with(role={"controller"}, user_key=300, accounting_group_key=55)

    def test_unauthorized_accounting_group_cannot_broaden_export(self):
        self._set_role("controller")
        with patch.object(app_module, "current_app_user_key", return_value=300), \
             patch.object(app_module.db, "get_user_accounting_group_keys", return_value=[55]), \
             patch.object(app_module.db, "get_dashboard_records", return_value=[]) as mock_get:
            self.client.get("/dashboard/export?accounting_group_key=77")
        mock_get.assert_called_once_with(role={"controller"}, user_key=300, accounting_group_key=None)

    def test_vp_export_matches_configured_scope(self):
        self._set_role("vp")
        with patch.object(app_module, "current_app_user_key", return_value=400), \
             patch.object(app_module.db, "get_user_accounting_group_keys", return_value=[]), \
             patch.object(app_module.db, "get_dashboard_records", return_value=[]) as mock_get:
            self.client.get("/dashboard/export")
        mock_get.assert_called_once_with(role={"vp"}, user_key=400, accounting_group_key=None)

    def test_cfo_export_matches_configured_scope(self):
        self._set_role("cfo")
        with patch.object(app_module, "current_app_user_key", return_value=500), \
             patch.object(app_module.db, "get_user_accounting_group_keys", return_value=[]), \
             patch.object(app_module.db, "get_dashboard_records", return_value=[]) as mock_get:
            self.client.get("/dashboard/export")
        mock_get.assert_called_once_with(role={"cfo"}, user_key=500, accounting_group_key=None)

    def test_treasury_export_includes_operational_records(self):
        self._set_role("treasury")
        records = [_record(request_id="TXN-AWAIT-REL", status=wf.STATUS_AWAITING_RELEASE)]
        with patch.object(app_module, "current_app_user_key", return_value=600), \
             patch.object(app_module.db, "get_dashboard_records", return_value=records):
            resp = self.client.get("/dashboard/export")
        self.assertIn(b"TXN-AWAIT-REL", resp.data)

    def test_business_admin_export_follows_unrestricted_scope(self):
        self._set_role("business_admin")
        records = [_record(request_id="TXN-ANY")]
        with patch.object(app_module, "current_app_user_key", return_value=700), \
             patch.object(app_module.db, "get_dashboard_records", return_value=records) as mock_get:
            resp = self.client.get("/dashboard/export")
        mock_get.assert_called_once_with(role={"business_admin"}, user_key=700, accounting_group_key=None)
        self.assertIn(b"TXN-ANY", resp.data)

    def test_export_never_invokes_reveal(self):
        self._set_role("sam")
        records = [_record()]
        with patch.object(app_module, "current_app_user_key", return_value=200), \
             patch.object(app_module.db, "get_dashboard_records", return_value=records), \
             patch.object(app_module.db, "get_transaction_banking_keys") as mock_reveal:
            resp = self.client.get("/dashboard/export")
        self.assertEqual(resp.status_code, 200)
        mock_reveal.assert_not_called()

    def test_no_authenticated_data_when_db_disabled(self):
        self._set_role("submitter")
        with patch.object(app_module, "database_enabled", return_value=False):
            resp = self.client.get("/dashboard/export", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)  # redirected back to dashboard, no file generated

    def test_content_disposition_and_content_type_headers(self):
        self._set_role("submitter")
        with patch.object(app_module, "current_app_user_key", return_value=11), \
             patch.object(app_module.db, "get_dashboard_records", return_value=[_record()]):
            resp = self.client.get("/dashboard/export")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.headers["Content-Type"].startswith("text/csv"))
        disposition = resp.headers["Content-Disposition"]
        self.assertIn("attachment", disposition)
        self.assertRegex(disposition, r'filename="ETransaction_Export_\d{8}_\d{6}\.csv"')

    def test_search_returning_nothing_on_dashboard_returns_nothing_on_export(self):
        # A property "search" that matches nobody's transaction on the dashboard
        # must also match nothing on export — never fall back to a broader set.
        self._set_role("submitter")
        with patch.object(app_module, "current_app_user_key", return_value=11), \
             patch.object(app_module.db, "get_dashboard_records", return_value=[_record(request_id="TXN-OWN-1")]):
            resp = self.client.get("/dashboard/export?property=NoSuchProperty")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b"TXN-OWN-1", resp.data)


class ExportFilterConsistencyTests(unittest.TestCase):
    """Every dashboard filter must behave identically for export (Part 11)."""

    def setUp(self):
        self.client = app_module.app.test_client()

    def _set_role(self, role):
        with self.client.session_transaction() as sess:
            sess["role"] = role

    def _scoped(self):
        return [
            _record(request_id="TXN-A", status=wf.STATUS_PENDING_APPROVER, request_type="ACH",
                    property_dept="Alpha Property", assigned_approver="Alice", urgent=True, amount=2_000_000.0),
            _record(request_id="TXN-B", status=wf.STATUS_PENDING_CONTROLLER, request_type="Wire",
                    property_dept="Beta Property", assigned_approver="Bob", urgent=False, amount=500.0),
        ]

    def _export_ids(self, query=""):
        with patch.object(app_module, "current_app_user_key", return_value=200), \
             patch.object(app_module.db, "get_dashboard_records", return_value=self._scoped()):
            resp = self.client.get(f"/dashboard/export{query}")
        text = resp.data.decode("utf-8-sig")
        return set(re.findall(r"TXN-[AB]", text))

    def test_status_filter_affects_export(self):
        self._set_role("sam")
        self.assertEqual(self._export_ids(f"?status={wf.STATUS_PENDING_APPROVER}"), {"TXN-A"})

    def test_request_type_filter_affects_export(self):
        self._set_role("sam")
        self.assertEqual(self._export_ids("?request_type=Wire"), {"TXN-B"})

    def test_property_filter_affects_export(self):
        self._set_role("sam")
        self.assertEqual(self._export_ids("?property=Alpha"), {"TXN-A"})

    def test_urgent_filter_affects_export(self):
        self._set_role("sam")
        self.assertEqual(self._export_ids("?urgent_only=1"), {"TXN-A"})

    def test_amount_range_affects_export(self):
        self._set_role("sam")
        self.assertEqual(self._export_ids("?amount_max=1000"), {"TXN-B"})

    def test_high_dollar_filter_affects_export(self):
        self._set_role("sam")
        self.assertEqual(self._export_ids("?over_1m_only=1"), {"TXN-A"})

    def test_multiple_filters_combine(self):
        self._set_role("sam")
        self.assertEqual(self._export_ids("?urgent_only=1&request_type=ACH"), {"TXN-A"})
        self.assertEqual(self._export_ids("?urgent_only=1&request_type=Wire"), set())

    def test_invalid_amount_filter_does_not_broaden_or_error(self):
        self._set_role("sam")
        # A non-numeric amount_min must be ignored, not raise, and must not
        # broaden the result beyond the authorized+other-filtered scope.
        self.assertEqual(self._export_ids("?amount_min=not-a-number"), {"TXN-A", "TXN-B"})

    def test_dashboard_and_export_return_same_rows_for_same_filters(self):
        self._set_role("sam")
        with patch.object(app_module, "current_app_user_key", return_value=200), \
             patch.object(app_module.db, "get_dashboard_records", return_value=self._scoped()):
            dash_resp = self.client.get("/dashboard?urgent_only=1")
        dash_body = dash_resp.data.decode("utf-8")
        self.assertIn("TXN-A", dash_body)
        self.assertNotIn("TXN-B", dash_body)
        self.assertEqual(self._export_ids("?urgent_only=1"), {"TXN-A"})


if __name__ == "__main__":
    unittest.main()
