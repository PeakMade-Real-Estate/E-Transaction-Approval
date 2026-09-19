"""
Pure-function tests for the centralized transaction ownership/approval workflow
engine (workflow.py). No database connection is required — these tests exercise
determine_next_step()/authorize_action() directly against constructed transaction
dicts, matching the 16 scenarios required by the workflow implementation task.

Run:  python -m unittest test_workflow -v
"""

import unittest

import workflow as wf


def make_txn(**overrides):
    txn = {
        "status": wf.STATUS_PENDING_APPROVER,
        "prepared_by_user_key": 1,
        "selected_approver_user_key": 2,
        "selected_controller_user_key": 3,
        "vp_approver_user_key": None,
        "cfo_approver_user_key": None,
        "bank_releaser_user_key": None,
        "requires_vp": False,
        "requires_cfo": False,
        "entity_classification": "Corporate",
    }
    txn.update(overrides)
    return txn


class SubmissionTests(unittest.TestCase):
    def test_1_submit_starts_pending_approver_owned_by_approver(self):
        # Submission itself is app.py's responsibility (record["status"] = STATUS_PENDING_APPROVER,
        # CurrentOwner = approver_key) — verified here as the expected starting state contract.
        txn = make_txn()
        self.assertEqual(txn["status"], wf.STATUS_PENDING_APPROVER)
        self.assertEqual(txn["selected_approver_user_key"], 2)


class DifferentApproverControllerTests(unittest.TestCase):
    def test_2_approver_approves_routes_to_controller(self):
        txn = make_txn(status=wf.STATUS_PENDING_APPROVER)
        status, owner, role, satisfied = wf.determine_next_step(txn, wf.ACTION_APPROVE)
        self.assertEqual(status, wf.STATUS_PENDING_CONTROLLER)
        self.assertEqual(owner, 3)
        self.assertEqual(role, "Controller")
        self.assertEqual(satisfied, ["Approver"])

    def test_5_different_vp_required_controller_routes_to_vp(self):
        txn = make_txn(status=wf.STATUS_PENDING_CONTROLLER, requires_vp=True, vp_approver_user_key=9)
        status, owner, role, satisfied = wf.determine_next_step(txn, wf.ACTION_APPROVE)
        self.assertEqual(status, wf.STATUS_PENDING_VP)
        self.assertEqual(owner, 9)

    def test_6_controller_approves_vp_required(self):
        txn = make_txn(status=wf.STATUS_PENDING_CONTROLLER, requires_vp=True, vp_approver_user_key=9)
        status, owner, role, satisfied = wf.determine_next_step(txn, wf.ACTION_APPROVE)
        self.assertEqual(status, wf.STATUS_PENDING_VP)
        self.assertEqual(owner, 9)

    def test_7_vp_approves_cfo_required(self):
        txn = make_txn(status=wf.STATUS_PENDING_VP, requires_vp=True, requires_cfo=True,
                        vp_approver_user_key=9, cfo_approver_user_key=10)
        status, owner, role, satisfied = wf.determine_next_step(txn, wf.ACTION_APPROVE)
        self.assertEqual(status, wf.STATUS_PENDING_CFO)
        self.assertEqual(owner, 10)

    def test_8_final_approval_reaches_ready_for_treasury(self):
        txn = make_txn(status=wf.STATUS_PENDING_CFO, requires_vp=True, requires_cfo=True)
        status, owner, role, satisfied = wf.determine_next_step(txn, wf.ACTION_APPROVE)
        self.assertEqual(status, wf.STATUS_READY_FOR_TREASURY)
        self.assertIsNone(owner)


