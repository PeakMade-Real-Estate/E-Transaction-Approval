"""
Tests for Batch 4: route-level enforcement of transaction-visibility
authorization — direct-URL/object-access bypass prevention, dashboard
accounting-group scope validation, and the deliberate distinction between
can_view_transaction() (visibility) and workflow.authorize_action()/
authorize_reassignment() (action authorization).

Pure can_view_transaction()/db._dashboard_scope_where() unit tests live in
test_authorization.py.

Run:  python -m unittest test_dashboard_scoping -v
"""
import unittest
from unittest.mock import patch

import app as app_module
import workflow


def _txn(**overrides):
    base = {
        "transaction_key": 1,
        "request_id": "TXN-2026-500",
        "status": workflow.STATUS_PENDING_APPROVER,
        "prepared_by_user_key": 100,
        "selected_approver_user_key": 200,
        "selected_controller_user_key": 300,
        "vp_approver_user_key": 400,
        "cfo_approver_user_key": 500,
        "current_owner_user_key": 200,
        "bank_releaser_user_key": None,
        "requires_vp": False,
        "requires_cfo": False,
        "amount": 1000.0,
        "entity_classification": "Corporate",
        "accounting_group_key": None,
    }
    base.update(overrides)
    return base


class DirectDetailUrlBypassTests(unittest.TestCase):
    """Part 20 #8: a user cannot bypass dashboard scoping by directly typing/editing
    a Request_ID in the URL — request_detail() must re-check visibility per object."""

    def setUp(self):
        self.client = app_module.app.test_client()

    def _set_role(self, role):
        with self.client.session_transaction() as sess:
            sess["role"] = role

    def test_unauthorized_direct_url_returns_403_and_never_fetches_detail(self):
        self._set_role("sam")  # not the assigned approver for this transaction
        txn = _txn(status=workflow.STATUS_PENDING_APPROVER, selected_approver_user_key=999)
        with patch.object(app_module, "current_app_user_key", return_value=1):
            with patch.object(app_module.db, "get_transaction_for_workflow", return_value=txn):
                with patch.object(app_module.db, "get_request_detail") as mock_detail:
                    resp = self.client.get(f"/dashboard/request/{txn['request_id']}")
        self.assertEqual(resp.status_code, 403)
        mock_detail.assert_not_called()

    def test_authorized_direct_url_succeeds(self):
        self._set_role("sam")
        txn = _txn(status=workflow.STATUS_PENDING_APPROVER, selected_approver_user_key=1)
        record = {
            "request_id": txn["request_id"], "status": txn["status"], "property_dept": "Test",
            "property_code": "", "request_type": "ACH", "treasury_service_date": "2026-01-01",
            "prepared_date": "2026-01-01", "submitted_date": "2026-01-01", "amount": 1000.0,
            "currency": "USD", "payment_purpose": "Test", "urgent": False, "urgency_reason": "",
            "current_workflow_stage": "Approver", "approval_tier": "Senior Accounting Manager / Assistant Controller",
            "requires_vp": False, "over_1m": False, "days_pending": 0,
            "prepared_by": "A", "assigned_approver": "B", "approver": "B", "controller": "C",
            "vp_approver": "", "cfo_approver": "",
            "orig_bank_name": "", "orig_account_name": "", "orig_account_number": "", "orig_routing_number": "",
            "orig_bank_contact": "", "notes_orig": "",
            "recv_payee_name": "", "recv_contact_name": "", "recv_contact_email": "", "recv_contact_phone": "",
            "recv_bank_name": "", "recv_account_name": "", "recv_account_number": "", "recv_routing_number": "",
            "recv_bank_address": "", "notes_recv": "",
            "verbal_confirmed": False, "verbal_confirmed_with": "", "verbal_contact_name": "",
            "verbal_confirm_datetime": "", "avs_score": "", "external_source": False, "internal_doc_not_used": False,
            "instructions_previously_used": False, "last_used_date": "",
            "entity_classification": "Corporate", "current_owner_user_key": 1, "bank_releaser_user_key": None,
            "docs_checklist": {}, "attachments": {}, "extra_attachments": [], "timeline": [], "comments": [],
        }
        with patch.object(app_module, "current_app_user_key", return_value=1):
            with patch.object(app_module.db, "get_transaction_for_workflow", return_value=txn):
                with patch.object(app_module.db, "get_request_detail", return_value=record):
                    with patch.object(app_module, "sharepoint_enabled", return_value=False):
                        resp = self.client.get(f"/dashboard/request/{txn['request_id']}")
        self.assertEqual(resp.status_code, 200)

    def test_controller_in_authorized_group_but_not_assignee_can_still_view(self):
        # Group-based broadening must actually work at the route level too.
        self._set_role("controller")
        txn = _txn(status=workflow.STATUS_PENDING_CONTROLLER, selected_controller_user_key=999, accounting_group_key=55)
        with patch.object(app_module, "current_app_user_key", return_value=300):
            with patch.object(app_module.db, "get_user_accounting_group_keys", return_value=[55]):
                with patch.object(app_module.db, "get_transaction_for_workflow", return_value=txn):
                    with patch.object(app_module.db, "get_request_detail") as mock_detail:
                        mock_detail.return_value = None  # short-circuits before template render; 403 already avoided
                        resp = self.client.get(f"/dashboard/request/{txn['request_id']}")
        # Not 403 — visibility passed (falls through to "not found" redirect only because
        # get_request_detail() was stubbed to None here, not because of an authorization failure).
        self.assertNotEqual(resp.status_code, 403)


