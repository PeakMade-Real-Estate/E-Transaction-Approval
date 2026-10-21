"""
Automatic VP / CFO assignment.

VP: the one active VP whose AppUser.Team matches the transaction (CA = Corporate, PA = Property). CFO: the one active CFO,
company-wide. Both are looked up only when the approval being recorded enters that stage, are written together with the
workflow transition, and never replace an existing assignment. Zero or several candidates stop the approval with a
configuration error; nothing falls back to the Controller, an alphabetical pick or the unscoped test users.

Mock-only: `external_guard` (imported first) makes SQL/SharePoint/network inert.

Run:  python -m unittest test_vp_cfo_assignment -v
"""
import unittest
import external_guard  # noqa: F401  (must precede app/db/sharepoint imports)
from unittest.mock import MagicMock, patch

import app as app_module
import db
import workflow as wf
from test_notification_contract import APPR, CTRL, REQ, _Written, _run, _txn

NICOLE, SEUNG, WENDY, OTHER_CFO = 19, 27, 48, 17      # keys only label the roles; the code never knows them
VPS = {"CA": [NICOLE], "PA": [SEUNG]}


def _find_vp(team):
    return VPS.get(team, [])


def _resolve(txn, vps=None, cfos=None):
    calls = {"vp": [], "cfo": 0}

    def find_vp(team):
        calls["vp"].append(team)
        return (VPS if vps is None else vps).get(team, [])

    def find_cfo():
        calls["cfo"] += 1
        return [WENDY] if cfos is None else cfos

    return wf.resolve_missing_approvers(txn, find_vp_candidates=find_vp, find_cfo_candidates=find_cfo), calls


def _vp_txn(**over):
    base = {"requires_vp": True, "vp_approver_user_key": None, "cfo_approver_user_key": None}
    base.update(over)
    return _txn(**base)


def _step(txn, role, user_key, **resolver):
    """One approval exactly as the route performs it: authorize, resolve the missing assignee, route, apply."""
    wf.authorize_action(role=role, user_key=user_key, txn=txn, action=wf.ACTION_APPROVE)
    txn.update(_resolve(txn, **resolver)[0])
    status, owner, owner_role, satisfied = wf.determine_next_step(txn, wf.ACTION_APPROVE)
    txn.update(status=status, current_owner_user_key=owner)
    return status, owner, owner_role, satisfied


