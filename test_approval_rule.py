"""
Tests for Batch 7 Part 7-14/17: ApprovalRule becomes the single source of
truth for Controller/VP/CFO routing (replacing hard-coded Python thresholds),
with deterministic, Decimal-based boundary resolution and safe failure when
the live configuration is missing or ambiguous. Routing/WorkflowEvent
behavior established in Batches 1-6 must not change except for sourcing the
rule from SQL.

Run:  python -m unittest test_approval_rule -v
"""
import unittest
from unittest.mock import MagicMock, patch

import db
import workflow as wf


def _rule_row(key, requires_approver=True, requires_controller=True, requires_vp=False, requires_cfo=False):
    return (key, requires_approver, requires_controller, requires_vp, requires_cfo)


class ResolveApprovalRuleBoundaryTests(unittest.TestCase):
    """Part 9: exact Decimal boundary behavior mirroring the live 4-rule configuration
    (0-249999.99 base, 250000-499999.99 base, 500000-999999.99 +VP, 1000000+ +VP+CFO)."""

    def _resolve(self, amount, rows):
        fake_cursor = MagicMock()
        fake_cursor.fetchall.return_value = rows
        fake_conn = MagicMock()
        fake_conn.cursor.return_value = fake_cursor
        with patch.object(db, "get_connection", return_value=fake_conn):
            return db.resolve_approval_rule(amount)

    def test_just_below_first_threshold(self):
        rule = self._resolve(249999.99, [_rule_row(7)])
        self.assertFalse(rule["requires_vp"])
        self.assertFalse(rule["requires_cfo"])

    def test_at_first_threshold(self):
        rule = self._resolve(250000.00, [_rule_row(8)])
        self.assertFalse(rule["requires_vp"])
        self.assertFalse(rule["requires_cfo"])

    def test_just_above_first_threshold(self):
        rule = self._resolve(250000.01, [_rule_row(8)])
        self.assertFalse(rule["requires_vp"])

    def test_just_below_vp_threshold(self):
        rule = self._resolve(499999.99, [_rule_row(8)])
        self.assertFalse(rule["requires_vp"])

    def test_at_vp_threshold(self):
        rule = self._resolve(500000.00, [_rule_row(9, requires_vp=True)])
        self.assertTrue(rule["requires_vp"])
        self.assertFalse(rule["requires_cfo"])

    def test_just_above_vp_threshold(self):
        rule = self._resolve(500000.01, [_rule_row(9, requires_vp=True)])
        self.assertTrue(rule["requires_vp"])

    def test_just_below_one_million(self):
        rule = self._resolve(999999.99, [_rule_row(9, requires_vp=True)])
        self.assertTrue(rule["requires_vp"])
        self.assertFalse(rule["requires_cfo"])

    def test_exactly_one_million_requires_vp_and_cfo(self):
        # Part 9: "At or above $1,000,000: VP required, CFO required" — never ">".
        rule = self._resolve(1000000.00, [_rule_row(10, requires_vp=True, requires_cfo=True)])
        self.assertTrue(rule["requires_vp"])
        self.assertTrue(rule["requires_cfo"])

    def test_just_above_one_million_requires_vp_and_cfo(self):
        rule = self._resolve(1000000.01, [_rule_row(10, requires_vp=True, requires_cfo=True)])
        self.assertTrue(rule["requires_vp"])
        self.assertTrue(rule["requires_cfo"])

    def test_uses_decimal_not_float_for_comparison(self):
        # A value that is famously imprecise in binary floating point.
        rule = self._resolve(0.1, [_rule_row(7)])
        self.assertEqual(rule["approval_rule_key"], 7)


class ResolveApprovalRuleFailureModeTests(unittest.TestCase):
    """Part 8/17 #17-18: fail safely rather than guessing."""

    def test_no_matching_rule_raises_configuration_error(self):
        fake_cursor = MagicMock()
        fake_cursor.fetchall.return_value = []
        fake_conn = MagicMock()
        fake_conn.cursor.return_value = fake_cursor
        with patch.object(db, "get_connection", return_value=fake_conn):
            with self.assertRaises(db.ApprovalRuleConfigurationError):
                db.resolve_approval_rule(100.0)

    def test_multiple_matching_rules_raises_configuration_error(self):
        fake_cursor = MagicMock()
        fake_cursor.fetchall.return_value = [_rule_row(7), _rule_row(8)]
        fake_conn = MagicMock()
        fake_conn.cursor.return_value = fake_cursor
        with patch.object(db, "get_connection", return_value=fake_conn):
            with self.assertRaises(db.ApprovalRuleConfigurationError):
                db.resolve_approval_rule(300000.0)


