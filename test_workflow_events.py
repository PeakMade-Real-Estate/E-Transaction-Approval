"""
Tests for Batch 6 (compatibility-corrected): the WorkflowEvent business-event
contract. Existing/established Event_Type literals that Power Automate already
consumes are PRESERVED EXACTLY ("Submitted", "approve", "more_info",
"requester_respond", "reassign", "cancel", "treasury_initiated",
"treasury_released", "bank_release"); only genuinely NEW information (a new
actionable owner) gets a new, additive Event_Type (APPROVER_ASSIGNED,
CONTROLLER_ASSIGNED, VP_ASSIGNED, CFO_ASSIGNED, READY_FOR_TREASURY).

Run:  python -m unittest test_workflow_events -v
"""
import unittest
from unittest.mock import MagicMock, patch

import db
import workflow as wf


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
        "entity_classification": "Corporate",
    }
    base.update(overrides)
    return base


class EstablishedLiteralsPreservedTests(unittest.TestCase):
    """Part 1/2/15: confirm the exact pre-Batch-6 literals are still what gets written."""

    def test_established_event_constants_hold_unchanged_literals(self):
        self.assertEqual(wf.EVENT_REQUEST_SUBMITTED, "Submitted")
        self.assertEqual(wf.EVENT_APPROVAL, "approve")
        self.assertEqual(wf.EVENT_RFI_REQUESTED, "more_info")
        self.assertEqual(wf.EVENT_RFI_RESPONSE_SUBMITTED, "requester_respond")
        self.assertEqual(wf.EVENT_REASSIGNED, "reassign")
        self.assertEqual(wf.EVENT_CANCELLED, "cancel")
        self.assertEqual(wf.EVENT_TREASURY_INITIATED, "treasury_initiated")
        self.assertEqual(wf.EVENT_TREASURY_RELEASED, "treasury_released")
        self.assertEqual(wf.EVENT_BANK_RELEASED, "bank_release")

    def test_action_reassign_reverted_to_lowercase_reassign(self):
        self.assertEqual(wf.ACTION_REASSIGN, "reassign")

    def test_additive_events_are_new_uppercase_values_not_previously_used(self):
        self.assertEqual(wf.EVENT_APPROVER_ASSIGNED, "APPROVER_ASSIGNED")
        self.assertEqual(wf.EVENT_CONTROLLER_ASSIGNED, "CONTROLLER_ASSIGNED")
        self.assertEqual(wf.EVENT_VP_ASSIGNED, "VP_ASSIGNED")
        self.assertEqual(wf.EVENT_CFO_ASSIGNED, "CFO_ASSIGNED")
        self.assertEqual(wf.EVENT_READY_FOR_TREASURY, "READY_FOR_TREASURY")


class ApprovalEventsForActionTests(unittest.TestCase):
    """Part 4/17 #3-8: approval always writes "approve"; stage is inferred from
    From_Status, not Event_Type. The additive assignment/progression event is
    still stage-specific and still skips a redundant Controller step."""

    def test_3_4_approver_approval_writes_approve_then_controller_assigned(self):
        events = wf.events_for_action(from_status=wf.STATUS_PENDING_APPROVER, action=wf.ACTION_APPROVE,
                                       new_status=wf.STATUS_PENDING_CONTROLLER)
        self.assertEqual(events, [wf.EVENT_APPROVAL, wf.EVENT_CONTROLLER_ASSIGNED])

    def test_5_controller_approval_writes_approve_then_vp_assigned(self):
        events = wf.events_for_action(from_status=wf.STATUS_PENDING_CONTROLLER, action=wf.ACTION_APPROVE,
                                       new_status=wf.STATUS_PENDING_VP)
        self.assertEqual(events, [wf.EVENT_APPROVAL, wf.EVENT_VP_ASSIGNED])

    def test_6_vp_approval_writes_approve_then_cfo_assigned(self):
        events = wf.events_for_action(from_status=wf.STATUS_PENDING_VP, action=wf.ACTION_APPROVE,
                                       new_status=wf.STATUS_PENDING_CFO)
        self.assertEqual(events, [wf.EVENT_APPROVAL, wf.EVENT_CFO_ASSIGNED])

    def test_7_final_approval_writes_approve_then_ready_for_treasury_once(self):
        events = wf.events_for_action(from_status=wf.STATUS_PENDING_CFO, action=wf.ACTION_APPROVE,
                                       new_status=wf.STATUS_READY_FOR_TREASURY)
        self.assertEqual(events, [wf.EVENT_APPROVAL, wf.EVENT_READY_FOR_TREASURY])
        self.assertEqual(events.count(wf.EVENT_READY_FOR_TREASURY), 1)

    def test_every_approval_event_is_the_same_literal_regardless_of_stage(self):
        for from_status, new_status in [
            (wf.STATUS_PENDING_APPROVER, wf.STATUS_PENDING_CONTROLLER),
            (wf.STATUS_PENDING_CONTROLLER, wf.STATUS_PENDING_VP),
            (wf.STATUS_PENDING_VP, wf.STATUS_PENDING_CFO),
            (wf.STATUS_PENDING_CFO, wf.STATUS_READY_FOR_TREASURY),
        ]:
            events = wf.events_for_action(from_status=from_status, action=wf.ACTION_APPROVE, new_status=new_status)
            self.assertEqual(events[0], "approve")


