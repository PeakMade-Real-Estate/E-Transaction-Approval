"""
Tests for Item 3: the External ACH/Wire Instructions attachment is NOT required for an
ACH between two DIFFERENT maintained, Open BankAccounts (internal-recipient mode). Every
other attachment and verification rule is unchanged, and the exemption is derived
server-side from database records, never from a browser flag.

Mock-only: `external_guard` (imported first) makes the database, SharePoint and network inert.

Run:  python -m unittest test_internal_ach_exemption -v
"""
import unittest
import external_guard  # noqa: F401  (must precede app/db/sharepoint imports)
from unittest.mock import patch

import app as app_module
import db
import workflow as wf
import test_internal_recipient as ir  # shared fixtures only; its classes are not re-collected here

NO_WIRE = {"file_wire_ach_instructions": True}
WIRE_REQUIRED = "External ACH/Wire Instructions attachment is required"


def _post_form(case, form, **kwargs):
    kwargs.setdefault("files", ir._files(**NO_WIRE))
    return case._post(form, **kwargs)


class HelperRuleTests(unittest.TestCase):
    def _rule(self, **overrides):
        args = dict(request_type="ACH", originating_key=5, originating_status="Open",
                    recipient_key=9, recipient_status="Open")
        args.update(overrides)
        return app_module.external_instructions_exempt(**args)

    def test_only_ach_between_two_different_open_accounts_is_exempt(self):
        self.assertTrue(self._rule())
        self.assertTrue(self._rule(originating_key="5", recipient_key="9"))

    def test_everything_else_is_not_exempt(self):
        cases = {
            "wire": dict(request_type="Wire"), "ach pull": dict(request_type="ACH Pull"),
            "eft": dict(request_type="EFT"), "intra bank": dict(request_type="Intra Bank Transfer"),
            "no type": dict(request_type=None),
            "same key": dict(recipient_key=5), "no recipient": dict(recipient_key=None),
            "blank recipient": dict(recipient_key=""), "bad recipient": dict(recipient_key="abc"),
            "no originating": dict(originating_key=None),
            "recipient closed": dict(recipient_status="Closed"),
            "recipient missing": dict(recipient_status=None),
            "originating closed": dict(originating_status="Closed"),
        }
        for name, overrides in cases.items():
            with self.subTest(name):
                self.assertFalse(self._rule(**overrides))

    def test_display_mode_ignores_later_account_closure_but_never_same_key_or_type(self):
        self.assertTrue(self._rule(recipient_status="Closed", require_open=False))
        self.assertFalse(self._rule(request_type="Wire", require_open=False))
        self.assertFalse(self._rule(recipient_key=5, require_open=False))


class _Base(ir._RouteBase):
    def _assert_rejected(self, resp, mocks, fragment):
        self.assertEqual(resp.status_code, 200)
        mocks["insert_transaction"].assert_not_called()
        mocks["finalize_draft_submission"].assert_not_called()
        self.assertIn(fragment, resp.get_data(as_text=True))


