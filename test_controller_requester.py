"""
Controller-initiated requests: when an active Controller submits a request, the VP of their own team (AppUser.Team CA for
Corporate, PA for Property) is assigned as BOTH Approver and Controller, so a Controller never approves their own request.
Everything downstream is the existing workflow: the combined Approver/Controller approval, a separate VP approval when the
amount requires it, a separate CFO approval above that, and the existing Property bank-release rules.

All users, keys and banking values are fake; `external_guard` makes SQL/SharePoint/network inert.
"""
import external_guard  # noqa: F401  (must precede app/db/sharepoint imports)
import inspect
import unittest
from unittest.mock import MagicMock, patch

from werkzeug.datastructures import MultiDict

import app as app_module
import db
import test_draft_persistence as dp
import test_internal_recipient as ir
import test_vp_cfo_assignment as vpt
import workflow as wf
from test_notification_contract import _Written, _run, _txn

NICOLE, SEUNG, WENDY = 19, 27, 48       # the VPs / CFO of the fake configuration; the code never knows them
_REAL_INSERT_TRANSACTION = db.insert_transaction
REQUESTER = dp.USER["user_key"]         # 11
VP_NAMES = {NICOLE: "Nicole VP", SEUNG: "Seung VP"}
CA_CTRL = {"is_controller": True, "team": "CA"}
PA_CTRL = {"is_controller": True, "team": "PA"}


def _vp_lookup(mapping=None, calls=None):
    mapping = {"CA": [NICOLE], "PA": [SEUNG]} if mapping is None else mapping

    def fn(team):
        if calls is not None:
            calls.append(team)
        return list(mapping.get(team, []))
    return fn


def _plan(ctx, classification, *, vps=None, roles=("sam", "vp"), user=None, vp_error=None, requester=REQUESTER):
    lookup = MagicMock(side_effect=vp_error) if vp_error else _vp_lookup(vps)
    with patch.object(app_module.db, "get_eligible_vp_user_keys", lookup), \
         patch.object(app_module.db, "get_app_user_role_codes", return_value=list(roles)), \
         patch.object(app_module.db, "get_app_user_by_key",
                      side_effect=lambda k: user if user is not None else {"user_key": k, "display_name": VP_NAMES.get(k, "X")}):
        return app_module._assignment_plan({"user_key": requester}, classification, ctx)


# ═══ Who is a Controller requester, and who is their VP ═══════════════════════
class ControllerRequesterTests(unittest.TestCase):
    def test_1_3_a_ca_controller_submitting_corporate_gets_the_corporate_vp_as_approver_and_controller(self):
        plan, error = _plan(CA_CTRL, "corporate")
        self.assertIsNone(error)
        self.assertEqual(plan["kind"], "controller_requester")
        self.assertEqual((plan["approver"]["user_key"], plan["controller"]["user_key"]), (NICOLE, NICOLE))
        self.assertEqual(plan["approver"]["display_name"], "Nicole VP")

    def test_2_4_a_pa_controller_submitting_property_gets_the_property_vp(self):
        plan, error = _plan(PA_CTRL, "property")
        self.assertEqual((error, plan["approver"]["user_key"], plan["controller"]["user_key"]), (None, SEUNG, SEUNG))

    def test_the_vp_comes_from_the_requesters_team_lookup_never_a_hard_coded_user(self):
        calls = []
        with patch.object(app_module.db, "get_eligible_vp_user_keys", _vp_lookup({"CA": [91]}, calls)), \
             patch.object(app_module.db, "get_app_user_role_codes", return_value=["sam"]), \
             patch.object(app_module.db, "get_app_user_by_key", return_value={"user_key": 91, "display_name": "Someone Else"}):
            plan, _ = app_module._assignment_plan({"user_key": REQUESTER}, "corporate", CA_CTRL)
        self.assertEqual((calls, plan["approver"]["user_key"]), (["CA"], 91))

    def test_12_a_controller_with_other_roles_is_still_a_controller_requester(self):
        plan, _ = _plan({"is_controller": True, "team": "PA"}, "property", roles=("sam", "vp"))
        self.assertEqual(plan["kind"], "controller_requester")

    def test_an_unclassified_draft_uses_the_requesters_own_team(self):
        plan, error = _plan(PA_CTRL, "")
        self.assertEqual((error, plan["approver"]["user_key"]), (None, SEUNG))

    def test_13_a_controller_submitting_for_the_other_team_is_rejected_without_a_vp_lookup(self):
        calls = []
        for ctx, classification, label in ((CA_CTRL, "property", "Corporate"), (PA_CTRL, "corporate", "Property")):
            with self.subTest(label=label):
                with patch.object(app_module.db, "get_eligible_vp_user_keys", _vp_lookup(calls=calls)):
                    plan, error = app_module._assignment_plan({"user_key": REQUESTER}, classification, ctx)
                self.assertIsNone(plan)
                self.assertIn(f"you can only submit {label} requests", error)
        self.assertEqual(calls, [])

    def test_13_a_controller_with_a_blank_or_unexpected_team_fails_safely(self):
        for team in (None, "", "AM", "CFO"):
            with self.subTest(team=team):
                plan, error = _plan({"is_controller": True, "team": team}, "corporate")
                self.assertIsNone(plan)
                self.assertIn("no Corporate (CA) or Property (PA) team", error)

    def test_14_no_eligible_vp_is_rejected_with_no_fallback(self):
        plan, error = _plan(CA_CTRL, "corporate", vps={"CA": []})
        self.assertIsNone(plan)
        self.assertIn("No Corporate VP is configured", error)

    def test_15_several_distinct_vps_are_rejected_never_a_pick(self):
        plan, error = _plan(PA_CTRL, "property", vps={"PA": [SEUNG, 99]})
        self.assertIsNone(plan)
        self.assertIn("More than one Property VP", error)

    def test_duplicate_role_rows_for_one_vp_are_one_candidate(self):
        plan, error = _plan(CA_CTRL, "corporate", vps={"CA": [NICOLE, NICOLE]})
        self.assertEqual((error, plan["approver"]["user_key"]), (None, NICOLE))

    def test_5_a_controller_who_is_also_the_teams_vp_cannot_approve_their_own_request(self):
        plan, error = _plan(CA_CTRL, "corporate", requester=NICOLE)
        self.assertIsNone(plan)
        self.assertIn("cannot be approved by yourself", error)

    def test_a_vp_who_cannot_act_as_approver_is_a_configuration_error(self):
        plan, error = _plan(CA_CTRL, "corporate", roles=("vp",))
        self.assertIsNone(plan)
        self.assertIn("cannot act as Approver", error)

    def test_a_failed_vp_lookup_is_reported_without_details(self):
        plan, error = _plan(CA_CTRL, "corporate", vp_error=RuntimeError("server.internal secret"))
        self.assertIsNone(plan)
        self.assertNotIn("secret", error)

    def test_the_team_membership_alone_never_makes_someone_a_controller(self):
        for classification, kind in (("corporate", "corporate_controller"), ("property", "picker"), ("", "picker")):
            with self.subTest(classification=classification):
                with patch.object(app_module.db, "get_eligible_controllers", return_value=[{"user_key": 20, "display_name": "Athena"}]):
                    plan, error = app_module._assignment_plan({"user_key": REQUESTER}, classification, {"is_controller": False, "team": "CA"})
                self.assertEqual((error, plan["kind"]), (None, kind))

    def test_a_missing_context_is_treated_as_an_ordinary_requester(self):
        plan, _ = app_module._assignment_plan({"user_key": REQUESTER}, "property", None)
        self.assertEqual(plan["kind"], "picker")


