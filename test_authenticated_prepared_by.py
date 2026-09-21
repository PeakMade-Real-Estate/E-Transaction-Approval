"""
Tests for Batch 7 Part 1-6/17: Prepared By/Prepared Date are resolved from the
authenticated application user (Easy Auth in production, an explicit
"Acting as" dev-selected AppUser in local development) — never trusted from
the browser. A crafted POST cannot change who prepared a transaction or when.

Run:  python -m unittest test_authenticated_prepared_by -v
"""
import io
import unittest
from unittest.mock import patch

import app as app_module


def _valid_intake_form(**overrides):
    data = {
        "request_type": "ACH",
        "treasury_service_date": "2026-10-01",
        "property_dept": "Sunset Ridge Apartments",
        "approver_key": "2",
        "controller_key": "3",
        "amount": "1000.00",
        "currency": "USD",
        "payment_purpose": "Vendor payment",
        "urgent": "no",
        "urgency_reason": "",
        "bank_account_key": "5",
        "recv_payee_name": "Acme Vendor LLC",
        "recv_bank_name": "Chase Bank",
        "recv_account_name": "Acme Operating",
        "recv_account_number": "12345678",
        "recv_routing_number": "021000021",
        "recv_bank_address": "",
        "instructions_previously_used": "yes",
        "last_used_date": "2026-01-01",
        "verbal_confirmed_with_known": "",
        "verbal_contact_name": "",
        "verbal_confirm_datetime": "",
        "avs_score": "95",
        # Crafted/attacker-supplied fields — must never be trusted server-side.
        "prepared_by_key": "999",
        "prepared_date": "2020-01-01",
    }
    data.update(overrides)
    return data


def _file(name="e.pdf"):
    return (io.BytesIO(b"data"), name)


class CurrentAppUserResolutionTests(unittest.TestCase):
    """Part 1/4/5: identity -> AppUser resolution for both production and dev."""

    def test_easy_auth_resolves_via_entra_object_id(self):
        with patch.object(app_module, "database_enabled", return_value=True):
            with patch.object(app_module.auth, "current_identity",
                               return_value={"source": "easy_auth", "user_id": "abc-123", "roles": ["submitter"]}):
                with patch.object(app_module.db, "get_app_user_by_entra_object_id",
                                   return_value={"user_key": 7, "display_name": "Alice", "email": "a@x.com"}) as mock_lookup:
                    user = app_module.current_app_user()
        self.assertEqual(user["user_key"], 7)
        mock_lookup.assert_called_once_with("abc-123")

    def test_easy_auth_unmapped_identity_returns_none(self):
        with patch.object(app_module, "database_enabled", return_value=True):
            with patch.object(app_module.auth, "current_identity",
                               return_value={"source": "easy_auth", "user_id": "unknown", "roles": ["submitter"]}):
                with patch.object(app_module.db, "get_app_user_by_entra_object_id", return_value=None):
                    user = app_module.current_app_user()
        self.assertIsNone(user)

    def test_dev_mode_without_acting_as_selection_returns_none(self):
        client = app_module.app.test_client()
        with client.session_transaction() as sess:
            sess["role"] = "submitter"
        with app_module.app.test_request_context("/"):
            with patch.object(app_module, "database_enabled", return_value=True):
                with patch.object(app_module.auth, "current_identity",
                                   return_value={"source": "dev", "user_id": "dev-local", "roles": []}):
                    user = app_module.current_app_user()
        self.assertIsNone(user)

    def test_dev_mode_with_acting_as_selection_resolves_via_get_app_user_by_key(self):
        with app_module.app.test_request_context("/"):
            from flask import session as flask_session
            flask_session["dev_user_key"] = 42
            with patch.object(app_module, "database_enabled", return_value=True):
                with patch.object(app_module.auth, "current_identity",
                                   return_value={"source": "dev", "user_id": "dev-local", "roles": []}):
                    with patch.object(app_module.db, "get_app_user_by_key",
                                       return_value={"user_key": 42, "display_name": "Dev Tester", "email": ""}) as mock_lookup:
                        user = app_module.current_app_user()
        self.assertEqual(user["user_key"], 42)
        mock_lookup.assert_called_once_with(42)

    def test_production_easy_auth_never_uses_dev_session_key(self):
        # Even if a stale dev_user_key exists in session, an easy_auth identity
        # must resolve strictly via Entra Object ID, never the dev session key.
        with app_module.app.test_request_context("/"):
            from flask import session as flask_session
            flask_session["dev_user_key"] = 999
            with patch.object(app_module, "database_enabled", return_value=True):
                with patch.object(app_module.auth, "current_identity",
                                   return_value={"source": "easy_auth", "user_id": "abc-123", "roles": ["submitter"]}):
                    with patch.object(app_module.db, "get_app_user_by_entra_object_id",
                                       return_value={"user_key": 7, "display_name": "Alice", "email": ""}) as mock_lookup:
                        with patch.object(app_module.db, "get_app_user_by_key") as mock_dev_lookup:
                            user = app_module.current_app_user()
        self.assertEqual(user["user_key"], 7)
        mock_lookup.assert_called_once()
        mock_dev_lookup.assert_not_called()

    def test_current_app_user_key_is_key_only_view_of_current_app_user(self):
        with patch.object(app_module, "current_app_user", return_value={"user_key": 55, "display_name": "X", "email": ""}):
            self.assertEqual(app_module.current_app_user_key(), 55)
        with patch.object(app_module, "current_app_user", return_value=None):
            self.assertIsNone(app_module.current_app_user_key())


