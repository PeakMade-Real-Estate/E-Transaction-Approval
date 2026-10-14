"""
Tests for Item 5: "Same Day ACH" request type with automatic urgency.

Selecting Same Day ACH always stores Urgent=True and Urgency_Reason="Same Day ACH",
derived server-side from the request type (never from the browser) in validation,
precheck, Save Draft, final submit and Draft finalization. Every other request type
keeps its manual urgency behavior. All existing ACH banking/verification rules apply to
Same Day ACH unchanged; the internal-ACH attachment exemption and the internal-recipient
selector stay ACH-only. No cutoff time / time-of-day rule exists.

Mock-only: `external_guard` (imported first) makes the database, SharePoint and network inert.

Run:  python -m unittest test_same_day_ach -v
"""
import csv
import io
import re
import unittest
import external_guard  # noqa: F401  (must precede app/db/sharepoint imports)
from unittest.mock import patch

import app as app_module
import db
import export
import workflow as wf
import test_draft as td                      # shared fixtures only; classes are not re-collected here
import test_intake_precheck as pre
import test_internal_recipient as ir
import test_prior_use_verification as pu

SDA = "Same Day ACH"
WIRE_REQUIRED = "External ACH/Wire Instructions attachment is required"
URGENCY_REQUIRED = "Urgency Reason is required when the request is marked urgent."


def _sda_form(**overrides):
    """A complete, valid EXTERNAL Same Day ACH submission as a browser might post it
    (urgent deliberately 'no' with a blank reason: the server must normalize it)."""
    overrides = {"urgent": "no", "urgency_reason": "", **overrides}
    return ir._external_form(request_type=SDA, **overrides)


def _all_files():
    return {"validation_evidence": True, "wire_ach_instructions": True, "payment_support": True}


def _validate(frm, **kwargs):
    kwargs.setdefault("files_present", _all_files())
    kwargs.setdefault("bank_account_status", "Open")
    return app_module._validate_intake_submission(frm, **kwargs)


class UrgencyHelperTests(unittest.TestCase):
    def test_same_day_ach_is_always_urgent_with_the_fixed_reason(self):
        for forged in ({"urgent": "no"}, {"urgent": "yes", "urgency_reason": "Vendor deadline"},
                       {"urgent": "no", "urgency_reason": "something else"}, {}):
            with self.subTest(forged=forged):
                self.assertEqual(app_module.effective_urgency(dict(request_type=SDA, **forged)), (True, SDA))
        self.assertEqual(app_module.effective_urgency({"request_type": f"  {SDA} "}), (True, SDA))

    def test_other_types_keep_the_requesters_manual_urgency(self):
        for rtype in ("ACH", "Wire", "ACH Pull", "Intra Bank Transfer", "EFT"):
            with self.subTest(rtype=rtype):
                self.assertEqual(app_module.effective_urgency({"request_type": rtype, "urgent": "no"}), (False, ""))
                self.assertEqual(
                    app_module.effective_urgency({"request_type": rtype, "urgent": "yes", "urgency_reason": "Vendor deadline"}),
                    (True, "Vendor deadline"))

    def test_only_the_exact_canonical_type_triggers_it(self):
        for lookalike in ("same day ach", "Same Day  ACH", "ACH", "Same Day", ""):
            with self.subTest(lookalike=lookalike):
                self.assertEqual(app_module.effective_urgency({"request_type": lookalike, "urgent": "no"}), (False, ""))


class IntakePageTests(ir._RouteBase):
    def _page(self):
        with patch.object(app_module, "current_app_user", return_value={"user_key": 11, "display_name": "U", "email": ""}), \
             patch.object(app_module.db, "get_user_list", return_value=[]), \
             patch.object(app_module.db, "get_bank_accounts", return_value=[]):
            return self.client.get("/intake").get_data(as_text=True)

    # 1, 2
    def test_01_02_same_day_ach_is_selectable_and_every_existing_type_remains(self):
        body = self._page()
        values = re.findall(r'<option value="([^"]*)"', body[body.index('id="request_type"'):])
        for expected in ("Wire", "ACH", SDA, "ACH Pull", "Intra Bank Transfer", "EFT"):
            self.assertIn(expected, values)
        self.assertLess(values.index("ACH"), values.index(SDA))

    # 11, 12 (markup level; the behaviour itself is a browser check)
    def test_11_12_switching_logic_touches_only_the_urgency_controls(self):
        body = self._page()
        start = body.index("function applyAutomaticUrgency")
        func = body[start: body.index("\n    }\n", start)]
        for untouched in ("file_", "recv_", "payment_", "amount", "recipient", ".files", "form.reset"):
            self.assertNotIn(untouched, func)
        self.assertIn("manualUrgency", func)  # the requester's own choice is held and restored
        self.assertIn("urgent_no", func)
        self.assertIn("applyAutomaticUrgency(false)", body)  # wired to request-type change
        self.assertIn("applyAutomaticUrgency(true)", body)   # and to a resumed Draft on load

    def test_intake_page_shows_the_automatic_urgency_note_and_no_cutoff_rule(self):
        body = self._page()
        self.assertIn("Same Day ACH requests are automatically marked urgent.", body)
        self.assertNotRegex(body, r"(?i)cut-?off\s+(time\s+)?(is|of)\s+\d|2:30|2:45|14:30|14:45")