class RequesterContextSqlTests(unittest.TestCase):
    def _ctx(self, row, user_key=11):
        cur = MagicMock()
        cur.fetchone.return_value = row
        conn = MagicMock()
        conn.cursor.return_value = cur
        with patch.object(db, "get_connection", return_value=conn):
            result = external_guard.REAL_GET_REQUESTER_CONTROLLER_CONTEXT(user_key)
        return result, cur

    def test_a_user_with_the_controller_role_is_a_controller_whatever_else_they_hold(self):
        result, cur = self._ctx(("CA", 1))
        self.assertEqual(result, {"is_controller": True, "team": "CA"})
        sql, params = cur.execute.call_args.args
        self.assertEqual(params, [11])
        for required in ("r.Role_Code = 'controller'", "r.Is_Active = 1", "u.Active_Status = 1",
                         "r.Effective_Start_Date IS NULL OR", "r.Effective_End_Date IS NULL OR"):
            self.assertIn(required, sql)
        self.assertRegex(sql.strip(), r"^SELECT ")

    def test_without_the_role_the_user_is_not_a_controller_even_on_the_ca_team(self):
        self.assertEqual(self._ctx(("CA", 0))[0], {"is_controller": False, "team": "CA"})

    def test_a_blank_team_is_reported_as_none_and_an_unknown_user_as_none(self):
        self.assertEqual(self._ctx(("  ", 1))[0], {"is_controller": True, "team": None})
        self.assertIsNone(self._ctx(None)[0])
        self.assertIsNone(external_guard.REAL_GET_REQUESTER_CONTROLLER_CONTEXT(None))


# ═══ The server-side form override ═══════════════════════════════════════════
class FormOverrideTests(unittest.TestCase):
    def _apply(self, ctx, posted, user=None):
        user = user or {"user_key": REQUESTER}
        with patch.object(app_module.db, "get_requester_controller_context", return_value=ctx), \
             patch.object(app_module.db, "get_eligible_vp_user_keys", _vp_lookup()), \
             patch.object(app_module.db, "get_app_user_role_codes", return_value=["sam"]), \
             patch.object(app_module.db, "get_app_user_by_key", side_effect=lambda k: {"user_key": k, "display_name": VP_NAMES[k]}), \
             patch.object(app_module.db, "get_eligible_controllers", return_value=[{"user_key": 20, "display_name": "Athena"}]):
            return app_module._with_assigned_approvers(MultiDict(posted), user)

    def test_18_tampered_approver_and_controller_values_are_replaced_by_the_vp(self):
        form, error = self._apply(CA_CTRL, {"classification": "corporate", "approver_key": "2", "controller_key": "3"})
        self.assertEqual((error, form["approver_key"], form["controller_key"], form[app_module.VP_ASSIGNED_FIELD]),
                         (None, str(NICOLE), str(NICOLE), "1"))

    def test_blank_values_are_filled_in_too(self):
        form, _ = self._apply(PA_CTRL, {"classification": "property"})
        self.assertEqual((form["approver_key"], form["controller_key"]), (str(SEUNG), str(SEUNG)))

    def test_a_posted_marker_can_never_switch_the_role_checks_off(self):
        for ctx in (None, {"is_controller": False, "team": "CA"}):
            form, _ = self._apply(ctx, {"classification": "property", "approver_key": "2", "controller_key": "3",
                                        app_module.VP_ASSIGNED_FIELD: "1"})
            self.assertNotIn(app_module.VP_ASSIGNED_FIELD, form)
            self.assertEqual(app_module._validate_eligible_approver_controller.__name__, "_validate_eligible_approver_controller")

    def test_11_non_controller_requests_keep_the_pickers_exactly(self):
        form, error = self._apply({"is_controller": False, "team": "PA"},
                                  {"classification": "property", "approver_key": "2", "controller_key": "3"})
        self.assertEqual((error, form["approver_key"], form["controller_key"]), (None, "2", "3"))

    def test_a_non_controller_corporate_request_still_gets_only_the_corporate_controller(self):
        form, _ = self._apply({"is_controller": False, "team": "CA"},
                              {"classification": "corporate", "approver_key": "2", "controller_key": "3"})
        self.assertEqual((form["approver_key"], form["controller_key"], app_module.VP_ASSIGNED_FIELD in form), ("2", "20", False))

    def test_a_role_lookup_failure_blocks_rather_than_guessing(self):
        with patch.object(app_module.db, "get_requester_controller_context", side_effect=RuntimeError("down")):
            form, error = app_module._with_assigned_approvers(MultiDict({"classification": "corporate"}), {"user_key": REQUESTER})
        self.assertIn("could not be verified", error)

    def test_the_original_post_is_never_mutated(self):
        posted = MultiDict({"classification": "corporate", "approver_key": "2", "controller_key": "3"})
        with patch.object(app_module.db, "get_requester_controller_context", return_value=CA_CTRL), \
             patch.object(app_module.db, "get_eligible_vp_user_keys", _vp_lookup()), \
             patch.object(app_module.db, "get_app_user_role_codes", return_value=["sam"]), \
             patch.object(app_module.db, "get_app_user_by_key", return_value={"user_key": NICOLE, "display_name": "N"}):
            app_module._with_assigned_approvers(posted, {"user_key": REQUESTER})
        self.assertEqual((posted["approver_key"], posted["controller_key"]), ("2", "3"))

    def test_19_the_override_only_reads_so_existing_submitted_requests_are_never_touched(self):
        source = "".join(inspect.getsource(f) for f in (app_module._with_assigned_approvers, app_module._assignment_plan,
                                                        app_module._controller_requester_vp, app_module._assignment_ui))
        for forbidden in ("advance_transaction_workflow", "UPDATE", "INSERT", "update_draft", "create_draft", "finalize"):
            self.assertNotIn(forbidden, source)