class SamePersonEventTests(unittest.TestCase):
    """Part 12/17 #8: same Approver/Controller must not emit a redundant
    CONTROLLER_ASSIGNED — only the actual next stage's progression event."""

    def test_8_same_person_skips_controller_assigned(self):
        txn = make_txn(selected_approver_user_key=20, selected_controller_user_key=20,
                        requires_vp=True, vp_approver_user_key=40)
        new_status, new_owner, owner_role, satisfied = wf.determine_next_step(txn, wf.ACTION_APPROVE)
        self.assertEqual(new_status, wf.STATUS_PENDING_VP)
        self.assertEqual(satisfied, ["Approver", "Controller"])
        events = wf.events_for_action(from_status=txn["status"], action=wf.ACTION_APPROVE, new_status=new_status)
        self.assertEqual(events, [wf.EVENT_APPROVAL, wf.EVENT_VP_ASSIGNED])
        self.assertNotIn(wf.EVENT_CONTROLLER_ASSIGNED, events)

    def test_same_person_progresses_to_ready_for_treasury_when_no_vp_cfo(self):
        txn = make_txn(selected_approver_user_key=20, selected_controller_user_key=20)
        new_status, new_owner, owner_role, satisfied = wf.determine_next_step(txn, wf.ACTION_APPROVE)
        self.assertEqual(new_status, wf.STATUS_READY_FOR_TREASURY)
        events = wf.events_for_action(from_status=txn["status"], action=wf.ACTION_APPROVE, new_status=new_status)
        self.assertEqual(events, [wf.EVENT_APPROVAL, wf.EVENT_READY_FOR_TREASURY])


class RfiEventTests(unittest.TestCase):
    """Part 6/17 #9-10: RFI keeps its established literals."""

    def test_9_more_info_writes_more_info(self):
        events = wf.events_for_action(from_status=wf.STATUS_PENDING_APPROVER, action=wf.ACTION_MORE_INFO,
                                       new_status=wf.STATUS_MORE_INFO)
        self.assertEqual(events, ["more_info"])

    def test_10_requester_respond_writes_requester_respond(self):
        events = wf.events_for_action(from_status=wf.STATUS_MORE_INFO, action=wf.ACTION_REQUESTER_RESPOND,
                                       new_status=wf.STATUS_PENDING_APPROVER)
        self.assertEqual(events, ["requester_respond"])

    def test_rfi_events_never_include_assignment_events(self):
        events = wf.events_for_action(from_status=wf.STATUS_PENDING_APPROVER, action=wf.ACTION_MORE_INFO,
                                       new_status=wf.STATUS_MORE_INFO)
        for assignment_event in (wf.EVENT_APPROVER_ASSIGNED, wf.EVENT_CONTROLLER_ASSIGNED,
                                  wf.EVENT_VP_ASSIGNED, wf.EVENT_CFO_ASSIGNED):
            self.assertNotIn(assignment_event, events)


class ReassignmentCancellationEventTests(unittest.TestCase):
    """Part 7/8/17 #11-12: reassignment/cancellation keep their established literals."""

    def test_11_reassignment_literal_is_reassign(self):
        self.assertEqual(wf.ACTION_REASSIGN, "reassign")
        self.assertEqual(wf.EVENT_REASSIGNED, "reassign")

    def test_12_cancel_writes_cancel(self):
        events = wf.events_for_action(from_status=wf.STATUS_PENDING_APPROVER, action=wf.ACTION_CANCEL,
                                       new_status=wf.STATUS_CANCELLED)
        self.assertEqual(events, ["cancel"])