class ServerNormalizationRouteTests(ir._RouteBase):
    # 3, 4, 5, 6, 17
    def test_03_04_17_same_day_ach_persists_distinct_type_urgent_and_fixed_reason(self):
        resp, m = self._post(_sda_form())
        self.assertEqual(resp.status_code, 302)
        data = m["insert_transaction"].call_args[0][0]
        self.assertEqual(data["request_type"], SDA)
        self.assertIs(data["urgent"], True)
        self.assertEqual(data["urgency_reason"], SDA)

    def test_05_forged_urgent_false_is_normalized(self):
        resp, m = self._post(_sda_form(urgent="no"))
        self.assertTrue(m["insert_transaction"].call_args[0][0]["urgent"])
        resp, m = self._post(_sda_form(urgent=""))  # radio missing entirely
        self.assertTrue(m["insert_transaction"].call_args[0][0]["urgent"])

    def test_06_forged_urgency_reason_is_replaced(self):
        for forged in ("Vendor deadline", "", "   ", "x" * 300):
            with self.subTest(forged=forged[:12]):
                resp, m = self._post(_sda_form(urgent="yes", urgency_reason=forged))
                self.assertEqual(resp.status_code, 302)
                self.assertEqual(m["insert_transaction"].call_args[0][0]["urgency_reason"], SDA)

    # 7, 8
    def test_07_regular_ach_does_not_become_urgent(self):
        resp, m = self._post(ir._external_form(urgent="no", urgency_reason=""))
        data = m["insert_transaction"].call_args[0][0]
        self.assertIs(data["urgent"], False)
        self.assertEqual(data["urgency_reason"], "")
        self.assertEqual(data["request_type"], "ACH")

    def test_08_manual_urgency_rules_are_unchanged_for_every_other_type(self):
        for rtype in ("ACH", "Wire", "ACH Pull", "EFT"):
            with self.subTest(rtype=rtype):
                extra = {"recv_bank_address": "1 Main St"} if rtype == "Wire" else {}
                resp, m = self._post(ir._external_form(request_type=rtype, urgent="yes",
                                                       urgency_reason="Vendor deadline", **extra))
                self.assertEqual(resp.status_code, 302)
                data = m["insert_transaction"].call_args[0][0]
                self.assertTrue(data["urgent"])
                self.assertEqual(data["urgency_reason"], "Vendor deadline")
                resp, m = self._post(ir._external_form(request_type=rtype, urgent="yes", urgency_reason="", **extra))
                self.assertEqual(resp.status_code, 200)
                self.assertIn(URGENCY_REQUIRED, resp.get_data(as_text=True))
                m["insert_transaction"].assert_not_called()

    # 9, 10 (server side: the type posted decides; stale urgency values never leak across)
    def test_09_changing_same_day_ach_back_to_ach_releases_the_automatic_urgency(self):
        resp, m = self._post(ir._external_form(request_type="ACH", urgent="no", urgency_reason=""))
        data = m["insert_transaction"].call_args[0][0]
        self.assertFalse(data["urgent"])
        self.assertNotEqual(data["urgency_reason"], SDA)

    def test_10_changing_ach_to_same_day_ach_applies_it(self):
        resp, m = self._post(ir._external_form(request_type=SDA, urgent="no", urgency_reason="Vendor deadline"))
        data = m["insert_transaction"].call_args[0][0]
        self.assertTrue(data["urgent"])
        self.assertEqual(data["urgency_reason"], SDA)

    # 11, 12 (server side)
    def test_11_12_unrelated_fields_and_attachments_are_untouched(self):
        resp, m = self._post(_sda_form(payment_purpose="Rent", amount="2500.00", recv_payee_name="Zed LLC"))
        data = m["insert_transaction"].call_args[0][0]
        self.assertEqual(data["payment_purpose"], "Rent")
        self.assertEqual(data["amount"], 2500.0)
        self.assertEqual(data["recv_payee_name"], "Zed LLC")
        self.assertEqual(data["recv_account_number"], "12345678")
        m["_upload_required_intake_attachments"].assert_called_once()

    def test_25_26_27_approval_routing_and_workflow_inputs_are_the_same_as_ach(self):
        for classification, dept in (("corporate", "Sunset Ridge"), ("property", "")):
            captured = {}
            for rtype in ("ACH", SDA):
                extra = {"property_entity_number": "124"} if classification == "property" else {}
                resp, m = self._post(ir._external_form(request_type=rtype, classification=classification,
                                                       property_dept=dept, **extra))
                self.assertEqual(resp.status_code, 302, (classification, rtype))
                # The rule is resolved from the amount alone, never the request type.
                m["resolve_approval_rule"].assert_called_once_with(1000.0)
                data = m["insert_transaction"].call_args[0][0]
                captured[rtype] = {k: v for k, v in data.items()
                                   if k not in ("request_type", "urgent", "urgency_reason")}
            self.assertEqual(captured["ACH"], captured[SDA])
            self.assertEqual(captured[SDA]["status"], wf.STATUS_PENDING_APPROVER)


