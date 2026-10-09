"""
Tests for the UAT fix: a server-backed /intake/precheck endpoint lets the
intake form validate the SAME authoritative business rules WITHOUT a full
page navigation, so a predictable validation error never clears the
requester's already-entered form data or already-selected file inputs
(native browser file inputs can never be restored after any page reload).

Covers: precheck correctness (reusing _validate_intake_submission(),
eligible-approver/controller, and prior-use verification unchanged), that it
is read-only (no ETransaction/Draft/WorkflowEvent/SharePoint upload), that
the final /intake/submit POST remains fully authoritative regardless of what
the precheck said, Draft finalize parity, and CSRF/authentication.

Run:  python -m unittest test_intake_precheck -v
"""
import io
import unittest
import external_guard  # noqa: F401  (must precede app/db/sharepoint imports)
from unittest.mock import patch

import app as app_module


def _valid_precheck_form(**overrides):
    data = {
        "transaction_key": "", "request_id": "",
        "request_type": "ACH", "treasury_service_date": "2026-10-01",
        "classification": "corporate", "property_dept": "Sunset Ridge",
        "approver_key": "2", "controller_key": "3",
        "amount": "1000.00", "currency": "USD",
        "payment_purpose": "Vendor payment", "urgent": "no", "urgency_reason": "",
        "bank_account_key": "5",
        "recv_payee_name": "Acme Vendor LLC", "recv_bank_name": "Chase Bank",
        "recv_account_name": "Acme Operating",
        "recv_account_number": "12345678", "recv_routing_number": "021000021",
        "recv_bank_address": "",
        "instructions_previously_used": "no", "last_used_date": "",
        "verbal_confirmed_with_known": "on", "verbal_contact_name": "Jane Doe",
        "verbal_confirm_datetime": "2026-09-18T10:00",
        "avs_score": "",
        "has_validation_evidence": "true",
        "has_wire_ach_instructions": "true",
        "has_payment_support": "true",
    }
    data.update(overrides)
    return data


def _files():
    return {
        "file_validation_evidence": (io.BytesIO(b"x"), "v.pdf"),
        "file_wire_ach_instructions": (io.BytesIO(b"x"), "w.pdf"),
        "file_payment_support": (io.BytesIO(b"x"), "p.pdf"),
    }


class PrecheckCorrectnessTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as sess:
            sess["role"] = "submitter"

    def _patches(self):
        return [
            patch.object(app_module, "current_app_user",
                         return_value={"user_key": 11, "display_name": "U", "email": ""}),
            patch.object(app_module.db, "get_bank_account_status", return_value="Open"),
            patch.object(app_module.db, "get_app_user_role_codes", return_value=["sam", "controller"]),
            patch.object(app_module.db, "resolve_approval_rule",
                         return_value={"approval_rule_key": 7, "requires_approver": True,
                                       "requires_controller": True, "requires_vp": False, "requires_cfo": False}),
        ]

    def _post(self, form):
        patches = self._patches()
        for p in patches:
            p.start()
        try:
            return self.client.post("/intake/precheck", data=form)
        finally:
            for p in patches:
                p.stop()

    def test_1_missing_ordinary_required_field_fails_precheck(self):
        resp = self._post(_valid_precheck_form(payment_purpose=""))
        data = resp.get_json()
        self.assertTrue(data["success"])
        self.assertTrue(any("Payment Purpose" in e for e in data["errors"]))

    def test_2_valid_form_passes_precheck(self):
        resp = self._post(_valid_precheck_form())
        data = resp.get_json()
        self.assertEqual(data["errors"], [])

    def test_3_payment_support_selection_flag_recognized(self):
        resp = self._post(_valid_precheck_form(has_payment_support="true"))
        data = resp.get_json()
        self.assertFalse(any("Payment Support" in e for e in data["errors"]))

    def test_4_missing_required_attachment_flag_fails_precheck(self):
        resp = self._post(_valid_precheck_form(has_payment_support="false"))
        data = resp.get_json()
        self.assertTrue(any("Payment Support" in e for e in data["errors"]))

    def test_eligible_approver_controller_checked(self):
        with patch.object(app_module, "current_app_user",
                           return_value={"user_key": 11, "display_name": "U", "email": ""}), \
             patch.object(app_module.db, "get_bank_account_status", return_value="Open"), \
             patch.object(app_module.db, "get_app_user_role_codes", return_value=["submitter"]), \
             patch.object(app_module.db, "resolve_approval_rule",
                           return_value={"approval_rule_key": 7, "requires_approver": True,
                                         "requires_controller": True, "requires_vp": False, "requires_cfo": False}):
            resp = self.client.post("/intake/precheck", data=_valid_precheck_form())
        data = resp.get_json()
        self.assertTrue(any("not an eligible" in e for e in data["errors"]))


