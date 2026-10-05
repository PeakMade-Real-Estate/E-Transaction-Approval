"""
Audit/regression tests for beneficiary "Previously Used Banking Instructions"
verification against the approved Version 1 requirements:

  - prior-use match is based ONLY on beneficiary/receiving identity (payee,
    receiving bank, receiving account number, receiving routing number) —
    the originating company bank account never participates;
  - only a transaction in Current_Status = Completed ever qualifies;
  - the server independently re-derives the match — selecting "Previously
    Used" alone can never bypass verification;
  - a Draft finalize that leaves recv_account_number/recv_routing_number
    blank (meaning "keep the already-stored value" — Batch 3) must check the
    match against the REAL stored value, never a blank form field;
  - the approver detail view can surface the matched prior Request_ID.

Run:  python -m unittest test_prior_use_verification -v
"""
import io
import unittest
from unittest.mock import MagicMock, patch

import app as app_module
import db
import workflow as wf


def _full_form(**overrides):
    data = {
        "form_mode": "submit",
        "transaction_key": "55",
        "request_id": "TXN-2026-D100",
        "request_type": "ACH",
        "treasury_service_date": "2026-10-01",
        "classification": "corporate",
        "property_dept": "Sunset Ridge",
        "approver_key": "2", "controller_key": "3",
        "amount": "1000.00", "currency": "USD",
        "payment_purpose": "Vendor payment",
        "urgent": "no",
        "bank_account_key": "5",
        "recv_payee_name": "Acme Vendor LLC", "recv_bank_name": "Chase Bank",
        "recv_account_name": "Acme Ops",
        "recv_account_number": "12345678", "recv_routing_number": "021000021",
        "instructions_previously_used": "yes",
        "last_used_date": "2026-01-01",
        "avs_score": "",
    }
    data.update(overrides)
    return data


def _files():
    return {
        "file_validation_evidence": (io.BytesIO(b"x"), "v.pdf"),
        "file_wire_ach_instructions": (io.BytesIO(b"x"), "w.pdf"),
        "file_payment_support": (io.BytesIO(b"x"), "p.pdf"),
    }


class MatchFieldsDoNotIncludeOriginatingAccountTests(unittest.TestCase):
    """Requirement: the match is on beneficiary/receiving identity only."""

    def test_query_never_references_originating_bank_account(self):
        fake_cursor = MagicMock()
        fake_cursor.fetchone.return_value = None
        fake_conn = MagicMock()
        fake_conn.cursor.return_value = fake_cursor
        with patch.object(db, "get_connection", return_value=fake_conn):
            db.find_prior_completed_beneficiary_match(
                payee_name="Acme Vendor LLC", receiving_bank_name="Chase Bank",
                receiving_account_number="12345678", receiving_routing_number="021000021",
            )
        executed_sql = fake_cursor.execute.call_args[0][0]
        self.assertNotIn("OriginatingBankAccount_Key", executed_sql)

    def test_only_completed_status_qualifies(self):
        fake_cursor = MagicMock()
        fake_cursor.fetchone.return_value = None
        fake_conn = MagicMock()
        fake_conn.cursor.return_value = fake_cursor
        with patch.object(db, "get_connection", return_value=fake_conn):
            db.find_prior_completed_beneficiary_match(
                payee_name="Acme", receiving_bank_name="Chase",
                receiving_account_number="1", receiving_routing_number="2",
            )
        executed_sql, executed_params = fake_cursor.execute.call_args[0]
        self.assertIn("t.Current_Status = ?", executed_sql)
        self.assertEqual(executed_params[0], wf.STATUS_COMPLETED)


