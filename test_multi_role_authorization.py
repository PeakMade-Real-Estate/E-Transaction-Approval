"""
Tests for the seamless multi-role authorization refactor
(e_transaction_multi_role_authorization_refactor.md).

Covers the Section 34 acceptance-criteria test matrix: Easy Auth users with
multiple Entra App Role claims get the UNION of every role's capabilities in
one seamless session (no role-selection page, no active-role switch), while
existing per-object assignment/stage/group rules continue to apply exactly
as before. Local dev "Acting As" simulates the same union from AppUserRole.

Run:  python -m unittest test_multi_role_authorization -v
"""
import base64
import json
import unittest
from unittest.mock import patch

import app as app_module
import authorization
import workflow as wf


def _easy_auth_headers(user_id, roles, name="Test User"):
    """Build the X-MS-CLIENT-PRINCIPAL* headers auth.py expects, with one
    claim per Entra App Role value (multiple roles = multiple claims)."""
    claims = [{"typ": "roles", "val": r} for r in roles]
    principal = base64.b64encode(json.dumps({"claims": claims}).encode()).decode()
    return {
        "X-MS-CLIENT-PRINCIPAL-ID": user_id,
        "X-MS-CLIENT-PRINCIPAL-NAME": name,
        "X-MS-CLIENT-PRINCIPAL": principal,
    }


