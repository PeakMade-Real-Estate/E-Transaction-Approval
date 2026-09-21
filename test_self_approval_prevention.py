"""
Tests for segregation-of-duties: a requester must not be able to select
themselves as their own Approver or Controller.

Run:  python -m unittest test_self_approval_prevention -v
"""
import unittest
from unittest.mock import patch

import app as app_module


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


class ExcludeSelfFromUserListTests(unittest.TestCase):
    def test_excludes_given_user_key(self):
        with patch.object(app_module.db, "get_user_list", return_value=[
            {"user_key": 1, "display_name": "Alice"},
            {"user_key": 2, "display_name": "Bob"},
        ]):
            result = app_module._get_user_list_excluding_self(1)
        self.assertEqual(result, [{"user_key": 2, "display_name": "Bob"}])

    def test_no_exclusion_when_user_key_is_none(self):
        with patch.object(app_module.db, "get_user_list", return_value=[
            {"user_key": 1, "display_name": "Alice"},
        ]):
            result = app_module._get_user_list_excluding_self(None)
        self.assertEqual(result, [{"user_key": 1, "display_name": "Alice"}])


class ValidateIntakeSubmissionSelfSelectionTests(unittest.TestCase):
    """Pure-function tests for _validate_intake_submission()'s new check."""

    def _base_form(self, **overrides):
        data = {
            "request_type": "ACH", "treasury_service_date": "2026-10-01",
            "property_dept": "Sunset Ridge", "approver_key": "2", "controller_key": "3",
            "payment_purpose": "Vendor payment", "currency": "USD", "amount": "1000",
            "bank_account_key": "5",
            "recv_payee_name": "P", "recv_bank_name": "B", "recv_account_name": "A",
            "recv_account_number": "123", "recv_routing_number": "021000021",
            "instructions_previously_used": "yes", "last_used_date": "2026-01-01",
        }
        data.update(overrides)
        return data

    def _files_present(self):
        return {"validation_evidence": True, "wire_ach_instructions": True, "payment_support": True}

    def test_rejects_self_as_approver(self):
        form = self._base_form(approver_key="11")
        errors = app_module._validate_intake_submission(
            form, files_present=self._files_present(), bank_account_status="Open",
            prepared_by_user_key=11,
        )
        self.assertTrue(any("cannot select yourself as the Approver" in e for e in errors))

    def test_rejects_self_as_controller(self):
        form = self._base_form(controller_key="11")
        errors = app_module._validate_intake_submission(
            form, files_present=self._files_present(), bank_account_status="Open",
            prepared_by_user_key=11,
        )
        self.assertTrue(any("cannot select yourself as the Controller" in e for e in errors))

    def test_allows_different_approver_and_controller(self):
        form = self._base_form(approver_key="2", controller_key="3")
        errors = app_module._validate_intake_submission(
            form, files_present=self._files_present(), bank_account_status="Open",
            prepared_by_user_key=11,
        )
        self.assertEqual(errors, [])

    def test_no_check_when_prepared_by_user_key_not_supplied(self):
        # Backward compatibility: omitting the kwarg must not raise or wrongly reject.
        form = self._base_form(approver_key="11")
        errors = app_module._validate_intake_submission(
            form, files_present=self._files_present(), bank_account_status="Open",
        )
        self.assertFalse(any("cannot select yourself" in e for e in errors))


class SaveDraftSelfSelectionRouteTests(unittest.TestCase):
    """Route-level: Save Draft must also reject self-selection."""

    def setUp(self):
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as sess:
            sess["role"] = "submitter"

    def test_save_draft_rejects_self_as_approver(self):
        with patch.object(app_module, "current_app_user", return_value={"user_key": 11, "display_name": "U", "email": ""}), \
             patch.object(app_module.db, "get_bank_account_status", return_value="Open"), \
             patch.object(app_module.db, "get_app_user_by_key", return_value={"user_key": 11, "display_name": "U", "email": ""}), \
             patch.object(app_module.db, "get_user_list", return_value=[]), \
             patch.object(app_module.db, "get_bank_accounts", return_value=[]), \
             patch.object(app_module.db, "create_draft") as mock_create:
            resp = self.client.post("/intake/submit", data=_draft_form(approver_key="11"))
        self.assertEqual(resp.status_code, 200)  # re-rendered with errors, not redirected
        mock_create.assert_not_called()


class IntakeSubmitSelfSelectionRouteTests(unittest.TestCase):
    """Route-level: a direct POST to /intake/submit (bypassing the UI) must
    still be rejected server-side if approver_key/controller_key == self."""

    def setUp(self):
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as sess:
            sess["role"] = "submitter"

    def _full_form(self, **overrides):
        data = {
            "form_mode": "submit",
            "transaction_key": "", "request_id": "",
            "request_type": "ACH", "treasury_service_date": "2026-10-01",
            "property_dept": "Sunset Ridge",
            "approver_key": "11", "controller_key": "3",
            "amount": "1000.00", "currency": "USD",
            "payment_purpose": "Vendor payment", "urgent": "no",
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

    def test_direct_post_with_self_as_approver_is_rejected(self):
        import io
        files = {
            "file_validation_evidence": (io.BytesIO(b"x"), "v.pdf"),
            "file_wire_ach_instructions": (io.BytesIO(b"x"), "w.pdf"),
            "file_payment_support": (io.BytesIO(b"x"), "p.pdf"),
        }
        with patch.object(app_module, "current_app_user", return_value={"user_key": 11, "display_name": "U", "email": ""}), \
             patch.object(app_module.db, "get_bank_account_status", return_value="Open"), \
             patch.object(app_module.db, "get_user_list", return_value=[]), \
             patch.object(app_module.db, "get_bank_accounts", return_value=[]), \
             patch.object(app_module.db, "insert_transaction") as mock_insert:
            data = dict(self._full_form(approver_key="11"))
            data.update(files)
            resp = self.client.post("/intake/submit", data=data, content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 200)
        mock_insert.assert_not_called()


if __name__ == "__main__":
    unittest.main()
