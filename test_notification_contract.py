"""
Item 9 - notification data contract (isolated, read-only diagnosis aid).

Power Automate reads WorkflowEvent / WorkflowAssignment / ETransaction; it sends nothing from this app. These tests run
every workflow action through the REAL decision functions and the REAL SQL writers (against a recording fake cursor) and
pin down, per event: which user the database says is responsible afterward, which column identifies that user, and what
the database does NOT say. They document today's behavior; they change nothing and send nothing.

Run:  python -m unittest test_notification_contract -v
"""
import unittest
import external_guard  # noqa: F401  (must precede app/db/sharepoint imports)
from unittest.mock import MagicMock, patch

import db
import workflow as wf

REQ, APPR, CTRL, VP, CFO, REL = 10, 20, 30, 40, 50, 60


def _txn(**over):
    base = {
        "transaction_key": 1, "request_id": "TXN-2026-9001", "status": wf.STATUS_PENDING_APPROVER,
        "prepared_by_user_key": REQ, "selected_approver_user_key": APPR, "selected_controller_user_key": CTRL,
        "vp_approver_user_key": VP, "cfo_approver_user_key": CFO, "current_owner_user_key": APPR,
        "bank_releaser_user_key": None, "requires_vp": False, "requires_cfo": False, "requires_controller": True,
        "amount": 1000.0, "entity_classification": "Corporate", "accounting_group_key": None,
    }
    base.update(over)
    return base


class _Written:
    """What one workflow transition wrote: the new owner on ETransaction, the new assignment row, and the events."""

    def __init__(self, statements):
        self.update = next(p for s, p in statements if "UPDATE etransactions.ETransaction" in s)
        assignment = [p for s, p in statements if "INSERT INTO etransactions.WorkflowAssignment" in s]
        self.assignment = assignment[0] if assignment else None
        self.events = [p for s, p in statements if "INSERT INTO etransactions.WorkflowEvent" in s]
        self.statements = [s for s, _ in statements]

    @property
    def owner(self):
        return self.update[2]

    @property
    def event_types(self):
        return [e[3] for e in self.events]

    def related(self, event_type):
        return [e[9] for e in self.events if e[3] == event_type][0]


def _run(action, txn, *, origin=None, actor=APPR):
    """Run `action` through workflow.determine_next_step() and db.advance_transaction_workflow(); return what was written."""
    txn = dict(txn)
    if origin:
        txn["rfi_origin_status"] = origin
    new_status, new_owner, role, _ = wf.determine_next_step(txn, action)
    cur = MagicMock()
    cur.rowcount = 1
    cur.fetchone.return_value = (77,)
    conn = MagicMock()
    conn.cursor.return_value = cur
    with patch.object(db, "get_connection", return_value=conn):
        db.advance_transaction_workflow(
            txn["transaction_key"], from_status=txn["status"], new_status=new_status, new_owner_user_key=new_owner,
            actor_user_key=actor, actor_role="role", action=action, workflow_role=role, comments="c")
    conn.commit.assert_called_once()
    return _Written([(c.args[0], c.args[1] if len(c.args) > 1 else None) for c in cur.execute.call_args_list])


class ApprovalProgressionContractTests(unittest.TestCase):
    def test_approver_approval_names_the_controller_through_the_linked_assignment(self):
        w = _run(wf.ACTION_APPROVE, _txn())
        self.assertEqual(w.event_types, ["approve", "CONTROLLER_ASSIGNED"])
        self.assertEqual(w.owner, CTRL)
        self.assertEqual(w.assignment[1:3], ["Controller", CTRL])
        self.assertEqual((w.related("approve"), w.related("CONTROLLER_ASSIGNED")), (None, 77))

    def test_controller_approval_names_the_vp_and_the_vp_must_already_be_on_the_transaction(self):
        w = _run(wf.ACTION_APPROVE, _txn(status=wf.STATUS_PENDING_CONTROLLER, requires_vp=True))
        self.assertEqual((w.event_types, w.owner, w.assignment[1:3]), (["approve", "VP_ASSIGNED"], VP, ["VP", VP]))
        with self.assertRaises(wf.WorkflowConfigurationError):          # no VP on the row: the approval fails, nothing is written
            wf.determine_next_step(_txn(status=wf.STATUS_PENDING_CONTROLLER, requires_vp=True, vp_approver_user_key=None), wf.ACTION_APPROVE)

    def test_vp_approval_names_the_cfo_and_the_cfo_must_already_be_on_the_transaction(self):
        w = _run(wf.ACTION_APPROVE, _txn(status=wf.STATUS_PENDING_VP, requires_vp=True, requires_cfo=True))
        self.assertEqual((w.event_types, w.owner, w.assignment[1:3]), (["approve", "CFO_ASSIGNED"], CFO, ["CFO", CFO]))
        with self.assertRaises(wf.WorkflowConfigurationError):
            wf.determine_next_step(_txn(status=wf.STATUS_PENDING_VP, requires_cfo=True, cfo_approver_user_key=None), wf.ACTION_APPROVE)

    def test_final_approval_names_nobody_so_treasury_recipients_cannot_come_from_these_rows(self):
        w = _run(wf.ACTION_APPROVE, _txn(status=wf.STATUS_PENDING_CONTROLLER))
        self.assertEqual(w.event_types, ["approve", "READY_FOR_TREASURY"])
        self.assertIsNone(w.owner)
        self.assertIsNone(w.assignment)
        self.assertEqual((w.related("approve"), w.related("READY_FOR_TREASURY")), (None, None))

    def test_the_same_person_as_approver_and_controller_skips_the_controller_event(self):
        w = _run(wf.ACTION_APPROVE, _txn(selected_controller_user_key=APPR))
        self.assertEqual(w.event_types, ["approve", "READY_FOR_TREASURY"])
        self.assertNotIn("CONTROLLER_ASSIGNED", w.event_types)
        self.assertIsNone(w.owner)

    def test_the_vp_and_cfo_columns_are_written_only_by_a_transition_that_resolved_an_assignee(self):
        w = _run(wf.ACTION_APPROVE, _txn())
        text = " ".join(w.statements)
        self.assertNotIn("VPApprover_User_Key", text)
        self.assertNotIn("CFOApprover_User_Key", text)