# ═══ VP / CFO selection ═════════════════════════════════════════════════════
class VpSelectionTests(unittest.TestCase):
    def test_1_a_corporate_transaction_gets_the_ca_vp(self):
        resolved, calls = _resolve(_vp_txn(status=wf.STATUS_PENDING_CONTROLLER, entity_classification="Corporate"))
        self.assertEqual((resolved, calls["vp"]), ({"vp_approver_user_key": NICOLE}, ["CA"]))

    def test_2_a_property_transaction_gets_the_pa_vp(self):
        resolved, calls = _resolve(_vp_txn(status=wf.STATUS_PENDING_CONTROLLER, entity_classification="Property"))
        self.assertEqual((resolved, calls["vp"]), ({"vp_approver_user_key": SEUNG}, ["PA"]))

    def test_the_classification_is_matched_case_insensitively_and_missing_group_data_is_irrelevant(self):
        for text in ("corporate", "CORPORATE", " Corporate "):
            self.assertEqual(_resolve(_vp_txn(status=wf.STATUS_PENDING_CONTROLLER, entity_classification=text,
                                              accounting_group_key=None))[0], {"vp_approver_user_key": NICOLE})

    def test_4_duplicate_role_rows_for_one_user_are_one_candidate(self):
        self.assertEqual(_resolve(_vp_txn(status=wf.STATUS_PENDING_CONTROLLER), vps={"CA": [NICOLE, NICOLE, NICOLE]})[0],
                         {"vp_approver_user_key": NICOLE})

    def test_6_a_missing_vp_is_a_clear_configuration_error(self):
        with self.assertRaisesRegex(wf.WorkflowConfigurationError, r"No eligible VP on the CA team"):
            _resolve(_vp_txn(status=wf.STATUS_PENDING_CONTROLLER), vps={"CA": []})

    def test_several_distinct_vps_are_an_error_never_an_alphabetical_or_random_pick(self):
        with self.assertRaisesRegex(wf.WorkflowConfigurationError, r"More than one eligible VP on the PA team was found \(2 distinct users\)"):
            _resolve(_vp_txn(status=wf.STATUS_PENDING_CONTROLLER, entity_classification="Property"), vps={"PA": [SEUNG, 99]})

    def test_7_an_unknown_classification_fails_and_never_defaults_to_ca(self):
        for text in ("", None, "Other", "AM"):
            with self.subTest(text=text), self.assertRaisesRegex(wf.WorkflowConfigurationError, "no Corporate or Property classification"):
                _resolve(_vp_txn(status=wf.STATUS_PENDING_CONTROLLER, entity_classification=text))

    def test_8_the_lookup_is_strict_by_team_so_the_unscoped_test_vp_cannot_be_a_fallback(self):
        cur = MagicMock()
        cur.fetchall.return_value = [(NICOLE,), (NICOLE,)]
        conn = MagicMock()
        conn.cursor.return_value = cur
        with patch.object(db, "get_connection", return_value=conn):
            keys = db.get_eligible_vp_user_keys("CA")
        sql, params = cur.execute.call_args.args
        self.assertEqual((keys, params), ([NICOLE, NICOLE], ["vp", "CA"]))
        self.assertIn("SELECT DISTINCT u.User_Key", sql)
        for required in ("r.Role_Code = ?", "r.Is_Active = 1", "u.Active_Status = 1", "u.Team = ?",
                         "r.Effective_Start_Date IS NULL OR", "r.Effective_End_Date IS NULL OR"):
            self.assertIn(required, sql)
        self.assertNotRegex(sql, r"Team IS NULL|Team = ''|OR u\.Team")        # a blank Team is never eligible
        with self.assertRaises(ValueError):
            db.get_eligible_vp_user_keys("  ")

    def test_an_existing_assignment_is_never_replaced_or_even_looked_up_again(self):
        resolved, calls = _resolve(_vp_txn(status=wf.STATUS_PENDING_CONTROLLER, vp_approver_user_key=NICOLE), vps={"CA": [SEUNG]})
        self.assertEqual((resolved, calls["vp"]), ({}, []))

    def test_no_lookup_happens_unless_the_approval_enters_the_stage(self):
        for txn in (_txn(), _txn(status=wf.STATUS_PENDING_CONTROLLER), _vp_txn(),            # approver stage, distinct controller
                    _vp_txn(status=wf.STATUS_PENDING_VP, vp_approver_user_key=NICOLE),
                    _txn(status=wf.STATUS_PENDING_CFO), _txn(status=wf.STATUS_MORE_INFO), _txn(status=wf.STATUS_READY_FOR_TREASURY)):
            with self.subTest(status=txn["status"]):
                resolved, calls = _resolve(txn)
                self.assertEqual((resolved, calls["vp"], calls["cfo"]), ({}, [], 0))

    def test_classification_does_not_change_the_other_inputs_same_day_ach_is_irrelevant(self):
        txn = _vp_txn(status=wf.STATUS_PENDING_CONTROLLER, request_type="Same Day ACH", urgent=True)
        self.assertEqual(_resolve(txn)[0], {"vp_approver_user_key": NICOLE})