# ═══ Intake, draft and final-submission routes ═════════════════════════════════
class _RouteBase(dp._Base):
    ctx = CA_CTRL

    def setUp(self):
        super().setUp()
        for target, name, kw in (
            (app_module.db, "get_requester_controller_context", {"side_effect": lambda key: self.ctx}),
            (app_module.db, "get_eligible_vp_user_keys", {"side_effect": lambda team: _vp_lookup()(team)}),
            (app_module.db, "get_app_user_by_key", {"side_effect": lambda k: {"user_key": k, "display_name": VP_NAMES.get(k, "A"), "email": ""}}),
            (app_module.sharepoint, "get_properties", {"return_value": [{"entity_number": 124, "name": "THE TIMBERS"}]}),
        ):
            patcher = patch.object(target, name, **kw)
            self.m[name] = patcher.start()
            self.addCleanup(patcher.stop)
        self.m["get_app_user_role_codes"].return_value = ["sam"]        # the VP holds no Controller role

    def submit(self, **overrides):
        form = ir._external_form(verbal_confirmed_with_known="on", **overrides)
        return self.client.post("/intake/submit", data={**form, **ir._files()}, content_type="multipart/form-data")

    def property_overrides(self, **extra):
        return dict(classification="property", property_entity_number="124", property_dept="", **extra)