class ApprovalTierLabelTests(unittest.TestCase):
    """Part 12/15: display label is derived purely from resolved rule booleans."""

    def test_base_tier_label(self):
        self.assertEqual(wf.approval_tier_label(requires_vp=False, requires_cfo=False),
                          "Senior Accounting Manager / Assistant Controller")

    def test_vp_tier_label(self):
        self.assertEqual(wf.approval_tier_label(requires_vp=True, requires_cfo=False), "Vice President")

    def test_vp_cfo_tier_label(self):
        self.assertEqual(wf.approval_tier_label(requires_vp=True, requires_cfo=True), "Vice President + CFO")

    def test_cfo_alone_still_reports_vp_cfo_label(self):
        # requires_cfo implies requires_vp in the live rule set, but the label
        # function itself is defensive regardless of that invariant.
        self.assertEqual(wf.approval_tier_label(requires_vp=False, requires_cfo=True), "Vice President + CFO")


def make_txn(**overrides):
    base = {
        "transaction_key": 1,
        "status": wf.STATUS_PENDING_APPROVER,
        "prepared_by_user_key": 10,
        "selected_approver_user_key": 20,
        "selected_controller_user_key": 30,
        "vp_approver_user_key": None,
        "cfo_approver_user_key": None,
        "bank_releaser_user_key": None,
        "requires_vp": False,
        "requires_cfo": False,
        "requires_controller": True,
        "entity_classification": "Corporate",
    }
    base.update(overrides)
    return base


class RoutingUsesResolvedRuleTests(unittest.TestCase):
    """Part 10/11/14/17 #21-28: routing/WorkflowEvents follow the resolved rule."""

    def test_21_base_rule_reaches_ready_for_treasury_after_approver_and_controller(self):
        txn = make_txn(status=wf.STATUS_PENDING_APPROVER, requires_vp=False, requires_cfo=False)
        new_status, *_ = wf.determine_next_step(txn, wf.ACTION_APPROVE)
        self.assertEqual(new_status, wf.STATUS_PENDING_CONTROLLER)
        txn2 = make_txn(status=wf.STATUS_PENDING_CONTROLLER, requires_vp=False, requires_cfo=False)
        new_status2, *_ = wf.determine_next_step(txn2, wf.ACTION_APPROVE)
        self.assertEqual(new_status2, wf.STATUS_READY_FOR_TREASURY)

    def test_22_vp_required_rule_creates_vp_stage(self):
        txn = make_txn(status=wf.STATUS_PENDING_CONTROLLER, requires_vp=True, requires_cfo=False,
                        vp_approver_user_key=40)
        new_status, new_owner, owner_role, satisfied = wf.determine_next_step(txn, wf.ACTION_APPROVE)
        self.assertEqual(new_status, wf.STATUS_PENDING_VP)
        self.assertEqual(new_owner, 40)
        events = wf.events_for_action(from_status=txn["status"], action=wf.ACTION_APPROVE, new_status=new_status)
        self.assertEqual(events, [wf.EVENT_APPROVAL, wf.EVENT_VP_ASSIGNED])

    def test_23_cfo_required_rule_creates_cfo_stage(self):
        txn = make_txn(status=wf.STATUS_PENDING_VP, requires_vp=True, requires_cfo=True, cfo_approver_user_key=50)
        new_status, new_owner, owner_role, satisfied = wf.determine_next_step(txn, wf.ACTION_APPROVE)
        self.assertEqual(new_status, wf.STATUS_PENDING_CFO)
        self.assertEqual(new_owner, 50)
        events = wf.events_for_action(from_status=txn["status"], action=wf.ACTION_APPROVE, new_status=new_status)
        self.assertEqual(events, [wf.EVENT_APPROVAL, wf.EVENT_CFO_ASSIGNED])

    def test_24_vp_plus_cfo_rule_creates_both_in_order(self):
        txn_ctrl = make_txn(status=wf.STATUS_PENDING_CONTROLLER, requires_vp=True, requires_cfo=True, vp_approver_user_key=40)
        status_after_ctrl, *_ = wf.determine_next_step(txn_ctrl, wf.ACTION_APPROVE)
        self.assertEqual(status_after_ctrl, wf.STATUS_PENDING_VP)

        txn_vp = make_txn(status=wf.STATUS_PENDING_VP, requires_vp=True, requires_cfo=True, cfo_approver_user_key=50)
        status_after_vp, *_ = wf.determine_next_step(txn_vp, wf.ACTION_APPROVE)
        self.assertEqual(status_after_vp, wf.STATUS_PENDING_CFO)

        txn_cfo = make_txn(status=wf.STATUS_PENDING_CFO, requires_vp=True, requires_cfo=True)
        status_after_cfo, *_ = wf.determine_next_step(txn_cfo, wf.ACTION_APPROVE)
        self.assertEqual(status_after_cfo, wf.STATUS_READY_FOR_TREASURY)

    def test_25_same_approver_controller_still_works_with_resolved_rule(self):
        txn = make_txn(status=wf.STATUS_PENDING_APPROVER, selected_approver_user_key=20,
                        selected_controller_user_key=20, requires_vp=False, requires_cfo=False)
        new_status, new_owner, owner_role, satisfied = wf.determine_next_step(txn, wf.ACTION_APPROVE)
        self.assertEqual(new_status, wf.STATUS_READY_FOR_TREASURY)
        self.assertEqual(satisfied, ["Approver", "Controller"])

    def test_26_ready_for_treasury_occurs_exactly_once(self):
        txn = make_txn(status=wf.STATUS_PENDING_CFO, requires_vp=True, requires_cfo=True)
        new_status, *_ = wf.determine_next_step(txn, wf.ACTION_APPROVE)
        events = wf.events_for_action(from_status=txn["status"], action=wf.ACTION_APPROVE, new_status=new_status)
        self.assertEqual(events.count(wf.EVENT_READY_FOR_TREASURY), 1)

    def test_27_stage_stays_synchronized_with_status(self):
        self.assertEqual(wf.stage_for_status(wf.STATUS_PENDING_VP), "VP")
        self.assertEqual(wf.stage_for_status(wf.STATUS_PENDING_CFO), "CFO")
        self.assertEqual(wf.stage_for_status(wf.STATUS_READY_FOR_TREASURY), "Treasury")

    def test_28_established_event_literal_preserved_regardless_of_rule(self):
        for status in (wf.STATUS_PENDING_APPROVER, wf.STATUS_PENDING_CONTROLLER, wf.STATUS_PENDING_VP, wf.STATUS_PENDING_CFO):
            events = wf.events_for_action(from_status=status, action=wf.ACTION_APPROVE, new_status=wf.STATUS_READY_FOR_TREASURY)
            self.assertEqual(events[0], "approve")