class CfoSelectionTests(unittest.TestCase):
    def _cfo_txn(self, **over):
        return _vp_txn(status=wf.STATUS_PENDING_VP, requires_cfo=True, vp_approver_user_key=NICOLE, **over)

    def test_3_the_cfo_is_the_one_distinct_active_cfo_for_both_classifications(self):
        for classification in ("Corporate", "Property"):
            with self.subTest(classification=classification):
                resolved, calls = _resolve(self._cfo_txn(entity_classification=classification))
                self.assertEqual((resolved, calls["cfo"], calls["vp"]), ({"cfo_approver_user_key": WENDY}, 1, []))

    def test_4_wendys_several_accounting_group_rows_are_one_candidate(self):
        self.assertEqual(_resolve(self._cfo_txn(), cfos=[WENDY, WENDY])[0], {"cfo_approver_user_key": WENDY})

    def test_5_two_distinct_active_cfos_fail_safely(self):
        with self.assertRaisesRegex(wf.WorkflowConfigurationError, r"More than one eligible CFO was found \(2 distinct users\)"):
            _resolve(self._cfo_txn(), cfos=[WENDY, OTHER_CFO])

    def test_no_cfo_fails_safely(self):
        with self.assertRaisesRegex(wf.WorkflowConfigurationError, "No eligible CFO"):
            _resolve(self._cfo_txn(), cfos=[])

    def test_the_cfo_lookup_never_involves_a_team(self):
        cur = MagicMock()
        cur.fetchall.return_value = [(WENDY,), (WENDY,)]
        conn = MagicMock()
        conn.cursor.return_value = cur
        with patch.object(db, "get_connection", return_value=conn):
            keys = db.get_eligible_cfo_user_keys()
        sql, params = cur.execute.call_args.args
        self.assertEqual((keys, params), ([WENDY, WENDY], ["cfo"]))
        self.assertNotIn("Team", sql)

    def test_a_transaction_that_does_not_need_a_cfo_never_triggers_the_lookup(self):
        resolved, calls = _resolve(_vp_txn(status=wf.STATUS_PENDING_VP, vp_approver_user_key=NICOLE), cfos=[WENDY, OTHER_CFO])
        self.assertEqual((resolved, calls["cfo"]), ({}, 0))