class ServerIndependentlyValidatesClaimTests(unittest.TestCase):
    """The checkbox alone must never bypass verification."""

    def setUp(self):
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as sess:
            sess["role"] = "submitter"

    def _patches(self, prior_match):
        return [
            patch.object(app_module, "current_app_user",
                         return_value={"user_key": 11, "display_name": "U", "email": ""}),
            patch.object(app_module.db, "get_bank_account_status", return_value="Open"),
            patch.object(app_module.db, "get_app_user_role_codes", return_value=["sam", "controller"]),
            patch.object(app_module.db, "find_prior_completed_beneficiary_match", return_value=prior_match),
            patch.object(app_module.db, "resolve_approval_rule",
                         return_value={"approval_rule_key": 7, "requires_approver": True,
                                       "requires_controller": True, "requires_vp": False, "requires_cfo": False}),
            patch.object(app_module.db, "reserve_request_id", return_value="TXN-2026-9999"),
            patch.object(app_module, "_upload_required_intake_attachments"),
            patch.object(app_module.db, "insert_transaction", return_value="TXN-2026-9999"),
        ]

    def _post(self, form, prior_match):
        patches = self._patches(prior_match)
        for p in patches:
            p.start()
        try:
            data = dict(form)
            data.update(_files())
            return self.client.post("/intake/submit", data=data, content_type="multipart/form-data",
                                     follow_redirects=False)
        finally:
            for p in patches:
                p.stop()

    def test_claimed_previously_used_but_no_match_is_blocked(self):
        form = _full_form(transaction_key="", request_id="", instructions_previously_used="yes",
                           last_used_date="2026-01-01")
        resp = self._post(form, prior_match=None)
        self.assertEqual(resp.status_code, 200)  # re-renders with error, never submits
        body = resp.get_data(as_text=True)
        self.assertIn("could not be verified as previously used", body)

    def test_exact_match_confirmed_bypasses_without_avs_evidence(self):
        form = _full_form(transaction_key="", request_id="", instructions_previously_used="yes",
                           last_used_date="2026-01-01", avs_score="", prior_use_ack="TXN-2026-001")
        resp = self._post(form, prior_match={"transaction_key": 42, "request_id": "TXN-2026-001",
                                              "last_used_date": "2026-01-01"})
        self.assertEqual(resp.status_code, 302)  # submission succeeds

    def test_unclaimed_but_match_exists_is_blocked(self):
        form = _full_form(transaction_key="", request_id="", instructions_previously_used="no",
                           last_used_date="", verbal_confirmed_with_known="on",
                           verbal_contact_name="Jane Doe", verbal_confirm_datetime="2026-09-18T10:00")
        resp = self._post(form, prior_match={"transaction_key": 42, "request_id": "TXN-2026-001",
                                              "last_used_date": "2026-01-01"})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("already used on a completed transaction", body)

    def test_user_entered_date_without_system_match_still_requires_verification(self):
        # A manually-typed last_used_date is never proof by itself.
        form = _full_form(transaction_key="", request_id="", instructions_previously_used="yes",
                           last_used_date="2026-05-05")
        resp = self._post(form, prior_match=None)
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("could not be verified as previously used", body)