class NoRoleSelectionForMultiRoleEasyAuthTests(unittest.TestCase):
    """Part 4/28/30: production Easy Auth users are never redirected to a
    role-selection/switching page, regardless of how many roles they hold."""

    def setUp(self):
        self.client = app_module.app.test_client()

    def test_multi_role_user_reaches_dashboard_without_role_select_redirect(self):
        headers = _easy_auth_headers("marilyn-oid", ["Submitter", "TreasuryManager"])
        with patch.object(app_module, "current_app_user_key", return_value=11), \
             patch.object(app_module.db, "get_dashboard_records", return_value=[]):
            resp = self.client.get("/dashboard", headers=headers, follow_redirects=False)
        self.assertEqual(resp.status_code, 200)

    def test_single_role_easy_auth_user_also_no_redirect(self):
        headers = _easy_auth_headers("athena-oid", ["Approver"])
        with patch.object(app_module, "current_app_user_key", return_value=14), \
             patch.object(app_module.db, "get_dashboard_records", return_value=[]):
            resp = self.client.get("/dashboard", headers=headers, follow_redirects=False)
        self.assertEqual(resp.status_code, 200)

    def test_role_select_route_is_a_no_op_under_easy_auth(self):
        headers = _easy_auth_headers("marilyn-oid", ["Submitter", "TreasuryManager"])
        resp = self.client.get("/role-select", headers=headers, follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/dashboard", resp.headers["Location"])

    def test_switch_role_is_a_no_op_under_easy_auth(self):
        headers = _easy_auth_headers("marilyn-oid", ["Submitter", "TreasuryManager"])
        resp = self.client.get("/switch-role", headers=headers, follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/dashboard", resp.headers["Location"])

    def test_current_roles_returns_full_set_from_entra_claims(self):
        headers = _easy_auth_headers("marilyn-oid", ["Submitter", "TreasuryManager"])
        with self.client:
            self.client.get("/dashboard", headers=headers)
            # current_roles() re-derives from the request context each call.
            with app_module.app.test_request_context("/", headers=headers):
                self.assertEqual(app_module.current_roles(), {"submitter", "treasury"})

    def test_client_cannot_add_roles_via_query_string(self):
        headers = _easy_auth_headers("marilyn-oid", ["Submitter"])
        with app_module.app.test_request_context("/?role=business_admin&roles=cfo", headers=headers):
            self.assertEqual(app_module.current_roles(), {"submitter"})

    def test_no_recognized_role_claim_is_denied(self):
        headers = _easy_auth_headers("nobody-oid", [])
        resp = self.client.get("/dashboard", headers=headers)
        self.assertEqual(resp.status_code, 403)


class MarilynScenarioTests(unittest.TestCase):
    """Section 2/34: submitter + treasury — the doc's canonical example."""

    def setUp(self):
        self.client = app_module.app.test_client()
        self.headers = _easy_auth_headers("marilyn-oid", ["Submitter", "TreasuryManager"])

    def test_can_reach_intake_form(self):
        with patch.object(app_module, "current_app_user", return_value={"user_key": 11, "display_name": "Marilyn", "email": ""}):
            resp = self.client.get("/intake", headers=self.headers)
        self.assertEqual(resp.status_code, 200)

    def test_dashboard_scope_is_union_of_submitter_and_treasury(self):
        submitter_row = {"request_id": "TXN-MINE", "status": wf.STATUS_PENDING_APPROVER,
                          "request_type": "ACH", "assigned_approver": "", "urgent": False,
                          "amount": 100.0, "submitted_date": "2026-01-01", "days_pending": 1}
        treasury_row = {"request_id": "TXN-TREASURY", "status": wf.STATUS_READY_FOR_TREASURY,
                        "request_type": "Wire", "assigned_approver": "", "urgent": False,
                        "amount": 200.0, "submitted_date": "2026-01-02", "days_pending": 1}

        def fake_get_dashboard_records(*, role, user_key, accounting_group_key=None):
            # Simulate the real SQL union: return rows matching ANY held role.
            rows = []
            if "submitter" in role:
                rows.append(submitter_row)
            if "treasury" in role:
                rows.append(treasury_row)
            return rows

        with patch.object(app_module, "current_app_user_key", return_value=11), \
             patch.object(app_module.db, "get_dashboard_records", side_effect=fake_get_dashboard_records):
            resp = self.client.get("/dashboard", headers=self.headers)
        body = resp.get_data(as_text=True)
        self.assertIn("TXN-MINE", body)
        self.assertIn("TXN-TREASURY", body)

    def test_cannot_approve_as_sam_solely_because_treasury_present(self):
        txn = {"status": wf.STATUS_PENDING_APPROVER, "selected_approver_user_key": 999,
               "prepared_by_user_key": 11}
        with self.assertRaises(wf.UnauthorizedActionError):
            wf.authorize_action(role={"submitter", "treasury"}, user_key=11, txn=txn, action=wf.ACTION_APPROVE)

    def test_treasury_role_authorizes_treasury_initiated_action(self):
        txn = {"status": wf.STATUS_READY_FOR_TREASURY}
        wf.authorize_action(role={"submitter", "treasury"}, user_key=11, txn=txn, action=wf.ACTION_TREASURY_INITIATED)  # no raise

    def test_bank_account_management_uses_treasury_permission(self):
        with self.client.session_transaction():
            pass
        with patch.object(app_module, "current_roles", return_value={"submitter", "treasury"}):
            self.assertTrue(app_module.can_view_bank_accounts())
            self.assertTrue(app_module.can_edit_bank_accounts())


class UnionVisibilityTests(unittest.TestCase):
    """Section 9/10/16: dashboard/object visibility is a union of all held
    roles' individual scopes, de-duplicated, never a blended condition."""

    def test_can_view_transaction_true_if_any_role_grants_it(self):
        txn = {"status": wf.STATUS_PENDING_APPROVER, "prepared_by_user_key": 999,
               "selected_approver_user_key": 11, "selected_controller_user_key": None,
               "vp_approver_user_key": None, "cfo_approver_user_key": None,
               "accounting_group_key": None}
        # user 11 is not the preparer (submitter path fails) but IS the selected
        # approver (sam path succeeds) -> overall union must be True.
        self.assertTrue(authorization.can_view_transaction(
            role={"submitter", "sam"}, user_key=11, txn=txn,
        ))

    def test_can_view_transaction_false_if_no_role_grants_it(self):
        txn = {"status": wf.STATUS_PENDING_APPROVER, "prepared_by_user_key": 999,
               "selected_approver_user_key": 888, "selected_controller_user_key": None,
               "vp_approver_user_key": None, "cfo_approver_user_key": None,
               "accounting_group_key": None}
        self.assertFalse(authorization.can_view_transaction(
            role={"submitter", "sam"}, user_key=11, txn=txn,
        ))

    def test_draft_only_visible_via_submitter_role_even_with_other_roles(self):
        txn = {"status": wf.STATUS_DRAFT, "prepared_by_user_key": 11,
               "selected_approver_user_key": 11, "accounting_group_key": None}
        # Even though this user also happens to be the (schema-required) selected
        # approver on the Draft row, only the submitter path may see a Draft.
        self.assertTrue(authorization.can_view_transaction(role={"submitter", "sam"}, user_key=11, txn=txn))
        txn_not_owner = {"status": wf.STATUS_DRAFT, "prepared_by_user_key": 999,
                          "selected_approver_user_key": 11, "accounting_group_key": None}
        self.assertFalse(authorization.can_view_transaction(role={"sam"}, user_key=11, txn=txn_not_owner))

    def test_single_role_stays_unrestricted_for_business_admin(self):
        txn = {"status": wf.STATUS_DRAFT, "prepared_by_user_key": 999, "accounting_group_key": None}
        self.assertTrue(authorization.can_view_transaction(role={"business_admin"}, user_key=1, txn=txn))

    def test_dashboard_union_deduplicates_overlapping_scope(self):
        # A transaction visible under BOTH submitter and treasury scope must
        # appear exactly once in the combined authorized scope.
        role, params = app_module.db._dashboard_scope_where({"submitter", "treasury"}, 11, None)
        self.assertEqual(len(role), 1)
        self.assertIn(" OR ", role[0])


class GroupScopePerRoleTests(unittest.TestCase):
    """Section 23: one role's AccountingGroup mapping must never apply to a
    different role's check."""

    def test_authorized_groups_kept_separate_per_role(self):
        txn_vp_group = {"status": wf.STATUS_PENDING_VP, "vp_approver_user_key": 999,
                         "selected_controller_user_key": 999, "accounting_group_key": 55,
                         "prepared_by_user_key": 1}
        # user holds controller (authorized for group 77 only) and vp (authorized
        # for group 99 only) — group 55 on this txn must not be treated as
        # authorized for either role.
        by_role = {"controller": [77], "vp": [99]}
        self.assertFalse(authorization.can_view_transaction(
            role={"controller", "vp"}, user_key=1, txn=txn_vp_group, authorized_group_keys=by_role,
        ))

    def test_authorized_group_matching_the_correct_role_grants_access(self):
        txn = {"status": wf.STATUS_PENDING_VP, "vp_approver_user_key": 999,
               "selected_controller_user_key": 999, "accounting_group_key": 99,
               "prepared_by_user_key": 1}
        by_role = {"controller": [77], "vp": [99]}
        self.assertTrue(authorization.can_view_transaction(
            role={"controller", "vp"}, user_key=1, txn=txn, authorized_group_keys=by_role,
        ))


class DevActingAsRoleSetTests(unittest.TestCase):
    """Section 27: dev Acting-As should resolve the FULL simulated role set
    automatically (from AppUserRole), not prompt for a single role; the
    DEV-ONLY isolation override still works when explicitly chosen."""

    def setUp(self):
        self.client = app_module.app.test_client()

    def test_acting_as_user_resolves_full_role_set_without_override(self):
        with self.client.session_transaction() as sess:
            sess["dev_user_key"] = 42
        with patch.object(app_module.db, "get_app_user_role_codes", return_value=["submitter", "treasury"]):
            with app_module.app.test_request_context("/"):
                from flask import session as flask_session
                flask_session["dev_user_key"] = 42
                self.assertEqual(app_module.current_roles(), {"submitter", "treasury"})

    def test_isolation_override_takes_precedence_over_acting_as(self):
        with patch.object(app_module.db, "get_app_user_role_codes", return_value=["submitter", "treasury"]):
            with app_module.app.test_request_context("/"):
                from flask import session as flask_session
                flask_session["dev_user_key"] = 42
                flask_session["role"] = "sam"
                self.assertEqual(app_module.current_roles(), {"sam"})

    def test_no_acting_as_and_no_override_yields_empty_role_set(self):
        with app_module.app.test_request_context("/"):
            self.assertEqual(app_module.current_roles(), set())


class ReassignmentUnionTests(unittest.TestCase):
    """Section 16: reassignment succeeds through ANY authorized role path
    without needing to "switch into" that role."""

    def test_business_admin_role_authorizes_reassignment_alongside_other_roles(self):
        txn = {"status": wf.STATUS_PENDING_APPROVER}
        wf.authorize_reassignment(role={"submitter", "business_admin"}, txn=txn)  # no raise

    def test_role_without_reassignment_permission_is_denied(self):
        txn = {"status": wf.STATUS_PENDING_APPROVER}
        with self.assertRaises(wf.UnauthorizedActionError):
            wf.authorize_reassignment(role={"submitter", "sam"}, txn=txn)


class SingleRoleBackwardCompatibilityTests(unittest.TestCase):
    """Section 34 #43: single-role behavior is unchanged — passing a bare
    string still works everywhere (existing call sites/tests untouched)."""

    def test_can_view_transaction_accepts_bare_string(self):
        txn = {"status": wf.STATUS_PENDING_APPROVER, "prepared_by_user_key": 11,
               "selected_approver_user_key": 11, "accounting_group_key": None}
        self.assertTrue(authorization.can_view_transaction(role="submitter", user_key=11, txn=txn))

    def test_authorize_action_accepts_bare_string(self):
        txn = {"status": wf.STATUS_PENDING_APPROVER, "selected_approver_user_key": 11}
        wf.authorize_action(role="sam", user_key=11, txn=txn, action=wf.ACTION_APPROVE)  # no raise

    def test_dashboard_scope_where_accepts_bare_string(self):
        where, params = app_module.db._dashboard_scope_where("submitter", 11, None)
        self.assertEqual(where, ["t.PreparedBy_User_Key = ?"])


if __name__ == "__main__":
    unittest.main()