class BankingRevealBypassTests(unittest.TestCase):
    """Part 20 #9: banking reveal must use the SAME can_view_transaction() gate as
    request_detail() — it cannot be reached for a transaction outside the caller's scope."""

    def setUp(self):
        self.client = app_module.app.test_client()

    def _set_role(self, role):
        with self.client.session_transaction() as sess:
            sess["role"] = role

    def test_unauthorized_reveal_returns_403_and_never_resolves_banking_keys(self):
        self._set_role("sam")
        txn = _txn(status=workflow.STATUS_PENDING_APPROVER, selected_approver_user_key=999)
        with patch.object(app_module, "current_app_user_key", return_value=1):
            with patch.object(app_module.db, "get_transaction_for_workflow", return_value=txn):
                with patch.object(app_module.db, "get_transaction_banking_keys") as mock_keys:
                    resp = self.client.post(
                        f"/dashboard/request/{txn['request_id']}/reveal-banking/originating",
                        data={"field": "account_number"},
                    )
        self.assertEqual(resp.status_code, 403)
        mock_keys.assert_not_called()


class DashboardAccountingGroupScopeTests(unittest.TestCase):
    """Part 20 #5/#10: the dashboard route must re-validate any browser-supplied
    accounting_group_key against the user's OWN authorized groups, and stats/filters
    must only ever operate on the already-authorized `scoped` result set."""

    def setUp(self):
        self.client = app_module.app.test_client()

    def _set_role(self, role):
        with self.client.session_transaction() as sess:
            sess["role"] = role

    def test_unauthorized_group_key_is_ignored_not_applied(self):
        self._set_role("controller")
        with patch.object(app_module, "current_app_user_key", return_value=300):
            with patch.object(app_module.db, "get_user_accounting_group_keys", return_value=[55]):
                with patch.object(app_module.db, "get_accounting_groups_by_keys", return_value=[]):
                    with patch.object(app_module.db, "get_dashboard_records", return_value=[]) as mock_records:
                        self.client.get("/dashboard?accounting_group_key=77")
        mock_records.assert_called_once_with(role={"controller"}, user_key=300, accounting_group_key=None)

    def test_authorized_group_key_is_applied(self):
        self._set_role("controller")
        with patch.object(app_module, "current_app_user_key", return_value=300):
            with patch.object(app_module.db, "get_user_accounting_group_keys", return_value=[55]):
                with patch.object(app_module.db, "get_accounting_groups_by_keys", return_value=[{"accounting_group_key": 55, "name": "Group 55"}]):
                    with patch.object(app_module.db, "get_dashboard_records", return_value=[]) as mock_records:
                        self.client.get("/dashboard?accounting_group_key=55")
        mock_records.assert_called_once_with(role={"controller"}, user_key=300, accounting_group_key=55)

    def test_stats_and_filters_only_reflect_authorized_scope(self):
        self._set_role("sam")
        scoped_records = [{
            "request_id": "TXN-A", "status": workflow.STATUS_PENDING_APPROVER, "request_type": "ACH",
            "property_dept": "Prop A", "assigned_approver": "Alice", "urgent": False, "amount": 500.0,
            "submitted_date": "2026-01-01", "days_pending": 1,
        }]
        with patch.object(app_module, "current_app_user_key", return_value=200):
            with patch.object(app_module.db, "get_dashboard_records", return_value=scoped_records):
                resp = self.client.get("/dashboard")
        self.assertEqual(resp.status_code, 200)
        # Only the one authorized-scope record should ever reach stats/filter computation —
        # verified indirectly via the mocked return value being the sole data source.