class DraftFinalizeUsesRealStoredValueTests(unittest.TestCase):
    """
    Audit fix: finalizing a Draft may leave recv_account_number/recv_routing_number
    blank to mean "keep the already-stored value" — the prior-use match must use
    that REAL stored value, not the blank form field.
    """

    def setUp(self):
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as sess:
            sess["role"] = "submitter"

    def test_blank_account_fields_resolve_to_stored_real_value_for_matching(self):
        form = _full_form(recv_account_number="", recv_routing_number="", prior_use_ack="TXN-2026-001")
        with patch.object(app_module, "current_app_user",
                           return_value={"user_key": 11, "display_name": "U", "email": ""}), \
             patch.object(app_module.db, "get_bank_account_status", return_value="Open"), \
             patch.object(app_module.db, "get_app_user_role_codes", return_value=["sam", "controller"]), \
             patch.object(app_module.db, "get_draft_beneficiary_instruction_key", return_value=77) as mock_bi_key, \
             patch.object(app_module.db, "get_beneficiary_instruction_sensitive_field") as mock_reveal, \
             patch.object(app_module.db, "find_prior_completed_beneficiary_match",
                           return_value={"transaction_key": 42, "request_id": "TXN-2026-001",
                                         "last_used_date": "2026-01-01"}) as mock_match, \
             patch.object(app_module.db, "resolve_approval_rule",
                           return_value={"approval_rule_key": 7, "requires_approver": True,
                                         "requires_controller": True, "requires_vp": False, "requires_cfo": False}), \
             patch.object(app_module, "_upload_intake_attachments"), \
             patch.object(app_module.db, "finalize_draft_submission") as mock_finalize:
            mock_reveal.side_effect = lambda bi_key, column: (
                "99988877" if column == "Receiving_Account_Number" else "026009593"
            )
            data = dict(form)
            data.update(_files())
            resp = self.client.post("/intake/submit", data=data, content_type="multipart/form-data",
                                     follow_redirects=False)

        self.assertEqual(resp.status_code, 302)  # bypass succeeds — not falsely blocked
        mock_bi_key.assert_called_once_with(55, 11)
        mock_match.assert_called_once()
        _, kwargs = mock_match.call_args
        self.assertEqual(kwargs["receiving_account_number"], "99988877")
        self.assertEqual(kwargs["receiving_routing_number"], "026009593")
        mock_finalize.assert_called_once()
        self.assertEqual(mock_finalize.call_args[0][1]["prior_transaction_key"], 42)

    def test_retyped_account_number_on_finalize_is_not_overridden_by_stored_value(self):
        # User actively changes the account number on finalize — the real
        # re-check must use what they typed, not fall back to any stored value.
        form = _full_form(recv_account_number="11112222", recv_routing_number="021000021")
        with patch.object(app_module, "current_app_user",
                           return_value={"user_key": 11, "display_name": "U", "email": ""}), \
             patch.object(app_module.db, "get_bank_account_status", return_value="Open"), \
             patch.object(app_module.db, "get_app_user_role_codes", return_value=["sam", "controller"]), \
             patch.object(app_module.db, "get_draft_beneficiary_instruction_key") as mock_bi_key, \
             patch.object(app_module.db, "find_prior_completed_beneficiary_match",
                           return_value=None) as mock_match, \
             patch.object(app_module.db, "resolve_approval_rule",
                           return_value={"approval_rule_key": 7, "requires_approver": True,
                                         "requires_controller": True, "requires_vp": False, "requires_cfo": False}), \
             patch.object(app_module, "_upload_intake_attachments"), \
             patch.object(app_module.db, "finalize_draft_submission") as mock_finalize:
            data = dict(form)
            data.update(_files())
            resp = self.client.post("/intake/submit", data=data, content_type="multipart/form-data",
                                     follow_redirects=False)

        mock_bi_key.assert_not_called()  # both fields were non-blank — no fallback needed
        _, kwargs = mock_match.call_args
        self.assertEqual(kwargs["receiving_account_number"], "11112222")
        self.assertEqual(resp.status_code, 200)  # claimed previously-used but no match -> blocked
        mock_finalize.assert_not_called()


class TransactionVerificationColumnSeparationTests(unittest.TestCase):
    """Prior-use, AVS, and alternative-verification are stored in independent columns."""

    def _finalize_data(self, **overrides):
        data = {
            "approver_key": 2, "controller_key": 3, "bank_account_key": 5,
            "request_type": "ACH", "classification": "corporate", "property_dept": "", "property_code": "",
            "treasury_service_date": "2026-10-01", "amount": 1000.0, "currency": "USD",
            "payment_purpose": "Vendor payment", "urgent": False, "urgency_reason": "",
            "recv_payee_name": "Payee", "recv_bank_name": "Bank", "recv_account_name": "",
            "recv_account_number": "12345678", "recv_routing_number": "021000021", "recv_bank_address": "",
            "recv_contact_name": "", "recv_contact_email": "", "recv_contact_phone": "",
            "approval_rule_key": 7, "requires_vp": False, "requires_cfo": False,
            "approval_tier": "Senior Accounting Manager / Assistant Controller",
            "instructions_previously_used": True, "last_used_date": "2026-01-01",
            "prior_transaction_key": 42, "avs_score": "", "verbal_confirmed": False,
        }
        data.update(overrides)
        return data

    def test_prior_use_bypass_does_not_write_a_false_avs_score(self):
        fake_cursor = MagicMock()
        fake_cursor.fetchone.side_effect = [(3,), (10, 20), (77,)]
        fake_cursor.rowcount = 1
        fake_conn = MagicMock()
        fake_conn.cursor.return_value = fake_cursor
        with patch.object(db, "get_connection", return_value=fake_conn):
            db.finalize_draft_submission(99, self._finalize_data(), prepared_by_user_key=11)

        verification_calls = [c for c in fake_cursor.execute.call_args_list
                               if "INSERT INTO [etransactions].[TransactionVerification]" in c[0][0]]
        self.assertEqual(len(verification_calls), 1)
        _, params = verification_calls[0][0]
        # Instructions_Previously_Used=1, Prior_Transaction_Key=42 are present,
        # while AVS_Score is NULL — never falsely recorded as AVS-verified.
        self.assertIn(1, params)
        self.assertIn(42, params)
        self.assertIn(None, params)


