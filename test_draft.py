"""
Tests for Batch 8: Save Draft / Resume Draft / Submit Existing Draft.

Covers: draft creation/update persistence (db.py), draft ownership/status
authorization, Resume, final submission reusing the Batch 1 validator +
Batch 7 ApprovalRule resolution, WorkflowEvent behavior (no premature events;
same "Submitted"+APPROVER_ASSIGNED contract at final submit), concurrency
guarding, and regression of the brand-new one-sitting submission path.

Run:  python -m unittest test_draft -v
"""
import io
import unittest
from unittest.mock import MagicMock, patch

import app as app_module
import db
import workflow as wf


def _draft_form(**overrides):
    data = {
        "form_mode": "draft",
        "transaction_key": "",
        "request_id": "",
        "request_type": "ACH",
        "treasury_service_date": "2026-10-01",
        "property_dept": "Sunset Ridge Apartments",
        "approver_key": "2",
        "controller_key": "3",
        "amount": "1000.00",
        "currency": "USD",
        "payment_purpose": "Vendor payment",
        "bank_account_key": "5",
        "recv_payee_name": "Acme Vendor LLC",
        "recv_bank_name": "Chase Bank",
        "recv_account_number": "12345678",
        "recv_routing_number": "021000021",
    }
    data.update(overrides)
    return data


class DraftCreationDbTests(unittest.TestCase):
    """Part 1/9/27 #1-9: create_draft() persists a partial request as Draft,
    with no TransactionVerification/WorkflowAssignment/WorkflowEvent rows."""

    def _fake_conn(self, txn_key=99):
        fake_cursor = MagicMock()
        fake_cursor.fetchone.side_effect = [(1,), (10,), (20,), (txn_key,)]
        fake_conn = MagicMock()
        fake_conn.cursor.return_value = fake_cursor
        return fake_conn, fake_cursor

    def test_5_6_7_creates_no_verification_assignment_or_workflow_event(self):
        fake_conn, fake_cursor = self._fake_conn()
        data = {
            "prepared_by_key": 11, "request_type": "ACH", "treasury_service_date": "2026-10-01",
            "amount": 1000.0, "bank_account_key": 5, "approver_key": 2, "controller_key": 3,
            "recv_payee_name": "Payee", "recv_bank_name": "Bank", "recv_account_number": "123",
            "request_id": "TXN-2026-D001",
        }
        with patch.object(db, "get_connection", return_value=fake_conn):
            request_id, transaction_key = db.create_draft(data)

        self.assertEqual(request_id, "TXN-2026-D001")
        self.assertEqual(transaction_key, 99)
        all_sql = " ".join(c[0][0] for c in fake_cursor.execute.call_args_list)
        self.assertNotIn("TransactionVerification", all_sql)
        self.assertNotIn("WorkflowAssignment", all_sql)
        self.assertNotIn("WorkflowEvent", all_sql)

    def test_4_draft_status_and_stage_are_draft(self):
        fake_conn, fake_cursor = self._fake_conn()
        data = {
            "prepared_by_key": 11, "request_type": "ACH", "treasury_service_date": "2026-10-01",
            "amount": 1000.0, "bank_account_key": 5, "approver_key": 2, "controller_key": 3,
            "recv_payee_name": "Payee", "recv_bank_name": "Bank", "recv_account_number": "123",
            "request_id": "TXN-2026-D002",
        }
        with patch.object(db, "get_connection", return_value=fake_conn):
            db.create_draft(data)

        etxn_sql, etxn_params = next(
            c[0] for c in fake_cursor.execute.call_args_list if "INSERT INTO [etransactions].[ETransaction]" in c[0][0]
        )
        self.assertIn(wf.STATUS_DRAFT, etxn_params)

    def test_8_9_prepared_by_comes_from_caller_supplied_authenticated_key(self):
        fake_conn, fake_cursor = self._fake_conn()
        data = {
            "prepared_by_key": 11, "request_type": "ACH", "treasury_service_date": "2026-10-01",
            "amount": 1000.0, "bank_account_key": 5, "approver_key": 2, "controller_key": 3,
            "recv_payee_name": "Payee", "recv_bank_name": "Bank", "recv_account_number": "123",
            "request_id": "TXN-2026-D003",
        }
        with patch.object(db, "get_connection", return_value=fake_conn):
            db.create_draft(data)
        etxn_sql, etxn_params = next(
            c[0] for c in fake_cursor.execute.call_args_list if "INSERT INTO [etransactions].[ETransaction]" in c[0][0]
        )
        self.assertIn(11, etxn_params)