class ExemptionRouteTests(_Base):
    # 1
    def test_01_ach_open_internal_to_open_internal_needs_no_instructions(self):
        resp, m = _post_form(self, ir._form(), summary=ir._summary())
        self.assertEqual(resp.status_code, 302)
        data = m["insert_transaction"].call_args[0][0]
        self.assertEqual(data["recipient_bank_account_key"], 9)
        self.assertEqual(data["status"], wf.STATUS_PENDING_APPROVER)

    def test_01b_exempt_transfer_still_accepts_a_voluntarily_attached_file(self):
        resp, _ = self._post(ir._form(), files=ir._files(), summary=ir._summary())
        self.assertEqual(resp.status_code, 302)

    # 2
    def test_02_ach_external_recipient_still_requires_instructions(self):
        resp, m = _post_form(self, ir._external_form())
        self._assert_rejected(resp, m, WIRE_REQUIRED)

    def test_19_external_transactions_still_pass_unchanged(self):
        resp, m = self._post(ir._external_form())
        self.assertEqual(resp.status_code, 302)
        self.assertIsNone(m["insert_transaction"].call_args[0][0]["recipient_bank_account_key"])

    # 3
    def test_03_missing_recipient_key_requires_instructions_and_is_rejected(self):
        resp, m = _post_form(self, ir._form(recipient_bank_account_key=""), summary=None)
        self._assert_rejected(resp, m, WIRE_REQUIRED)
        resp, m = _post_form(self, ir._form(recipient_mode="", recipient_bank_account_key=""))
        self._assert_rejected(resp, m, WIRE_REQUIRED)

    # 4
    def test_04_forged_flags_never_grant_the_exemption(self):
        forged = {"external_instructions_not_required": "yes", "internal_transfer": "true",
                  "recipient_internal": "yes", "instructions_exempt": "1", "has_wire_ach_instructions": "true"}
        resp, m = _post_form(self, ir._external_form(**forged))
        self._assert_rejected(resp, m, WIRE_REQUIRED)
        # An "internal" claim that the database cannot back up is rejected, not exempted.
        resp, m = _post_form(self, ir._form(), summary=None)
        self._assert_rejected(resp, m, WIRE_REQUIRED)

    # 5
    def test_05_closed_recipient_gets_no_exemption(self):
        resp, m = _post_form(self, ir._form(), summary=ir._summary(status="Closed"))
        self._assert_rejected(resp, m, WIRE_REQUIRED)
        self.assertIn("closed and cannot be used", resp.get_data(as_text=True))

    def test_05b_closed_originating_gets_no_exemption(self):
        closed = patch.object(app_module.db, "get_bank_account_status", return_value="Closed")
        resp, m = _post_form(self, ir._form(), summary=ir._summary(), extra=(closed,))
        self._assert_rejected(resp, m, WIRE_REQUIRED)

    # 6
    def test_06_invalid_recipient_key_gets_no_exemption(self):
        for key in ("999", "abc", "0"):
            with self.subTest(key=key):
                resp, m = _post_form(self, ir._form(recipient_bank_account_key=key), summary=None)
                self._assert_rejected(resp, m, WIRE_REQUIRED)

    # 7
    def test_07_same_originating_and_recipient_is_rejected(self):
        resp, m = _post_form(self, ir._form(recipient_bank_account_key="5"), summary=ir._summary(key=5))
        self._assert_rejected(resp, m, "cannot be the same as the originating")
        self.assertIn(WIRE_REQUIRED, resp.get_data(as_text=True))

    # 8, 9, 10
    def test_08_wire_unchanged(self):
        resp, m = _post_form(self, ir._form(request_type="Wire"), summary=ir._summary())
        self._assert_rejected(resp, m, "only be the recipient of an ACH or Intra Bank Transfer")
        resp, m = _post_form(self, ir._external_form(request_type="Wire", recv_bank_address="1 Main St"))
        self._assert_rejected(resp, m, WIRE_REQUIRED)

    def test_09_ach_pull_unchanged(self):
        resp, m = _post_form(self, ir._form(request_type="ACH Pull"), summary=ir._summary())
        self._assert_rejected(resp, m, WIRE_REQUIRED)
        resp, m = _post_form(self, ir._external_form(request_type="ACH Pull"))
        self._assert_rejected(resp, m, WIRE_REQUIRED)

    def test_09b_eft_unchanged(self):
        resp, m = _post_form(self, ir._external_form(request_type="EFT"))
        self._assert_rejected(resp, m, WIRE_REQUIRED)

    def test_10_intra_bank_transfer_is_not_exempt_even_between_internal_accounts(self):
        resp, m = _post_form(self, ir._form(request_type="Intra Bank Transfer"), summary=ir._summary())
        self._assert_rejected(resp, m, WIRE_REQUIRED)
        resp, m = self._post(ir._form(request_type="Intra Bank Transfer"), summary=ir._summary())
        self.assertEqual(resp.status_code, 302)