class ApproverViewPriorUseDisplayTests(unittest.TestCase):
    """get_request_detail() must surface the matched prior Request_ID so the
    approver screen can show it (never the raw account/routing numbers)."""

    def test_detail_sql_selects_prior_transaction_and_its_request_id(self):
        fake_cursor = MagicMock()
        fake_cursor.description = [(c,) for c in (
            "request_id", "property_dept", "property_code", "request_type", "treasury_service_date",
            "prepared_date", "submitted_date", "amount", "currency", "payment_purpose", "urgent",
            "urgency_reason", "status", "current_workflow_stage", "approval_tier", "requires_vp", "over_1m",
            "days_pending", "prepared_by", "assigned_approver", "approver", "controller", "vp_approver",
            "cfo_approver", "orig_bank_name", "orig_account_name", "orig_account_number", "orig_routing_number",
            "orig_bank_contact", "notes_orig", "recv_payee_name", "recv_contact_name", "recv_contact_email",
            "recv_contact_phone", "recv_bank_name", "recv_account_name", "recv_account_number",
            "recv_routing_number", "recv_bank_address", "verbal_confirmed", "_verbal_known_contact",
            "_verbal_requester", "verbal_contact_name", "verbal_confirm_datetime", "avs_score",
            "external_source", "internal_doc_not_used", "instructions_previously_used", "last_used_date",
            "prior_transaction_key", "prior_request_id", "entity_classification", "current_owner_user_key",
            "bank_releaser_user_key",
        )]
        fake_cursor.fetchone.return_value = tuple(None for _ in fake_cursor.description)
        fake_cursor.fetchall.return_value = []
        fake_conn = MagicMock()
        fake_conn.cursor.return_value = fake_cursor

        with patch.object(db, "get_connection", return_value=fake_conn):
            record = db.get_request_detail("TXN-2026-001")

        detail_sql = fake_cursor.execute.call_args_list[0][0][0]
        self.assertIn("Prior_Transaction_Key", detail_sql)
        self.assertIn("priorTxn.Request_ID", detail_sql)
        self.assertIn("prior_transaction_key", record)
        self.assertIn("prior_request_id", record)


class ApproverViewPriorUseBannerRenderTests(unittest.TestCase):
    """Section B must not show misleading AVS/evidence warnings when
    re-verification was legitimately bypassed by a confirmed exact match."""

    def _fake_record(self, **overrides):
        record = {
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
            "prior_transaction_key": None, "prior_request_id": None,
            "entity_classification": "Corporate", "current_owner_user_key": 2, "bank_releaser_user_key": None,
            "docs_checklist": {}, "attachments": {}, "extra_attachments": [], "timeline": [], "comments": [],
        }
        record.update(overrides)
        return record

    def _get(self, record):
        client = app_module.app.test_client()
        with client.session_transaction() as sess:
            sess["role"] = "sam"
        with patch.object(app_module.db, "get_transaction_for_workflow",
                           return_value={"status": "Pending Approver", "accounting_group_key": None}), \
             patch.object(app_module.db, "get_request_detail", return_value=record), \
             patch.object(app_module, "sharepoint_enabled", return_value=False):
            return client.get("/dashboard/request/TXN-2026-999")

    def test_prior_use_validated_shows_banner_and_prior_transaction(self):
        record = self._fake_record(
            instructions_previously_used=True, last_used_date="2026-01-01",
            prior_transaction_key=42, prior_request_id="TXN-2026-001",
        )
        resp = self._get(record)
        body = resp.get_data(as_text=True)
        self.assertIn("Re-verification", body)
        self.assertIn("TXN-2026-001", body)
        self.assertNotIn("9938271045", body)  # never the real account number

    def test_new_instructions_shows_no_prior_use_banner(self):
        resp = self._get(self._fake_record())
        body = resp.get_data(as_text=True)
        self.assertNotIn("Re-verification: Not required", body)