class BankingAndVerificationRulesStillApplyTests(ir._RouteBase):
    # 21
    def test_21_ordinary_ach_banking_validation_applies_to_same_day_ach(self):
        for field, message in (("recv_routing_number", "Receiving Routing Number is required"),
                               ("recv_account_number", "Receiving Account Number is required"),
                               ("recv_bank_name", "Receiving Bank Name is required"),
                               ("recv_account_name", "Receiving Account Name is required"),
                               ("recv_payee_name", "Recipient / Payee Name is required")):
            with self.subTest(field=field):
                resp, m = self._post(_sda_form(**{field: ""}))
                self.assertEqual(resp.status_code, 200)
                self.assertIn(message, resp.get_data(as_text=True))
                m["insert_transaction"].assert_not_called()

    # 22
    def test_22_avs_evidence_and_payment_support_remain_enforced(self):
        for omitted, message in (("file_validation_evidence", "Validation Evidence attachment is required"),
                                 ("file_payment_support", "Payment Support attachment is required"),
                                 ("file_wire_ach_instructions", WIRE_REQUIRED)):
            with self.subTest(omitted=omitted):
                resp, m = self._post(_sda_form(), files=ir._files(**{omitted: True}))
                self.assertEqual(resp.status_code, 200)
                self.assertIn(message, resp.get_data(as_text=True))
                m["insert_transaction"].assert_not_called()
        resp, m = self._post(_sda_form(avs_score="150"))
        self.assertIn("AVS Score must be between 0 and 100.", resp.get_data(as_text=True))

    # 23
    def test_23_verbal_confirmation_and_prior_use_rules_are_unchanged(self):
        no_verbal = _sda_form(verbal_confirmed_with_known="", verbal_contact_name="", verbal_confirm_datetime="")
        resp, m = self._post(no_verbal)
        body = resp.get_data(as_text=True)
        self.assertIn("Verbal confirmation with a known contact is required", body)
        match = {"transaction_key": 42, "request_id": "TXN-2026-001", "last_used_date": "2026-01-01"}
        resp, m = self._post(_sda_form(), prior_match=match)  # exact match exists, claim/ack missing
        self.assertEqual(resp.status_code, 200)
        m["insert_transaction"].assert_not_called()
        resp, m = self._post(_sda_form(instructions_previously_used="yes", last_used_date="2026-01-01",
                                       prior_use_ack="TXN-2026-001", avs_score="", verbal_confirmed_with_known=""),
                             prior_match=match)
        self.assertEqual(resp.status_code, 302)

    # 24
    def test_24_internal_ach_exemption_and_internal_recipient_stay_ach_only(self):
        self.assertFalse(app_module.external_instructions_exempt(
            request_type=SDA, originating_key=5, originating_status="Open", recipient_key=9, recipient_status="Open"))
        self.assertTrue(app_module.external_instructions_exempt(
            request_type="ACH", originating_key=5, originating_status="Open", recipient_key=9, recipient_status="Open"))
        # Internal recipient is not offered for Same Day ACH: rejected, never silently exempted.
        resp, m = self._post(ir._form(request_type=SDA, urgent="no"), summary=ir._summary(),
                             files=ir._files(file_wire_ach_instructions=True))
        body = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("only be the recipient of an ACH or Intra Bank Transfer", body)
        self.assertIn(WIRE_REQUIRED, body)
        m["insert_transaction"].assert_not_called()
        self.assertEqual(app_module.INTERNAL_RECIPIENT_REQUEST_TYPES, ("ACH", "Intra Bank Transfer"))
        # An external Same Day ACH still needs the instructions attachment.
        resp, m = self._post(_sda_form(), files=ir._files(file_wire_ach_instructions=True))
        self.assertIn(WIRE_REQUIRED, resp.get_data(as_text=True))

    # 29
    def test_29_existing_ach_submission_is_backward_compatible(self):
        resp, m = self._post(ir._external_form())
        self.assertEqual(resp.status_code, 302)
        data = m["insert_transaction"].call_args[0][0]
        self.assertEqual((data["request_type"], data["urgent"], data["urgency_reason"]), ("ACH", False, ""))