class IntakeSubmitPreparedByTests(unittest.TestCase):
    """Part 2/3/17: a crafted POST cannot change Prepared By or Prepared Date."""

    def setUp(self):
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as sess:
            sess["role"] = "submitter"

    def _post_intake(self, form_overrides=None):
        data = _valid_intake_form(**(form_overrides or {}))
        data["file_validation_evidence"] = _file("validation.pdf")
        data["file_wire_ach_instructions"] = _file("wire.pdf")
        data["file_payment_support"] = _file("support.pdf")
        return self.client.post("/intake/submit", data=data, content_type="multipart/form-data",
                                 follow_redirects=False)

    def test_authenticated_user_becomes_prepared_by_ignoring_crafted_key(self):
        with patch.object(app_module, "current_app_user",
                           return_value={"user_key": 11, "display_name": "Real User", "email": ""}), \
             patch.object(app_module.db, "get_bank_account_status", return_value="Open"), \
             patch.object(app_module.db, "get_app_user_role_codes", return_value=["sam", "controller"]), \
             patch.object(app_module.db, "find_prior_completed_beneficiary_match",
                           return_value={"transaction_key": 1, "request_id": "TXN-X", "last_used_date": "2026-01-01"}), \
             patch.object(app_module.db, "resolve_approval_rule",
                           return_value={"approval_rule_key": 7, "requires_approver": True,
                                         "requires_controller": True, "requires_vp": False, "requires_cfo": False}), \
             patch.object(app_module, "_upload_required_intake_attachments"), \
             patch.object(app_module.db, "reserve_request_id", return_value="TXN-2026-5001"), \
             patch.object(app_module.db, "insert_transaction", return_value="TXN-2026-5001") as mock_insert:
            resp = self._post_intake({"prepared_by_key": "999"})  # attacker-supplied, must be ignored
        self.assertEqual(resp.status_code, 302)
        mock_insert.assert_called_once()
        db_data = mock_insert.call_args[0][0]
        self.assertEqual(db_data["prepared_by_key"], 11)  # authenticated user, NOT 999
        self.assertNotEqual(db_data["prepared_by_key"], 999)

    def test_prepared_date_is_server_derived_ignoring_crafted_date(self):
        from datetime import datetime
        with patch.object(app_module, "current_app_user",
                           return_value={"user_key": 11, "display_name": "Real User", "email": ""}), \
             patch.object(app_module.db, "get_bank_account_status", return_value="Open"), \
             patch.object(app_module.db, "get_app_user_role_codes", return_value=["sam", "controller"]), \
             patch.object(app_module.db, "find_prior_completed_beneficiary_match",
                           return_value={"transaction_key": 1, "request_id": "TXN-X", "last_used_date": "2026-01-01"}), \
             patch.object(app_module.db, "resolve_approval_rule",
                           return_value={"approval_rule_key": 7, "requires_approver": True,
                                         "requires_controller": True, "requires_vp": False, "requires_cfo": False}), \
             patch.object(app_module, "_upload_required_intake_attachments"), \
             patch.object(app_module.db, "reserve_request_id", return_value="TXN-2026-5002"), \
             patch.object(app_module.db, "insert_transaction", return_value="TXN-2026-5002") as mock_insert:
            resp = self._post_intake({"prepared_date": "2020-01-01"})
        self.assertEqual(resp.status_code, 302)
        db_data = mock_insert.call_args[0][0]
        self.assertEqual(db_data["prepared_date"], datetime.now().strftime("%Y-%m-%d"))
        self.assertNotEqual(db_data["prepared_date"], "2020-01-01")

    def test_unmapped_identity_cannot_submit(self):
        with patch.object(app_module, "current_app_user", return_value=None), \
             patch.object(app_module.db, "insert_transaction") as mock_insert, \
             patch.object(app_module.db, "get_user_list", return_value=[]), \
             patch.object(app_module.db, "get_bank_accounts", return_value=[]):
            resp = self._post_intake()
        self.assertEqual(resp.status_code, 200)  # re-renders intake.html, does not proceed
        mock_insert.assert_not_called()

    def test_intake_get_displays_authenticated_users_name(self):
        with patch.object(app_module.db, "get_user_list", return_value=[]), \
             patch.object(app_module.db, "get_bank_accounts", return_value=[]), \
             patch.object(app_module, "current_app_user",
                           return_value={"user_key": 11, "display_name": "Real User", "email": ""}):
            resp = self.client.get("/intake")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Real User", resp.data)

    def test_intake_get_blocks_when_identity_unmapped(self):
        with patch.object(app_module.db, "get_user_list", return_value=[]), \
             patch.object(app_module.db, "get_bank_accounts", return_value=[]), \
             patch.object(app_module, "current_app_user", return_value=None):
            resp = self.client.get("/intake")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"could not be matched", resp.data)


if __name__ == "__main__":
    unittest.main()