# ═══ Approval chains: every required stage is a separate action ═══════════════
class ApprovalChainTests(unittest.TestCase):
    def test_9_10_an_approver_who_is_also_the_vp_approves_twice_with_the_controller_in_between(self):
        txn = _vp_txn(selected_approver_user_key=NICOLE, selected_controller_user_key=CTRL, current_owner_user_key=NICOLE)
        self.assertEqual(_step(txn, "sam", NICOLE)[:3], (wf.STATUS_PENDING_CONTROLLER, CTRL, "Controller"))
        with self.assertRaises(wf.UnauthorizedActionError):                          # her Approver decision does not cover the Controller stage
            wf.authorize_action(role={"sam", "vp"}, user_key=NICOLE, txn=txn, action=wf.ACTION_APPROVE)
        self.assertEqual(_step(txn, "controller", CTRL)[:3], (wf.STATUS_PENDING_VP, NICOLE, "VP"))
        self.assertEqual(txn["vp_approver_user_key"], NICOLE)
        self.assertEqual(_step(txn, "vp", NICOLE)[:2], (wf.STATUS_READY_FOR_TREASURY, None))   # the second, separate approval

    def test_11_an_approver_who_is_also_the_controller_still_gets_the_existing_combined_approval(self):
        txn = _txn(selected_controller_user_key=APPR)
        status, owner, _, satisfied = _step(txn, "sam", APPR)
        self.assertEqual((status, satisfied), (wf.STATUS_READY_FOR_TREASURY, ["Approver", "Controller"]))

    def test_12_a_combined_approver_controller_who_is_also_the_vp_still_needs_a_separate_vp_action(self):
        txn = _vp_txn(selected_approver_user_key=NICOLE, selected_controller_user_key=NICOLE, current_owner_user_key=NICOLE)
        status, owner, role, satisfied = _step(txn, "sam", NICOLE)
        self.assertEqual((status, owner, role, satisfied), (wf.STATUS_PENDING_VP, NICOLE, "VP", ["Approver", "Controller"]))
        self.assertEqual(_step(txn, "vp", NICOLE)[0], wf.STATUS_READY_FOR_TREASURY)

    def test_13_a_cfo_level_transaction_goes_approver_controller_vp_cfo_treasury(self):
        txn = _vp_txn(requires_cfo=True, selected_approver_user_key=NICOLE, selected_controller_user_key=CTRL, current_owner_user_key=NICOLE)
        trail = [_step(txn, "sam", NICOLE)[:2], _step(txn, "controller", CTRL)[:2], _step(txn, "vp", NICOLE)[:2], _step(txn, "cfo", WENDY)[:2]]
        self.assertEqual(trail, [(wf.STATUS_PENDING_CONTROLLER, CTRL), (wf.STATUS_PENDING_VP, NICOLE),
                                 (wf.STATUS_PENDING_CFO, WENDY), (wf.STATUS_READY_FOR_TREASURY, None)])
        self.assertEqual((txn["vp_approver_user_key"], txn["cfo_approver_user_key"]), (NICOLE, WENDY))

    def test_the_vp_is_looked_up_at_the_controller_approval_and_the_cfo_at_the_vp_approval(self):
        txn = _vp_txn(requires_cfo=True, status=wf.STATUS_PENDING_CONTROLLER)
        self.assertEqual(list(_resolve(txn)[0]), ["vp_approver_user_key"])
        txn.update(status=wf.STATUS_PENDING_VP, vp_approver_user_key=NICOLE)
        self.assertEqual(list(_resolve(txn)[0]), ["cfo_approver_user_key"])

    def test_a_combined_approver_controller_cfo_level_transaction_still_visits_vp_then_cfo(self):
        txn = _vp_txn(requires_cfo=True, selected_approver_user_key=APPR, selected_controller_user_key=APPR, current_owner_user_key=APPR)
        trail = [_step(txn, "sam", APPR)[0], _step(txn, "vp", NICOLE)[0], _step(txn, "cfo", WENDY)[0]]
        self.assertEqual(trail, [wf.STATUS_PENDING_VP, wf.STATUS_PENDING_CFO, wf.STATUS_READY_FOR_TREASURY])

    def test_28_property_and_corporate_follow_the_same_chain(self):
        for classification, vp in (("Corporate", NICOLE), ("Property", SEUNG)):
            with self.subTest(classification=classification):
                txn = _vp_txn(entity_classification=classification, current_owner_user_key=APPR)
                self.assertEqual(_step(txn, "sam", APPR)[0], wf.STATUS_PENDING_CONTROLLER)
                self.assertEqual(_step(txn, "controller", CTRL)[:2], (wf.STATUS_PENDING_VP, vp))
                self.assertEqual(_step(txn, "vp", vp)[0], wf.STATUS_READY_FOR_TREASURY)

    def test_a_failed_lookup_leaves_the_transaction_where_it_was(self):
        txn = _vp_txn(status=wf.STATUS_PENDING_CONTROLLER)
        before = dict(txn)
        with self.assertRaises(wf.WorkflowConfigurationError):
            _step(txn, "controller", CTRL, vps={"CA": []})
        self.assertEqual(txn, before)

    def test_requester_self_assignment_is_not_blocked_here_no_new_policy_exposure_for_business_review(self):
        txn = _vp_txn(status=wf.STATUS_PENDING_CONTROLLER, prepared_by_user_key=NICOLE)
        self.assertEqual(_resolve(txn)[0], {"vp_approver_user_key": NICOLE})           # the requester can be the resolved VP


class AuthorizationTests(unittest.TestCase):
    def test_14_only_the_assigned_vp_may_approve_the_vp_stage(self):
        txn = _vp_txn(status=wf.STATUS_PENDING_VP, vp_approver_user_key=NICOLE)
        wf.authorize_action(role="vp", user_key=NICOLE, txn=txn, action=wf.ACTION_APPROVE)
        for role, key in (("vp", SEUNG), ("sam", NICOLE), ("controller", CTRL), ("cfo", WENDY)):
            with self.subTest(role=role, key=key), self.assertRaises(wf.UnauthorizedActionError):
                wf.authorize_action(role=role, user_key=key, txn=txn, action=wf.ACTION_APPROVE)

    def test_15_only_the_assigned_cfo_may_approve_the_cfo_stage(self):
        txn = _vp_txn(status=wf.STATUS_PENDING_CFO, requires_cfo=True, vp_approver_user_key=NICOLE, cfo_approver_user_key=WENDY)
        wf.authorize_action(role="cfo", user_key=WENDY, txn=txn, action=wf.ACTION_APPROVE)
        for role, key in (("cfo", OTHER_CFO), ("vp", NICOLE), ("sam", WENDY)):
            with self.subTest(role=role, key=key), self.assertRaises(wf.UnauthorizedActionError):
                wf.authorize_action(role=role, user_key=key, txn=txn, action=wf.ACTION_APPROVE)

    def test_an_earlier_stage_assignment_does_not_authorize_a_later_stage(self):
        txn = _vp_txn(status=wf.STATUS_PENDING_CONTROLLER, selected_approver_user_key=NICOLE, vp_approver_user_key=NICOLE)
        with self.assertRaises(wf.UnauthorizedActionError):
            wf.authorize_action(role={"sam", "vp"}, user_key=NICOLE, txn=txn, action=wf.ACTION_APPROVE)