class PrecheckPriorUseParityTests(unittest.TestCase):
    """Prior-use verification (exact match + acknowledgment) must behave
    identically in the precheck and the final submission."""

    def setUp(self):
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as sess:
            sess["role"] = "submitter"

    def _post(self, form, prior_match):
        patches = [
            patch.object(app_module, "current_app_user",
                         return_value={"user_key": 11, "display_name": "U", "email": ""}),
            patch.object(app_module.db, "get_bank_account_status", return_value="Open"),
            patch.object(app_module.db, "get_app_user_role_codes", return_value=["sam", "controller"]),
            patch.object(app_module.db, "resolve_approval_rule",
                         return_value={"approval_rule_key": 7, "requires_approver": True,
                                       "requires_controller": True, "requires_vp": False, "requires_cfo": False}),
            patch.object(app_module.db, "find_prior_completed_beneficiary_match", return_value=prior_match),
        ]
        for p in patches:
            p.start()
        try:
            return self.client.post("/intake/precheck", data=form)
        finally:
            for p in patches:
                p.stop()

    def test_6_prior_use_validated_request_passes_precheck(self):
        form = _valid_precheck_form(instructions_previously_used="yes", last_used_date="2026-01-01",
                                     prior_use_ack="TXN-2026-001")
        resp = self._post(form, prior_match={"transaction_key": 42, "request_id": "TXN-2026-001",
                                              "last_used_date": "2026-01-01"})
        data = resp.get_json()
        self.assertEqual(data["errors"], [])

    def test_7_prior_use_claim_without_match_remains_blocked(self):
        form = _valid_precheck_form(instructions_previously_used="yes", last_used_date="2026-01-01")
        resp = self._post(form, prior_match=None)
        data = resp.get_json()
        self.assertTrue(any("could not be verified as previously used" in e for e in data["errors"]))


class PrecheckDraftParityTests(unittest.TestCase):
    """Draft finalize gets the same precheck behavior, including the blank
    masked-field draft fallback."""

    def setUp(self):
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as sess:
            sess["role"] = "submitter"

    def test_8_draft_precheck_resolves_blank_account_fields_from_stored_value(self):
        form = _valid_precheck_form(transaction_key="55", request_id="TXN-2026-D100",
                                     recv_account_number="", recv_routing_number="",
                                     instructions_previously_used="yes", last_used_date="2026-01-01",
                                     prior_use_ack="TXN-2026-001")
        with patch.object(app_module, "current_app_user",
                           return_value={"user_key": 11, "display_name": "U", "email": ""}), \
             patch.object(app_module.db, "get_bank_account_status", return_value="Open"), \
             patch.object(app_module.db, "get_app_user_role_codes", return_value=["sam", "controller"]), \
             patch.object(app_module.db, "resolve_approval_rule",
                           return_value={"approval_rule_key": 7, "requires_approver": True,
                                         "requires_controller": True, "requires_vp": False, "requires_cfo": False}), \
             patch.object(app_module.db, "get_draft_beneficiary_instruction_key", return_value=77), \
             patch.object(app_module.db, "get_beneficiary_instruction_sensitive_field",
                           side_effect=lambda bi_key, column: (
                               "99988877" if column == "Receiving_Account_Number" else "026009593"
                           )), \
             patch.object(app_module.db, "find_prior_completed_beneficiary_match",
                           return_value={"transaction_key": 42, "request_id": "TXN-2026-001",
                                         "last_used_date": "2026-01-01"}) as mock_match:
            resp = self.client.post("/intake/precheck", data=form)
        data = resp.get_json()
        self.assertEqual(data["errors"], [])
        self.assertEqual(mock_match.call_args.kwargs["receiving_account_number"], "99988877")


