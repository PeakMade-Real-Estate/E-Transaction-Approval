"""
Tests for Batch 3: centralized banking-data masking, controlled server-side
reveal, mask-placeholder edit-safety, and reveal auditing.

Run:  python -m unittest test_banking_security -v
"""

import unittest
from unittest.mock import MagicMock, patch

import app as app_module
import banking_security
import db
import workflow


class MaskingTests(unittest.TestCase):
    def test_account_number_preserves_last4(self):
        self.assertEqual(banking_security.mask_account_number("1234567890"), "********7890")

    def test_routing_number_fully_masked_regardless_of_input(self):
        self.assertEqual(banking_security.mask_routing_number("021000021"), "*********")
        self.assertNotIn("1", banking_security.mask_routing_number("111111111"))

    def test_tax_id_fully_masked(self):
        self.assertEqual(banking_security.mask_tax_id("12-3456789"), "*********")

    def test_transit_and_institution_fully_masked(self):
        self.assertEqual(banking_security.mask_transit_number("00123"), "****")
        self.assertEqual(banking_security.mask_institution_number("001"), "***")

    def test_blank_or_none_value_still_masked_safely(self):
        self.assertEqual(banking_security.mask_account_number(""), "********")
        self.assertEqual(banking_security.mask_account_number(None), "********")

    def test_mask_field_dispatches_by_key(self):
        self.assertEqual(banking_security.mask_field("account_number", "1234567890"), "********7890")
        self.assertEqual(banking_security.mask_field("routing_number", "021000021"), "*********")


class MaskPlaceholderDetectionTests(unittest.TestCase):
    def test_18_ddm_partial_placeholder_rejected(self):
        self.assertTrue(banking_security.is_mask_placeholder("XXXX-XXXX-1234"))

    def test_19_ddm_full_placeholder_rejected(self):
        self.assertTrue(banking_security.is_mask_placeholder("xxxx"))

    def test_app_masked_placeholder_rejected(self):
        self.assertTrue(banking_security.is_mask_placeholder("********1234"))

    def test_bullet_masked_placeholder_rejected(self):
        self.assertTrue(banking_security.is_mask_placeholder("\u2022\u2022\u2022\u20221234"))

    def test_20_genuine_numeric_value_with_leading_zeros_accepted(self):
        # Preserve-as-string requirement: '001234567' must not be treated as a
        # placeholder just because it has leading zeros.
        self.assertFalse(banking_security.is_mask_placeholder("001234567"))

    def test_genuine_numeric_value_accepted(self):
        self.assertFalse(banking_security.is_mask_placeholder("4521039812"))

    def test_blank_is_not_a_placeholder(self):
        self.assertFalse(banking_security.is_mask_placeholder(""))
        self.assertFalse(banking_security.is_mask_placeholder(None))


class AuditTests(unittest.TestCase):
    def test_12_audit_never_logs_a_sensitive_value(self):
        # The function signature has no "value" parameter at all — it is
        # structurally impossible to pass a revealed value into the audit log.
        with self.assertLogs("banking_security", level="WARNING") as cm:
            banking_security.audit_banking_data_reveal(
                actor_user_key=1, actor_role="treasury", object_type="BankAccount",
                object_key=42, field="account_number", success=True,
            )
        self.assertEqual(len(cm.output), 1)
        self.assertIn("BANKING_DATA_REVEAL", cm.output[0])
        self.assertIn("user_key=1", cm.output[0])
        self.assertIn("success=True", cm.output[0])


class BankAccountRevealRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def _set_role(self, role):
        with self.client.session_transaction() as sess:
            sess["role"] = role

    def test_6_unauthorized_reveal_returns_403(self):
        self._set_role("submitter")  # cannot view Bank Account Management at all
        resp = self.client.post("/bank-accounts/1/reveal", data={"field": "account_number"})
        self.assertEqual(resp.status_code, 403)

    def test_vp_view_only_can_still_reveal(self):
        # Per project decision: reveal is tied to VIEW access, not EDIT access.
        self._set_role("vp")
        with patch.object(app_module.db, "get_bank_account_sensitive_field", return_value="9876543210"):
            resp = self.client.post("/bank-accounts/1/reveal", data={"field": "account_number"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["success"])

    def test_10_11_unknown_field_name_rejected(self):
        self._set_role("treasury")
        resp = self.client.post("/bank-accounts/1/reveal", data={"field": "ssn"})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.get_json()["success"])

    def test_8_authorized_reveal_with_real_value_returns_only_that_value(self):
        self._set_role("treasury")
        with patch.object(app_module.db, "get_bank_account_sensitive_field", return_value="9876543210") as mock_fn:
            resp = self.client.post("/bank-accounts/1/reveal", data={"field": "account_number"})
        payload = resp.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["value"], "9876543210")
        self.assertEqual(set(payload.keys()), {"success", "value"})
        mock_fn.assert_called_once_with(1, "AccountNumber")

    def test_9_ddm_masked_value_reports_unavailable_not_fake_success(self):
        self._set_role("treasury")
        with patch.object(app_module.db, "get_bank_account_sensitive_field", return_value="XXXX-XXXX-1234"):
            resp = self.client.post("/bank-accounts/1/reveal", data={"field": "account_number"})
        payload = resp.get_json()
        self.assertFalse(payload["success"])
        self.assertIn("not currently available", payload["reason"])
        self.assertNotIn("value", payload)

    def test_not_found_account_returns_404(self):
        self._set_role("treasury")
        with patch.object(app_module.db, "get_bank_account_sensitive_field", return_value=None):
            resp = self.client.post("/bank-accounts/999999/reveal", data={"field": "account_number"})
        self.assertEqual(resp.status_code, 404)


class TransactionRevealRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def _set_role(self, role):
        with self.client.session_transaction() as sess:
            sess["role"] = role

    def test_originating_side_resolves_bank_account_key_server_side(self):
        self._set_role("treasury")
        with patch.object(app_module.db, "get_transaction_for_workflow",
                           return_value={"status": workflow.STATUS_READY_FOR_TREASURY, "accounting_group_key": None}):
            with patch.object(app_module.db, "get_transaction_banking_keys",
                               return_value={"originating_bank_account_key": 7, "beneficiary_instruction_key": 8}):
                with patch.object(app_module.db, "get_bank_account_sensitive_field", return_value="4521039812") as mock_fn:
                    resp = self.client.post(
                        "/dashboard/request/TXN-2026-001/reveal-banking/originating",
                        data={"field": "account_number"},
                    )
        payload = resp.get_json()
        self.assertTrue(payload["success"])
        mock_fn.assert_called_once_with(7, "AccountNumber")

    def test_receiving_side_uses_beneficiary_instruction_query(self):
        self._set_role("treasury")
        with patch.object(app_module.db, "get_transaction_for_workflow",
                           return_value={"status": workflow.STATUS_READY_FOR_TREASURY, "accounting_group_key": None}):
            with patch.object(app_module.db, "get_transaction_banking_keys",
                               return_value={"originating_bank_account_key": 7, "beneficiary_instruction_key": 8}):
                with patch.object(app_module.db, "get_beneficiary_instruction_sensitive_field", return_value="11223344") as mock_fn:
                    resp = self.client.post(
                        "/dashboard/request/TXN-2026-101/reveal-banking/receiving",
                        data={"field": "account_number"},
                    )
        payload = resp.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["value"], "11223344")
        mock_fn.assert_called_once_with(8, "Receiving_Account_Number")

    def test_9_ddm_masked_receiving_value_reports_unavailable(self):
        self._set_role("treasury")
        with patch.object(app_module.db, "get_transaction_for_workflow",
                           return_value={"status": workflow.STATUS_READY_FOR_TREASURY, "accounting_group_key": None}):
            with patch.object(app_module.db, "get_transaction_banking_keys",
                               return_value={"originating_bank_account_key": 7, "beneficiary_instruction_key": 8}):
                with patch.object(app_module.db, "get_beneficiary_instruction_sensitive_field", return_value="xxxx"):
                    resp = self.client.post(
                        "/dashboard/request/TXN-2026-101/reveal-banking/receiving",
                        data={"field": "routing_number"},
                    )
        payload = resp.get_json()
        self.assertFalse(payload["success"])

    def test_unknown_side_rejected(self):
        self._set_role("treasury")
        resp = self.client.post("/dashboard/request/TXN-2026-001/reveal-banking/bogus",
                                 data={"field": "account_number"})
        self.assertEqual(resp.status_code, 400)

    def test_field_outside_orig_recv_allowlist_rejected(self):
        self._set_role("treasury")
        resp = self.client.post("/dashboard/request/TXN-2026-001/reveal-banking/originating",
                                 data={"field": "tax_id"})
        self.assertEqual(resp.status_code, 400)

    def test_unknown_transaction_returns_404(self):
        self._set_role("treasury")
        with patch.object(app_module.db, "get_transaction_for_workflow", return_value=None):
            resp = self.client.post("/dashboard/request/BOGUS-ID/reveal-banking/originating",
                                     data={"field": "account_number"})
        self.assertEqual(resp.status_code, 404)

    def test_no_authenticated_role_is_blocked(self):
        # No session role at all -> global require_role() redirects (dev bypass) or 403s.
        resp = self.client.post("/dashboard/request/TXN-2026-001/reveal-banking/originating",
                                 data={"field": "account_number"}, follow_redirects=False)
        self.assertIn(resp.status_code, (302, 403))


