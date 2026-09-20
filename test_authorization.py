"""
Tests for Batch 4: centralized transaction-visibility authorization.

Covers authorization.can_view_transaction() (the single source of truth for
"can this signed-in user see this transaction at all") and db._dashboard_scope_where()
(its SQL-side equivalent for the dashboard query). Route-level enforcement
(direct URL access, banking reveal, dashboard filtering, and the deliberate
non-gating of workflow actions/reassignment) is covered in
test_dashboard_scoping.py.

Run:  python -m unittest test_authorization -v
"""
import unittest

import authorization
import db
import workflow


def _txn(**overrides):
    base = {
        "status": workflow.STATUS_PENDING_APPROVER,
        "prepared_by_user_key": 100,
        "selected_approver_user_key": 200,
        "selected_controller_user_key": 300,
        "vp_approver_user_key": 400,
        "cfo_approver_user_key": 500,
        "accounting_group_key": None,
    }
    base.update(overrides)
    return base


class RequesterVisibilityTests(unittest.TestCase):
    """Part 20 #1: Requester sees only their own transaction."""

    def test_can_view_own_transaction(self):
        txn = _txn(prepared_by_user_key=100)
        self.assertTrue(authorization.can_view_transaction(role="submitter", user_key=100, txn=txn))

    def test_cannot_view_another_requesters_transaction(self):
        txn = _txn(prepared_by_user_key=100)
        self.assertFalse(authorization.can_view_transaction(role="submitter", user_key=999, txn=txn))


class ApproverVisibilityTests(unittest.TestCase):
    """Part 20 #2: Approver (sam) sees assigned transactions, not all Pending Approver work."""

    def test_can_view_assigned_transaction(self):
        txn = _txn(status=workflow.STATUS_PENDING_APPROVER, selected_approver_user_key=200)
        self.assertTrue(authorization.can_view_transaction(role="sam", user_key=200, txn=txn))

    def test_cannot_view_unassigned_transaction_at_same_status(self):
        txn = _txn(status=workflow.STATUS_PENDING_APPROVER, selected_approver_user_key=200)
        self.assertFalse(authorization.can_view_transaction(role="sam", user_key=201, txn=txn))


class ControllerVisibilityTests(unittest.TestCase):
    """Part 20 #3/4/5: Controller defaults to personal assignment, may broaden to an
    authorized accounting group, and may never be expanded by an unauthorized group key."""

    def test_defaults_to_personal_assignment(self):
        txn = _txn(selected_controller_user_key=300, accounting_group_key=None)
        self.assertTrue(authorization.can_view_transaction(role="controller", user_key=300, txn=txn))

    def test_not_assigned_and_no_group_context_denied(self):
        txn = _txn(selected_controller_user_key=300, accounting_group_key=None)
        self.assertFalse(authorization.can_view_transaction(role="controller", user_key=999, txn=txn))

    def test_broadens_to_authorized_accounting_group(self):
        txn = _txn(selected_controller_user_key=999, accounting_group_key=55)
        self.assertTrue(authorization.can_view_transaction(
            role="controller", user_key=300, txn=txn, authorized_group_keys=[55],
        ))

    def test_unauthorized_accounting_group_does_not_expand_access(self):
        txn = _txn(selected_controller_user_key=999, accounting_group_key=77)
        self.assertFalse(authorization.can_view_transaction(
            role="controller", user_key=300, txn=txn, authorized_group_keys=[55],
        ))

    def test_no_authorized_groups_falls_back_to_personal_assignment_only(self):
        txn = _txn(selected_controller_user_key=999, accounting_group_key=55)
        self.assertFalse(authorization.can_view_transaction(
            role="controller", user_key=300, txn=txn, authorized_group_keys=[],
        ))


class VpCfoVisibilityTests(unittest.TestCase):
    """Part 20 #6: VP/CFO share the same personal-assignment + accounting-group rule as Controller."""

    def test_vp_personal_assignment(self):
        txn = _txn(vp_approver_user_key=400, accounting_group_key=None)
        self.assertTrue(authorization.can_view_transaction(role="vp", user_key=400, txn=txn))

    def test_vp_accounting_group_broadens_access(self):
        txn = _txn(vp_approver_user_key=999, accounting_group_key=55)
        self.assertTrue(authorization.can_view_transaction(
            role="vp", user_key=400, txn=txn, authorized_group_keys=[55],
        ))

    def test_cfo_personal_assignment(self):
        txn = _txn(cfo_approver_user_key=500, accounting_group_key=None)
        self.assertTrue(authorization.can_view_transaction(role="cfo", user_key=500, txn=txn))

    def test_cfo_unauthorized_group_denied(self):
        txn = _txn(cfo_approver_user_key=999, accounting_group_key=77)
        self.assertFalse(authorization.can_view_transaction(
            role="cfo", user_key=500, txn=txn, authorized_group_keys=[55],
        ))