class SamePersonApproverControllerTests(unittest.TestCase):
    def test_3_same_person_skips_pending_controller(self):
        txn = make_txn(status=wf.STATUS_PENDING_APPROVER,
                        selected_approver_user_key=5, selected_controller_user_key=5)
        status, owner, role, satisfied = wf.determine_next_step(txn, wf.ACTION_APPROVE)
        self.assertEqual(status, wf.STATUS_READY_FOR_TREASURY)
        self.assertEqual(satisfied, ["Approver", "Controller"])
        self.assertNotEqual(status, wf.STATUS_PENDING_CONTROLLER)

    def test_4_same_person_plus_vp_routes_to_vp(self):
        txn = make_txn(status=wf.STATUS_PENDING_APPROVER,
                        selected_approver_user_key=5, selected_controller_user_key=5,
                        requires_vp=True, vp_approver_user_key=9)
        status, owner, role, satisfied = wf.determine_next_step(txn, wf.ACTION_APPROVE)
        self.assertEqual(status, wf.STATUS_PENDING_VP)
        self.assertEqual(owner, 9)
        self.assertEqual(satisfied, ["Approver", "Controller"])

    def test_4b_same_person_plus_vp_plus_cfo_routes_vp_then_cfo(self):
        txn = make_txn(status=wf.STATUS_PENDING_APPROVER,
                        selected_approver_user_key=5, selected_controller_user_key=5,
                        requires_vp=True, requires_cfo=True,
                        vp_approver_user_key=9, cfo_approver_user_key=10)
        status, owner, role, satisfied = wf.determine_next_step(txn, wf.ACTION_APPROVE)
        self.assertEqual(status, wf.STATUS_PENDING_VP)
        self.assertEqual(owner, 9)

        txn2 = make_txn(status=wf.STATUS_PENDING_VP, requires_vp=True, requires_cfo=True,
                         vp_approver_user_key=9, cfo_approver_user_key=10)
        status2, owner2, role2, satisfied2 = wf.determine_next_step(txn2, wf.ACTION_APPROVE)
        self.assertEqual(status2, wf.STATUS_PENDING_CFO)
        self.assertEqual(owner2, 10)


class PropertyTreasuryTests(unittest.TestCase):
    def test_9_property_no_vp_bank_releaser_is_controller(self):
        txn = make_txn(status=wf.STATUS_READY_FOR_TREASURY, entity_classification="Property",
                        selected_controller_user_key=3, vp_approver_user_key=None)
        status, owner, role, satisfied = wf.determine_next_step(txn, wf.ACTION_TREASURY_INITIATED)
        self.assertEqual(status, wf.STATUS_AWAITING_RELEASE)
        self.assertEqual(owner, 3)

    def test_10_property_with_vp_bank_releaser_is_vp(self):
        txn = make_txn(status=wf.STATUS_READY_FOR_TREASURY, entity_classification="Property",
                        selected_controller_user_key=3, vp_approver_user_key=9)
        status, owner, role, satisfied = wf.determine_next_step(txn, wf.ACTION_TREASURY_INITIATED)
        self.assertEqual(status, wf.STATUS_AWAITING_RELEASE)
        self.assertEqual(owner, 9)

    def test_11_cfo_never_becomes_bank_releaser(self):
        txn = make_txn(status=wf.STATUS_READY_FOR_TREASURY, entity_classification="Property",
                        selected_controller_user_key=3, vp_approver_user_key=None, cfo_approver_user_key=10)
        status, owner, role, satisfied = wf.determine_next_step(txn, wf.ACTION_TREASURY_INITIATED)
        self.assertNotEqual(owner, 10)
        self.assertEqual(owner, 3)


class CancellationTests(unittest.TestCase):
    def test_12_approver_cancels(self):
        txn = make_txn(status=wf.STATUS_PENDING_APPROVER)
        wf.authorize_action(role="sam", user_key=2, txn=txn, action=wf.ACTION_CANCEL)  # no raise
        status, owner, role, satisfied = wf.determine_next_step(txn, wf.ACTION_CANCEL)
        self.assertEqual(status, wf.STATUS_CANCELLED)
        self.assertIsNone(owner)

    def test_13_controller_cancels(self):
        txn = make_txn(status=wf.STATUS_PENDING_CONTROLLER)
        wf.authorize_action(role="controller", user_key=3, txn=txn, action=wf.ACTION_CANCEL)  # no raise
        status, owner, role, satisfied = wf.determine_next_step(txn, wf.ACTION_CANCEL)
        self.assertEqual(status, wf.STATUS_CANCELLED)
        self.assertIsNone(owner)

    def test_cancel_terminal_status_rejected(self):
        txn = make_txn(status=wf.STATUS_COMPLETED)
        with self.assertRaises(wf.UnauthorizedActionError):
            wf.authorize_action(role="sam", user_key=2, txn=txn, action=wf.ACTION_CANCEL)


