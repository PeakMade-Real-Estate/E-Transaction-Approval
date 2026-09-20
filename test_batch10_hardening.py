"""
Tests for Batch 10: final application-completion / UAT-readiness hardening.

Covers the confirmed, fixed defects from the Batch 10 audit:
  - SECRET_KEY must never silently fall back to a hardcoded value outside
    the explicit local dev posture (Part 5).
  - The legacy mock/session fallback (MOCK_REQUESTS, session["submitted_requests"])
    must never be reachable outside that same explicit dev posture, in
    request_detail(), confirmation(), request_action(), or dashboard() (Part 3/4).
  - request_attach() must enforce the same object-level authorization as
    request_detail()/reveal — previously had none at all (Part 2/24 IDOR fix).
  - CSRF protection is wired in and enforced when active (Part 6).
  - Basic security headers / session cookie flags are set (Part 26).

Run:  python -m unittest test_batch10_hardening -v
"""
import io
import unittest
from unittest.mock import patch

import app as app_module
import workflow as wf


class SecretKeyResolutionTests(unittest.TestCase):
    """Pure-function tests for app.resolve_secret_key() — Part 5."""

    def test_explicit_env_value_is_used_regardless_of_mode(self):
        self.assertEqual(app_module.resolve_secret_key("real-secret", True), "real-secret")
        self.assertEqual(app_module.resolve_secret_key("real-secret", False), "real-secret")

    def test_dev_mode_allows_fallback_when_missing(self):
        key = app_module.resolve_secret_key(None, True)
        self.assertTrue(key)  # some non-empty dev-only value

    def test_production_mode_raises_when_missing(self):
        with self.assertRaises(RuntimeError):
            app_module.resolve_secret_key(None, False)

    def test_production_mode_raises_on_empty_string(self):
        with self.assertRaises(RuntimeError):
            app_module.resolve_secret_key("", False)


class DevFallbackGateTests(unittest.TestCase):
    """dev_fallback_allowed() mirrors the app's one dev/production posture signal."""

    def test_mirrors_dev_login_enabled(self):
        with patch.object(app_module.auth, "dev_login_enabled", return_value=True):
            self.assertTrue(app_module.dev_fallback_allowed())
        with patch.object(app_module.auth, "dev_login_enabled", return_value=False):
            self.assertFalse(app_module.dev_fallback_allowed())


class MockFallbackProductionSafetyTests(unittest.TestCase):
    """
    Batch 10 Part 3/4: MOCK_REQUESTS/session fallback data must never surface
    in a production posture (DEV_LOGIN_ENABLED=false), regardless of
    DB_ENABLED/MOCK_DATA_ENABLED, and regardless of whether a matching
    request_id happens to exist in the static demo data.
    """

    def setUp(self):
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as sess:
            sess["role"] = "submitter"
        self.mock_request_id = app_module.MOCK_REQUESTS[0]["request_id"]

    def test_request_detail_mock_fallback_blocked_in_production_mode(self):
        with patch.object(app_module, "database_enabled", return_value=False), \
             patch.object(app_module, "dev_fallback_allowed", return_value=False):
            resp = self.client.get(f"/dashboard/request/{self.mock_request_id}", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)  # "Request not found" redirect, no mock data shown

    def test_request_detail_mock_fallback_available_in_dev_mode(self):
        with patch.object(app_module, "database_enabled", return_value=False), \
             patch.object(app_module, "dev_fallback_allowed", return_value=True):
            resp = self.client.get(f"/dashboard/request/{self.mock_request_id}", follow_redirects=False)
        self.assertEqual(resp.status_code, 200)  # unchanged dev/demo behavior

    def test_confirmation_mock_fallback_blocked_in_production_mode(self):
        with patch.object(app_module, "database_enabled", return_value=False), \
             patch.object(app_module, "dev_fallback_allowed", return_value=False):
            resp = self.client.get(f"/confirmation/{self.mock_request_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"not found", resp.data.lower())

    def test_request_action_legacy_fallback_blocked_in_production_mode(self):
        with patch.object(app_module, "database_enabled", return_value=False), \
             patch.object(app_module, "dev_fallback_allowed", return_value=False):
            resp = self.client.post(
                f"/dashboard/request/{self.mock_request_id}/action",
                data={"action": "cancel", "comment": "test"},
                follow_redirects=False,
            )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], "/dashboard")

    def test_dashboard_legacy_records_excluded_in_production_mode(self):
        with patch.object(app_module, "current_app_user_key", return_value=None), \
             patch.object(app_module, "database_enabled", return_value=False), \
             patch.object(app_module, "dev_fallback_allowed", return_value=False), \
             patch.object(app_module, "mock_data_enabled", return_value=True):
            resp = self.client.get("/dashboard")
        self.assertNotIn(app_module.MOCK_REQUESTS[0]["request_id"].encode(), resp.data)

    def test_intake_submit_session_fallback_blocked_in_production_mode(self):
        with patch.object(app_module, "database_enabled", return_value=False), \
             patch.object(app_module, "dev_fallback_allowed", return_value=False):
            resp = self.client.post("/intake/submit", data={"request_type": "ACH"}, follow_redirects=False)
        self.assertEqual(resp.status_code, 200)  # re-renders intake with a controlled error, no fake success