class TransactionDetailNoRawValueTests(unittest.TestCase):
    """Part 3/4/5: the normal page response must contain only masked values."""

    def _fake_record(self):
        return {
            "request_id": "TXN-2026-999", "status": "Pending Approver", "property_dept": "Test Prop",
            "property_code": "", "request_type": "ACH", "treasury_service_date": "2026-01-01",
            "prepared_date": "2026-01-01", "submitted_date": "2026-01-01", "amount": 1000.0,
            "currency": "USD", "payment_purpose": "Test", "urgent": False, "urgency_reason": "",
            "current_workflow_stage": "Approver", "approval_tier": "Senior Accounting Manager / Assistant Controller",
            "requires_vp": False, "over_1m": False, "days_pending": 0,
            "prepared_by": "A", "assigned_approver": "B", "approver": "B", "controller": "C",
            "vp_approver": "", "cfo_approver": "",
            "orig_bank_name": "Wells Fargo", "orig_account_name": "Ops",
            "orig_account_number": "4521039812", "orig_routing_number": "121000248",
            "orig_bank_contact": "", "notes_orig": "",
            "recv_payee_name": "Acme", "recv_contact_name": "", "recv_contact_email": "", "recv_contact_phone": "",
            "recv_bank_name": "Chase", "recv_account_name": "Acme Ops",
            "recv_account_number": "9938271045", "recv_routing_number": "021000021",
            "recv_bank_address": "", "notes_recv": "",
            "verbal_confirmed": False, "verbal_confirmed_with": "", "verbal_contact_name": "",
            "verbal_confirm_datetime": "", "avs_score": "", "external_source": False, "internal_doc_not_used": False,
            "instructions_previously_used": False, "last_used_date": "",
            "entity_classification": "Corporate", "current_owner_user_key": 2, "bank_releaser_user_key": None,
            "docs_checklist": {}, "attachments": {}, "extra_attachments": [], "timeline": [], "comments": [],
        }

    def test_1_2_3_4_5_full_values_never_in_page_source(self):
        client = app_module.app.test_client()
        with client.session_transaction() as sess:
            sess["role"] = "sam"  # Approver — dev-bypass visibility matches "Pending Approver" status below

        with patch.object(app_module.db, "get_transaction_for_workflow",
                           return_value={"status": "Pending Approver", "accounting_group_key": None}):
            with patch.object(app_module.db, "get_request_detail", return_value=self._fake_record()):
                with patch.object(app_module, "sharepoint_enabled", return_value=False):
                    resp = client.get("/dashboard/request/TXN-2026-999")

        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        for secret in ("4521039812", "121000248", "9938271045", "021000021"):
            self.assertNotIn(secret, body)
        self.assertIn("********9812", body)  # masked originating account, last4 preserved