class RfiContractTests(unittest.TestCase):
    def test_more_info_makes_the_requester_the_owner_but_the_event_does_not_point_at_that_assignment(self):
        w = _run(wf.ACTION_MORE_INFO, _txn(), actor=APPR)
        self.assertEqual(w.event_types, ["more_info"])
        self.assertEqual((w.owner, w.assignment[1:3]), (REQ, ["Requester", REQ]))
        self.assertIsNone(w.related("more_info"))                      # recipient = ETransaction owner / PreparedBy, not the event key

    def test_requester_respond_returns_ownership_to_the_reviewer_who_asked(self):
        for origin, owner, role in ((wf.STATUS_PENDING_APPROVER, APPR, "Approver"), (wf.STATUS_PENDING_CONTROLLER, CTRL, "Controller"),
                                    (wf.STATUS_PENDING_VP, VP, "VP"), (wf.STATUS_PENDING_CFO, CFO, "CFO")):
            with self.subTest(origin=origin):
                w = _run(wf.ACTION_REQUESTER_RESPOND, _txn(status=wf.STATUS_MORE_INFO, current_owner_user_key=REQ), origin=origin, actor=REQ)
                self.assertEqual(w.event_types, ["requester_respond"])
                self.assertEqual((w.owner, w.assignment[1:3]), (owner, [role, owner]))
                self.assertIsNone(w.related("requester_respond"))
                self.assertNotIn("APPROVER_ASSIGNED", w.event_types)    # no assignment event: a flow keyed on it is never triggered by a response


class TerminalAndTreasuryContractTests(unittest.TestCase):
    def test_cancel_leaves_no_owner_and_no_assignment_to_notify(self):
        w = _run(wf.ACTION_CANCEL, _txn(), actor=REQ)
        self.assertEqual(w.event_types, ["cancel"])
        self.assertIsNone(w.owner)
        self.assertIsNone(w.assignment)
        self.assertIn("UPDATE etransactions.WorkflowAssignment SET Is_Current = 0", " ".join(w.statements))   # every assignment is closed

    def test_property_treasury_initiated_names_the_bank_releaser_vp_else_controller(self):
        with_vp = _run(wf.ACTION_TREASURY_INITIATED, _txn(status=wf.STATUS_READY_FOR_TREASURY, entity_classification="Property"), actor=REL)
        self.assertEqual((with_vp.event_types, with_vp.owner, with_vp.assignment[1:3]), (["treasury_initiated"], VP, ["Bank Releaser", VP]))
        self.assertEqual(with_vp.related("treasury_initiated"), 77)
        without_vp = _run(wf.ACTION_TREASURY_INITIATED,
                          _txn(status=wf.STATUS_READY_FOR_TREASURY, entity_classification="Property", vp_approver_user_key=None), actor=REL)
        self.assertEqual(without_vp.owner, CTRL)

    def test_corporate_release_and_property_bank_release_complete_with_no_recipient_columns(self):
        for action, txn in ((wf.ACTION_TREASURY_RELEASED, _txn(status=wf.STATUS_READY_FOR_TREASURY)),
                            (wf.ACTION_BANK_RELEASE, _txn(status=wf.STATUS_AWAITING_RELEASE, entity_classification="Property"))):
            with self.subTest(action=action):
                w = _run(action, txn, actor=REL)
                self.assertEqual(w.event_types, [action])
                self.assertEqual((w.owner, w.assignment, w.events[0][6]), (None, None, wf.STATUS_COMPLETED))
                self.assertIsNone(w.related(action))