class PrecheckIsReadOnlyTests(unittest.TestCase):
    """Precheck must never create/write anything — pure validation only."""

    def setUp(self):
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as sess:
            sess["role"] = "submitter"

    def test_10_creates_no_transaction_or_draft_records(self):
        with patch.object(app_module, "current_app_user",
                           return_value={"user_key": 11, "display_name": "U", "email": ""}), \
             patch.object(app_module.db, "get_bank_account_status", return_value="Open"), \
             patch.object(app_module.db, "get_app_user_role_codes", return_value=["sam", "controller"]), \
             patch.object(app_module.db, "resolve_approval_rule",
                           return_value={"approval_rule_key": 7, "requires_approver": True,
                                         "requires_controller": True, "requires_vp": False, "requires_cfo": False}), \
             patch.object(app_module.db, "insert_transaction") as mock_insert, \
             patch.object(app_module.db, "finalize_draft_submission") as mock_finalize, \
             patch.object(app_module.db, "create_draft") as mock_create, \
             patch.object(app_module.db, "update_draft") as mock_update, \
             patch.object(app_module.db, "advance_transaction_workflow") as mock_advance:
            self.client.post("/intake/precheck", data=_valid_precheck_form())
        mock_insert.assert_not_called()
        mock_finalize.assert_not_called()
        mock_create.assert_not_called()
        mock_update.assert_not_called()
        mock_advance.assert_not_called()

    def test_11_uploads_no_file(self):
        with patch.object(app_module, "current_app_user",
                           return_value={"user_key": 11, "display_name": "U", "email": ""}), \
             patch.object(app_module.db, "get_bank_account_status", return_value="Open"), \
             patch.object(app_module.db, "get_app_user_role_codes", return_value=["sam", "controller"]), \
             patch.object(app_module.db, "resolve_approval_rule",
                           return_value={"approval_rule_key": 7, "requires_approver": True,
                                         "requires_controller": True, "requires_vp": False, "requires_cfo": False}), \
             patch.object(app_module.sharepoint, "upload_attachment") as mock_upload:
            self.client.post("/intake/precheck", data=_valid_precheck_form())
        mock_upload.assert_not_called()


class PrecheckSecurityTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def test_12a_requires_authentication_and_submitter_role(self):
        with self.client.session_transaction() as sess:
            sess["role"] = "sam"
        resp = self.client.post("/intake/precheck", data=_valid_precheck_form())
        self.assertEqual(resp.status_code, 403)

    def test_12b_no_session_role_is_blocked(self):
        resp = self.client.post("/intake/precheck", data=_valid_precheck_form(), follow_redirects=False)
        self.assertIn(resp.status_code, (302, 403))

    def test_12c_csrf_protection_enforced(self):
        with self.client.session_transaction() as sess:
            sess["role"] = "submitter"
        orig = app_module.app.config["WTF_CSRF_ENABLED"]
        app_module.app.config["WTF_CSRF_ENABLED"] = True
        try:
            resp = self.client.post("/intake/precheck", data=_valid_precheck_form())
            self.assertEqual(resp.status_code, 400)
        finally:
            app_module.app.config["WTF_CSRF_ENABLED"] = orig