class DraftExemptionTests(_Base):
    def _final(self, **overrides):
        return ir._form(transaction_key="55", request_id="TXN-2026-D100", **overrides)

    # 11
    def test_11_draft_finalize_with_valid_internal_ach_needs_no_new_attachment(self):
        resp, m = _post_form(self, self._final(), summary=ir._summary())
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(m["finalize_draft_submission"].call_args[0][1]["recipient_bank_account_key"], 9)

    def test_11b_internal_ach_draft_saves_normally_without_the_attachment(self):
        resp, m = _post_form(self, ir._form(form_mode="draft"), summary=ir._summary())
        self.assertEqual(resp.status_code, 302)
        m["create_draft"].assert_called_once()

    def test_11c_recipient_closed_since_the_draft_was_saved_no_longer_qualifies(self):
        resp, m = _post_form(self, self._final(), summary=ir._summary(status="Closed"))
        self._assert_rejected(resp, m, WIRE_REQUIRED)

    # 12
    def test_12_draft_switched_to_external_requires_instructions_again(self):
        form = ir._external_form(transaction_key="55", request_id="TXN-2026-D100")
        resp, m = _post_form(self, form)
        self._assert_rejected(resp, m, WIRE_REQUIRED)

    def test_12b_previously_uploaded_instructions_still_satisfy_an_external_draft(self):
        form = ir._external_form(transaction_key="55", request_id="TXN-2026-D100")
        existing = patch.object(app_module, "_resolve_files_present",
                                return_value={"validation_evidence": True, "wire_ach_instructions": True,
                                              "payment_support": True})
        resp, m = _post_form(self, form, extra=(existing,))
        self.assertEqual(resp.status_code, 302)
        m["finalize_draft_submission"].assert_called_once()


class PrecheckAndFinalAgreementTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as sess:
            sess["role"] = "submitter"

    def _precheck(self, form, summary, has_wire=False, bank_status="Open"):
        data = dict(form)
        data.update({"has_validation_evidence": "true", "has_payment_support": "true",
                     "has_wire_ach_instructions": "true" if has_wire else "false"})
        with patch.object(app_module, "current_app_user", return_value={"user_key": 11, "display_name": "U", "email": ""}), \
             patch.object(app_module.db, "get_bank_account_status", return_value=bank_status), \
             patch.object(app_module.db, "get_app_user_role_codes", return_value=["sam", "controller"]), \
             patch.object(app_module.db, "get_internal_recipient_summary", return_value=summary), \
             patch.object(app_module.db, "get_internal_recipient_instruction_values",
                          return_value={"bank_name": "WF", "account_name": "A", "account_number": ir.REAL_ACCT,
                                        "routing_number": ir.REAL_RTN, "address": ""}), \
             patch.object(app_module.db, "find_prior_completed_beneficiary_match", return_value=None), \
             patch.object(app_module.db, "resolve_approval_rule", return_value=ir._RULE):
            return self.client.post("/intake/precheck", data=data).get_json()["errors"]

    def _final_validator(self, form, summary, has_wire=False, bank_status="Open"):
        return app_module._validate_intake_submission(
            form, files_present={"validation_evidence": True, "wire_ach_instructions": has_wire,
                                 "payment_support": True},
            bank_account_status=bank_status, prepared_by_user_key=11, recipient_summary=summary)

    # 13
    def test_13_precheck_and_final_validation_agree_on_the_instructions_requirement(self):
        cases = {
            "internal ach": (ir._form(), ir._summary()),
            "internal ach closed": (ir._form(), ir._summary(status="Closed")),
            "internal ach unknown": (ir._form(), None),
            "internal intra bank": (ir._form(request_type="Intra Bank Transfer"), ir._summary()),
            "external ach": (ir._external_form(), None),
            "external wire": (ir._external_form(request_type="Wire", recv_bank_address="1 Main St"), None),
            "same key": (ir._form(recipient_bank_account_key="5"), ir._summary(key=5)),
        }
        for name, (form, summary) in cases.items():
            for bank_status in ("Open", "Closed"):
                with self.subTest(case=name, originating=bank_status):
                    pre = self._precheck(form, summary, bank_status=bank_status)
                    fin = self._final_validator(form, summary, bank_status=bank_status)
                    self.assertEqual(any(WIRE_REQUIRED in e for e in pre), any(WIRE_REQUIRED in e for e in fin))
                    self.assertEqual(pre, fin)

    def test_13b_precheck_accepts_exempt_internal_ach_and_rejects_external_ach(self):
        self.assertEqual(self._precheck(ir._form(), ir._summary()), [])
        self.assertTrue(any(WIRE_REQUIRED in e for e in self._precheck(ir._external_form(), None)))

    def test_13c_precheck_is_read_only(self):
        with patch.object(app_module.db, "insert_transaction") as ins, \
             patch.object(app_module.db, "finalize_draft_submission") as fin, \
             patch.object(app_module.db, "create_draft") as cd:
            self._precheck(ir._form(), ir._summary())
        ins.assert_not_called()
        fin.assert_not_called()
        cd.assert_not_called()