class ControllerSubmissionTests(_RouteBase):
    def test_1_a_ca_controller_submission_is_stored_with_the_corporate_vp_as_approver_and_controller(self):
        resp = self.submit(approver_key="2", controller_key="3")
        self.assertEqual(resp.status_code, 302)
        data = self.m["insert_transaction"].call_args[0][0]
        self.assertEqual((data["approver_key"], data["controller_key"], data["prepared_by_key"]), (NICOLE, NICOLE, REQUESTER))

    def test_2_a_pa_controller_submission_is_stored_with_the_property_vp(self):
        type(self).ctx = PA_CTRL
        self.addCleanup(setattr, type(self), "ctx", CA_CTRL)
        resp = self.submit(**self.property_overrides(approver_key="2", controller_key="3"))
        self.assertEqual(resp.status_code, 302, resp.get_data(as_text=True)[:0])
        data = self.m["insert_transaction"].call_args[0][0]
        self.assertEqual((data["approver_key"], data["controller_key"]), (SEUNG, SEUNG))

    def test_18_forged_or_blank_assignments_never_reach_the_database(self):
        for approver, controller in (("2", "3"), ("", ""), (str(REQUESTER), str(REQUESTER)), ("999", "998")):
            with self.subTest(approver=approver):
                self.m["insert_transaction"].reset_mock()
                self.assertEqual(self.submit(approver_key=approver, controller_key=controller).status_code, 302)
                data = self.m["insert_transaction"].call_args[0][0]
                self.assertEqual((data["approver_key"], data["controller_key"]), (NICOLE, NICOLE))

    def test_5_the_controller_is_never_their_own_approver_or_controller(self):
        self.submit()
        data = self.m["insert_transaction"].call_args[0][0]
        self.assertNotIn(REQUESTER, (data["approver_key"], data["controller_key"]))

    def test_13_a_team_mismatch_is_rejected_before_anything_is_reserved_or_stored(self):
        resp = self.submit(**self.property_overrides())          # a CA Controller submitting Property
        body = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("you can only submit Corporate requests", body)
        self.m["insert_transaction"].assert_not_called()
        self.m["reserve_request_id"].assert_not_called()

    def test_14_15_a_missing_or_ambiguous_vp_blocks_the_submission(self):
        for mapping, text in (({"CA": []}, "No Corporate VP is configured"), ({"CA": [NICOLE, 99]}, "More than one Corporate VP")):
            with self.subTest(text=text):
                self.m["get_eligible_vp_user_keys"].side_effect = lambda team, mapping=mapping: _vp_lookup(mapping)(team)
                self.m["insert_transaction"].reset_mock()
                resp = self.submit()
                self.assertEqual(resp.status_code, 200)
                self.assertIn(text, resp.get_data(as_text=True))
                self.m["insert_transaction"].assert_not_called()

    def test_there_is_no_fallback_to_the_requester_when_the_vp_is_the_requester(self):
        self.m["get_eligible_vp_user_keys"].side_effect = lambda team: [REQUESTER]
        resp = self.submit()
        self.assertEqual(resp.status_code, 200)
        self.assertIn("cannot be approved by yourself", resp.get_data(as_text=True))
        self.m["insert_transaction"].assert_not_called()

    def test_11_a_non_controller_keeps_the_normal_role_checked_pickers(self):
        type(self).ctx = {"is_controller": False, "team": "PA"}
        self.addCleanup(setattr, type(self), "ctx", CA_CTRL)
        self.m["get_app_user_role_codes"].return_value = ["sam"]       # the picked Controller lacks the controller role
        resp = self.submit(**self.property_overrides(approver_key="2", controller_key="3"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("not an eligible Controller", resp.get_data(as_text=True))
        self.m["get_app_user_role_codes"].return_value = ["sam", "controller"]
        self.assertEqual(self.submit(**self.property_overrides(approver_key="2", controller_key="3")).status_code, 302)
        data = self.m["insert_transaction"].call_args[0][0]
        self.assertEqual((data["approver_key"], data["controller_key"]), (2, 3))

    def test_the_precheck_needs_no_posted_assignment_and_reports_configuration_errors(self):
        form = ir._external_form(has_validation_evidence="true", has_wire_ach_instructions="true", has_payment_support="true",
                                 verbal_confirmed_with_known="on", approver_key="", controller_key="")
        self.assertEqual(self.client.post("/intake/precheck", data=form).get_json(), {"success": True, "errors": []})
        self.m["get_eligible_vp_user_keys"].side_effect = lambda team: []
        errors = self.client.post("/intake/precheck", data=form).get_json()["errors"]
        self.assertEqual(len(errors), 1)
        self.assertIn("No Corporate VP is configured", errors[0])

    def test_25_submission_writes_no_extra_assignment_events_for_the_combined_assignment(self):
        self.submit()
        data = self.m["insert_transaction"].call_args[0][0]
        self.assertEqual(data["approver_key"], data["controller_key"])      # one person; insert_transaction writes only Submitted + APPROVER_ASSIGNED
        self.assertNotIn("CONTROLLER_ASSIGNED", inspect.getsource(_REAL_INSERT_TRANSACTION))


class ControllerDraftTests(_RouteBase):
    def test_16_a_draft_is_saved_with_the_vp_and_restored_on_resume(self):
        self.assertEqual(self.save(dp._draft_form(approver_key="2", controller_key="3")).status_code, 302)
        stored = self.store.drafts[55]
        self.assertEqual((stored["approver_key"], stored["controller_key"]), (NICOLE, NICOLE))
        body = self.resume()
        self.assertIn('id="approver-auto"', body)
        self.assertIn("Nicole VP", body)
        self.assertIn("Because you are submitting as a Controller", body)

    def test_17_final_submission_revalidates_the_vp_instead_of_trusting_the_draft(self):
        self.save(dp._draft_form(approver_key="2", controller_key="3"))
        rid = self.store.drafts[55]["request_id"]
        self.sp.seed(rid, ve=("avs.png", dp.sharepoint.DOC_TYPE_AVS_SCREENSHOT),
                     wire=("w.pdf", dp.sharepoint.DOC_TYPE_WIRE_ACH_INSTRUCTIONS), pay=("inv.pdf", dp.sharepoint.DOC_TYPE_PAYMENT_SUPPORT))
        self.store.drafts[55]["approver_key"] = self.store.drafts[55]["controller_key"] = 99        # a stale draft
        self.m["get_eligible_vp_user_keys"].side_effect = lambda team: [31] if team == "CA" else []    # VP changed since
        self.m["get_app_user_by_key"].side_effect = lambda k: {"user_key": k, "display_name": "New VP", "email": ""}
        form = dp._final_form(request_id=rid, approver_key="99", controller_key="99")
        self.assertEqual(self.client.post("/intake/submit", data=form, content_type="multipart/form-data").status_code, 302)
        data = self.m["finalize_draft_submission"].call_args[0][1]
        self.assertEqual((data["approver_key"], data["controller_key"]), (31, 31))

    def test_a_pre_existing_draft_with_picked_people_is_corrected_at_finalization(self):
        type(self).ctx = {"is_controller": False, "team": "CA"}
        self.addCleanup(setattr, type(self), "ctx", CA_CTRL)
        self.m["get_app_user_role_codes"].return_value = ["sam", "controller"]       # the pickers' normal role check
        self.save(dp._draft_form(approver_key="2", controller_key="3"))
        self.assertEqual(self.store.drafts[55]["approver_key"], 2)
        self.m["get_app_user_role_codes"].return_value = ["sam"]
        type(self).ctx = CA_CTRL                                  # the requester has since become a Controller
        rid = self.store.drafts[55]["request_id"]
        self.sp.seed(rid, ve=("avs.png", dp.sharepoint.DOC_TYPE_AVS_SCREENSHOT),
                     wire=("w.pdf", dp.sharepoint.DOC_TYPE_WIRE_ACH_INSTRUCTIONS), pay=("inv.pdf", dp.sharepoint.DOC_TYPE_PAYMENT_SUPPORT))
        form = dp._final_form(request_id=rid, approver_key="2", controller_key="3")
        self.assertEqual(self.client.post("/intake/submit", data=form, content_type="multipart/form-data").status_code, 302)
        data = self.m["finalize_draft_submission"].call_args[0][1]
        self.assertEqual((data["approver_key"], data["controller_key"]), (NICOLE, NICOLE))

    def test_a_draft_cannot_be_saved_when_the_team_does_not_match(self):
        resp = self.save(dp._draft_form(**self.property_overrides()))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.store.created, [])

    def test_draft_ownership_is_unchanged(self):
        self.save()
        self.assertEqual(self.store.drafts[55]["prepared_by_key"], REQUESTER)


class IntakePageTests(_RouteBase):
    def test_the_page_explains_why_the_assignment_was_made(self):
        body = self.client.get("/intake").get_data(as_text=True)
        self.assertIn("Because you are submitting as a Controller, your team&#39;s VP will serve as the Approver", body.replace("\\u0027", "&#39;").replace("'", "&#39;"))
        for ident in ("approver-picker", "approver-auto", "controller-picker", "controller-auto"):
            self.assertIn(f'id="{ident}"', body)

    def test_the_ui_data_locks_both_fields_per_classification_and_shows_a_mismatch_error(self):
        with app_module.app.test_request_context("/intake"):
            ui = app_module._assignment_ui()
        corporate, property_ = ui["by_class"]["corporate"], ui["by_class"]["property"]
        self.assertTrue(ui["controller_requester"])
        self.assertEqual((corporate["approver"]["name"], corporate["controller"]["name"]), ("Nicole VP", "Nicole VP"))
        self.assertEqual(corporate["approver"]["note"], app_module.CONTROLLER_REQUESTER_NOTE)
        self.assertIn("you can only submit Corporate requests", property_["approver"]["error"])
        self.assertEqual(property_["approver"]["name"], "")

    def test_a_non_controller_sees_no_locked_approver(self):
        type(self).ctx = {"is_controller": False, "team": "CA"}
        self.addCleanup(setattr, type(self), "ctx", CA_CTRL)
        with patch.object(app_module.db, "get_eligible_controllers", return_value=[{"user_key": 20, "display_name": "Athena"}]):
            with app_module.app.test_request_context("/intake"):
                ui = app_module._assignment_ui()
        self.assertFalse(ui["controller_requester"])
        self.assertEqual(set(ui["by_class"]["corporate"]), {"controller"})
        self.assertEqual((ui["by_class"]["property"], ui["by_class"][""]), ({}, {}))


# ═══ Approval chain after submission (existing workflow, new assignment) ═══════
def _vp_assigned(**over):
    """A Controller-originated transaction as stored: Approver = Controller = the VP, the requester is someone else."""
    base = dict(selected_approver_user_key=vpt.NICOLE, selected_controller_user_key=vpt.NICOLE, prepared_by_user_key=REQUESTER,
                current_owner_user_key=vpt.NICOLE, vp_approver_user_key=None, entity_classification="Corporate")
    base.update(over)
    return vpt._vp_txn(**base)


class CombinedApprovalChainTests(unittest.TestCase):
    ALL_THREE = ["Approver", "Controller", "VP"]

    def test_1_approver_controller_and_vp_the_same_person_below_the_cfo_threshold_one_approval_then_treasury(self):
        txn = _vp_assigned()
        status, owner, role, satisfied = vpt._step(txn, {"sam", "vp"}, vpt.NICOLE)
        self.assertEqual((status, owner, role, satisfied), (wf.STATUS_READY_FOR_TREASURY, None, "Treasury", self.ALL_THREE))
        self.assertEqual(txn["vp_approver_user_key"], vpt.NICOLE)          # the VP responsibility is recorded on the transaction

    def test_2_the_same_person_in_all_three_roles_with_a_cfo_required_approves_once_then_the_cfo_acts(self):
        txn = _vp_assigned(requires_cfo=True)
        status, owner, role, satisfied = vpt._step(txn, {"sam", "vp"}, vpt.NICOLE)
        self.assertEqual((status, owner, role, satisfied), (wf.STATUS_PENDING_CFO, vpt.WENDY, "CFO", self.ALL_THREE))
        self.assertEqual((txn["vp_approver_user_key"], txn["cfo_approver_user_key"]), (vpt.NICOLE, vpt.WENDY))
        self.assertEqual(vpt._step(txn, "cfo", vpt.WENDY)[:2], (wf.STATUS_READY_FOR_TREASURY, None))

    def test_3_7_a_different_controller_is_never_skipped_and_the_vp_acts_separately_afterwards(self):
        txn = _vp_assigned(selected_controller_user_key=vpt.CTRL, requires_cfo=True)
        trail = [vpt._step(txn, "sam", vpt.NICOLE)[:3], vpt._step(txn, "controller", vpt.CTRL)[:3],
                 vpt._step(txn, "vp", vpt.NICOLE)[:3], vpt._step(txn, "cfo", vpt.WENDY)[:3]]
        self.assertEqual(trail, [(wf.STATUS_PENDING_CONTROLLER, vpt.CTRL, "Controller"), (wf.STATUS_PENDING_VP, vpt.NICOLE, "VP"),
                                 (wf.STATUS_PENDING_CFO, vpt.WENDY, "CFO"), (wf.STATUS_READY_FOR_TREASURY, None, "Treasury")])

    def test_4_5_the_vp_is_always_the_one_for_the_transactions_team(self):
        for classification, vp in (("Corporate", vpt.NICOLE), ("Property", vpt.SEUNG)):
            with self.subTest(classification=classification):
                txn = _vp_assigned(entity_classification=classification, selected_approver_user_key=vp, selected_controller_user_key=vp)
                self.assertEqual(vpt._step(txn, {"sam", "vp"}, vp)[:2], (wf.STATUS_READY_FOR_TREASURY, None))
                self.assertEqual(txn["vp_approver_user_key"], vp)
                txn = _vp_assigned(entity_classification=classification, selected_approver_user_key=vp, selected_controller_user_key=vp, requires_cfo=True)
                self.assertEqual(vpt._step(txn, {"sam", "vp"}, vp)[:3], (wf.STATUS_PENDING_CFO, vpt.WENDY, "CFO"))

    def test_4_the_shortcut_needs_the_resolved_vp_to_be_that_exact_user_key(self):
        txn = _vp_assigned(selected_approver_user_key=vpt.APPR, selected_controller_user_key=vpt.APPR)    # approver==controller, VP is someone else
        self.assertEqual(vpt._step(txn, "sam", vpt.APPR)[:3], (wf.STATUS_PENDING_VP, vpt.NICOLE, "VP"))
        stale = _vp_assigned(vp_approver_user_key=vpt.SEUNG)                                               # a different VP already stored
        self.assertEqual(vpt._step(stale, {"sam", "vp"}, vpt.NICOLE)[:3], (wf.STATUS_PENDING_VP, vpt.SEUNG, "VP"))

    def test_only_two_matching_roles_keep_the_existing_rules_no_new_shortcuts(self):
        controller_is_vp = _vp_assigned(selected_approver_user_key=vpt.APPR, selected_controller_user_key=vpt.NICOLE)
        self.assertEqual(vpt._step(controller_is_vp, "sam", vpt.APPR)[:2], (wf.STATUS_PENDING_CONTROLLER, vpt.NICOLE))
        self.assertEqual(vpt._step(controller_is_vp, "controller", vpt.NICOLE)[:3], (wf.STATUS_PENDING_VP, vpt.NICOLE, "VP"))
        approver_is_controller = _vp_assigned(selected_approver_user_key=vpt.APPR, selected_controller_user_key=vpt.APPR)
        status, _, _, satisfied = vpt._step(approver_is_controller, "sam", vpt.APPR)
        self.assertEqual((status, satisfied), (wf.STATUS_PENDING_VP, ["Approver", "Controller"]))
        approver_is_vp = _vp_assigned(selected_controller_user_key=vpt.CTRL)
        self.assertEqual(vpt._step(approver_is_vp, "sam", vpt.NICOLE)[0], wf.STATUS_PENDING_CONTROLLER)

    def test_14_the_normal_approver_controller_shortcut_is_unchanged(self):
        txn = _txn(selected_controller_user_key=vpt.APPR)
        status, _, _, satisfied = vpt._step(txn, "sam", vpt.APPR)
        self.assertEqual((status, satisfied), (wf.STATUS_READY_FOR_TREASURY, ["Approver", "Controller"]))

    def test_6_the_cfo_is_never_collapsed_into_another_role(self):
        txn = _vp_assigned(requires_cfo=True)
        status, owner, role, satisfied = vpt._step(txn, {"sam", "vp"}, vpt.NICOLE, cfos=[vpt.NICOLE])   # even if the CFO is the same person
        self.assertEqual((status, owner, role), (wf.STATUS_PENDING_CFO, vpt.NICOLE, "CFO"))
        self.assertNotIn("CFO", satisfied)
        self.assertEqual(vpt._step(txn, "cfo", vpt.NICOLE)[:2], (wf.STATUS_READY_FOR_TREASURY, None))

    def test_13_a_failed_cfo_resolution_changes_nothing(self):
        for cfos in ([], [vpt.WENDY, vpt.OTHER_CFO]):
            with self.subTest(cfos=cfos):
                txn = _vp_assigned(requires_cfo=True)
                before = dict(txn)
                with self.assertRaises(wf.WorkflowConfigurationError):
                    vpt._step(txn, {"sam", "vp"}, vpt.NICOLE, cfos=cfos)
                self.assertEqual(txn, before)               # no VP key, owner or status was recorded either

    def test_a_missing_or_ambiguous_vp_changes_nothing(self):
        for vps in ({"CA": []}, {"CA": [vpt.NICOLE, 99]}):
            with self.subTest(vps=vps):
                txn = _vp_assigned()
                before = dict(txn)
                with self.assertRaises(wf.WorkflowConfigurationError):
                    vpt._step(txn, {"sam", "vp"}, vpt.NICOLE, vps=vps)
                self.assertEqual(txn, before)

    def test_requires_no_vp_means_the_original_combined_approval_only(self):
        txn = _vp_assigned(requires_vp=False)
        status, _, _, satisfied = vpt._step(txn, {"sam", "vp"}, vpt.NICOLE)
        self.assertEqual((status, satisfied), (wf.STATUS_READY_FOR_TREASURY, ["Approver", "Controller"]))

    def test_11_only_the_assigned_user_may_take_the_combined_action(self):
        txn = _vp_assigned()
        wf.authorize_action(role={"sam", "vp"}, user_key=vpt.NICOLE, txn=txn, action=wf.ACTION_APPROVE)
        for role, key in (({"sam", "vp"}, vpt.SEUNG), ("vp", vpt.NICOLE), ("controller", vpt.NICOLE), ("submitter", REQUESTER),
                          ({"sam", "controller", "submitter"}, REQUESTER)):
            with self.subTest(role=role, key=key), self.assertRaises(wf.UnauthorizedActionError):
                wf.authorize_action(role=role, user_key=key, txn=txn, action=wf.ACTION_APPROVE)

    def test_the_combined_approval_needs_the_approver_role_not_the_controller_role(self):
        txn = _vp_assigned()
        wf.authorize_action(role={"sam", "vp"}, user_key=vpt.NICOLE, txn=txn, action=wf.ACTION_APPROVE)
        with self.assertRaises(wf.UnauthorizedActionError):
            wf.authorize_action(role={"vp"}, user_key=vpt.NICOLE, txn=txn, action=wf.ACTION_APPROVE)

    def test_a_finished_transaction_accepts_no_second_approval(self):
        txn = _vp_assigned()
        vpt._step(txn, {"sam", "vp"}, vpt.NICOLE)
        with self.assertRaises(wf.UnauthorizedActionError):         # now Ready for Treasury: a repeat approval is refused
            wf.authorize_action(role={"sam", "vp"}, user_key=vpt.NICOLE, txn=txn, action=wf.ACTION_APPROVE)


class EventContractTests(unittest.TestCase):
    def _txn(self, **over):
        return _txn(**{**dict(selected_approver_user_key=vpt.NICOLE, selected_controller_user_key=vpt.NICOLE, requires_vp=True,
                    vp_approver_user_key=vpt.NICOLE, entity_classification="Corporate"), **over})

    def test_8_9_10_a_three_role_approval_with_a_cfo_writes_approve_and_cfo_assigned_with_one_consistent_assignment(self):
        w = _run(wf.ACTION_APPROVE, self._txn(requires_cfo=True), actor=vpt.NICOLE)
        self.assertEqual(w.event_types, ["approve", "CFO_ASSIGNED"])
        self.assertEqual((w.owner, w.assignment[1:3]), (w.assignment[2], ["CFO", w.owner]))       # CurrentOwner == the new assignment
        self.assertEqual(sum("INSERT INTO etransactions.WorkflowAssignment" in s for s in w.statements), 1)
        self.assertEqual((w.related("approve"), w.related("CFO_ASSIGNED")), (None, 77))
        for absent in ("VP_ASSIGNED", "CONTROLLER_ASSIGNED"):                                     # no artificial stage, no duplicate email trigger
            self.assertNotIn(absent, w.event_types)

    def test_a_three_role_approval_without_a_cfo_writes_approve_and_ready_for_treasury(self):
        w = _run(wf.ACTION_APPROVE, self._txn(), actor=vpt.NICOLE)
        self.assertEqual(w.event_types, ["approve", "READY_FOR_TREASURY"])
        self.assertIsNone(w.assignment)

    def test_a_different_controller_still_produces_the_controller_assignment(self):
        w = _run(wf.ACTION_APPROVE, self._txn(selected_controller_user_key=vpt.CTRL), actor=vpt.NICOLE)
        self.assertEqual(w.event_types, ["approve", "CONTROLLER_ASSIGNED"])

    def test_the_event_literals_and_assignment_events_are_unchanged(self):
        self.assertEqual([wf.EVENT_REQUEST_SUBMITTED, wf.EVENT_APPROVER_ASSIGNED, wf.EVENT_CONTROLLER_ASSIGNED, wf.EVENT_APPROVAL,
                          wf.EVENT_VP_ASSIGNED, wf.EVENT_CFO_ASSIGNED, wf.EVENT_RFI_REQUESTED, wf.EVENT_RFI_RESPONSE_SUBMITTED],
                         ["Submitted", "APPROVER_ASSIGNED", "CONTROLLER_ASSIGNED", "approve", "VP_ASSIGNED", "CFO_ASSIGNED",
                          "more_info", "requester_respond"])

    def test_26_an_rfi_from_the_combined_stage_returns_to_the_vp(self):
        txn = _vp_assigned(status=wf.STATUS_MORE_INFO, current_owner_user_key=REQUESTER)
        txn["rfi_origin_status"] = wf.STATUS_PENDING_APPROVER
        self.assertEqual(wf.determine_next_step(txn, wf.ACTION_REQUESTER_RESPOND)[:3], (wf.STATUS_PENDING_APPROVER, vpt.NICOLE, "Approver"))


class AtomicTransitionTests(unittest.TestCase):
    def _advance(self, *, rowcount=1, fail_on=None):
        cur = MagicMock()
        cur.rowcount = rowcount
        cur.fetchone.return_value = (77,)
        if fail_on:
            def execute(sql, *a):
                if fail_on in sql:
                    raise RuntimeError("boom")
            cur.execute.side_effect = execute
        conn = MagicMock()
        conn.cursor.return_value = cur
        with patch.object(db, "get_connection", return_value=conn):
            db.advance_transaction_workflow(
                1, from_status=wf.STATUS_PENDING_APPROVER, new_status=wf.STATUS_PENDING_CFO, new_owner_user_key=vpt.WENDY,
                actor_user_key=vpt.NICOLE, actor_role="r", action=wf.ACTION_APPROVE, workflow_role="CFO",
                vp_approver_user_key=vpt.NICOLE, cfo_approver_user_key=vpt.WENDY)
        return cur, conn

    def test_the_vp_key_cfo_key_owner_assignment_and_events_are_one_transaction(self):
        cur, conn = self._advance()
        sql, params = cur.execute.call_args_list[0].args
        self.assertIn("VPApprover_User_Key = ?", sql)
        self.assertIn("CFOApprover_User_Key = ?", sql)
        self.assertTrue(sql.rstrip().endswith("AND VPApprover_User_Key IS NULL AND CFOApprover_User_Key IS NULL"))
        self.assertIn(wf.STATUS_PENDING_CFO, params)
        self.assertEqual(params[-1], wf.STATUS_PENDING_APPROVER)                          # guarded by the expected status
        conn.commit.assert_called_once()

    def test_12_a_duplicate_action_writes_nothing(self):
        cur = MagicMock()
        cur.rowcount = 0
        conn = MagicMock()
        conn.cursor.return_value = cur
        with patch.object(db, "get_connection", return_value=conn), self.assertRaises(db.WorkflowConflictError):
            db.advance_transaction_workflow(
                1, from_status=wf.STATUS_PENDING_APPROVER, new_status=wf.STATUS_PENDING_CFO, new_owner_user_key=vpt.WENDY,
                actor_user_key=vpt.NICOLE, actor_role="r", action=wf.ACTION_APPROVE, workflow_role="CFO",
                vp_approver_user_key=vpt.NICOLE, cfo_approver_user_key=vpt.WENDY)
        self.assertEqual(cur.execute.call_count, 1)
        conn.rollback.assert_called_once()
        conn.commit.assert_not_called()

    def test_a_failure_part_way_rolls_everything_back(self):
        for fail_on in ("INSERT INTO etransactions.WorkflowAssignment", "INSERT INTO etransactions.WorkflowEvent"):
            with self.subTest(fail_on=fail_on):
                conn = MagicMock()
                with self.assertRaises(RuntimeError):
                    with patch.object(db, "get_connection", return_value=conn):
                        cur = MagicMock(rowcount=1)
                        cur.fetchone.return_value = (77,)
                        cur.execute.side_effect = lambda sql, *a: (_ for _ in ()).throw(RuntimeError("boom")) if fail_on in sql else None
                        conn.cursor.return_value = cur
                        db.advance_transaction_workflow(
                            1, from_status=wf.STATUS_PENDING_APPROVER, new_status=wf.STATUS_PENDING_CFO, new_owner_user_key=vpt.WENDY,
                            actor_user_key=vpt.NICOLE, actor_role="r", action=wf.ACTION_APPROVE, workflow_role="CFO",
                            vp_approver_user_key=vpt.NICOLE, cfo_approver_user_key=vpt.WENDY)
                conn.rollback.assert_called_once()
                conn.commit.assert_not_called()


class CombinedApprovalRouteTests(unittest.TestCase):
    setUp = vpt.RouteTests.setUp
    _post = vpt.RouteTests._post

    def test_1_the_route_records_one_approval_for_all_three_roles_and_sends_the_transaction_to_treasury(self):
        txn = _vp_assigned(status=wf.STATUS_PENDING_APPROVER)
        resp, advance, flashes = self._post("sam", vpt.NICOLE, txn)
        kwargs = advance.call_args.kwargs
        self.assertEqual((kwargs["new_status"], kwargs["new_owner_user_key"], kwargs["vp_approver_user_key"], kwargs["cfo_approver_user_key"]),
                         (wf.STATUS_READY_FOR_TREASURY, None, vpt.NICOLE, None))
        self.assertIn("One approval satisfied: Approver, Controller, VP (same employee).", kwargs["comments"])
        self.vp_lookup.assert_called_once_with("CA")
        self.cfo_lookup.assert_not_called()
        self.assertEqual(flashes[0][0], "success")

    def test_2_the_route_assigns_the_cfo_in_the_same_call_for_a_cfo_level_transaction(self):
        txn = _vp_assigned(status=wf.STATUS_PENDING_APPROVER, requires_cfo=True)
        resp, advance, flashes = self._post("sam", vpt.NICOLE, txn)
        advance.assert_called_once()
        kwargs = advance.call_args.kwargs
        self.assertEqual((kwargs["new_status"], kwargs["new_owner_user_key"], kwargs["workflow_role"],
                          kwargs["vp_approver_user_key"], kwargs["cfo_approver_user_key"]),
                         (wf.STATUS_PENDING_CFO, vpt.WENDY, "CFO", vpt.NICOLE, vpt.WENDY))
        self.assertIn("Approver, Controller, VP", kwargs["comments"])
        self.cfo_lookup.assert_called_once_with()

    def test_5_a_property_transaction_uses_the_pa_vp(self):
        txn = _vp_assigned(status=wf.STATUS_PENDING_APPROVER, entity_classification="Property",
                           selected_approver_user_key=vpt.SEUNG, selected_controller_user_key=vpt.SEUNG, current_owner_user_key=vpt.SEUNG)
        resp, advance, _ = self._post("sam", vpt.SEUNG, txn, vps=(vpt.SEUNG,))
        self.vp_lookup.assert_called_once_with("PA")
        kwargs = advance.call_args.kwargs
        self.assertEqual((kwargs["new_status"], kwargs["vp_approver_user_key"]), (wf.STATUS_READY_FOR_TREASURY, vpt.SEUNG))

    def test_13_two_active_cfos_stop_the_approval_before_anything_is_written(self):
        txn = _vp_assigned(status=wf.STATUS_PENDING_APPROVER, requires_cfo=True)
        resp, advance, flashes = self._post("sam", vpt.NICOLE, txn, cfos=(vpt.WENDY, vpt.OTHER_CFO))
        advance.assert_not_called()
        self.assertIn("More than one eligible CFO", flashes[0][1])
        self.assertEqual(flashes[0][0], "danger")

    def test_3_a_different_controller_keeps_the_original_chain_and_looks_up_no_vp_yet(self):
        txn = _vp_assigned(status=wf.STATUS_PENDING_APPROVER, selected_controller_user_key=vpt.CTRL)
        resp, advance, _ = self._post("sam", vpt.NICOLE, txn)
        kwargs = advance.call_args.kwargs
        self.assertEqual((kwargs["new_status"], kwargs["new_owner_user_key"], kwargs["workflow_role"], kwargs["vp_approver_user_key"]),
                         (wf.STATUS_PENDING_CONTROLLER, vpt.CTRL, "Controller", None))
        self.vp_lookup.assert_not_called()

    def test_3_the_vp_acts_separately_after_a_different_controller(self):
        txn = _vp_assigned(status=wf.STATUS_PENDING_VP, selected_controller_user_key=vpt.CTRL, vp_approver_user_key=vpt.NICOLE)
        resp, advance, _ = self._post("vp", vpt.NICOLE, txn)
        self.assertEqual(advance.call_args.kwargs["new_status"], wf.STATUS_READY_FOR_TREASURY)

    def test_11_another_vp_cannot_act_on_the_transaction(self):
        txn = _vp_assigned(status=wf.STATUS_PENDING_APPROVER)
        resp, advance, flashes = self._post("sam", vpt.SEUNG, txn)
        advance.assert_not_called()
        self.assertEqual(flashes[0][0], "warning")

    def test_12_a_repeated_click_is_reported_as_already_processed(self):
        resp, advance, flashes = self._post("sam", vpt.NICOLE, _vp_assigned(status=wf.STATUS_PENDING_APPROVER),
                                            advance_error=db.WorkflowConflictError("moved"))
        self.assertEqual(flashes, [("info", "This action was already processed for this transaction.")])

    def test_a_missing_vp_stops_the_combined_approval_before_anything_is_written(self):
        resp, advance, flashes = self._post("sam", vpt.NICOLE, _vp_assigned(status=wf.STATUS_PENDING_APPROVER), vps=())
        advance.assert_not_called()
        self.assertIn("No eligible VP on the CA team", flashes[0][1])

# ═══ Property bank release ═══════════════════════════════════════════════════
class PropertyBankReleaseTests(unittest.TestCase):
    def _initiate(self, **over):
        txn = _vp_assigned(status=wf.STATUS_READY_FOR_TREASURY, entity_classification="Property",
                           selected_approver_user_key=vpt.SEUNG, selected_controller_user_key=vpt.SEUNG, **over)
        return txn, wf.determine_next_step(txn, wf.ACTION_TREASURY_INITIATED)

    def test_20_below_the_vp_threshold_the_assigned_vp_is_the_releaser_not_the_requesting_controller(self):
        txn, (status, owner, role, _) = self._initiate(requires_vp=False)
        self.assertEqual((status, owner, role), (wf.STATUS_AWAITING_RELEASE, vpt.SEUNG, "Bank Releaser"))
        self.assertNotEqual(owner, REQUESTER)

    def test_21_at_or_above_the_threshold_the_vp_remains_the_releaser(self):
        _, (status, owner, _, _) = self._initiate(requires_vp=True, vp_approver_user_key=vpt.SEUNG)
        self.assertEqual((status, owner), (wf.STATUS_AWAITING_RELEASE, vpt.SEUNG))

    def test_21_a_missing_vp_is_an_error_never_the_original_requester(self):
        with self.assertRaisesRegex(wf.WorkflowConfigurationError, "bank releaser cannot be determined"):
            self._initiate(requires_vp=True, vp_approver_user_key=None)

    def test_only_the_designated_releaser_may_complete_the_release(self):
        txn = _vp_assigned(status=wf.STATUS_AWAITING_RELEASE, entity_classification="Property", bank_releaser_user_key=vpt.SEUNG)
        wf.authorize_action(role={"vp", "sam"}, user_key=vpt.SEUNG, txn=txn, action=wf.ACTION_BANK_RELEASE)
        for role, key in (({"controller", "submitter"}, REQUESTER), ({"vp"}, vpt.NICOLE)):
            with self.subTest(key=key), self.assertRaises(wf.UnauthorizedActionError):
                wf.authorize_action(role=role, user_key=key, txn=txn, action=wf.ACTION_BANK_RELEASE)

    def test_corporate_treasury_release_is_unchanged(self):
        txn = _vp_assigned(status=wf.STATUS_READY_FOR_TREASURY)
        self.assertEqual(wf.determine_next_step(txn, wf.ACTION_TREASURY_RELEASED)[:2], (wf.STATUS_COMPLETED, None))


class ExternalIsolationTests(unittest.TestCase):
    def test_no_external_access_was_attempted(self):
        self.assertEqual(external_guard.state.blocked, [])


if __name__ == "__main__":
    unittest.main()