class DraftUpdateDbTests(unittest.TestCase):
    """Part 8/9/27 #10-15: repeated Save Draft updates the SAME Transaction_Key,
    never inserts a new row; blank account/routing preserves the stored value."""

    def test_10_11_update_uses_update_statements_only(self):
        fake_cursor = MagicMock()
        fake_cursor.fetchone.return_value = (10, 20)  # ben_key, bi_key
        fake_cursor.rowcount = 1
        fake_conn = MagicMock()
        fake_conn.cursor.return_value = fake_cursor

        data = {
            "request_type": "ACH", "treasury_service_date": "2026-10-01", "amount": 2000.0,
            "bank_account_key": 5, "approver_key": 2, "controller_key": 3,
            "recv_payee_name": "Payee", "recv_bank_name": "Bank", "recv_account_number": "999",
        }
        with patch.object(db, "get_connection", return_value=fake_conn):
            db.update_draft(99, data, prepared_by_user_key=11)

        all_sql = " ".join(c[0][0] for c in fake_cursor.execute.call_args_list)
        self.assertNotIn("INSERT INTO", all_sql)
        self.assertIn("UPDATE", all_sql)

    def test_12_request_id_unaffected_by_update(self):
        # update_draft() never touches Request_ID at all.
        fake_cursor = MagicMock()
        fake_cursor.fetchone.return_value = (10, 20)
        fake_cursor.rowcount = 1
        fake_conn = MagicMock()
        fake_conn.cursor.return_value = fake_cursor
        data = {
            "request_type": "ACH", "treasury_service_date": "2026-10-01", "amount": 2000.0,
            "bank_account_key": 5, "approver_key": 2, "controller_key": 3,
            "recv_payee_name": "Payee", "recv_bank_name": "Bank", "recv_account_number": "999",
        }
        with patch.object(db, "get_connection", return_value=fake_conn):
            db.update_draft(99, data, prepared_by_user_key=11)
        all_sql = " ".join(c[0][0] for c in fake_cursor.execute.call_args_list)
        self.assertNotIn("Request_ID", all_sql)

    def test_14_blank_account_number_preserves_existing_value(self):
        fake_cursor = MagicMock()
        fake_cursor.fetchone.return_value = (10, 20)
        fake_cursor.rowcount = 1
        fake_conn = MagicMock()
        fake_conn.cursor.return_value = fake_cursor
        data = {
            "request_type": "ACH", "treasury_service_date": "2026-10-01", "amount": 2000.0,
            "bank_account_key": 5, "approver_key": 2, "controller_key": 3,
            "recv_payee_name": "Payee", "recv_bank_name": "Bank",
            "recv_account_number": "", "recv_routing_number": "",  # left blank on purpose
        }
        with patch.object(db, "get_connection", return_value=fake_conn):
            db.update_draft(99, data, prepared_by_user_key=11)
        bi_sql, bi_params = next(
            c[0] for c in fake_cursor.execute.call_args_list if "BeneficiaryBankInstruction" in c[0][0] and "UPDATE" in c[0][0]
        )
        self.assertNotIn("Receiving_Account_Number = ?", bi_sql)
        self.assertNotIn("Receiving_Routing_Number = ?", bi_sql)

    def test_not_owned_or_not_draft_raises(self):
        fake_cursor = MagicMock()
        fake_cursor.fetchone.return_value = None  # guard SELECT found nothing
        fake_conn = MagicMock()
        fake_conn.cursor.return_value = fake_cursor
        data = {"request_type": "ACH", "treasury_service_date": "2026-10-01", "amount": 1.0,
                "bank_account_key": 5, "approver_key": 2, "controller_key": 3,
                "recv_payee_name": "P", "recv_bank_name": "B", "recv_account_number": "1"}
        with patch.object(db, "get_connection", return_value=fake_conn):
            with self.assertRaises(db.DraftNotEditableError):
                db.update_draft(99, data, prepared_by_user_key=11)