class TreasuryReleaseEventTests(unittest.TestCase):
    """Part 9/17 #13-18: Treasury/release events keep established literals;
    Corporate/Property completion semantics are unchanged."""

    def test_13_property_treasury_initiated_writes_treasury_initiated(self):
        events = wf.events_for_action(from_status=wf.STATUS_READY_FOR_TREASURY, action=wf.ACTION_TREASURY_INITIATED,
                                       new_status=wf.STATUS_AWAITING_RELEASE)
        self.assertEqual(events, ["treasury_initiated"])

    def test_14_corporate_treasury_released_writes_treasury_released(self):
        events = wf.events_for_action(from_status=wf.STATUS_READY_FOR_TREASURY, action=wf.ACTION_TREASURY_RELEASED,
                                       new_status=wf.STATUS_COMPLETED)
        self.assertEqual(events, ["treasury_released"])

    def test_15_property_bank_release_writes_bank_release(self):
        events = wf.events_for_action(from_status=wf.STATUS_AWAITING_RELEASE, action=wf.ACTION_BANK_RELEASE,
                                       new_status=wf.STATUS_COMPLETED)
        self.assertEqual(events, ["bank_release"])

    def test_16_corporate_treasury_released_completes_directly(self):
        txn = make_txn(status=wf.STATUS_READY_FOR_TREASURY, entity_classification="Corporate")
        new_status, new_owner, owner_role, satisfied = wf.determine_next_step(txn, wf.ACTION_TREASURY_RELEASED)
        self.assertEqual(new_status, wf.STATUS_COMPLETED)
        self.assertIsNone(new_owner)

    def test_17_property_bank_release_completes_directly(self):
        txn = make_txn(status=wf.STATUS_AWAITING_RELEASE, entity_classification="Property")
        new_status, new_owner, owner_role, satisfied = wf.determine_next_step(txn, wf.ACTION_BANK_RELEASE)
        self.assertEqual(new_status, wf.STATUS_COMPLETED)

    def test_18_no_separate_completed_event_generated(self):
        self.assertNotIn("COMPLETED", wf.EVENT_TYPE_LABELS)
        for action, from_status, new_status in [
            (wf.ACTION_TREASURY_RELEASED, wf.STATUS_READY_FOR_TREASURY, wf.STATUS_COMPLETED),
            (wf.ACTION_BANK_RELEASE, wf.STATUS_AWAITING_RELEASE, wf.STATUS_COMPLETED),
        ]:
            events = wf.events_for_action(from_status=from_status, action=action, new_status=new_status)
            self.assertNotIn("COMPLETED", events)
            self.assertEqual(len(events), 1)  # exactly one definitive release event, nothing appended


class AdvanceTransactionWorkflowTests(unittest.TestCase):
    """Confirms actual DB writes use the established literals + additive events,
    with stage-aware Decision text and correct Related_Assignment_Key linkage."""

    def test_approve_writes_established_literal_and_additive_assignment_event(self):
        fake_cursor = MagicMock()
        fake_cursor.rowcount = 1
        fake_cursor.fetchone.return_value = (77,)
        fake_conn = MagicMock()
        fake_conn.cursor.return_value = fake_cursor

        with patch.object(db, "get_connection", return_value=fake_conn):
            db.advance_transaction_workflow(
                1, from_status=wf.STATUS_PENDING_CONTROLLER, new_status=wf.STATUS_PENDING_VP,
                new_owner_user_key=40, actor_user_key=30, actor_role="Controller",
                action=wf.ACTION_APPROVE, workflow_role="VP", comments="Approved for VP review",
            )

        event_calls = [c for c in fake_cursor.execute.call_args_list if "INSERT INTO etransactions.WorkflowEvent" in c[0][0]]
        self.assertEqual(len(event_calls), 2)

        first_sql, first_params = event_calls[0][0]
        self.assertIn("approve", first_params)          # established literal, not CONTROLLER_APPROVED
        self.assertIn("Controller Approved", first_params)  # stage-aware Decision text derived from From_Status
        self.assertIn("Approved for VP review", first_params)

        second_sql, second_params = event_calls[1][0]
        self.assertIn(wf.EVENT_VP_ASSIGNED, second_params)
        self.assertIn(77, second_params)  # Related_Assignment_Key

    def test_conflict_writes_no_event_rows(self):
        fake_cursor = MagicMock()
        fake_cursor.rowcount = 0
        fake_conn = MagicMock()
        fake_conn.cursor.return_value = fake_cursor

        with patch.object(db, "get_connection", return_value=fake_conn):
            with self.assertRaises(db.WorkflowConflictError):
                db.advance_transaction_workflow(
                    1, from_status=wf.STATUS_PENDING_APPROVER, new_status=wf.STATUS_PENDING_CONTROLLER,
                    new_owner_user_key=20, actor_user_key=10, actor_role="Sr. Accounting Manager",
                    action=wf.ACTION_APPROVE, workflow_role="Controller",
                )
        self.assertEqual(fake_cursor.execute.call_count, 1)