class GetBankAccountRecordMaskingTests(unittest.TestCase):
    """Part 2/17: even a raw DDM-masked value gets application-masked on top (defense in depth)."""

    def test_masks_all_five_sensitive_fields(self):
        columns = [
            "BankAccount_Key", "Entity_Key", "AccountClassification", "BankName",
            "AccountNameID", "AccountTitle", "AccountTitleModifier", "SystemAccountName",
            "AccountNumber", "RoutingNumber", "TransitNumberCanada", "InstitutionNumberCanada",
            "GLAccountNumber", "GLAccountName", "TaxIDNumber", "Address", "PhoneNumber",
            "AccountType", "Status", "DateOpened", "DateClosed", "BankContactName", "Notes",
            *db.BANK_ACCOUNT_SERVICE_FLAGS,
        ]
        row = [None] * len(columns)
        row[0] = 1
        row[8] = "XXXX-XXXX-0123"   # AccountNumber (already DDM-masked)
        row[9] = "xxxx"             # RoutingNumber
        row[10] = "0012"            # TransitNumberCanada
        row[11] = "003"             # InstitutionNumberCanada
        row[14] = "xxxxxxxxx"       # TaxIDNumber
        row[18] = "Open"            # Status

        fake_cursor = MagicMock()
        fake_cursor.fetchone.return_value = tuple(row)
        fake_conn = MagicMock()
        fake_conn.cursor.return_value = fake_cursor

        with patch.object(db, "get_connection", return_value=fake_conn):
            record = db.get_bank_account_record(1)

        self.assertEqual(record["accountnumber"], "********0123")
        self.assertEqual(record["routingnumber"], "*********")
        self.assertEqual(record["taxidnumber"], "*********")
        self.assertEqual(record["transitnumbercanada"], "****")
        self.assertEqual(record["institutionnumbercanada"], "***")


class UpdateBankAccountSensitiveFieldTests(unittest.TestCase):
    def test_21_blank_sensitive_fields_excluded_from_update(self):
        fake_cursor = MagicMock()
        fake_conn = MagicMock()
        fake_conn.cursor.return_value = fake_cursor
        data = {
            "entity_key": "1", "account_classification": "Operating", "bank_name": "Wells Fargo",
            "account_name_id": "", "account_title": "", "account_title_modifier": "", "system_account_name": "",
            "gl_account_number": "", "gl_account_name": "", "address": "", "phone_number": "",
            "account_type": "Checking", "status": "Open", "date_opened": "2026-01-01", "date_closed": "",
            "bank_contact_name": "", "notes": "",
            "account_number": "", "routing_number": "", "transit_number_canada": "",
            "institution_number_canada": "", "tax_id_number": "",
        }
        for flag in db.BANK_ACCOUNT_SERVICE_FLAGS:
            data[flag] = False

        with patch.object(db, "get_connection", return_value=fake_conn):
            db.update_bank_account(1, data, actor_user_key=5)

        sql, params = fake_cursor.execute.call_args[0]
        # Use ", X = ?" (not a bare substring) so "GLAccountNumber = ?" (always
        # included, unrelated to sensitive-field blanking) doesn't false-match
        # "AccountNumber = ?".
        self.assertNotIn(", AccountNumber = ?", sql)
        self.assertNotIn(", RoutingNumber = ?", sql)
        self.assertNotIn(", TaxIDNumber = ?", sql)