class FinalSubmissionRemainsAuthoritativeTests(unittest.TestCase):
    """Precheck passing must never be trusted — the real POST independently
    re-validates everything, including a forged/stale attachment claim."""

    def setUp(self):
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as sess:
            sess["role"] = "submitter"

    def test_5_precheck_passes_but_forged_final_post_without_attachment_is_rejected(self):
        # Simulate: precheck said "fine" (has_payment_support=true), but the
        # real multipart POST arrives with NO actual payment-support file.
        with patch.object(app_module, "current_app_user",
                           return_value={"user_key": 11, "display_name": "U", "email": ""}), \
             patch.object(app_module.db, "get_bank_account_status", return_value="Open"), \
             patch.object(app_module.db, "get_app_user_role_codes", return_value=["sam", "controller"]), \
             patch.object(app_module.db, "insert_transaction") as mock_insert:
            data = dict(_valid_precheck_form())
            data.update({
                "file_validation_evidence": (io.BytesIO(b"x"), "v.pdf"),
                "file_wire_ach_instructions": (io.BytesIO(b"x"), "w.pdf"),
                # file_payment_support intentionally omitted
            })
            resp = self.client.post("/intake/submit", data=data, content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 200)  # re-renders with errors, never submits
        body = resp.get_data(as_text=True)
        self.assertIn("Payment Support", body)
        mock_insert.assert_not_called()


class IntakeFormFailClosedMarkupTests(unittest.TestCase):
    """
    The actual network-failure/fail-closed branching lives in browser
    JavaScript, which this Python suite cannot execute. These are the
    strongest practical assertions available short of a browser automation
    test: they pin down the exact rendered JS source so a regression (e.g.
    someone reintroducing an unconditional form.submit()) is caught.

    MANUAL UAT VERIFICATION required for the actual runtime behavior:
      1. Successful precheck (no errors) -> real multipart submit proceeds normally.
      2. Business validation failure (e.g. missing Payment Purpose) -> page
         stays, entered values and selected files remain, Submit re-enabled.
      3. Simulate a precheck network/server failure (e.g. DevTools "offline",
         or temporarily blocking POST /intake/precheck) -> page stays, the
         "We couldn't validate the request right now..." message appears,
         entered values and selected files remain untouched, Submit re-enabled,
         and clicking Submit again retries the precheck.
      4. Save Draft (form_mode=draft) still submits immediately, unaffected.
    """

    def _get_intake_page(self):
        client = app_module.app.test_client()
        with client.session_transaction() as sess:
            sess["role"] = "submitter"
        with patch.object(app_module, "current_app_user",
                           return_value={"user_key": 11, "display_name": "U", "email": ""}), \
             patch.object(app_module.db, "get_user_list", return_value=[]), \
             patch.object(app_module.db, "get_bank_accounts", return_value=[]):
            resp = client.get("/intake")
        return resp.get_data(as_text=True)

    def test_fail_closed_message_present(self):
        body = self._get_intake_page()
        self.assertIn("We couldn't validate the request right now", body)
        self.assertIn("Your information and selected files have", body)

    def test_old_fail_open_behavior_is_gone(self):
        body = self._get_intake_page()
        self.assertNotIn("Fail open", body)

    def test_submit_only_happens_after_precheck_passed_flag(self):
        body = self._get_intake_page()
        # precheckPassed starts false and is only set true in the success
        # branch; the submit call is gated behind checking it first.
        self.assertIn("let precheckPassed = false;", body)
        self.assertIn("precheckPassed = true;", body)
        self.assertIn("if (!precheckPassed) return;", body)

    def test_submit_button_restored_in_finally_block(self):
        body = self._get_intake_page()
        self.assertIn("setSubmitBusy(form, false);", body)

    def test_timeout_handling_present(self):
        body = self._get_intake_page()
        self.assertIn("AbortSignal.timeout", body)

    def test_save_draft_bypass_unaffected(self):
        body = self._get_intake_page()
        self.assertIn("e.submitter.value !== 'submit'", body)


if __name__ == "__main__":
    unittest.main()
