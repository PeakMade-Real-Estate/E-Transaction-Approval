"""
Tests for team-shared activity logging (Innovation Use Log) — Login/Logout
events written to the SharePoint list shared across multiple apps
(SHAREPOINT_LOG_LIST_ID). The live list's ActivityType column is a strict
choice (allowTextEntry=False, confirmed via Graph) accepting only Login/Logout,
so sharepoint.log_activity() must reject anything else before making a call.

Run:  python -m unittest test_activity_logging -v
"""
import unittest
from unittest.mock import MagicMock, patch

import app as app_module
import sharepoint


class LogActivityFieldTests(unittest.TestCase):
    """sharepoint.log_activity() — pure validation + POST body shape."""

    def test_rejects_unsupported_activity_type_without_any_network_call(self):
        with patch.object(sharepoint, "_graph_request") as mock_request:
            with self.assertRaises(ValueError):
                sharepoint.log_activity(
                    activity_type="Contract Submitted",
                    user_email="a@b.com", user_name="A B", user_role="Submitter",
                )
        mock_request.assert_not_called()

    def test_login_posts_expected_fields_to_the_log_list(self):
        fake_response = MagicMock()
        fake_response.json.return_value = {"id": "1"}
        with patch.object(sharepoint, "get_site_id", return_value="SITE123"), \
             patch.object(sharepoint, "_graph_request", return_value=fake_response) as mock_request, \
             patch.dict("os.environ", {"SHAREPOINT_LOG_LIST_ID": "LIST123", "DEV_LOGIN_ENABLED": "true"}):
            sharepoint.log_activity(
                activity_type=sharepoint.LOG_ACTIVITY_LOGIN,
                user_email="jane@company.com", user_name="Jane Doe", user_role="Submitter",
            )
        method, url = mock_request.call_args[0]
        fields = mock_request.call_args.kwargs["json"]["fields"]
        self.assertEqual(method, "POST")
        self.assertIn("/sites/SITE123/lists/LIST123/items", url)
        self.assertEqual(fields["Title"], "jane@company.com")
        self.assertEqual(fields["UserEmail"], "jane@company.com")
        self.assertEqual(fields["UserName"], "Jane Doe")
        self.assertEqual(fields["UserRole"], "Submitter")
        self.assertEqual(fields["ActivityType"], "Login")
        self.assertEqual(fields["Application"], "E-Transaction Approval Dashboard")
        self.assertEqual(fields["Env"], "Development")
        self.assertTrue(fields["LoginTimestamp"].endswith("Z"))

    def test_production_posture_writes_env_production(self):
        fake_response = MagicMock()
        fake_response.json.return_value = {"id": "1"}
        with patch.object(sharepoint, "get_site_id", return_value="SITE123"), \
             patch.object(sharepoint, "_graph_request", return_value=fake_response) as mock_request, \
             patch.dict("os.environ", {"SHAREPOINT_LOG_LIST_ID": "LIST123", "DEV_LOGIN_ENABLED": "false"}):
            sharepoint.log_activity(
                activity_type=sharepoint.LOG_ACTIVITY_LOGOUT,
                user_email="jane@company.com", user_name="Jane Doe", user_role="Submitter",
            )
        fields = mock_request.call_args.kwargs["json"]["fields"]
        self.assertEqual(fields["Env"], "Production")

    def test_missing_list_id_raises_before_any_network_call(self):
        with patch.dict("os.environ", {"SHAREPOINT_LOG_LIST_ID": ""}), \
             patch.object(sharepoint, "_graph_request") as mock_request:
            with self.assertRaises(RuntimeError):
                sharepoint.log_activity(
                    activity_type=sharepoint.LOG_ACTIVITY_LOGIN,
                    user_email="a@b.com", user_name="A B", user_role="Submitter",
                )
        mock_request.assert_not_called()


class AppLoginLogoutHelperTests(unittest.TestCase):
    """app._log_login_logout_activity() — best-effort, never blocks the caller."""

    def test_noop_when_sharepoint_disabled(self):
        with patch.object(app_module, "sharepoint_enabled", return_value=False), \
             patch.object(app_module.sharepoint, "log_activity") as mock_log:
            app_module._log_login_logout_activity(sharepoint.LOG_ACTIVITY_LOGIN)
        mock_log.assert_not_called()

    def test_swallows_logging_failures_without_raising(self):
        with patch.object(app_module, "sharepoint_enabled", return_value=True), \
             patch.object(app_module.auth, "current_identity",
                          return_value={"source": "dev", "display_name": "Local Developer", "roles": [], "user_id": "dev-local"}), \
             patch.object(app_module, "current_app_user", return_value={"email": "a@b.com", "display_name": "A B"}), \
             patch.object(app_module.sharepoint, "log_activity", side_effect=RuntimeError("Graph error")):
            app_module._log_login_logout_activity(sharepoint.LOG_ACTIVITY_LOGIN)  # must not raise

    def test_skips_logging_when_no_email_can_be_resolved(self):
        with patch.object(app_module, "sharepoint_enabled", return_value=True), \
             patch.object(app_module.auth, "current_identity",
                          return_value={"source": "dev", "display_name": "", "roles": [], "user_id": "dev-local"}), \
             patch.object(app_module, "current_app_user", return_value=None), \
             patch.object(app_module.sharepoint, "log_activity") as mock_log:
            app_module._log_login_logout_activity(sharepoint.LOG_ACTIVITY_LOGIN)
        mock_log.assert_not_called()


class RequireRoleLoginLoggingTests(unittest.TestCase):
    """A brand-new authenticated session logs exactly one Login; later requests in
    the same session (idle timeout not yet reached) must not re-log."""

    def setUp(self):
        self.client = app_module.app.test_client()

    def test_first_request_in_a_session_logs_login_once(self):
        with self.client.session_transaction() as sess:
            sess["role"] = "submitter"
        with patch.object(app_module, "_log_login_logout_activity") as mock_log:
            self.client.get("/dashboard")
        mock_log.assert_called_once_with(sharepoint.LOG_ACTIVITY_LOGIN)

    def test_subsequent_request_in_same_session_does_not_relog(self):
        with self.client.session_transaction() as sess:
            sess["role"] = "submitter"
        with patch.object(app_module, "_log_login_logout_activity") as mock_log:
            self.client.get("/dashboard")
            mock_log.reset_mock()
            self.client.get("/dashboard")
        mock_log.assert_not_called()

    def test_logout_logs_logout(self):
        with self.client.session_transaction() as sess:
            sess["role"] = "submitter"
        with patch.object(app_module, "_log_login_logout_activity") as mock_log:
            self.client.get("/logout")
        mock_log.assert_called_once_with(sharepoint.LOG_ACTIVITY_LOGOUT)

    def test_session_timeout_page_logs_logout(self):
        with self.client.session_transaction() as sess:
            sess["role"] = "submitter"
        with patch.object(app_module, "_log_login_logout_activity") as mock_log:
            self.client.get("/session-timeout")
        mock_log.assert_called_once_with(sharepoint.LOG_ACTIVITY_LOGOUT)


if __name__ == "__main__":
    unittest.main()