class DraftFinalizeDbTests(unittest.TestCase):
    """Part 15/16/17/23/27 #29-39: finalize_draft_submission() reuses the same
    Transaction_Key/Request_ID, writes the established Submitted/APPROVER_ASSIGNED
    events, and guards against double-submit."""

    def _finalize_data(self, **overrides):
        data = {
            "approver_key": 2, "controller_key": 3, "bank_account_key": 5,
            "request_type": "ACH", "property_dept": "", "property_code": "",
            "treasury_service_date": "2026-10-01", "amount": 1000.0, "currency": "USD",
            "payment_purpose": "Vendor payment", "urgent": False, "urgency_reason": "",
            "recv_payee_name": "Payee", "recv_bank_name": "Bank", "recv_account_name": "",
            "recv_account_number": "12345678", "recv_routing_number": "021000021", "recv_bank_address": "",
            "recv_contact_name": "", "recv_contact_email": "", "recv_contact_phone": "",
            "approval_rule_key": 7, "requires_vp": False, "requires_cfo": False,
            "approval_tier": "Senior Accounting Manager / Assistant Controller",
        }
        data.update(overrides)
        return data

    def test_33_34_35_writes_submitted_approver_assigned_and_assignment(self):
        fake_cursor = MagicMock()
        fake_cursor.fetchone.side_effect = [(10, 20), (77,)]  # guard SELECT, WorkflowAssignment.Assignment_Key
        fake_cursor.rowcount = 1
        fake_conn = MagicMock()
        fake_conn.cursor.return_value = fake_cursor

        with patch.object(db, "get_connection", return_value=fake_conn):
            db.finalize_draft_submission(99, self._finalize_data(), prepared_by_user_key=11)

        event_calls = [c for c in fake_cursor.execute.call_args_list if "INSERT INTO [etransactions].[WorkflowEvent]" in c[0][0]]
        self.assertEqual(len(event_calls), 1 + 1)  # Submitted + APPROVER_ASSIGNED... exactly 2
        self.assertEqual(len(event_calls), 2)
        self.assertIn("Submitted", event_calls[0][0][1])
        self.assertIn(wf.EVENT_APPROVER_ASSIGNED, event_calls[1][0][1])

        assignment_calls = [c for c in fake_cursor.execute.call_args_list if "INSERT INTO [etransactions].[WorkflowAssignment]" in c[0][0]]
        self.assertEqual(len(assignment_calls), 1)

        etxn_sql, etxn_params = next(
            c[0] for c in fake_cursor.execute.call_args_list if "UPDATE [etransactions].[ETransaction]" in c[0][0]
        )
        self.assertIn(wf.STATUS_PENDING_APPROVER, etxn_params)

    def test_32_status_stage_owner_transition(self):
        fake_cursor = MagicMock()
        fake_cursor.fetchone.side_effect = [(10, 20), (77,)]
        fake_cursor.rowcount = 1
        fake_conn = MagicMock()
        fake_conn.cursor.return_value = fake_cursor
        with patch.object(db, "get_connection", return_value=fake_conn):
            db.finalize_draft_submission(99, self._finalize_data(), prepared_by_user_key=11)
        etxn_sql, etxn_params = next(
            c[0] for c in fake_cursor.execute.call_args_list if "UPDATE [etransactions].[ETransaction]" in c[0][0]
        )
        self.assertIn("Current_Workflow_Stage = ?", etxn_sql)
        self.assertIn("CurrentOwner_User_Key = ?", etxn_sql)
        self.assertIn(2, etxn_params)  # approver becomes CurrentOwner

    def test_29_30_double_submit_second_call_raises_workflow_conflict(self):
        fake_cursor = MagicMock()
        fake_cursor.fetchone.return_value = None  # second call: no longer a Draft
        fake_conn = MagicMock()
        fake_conn.cursor.return_value = fake_cursor
        with patch.object(db, "get_connection", return_value=fake_conn):
            with self.assertRaises(db.WorkflowConflictError):
                db.finalize_draft_submission(99, self._finalize_data(), prepared_by_user_key=11)
        # No WorkflowEvent/WorkflowAssignment written on the failed/duplicate call.
        all_sql = " ".join(c[0][0] for c in fake_cursor.execute.call_args_list)
        self.assertNotIn("WorkflowEvent", all_sql)
        self.assertNotIn("WorkflowAssignment", all_sql)

    def test_31_no_completed_or_other_extra_workflow_event(self):
        fake_cursor = MagicMock()
        fake_cursor.fetchone.side_effect = [(10, 20), (77,)]
        fake_cursor.rowcount = 1
        fake_conn = MagicMock()
        fake_conn.cursor.return_value = fake_cursor
        with patch.object(db, "get_connection", return_value=fake_conn):
            db.finalize_draft_submission(99, self._finalize_data(), prepared_by_user_key=11)
        event_calls = [c for c in fake_cursor.execute.call_args_list if "INSERT INTO [etransactions].[WorkflowEvent]" in c[0][0]]
        written_types = [c[0][1][3] for c in event_calls]  # Event_Type is the 4th bound param
        self.assertNotIn("DRAFT_SAVED", written_types)
        self.assertNotIn("COMPLETED", written_types)