class FinalPostRevalidationTests(ir._RouteBase):
    # 14
    def test_14_final_post_rederives_eligibility_from_the_database_not_the_precheck(self):
        # A precheck may have said "fine"; the final POST asks the database again and the
        # recipient is now Closed, so the transaction must not be created.
        resp, m = _post_form(self, ir._form(), summary=ir._summary(status="Closed"))
        self.assertEqual(resp.status_code, 200)
        m["get_internal_recipient_summary"].assert_called()
        m["insert_transaction"].assert_not_called()

    def test_14b_final_post_ignores_client_asserted_attachment_flags(self):
        resp, m = _post_form(self, ir._external_form(has_wire_ach_instructions="true"))
        self.assertEqual(resp.status_code, 200)
        m["insert_transaction"].assert_not_called()

    def test_14c_final_post_rechecks_originating_account_status(self):
        closed = patch.object(app_module.db, "get_bank_account_status", return_value="Closed")
        resp, m = _post_form(self, ir._form(), summary=ir._summary(), extra=(closed,))
        self.assertEqual(resp.status_code, 200)
        m["insert_transaction"].assert_not_called()


class DetailDisplayTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as sess:
            sess["role"] = "sam"

    def _render(self, **overrides):
        overrides.setdefault("request_type", "ACH")
        record = ir.DetailPageTests._record(None, **overrides)
        with patch.object(app_module.db, "get_transaction_for_workflow",
                          return_value={"status": "Pending Approver", "accounting_group_key": None}), \
             patch.object(app_module.db, "get_request_detail", return_value=record), \
             patch.object(app_module, "sharepoint_enabled", return_value=False):
            body = self.client.get("/dashboard/request/TXN-2026-7253").get_data(as_text=True)
        start = body.index('section-label">External ACH/Wire Instructions')
        self.instructions_block = body[start: start + 1200]  # only this card, not other attachments' badges
        return body

    _internal = dict(recipient_mode="internal", recipient_bank_account_key=9, originating_bank_account_key=5,
                     originating_bank_account_status="Open", recipient_bank_account_status="Open")

    # 15
    def test_15_qualifying_internal_ach_shows_not_required_instead_of_missing(self):
        body = self._render(**self._internal)
        self.assertIn("Not required &mdash; internal account transfer.", body)
        self.assertNotIn("Missing", self.instructions_block)

    def test_15b_submitted_transaction_still_shows_not_required_after_account_is_closed(self):
        body = self._render(**dict(self._internal, recipient_bank_account_status="Closed"))
        self.assertIn("Not required &mdash; internal account transfer.", body)

    def test_15c_an_attached_file_is_still_shown_normally(self):
        body = self._render(**dict(self._internal, attachments={"wire_ach_instructions": "instr.pdf"}))
        self.assertIn("instr.pdf", body)
        self.assertNotIn("internal account transfer.", body)

    def test_15d_non_qualifying_transactions_keep_the_existing_missing_badge(self):
        cases = {
            "external ach": dict(),
            "internal intra bank": dict(self._internal, request_type="Intra Bank Transfer"),
            "internal wire": dict(self._internal, request_type="Wire"),
            "same key": dict(self._internal, recipient_bank_account_key=5),
        }
        for name, overrides in cases.items():
            with self.subTest(name):
                body = self._render(**overrides)
                self.assertIn("Missing", self.instructions_block)
                self.assertNotIn("internal account transfer.", body)

    def test_15e_draft_view_requires_the_recipient_to_be_open_now(self):
        body = self._render(**dict(self._internal, status="Draft", recipient_bank_account_status="Closed"))
        self.assertIn("Missing", self.instructions_block)

    def test_15f_avs_and_payment_support_display_unchanged(self):
        body = self._render(**dict(self._internal, attachments={"validation_evidence": "avs.png"}))
        self.assertIn("avs.png", body)
        self.assertIn("Payment Support", body)