class PrecheckParityTests(unittest.TestCase):
    """16: precheck and final submit apply the same urgency derivation."""

    def setUp(self):
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as sess:
            sess["role"] = "submitter"

    def _precheck(self, **overrides):
        form = pre._valid_precheck_form(**overrides)
        with patch.object(app_module, "current_app_user", return_value={"user_key": 11, "display_name": "U", "email": ""}), \
             patch.object(app_module.db, "get_bank_account_status", return_value="Open"), \
             patch.object(app_module.db, "get_app_user_role_codes", return_value=["sam", "controller"]), \
             patch.object(app_module.db, "resolve_approval_rule", return_value=ir._RULE):
            return self.client.post("/intake/precheck", data=form).get_json()["errors"]

    def test_16_precheck_accepts_same_day_ach_without_a_manual_urgency_reason(self):
        self.assertEqual(self._precheck(request_type=SDA, urgent="no", urgency_reason=""), [])
        self.assertEqual(self._precheck(request_type=SDA, urgent="yes", urgency_reason=""), [])

    def test_16_precheck_still_requires_a_reason_for_manual_urgency(self):
        errors = self._precheck(request_type="ACH", urgent="yes", urgency_reason="")
        self.assertIn(URGENCY_REQUIRED, errors)

    def test_16_precheck_and_final_validator_agree_on_urgency_cases(self):
        cases = [dict(request_type=SDA, urgent="no", urgency_reason=""),
                 dict(request_type=SDA, urgent="yes", urgency_reason=""),
                 dict(request_type="ACH", urgent="yes", urgency_reason=""),
                 dict(request_type="ACH", urgent="yes", urgency_reason="Vendor deadline"),
                 dict(request_type="Wire", urgent="yes", urgency_reason="", recv_bank_address="1 Main St")]
        for case in cases:
            with self.subTest(case=case):
                precheck_has = URGENCY_REQUIRED in self._precheck(**case)
                final_has = URGENCY_REQUIRED in _validate(pre._valid_precheck_form(**case))
                self.assertEqual(precheck_has, final_has)

    def test_16_precheck_applies_ordinary_ach_rules_to_same_day_ach(self):
        errors = self._precheck(request_type=SDA, recv_routing_number="")
        self.assertTrue(any("Receiving Routing Number" in e for e in errors))
        errors = self._precheck(request_type=SDA, has_wire_ach_instructions="false")
        self.assertTrue(any(WIRE_REQUIRED in e for e in errors))
        errors = self._precheck(request_type=SDA, has_payment_support="false")
        self.assertTrue(any("Payment Support" in e for e in errors))


