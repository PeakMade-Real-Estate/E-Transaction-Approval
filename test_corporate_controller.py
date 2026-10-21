"""
Corporate requests are routed automatically to the one Controller on the Corporate (CA) team; the intake form no
longer asks for it. Property requests keep the Controller picker. All banking/user values are fake.
"""
import external_guard  # noqa: F401  (must precede app/db/sharepoint imports)
import re
import unittest
from unittest.mock import MagicMock, patch

from werkzeug.datastructures import MultiDict

import app as app_module
import db
import test_draft_persistence as dp
import test_internal_recipient as ir
import workflow as wf

ATHENA = {"user_key": 20, "display_name": "Athena Little"}
OTHER = {"user_key": 21, "display_name": "Second Controller"}


def _lookup(*holders):
    calls = []

    def fn(team):
        calls.append(team)
        return list(holders)
    fn.calls = calls
    return fn


class ResolveCorporateControllerTests(unittest.TestCase):
    def _resolve(self, *holders):
        lookup = _lookup(*holders)
        with patch.object(app_module.db, "get_eligible_controllers", lookup):
            return app_module._resolve_corporate_controller(), lookup.calls

    def test_the_single_ca_controller_is_selected_by_the_ca_team(self):
        (controller, error), calls = self._resolve(ATHENA)
        self.assertEqual((controller, error, calls), (ATHENA, None, ["CA"]))

    def test_duplicate_rows_for_one_user_are_one_controller(self):
        (controller, error), _ = self._resolve(ATHENA, ATHENA)
        self.assertEqual((controller["user_key"], error), (20, None))

    def test_no_controller_is_a_clear_configuration_error(self):
        (controller, error), _ = self._resolve()
        self.assertIsNone(controller)
        self.assertIn("No Corporate Controller is configured", error)

    def test_several_controllers_are_an_error_never_a_pick(self):
        (controller, error), _ = self._resolve(ATHENA, OTHER)
        self.assertIsNone(controller)
        self.assertIn("More than one Corporate Controller", error)

    def test_a_lookup_failure_is_reported_without_details(self):
        with patch.object(app_module.db, "get_eligible_controllers", side_effect=RuntimeError("server.internal secret")):
            controller, error = app_module._resolve_corporate_controller()
        self.assertIsNone(controller)
        self.assertNotIn("secret", error)


class ControllerLookupSqlTests(unittest.TestCase):
    def test_the_lookup_is_strict_by_team_and_ignores_blank_team_users(self):
        cur = MagicMock()
        cur.fetchall.return_value = [(20, "Athena Little"), (20, "Athena Little")]
        conn = MagicMock()
        conn.cursor.return_value = cur
        with patch.object(db, "get_connection", return_value=conn):
            rows = external_guard.REAL_GET_ELIGIBLE_CONTROLLERS("CA")
        sql, params = cur.execute.call_args.args
        self.assertEqual(params, ["CA"])
        self.assertEqual(rows[0], {"user_key": 20, "display_name": "Athena Little"})
        for required in ("SELECT DISTINCT u.User_Key", "r.Role_Code = 'controller'", "r.Is_Active = 1", "u.Active_Status = 1",
                         "u.Team = ?", "r.Effective_Start_Date IS NULL OR", "r.Effective_End_Date IS NULL OR"):
            self.assertIn(required, sql)
        self.assertNotRegex(sql, r"Team IS NULL|Team = ''|OR u\.Team")
        self.assertRegex(sql.strip(), r"^SELECT ")

    def test_a_team_is_required(self):
        with self.assertRaises(ValueError):
            external_guard.REAL_GET_ELIGIBLE_CONTROLLERS("  ")


class _Base(dp._Base):
    def setUp(self):
        super().setUp()
        patcher = patch.object(app_module.db, "get_eligible_controllers", _lookup(ATHENA))
        patcher.start()
        self.addCleanup(patcher.stop)

    def submit(self, **overrides):
        form = ir._external_form(verbal_confirmed_with_known="on", **overrides)
        return self.client.post("/intake/submit", data={**form, **ir._files()}, content_type="multipart/form-data")