class VerificationControlsUnchangedTests(ir._RouteBase):
    # 16
    def test_16_avs_and_verbal_confirmation_still_enforced_when_exempt(self):
        form = ir._form()
        for key in ("verbal_confirmed_with_known", "verbal_contact_name", "verbal_confirm_datetime"):
            form.pop(key)
        resp, m = _post_form(self, form, summary=ir._summary())
        self.assertEqual(resp.status_code, 200)
        m["insert_transaction"].assert_not_called()
        self.assertIn("Verbal confirmation with a known contact is required", resp.get_data(as_text=True))
        resp, m = _post_form(self, ir._form(avs_score="150"), summary=ir._summary())
        self.assertEqual(resp.status_code, 200)
        m["insert_transaction"].assert_not_called()

    # 17
    def test_17_prior_use_matching_and_acknowledgment_still_enforced_when_exempt(self):
        match = {"transaction_key": 42, "request_id": "TXN-2026-001", "last_used_date": "2026-01-01"}
        resp, m = _post_form(self, ir._form(), summary=ir._summary(), prior_match=match)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("already used on a completed transaction", resp.get_data(as_text=True))
        form = ir._form(instructions_previously_used="yes", last_used_date="2026-01-01")
        resp, m = _post_form(self, form, summary=ir._summary(), prior_match=match)
        self.assertEqual(resp.status_code, 200)
        resp, m = _post_form(self, dict(form, prior_use_ack="TXN-2026-001"), summary=ir._summary(), prior_match=match)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(m["insert_transaction"].call_args[0][0]["prior_transaction_key"], 42)

    # 18
    def test_18_validation_evidence_and_payment_support_still_required_when_exempt(self):
        for omitted, fragment in (("file_payment_support", "Payment Support attachment is required"),
                                  ("file_validation_evidence", "Validation Evidence attachment is required")):
            with self.subTest(omitted=omitted):
                files = ir._files(file_wire_ach_instructions=True, **{omitted: True})
                resp, m = self._post(ir._form(), files=files, summary=ir._summary())
                self.assertEqual(resp.status_code, 200)
                m["insert_transaction"].assert_not_called()
                self.assertIn(fragment, resp.get_data(as_text=True))

    # 20
    def test_20_routing_inputs_and_unauthenticated_access_are_unchanged(self):
        resp, m = _post_form(self, ir._form(), summary=ir._summary())
        data = m["insert_transaction"].call_args[0][0]
        self.assertEqual((data["approver_key"], data["controller_key"]), (2, 3))
        m["resolve_approval_rule"].assert_called_once_with(1000.0)
        anonymous = app_module.app.test_client()
        with patch.object(app_module.db, "insert_transaction") as ins:
            resp = anonymous.post("/intake/submit", data=ir._form())
        self.assertIn(resp.status_code, (302, 400, 401, 403))
        ins.assert_not_called()

    def test_20b_exempt_insert_writes_the_same_records_and_events_as_before(self):
        def targets(data, bank_row):
            cur = ir._FakeCursor(bank_row=bank_row)
            with patch.object(db, "get_connection", return_value=ir._conn(cur)), \
                 patch.object(db, "resolve_approval_rule", return_value=ir._RULE), \
                 patch.object(db, "resolve_entity_key", return_value=3):
                db.insert_transaction(dict(data))
            return [(s.split("]")[1].strip(" .[") if "]" in s else s[:40], (p or [None] * 4)[3] if "WorkflowEvent" in s else None)
                    for s, p in cur.executed if s.lstrip().startswith(("INSERT INTO", "UPDATE"))]
        ext = dict(ir._INSERT_DATA, recipient_bank_account_key=None, recv_bank_name="Chase", recv_account_name="A",
                   recv_account_number="12345678", recv_routing_number="021000021")
        self.assertEqual(targets(ir._INSERT_DATA, ir._bank_row()), targets(ext, None))