# ═══ SQL: one atomic transition ═════════════════════════════════════════════
class AtomicPersistenceTests(unittest.TestCase):
    def _advance(self, *, status, new_status, owner, role, vp=None, cfo=None, rowcount=1, fail_on=None):
        cur = MagicMock()
        cur.rowcount = rowcount
        cur.fetchone.return_value = (77,)
        if fail_on:
            seen = []

            def execute(sql, *a):
                seen.append(sql)
                if fail_on in sql:
                    raise RuntimeError("boom")

            cur.execute.side_effect = execute
        conn = MagicMock()
        conn.cursor.return_value = cur
        with patch.object(db, "get_connection", return_value=conn):
            db.advance_transaction_workflow(
                1, from_status=status, new_status=new_status, new_owner_user_key=owner, actor_user_key=APPR, actor_role="r",
                action=wf.ACTION_APPROVE, workflow_role=role, vp_approver_user_key=vp, cfo_approver_user_key=cfo)
        return cur, conn

    def test_16_17_18_19_the_vp_key_assignment_owner_and_events_are_written_together(self):
        cur, conn = self._advance(status=wf.STATUS_PENDING_CONTROLLER, new_status=wf.STATUS_PENDING_VP, owner=NICOLE, role="VP", vp=NICOLE)
        sql, params = cur.execute.call_args_list[0].args
        self.assertIn("VPApprover_User_Key = ?", sql)
        self.assertNotIn("CFOApprover_User_Key", sql)
        self.assertTrue(sql.rstrip().endswith("AND VPApprover_User_Key IS NULL"))      # an existing VP is never overwritten
        self.assertEqual(params, [wf.STATUS_PENDING_VP, "VP", NICOLE, params[3], NICOLE, 1, wf.STATUS_PENDING_CONTROLLER])
        written = _Written([(c.args[0], c.args[1] if len(c.args) > 1 else None) for c in cur.execute.call_args_list])
        self.assertEqual(written.owner, written.assignment[2])                         # CurrentOwner == the new assignment's user
        self.assertEqual((written.assignment[1], written.assignment[2]), ("VP", NICOLE))
        self.assertEqual(written.event_types, ["approve", "VP_ASSIGNED"])
        self.assertEqual((written.related("approve"), written.related("VP_ASSIGNED")), (None, 77))
        conn.commit.assert_called_once()

    def test_the_cfo_key_is_written_the_same_way(self):
        cur, conn = self._advance(status=wf.STATUS_PENDING_VP, new_status=wf.STATUS_PENDING_CFO, owner=WENDY, role="CFO", cfo=WENDY)
        sql, params = cur.execute.call_args_list[0].args
        self.assertIn("CFOApprover_User_Key = ?", sql)
        self.assertNotIn("VPApprover_User_Key", sql)
        self.assertTrue(sql.rstrip().endswith("AND CFOApprover_User_Key IS NULL"))
        written = _Written([(c.args[0], c.args[1] if len(c.args) > 1 else None) for c in cur.execute.call_args_list])
        self.assertEqual((written.owner, written.assignment[1:3], written.event_types), (WENDY, ["CFO", WENDY], ["approve", "CFO_ASSIGNED"]))
        conn.commit.assert_called_once()

    def test_without_a_resolved_assignee_the_statement_is_exactly_what_it_was(self):
        cur, _ = self._advance(status=wf.STATUS_PENDING_APPROVER, new_status=wf.STATUS_PENDING_CONTROLLER, owner=CTRL, role="Controller")
        sql = cur.execute.call_args_list[0].args[0]
        self.assertNotIn("VPApprover_User_Key", sql)
        self.assertNotIn("CFOApprover_User_Key", sql)
        self.assertTrue(sql.endswith("WHERE Transaction_Key = ? AND Current_Status = ?"))

    def test_21_a_repeated_or_conflicting_transition_writes_nothing(self):
        cur = MagicMock()
        cur.rowcount = 0
        conn = MagicMock()
        conn.cursor.return_value = cur
        with patch.object(db, "get_connection", return_value=conn), self.assertRaises(db.WorkflowConflictError):
            db.advance_transaction_workflow(
                1, from_status=wf.STATUS_PENDING_CONTROLLER, new_status=wf.STATUS_PENDING_VP, new_owner_user_key=NICOLE,
                actor_user_key=CTRL, actor_role="r", action=wf.ACTION_APPROVE, workflow_role="VP", vp_approver_user_key=NICOLE)
        self.assertEqual(cur.execute.call_count, 1)
        conn.rollback.assert_called_once()
        conn.commit.assert_not_called()

    def test_20_a_failure_part_way_commits_nothing(self):
        for fail_on in ("INSERT INTO etransactions.WorkflowAssignment", "INSERT INTO etransactions.WorkflowEvent"):
            with self.subTest(fail_on=fail_on):
                cur = MagicMock()
                cur.rowcount = 1
                cur.fetchone.return_value = (77,)
                cur.execute.side_effect = lambda sql, *a: (_ for _ in ()).throw(RuntimeError("boom")) if fail_on in sql else None
                conn = MagicMock()
                conn.cursor.return_value = cur
                with patch.object(db, "get_connection", return_value=conn), self.assertRaises(RuntimeError):
                    db.advance_transaction_workflow(
                        1, from_status=wf.STATUS_PENDING_CONTROLLER, new_status=wf.STATUS_PENDING_VP, new_owner_user_key=NICOLE,
                        actor_user_key=CTRL, actor_role="r", action=wf.ACTION_APPROVE, workflow_role="VP", vp_approver_user_key=NICOLE)
                conn.rollback.assert_called_once()
                conn.commit.assert_not_called()

    def test_the_event_literals_are_unchanged(self):
        self.assertEqual([wf.EVENT_VP_ASSIGNED, wf.EVENT_CFO_ASSIGNED, wf.EVENT_APPROVAL], ["VP_ASSIGNED", "CFO_ASSIGNED", "approve"])
        self.assertEqual(wf.events_for_action(from_status=wf.STATUS_PENDING_CONTROLLER, action=wf.ACTION_APPROVE,
                                              new_status=wf.STATUS_PENDING_VP), ["approve", "VP_ASSIGNED"])
        self.assertEqual(wf.events_for_action(from_status=wf.STATUS_PENDING_VP, action=wf.ACTION_APPROVE,
                                              new_status=wf.STATUS_PENDING_CFO), ["approve", "CFO_ASSIGNED"])