class RoutingTests(_Base):
    def test_a_corporate_request_is_assigned_to_the_ca_controller(self):
        self.assertEqual(self.submit(controller_key="3").status_code, 302)
        self.assertEqual(self.m["insert_transaction"].call_args[0][0]["controller_key"], 20)

    def test_a_forged_or_blank_posted_controller_is_ignored_for_corporate(self):
        for posted in ("99", "", "15"):
            with self.subTest(posted=posted):
                self.m["insert_transaction"].reset_mock()
                self.assertEqual(self.submit(controller_key=posted).status_code, 302)
                self.assertEqual(self.m["insert_transaction"].call_args[0][0]["controller_key"], 20)

    def test_a_missing_controller_blocks_the_submission_with_a_clear_message(self):
        with patch.object(app_module.db, "get_eligible_controllers", _lookup()):
            resp = self.submit()
        self.assertEqual(resp.status_code, 200)
        self.assertIn("No Corporate Controller is configured", resp.get_data(as_text=True))
        self.m["insert_transaction"].assert_not_called()
        self.m["reserve_request_id"].assert_not_called()

    def test_several_controllers_block_the_submission(self):
        with patch.object(app_module.db, "get_eligible_controllers", _lookup(ATHENA, OTHER)):
            resp = self.submit()
        self.assertEqual(resp.status_code, 200)
        self.assertIn("More than one Corporate Controller", resp.get_data(as_text=True))
        self.m["insert_transaction"].assert_not_called()

    def test_the_corporate_controller_cannot_submit_a_request_for_themselves(self):
        with patch.object(app_module.db, "get_eligible_controllers", _lookup({"user_key": dp.USER["user_key"], "display_name": "Owner"})):
            resp = self.submit()
        self.assertEqual(resp.status_code, 200)
        self.assertIn("You are the Corporate Controller", resp.get_data(as_text=True))
        self.m["insert_transaction"].assert_not_called()

    def test_the_assigned_controller_must_still_hold_the_controller_role(self):
        self.m["get_app_user_role_codes"].return_value = ["sam"]
        resp = self.submit()
        self.assertEqual(resp.status_code, 200)
        self.assertIn("not an eligible Controller", resp.get_data(as_text=True))
        self.m["insert_transaction"].assert_not_called()

    def test_a_draft_is_saved_with_the_ca_controller(self):
        self.save(dp._draft_form(controller_key=""))
        self.assertEqual(self.store.drafts[55]["controller_key"], 20)

    def test_a_draft_cannot_be_saved_when_the_controller_is_not_configured(self):
        with patch.object(app_module.db, "get_eligible_controllers", _lookup()):
            resp = self.save(dp._draft_form())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.store.created, [])

    def test_a_resumed_draft_is_finalized_with_the_ca_controller(self):
        self.save()
        rid = self.store.drafts[55]["request_id"]
        self.sp.seed(rid, ve=("avs.png", dp.sharepoint.DOC_TYPE_AVS_SCREENSHOT),
                     wire=("w.pdf", dp.sharepoint.DOC_TYPE_WIRE_ACH_INSTRUCTIONS), pay=("inv.pdf", dp.sharepoint.DOC_TYPE_PAYMENT_SUPPORT))
        form = dp._final_form(request_id=rid, controller_key="99")
        self.assertEqual(self.client.post("/intake/submit", data=form, content_type="multipart/form-data").status_code, 302)
        self.assertEqual(self.m["finalize_draft_submission"].call_args[0][1]["controller_key"], 20)