class DraftAuthorizationTests(unittest.TestCase):
    """Part 3/14/27 #16-20: ownership + Current_Status='Draft' enforced server-side."""

    def setUp(self):
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as sess:
            sess["role"] = "submitter"

    def test_16_owner_can_access_own_draft(self):
        draft = {"transaction_key": 5, "request_id": "TXN-2026-D010", "property_dept": "", "property_code": "",
                  "bank_account_key": 5, "approver_key": 2, "controller_key": 3, "request_type": "ACH",
                  "treasury_service_date": "2026-10-01", "amount": 1000.0, "currency": "USD",
                  "payment_purpose": "", "urgent": False, "urgency_reason": "", "prepared_date": "2026-09-18",
                  "recv_payee_name": "P", "recv_contact_name": "", "recv_contact_email": "", "recv_contact_phone": "",
                  "recv_bank_name": "B", "recv_account_name": "", "recv_bank_address": "",
                  "recv_account_number_masked": "********5678", "recv_routing_number_masked": "*********"}
        with patch.object(app_module, "current_app_user", return_value={"user_key": 11, "display_name": "Owner", "email": ""}), \
             patch.object(app_module.db, "get_draft_for_edit", return_value=draft) as mock_get, \
             patch.object(app_module.db, "get_user_list", return_value=[]), \
             patch.object(app_module.db, "get_bank_accounts", return_value=[]), \
             patch.object(app_module.sharepoint, "list_attachments", return_value=[]):
            resp = self.client.get("/intake/draft/5")
        self.assertEqual(resp.status_code, 200)
        mock_get.assert_called_once_with(5, prepared_by_user_key=11)

    def test_17_18_other_users_draft_is_denied(self):
        with patch.object(app_module, "current_app_user", return_value={"user_key": 999, "display_name": "Other", "email": ""}), \
             patch.object(app_module.db, "get_draft_for_edit", return_value=None):
            resp = self.client.get("/intake/draft/5", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)  # redirected to dashboard, not the draft

    def test_19_20_submitted_or_completed_transaction_not_editable_via_draft_route(self):
        # get_draft_for_edit() itself enforces Current_Status='Draft' in its WHERE
        # clause — any non-Draft status returns None regardless of ownership.
        with patch.object(app_module, "current_app_user", return_value={"user_key": 11, "display_name": "Owner", "email": ""}), \
             patch.object(app_module.db, "get_draft_for_edit", return_value=None) as mock_get:
            resp = self.client.get("/intake/draft/5", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        mock_get.assert_called_once_with(5, prepared_by_user_key=11)


class DraftDashboardVisibilityTests(unittest.TestCase):
    """Part 13/17: Drafts are scoped to the owning submitter only, never the
    Approver/Controller/VP/CFO queues."""

    def test_submitter_scope_includes_draft_status(self):
        where, params = db._dashboard_scope_where("submitter", 11, None)
        self.assertEqual(where, ["t.PreparedBy_User_Key = ?"])  # no status filter — Drafts included

    def test_approver_scope_excludes_draft_status(self):
        where, params = db._dashboard_scope_where("sam", 20, None)
        self.assertIn("t.Current_Status <> ?", where[0])
        self.assertIn(wf.STATUS_DRAFT, params)


class DraftVisibilityAuthorizationTests(unittest.TestCase):
    """Part 3/13: can_view_transaction() excludes Draft for non-owning roles
    even if a real Selected*_User_Key is already stored (schema-required)."""

    def test_approver_cannot_view_draft_even_if_listed_as_selected_approver(self):
        import authorization
        txn = {"status": wf.STATUS_DRAFT, "selected_approver_user_key": 20, "prepared_by_user_key": 11}
        self.assertFalse(authorization.can_view_transaction(role="sam", user_key=20, txn=txn))

    def test_submitter_can_view_own_draft(self):
        import authorization
        txn = {"status": wf.STATUS_DRAFT, "prepared_by_user_key": 11}
        self.assertTrue(authorization.can_view_transaction(role="submitter", user_key=11, txn=txn))

    def test_submitter_cannot_view_another_users_draft(self):
        import authorization
        txn = {"status": wf.STATUS_DRAFT, "prepared_by_user_key": 999}
        self.assertFalse(authorization.can_view_transaction(role="submitter", user_key=11, txn=txn))


class SaveDraftValidationTests(unittest.TestCase):
    """Part 6/27 #1-2, #40-42: draft saves allow incompleteness but reject
    structurally unsafe/invalid data; mask placeholders are never persisted."""

    def test_1_2_valid_partial_form_has_no_errors(self):
        errors = app_module._validate_draft_submission(_draft_form())
        self.assertEqual(errors, [])

    def test_can_omit_business_completeness_fields(self):
        # No urgency_reason, no currency override, no payment_purpose — all fine for a draft.
        form = _draft_form()
        form.pop("payment_purpose")
        errors = app_module._validate_draft_submission(form)
        self.assertEqual(errors, [])

    def test_41_mask_placeholder_rejected(self):
        form = _draft_form(recv_account_number="XXXX-XXXX-1234")
        errors = app_module._validate_draft_submission(form)
        self.assertTrue(any("masked placeholder" in e for e in errors))

    def test_unsupported_request_type_rejected(self):
        form = _draft_form(request_type="Bogus")
        errors = app_module._validate_draft_submission(form)
        self.assertTrue(any("Unsupported Request Type" in e for e in errors))

    def test_update_mode_allows_blank_account_number(self):
        form = _draft_form(recv_account_number="")
        errors = app_module._validate_draft_submission(form, is_update=True)
        self.assertEqual(errors, [])

    def test_new_draft_mode_requires_account_number(self):
        form = _draft_form(recv_account_number="")
        errors = app_module._validate_draft_submission(form, is_update=False)
        self.assertTrue(any("Receiving Account Number" in e for e in errors))


class SaveDraftRouteTests(unittest.TestCase):
    """Part 5/8/27: Save Draft is a distinct POST outcome — first save creates,
    subsequent saves update the same Transaction_Key."""

    def setUp(self):
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as sess:
            sess["role"] = "submitter"

    def test_3_first_save_draft_creates_new_draft(self):
        with patch.object(app_module, "current_app_user", return_value={"user_key": 11, "display_name": "U", "email": ""}), \
             patch.object(app_module.db, "get_bank_account_status", return_value="Open"), \
             patch.object(app_module.db, "get_app_user_by_key", return_value={"user_key": 2, "display_name": "A", "email": ""}), \
             patch.object(app_module.db, "get_user_list", return_value=[]), \
             patch.object(app_module.db, "get_bank_accounts", return_value=[]), \
             patch.object(app_module, "_upload_intake_attachments"), \
             patch.object(app_module.db, "create_draft", return_value=("TXN-2026-D100", 55)) as mock_create:
            resp = self.client.post("/intake/submit", data=_draft_form(), follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        mock_create.assert_called_once()

    def test_11_second_save_draft_updates_same_transaction_key(self):
        with patch.object(app_module, "current_app_user", return_value={"user_key": 11, "display_name": "U", "email": ""}), \
             patch.object(app_module.db, "get_bank_account_status", return_value="Open"), \
             patch.object(app_module.db, "get_app_user_by_key", return_value={"user_key": 2, "display_name": "A", "email": ""}), \
             patch.object(app_module.db, "get_user_list", return_value=[]), \
             patch.object(app_module.db, "get_bank_accounts", return_value=[]), \
             patch.object(app_module, "_upload_intake_attachments"), \
             patch.object(app_module.db, "create_draft") as mock_create, \
             patch.object(app_module.db, "update_draft") as mock_update:
            resp = self.client.post(
                "/intake/submit",
                data=_draft_form(transaction_key="55", request_id="TXN-2026-D100"),
                follow_redirects=False,
            )
        self.assertEqual(resp.status_code, 302)
        mock_create.assert_not_called()
        mock_update.assert_called_once()
        self.assertEqual(mock_update.call_args[0][0], 55)


class FinalizeDraftRouteTests(unittest.TestCase):
    """Part 15/25/27 #25-28: finalizing a Draft reuses the full Batch 1
    validator and Batch 7 rule resolution; incomplete data is rejected exactly
    the same way as a brand-new submission."""

    def setUp(self):
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as sess:
            sess["role"] = "submitter"

    def _full_form(self, **overrides):
        data = {
            "form_mode": "submit",
            "transaction_key": "55",
            "request_id": "TXN-2026-D100",
            "request_type": "ACH",
            "treasury_service_date": "2026-10-01",
            "property_dept": "Sunset Ridge",
            "approver_key": "2", "controller_key": "3",
            "amount": "1000.00", "currency": "USD",
            "payment_purpose": "Vendor payment",
            "urgent": "no",
            "bank_account_key": "5",
            "recv_payee_name": "Acme Vendor LLC", "recv_bank_name": "Chase Bank",
            "recv_account_name": "Acme Ops",
            "recv_account_number": "12345678", "recv_routing_number": "021000021",
            "verbal_confirmed_with_known": "on", "verbal_contact_name": "John Doe",
            "verbal_confirm_datetime": "2026-09-18T10:00",
            "avs_score": "95",
        }
        data.update(overrides)
        return data

    def _files(self):
        return {
            "file_validation_evidence": (io.BytesIO(b"x"), "v.pdf"),
            "file_wire_ach_instructions": (io.BytesIO(b"x"), "w.pdf"),
            "file_payment_support": (io.BytesIO(b"x"), "p.pdf"),
        }

    def test_25_26_27_incomplete_draft_fails_final_submit_and_writes_nothing(self):
        with patch.object(app_module, "current_app_user", return_value={"user_key": 11, "display_name": "U", "email": ""}), \
             patch.object(app_module.db, "get_bank_account_status", return_value="Open"), \
             patch.object(app_module.db, "get_user_list", return_value=[]), \
             patch.object(app_module.db, "get_bank_accounts", return_value=[]), \
             patch.object(app_module.db, "finalize_draft_submission") as mock_finalize:
            form = self._full_form(recv_account_name="")  # missing a required final-submit field
            data = dict(form)
            data.update(self._files())
            resp = self.client.post("/intake/submit", data=data, content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 200)  # re-renders the form with errors
        mock_finalize.assert_not_called()

    def test_28_29_30_complete_draft_passes_validation_and_reuses_same_ids(self):
        with patch.object(app_module, "current_app_user", return_value={"user_key": 11, "display_name": "U", "email": ""}), \
             patch.object(app_module.db, "get_bank_account_status", return_value="Open"), \
             patch.object(app_module.db, "find_prior_completed_beneficiary_match", return_value=None), \
             patch.object(app_module.db, "resolve_approval_rule",
                           return_value={"approval_rule_key": 7, "requires_approver": True,
                                         "requires_controller": True, "requires_vp": False, "requires_cfo": False}), \
             patch.object(app_module, "_upload_intake_attachments"), \
             patch.object(app_module.db, "finalize_draft_submission") as mock_finalize:
            data = dict(self._full_form())
            data.update(self._files())
            resp = self.client.post("/intake/submit", data=data, content_type="multipart/form-data",
                                     follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        mock_finalize.assert_called_once()
        args, kwargs = mock_finalize.call_args
        self.assertEqual(args[0], 55)  # same Transaction_Key
        self.assertEqual(kwargs["prepared_by_user_key"], 11)

    def test_conflict_on_finalize_redirects_to_dashboard(self):
        with patch.object(app_module, "current_app_user", return_value={"user_key": 11, "display_name": "U", "email": ""}), \
             patch.object(app_module.db, "get_bank_account_status", return_value="Open"), \
             patch.object(app_module.db, "find_prior_completed_beneficiary_match", return_value=None), \
             patch.object(app_module.db, "resolve_approval_rule",
                           return_value={"approval_rule_key": 7, "requires_approver": True,
                                         "requires_controller": True, "requires_vp": False, "requires_cfo": False}), \
             patch.object(app_module, "_upload_intake_attachments"), \
             patch.object(app_module.db, "finalize_draft_submission", side_effect=db.WorkflowConflictError("already submitted")):
            data = dict(self._full_form())
            data.update(self._files())
            resp = self.client.post("/intake/submit", data=data, content_type="multipart/form-data",
                                     follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/dashboard", resp.headers["Location"])


class BrandNewSubmissionRegressionTests(unittest.TestCase):
    """Part 27 #43: normal one-sitting intake (no Draft involved at all) still works."""

    def setUp(self):
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as sess:
            sess["role"] = "submitter"

    def test_one_sitting_submission_unaffected_by_draft_changes(self):
        data = {
            "form_mode": "submit",
            "transaction_key": "", "request_id": "",
            "request_type": "ACH", "treasury_service_date": "2026-10-01",
            "property_dept": "Sunset Ridge",
            "approver_key": "2", "controller_key": "3",
            "amount": "1000.00", "currency": "USD",
            "payment_purpose": "Vendor payment", "urgent": "no",
            "bank_account_key": "5",
            "recv_payee_name": "Acme Vendor LLC", "recv_bank_name": "Chase Bank",
            "recv_account_name": "Acme Ops",
            "recv_account_number": "12345678", "recv_routing_number": "021000021",
            "verbal_confirmed_with_known": "on", "verbal_contact_name": "John Doe",
            "verbal_confirm_datetime": "2026-09-18T10:00",
            "avs_score": "95",
            "file_validation_evidence": (io.BytesIO(b"x"), "v.pdf"),
            "file_wire_ach_instructions": (io.BytesIO(b"x"), "w.pdf"),
            "file_payment_support": (io.BytesIO(b"x"), "p.pdf"),
        }
        with patch.object(app_module, "current_app_user", return_value={"user_key": 11, "display_name": "U", "email": ""}), \
             patch.object(app_module.db, "get_bank_account_status", return_value="Open"), \
             patch.object(app_module.db, "find_prior_completed_beneficiary_match", return_value=None), \
             patch.object(app_module.db, "resolve_approval_rule",
                           return_value={"approval_rule_key": 7, "requires_approver": True,
                                         "requires_controller": True, "requires_vp": False, "requires_cfo": False}), \
             patch.object(app_module.db, "reserve_request_id", return_value="TXN-2026-9999"), \
             patch.object(app_module, "_upload_required_intake_attachments"), \
             patch.object(app_module.db, "insert_transaction", return_value="TXN-2026-9999") as mock_insert, \
             patch.object(app_module.db, "finalize_draft_submission") as mock_finalize:
            resp = self.client.post("/intake/submit", data=data, content_type="multipart/form-data",
                                     follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        mock_insert.assert_called_once()
        mock_finalize.assert_not_called()


class ResumeFieldCompletenessTests(unittest.TestCase):
    """
    Batch 8 follow-up (not Batch 9): every persisted optional Draft field
    round-trips correctly on Resume (contact fields, urgency reason/flag),
    the originating bank account is shown as a safe masked summary (never a
    full account number), previously-uploaded attachments are recognized
    without requiring re-upload, and Prepared_Date is never reset by an
    update/finalize.
    """

    def setUp(self):
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as sess:
            sess["role"] = "submitter"

    def _resume_draft(self, **overrides):
        draft = {
            "transaction_key": 5, "request_id": "TXN-2026-D010", "property_dept": "", "property_code": "",
            "bank_account_key": 5, "approver_key": 2, "controller_key": 3, "request_type": "ACH",
            "treasury_service_date": "2026-10-01", "amount": 1000.0, "currency": "USD",
            "payment_purpose": "", "urgent": True, "urgency_reason": "Vendor deadline today",
            "prepared_date": "2026-09-18",
            "recv_payee_name": "P",
            "recv_contact_name": "Jane Contact", "recv_contact_email": "jane@example.com",
            "recv_contact_phone": "555-000-1111",
            "recv_bank_name": "B", "recv_account_name": "", "recv_bank_address": "",
            "recv_account_number_masked": "********5678", "recv_routing_number_masked": "*********",
            "originating_bank_account": {"bank_name": "Wells Fargo", "account_title": "Operating Account", "last4": "1234"},
        }
        draft.update(overrides)
        return draft

    def _render_resume(self, draft, existing_attachments=None):
        with patch.object(app_module, "current_app_user", return_value={"user_key": 11, "display_name": "Owner", "email": ""}), \
             patch.object(app_module.db, "get_draft_for_edit", return_value=draft), \
             patch.object(app_module.db, "get_user_list", return_value=[]), \
             patch.object(app_module.db, "get_bank_accounts", return_value=[{"bank_account_key": 5, "bank_name": "Wells Fargo"}]), \
             patch.object(app_module.sharepoint, "list_attachments", return_value=existing_attachments or []):
            return self.client.get("/intake/draft/5")

    def test_resume_page_prefills_urgency_reason_and_contact_fields(self):
        resp = self._render_resume(self._resume_draft())
        body = resp.get_data(as_text=True)
        self.assertIn("Vendor deadline today", body)
        self.assertIn("Jane Contact", body)
        self.assertIn("jane@example.com", body)
        self.assertIn("555-000-1111", body)

    def test_resume_page_checks_urgent_yes_radio_when_draft_urgent(self):
        resp = self._render_resume(self._resume_draft(urgent=True))
        body = resp.get_data(as_text=True)
        # The urgent_yes radio input must carry `checked` when resuming an urgent draft.
        idx = body.find('id="urgent_yes"')
        self.assertNotEqual(idx, -1)
        # `checked` should appear on the same <input> tag (search a small window forward).
        tag_end = body.find(">", idx)
        self.assertIn("checked", body[idx:tag_end])

    def test_resume_page_does_not_check_urgent_yes_when_draft_not_urgent(self):
        resp = self._render_resume(self._resume_draft(urgent=False, urgency_reason=""))
        body = resp.get_data(as_text=True)
        idx = body.find('id="urgent_yes"')
        tag_end = body.find(">", idx)
        self.assertNotIn("checked", body[idx:tag_end])

    def test_resume_page_shows_originating_bank_friendly_summary(self):
        resp = self._render_resume(self._resume_draft())
        body = resp.get_data(as_text=True)
        self.assertIn("Wells Fargo", body)
        self.assertIn("Operating Account", body)
        self.assertIn("1234", body)

    def test_resume_page_never_leaks_a_full_account_or_routing_number(self):
        resp = self._render_resume(self._resume_draft())
        body = resp.get_data(as_text=True)
        # The only real banking data ever passed to the template is already
        # masked (recv_*_masked, originating_bank_account.last4) — a raw,
        # unmasked full account/routing number must never appear.
        self.assertNotIn("12345678", body)
        self.assertNotIn("021000021", body)

    def test_resume_page_shows_existing_attachment_without_requiring_reupload(self):
        resp = self._render_resume(
            self._resume_draft(),
            existing_attachments=[
                {"doc_type": app_module.sharepoint.DOC_TYPE_VALIDATION_EVIDENCE, "filename": "avs_screenshot.png"},
                {"doc_type": app_module.sharepoint.DOC_TYPE_WIRE_ACH_INSTRUCTIONS, "filename": "wire_instructions.pdf"},
                {"doc_type": app_module.sharepoint.DOC_TYPE_PAYMENT_SUPPORT, "filename": "invoice.pdf"},
            ],
        )
        body = resp.get_data(as_text=True)
        self.assertIn("avs_screenshot.png", body)
        self.assertIn("wire_instructions.pdf", body)
        self.assertIn("invoice.pdf", body)
        # File inputs must remain optional on resume — never re-force `required`.
        self.assertNotIn('id="file_validation_evidence" required', body)

    def test_resume_page_survives_sharepoint_being_unavailable(self):
        # A live SharePoint/Graph failure must not break Resume — the route
        # catches and logs, showing no "already uploaded" indicators instead.
        with patch.object(app_module, "current_app_user", return_value={"user_key": 11, "display_name": "Owner", "email": ""}), \
             patch.object(app_module.db, "get_draft_for_edit", return_value=self._resume_draft()), \
             patch.object(app_module.db, "get_user_list", return_value=[]), \
             patch.object(app_module.db, "get_bank_accounts", return_value=[]), \
             patch.object(app_module.sharepoint, "list_attachments", side_effect=RuntimeError("Graph error")):
            resp = self.client.get("/intake/draft/5")
        self.assertEqual(resp.status_code, 200)


class OriginatingBankAccountSummaryDbTests(unittest.TestCase):
    """db._draft_originating_bank_account_summary() — only bank name, account
    title, and last 4 digits are ever surfaced; never a full account number."""

    def test_summary_uses_masked_account_number_last4_only(self):
        fake_record = {
            "bankname": "Wells Fargo", "accounttitle": "Operating Account",
            "accountnameid": "OPS-001", "systemaccountname": "",
            "accountnumber": "********1234",
        }
        with patch.object(db, "get_bank_account_record", return_value=fake_record):
            summary = db._draft_originating_bank_account_summary(5)
        self.assertEqual(summary, {"bank_name": "Wells Fargo", "account_title": "Operating Account", "last4": "1234"})

    def test_summary_none_when_no_bank_account_key(self):
        self.assertIsNone(db._draft_originating_bank_account_summary(None))

    def test_summary_none_when_bank_account_not_found(self):
        with patch.object(db, "get_bank_account_record", return_value=None):
            self.assertIsNone(db._draft_originating_bank_account_summary(5))

    def test_summary_falls_back_to_account_name_id_when_no_title(self):
        fake_record = {
            "bankname": "Chase", "accounttitle": "", "accountnameid": "OPS-002",
            "systemaccountname": "", "accountnumber": "********9999",
        }
        with patch.object(db, "get_bank_account_record", return_value=fake_record):
            summary = db._draft_originating_bank_account_summary(5)
        self.assertEqual(summary["account_title"], "OPS-002")


class DraftBankAccountKeyPersistenceRouteTests(unittest.TestCase):
    """Save Draft preserves an unchanged originating bank account, and
    correctly updates it when the requester picks a different one."""

    def setUp(self):
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as sess:
            sess["role"] = "submitter"

    def test_unchanged_bank_account_key_preserved_on_second_save(self):
        with patch.object(app_module, "current_app_user", return_value={"user_key": 11, "display_name": "U", "email": ""}), \
             patch.object(app_module.db, "get_bank_account_status", return_value="Open"), \
             patch.object(app_module.db, "get_app_user_by_key", return_value={"user_key": 2, "display_name": "A", "email": ""}), \
             patch.object(app_module.db, "get_user_list", return_value=[]), \
             patch.object(app_module.db, "get_bank_accounts", return_value=[]), \
             patch.object(app_module, "_upload_intake_attachments"), \
             patch.object(app_module.db, "update_draft") as mock_update:
            self.client.post(
                "/intake/submit",
                data=_draft_form(transaction_key="55", request_id="TXN-2026-D100", bank_account_key="5"),
                follow_redirects=False,
            )
        mock_update.assert_called_once()
        self.assertEqual(mock_update.call_args[0][1]["bank_account_key"], 5)

    def test_changed_bank_account_key_is_updated(self):
        with patch.object(app_module, "current_app_user", return_value={"user_key": 11, "display_name": "U", "email": ""}), \
             patch.object(app_module.db, "get_bank_account_status", return_value="Open"), \
             patch.object(app_module.db, "get_app_user_by_key", return_value={"user_key": 2, "display_name": "A", "email": ""}), \
             patch.object(app_module.db, "get_user_list", return_value=[]), \
             patch.object(app_module.db, "get_bank_accounts", return_value=[]), \
             patch.object(app_module, "_upload_intake_attachments"), \
             patch.object(app_module.db, "update_draft") as mock_update:
            self.client.post(
                "/intake/submit",
                data=_draft_form(transaction_key="55", request_id="TXN-2026-D100", bank_account_key="9"),
                follow_redirects=False,
            )
        mock_update.assert_called_once()
        self.assertEqual(mock_update.call_args[0][1]["bank_account_key"], 9)


class PreparedDateNotResetTests(unittest.TestCase):
    """Part 3 (verified, no code change required): Prepared_Date is set once
    by create_draft() and never touched again by update_draft() or
    finalize_draft_submission()."""

    def test_update_draft_sql_never_touches_prepared_date(self):
        fake_cursor = MagicMock()
        fake_cursor.fetchone.return_value = (10, 20)
        fake_cursor.rowcount = 1
        fake_conn = MagicMock()
        fake_conn.cursor.return_value = fake_cursor
        data = {
            "request_type": "ACH", "treasury_service_date": "2026-10-01", "amount": 2000.0,
            "bank_account_key": 5, "approver_key": 2, "controller_key": 3,
            "recv_payee_name": "Payee", "recv_bank_name": "Bank", "recv_account_number": "999",
        }
        with patch.object(db, "get_connection", return_value=fake_conn):
            db.update_draft(99, data, prepared_by_user_key=11)
        all_sql = " ".join(c[0][0] for c in fake_cursor.execute.call_args_list)
        self.assertNotIn("Prepared_Date", all_sql)

    def test_finalize_draft_submission_sql_never_touches_prepared_date(self):
        fake_cursor = MagicMock()
        fake_cursor.fetchone.side_effect = [(10, 20), (77,)]
        fake_cursor.rowcount = 1
        fake_conn = MagicMock()
        fake_conn.cursor.return_value = fake_cursor
        data = {
            "approver_key": 2, "controller_key": 3, "bank_account_key": 5,
            "request_type": "ACH", "property_dept": "", "property_code": "",
            "treasury_service_date": "2026-10-01", "amount": 1000.0, "currency": "USD",
            "payment_purpose": "Vendor payment", "urgent": False, "urgency_reason": "",
            "recv_payee_name": "Payee", "recv_bank_name": "Bank", "recv_account_name": "",
            "recv_account_number": "12345678", "recv_routing_number": "021000021", "recv_bank_address": "",
            "recv_contact_name": "", "recv_contact_email": "", "recv_contact_phone": "",
            "approval_rule_key": 7, "requires_vp": False, "requires_cfo": False,
            "approval_tier": "Senior Accounting Manager / Assistant Controller",
        }
        with patch.object(db, "get_connection", return_value=fake_conn):
            db.finalize_draft_submission(99, data, prepared_by_user_key=11)
        all_sql = " ".join(c[0][0] for c in fake_cursor.execute.call_args_list)
        self.assertNotIn("Prepared_Date", all_sql)


if __name__ == "__main__":
    unittest.main()