class PriorUsePrecheckEndpointTests(unittest.TestCase):
    """
    INT-017 acknowledgment UX: a server-backed AJAX precheck lets the intake
    page show the matched prior Request_ID/last-used date and enable the
    acknowledgment WITHOUT navigating away — avoiding any risk of clearing
    already-selected required attachment files (a full page reload/redirect
    always clears native <input type="file"> selections; an AJAX call to a
    separate endpoint never touches the page at all).
    """

    def setUp(self):
        self.client = app_module.app.test_client()

    def test_requires_submitter_role(self):
        with self.client.session_transaction() as sess:
            sess["role"] = "sam"  # not submitter
        resp = self.client.post("/intake/prior-use-check", data={
            "recv_payee_name": "Acme", "recv_bank_name": "Chase",
            "recv_account_number": "12345678", "recv_routing_number": "021000021",
            "transaction_key": "",
        })
        self.assertEqual(resp.status_code, 403)

    def test_endpoint_never_touches_file_uploads(self):
        # A pure text-field precheck — never requires/reads request.files,
        # so calling it can never discard any file already selected in the browser.
        with self.client.session_transaction() as sess:
            sess["role"] = "submitter"
        with patch.object(app_module, "current_app_user",
                           return_value={"user_key": 11, "display_name": "U", "email": ""}), \
             patch.object(app_module.db, "find_prior_completed_beneficiary_match", return_value=None):
            resp = self.client.post("/intake/prior-use-check", data={
                "recv_payee_name": "Acme", "recv_bank_name": "Chase",
                "recv_account_number": "12345678", "recv_routing_number": "021000021",
                "transaction_key": "",
            })  # no files in this request at all
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"success": True, "matched": False})

    def test_matched_response_includes_request_id_and_last_used_date(self):
        with self.client.session_transaction() as sess:
            sess["role"] = "submitter"
        with patch.object(app_module, "current_app_user",
                           return_value={"user_key": 11, "display_name": "U", "email": ""}), \
             patch.object(app_module.db, "find_prior_completed_beneficiary_match",
                           return_value={"transaction_key": 42, "request_id": "TXN-2026-001",
                                         "last_used_date": "2026-01-01"}):
            resp = self.client.post("/intake/prior-use-check", data={
                "recv_payee_name": "Acme", "recv_bank_name": "Chase",
                "recv_account_number": "12345678", "recv_routing_number": "021000021",
                "transaction_key": "",
            })
        self.assertEqual(resp.get_json(), {
            "success": True, "matched": True,
            "request_id": "TXN-2026-001", "last_used_date": "2026-01-01",
        })

    def test_draft_blank_fields_resolved_the_same_way_as_final_submit(self):
        with self.client.session_transaction() as sess:
            sess["role"] = "submitter"
        with patch.object(app_module, "current_app_user",
                           return_value={"user_key": 11, "display_name": "U", "email": ""}), \
             patch.object(app_module.db, "get_draft_beneficiary_instruction_key", return_value=77) as mock_bi_key, \
             patch.object(app_module.db, "get_beneficiary_instruction_sensitive_field",
                           side_effect=lambda bi_key, column: (
                               "99988877" if column == "Receiving_Account_Number" else "026009593"
                           )), \
             patch.object(app_module.db, "find_prior_completed_beneficiary_match",
                           return_value={"transaction_key": 42, "request_id": "TXN-2026-001",
                                         "last_used_date": "2026-01-01"}) as mock_match:
            resp = self.client.post("/intake/prior-use-check", data={
                "recv_payee_name": "Acme", "recv_bank_name": "Chase",
                "recv_account_number": "", "recv_routing_number": "",
                "transaction_key": "55",
            })
        mock_bi_key.assert_called_once_with(55, 11)
        self.assertEqual(mock_match.call_args.kwargs["receiving_account_number"], "99988877")
        self.assertTrue(resp.get_json()["matched"])


class IntakePageRendersAcknowledgmentElementsTests(unittest.TestCase):
    """The hidden ack field and live match panel must be present for the JS to drive."""

    def test_intake_page_includes_prior_use_ack_and_panel(self):
        client = app_module.app.test_client()
        with client.session_transaction() as sess:
            sess["role"] = "submitter"
        with patch.object(app_module, "current_app_user",
                           return_value={"user_key": 11, "display_name": "U", "email": ""}), \
             patch.object(app_module.db, "get_user_list", return_value=[]), \
             patch.object(app_module.db, "get_bank_accounts", return_value=[]):
            resp = client.get("/intake")
        body = resp.get_data(as_text=True)
        self.assertIn('id="prior_use_ack"', body)
        self.assertIn('id="prior-use-match-panel"', body)
        self.assertIn('id="transaction_key"', body)