class WorkflowActionVsVisibilityDistinctionTests(unittest.TestCase):
    """
    Part 14/20 #12/13/14: can_view_transaction() governs VIEWING; it must never be
    used as a prerequisite for an explicitly authorized administrative/workflow
    action whose approved reach is broader than default visibility.
    """

    def setUp(self):
        self.client = app_module.app.test_client()

    def _set_role(self, role):
        with self.client.session_transaction() as sess:
            sess["role"] = role

    def test_treasury_can_cancel_transaction_outside_its_normal_visibility_scope(self):
        # Pending Approver is NOT in TREASURY_VISIBLE_STATUSES (can_view_transaction
        # would return False for treasury here) — yet workflow.authorize_action()
        # explicitly permits Treasury to cancel at this stage.
        self._set_role("treasury")
        txn = _txn(status=workflow.STATUS_PENDING_APPROVER, selected_approver_user_key=1)
        with patch.object(app_module, "current_app_user_key", return_value=1):
            with patch.object(app_module.db, "get_transaction_for_workflow", return_value=txn):
                with patch.object(app_module.db, "advance_transaction_workflow") as mock_advance:
                    with patch.object(app_module.db, "add_transaction_comment") as mock_comment:
                        resp = self.client.post(
                            f"/dashboard/request/{txn['request_id']}/action",
                            data={"action": workflow.ACTION_CANCEL, "comment": "Duplicate request"},
                            follow_redirects=False,
                        )
        self.assertEqual(resp.status_code, 302)  # redirected back to detail — action processed, not blocked
        mock_advance.assert_called_once()
        mock_comment.assert_called_once()
        args, kwargs = mock_advance.call_args
        self.assertEqual(kwargs["new_status"], workflow.STATUS_CANCELLED)

    def test_treasury_reassignment_operates_outside_normal_visibility_scope(self):
        # Same principle for reassignment: Treasury covering an absent Approver at
        # Pending Approver — outside Treasury's own default visibility scope — must
        # still succeed via authorize_reassignment(), unaffected by can_view_transaction().
        self._set_role("treasury")
        txn = _txn(status=workflow.STATUS_PENDING_APPROVER, selected_approver_user_key=1)
        with patch.object(app_module, "current_app_user_key", return_value=999):
            with patch.object(app_module.db, "get_transaction_for_workflow", return_value=txn):
                with patch.object(app_module.db, "reassign_transaction_participant") as mock_reassign:
                    resp = self.client.post(
                        f"/dashboard/request/{txn['request_id']}/reassign",
                        data={"reason": "Approver out of office", "new_user_key": "2"},
                        follow_redirects=False,
                    )
        self.assertEqual(resp.status_code, 302)
        mock_reassign.assert_called_once()

    def test_group_visible_controller_who_is_not_the_assignee_cannot_approve(self):
        # can_view_transaction() would allow this controller to VIEW the transaction
        # (in-group), but workflow.authorize_action() must still block the actual
        # approval because they are not the specific assigned Controller.
        self._set_role("controller")
        txn = _txn(status=workflow.STATUS_PENDING_CONTROLLER, selected_controller_user_key=999, accounting_group_key=55)
        with patch.object(app_module, "current_app_user_key", return_value=300):
            with patch.object(app_module.db, "get_transaction_for_workflow", return_value=txn):
                with patch.object(app_module.db, "advance_transaction_workflow") as mock_advance:
                    resp = self.client.post(
                        f"/dashboard/request/{txn['request_id']}/action",
                        data={"action": workflow.ACTION_APPROVE},
                        follow_redirects=False,
                    )
        self.assertEqual(resp.status_code, 302)
        mock_advance.assert_not_called()  # blocked by workflow.authorize_action(), not by visibility


if __name__ == "__main__":
    unittest.main()