class RequestMoreInfoTests(unittest.TestCase):
    def test_14_rfi_returns_ownership_to_requester_then_back_to_origin(self):
        txn = make_txn(status=wf.STATUS_PENDING_CONTROLLER)
        status, owner, role, satisfied = wf.determine_next_step(txn, wf.ACTION_MORE_INFO)
        self.assertEqual(status, wf.STATUS_MORE_INFO)
        self.assertEqual(owner, txn["prepared_by_user_key"])

        # Requester responds — must return to the exact stage that requested it, not restart.
        txn["rfi_origin_status"] = wf.STATUS_PENDING_CONTROLLER
        status2, owner2, role2, satisfied2 = wf.determine_next_step(txn, wf.ACTION_REQUESTER_RESPOND)
        self.assertEqual(status2, wf.STATUS_PENDING_CONTROLLER)
        self.assertEqual(owner2, txn["selected_controller_user_key"])

    def test_14b_approver_originated_rfi_returns_to_approver(self):
        txn = make_txn(status=wf.STATUS_PENDING_APPROVER)
        status, owner, role, satisfied = wf.determine_next_step(txn, wf.ACTION_MORE_INFO)
        self.assertEqual(status, wf.STATUS_MORE_INFO)
        self.assertEqual(owner, txn["prepared_by_user_key"])

        # db.get_last_rfi_origin_status() now correctly resolves this to Pending Approver
        # (previously always returned None due to an Event_Type string mismatch).
        txn["rfi_origin_status"] = wf.STATUS_PENDING_APPROVER
        status2, owner2, role2, satisfied2 = wf.determine_next_step(txn, wf.ACTION_REQUESTER_RESPOND)
        self.assertEqual(status2, wf.STATUS_PENDING_APPROVER)
        self.assertEqual(owner2, txn["selected_approver_user_key"])

    def test_vp_and_cfo_are_currently_authorized_to_request_more_info(self):
        # FLAG, NOT A REQUIREMENT: the signed spec only gives RFI to Approver/Controller,
        # but authorize_action() currently shares ACTION_MORE_INFO's stage_map with
        # ACTION_APPROVE, which also permits VP and CFO. This test documents that live
        # behavior rather than endorsing it — see workflow.authorize_action().
        txn_vp = make_txn(status=wf.STATUS_PENDING_VP, vp_approver_user_key=9)
        wf.authorize_action(role="vp", user_key=9, txn=txn_vp, action=wf.ACTION_MORE_INFO)  # no raise

        txn_cfo = make_txn(status=wf.STATUS_PENDING_CFO, cfo_approver_user_key=10)
        wf.authorize_action(role="cfo", user_key=10, txn=txn_cfo, action=wf.ACTION_MORE_INFO)  # no raise



class AuthorizationTests(unittest.TestCase):
    def test_15_unassigned_user_with_correct_role_is_rejected(self):
        txn = make_txn(status=wf.STATUS_PENDING_APPROVER, selected_approver_user_key=2)
        with self.assertRaises(wf.UnauthorizedActionError):
            wf.authorize_action(role="sam", user_key=999, txn=txn, action=wf.ACTION_APPROVE)

    def test_wrong_role_is_rejected(self):
        txn = make_txn(status=wf.STATUS_PENDING_APPROVER)
        with self.assertRaises(wf.UnauthorizedActionError):
            wf.authorize_action(role="controller", user_key=3, txn=txn, action=wf.ACTION_APPROVE)

    def test_dev_mode_relaxes_assignment_check(self):
        # user_key=None (local dev bypass) — role-only check still enforced, assignment relaxed.
        txn = make_txn(status=wf.STATUS_PENDING_APPROVER, selected_approver_user_key=2)
        wf.authorize_action(role="sam", user_key=None, txn=txn, action=wf.ACTION_APPROVE)  # no raise

    def test_vp_cannot_approve_at_wrong_stage(self):
        txn = make_txn(status=wf.STATUS_PENDING_APPROVER)
        with self.assertRaises(wf.UnauthorizedActionError):
            wf.authorize_action(role="vp", user_key=9, txn=txn, action=wf.ACTION_APPROVE)