class PropertyAndUnclassifiedTests(unittest.TestCase):
    def test_property_requests_keep_the_selected_controller_and_never_look_one_up(self):
        lookup = _lookup(ATHENA)
        frm = MultiDict({"classification": "property", "controller_key": "28"})
        with patch.object(app_module.db, "get_eligible_controllers", lookup):
            effective, error = app_module._with_assigned_approvers(frm, {"user_key": 11})
        self.assertEqual((effective.get("controller_key"), error, lookup.calls), ("28", None, []))

    def test_unclassified_requests_are_left_alone(self):
        frm = MultiDict({"controller_key": "28"})
        with patch.object(app_module.db, "get_eligible_controllers", _lookup(ATHENA)):
            effective, error = app_module._with_assigned_approvers(frm, {"user_key": 11})
        self.assertEqual((effective.get("controller_key"), error), ("28", None))

    def test_the_original_form_is_never_mutated(self):
        frm = MultiDict({"classification": "Corporate", "controller_key": "3"})
        with patch.object(app_module.db, "get_eligible_controllers", _lookup(ATHENA)):
            effective, _ = app_module._with_assigned_approvers(frm, {"user_key": 11})
        self.assertEqual((effective.get("controller_key"), frm.get("controller_key")), ("20", "3"))


class PrecheckTests(_Base):
    def _precheck(self, **overrides):
        form = ir._external_form(has_validation_evidence="true", has_wire_ach_instructions="true", has_payment_support="true",
                                 verbal_confirmed_with_known="on", **overrides)
        return self.client.post("/intake/precheck", data=form).get_json()

    def test_a_corporate_precheck_needs_no_posted_controller(self):
        self.assertEqual(self._precheck(controller_key=""), {"success": True, "errors": []})

    def test_a_precheck_reports_a_missing_controller_configuration(self):
        with patch.object(app_module.db, "get_eligible_controllers", _lookup()):
            body = self._precheck()
        self.assertEqual(len(body["errors"]), 1)
        self.assertIn("No Corporate Controller is configured", body["errors"][0])


class IntakePageTests(_Base):
    def _page(self):
        return self.client.get("/intake").get_data(as_text=True)

    def _ui(self, *holders):
        with patch.object(app_module.db, "get_eligible_controllers", _lookup(*holders)):
            with app_module.app.test_request_context("/intake"):
                return app_module._assignment_ui()

    def test_the_page_carries_the_automatic_controller_for_corporate_only(self):
        ui = self._ui(ATHENA)
        self.assertEqual(ui["by_class"]["corporate"]["controller"],
                         {"name": "Athena Little", "note": app_module.CORPORATE_CONTROLLER_NOTE, "error": ""})
        self.assertNotIn("approver", ui["by_class"]["corporate"])
        self.assertEqual((ui["by_class"]["property"], ui["by_class"][""]), ({}, {}))

    def test_the_picker_stays_and_script_swaps_it_for_the_assigned_value(self):
        body = self._page()
        for ident in ("controller-picker", "controller-auto", "approver-picker", "approver-auto"):
            self.assertIn(f'id="{ident}"', body)
        self.assertRegex(body, r'<select class="form-select" id="controller" name="controller_key" required>')
        self.assertIn("pickerField.required = !assigned", body)
        self.assertIn("Athena Little", body)

    def test_a_misconfiguration_is_shown_instead_of_a_name(self):
        ui = self._ui(ATHENA, OTHER)
        entry = ui["by_class"]["corporate"]["controller"]
        self.assertEqual(entry["name"], "")
        self.assertIn("More than one Corporate Controller", entry["error"])

    def test_when_sql_is_off_nothing_is_assigned(self):
        with patch.object(app_module, "database_enabled", return_value=False):
            ui = app_module._assignment_ui()
        self.assertEqual(ui, {"controller_requester": False, "by_class": {"": {}, "corporate": {}, "property": {}}})

class UnchangedBehaviorTests(unittest.TestCase):
    def test_approvals_and_workflow_are_untouched(self):
        import inspect
        source = inspect.getsource(app_module._resolve_corporate_controller) + inspect.getsource(app_module._with_assigned_approvers)
        for sensitive in ("advance_transaction_workflow", "WorkflowEvent", "has_role", "approval_rule"):
            self.assertNotIn(sensitive, source)

    def test_the_team_constant_is_the_ca_team(self):
        self.assertEqual(wf.TEAM_CORPORATE, "CA")


if __name__ == "__main__":
    unittest.main()
