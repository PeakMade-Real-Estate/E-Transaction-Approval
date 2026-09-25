"""Tests for application/Easy Auth idle-session expiration."""

import unittest
from unittest.mock import patch

import app as app_module


class IdleTimeoutConfigurationTests(unittest.TestCase):
    def test_defaults_to_five_minutes_when_unset(self):
        self.assertEqual(app_module.resolve_idle_timeout_minutes(None), 5)
        self.assertEqual(app_module.resolve_idle_timeout_minutes(""), 5)

    def test_accepts_positive_integer_minutes(self):
        self.assertEqual(app_module.resolve_idle_timeout_minutes("12"), 12)

    def test_rejects_invalid_or_nonpositive_values(self):
        for value in ("abc", "0", "-1", "1.5"):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                app_module.resolve_idle_timeout_minutes(value)


class IdleTimeoutRequestTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()
        self.original_timeout = app_module.app.config["IDLE_TIMEOUT_SECONDS"]
        app_module.app.config["IDLE_TIMEOUT_SECONDS"] = 300

    def tearDown(self):
        app_module.app.config["IDLE_TIMEOUT_SECONDS"] = self.original_timeout

    def _set_session(self, *, role="submitter", last_activity=100.0):
        with self.client.session_transaction() as sess:
            sess["role"] = role
            sess[app_module.SESSION_LAST_ACTIVITY_KEY] = last_activity

    def test_expired_html_request_clears_session_renders_message_and_expires_easy_auth_cookie(self):
        self._set_session(last_activity=100.0)
        with patch.object(app_module, "_now_timestamp", return_value=401.0):
            response = self.client.get("/dashboard", follow_redirects=False)

        self.assertEqual(response.status_code, 401)
        self.assertIn(b"Session expired", response.data)
        self.assertIn("AppServiceAuthSession=", response.headers.get("Set-Cookie", ""))
        self.assertIn("Max-Age=0", response.headers.get("Set-Cookie", ""))
        with self.client.session_transaction() as sess:
            self.assertNotIn("role", sess)
            self.assertNotIn(app_module.SESSION_LAST_ACTIVITY_KEY, sess)

    def test_expired_json_endpoint_returns_401_json_and_expires_cookie(self):
        self._set_session(role="business_admin", last_activity=100.0)
        with patch.object(app_module, "_now_timestamp", return_value=401.0):
            response = self.client.get("/admin/database-test")

        self.assertEqual(response.status_code, 401)
        self.assertTrue(response.is_json)
        self.assertTrue(response.get_json()["session_expired"])
        self.assertIn("AppServiceAuthSession=", response.headers.get("Set-Cookie", ""))

    def test_activity_endpoint_updates_only_after_throttle_interval(self):
        self._set_session(last_activity=100.0)
        with patch.object(app_module, "_now_timestamp", return_value=120.0):
            response = self.client.post("/api/session/activity")
        self.assertEqual(response.status_code, 200)
        with self.client.session_transaction() as sess:
            self.assertEqual(sess[app_module.SESSION_LAST_ACTIVITY_KEY], 100.0)

        with patch.object(app_module, "_now_timestamp", return_value=131.0):
            response = self.client.post("/api/session/activity")
        self.assertEqual(response.status_code, 200)
        with self.client.session_transaction() as sess:
            self.assertEqual(sess[app_module.SESSION_LAST_ACTIVITY_KEY], 131.0)

    def test_normal_protected_request_does_not_refresh_activity(self):
        self._set_session(role="business_admin", last_activity=100.0)
        with patch.object(app_module, "_now_timestamp", return_value=200.0), \
             patch.object(app_module.db, "test_connection", return_value=True):
            response = self.client.get("/admin/database-test")

        self.assertEqual(response.status_code, 200)
        with self.client.session_transaction() as sess:
            self.assertEqual(sess[app_module.SESSION_LAST_ACTIVITY_KEY], 100.0)

    def test_timeout_page_clears_state_and_links_to_fresh_easy_auth_login(self):
        self._set_session()
        with patch.object(app_module, "sharepoint_enabled", return_value=False):
            response = self.client.get("/session-timeout")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Session expired", response.data)
        self.assertIn(b"/.auth/login/aad?post_login_redirect_uri=%2F", response.data)
        self.assertNotIn(b"/.auth/logout", response.data)
        self.assertIn("AppServiceAuthSession=", response.headers.get("Set-Cookie", ""))

    def test_manual_easy_auth_logout_uses_local_cookie_expiration_not_provider_logout(self):
        with patch.object(
            app_module.auth,
            "current_identity",
            return_value={"source": "easy_auth", "user_id": "oid", "display_name": "User", "roles": ["submitter"]},
        ):
            response = self.client.get("/logout", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertIn("/.auth/login/aad?post_login_redirect_uri=%2F", response.headers["Location"])
        self.assertNotIn("/.auth/logout", response.headers["Location"])
        self.assertIn("AppServiceAuthSession=", response.headers.get("Set-Cookie", ""))


if __name__ == "__main__":
    unittest.main()