class DraftLifecycleTests(ir._RouteBase):
    # 13
    def test_13_save_draft_preserves_same_day_ach_with_normalized_urgency(self):
        resp, m = self._post(_sda_form(form_mode="draft", urgent="no", urgency_reason="forged"))
        self.assertEqual(resp.status_code, 302)
        data = m["create_draft"].call_args[0][0]
        self.assertEqual((data["request_type"], data["urgent"], data["urgency_reason"]), (SDA, True, SDA))

    def test_13b_draft_update_is_normalized_too(self):
        resp, m = self._post(_sda_form(form_mode="draft", transaction_key="55", request_id="TXN-2026-D100"))
        self.assertEqual(m["update_draft"].call_args[0][0], 55)
        data = m["update_draft"].call_args[0][1]
        self.assertEqual((data["request_type"], data["urgent"], data["urgency_reason"]), (SDA, True, SDA))

    def test_13c_draft_validation_accepts_the_new_type(self):
        self.assertEqual(app_module._validate_draft_submission(td._draft_form(request_type=SDA)), [])
        self.assertIn("Unsupported Request Type.",
                      app_module._validate_draft_submission(td._draft_form(request_type="same day ach")))

    # 14
    def _resume(self, **overrides):
        case = td.ResumeFieldCompletenessTests("test_resume_page_prefills_urgency_reason_and_contact_fields")
        case.setUp()
        draft = case._resume_draft(**overrides)
        return case._render_resume(draft).get_data(as_text=True)

    def test_14_resume_restores_same_day_ach_and_its_urgency(self):
        body = self._resume(request_type=SDA, urgent=True, urgency_reason=SDA)
        self.assertRegex(body, r'<option value="Same Day ACH"\s+selected')
        idx = body.index('id="urgent_yes"')
        self.assertIn("checked", body[idx: body.index(">", idx)])
        self.assertIn(">Same Day ACH</textarea>", body)

    def test_14b_resumed_regular_ach_draft_is_not_urgent(self):
        body = self._resume(request_type="ACH", urgent=False, urgency_reason="")
        self.assertRegex(body, r'<option value="ACH"\s+selected')
        self.assertNotRegex(body, r'<option value="Same Day ACH"\s+selected')
        idx = body.index('id="urgent_yes"')
        self.assertNotIn("checked", body[idx: body.index(">", idx)])

    # 15 / scenarios B and C
    def _finalize(self, **overrides):
        resp, m = self._post(_sda_form(transaction_key="55", request_id="TXN-2026-D100", **overrides))
        self.assertEqual(resp.status_code, 302)
        m["finalize_draft_submission"].assert_called_once()
        args, kwargs = m["finalize_draft_submission"].call_args
        self.assertEqual(args[0], 55)
        return args[1]

    def test_15_scenario_b_normal_ach_draft_changed_to_same_day_ach_is_normalized_at_finalize(self):
        data = self._finalize(urgent="no", urgency_reason="")
        self.assertEqual((data["request_type"], data["urgent"], data["urgency_reason"]), (SDA, True, SDA))

    def test_15_scenario_c_same_day_ach_draft_changed_to_ach_releases_urgency_at_finalize(self):
        resp, m = self._post(ir._external_form(transaction_key="55", request_id="TXN-2026-D100",
                                               request_type="ACH", urgent="no", urgency_reason=""))
        data = m["finalize_draft_submission"].call_args[0][1]
        self.assertEqual((data["request_type"], data["urgent"], data["urgency_reason"]), ("ACH", False, ""))

    def test_15_a_draft_cannot_bypass_normalization_at_finalize(self):
        data = self._finalize(urgent="no", urgency_reason="Draft said not urgent")
        self.assertTrue(data["urgent"])
        self.assertEqual(data["urgency_reason"], SDA)


class PersistenceTests(unittest.TestCase):
    """17, 28: what reaches SQL (and so Power Automate) for a Same Day ACH."""

    def _insert(self, request_type):
        cur = ir._FakeCursor(bank_row=None)
        data = dict(ir._INSERT_DATA, request_type=request_type, recipient_bank_account_key=None,
                    recv_bank_name="Chase", recv_account_name="Acme", recv_account_number="12345678",
                    recv_routing_number="021000021", urgent=True, urgency_reason=SDA)
        with patch.object(db, "get_connection", return_value=ir._conn(cur)), \
             patch.object(db, "resolve_approval_rule", return_value=ir._RULE), \
             patch.object(db, "resolve_entity_key", return_value=3):
            db.insert_transaction(data)
        return cur

    def test_17_28_request_type_urgent_flag_and_reason_are_written_to_the_transaction_row(self):
        cur = self._insert(SDA)
        sql, params = cur.statements("INSERT INTO [etransactions].[ETransaction]")[0]
        columns = [c.strip() for c in sql[sql.index("(") + 1: sql.index(") OUTPUT")].split(",")]
        self.assertEqual(params[columns.index("Request_Type")], SDA)
        self.assertEqual(params[columns.index("Urgent_Flag")], 1)
        self.assertEqual(params[columns.index("Urgency_Reason")], SDA)
        self.assertLessEqual(len(SDA), 30)  # ETransaction.Request_Type is varchar(30)

    def test_28_finalize_writes_the_same_urgent_columns(self):
        from unittest.mock import MagicMock
        fake_cursor = MagicMock()
        fake_cursor.fetchone.side_effect = [(3,), (10, 20), (77,)]
        fake_cursor.rowcount = 1
        conn = MagicMock()
        conn.cursor.return_value = fake_cursor
        case = td.DraftFinalizeDbTests("test_32_status_stage_owner_transition")
        with patch.object(db, "get_connection", return_value=conn):
            db.finalize_draft_submission(
                99, case._finalize_data(request_type=SDA, urgent=True, urgency_reason=SDA), prepared_by_user_key=11)
        sql, params = next(c[0] for c in fake_cursor.execute.call_args_list
                           if "UPDATE [etransactions].[ETransaction]" in c[0][0])
        self.assertIn("Urgent_Flag = ?, Urgency_Reason = ?", sql)
        self.assertIn(SDA, params)
        self.assertIn(1, params)

    def test_27_workflow_event_statements_are_identical_to_ach(self):
        ach, sda = self._insert("ACH"), self._insert(SDA)
        self.assertEqual([s for s, _ in ach.executed], [s for s, _ in sda.executed])

        def event_types(cur):
            return [p[3] for s, p in cur.statements("INSERT INTO [etransactions].[WorkflowEvent]")]
        self.assertEqual(event_types(ach), event_types(sda))
        self.assertEqual(event_types(sda), [wf.EVENT_REQUEST_SUBMITTED, wf.EVENT_APPROVER_ASSIGNED])


class DisplayTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def _record(self, **overrides):
        case = pu.ApproverViewPriorUseBannerRenderTests("test_new_instructions_shows_no_prior_use_banner")
        return case, case._fake_record(**overrides)

    # 19
    def test_19_detail_page_shows_same_day_ach_and_the_urgent_indicators(self):
        case, record = self._record(request_type=SDA, urgent=True, urgency_reason=SDA)
        body = case._get(record).get_data(as_text=True)
        self.assertIn(f'{SDA}</span>', body)
        self.assertIn("URGENT", body)
        self.assertIn("Urgency Reason:", body)
        self.assertIn(SDA, body[body.index("Urgency Reason:"):][:200])

    def test_29_detail_page_still_shows_plain_ach_as_ach_and_not_urgent(self):
        case, record = self._record(request_type="ACH")
        body = case._get(record).get_data(as_text=True)
        self.assertNotIn(SDA, body)
        self.assertNotIn("Urgency Reason:", body)

    # 18
    def test_18_confirmation_page_shows_same_day_ach_and_urgency(self):
        with self.client.session_transaction() as sess:
            sess["role"] = "submitter"
        _, record = self._record(request_type=SDA, urgent=True, urgency_reason=SDA)
        with patch.object(app_module, "current_app_user_key", return_value=11), \
             patch.object(app_module.db, "get_transaction_for_workflow",
                          return_value={"status": wf.STATUS_PENDING_APPROVER, "accounting_group_key": None}), \
             patch.object(app_module.authorization, "can_view_transaction", return_value=True), \
             patch.object(app_module, "_authorized_group_keys_by_role", return_value={}), \
             patch.object(app_module.db, "get_request_detail", return_value=record):
            body = self.client.get("/confirmation/TXN-2026-999").get_data(as_text=True)
        self.assertIn(SDA, body)
        self.assertIn("URGENT:", body)

    # 20
    def _dashboard_records(self):
        base = {"status": wf.STATUS_PENDING_APPROVER, "property_dept": "Prop A", "assigned_approver": "Alice",
                "amount": 500.0, "submitted_date": "2026-01-01", "days_pending": 1}
        return [dict(base, request_id="TXN-SDA", request_type=SDA, urgent=True, urgency_reason=SDA),
                dict(base, request_id="TXN-ACH", request_type="ACH", urgent=False, urgency_reason="")]

    def test_20_dashboard_distinguishes_same_day_ach_from_ach_and_keeps_urgent_indicators(self):
        with self.client.session_transaction() as sess:
            sess["role"] = "sam"
        with patch.object(app_module, "current_app_user_key", return_value=200), \
             patch.object(app_module.db, "get_dashboard_records", return_value=self._dashboard_records()):
            resp = self.client.get("/dashboard")
            filtered = self.client.get("/dashboard?request_type=Same+Day+ACH")
        body = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(f'<option value="{SDA}"', body)
        self.assertIn('<option value="ACH"', body)
        self.assertIn("TXN-SDA", body)
        self.assertIn("row-urgent", body)
        only = filtered.get_data(as_text=True)
        self.assertIn("TXN-SDA", only)
        self.assertNotIn("TXN-ACH", only)

    def test_20_urgent_only_filter_and_export_keep_request_type_and_urgency(self):
        filters = {"status": "", "request_type": "", "property": "", "approver": "", "urgent_only": "1",
                   "over_1m_only": "", "amount_min": "", "amount_max": ""}
        kept = app_module._apply_dashboard_filters(self._dashboard_records(), filters)
        self.assertEqual([r["request_id"] for r in kept], ["TXN-SDA"])

        records = [dict(r, treasury_service_date="2026-10-01") for r in self._dashboard_records()]
        text = export.build_dashboard_export_csv(records).decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(text)))
        keys = [k for _, k in export.EXPORT_COLUMNS]
        by_id = {r[keys.index("request_id")]: r for r in rows[1:]}
        self.assertEqual(by_id["TXN-SDA"][keys.index("request_type")], SDA)
        self.assertEqual(by_id["TXN-SDA"][keys.index("urgent")], "Yes")
        self.assertEqual(by_id["TXN-ACH"][keys.index("request_type")], "ACH")
        self.assertEqual(by_id["TXN-ACH"][keys.index("urgent")], "No")