# ═══ The real approval route ═══════════════════════════════════════════════
class RouteTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def _post(self, role, user_key, txn, *, vps=(NICOLE,), cfos=(WENDY,), advance_error=None, action=wf.ACTION_APPROVE,
              origin=None, bulk_gate=False, vp_error=None):
        with self.client.session_transaction() as sess:
            sess["role"] = role
        self.vp_lookup = MagicMock(side_effect=vp_error) if vp_error else MagicMock(return_value=list(vps))
        self.cfo_lookup = MagicMock(return_value=list(cfos))
        with patch.object(app_module, "current_app_user_key", return_value=user_key), \
             patch.object(app_module, "bulk_recipient_enabled", return_value=bulk_gate), \
             patch.object(app_module.db, "get_transaction_for_workflow", return_value=txn), \
             patch.object(app_module.db, "get_last_rfi_origin_status", return_value=origin), \
             patch.object(app_module.db, "get_eligible_vp_user_keys", self.vp_lookup), \
             patch.object(app_module.db, "get_eligible_cfo_user_keys", self.cfo_lookup), \
             patch.object(app_module.db, "add_transaction_comment"), \
             patch.object(app_module.sharepoint, "upload_attachment") as upload, \
             patch.object(app_module.db, "advance_transaction_workflow", **({"side_effect": advance_error} if advance_error else {})) as advance:
            resp = self.client.post(f"/dashboard/request/{txn['request_id']}/action", data={"action": action, "comment": "Here is the information." if action == wf.ACTION_REQUESTER_RESPOND else ""},
                                    content_type="multipart/form-data")
        with self.client.session_transaction() as sess:
            flashes = [(c, m) for c, m in sess.get("_flashes", [])]
        self.upload = upload
        return resp, advance, flashes

    def test_the_controller_approval_assigns_the_vp_in_the_same_call_that_advances_the_transaction(self):
        resp, advance, flashes = self._post("controller", CTRL, _vp_txn(status=wf.STATUS_PENDING_CONTROLLER, current_owner_user_key=CTRL))
        self.assertEqual(resp.status_code, 302)
        kwargs = advance.call_args.kwargs
        self.assertEqual((kwargs["new_status"], kwargs["new_owner_user_key"], kwargs["workflow_role"], kwargs["vp_approver_user_key"],
                          kwargs["cfo_approver_user_key"]), (wf.STATUS_PENDING_VP, NICOLE, "VP", NICOLE, None))
        self.vp_lookup.assert_called_once_with("CA")
        self.assertEqual(flashes[0][0], "success")

    def test_the_vp_approval_assigns_the_cfo(self):
        txn = _vp_txn(status=wf.STATUS_PENDING_VP, requires_cfo=True, vp_approver_user_key=NICOLE, current_owner_user_key=NICOLE)
        resp, advance, _ = self._post("vp", NICOLE, txn)
        kwargs = advance.call_args.kwargs
        self.assertEqual((kwargs["new_status"], kwargs["new_owner_user_key"], kwargs["cfo_approver_user_key"], kwargs["vp_approver_user_key"]),
                         (wf.STATUS_PENDING_CFO, WENDY, WENDY, None))
        self.vp_lookup.assert_not_called()

    def test_6_a_missing_vp_stops_the_approval_before_anything_is_written(self):
        resp, advance, flashes = self._post("controller", CTRL, _vp_txn(status=wf.STATUS_PENDING_CONTROLLER), vps=())
        advance.assert_not_called()
        self.assertEqual(flashes[0][0], "danger")
        self.assertIn("No eligible VP on the CA team", flashes[0][1])
        self.assertNotIn("Action recorded", flashes[0][1])

    def test_5_two_cfos_stop_the_approval(self):
        txn = _vp_txn(status=wf.STATUS_PENDING_VP, requires_cfo=True, vp_approver_user_key=NICOLE)
        resp, advance, flashes = self._post("vp", NICOLE, txn, cfos=(WENDY, OTHER_CFO))
        advance.assert_not_called()
        self.assertIn("More than one eligible CFO was found", flashes[0][1])

    def test_20_a_lookup_that_errors_commits_nothing_and_reports_no_approval(self):
        resp, advance, flashes = self._post("controller", CTRL, _vp_txn(status=wf.STATUS_PENDING_CONTROLLER), vp_error=RuntimeError("SQL down"))
        advance.assert_not_called()
        self.assertEqual([c for c, _ in flashes], ["danger"])
        self.assertNotIn("Action recorded", flashes[0][1])

    def test_success_is_reported_only_after_the_sql_transition_succeeds(self):
        resp, advance, flashes = self._post("controller", CTRL, _vp_txn(status=wf.STATUS_PENDING_CONTROLLER), advance_error=RuntimeError("SQL down"))
        self.assertEqual([c for c, _ in flashes], ["danger"])
        self.assertNotIn("Action recorded", flashes[0][1])

    def test_21_a_repeated_click_is_reported_as_already_processed(self):
        resp, advance, flashes = self._post("controller", CTRL, _vp_txn(status=wf.STATUS_PENDING_CONTROLLER),
                                            advance_error=db.WorkflowConflictError("moved"))
        self.assertEqual(flashes, [("info", "This action was already processed for this transaction.")])

    def test_an_unauthorized_user_triggers_no_lookup(self):
        resp, advance, flashes = self._post("sam", APPR, _vp_txn(status=wf.STATUS_PENDING_CONTROLLER))
        advance.assert_not_called()
        self.vp_lookup.assert_not_called()
        self.assertEqual(flashes[0][0], "warning")

    def test_a_transaction_that_needs_no_vp_is_approved_exactly_as_before(self):
        resp, advance, _ = self._post("controller", CTRL, _txn(status=wf.STATUS_PENDING_CONTROLLER))
        kwargs = advance.call_args.kwargs
        self.assertEqual((kwargs["new_status"], kwargs["vp_approver_user_key"], kwargs["cfo_approver_user_key"]),
                         (wf.STATUS_READY_FOR_TREASURY, None, None))
        self.vp_lookup.assert_not_called()
        self.cfo_lookup.assert_not_called()

    def test_26_the_bulk_feature_gate_does_not_affect_approvals(self):
        for gate in (False, True):
            with self.subTest(gate=gate):
                resp, advance, flashes = self._post("controller", CTRL, _vp_txn(status=wf.STATUS_PENDING_CONTROLLER), bulk_gate=gate)
                advance.assert_called_once()
                self.assertEqual(flashes[0][0], "success")

    def test_24_an_rfi_response_returns_to_the_stored_vp_without_any_lookup(self):
        txn = _vp_txn(status=wf.STATUS_MORE_INFO, vp_approver_user_key=NICOLE, current_owner_user_key=REQ)
        resp, advance, _ = self._post("submitter", REQ, txn, action=wf.ACTION_REQUESTER_RESPOND, origin=wf.STATUS_PENDING_VP)
        kwargs = advance.call_args.kwargs
        self.assertEqual((kwargs["new_status"], kwargs["new_owner_user_key"], kwargs["vp_approver_user_key"]),
                         (wf.STATUS_PENDING_VP, NICOLE, None))
        self.vp_lookup.assert_not_called()

    def test_22_a_property_transaction_requiring_a_vp_cannot_start_bank_release_without_one(self):
        txn = _vp_txn(status=wf.STATUS_READY_FOR_TREASURY, entity_classification="Property", vp_approver_user_key=None)
        resp, advance, flashes = self._post("treasury", None, txn, action=wf.ACTION_TREASURY_INITIATED)
        advance.assert_not_called()
        self.upload.assert_not_called()                                      # rejected before any evidence upload
        self.assertIn("required VP approval but no VP is assigned", flashes[0][1])