class RequiresControllerSkipTests(unittest.TestCase):
    """Part 10/11: honoring ApprovalRule.Requires_Controller (currently always
    True in live data, but functional/future-proof)."""

    def test_requires_controller_false_skips_directly_to_next_stage(self):
        txn = make_txn(status=wf.STATUS_PENDING_APPROVER, requires_controller=False,
                        requires_vp=True, vp_approver_user_key=40)
        new_status, new_owner, owner_role, satisfied = wf.determine_next_step(txn, wf.ACTION_APPROVE)
        self.assertEqual(new_status, wf.STATUS_PENDING_VP)
        self.assertEqual(new_owner, 40)

    def test_requires_controller_defaults_true_when_key_absent(self):
        txn = make_txn(status=wf.STATUS_PENDING_APPROVER, selected_controller_user_key=30)
        txn.pop("requires_controller")
        new_status, *_ = wf.determine_next_step(txn, wf.ACTION_APPROVE)
        self.assertEqual(new_status, wf.STATUS_PENDING_CONTROLLER)  # unchanged default behavior


class InsertTransactionUsesResolvedRuleTests(unittest.TestCase):
    """Part 13/17 #19-20: ApprovalRule_Key/Requires_VP/Requires_CFO on ETransaction
    come from the resolved rule, never from caller-supplied data."""

    def test_stores_resolved_rule_key_and_flags_ignoring_any_caller_supplied_tier(self):
        fake_cursor = MagicMock()
        fake_cursor.fetchone.side_effect = [(1,), (10,), (20,), (30,), (40,)]
        fake_cursor.fetchall.return_value = [_rule_row(9, requires_vp=True, requires_cfo=False)]
        fake_conn = MagicMock()
        fake_conn.cursor.return_value = fake_cursor

        data = {
            "request_id": "TXN-2026-9201",
            "prepared_by_key": 1, "approver_key": 2, "controller_key": 3,
            "bank_account_key": 4, "recv_payee_name": "Payee", "recv_contact_name": "",
            "recv_contact_email": "", "recv_contact_phone": "", "recv_bank_name": "Bank",
            "recv_account_name": "Acct", "recv_account_number": "123", "recv_routing_number": "456",
            "recv_bank_address": "",
            "amount": 600000.0, "request_type": "ACH", "classification": "corporate",
        }
        with patch.object(db, "get_connection", return_value=fake_conn):
            db.insert_transaction(data)

        etxn_sql, etxn_params = next(
            c[0] for c in fake_cursor.execute.call_args_list if "INSERT INTO [etransactions].[ETransaction]" in c[0][0]
        )
        self.assertIn(9, etxn_params)     # resolved ApprovalRule_Key
        self.assertIn(1, etxn_params)     # Requires_VP = 1 (bit)
        self.assertIn("Vice President", etxn_params)  # tier label derived from resolved rule

    def test_ambiguous_or_missing_rule_prevents_transaction_creation(self):
        fake_cursor = MagicMock()
        fake_cursor.fetchall.return_value = []  # no matching rule
        fake_conn = MagicMock()
        fake_conn.cursor.return_value = fake_cursor
        data = {"amount": 100.0, "request_id": "TXN-2026-9202"}
        with patch.object(db, "get_connection", return_value=fake_conn):
            with self.assertRaises(db.ApprovalRuleConfigurationError):
                db.insert_transaction(data)
        # No ETransaction/Beneficiary INSERT should have been attempted.
        self.assertFalse(any("INSERT INTO" in c[0][0] for c in fake_cursor.execute.call_args_list))


if __name__ == "__main__":
    unittest.main()