class AchRoutingConsistencyTests(ir._RouteBase):
    """9B: an external ACH requires routing; an internal ACH must not turn a blank one into an accepted submission."""

    def test_b1_internal_ach_with_blank_master_routing_is_rejected_even_though_exempt(self):
        resp, m = _post_form(self, ir._form(), summary=ir._summary(routing_present=False))
        self.assertEqual(resp.status_code, 200)
        m["insert_transaction"].assert_not_called()
        self.assertIn("no routing number on file", resp.get_data(as_text=True))

    def test_b2_internal_intra_bank_transfer_gets_no_new_routing_requirement(self):
        resp, m = self._post(ir._form(request_type="Intra Bank Transfer"), summary=ir._summary(routing_present=False))
        self.assertEqual(resp.status_code, 302)

    def test_b3_draft_save_stays_light_but_finalize_enforces_routing_for_ach(self):
        resp, m = _post_form(self, ir._form(form_mode="draft"), summary=ir._summary(routing_present=False))
        self.assertEqual(resp.status_code, 302)
        resp, m = _post_form(self, ir._form(transaction_key="55", request_id="TXN-2026-D100"),
                             summary=ir._summary(routing_present=False))
        self.assertEqual(resp.status_code, 200)
        m["finalize_draft_submission"].assert_not_called()

    def test_b4_db_layer_enforces_routing_for_ach_at_insert_and_finalize_only(self):
        blank = ir._bank_row(routing=None)
        cur = ir._FakeCursor(bank_row=blank)
        conn = ir._conn(cur)
        with patch.object(db, "get_connection", return_value=conn), \
             patch.object(db, "resolve_approval_rule", return_value=ir._RULE), \
             patch.object(db, "resolve_entity_key", return_value=3):
            with self.assertRaises(db.InternalRecipientError):
                db.insert_transaction(dict(ir._INSERT_DATA))
        self.assertFalse(cur.statements("INSERT INTO"))
        conn.commit.assert_not_called()

        cur = ir._FakeCursor(bank_row=blank)
        conn = ir._conn(cur)
        data = dict(ir._INSERT_DATA, approval_rule_key=7, requires_vp=False, requires_cfo=False, approval_tier="T")
        with patch.object(db, "get_connection", return_value=conn), patch.object(db, "resolve_entity_key", return_value=3):
            with self.assertRaises(db.InternalRecipientError):
                db.finalize_draft_submission(55, data, prepared_by_user_key=11)
        conn.commit.assert_not_called()
        self.assertFalse(cur.statements("INSERT INTO [etransactions].[WorkflowEvent]"))

        cur = ir._FakeCursor(bank_row=blank)
        with patch.object(db, "get_connection", return_value=ir._conn(cur)), patch.object(db, "resolve_entity_key", return_value=3):
            db.update_draft(55, dict(ir._INSERT_DATA), prepared_by_user_key=11)  # drafts remain light
        self.assertTrue(cur.statements("UPDATE [etransactions].[BeneficiaryBankInstruction]"))

    def test_b5_summary_reports_whether_a_routing_number_is_present(self):
        from unittest.mock import MagicMock
        for routing, expected in ((ir.REAL_RTN, True), (None, False), ("", False)):
            cur = MagicMock()
            cur.fetchone.return_value = (9, "WF", "T", None, ir.REAL_ACCT, routing, "Open", "", "")
            conn = MagicMock()
            conn.cursor.return_value = cur
            with patch.object(db, "get_connection", return_value=conn):
                self.assertEqual(db.get_internal_recipient_summary(9)["routing_present"], expected)

    def test_b6_external_ach_still_requires_routing(self):
        resp, m = self._post(ir._external_form(recv_routing_number=""))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Receiving Routing Number is required", resp.get_data(as_text=True))


class IntakePageMarkupTests(ir._RouteBase):
    def test_intake_page_has_conditional_instructions_markup_with_exact_wording(self):
        with patch.object(app_module, "current_app_user", return_value={"user_key": 11, "display_name": "U", "email": ""}), \
             patch.object(app_module.db, "get_user_list", return_value=[]), \
             patch.object(app_module.db, "get_bank_accounts",
                          return_value=[{"bank_account_key": 9, "bank_name": "WF", "account_title": "T",
                                         "account_last4": "2233", "entity_name": "E", "classification": "Property"}]):
            body = self.client.get("/intake").get_data(as_text=True)
        self.assertIn("External ACH/Wire Instructions: Not required for transfers between maintained internal accounts.", body)
        self.assertIn('id="wire-instructions-required-star"', body)
        self.assertIn("function updateInstructionsRequirement", body)
        # The file input itself is never made `required` by markup.
        idx = body.index('id="file_wire_ach_instructions"')
        self.assertNotIn("required", body[idx: body.index(">", idx)])


if __name__ == "__main__":
    unittest.main()