class ReassignmentContractTests(unittest.TestCase):
    def test_reassign_points_the_event_at_the_new_assignment_and_updates_the_owner_together(self):
        cur = MagicMock()
        cur.rowcount = 1
        cur.fetchone.return_value = (77,)
        conn = MagicMock()
        conn.cursor.return_value = cur
        with patch.object(db, "get_connection", return_value=conn):
            db.reassign_transaction_participant(
                1, expected_status=wf.STATUS_PENDING_APPROVER, stage_field="selected_approver_user_key", workflow_role="Approver",
                prior_user_key=APPR, new_user_key=99, actor_user_key=CTRL, actor_role="Controller", event_type=wf.ACTION_REASSIGN,
                decision="Reassigned", reason="Out of office")
        written = _Written([(c.args[0], c.args[1] if len(c.args) > 1 else None) for c in cur.execute.call_args_list])
        self.assertEqual(written.event_types, ["reassign"])
        self.assertEqual(written.related("reassign"), 77)
        self.assertEqual(written.assignment[1:3], ["Approver", 99])
        self.assertIn("SelectedApprover_User_Key = ?, CurrentOwner_User_Key = ?", written.statements[0])
        self.assertEqual(written.events[0][5:7], [wf.STATUS_PENDING_APPROVER, wf.STATUS_PENDING_APPROVER])
        conn.commit.assert_called_once()


class ConsistencyAndTimingContractTests(unittest.TestCase):
    def test_one_commit_writes_status_owner_assignment_and_every_event_with_one_shared_timestamp(self):
        w = _run(wf.ACTION_APPROVE, _txn())
        stamps = {e[7] for e in w.events}
        self.assertEqual(len(stamps), 1)                                 # identical Event_DateTime: order by WorkflowEvent_Key, not time
        self.assertEqual(w.update[3], next(iter(stamps)))
        self.assertEqual({(e[5], e[6]) for e in w.events}, {(wf.STATUS_PENDING_APPROVER, wf.STATUS_PENDING_CONTROLLER)})

    def test_the_new_assignment_user_always_equals_the_owner_written_in_the_same_statement_set(self):
        cases = ((wf.ACTION_APPROVE, _txn(), None), (wf.ACTION_MORE_INFO, _txn(), None),
                 (wf.ACTION_REQUESTER_RESPOND, _txn(status=wf.STATUS_MORE_INFO), wf.STATUS_PENDING_CONTROLLER),
                 (wf.ACTION_TREASURY_INITIATED, _txn(status=wf.STATUS_READY_FOR_TREASURY, entity_classification="Property"), None))
        for action, txn, origin in cases:
            with self.subTest(action=action):
                w = _run(action, txn, origin=origin)
                self.assertEqual(w.assignment[2], w.owner)

    def test_a_conflicting_transition_writes_no_event_for_a_flow_to_react_to(self):
        cur = MagicMock()
        cur.rowcount = 0
        conn = MagicMock()
        conn.cursor.return_value = cur
        with patch.object(db, "get_connection", return_value=conn), self.assertRaises(db.WorkflowConflictError):
            db.advance_transaction_workflow(1, from_status=wf.STATUS_PENDING_APPROVER, new_status=wf.STATUS_PENDING_CONTROLLER,
                                            new_owner_user_key=CTRL, actor_user_key=APPR, actor_role="r",
                                            action=wf.ACTION_APPROVE, workflow_role="Controller")
        self.assertEqual(cur.execute.call_count, 1)
        conn.rollback.assert_called_once()

    def test_the_event_literals_the_flow_switches_on_are_unchanged(self):
        self.assertEqual(
            [wf.EVENT_REQUEST_SUBMITTED, wf.EVENT_APPROVAL, wf.EVENT_RFI_REQUESTED, wf.EVENT_RFI_RESPONSE_SUBMITTED, wf.EVENT_REASSIGNED,
             wf.EVENT_CANCELLED, wf.EVENT_TREASURY_INITIATED, wf.EVENT_TREASURY_RELEASED, wf.EVENT_BANK_RELEASED,
             wf.EVENT_APPROVER_ASSIGNED, wf.EVENT_CONTROLLER_ASSIGNED, wf.EVENT_VP_ASSIGNED, wf.EVENT_CFO_ASSIGNED, wf.EVENT_READY_FOR_TREASURY],
            ["Submitted", "approve", "more_info", "requester_respond", "reassign", "cancel", "treasury_initiated", "treasury_released",
             "bank_release", "APPROVER_ASSIGNED", "CONTROLLER_ASSIGNED", "VP_ASSIGNED", "CFO_ASSIGNED", "READY_FOR_TREASURY"])


class ExternalIsolationTests(unittest.TestCase):
    def test_no_external_access_was_attempted(self):
        self.assertEqual(external_guard.state.blocked, [])


if __name__ == "__main__":
    unittest.main()