class AcknowledgmentCorrespondsToCurrentMatchTests(unittest.TestCase):
    """
    INT-017 requirements #3/#4/#5: the hidden acknowledgment is never trusted
    by itself — final submission re-runs the exact-match lookup and only
    accepts an acknowledgment that names the SAME Request_ID that fresh
    lookup just found. A forged value, or a stale ack left over from before
    the beneficiary fields changed, must be rejected.
    """

    def setUp(self):
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as sess:
            sess["role"] = "submitter"

    def _patches(self, prior_match):
        return [
            patch.object(app_module, "current_app_user",
                         return_value={"user_key": 11, "display_name": "U", "email": ""}),
            patch.object(app_module.db, "get_bank_account_status", return_value="Open"),
            patch.object(app_module.db, "get_app_user_role_codes", return_value=["sam", "controller"]),
            patch.object(app_module.db, "find_prior_completed_beneficiary_match", return_value=prior_match),
            patch.object(app_module.db, "resolve_approval_rule",
                         return_value={"approval_rule_key": 7, "requires_approver": True,
                                       "requires_controller": True, "requires_vp": False, "requires_cfo": False}),
            patch.object(app_module.db, "reserve_request_id", return_value="TXN-2026-9999"),
            patch.object(app_module, "_upload_required_intake_attachments"),
            patch.object(app_module.db, "insert_transaction", return_value="TXN-2026-9999"),
        ]

    def _post(self, form, prior_match):
        patches = self._patches(prior_match)
        for p in patches:
            p.start()
        try:
            data = dict(form)
            data.update(_files())
            return self.client.post("/intake/submit", data=data, content_type="multipart/form-data",
                                     follow_redirects=False)
        finally:
            for p in patches:
                p.stop()

    def test_no_acknowledgment_is_blocked_even_with_a_real_match(self):
        form = _full_form(transaction_key="", request_id="", prior_use_ack="")
        resp = self._post(form, prior_match={"transaction_key": 42, "request_id": "TXN-2026-001",
                                              "last_used_date": "2026-01-01"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("confirm these are the same", resp.get_data(as_text=True))

    def test_correct_acknowledgment_succeeds(self):
        form = _full_form(transaction_key="", request_id="", prior_use_ack="TXN-2026-001")
        resp = self._post(form, prior_match={"transaction_key": 42, "request_id": "TXN-2026-001",
                                              "last_used_date": "2026-01-01"})
        self.assertEqual(resp.status_code, 302)

    def test_forged_acknowledgment_naming_a_different_transaction_is_rejected(self):
        form = _full_form(transaction_key="", request_id="", prior_use_ack="TXN-FORGED-999")
        resp = self._post(form, prior_match={"transaction_key": 42, "request_id": "TXN-2026-001",
                                              "last_used_date": "2026-01-01"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("confirm these are the same", resp.get_data(as_text=True))

    def test_stale_acknowledgment_after_fields_changed_is_rejected(self):
        # The browser still holds an ack for the OLD match, but the current
        # fields no longer resolve to any match at all server-side.
        form = _full_form(transaction_key="", request_id="", prior_use_ack="TXN-2026-001",
                           recv_account_number="99999999")
        resp = self._post(form, prior_match=None)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("could not be verified as previously used", resp.get_data(as_text=True))


class DraftFinalizeAcknowledgmentTests(unittest.TestCase):
    """Draft finalization must enforce the identical acknowledgment requirement."""

    def setUp(self):
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as sess:
            sess["role"] = "submitter"

    def _patches(self, prior_match):
        return [
            patch.object(app_module, "current_app_user",
                         return_value={"user_key": 11, "display_name": "U", "email": ""}),
            patch.object(app_module.db, "get_bank_account_status", return_value="Open"),
            patch.object(app_module.db, "get_app_user_role_codes", return_value=["sam", "controller"]),
            patch.object(app_module.db, "find_prior_completed_beneficiary_match", return_value=prior_match),
            patch.object(app_module.db, "resolve_approval_rule",
                         return_value={"approval_rule_key": 7, "requires_approver": True,
                                       "requires_controller": True, "requires_vp": False, "requires_cfo": False}),
            patch.object(app_module, "_upload_intake_attachments"),
        ]

    def test_draft_finalize_without_acknowledgment_is_blocked(self):
        patches = self._patches({"transaction_key": 42, "request_id": "TXN-2026-001", "last_used_date": "2026-01-01"})
        with patch.object(app_module.db, "finalize_draft_submission") as mock_finalize:
            for p in patches:
                p.start()
            try:
                data = dict(_full_form(prior_use_ack=""))
                data.update(_files())
                resp = self.client.post("/intake/submit", data=data, content_type="multipart/form-data",
                                         follow_redirects=False)
            finally:
                for p in patches:
                    p.stop()
        self.assertEqual(resp.status_code, 200)
        mock_finalize.assert_not_called()

    def test_draft_finalize_with_correct_acknowledgment_succeeds(self):
        patches = self._patches({"transaction_key": 42, "request_id": "TXN-2026-001", "last_used_date": "2026-01-01"})
        with patch.object(app_module.db, "finalize_draft_submission") as mock_finalize:
            for p in patches:
                p.start()
            try:
                data = dict(_full_form(prior_use_ack="TXN-2026-001"))
                data.update(_files())
                resp = self.client.post("/intake/submit", data=data, content_type="multipart/form-data",
                                         follow_redirects=False)
            finally:
                for p in patches:
                    p.stop()
        self.assertEqual(resp.status_code, 302)
        mock_finalize.assert_called_once()
        self.assertEqual(mock_finalize.call_args[0][1]["prior_transaction_key"], 42)


class AttachmentsPreservedThroughAcknowledgmentWorkflowTests(unittest.TestCase):
    """
    The acknowledgment UX never introduces a POST → re-render → resubmit
    round trip (which would silently clear native file inputs) — the precheck
    is a side AJAX call, and the single final /intake/submit POST uploads
    attachments exactly once, whether or not Previously Used is involved.
    """

    def setUp(self):
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as sess:
            sess["role"] = "submitter"

    def test_single_post_uploads_attachments_exactly_once_when_acknowledged(self):
        with patch.object(app_module, "current_app_user",
                           return_value={"user_key": 11, "display_name": "U", "email": ""}), \
             patch.object(app_module.db, "get_bank_account_status", return_value="Open"), \
             patch.object(app_module.db, "get_app_user_role_codes", return_value=["sam", "controller"]), \
             patch.object(app_module.db, "find_prior_completed_beneficiary_match",
                           return_value={"transaction_key": 42, "request_id": "TXN-2026-001",
                                         "last_used_date": "2026-01-01"}), \
             patch.object(app_module.db, "resolve_approval_rule",
                           return_value={"approval_rule_key": 7, "requires_approver": True,
                                         "requires_controller": True, "requires_vp": False, "requires_cfo": False}), \
             patch.object(app_module.db, "reserve_request_id", return_value="TXN-2026-9999"), \
             patch.object(app_module, "_upload_required_intake_attachments") as mock_upload, \
             patch.object(app_module.db, "insert_transaction", return_value="TXN-2026-9999"):
            data = dict(_full_form(transaction_key="", request_id="", prior_use_ack="TXN-2026-001"))
            data.update(_files())
            resp = self.client.post("/intake/submit", data=data, content_type="multipart/form-data",
                                     follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        mock_upload.assert_called_once()

    def test_blocked_submission_never_uploads_attachments(self):
        # Ack missing → blocked before any attachment upload is attempted —
        # files the requester selected are simply never consumed/discarded.
        with patch.object(app_module, "current_app_user",
                           return_value={"user_key": 11, "display_name": "U", "email": ""}), \
             patch.object(app_module.db, "get_bank_account_status", return_value="Open"), \
             patch.object(app_module.db, "get_app_user_role_codes", return_value=["sam", "controller"]), \
             patch.object(app_module.db, "find_prior_completed_beneficiary_match",
                           return_value={"transaction_key": 42, "request_id": "TXN-2026-001",
                                         "last_used_date": "2026-01-01"}), \
             patch.object(app_module, "_upload_required_intake_attachments") as mock_upload:
            data = dict(_full_form(transaction_key="", request_id="", prior_use_ack=""))
            data.update(_files())
            resp = self.client.post("/intake/submit", data=data, content_type="multipart/form-data",
                                     follow_redirects=False)
        self.assertEqual(resp.status_code, 200)
        mock_upload.assert_not_called()


if __name__ == "__main__":
    unittest.main()