class DuplicateActionTests(unittest.TestCase):
    def test_16_double_click_yields_single_transition(self):
        # determine_next_step is deterministic/pure — calling it twice with the SAME
        # unchanged txn yields the identical result. The actual double-click guard
        # (WorkflowConflictError on a stale from_status) lives in db.advance_transaction_workflow,
        # which requires a live connection — see db.py for the conditional UPDATE.
        txn = make_txn(status=wf.STATUS_PENDING_APPROVER)
        result1 = wf.determine_next_step(txn, wf.ACTION_APPROVE)
        result2 = wf.determine_next_step(txn, wf.ACTION_APPROVE)
        self.assertEqual(result1, result2)


class StageSynchronizationTests(unittest.TestCase):
    """
    Current_Workflow_Stage must never go stale relative to Current_Status.
    db.advance_transaction_workflow()/db.insert_transaction() both derive Stage
    from workflow.stage_for_status(new_status) in the SAME UPDATE/INSERT that
    writes Current_Status — these tests pin down that mapping (test #20-31's
    Status/Stage correspondence) independent of any live DB connection.
    """

    def test_20_submission_stage_is_approver(self):
        self.assertEqual(wf.stage_for_status(wf.STATUS_PENDING_APPROVER), "Approver")

    def test_21_approver_approval_moves_to_controller_stage(self):
        txn = make_txn(status=wf.STATUS_PENDING_APPROVER)
        new_status, owner, role, satisfied = wf.determine_next_step(txn, wf.ACTION_APPROVE)
        self.assertEqual(new_status, wf.STATUS_PENDING_CONTROLLER)
        self.assertEqual(wf.stage_for_status(new_status), "Controller")
        self.assertEqual(owner, txn["selected_controller_user_key"])

    def test_22_controller_approval_moves_to_vp_stage_when_required(self):
        txn = make_txn(status=wf.STATUS_PENDING_CONTROLLER, requires_vp=True, vp_approver_user_key=9)
        new_status, owner, role, satisfied = wf.determine_next_step(txn, wf.ACTION_APPROVE)
        self.assertEqual(new_status, wf.STATUS_PENDING_VP)
        self.assertEqual(wf.stage_for_status(new_status), "VP")
        self.assertEqual(owner, 9)

    def test_23_vp_approval_moves_to_cfo_stage_when_required(self):
        txn = make_txn(status=wf.STATUS_PENDING_VP, requires_vp=True, requires_cfo=True,
                        vp_approver_user_key=9, cfo_approver_user_key=10)
        new_status, owner, role, satisfied = wf.determine_next_step(txn, wf.ACTION_APPROVE)
        self.assertEqual(new_status, wf.STATUS_PENDING_CFO)
        self.assertEqual(wf.stage_for_status(new_status), "CFO")
        self.assertEqual(owner, 10)

    def test_24_final_approval_moves_to_treasury_stage(self):
        txn = make_txn(status=wf.STATUS_PENDING_CFO, requires_vp=True, requires_cfo=True)
        new_status, owner, role, satisfied = wf.determine_next_step(txn, wf.ACTION_APPROVE)
        self.assertEqual(new_status, wf.STATUS_READY_FOR_TREASURY)
        self.assertEqual(wf.stage_for_status(new_status), "Treasury")
        self.assertIsNone(owner)

    def test_25_rfi_moves_stage_and_owner_to_requester(self):
        txn = make_txn(status=wf.STATUS_PENDING_CONTROLLER)
        new_status, owner, role, satisfied = wf.determine_next_step(txn, wf.ACTION_MORE_INFO)
        self.assertEqual(new_status, wf.STATUS_MORE_INFO)
        self.assertEqual(wf.stage_for_status(new_status), "Requester")
        self.assertEqual(owner, txn["prepared_by_user_key"])

    def test_26_rfi_response_restores_prior_stage_and_owner(self):
        txn = make_txn(status=wf.STATUS_MORE_INFO, rfi_origin_status=wf.STATUS_PENDING_CONTROLLER)
        new_status, owner, role, satisfied = wf.determine_next_step(txn, wf.ACTION_REQUESTER_RESPOND)
        self.assertEqual(new_status, wf.STATUS_PENDING_CONTROLLER)
        self.assertEqual(wf.stage_for_status(new_status), "Controller")
        self.assertEqual(owner, txn["selected_controller_user_key"])

    def test_27_reassignment_stage_map_preserves_stage(self):
        # Reassignment never changes Current_Status, so Stage (derived from
        # Status) is unaffected by definition — only CurrentOwner_User_Key and
        # the relevant Selected*_User_Key FK change. Confirm the two reassignable
        # stages map to the expected stage labels.
        self.assertEqual(wf.REASSIGNMENT_STAGE_MAP[wf.STATUS_PENDING_APPROVER][1], "Approver")
        self.assertEqual(wf.stage_for_status(wf.STATUS_PENDING_APPROVER), "Approver")
        self.assertEqual(wf.REASSIGNMENT_STAGE_MAP[wf.STATUS_PENDING_CONTROLLER][1], "Controller")
        self.assertEqual(wf.stage_for_status(wf.STATUS_PENDING_CONTROLLER), "Controller")

    def test_28_corporate_treasury_released_stage(self):
        txn = make_txn(status=wf.STATUS_READY_FOR_TREASURY, entity_classification="Corporate")
        new_status, owner, role, satisfied = wf.determine_next_step(txn, wf.ACTION_TREASURY_RELEASED)
        self.assertEqual(new_status, wf.STATUS_TREASURY_RELEASED)
        self.assertIsNone(owner)
        self.assertEqual(wf.stage_for_status(new_status), "Treasury")

    def test_29_property_treasury_initiated_stage_is_bank_release(self):
        txn = make_txn(status=wf.STATUS_READY_FOR_TREASURY, entity_classification="Property",
                        selected_controller_user_key=3, vp_approver_user_key=9)
        new_status, owner, role, satisfied = wf.determine_next_step(txn, wf.ACTION_TREASURY_INITIATED)
        self.assertEqual(new_status, wf.STATUS_AWAITING_RELEASE)
        self.assertEqual(wf.stage_for_status(new_status), "Bank Release")
        self.assertEqual(owner, 9)

    def test_30_property_bank_release_completes(self):
        txn = make_txn(status=wf.STATUS_AWAITING_RELEASE, entity_classification="Property")
        new_status, owner, role, satisfied = wf.determine_next_step(txn, wf.ACTION_BANK_RELEASE)
        self.assertEqual(new_status, wf.STATUS_COMPLETED)
        self.assertIsNone(owner)
        self.assertEqual(wf.stage_for_status(new_status), "Completed")

    def test_31_cancellation_stage_is_cancelled(self):
        txn = make_txn(status=wf.STATUS_PENDING_APPROVER)
        new_status, owner, role, satisfied = wf.determine_next_step(txn, wf.ACTION_CANCEL)
        self.assertEqual(new_status, wf.STATUS_CANCELLED)
        self.assertIsNone(owner)
        self.assertEqual(wf.stage_for_status(new_status), "Cancelled")

    def test_corporate_and_property_paths_stay_mutually_exclusive(self):
        # Corporate can never reach Awaiting Bank Release; Property can never
        # reach Treasury Released — guards against the "3 sequential steps on
        # one transaction" regression called out in this batch's requirements.
        corporate_txn = make_txn(status=wf.STATUS_READY_FOR_TREASURY, entity_classification="Corporate")
        with self.assertRaises(wf.UnauthorizedActionError):
            wf.determine_next_step(corporate_txn, wf.ACTION_TREASURY_INITIATED)

        property_txn = make_txn(status=wf.STATUS_READY_FOR_TREASURY, entity_classification="Property")
        with self.assertRaises(wf.UnauthorizedActionError):
            wf.determine_next_step(property_txn, wf.ACTION_TREASURY_RELEASED)



class ConfigurationErrorTests(unittest.TestCase):
    def test_missing_vp_assignment_raises_configuration_error(self):
        txn = make_txn(status=wf.STATUS_PENDING_CONTROLLER, requires_vp=True, vp_approver_user_key=None)
        with self.assertRaises(wf.WorkflowConfigurationError):
            wf.determine_next_step(txn, wf.ACTION_APPROVE)


if __name__ == "__main__":
    unittest.main()