class SubmissionEventTests(unittest.TestCase):
    """Part 5/17 #1-2: submission keeps "Submitted" and additionally writes APPROVER_ASSIGNED."""

    def test_1_2_submit_writes_submitted_then_approver_assigned(self):
        fake_cursor = MagicMock()
        fake_cursor.fetchone.side_effect = [(1,), (10,), (20,), (30,), (40,)]
        fake_cursor.fetchall.return_value = [(7, True, True, False, False)]
        fake_conn = MagicMock()
        fake_conn.cursor.return_value = fake_cursor

        data = {
            "request_id": "TXN-2026-9101",
            "prepared_by_key": 1, "approver_key": 2, "controller_key": 3,
            "bank_account_key": 4, "recv_payee_name": "Payee", "recv_contact_name": "",
            "recv_contact_email": "", "recv_contact_phone": "", "recv_bank_name": "Bank",
            "recv_account_name": "Acct", "recv_account_number": "123", "recv_routing_number": "456",
            "recv_bank_address": "",
            "amount": 100.0, "request_type": "ACH",
            "classification": "corporate", "property_dept": "Test Dept",
        }
        with patch.object(db, "get_connection", return_value=fake_conn):
            db.insert_transaction(data)

        event_calls = [c for c in fake_cursor.execute.call_args_list if "INSERT INTO [etransactions].[WorkflowEvent]" in c[0][0]]
        self.assertEqual(len(event_calls), 2)
        self.assertIn("Submitted", event_calls[0][0][1])
        self.assertIn(wf.EVENT_APPROVER_ASSIGNED, event_calls[1][0][1])


class LegacyDisplayCompatibilityTests(unittest.TestCase):
    """Part 17 #19: history/labels never fail for any Event_Type."""

    def test_19_all_established_and_additive_events_have_friendly_labels(self):
        for evt_type in ("Submitted", "approve", "more_info", "requester_respond", "reassign",
                          "cancel", "treasury_initiated", "treasury_released", "bank_release",
                          "mark_completed", wf.EVENT_APPROVER_ASSIGNED, wf.EVENT_CONTROLLER_ASSIGNED,
                          wf.EVENT_VP_ASSIGNED, wf.EVENT_CFO_ASSIGNED, wf.EVENT_READY_FOR_TREASURY,
                          "TotallyUnknownValue"):
            label = wf.friendly_event_label(evt_type)
            self.assertIsInstance(label, str)
            dot_type = db._EVENT_TYPE_MAP.get(evt_type.lower().replace(" ", "").replace("_", ""), "routed")
            self.assertIsInstance(dot_type, str)

    def test_approve_label_is_stage_specific_when_from_status_known(self):
        self.assertEqual(wf.friendly_event_label("approve", wf.STATUS_PENDING_APPROVER), "Approver Approved")
        self.assertEqual(wf.friendly_event_label("approve", wf.STATUS_PENDING_CONTROLLER), "Controller Approved")
        self.assertEqual(wf.friendly_event_label("approve", wf.STATUS_PENDING_VP), "VP Approved")
        self.assertEqual(wf.friendly_event_label("approve", wf.STATUS_PENDING_CFO), "CFO Approved")

    def test_approve_label_falls_back_generically_without_from_status(self):
        self.assertEqual(wf.friendly_event_label("approve"), "Approved")


if __name__ == "__main__":
    unittest.main()