class BankReleaserTests(unittest.TestCase):
    def _releaser(self, **over):
        txn = _txn(status=wf.STATUS_READY_FOR_TREASURY, entity_classification="Property", **over)
        return wf.determine_next_step(txn, wf.ACTION_TREASURY_INITIATED)

    def test_22_a_required_vp_is_the_releaser_and_a_missing_one_is_an_error_not_the_controller(self):
        self.assertEqual(self._releaser(requires_vp=True, vp_approver_user_key=NICOLE)[1], NICOLE)
        with self.assertRaisesRegex(wf.WorkflowConfigurationError, "bank releaser cannot be determined"):
            self._releaser(requires_vp=True, vp_approver_user_key=None)

    def test_23_when_no_vp_is_required_the_controller_remains_the_releaser(self):
        status, owner, role, _ = self._releaser(requires_vp=False, vp_approver_user_key=None)
        self.assertEqual((status, owner, role), (wf.STATUS_AWAITING_RELEASE, CTRL, "Bank Releaser"))

    def test_the_cfo_is_never_the_releaser_and_corporate_release_is_untouched(self):
        self.assertEqual(self._releaser(requires_vp=True, vp_approver_user_key=NICOLE, requires_cfo=True, cfo_approver_user_key=WENDY)[1], NICOLE)
        corporate = _txn(status=wf.STATUS_READY_FOR_TREASURY, requires_vp=True, vp_approver_user_key=None)
        self.assertEqual(wf.determine_next_step(corporate, wf.ACTION_TREASURY_RELEASED)[:2], (wf.STATUS_COMPLETED, None))


class ExternalIsolationTests(unittest.TestCase):
    def test_no_external_access_was_attempted(self):
        self.assertEqual(external_guard.state.blocked, [])


if __name__ == "__main__":
    unittest.main()