class NoTimeOfDayRuleTests(unittest.TestCase):
    def test_submission_is_not_time_dependent(self):
        # A Same Day ACH validates identically whatever the clock says: no cutoff is enforced.
        import datetime as real_datetime
        for hour in (6, 14, 23):
            with self.subTest(hour=hour), patch.object(app_module, "datetime") as fake:
                fake.now.return_value = real_datetime.datetime(2026, 10, 1, hour, 59)
                fake.strptime.side_effect = real_datetime.datetime.strptime
                self.assertEqual(_validate(_sda_form()), [])


class AllowedRequestTypeTests(ir._RouteBase):
    """Final submission accepts only the canonical request types, before any side effect."""

    SIDE_EFFECTS = ("reserve_request_id", "insert_transaction", "finalize_draft_submission",
                    "_upload_required_intake_attachments", "_upload_intake_attachments")
    INVALID_MESSAGE = "Please select a valid Request Type."

    def _assert_nothing_happened(self, resp, m, message):
        self.assertEqual(resp.status_code, 200)
        self.assertIn(message, resp.get_data(as_text=True))
        for name in self.SIDE_EFFECTS:
            m[name].assert_not_called()

    def test_one_shared_list_drives_final_submit_drafts_and_the_dropdown(self):
        self.assertEqual(app_module.REQUEST_TYPES,
                         ("Wire", "ACH", "Same Day ACH", "ACH Pull", "Intra Bank Transfer", "EFT"))
        self.assertIs(app_module._DRAFT_ALLOWED_REQUEST_TYPES, app_module.REQUEST_TYPES)
        with patch.object(app_module, "current_app_user", return_value={"user_key": 11, "display_name": "U", "email": ""}), \
             patch.object(app_module.db, "get_user_list", return_value=[]), \
             patch.object(app_module.db, "get_bank_accounts", return_value=[]):
            body = self.client.get("/intake").get_data(as_text=True)
        values = re.findall(r'<option value="([^"]*)"', body[body.index('id="request_type"'):])
        self.assertEqual([v for v in values if v][:len(app_module.REQUEST_TYPES)], list(app_module.REQUEST_TYPES))

    # 1-8
    def test_01_to_08_every_canonical_type_is_accepted(self):
        for rtype in app_module.REQUEST_TYPES:
            with self.subTest(rtype=rtype):
                extra = {"recv_bank_address": "1 Main St"} if rtype == "Wire" else {}
                resp, m = self._post(ir._external_form(request_type=rtype, **extra))
                self.assertEqual(resp.status_code, 302)
                data = m["insert_transaction"].call_args[0][0]
                self.assertEqual(data["request_type"], rtype)

    def test_02_03_same_day_ach_still_forces_urgency_and_the_reason(self):
        resp, m = self._post(_sda_form(urgent="no", urgency_reason="Vendor deadline"))
        data = m["insert_transaction"].call_args[0][0]
        self.assertEqual((data["request_type"], data["urgent"], data["urgency_reason"]), (SDA, True, SDA))

    def test_04_ach_keeps_the_existing_manual_urgency_rules(self):
        resp, m = self._post(ir._external_form(urgent="yes", urgency_reason="Vendor deadline"))
        data = m["insert_transaction"].call_args[0][0]
        self.assertEqual((data["urgent"], data["urgency_reason"]), (True, "Vendor deadline"))
        resp, m = self._post(ir._external_form(urgent="yes", urgency_reason=""))
        self._assert_nothing_happened(resp, m, "Urgency Reason is required when the request is marked urgent.")

    # 9, 10, 15
    def test_09_lowercase_same_day_ach_is_rejected_and_never_stored_as_a_plain_transaction(self):
        resp, m = self._post(ir._external_form(request_type="same day ach", urgent="no"))
        self._assert_nothing_happened(resp, m, self.INVALID_MESSAGE)

    def test_10_15_unsupported_values_are_rejected_before_any_side_effect(self):
        for bad in ("Bitcoin", "ach", "WIRE", "Same Day ACH ", " ACH", "Same  Day ACH", "SameDayACH", "ACH;DROP"):
            with self.subTest(bad=bad):
                resp, m = self._post(ir._external_form(request_type=bad))
                self._assert_nothing_happened(resp, m, self.INVALID_MESSAGE)
                m["resolve_approval_rule"].assert_not_called()

    # 11
    def test_11_blank_request_type_is_rejected_at_final_submission(self):
        for blank in ("", "   "):
            with self.subTest(blank=repr(blank)):
                resp, m = self._post(ir._external_form(request_type=blank))
                self._assert_nothing_happened(resp, m, "Request Type is required.")

    # 12
    def test_12_incomplete_drafts_can_still_be_saved(self):
        resp, m = self._post(td._draft_form(form_mode="draft", amount="5", request_type="ACH"), files={})
        self.assertEqual(resp.status_code, 302)
        m["create_draft"].assert_called_once()
        resp, m = self._post(td._draft_form(form_mode="draft", request_type="same day ach"), files={})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Unsupported Request Type.", resp.get_data(as_text=True))
        m["create_draft"].assert_not_called()

    # 13
    def test_13_draft_finalization_rejects_an_unsupported_or_stale_type(self):
        for bad in ("same day ach", "Legacy Transfer", ""):
            with self.subTest(bad=bad):
                resp, m = self._post(ir._external_form(transaction_key="55", request_id="TXN-2026-D100",
                                                       request_type=bad))
                self.assertEqual(resp.status_code, 200)
                for name in self.SIDE_EFFECTS:
                    m[name].assert_not_called()

    # 14
    def test_14_precheck_and_final_validation_agree(self):
        case = PrecheckParityTests("test_16_precheck_accepts_same_day_ach_without_a_manual_urgency_reason")
        case.setUp()
        for rtype in list(app_module.REQUEST_TYPES) + ["same day ach", "Bitcoin", " ACH", ""]:
            with self.subTest(rtype=rtype):
                extra = {"recv_bank_address": "1 Main St"} if rtype == "Wire" else {}
                form = pre._valid_precheck_form(request_type=rtype, **extra)
                precheck = case._precheck(request_type=rtype, **extra)
                final = _validate(form)
                self.assertEqual(self.INVALID_MESSAGE in precheck, self.INVALID_MESSAGE in final)
                self.assertEqual(precheck == [], final == [])
        self.assertIn(self.INVALID_MESSAGE, case._precheck(request_type="same day ach"))

    # 16
    def test_16_historical_transactions_with_any_stored_type_stay_readable(self):
        detail_case = pu.ApproverViewPriorUseBannerRenderTests("test_new_instructions_shows_no_prior_use_banner")
        for stored in ("Wire Transfer (legacy)", "same day ach", "ACH"):
            with self.subTest(stored=stored):
                body = detail_case._get(detail_case._fake_record(request_type=stored)).get_data(as_text=True)
                self.assertIn(stored, body)
        records = [{"request_id": "TXN-OLD", "status": wf.STATUS_PENDING_APPROVER, "request_type": "Legacy Type",
                    "property_dept": "P", "assigned_approver": "A", "urgent": False, "amount": 1.0,
                    "submitted_date": "2025-01-01", "days_pending": 1}]
        with self.client.session_transaction() as sess:
            sess["role"] = "sam"
        with patch.object(app_module, "current_app_user_key", return_value=200), \
             patch.object(app_module.db, "get_dashboard_records", return_value=records):
            resp = self.client.get("/dashboard")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Legacy Type", resp.get_data(as_text=True))

    # 17
    def test_17_internal_ach_exemption_remains_ach_only(self):
        for rtype in app_module.REQUEST_TYPES:
            with self.subTest(rtype=rtype):
                self.assertEqual(
                    app_module.external_instructions_exempt(
                        request_type=rtype, originating_key=5, originating_status="Open",
                        recipient_key=9, recipient_status="Open"),
                    rtype == "ACH")

    # 18
    def test_18_approval_rule_resolution_is_unchanged_for_valid_types(self):
        resp, m = self._post(ir._external_form(request_type=SDA))
        self.assertEqual(resp.status_code, 302)
        m["resolve_approval_rule"].assert_called_once_with(1000.0)


class GuardTests(unittest.TestCase):
    # 30
    def test_30_no_external_access_was_attempted(self):
        self.assertEqual(external_guard.state.blocked, [])


if __name__ == "__main__":
    unittest.main()