class RequestAttachAuthorizationTests(unittest.TestCase):
    """Part 2/24 IDOR fix: request_attach() must enforce can_view_transaction()."""

    def setUp(self):
        self.client = app_module.app.test_client()

    def _set_role(self, role):
        with self.client.session_transaction() as sess:
            sess["role"] = role

    def _txn(self, **overrides):
        base = {
            "transaction_key": 1, "request_id": "TXN-2026-500", "status": wf.STATUS_PENDING_APPROVER,
            "prepared_by_user_key": 100, "selected_approver_user_key": 200,
            "selected_controller_user_key": 300, "vp_approver_user_key": 400, "cfo_approver_user_key": 500,
            "current_owner_user_key": 200, "bank_releaser_user_key": None,
            "requires_vp": False, "requires_cfo": False, "amount": 1000.0,
            "entity_classification": "Corporate", "accounting_group_key": None,
        }
        base.update(overrides)
        return base

    def test_unauthorized_user_cannot_attach_to_unrelated_transaction(self):
        self._set_role("sam")
        txn = self._txn(selected_approver_user_key=999)  # not this user's assignment
        with patch.object(app_module, "current_app_user_key", return_value=1), \
             patch.object(app_module.db, "get_transaction_for_workflow", return_value=txn), \
             patch.object(app_module.sharepoint, "upload_attachment") as mock_upload:
            resp = self.client.post(
                f"/dashboard/request/{txn['request_id']}/attach",
                data={"extra_files": (io.BytesIO(b"x"), "f.pdf")},
                content_type="multipart/form-data",
            )
        self.assertEqual(resp.status_code, 403)
        mock_upload.assert_not_called()

    def test_authorized_user_can_attach_to_own_transaction(self):
        self._set_role("sam")
        txn = self._txn(selected_approver_user_key=1)
        with patch.object(app_module, "current_app_user_key", return_value=1), \
             patch.object(app_module.db, "get_transaction_for_workflow", return_value=txn), \
             patch.object(app_module, "sharepoint_enabled", return_value=False):
            resp = self.client.post(
                f"/dashboard/request/{txn['request_id']}/attach",
                data={"extra_files": (io.BytesIO(b"x"), "f.pdf")},
                content_type="multipart/form-data",
                follow_redirects=False,
            )
        self.assertEqual(resp.status_code, 302)


class CsrfProtectionTests(unittest.TestCase):
    """Part 6: CSRF protection is wired in and enforceable."""

    def setUp(self):
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as sess:
            sess["role"] = "submitter"
        self._orig = app_module.app.config["WTF_CSRF_ENABLED"]
        app_module.app.config["WTF_CSRF_ENABLED"] = True

    def tearDown(self):
        app_module.app.config["WTF_CSRF_ENABLED"] = self._orig

    def test_post_without_token_is_rejected(self):
        resp = self.client.post("/dashboard/request/TXN-NOPE/comment", data={"comment_text": "hi"})
        self.assertEqual(resp.status_code, 400)

    def test_csrf_token_available_in_templates(self):
        fresh_client = app_module.app.test_client()
        resp = fresh_client.get("/role-select")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'name="csrf-token"', resp.data)


class SecurityHeaderTests(unittest.TestCase):
    """Part 26: minimal safe security headers and session cookie flags."""

    def setUp(self):
        self.client = app_module.app.test_client()

    def test_security_headers_present(self):
        with self.client.session_transaction() as sess:
            sess["role"] = "submitter"
        resp = self.client.get("/dashboard")
        self.assertEqual(resp.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(resp.headers.get("X-Frame-Options"), "SAMEORIGIN")
        self.assertIn("Referrer-Policy", resp.headers)

    def test_session_cookie_flags_configured(self):
        self.assertTrue(app_module.app.config["SESSION_COOKIE_HTTPONLY"])
        self.assertEqual(app_module.app.config["SESSION_COOKIE_SAMESITE"], "Lax")


if __name__ == "__main__":
    unittest.main()