class TreasuryVisibilityTests(unittest.TestCase):
    """Part 20 #7: Treasury sees its operational scope, including Property transactions
    still awaiting final bank release (Controller/VP owns the action, Treasury retains view)."""

    def test_treasury_sees_ready_for_treasury(self):
        txn = _txn(status=workflow.STATUS_READY_FOR_TREASURY)
        self.assertTrue(authorization.can_view_transaction(role="treasury", user_key=1, txn=txn))

    def test_treasury_sees_property_awaiting_bank_release(self):
        txn = _txn(status=workflow.STATUS_AWAITING_RELEASE)
        self.assertTrue(authorization.can_view_transaction(role="treasury", user_key=1, txn=txn))

    def test_treasury_sees_completed(self):
        txn = _txn(status=workflow.STATUS_COMPLETED)
        self.assertTrue(authorization.can_view_transaction(role="treasury", user_key=1, txn=txn))

    def test_treasury_does_not_see_early_stage_transaction(self):
        txn = _txn(status=workflow.STATUS_PENDING_APPROVER)
        self.assertFalse(authorization.can_view_transaction(role="treasury", user_key=1, txn=txn))


class BusinessAdminAndUndefinedRoleTests(unittest.TestCase):
    def test_business_admin_sees_everything(self):
        txn = _txn(status=workflow.STATUS_PENDING_APPROVER, prepared_by_user_key=999)
        self.assertTrue(authorization.can_view_transaction(role="business_admin", user_key=1, txn=txn))

    def test_roles_with_no_defined_visibility_see_nothing(self):
        txn = _txn()
        self.assertFalse(authorization.can_view_transaction(role="it_admin", user_key=1, txn=txn))
        self.assertFalse(authorization.can_view_transaction(role="treasury_bank_admin", user_key=1, txn=txn))


class HistoricalParticipantVisibilityTests(unittest.TestCase):
    """
    Part 20 #11: historical participant visibility. Selected*_User_Key fields are
    set once (at assignment time) and never cleared as the workflow advances to
    later stages, so a participant's access to a transaction they were assigned to
    persists automatically after their stage completes — no separate
    WorkflowAssignment-history lookup is needed for this.
    """

    def test_controller_retains_visibility_after_stage_advances_to_vp(self):
        txn = _txn(status=workflow.STATUS_PENDING_VP, selected_controller_user_key=300, vp_approver_user_key=400)
        self.assertTrue(authorization.can_view_transaction(role="controller", user_key=300, txn=txn))

    def test_approver_retains_visibility_after_stage_advances_to_cfo(self):
        txn = _txn(status=workflow.STATUS_PENDING_CFO, selected_approver_user_key=200, cfo_approver_user_key=500)
        self.assertTrue(authorization.can_view_transaction(role="sam", user_key=200, txn=txn))


class DevBypassVisibilityTests(unittest.TestCase):
    """user_key is None (no Easy Auth identity resolvable) — falls back to the
    pre-Batch-4 role+status-only relaxation, same as authorize_action()'s existing convention."""

    def test_dev_bypass_uses_status_only(self):
        txn = _txn(status=workflow.STATUS_PENDING_CONTROLLER, selected_controller_user_key=999)
        self.assertTrue(authorization.can_view_transaction(role="controller", user_key=None, txn=txn))
        txn2 = _txn(status=workflow.STATUS_PENDING_VP, selected_controller_user_key=999)
        self.assertFalse(authorization.can_view_transaction(role="controller", user_key=None, txn=txn2))


class DashboardScopeWhereTests(unittest.TestCase):
    """
    Part 20 #5/#10: the SQL-side scoping (db._dashboard_scope_where()) must stay
    logically equivalent to can_view_transaction() and must never trust an
    unauthorized accounting_group_key.
    """

    def test_controller_personal_scope_sql(self):
        where, params = db._dashboard_scope_where("controller", 300, None)
        self.assertEqual(where, ["t.SelectedController_User_Key = ? AND t.Current_Status <> ?"])
        self.assertEqual(params, [300, workflow.STATUS_DRAFT])

    def test_controller_group_scope_sql(self):
        where, params = db._dashboard_scope_where("controller", 300, 55)
        self.assertEqual(where, ["be.AccountingGroup_Key = ? AND t.Current_Status <> ?"])
        self.assertEqual(params, [55, workflow.STATUS_DRAFT])

    def test_business_admin_unrestricted_sql(self):
        where, params = db._dashboard_scope_where("business_admin", 1, None)
        self.assertEqual(where, [])
        self.assertEqual(params, [])

    def test_no_defined_visibility_role_excludes_everything_sql(self):
        where, params = db._dashboard_scope_where("it_admin", 1, None)
        self.assertEqual(where, ["1 = 0"])

    def test_treasury_scope_sql_includes_awaiting_release(self):
        where, params = db._dashboard_scope_where("treasury", 1, None)
        self.assertIn(workflow.STATUS_AWAITING_RELEASE, params)
        self.assertIn(workflow.STATUS_READY_FOR_TREASURY, params)


if __name__ == "__main__":
    unittest.main()
