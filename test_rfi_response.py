"""
Item 8 - Request for Information (RFI) response and resubmission experience.

Covers the "Additional Information Requested" section, the single inline response form, upload-versus-submit
clarity, preserved text on failures, and that the existing workflow (requester_respond, WorkflowEvent literals,
routing back to the reviewer who asked, approval history) is untouched.

Mock-only: `external_guard` (imported first) makes SQL/SharePoint/network inert.

Run:  python -m unittest test_rfi_response -v
"""
import io
import re
import unittest
import external_guard  # noqa: F401  (must precede app/db/sharepoint imports)
from contextlib import ExitStack
from datetime import datetime
from unittest.mock import MagicMock, patch

import app as app_module
import db
import sharepoint
import test_prior_use_verification as pu
import workflow as wf

REQUESTER, APPROVER, CONTROLLER, VP, CFO, OTHER = 100, 200, 300, 400, 500, 999
RFI_AT = datetime(2026, 10, 19, 9, 30)
REASON = "Please attach the signed invoice.\nAlso confirm the amount."
RID = "TXN-2026-8001"


def _rfi(**over):
    rfi = {"reason": REASON, "requested_by": "Alice Approver", "requested_at": "2026-10-19 09:30 AM",
           "requested_dt": RFI_AT, "origin_status": wf.STATUS_PENDING_APPROVER}
    rfi.update(over)
    return rfi


def _txn(**over):
    base = {
        "transaction_key": 8001, "request_id": RID, "status": wf.STATUS_MORE_INFO,
        "prepared_by_user_key": REQUESTER, "selected_approver_user_key": APPROVER,
        "selected_controller_user_key": CONTROLLER, "vp_approver_user_key": VP, "cfo_approver_user_key": CFO,
        "current_owner_user_key": REQUESTER, "bank_releaser_user_key": None, "requires_vp": True, "requires_cfo": False,
        "requires_controller": True, "amount": 5000.0, "entity_classification": "Corporate", "accounting_group_key": None,
    }
    base.update(over)
    return base


def _record(**over):
    case = pu.ApproverViewPriorUseBannerRenderTests("test_new_instructions_shows_no_prior_use_banner")
    record = case._fake_record(
        request_id=RID, status=wf.STATUS_MORE_INFO, prepared_by="Rita Requester", approver="Alice Approver",
        controller="Carl Controller", vp_approver="Vera VP", cfo_approver="Cleo CFO", entity_classification="Corporate",
        rfi=_rfi(), extra_attachments=[], attachments={})
    record.update(over)
    return record


def _file(name="invoice.pdf"):
    return (io.BytesIO(b"fake bytes"), name)


class _Base(unittest.TestCase):
    """A requester/approver session against one More-Information-Requested transaction, all boundaries mocked."""

    def setUp(self):
        self.client = app_module.app.test_client()
        self.txn = _txn()
        self.record = _record()
        self.user_key = REQUESTER
        self.sp_on = False
        self.sp_items = []
        self.stack = ExitStack()
        self.addCleanup(lambda: self.stack.close())
        self.m = {}

    def reset(self):
        """Start over with fresh patches and a fresh client (several sessions inside one test)."""
        self.stack.close()
        self.stack = ExitStack()
        self.client = app_module.app.test_client()
        self.m = {}

    def start(self, role="submitter", user_key=REQUESTER, **kw):
        self.user_key = user_key
        with self.client.session_transaction() as sess:
            sess["role"] = role
        d = app_module.db

        def p(target, name, **opts):
            self.m[name] = self.stack.enter_context(patch.object(target, name, **opts))
            return self.m[name]

        p(app_module, "current_app_user_key", return_value=user_key)
        p(d, "get_transaction_for_workflow", side_effect=lambda rid: dict(self.txn))
        p(d, "get_request_detail", side_effect=lambda rid: dict(self.record))
        p(d, "get_last_rfi_origin_status", return_value=kw.get("origin", wf.STATUS_PENDING_APPROVER))
        p(d, "get_last_rfi_event_datetime", return_value=RFI_AT)
        p(d, "advance_transaction_workflow", **({"side_effect": kw["advance_error"]} if "advance_error" in kw else {}))
        p(d, "add_transaction_comment")
        p(d, "get_app_user_by_key", side_effect=lambda k: {"user_key": k, "display_name": kw.get("owner_name", "Alice Approver"), "email": ""})
        p(app_module.authorization, "can_view_transaction", return_value=True)
        p(app_module, "_authorized_group_keys_by_role", return_value={})
        p(app_module, "sharepoint_enabled", side_effect=lambda: self.sp_on)
        p(app_module.sharepoint, "list_attachments", side_effect=lambda rid: list(self.sp_items))
        p(app_module.sharepoint, "upload_attachment", **({"side_effect": kw["upload_error"]} if "upload_error" in kw else
                                                         {"return_value": {"correlation_id": "c-1"}}))
        p(app_module, "bulk_recipient_enabled", return_value=kw.get("bulk_gate", False))

    def page(self):
        resp = self.client.get(f"/dashboard/request/{RID}")
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def respond(self, comment="", files=None):
        data = {"action": wf.ACTION_REQUESTER_RESPOND, "comment": comment}
        if files:
            data["resubmit_file"] = files
        return self.client.post(f"/dashboard/request/{RID}/action", data=data, content_type="multipart/form-data")

    def flashes(self):
        with self.client.session_transaction() as sess:
            return [(c, m) for c, m in sess.get("_flashes", [])]


# ═══ The section the requester sees ═════════════════════════════════════════
class RfiSectionTests(_Base):
    def test_1_2_3_an_active_rfi_shows_what_was_asked_who_asked_and_when(self):
        self.start()
        body = self.page()
        self.assertIn("Additional Information Requested", body)
        self.assertIn("Please attach the signed invoice.", body)
        self.assertIn("Also confirm the amount.", body)
        self.assertRegex(body, r'id="rfi-requested-reason"[^>]*>Please attach')      # no stray indentation in the quoted reason
        details = body[body.index('id="rfi-details"'):][:900]
        self.assertIn("Alice Approver", details)
        self.assertIn("2026-10-19 09:30 AM", details)
        self.assertIn("Action required", body)

    def test_the_response_goes_to_the_stage_that_asked_and_the_page_says_so(self):
        self.start()
        self.record["rfi"] = _rfi(origin_status=wf.STATUS_PENDING_CONTROLLER)
        details = self.page().split('id="rfi-details"', 1)[1][:900]
        self.assertIn("Controller", details)
        self.assertIn("Carl Controller", details)
        self.assertIn(wf.STATUS_PENDING_CONTROLLER, details)

    def test_4_the_requester_gets_one_response_form_with_text_files_and_the_submit_action(self):
        self.start()
        body = self.page()
        form = body[body.index('id="rfi-response-form"'):]
        form = form[:form.index("</form>")]
        self.assertIn(f'action="/dashboard/request/{RID}/action"', body)
        self.assertIn('name="csrf_token"', form)
        self.assertRegex(form, r'name="action" value="requester_respond"')
        self.assertRegex(form, r'<textarea[^>]*name="comment"')
        self.assertRegex(form, r'<input type="file"[^>]*name="resubmit_file" multiple')
        self.assertIn("Submit Additional Information", form)
        self.assertIn('type="submit"', form)
        self.assertIn("does <strong>not</strong> send anything", form)       # attaching is not submitting
        self.assertLess(form.index("rfi-response-comment"), form.index("rfi-response-files"))
        self.assertLess(form.index("rfi-response-files"), form.index('id="rfi-response-submit"'))

    def test_9_the_button_is_a_real_submit_of_the_existing_action_not_a_decoration(self):
        self.start()
        self.assertEqual(self.respond("Corrected invoice attached.").status_code, 302)
        kwargs = self.m["advance_transaction_workflow"].call_args.kwargs
        self.assertEqual(kwargs["action"], "requester_respond")
        self.assertEqual(kwargs["from_status"], wf.STATUS_MORE_INFO)

    def test_5_unrelated_users_get_no_response_controls(self):
        for role, key in (("sam", APPROVER), ("controller", CONTROLLER), ("submitter", OTHER)):
            with self.subTest(role=role):
                self.reset()
                self.start(role=role, user_key=key)
                body = self.page()
                self.assertIn('id="additional-information-requested"', body)             # everyone who may view sees the request
                self.assertNotIn('id="rfi-response-form"', body)
                self.assertNotIn("Submit Additional Information", body)
                self.assertIn("waiting for Rita Requester", body)

    def test_5_a_user_who_is_not_the_requester_cannot_respond_by_posting(self):
        for role, key in (("sam", APPROVER), ("controller", CONTROLLER), ("submitter", OTHER)):
            with self.subTest(role=role):
                self.reset()
                self.start(role=role, user_key=key)
                self.respond("I will answer for the requester")
                self.m["advance_transaction_workflow"].assert_not_called()

    def test_a_changed_request_id_cannot_be_used_to_answer_someone_elses_transaction(self):
        self.start(role="submitter", user_key=OTHER)
        self.txn = _txn(prepared_by_user_key=REQUESTER)           # the Request_ID in the URL belongs to REQUESTER
        resp = self.respond("not mine")
        self.assertIn("original requester", resp.get_data(as_text=True))
        self.m["advance_transaction_workflow"].assert_not_called()
        self.m["add_transaction_comment"].assert_not_called()

    def test_17_once_the_response_is_in_the_controls_are_gone(self):
        self.start()
        self.txn = _txn(status=wf.STATUS_PENDING_APPROVER)
        self.record = _record(status=wf.STATUS_PENDING_APPROVER, rfi=None)
        body = self.page()
        self.assertNotIn('id="rfi-response-form"', body)
        self.assertNotIn('id="additional-information-requested"', body)
        self.assertNotIn("Submit Additional Information", body)
        self.assertNotIn("Respond to Information Request", body)

    def test_the_sidebar_points_to_the_section_instead_of_a_hidden_modal(self):
        self.start()
        body = self.page()
        self.assertIn('href="#additional-information-requested"', body)
        self.assertNotIn("resubmitModal", body)
        self.assertNotIn("Resubmit with Additional Info", body)

    def test_no_unrelated_approval_buttons_appear_for_the_requester_during_an_rfi(self):
        self.start()
        body = self.page()
        for forbidden in ('value="approve"', 'value="treasury_released"', 'value="mark_completed"', 'data-bs-target="#rfiModal"'):
            self.assertNotIn(forbidden, body)

    def test_the_approvers_own_request_more_information_modal_still_works_and_ids_do_not_collide(self):
        self.start(role="sam", user_key=APPROVER)
        self.txn = _txn(status=wf.STATUS_PENDING_APPROVER)
        self.record = _record(status=wf.STATUS_PENDING_APPROVER, rfi=None)
        body = self.page()
        self.assertIn('id="rfiModal"', body)
        self.assertEqual(body.count('id="rfi-reason"'), 1)           # the Request More Information textarea
        self.reset()
        self.txn, self.record = _txn(), _record()
        self.start(role="sam", user_key=APPROVER)                     # an approver viewing an active RFI
        ids = re.findall(r'\bid="([^"]+)"', self.page())
        self.assertEqual(ids.count("rfi-reason"), 0)
        self.assertEqual(ids.count("rfi-requested-reason"), 1)

    def test_stored_documents_since_the_request_are_listed_and_older_ones_are_not(self):
        self.sp_on = True
        self.sp_items = [
            {"filename": "Other - old.pdf", "original_filename": "old.pdf", "doc_type": "Other", "web_url": "u1",
             "uploaded_date": "2026-10-18T10:00:00Z"},
            {"filename": "Other - signed.pdf", "original_filename": "signed.pdf", "doc_type": "Other", "web_url": "u2",
             "uploaded_date": "2026-10-20T10:00:00Z"},
        ]
        self.start()
        body = self.page()
        stored = body[body.index('id="rfi-stored-files"'):][:700]
        self.assertIn("signed.pdf", stored)
        self.assertNotIn("old.pdf", stored)
        self.assertIn("has not been submitted yet", stored)

    def test_the_upload_card_warns_the_requester_that_uploading_is_not_responding(self):
        self.start()
        self.assertIn('id="upload-not-a-response-note"', self.page())
        self.reset()
        self.start(role="sam", user_key=APPROVER)
        self.assertNotIn('id="upload-not-a-response-note"', self.page())

    def test_the_inline_script_blocks_empty_responses_and_repeat_clicks(self):
        self.start()
        script = self.page().split("Additional Information Requested: inline response form", 1)[1].split("Cancel Transaction Modal", 1)[0]
        self.assertIn("hasText", script)
        self.assertIn("storedCount === 0", script)
        self.assertIn("if (submitBtn.disabled)", script)
        self.assertIn("setSubmitting(true)", script)
        self.assertIn("selected, not yet submitted", script)
        self.assertEqual(script.count("{"), script.count("}"))

    def test_21_same_day_ach_urgency_is_untouched_by_an_rfi(self):
        self.start()
        self.record = _record(request_type="Same Day ACH", urgent=True, urgency_reason="Same Day ACH")
        body = self.page()
        self.assertIn("Urgency Reason:", body)
        self.assertIn("Additional Information Requested", body)

    def test_22_item_6_bulk_records_render_with_the_gate_on_or_off(self):
        for gate in (False, True):
            with self.subTest(gate=gate):
                self.reset()
                self.start(bulk_gate=gate)
                self.record = _record(recipient_mode="bulk", bulk_recipient_rows=3, bulk_matched_rows=3,
                                      bulk_validated_at="2026-10-19 09:00 AM", bulk_listing_sha256="a" * 64)
                body = self.page()
                self.assertIn("Additional Information Requested", body)
                self.assertIn('id="rfi-response-form"', body)

    def test_23_item_7_entity_id_still_shows_on_the_detail_page(self):
        self.start()
        self.record = _record(property_code="LEGACY-42")
        self.assertIn("Entity ID: LEGACY-42", self.page())


# ═══ Submitting, feedback and failures ═════════════════════════════════════
class RespondFlowTests(_Base):
    def test_10_the_response_returns_to_the_stage_and_person_that_asked(self):
        cases = (
            (wf.STATUS_PENDING_APPROVER, wf.STATUS_PENDING_APPROVER, APPROVER, "Approver"),
            (wf.STATUS_PENDING_CONTROLLER, wf.STATUS_PENDING_CONTROLLER, CONTROLLER, "Controller"),
            (wf.STATUS_PENDING_VP, wf.STATUS_PENDING_VP, VP, "VP"),
            (wf.STATUS_PENDING_CFO, wf.STATUS_PENDING_CFO, CFO, "CFO"),
        )
        for origin, expected_status, owner, role in cases:
            with self.subTest(origin=origin):
                self.reset()
                self.start(origin=origin)
                self.assertEqual(self.respond("Here is the information.").status_code, 302)
                kwargs = self.m["advance_transaction_workflow"].call_args.kwargs
                self.assertEqual((kwargs["new_status"], kwargs["new_owner_user_key"], kwargs["workflow_role"]),
                                 (expected_status, owner, role))

    def test_11_12_the_same_transaction_is_updated_and_the_comment_joins_the_history(self):
        self.start()
        resp = self.respond("Signed invoice attached.")
        self.assertTrue(resp.headers["Location"].endswith(f"/dashboard/request/{RID}"))
        args, kwargs = self.m["advance_transaction_workflow"].call_args
        self.assertEqual(args[0], 8001)
        self.assertEqual(kwargs["comments"], "Signed invoice attached.")
        self.assertEqual(kwargs["actor_user_key"], REQUESTER)
        comment = self.m["add_transaction_comment"].call_args.kwargs
        self.assertEqual((comment["comment_type"], comment["comment_text"]), (wf.COMMENT_TYPE_RFI, "Signed invoice attached."))

    def test_15_success_confirms_the_response_the_new_status_and_the_reviewer(self):
        self.start(origin=wf.STATUS_PENDING_CONTROLLER, owner_name="Carl Controller")
        self.respond("Done.")
        (category, message), = self.flashes()
        self.assertEqual(category, "success")
        for expected in ("Response submitted", RID, "Controller", "Carl Controller", wf.STATUS_PENDING_CONTROLLER):
            self.assertIn(expected, message)
        self.assertNotIn("Resubmitted", message)

    def test_the_reviewer_name_in_the_confirmation_is_escaped(self):
        self.start(owner_name="<img src=x onerror=alert(1)>")
        self.respond("Done.")
        message = self.flashes()[0][1]
        self.assertNotIn("<img", message)
        self.assertIn("&lt;img", message)

    def test_a_comment_alone_a_file_alone_or_both_are_accepted(self):
        for comment, files in (("Only text", None), ("", [_file()]), ("Text and file", [_file()])):
            with self.subTest(comment=comment, files=bool(files)):
                self.reset()
                self.sp_on = True
                self.start()
                self.assertEqual(self.respond(comment, files).status_code, 302)
                self.m["advance_transaction_workflow"].assert_called_once()

    def test_several_files_are_uploaded_with_the_response_under_the_unchanged_classification(self):
        self.sp_on = True
        self.start()
        self.respond("Three files", [_file("a.pdf"), _file("b.pdf"), _file("c.pdf")])
        calls = self.m["upload_attachment"].call_args_list
        self.assertEqual([c.args[1].filename for c in calls], ["a.pdf", "b.pdf", "c.pdf"])
        for call in calls:
            self.assertEqual((call.kwargs["section"], call.kwargs["doc_type"]),
                             (sharepoint.SECTION_ADDITIONAL, sharepoint.DOC_TYPE_OTHER))
        self.m["advance_transaction_workflow"].assert_called_once()

    def test_7_a_document_already_stored_since_the_request_counts_as_the_response(self):
        self.sp_on = True
        self.sp_items = [{"filename": "Other - signed.pdf", "original_filename": "signed.pdf", "doc_type": "Other",
                          "uploaded_date": "2026-10-20T10:00:00Z", "web_url": "u"}]
        self.start()
        self.assertEqual(self.respond("").status_code, 302)
        self.m["advance_transaction_workflow"].assert_called_once()

    def test_an_empty_response_is_refused_keeps_the_transaction_waiting_and_says_so(self):
        self.sp_on = True
        self.sp_items = [{"filename": "Other - old.pdf", "original_filename": "old.pdf", "doc_type": "Other",
                          "uploaded_date": "2026-10-18T10:00:00Z", "web_url": "u"}]      # before the request
        self.start()
        resp = self.respond("   ")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("Your response was not submitted", body)
        self.assertIn("still waiting for your information", body)
        self.assertNotIn("Response submitted", body)
        self.assertIn('id="rfi-response-form"', body)
        self.m["advance_transaction_workflow"].assert_not_called()

    def test_6_16_a_failed_upload_keeps_the_text_shows_an_error_and_advances_nothing(self):
        self.sp_on = True
        self.start(upload_error=RuntimeError("Graph error"))
        resp = self.respond("My careful explanation", [_file("big.pdf")])
        body = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200)
        self.assertRegex(body, r"<textarea[^>]*id=\"rfi-response-comment\"[^>]*>\s*My careful explanation\s*</textarea>")
        self.assertIn("Your response was NOT submitted", body)
        self.assertIn("big.pdf", body)
        self.assertNotIn("Response submitted", body)
        self.assertIn('alert-danger', body)
        self.m["advance_transaction_workflow"].assert_not_called()
        self.m["add_transaction_comment"].assert_not_called()

    def test_16_a_failed_workflow_update_keeps_the_text_and_implies_no_progress(self):
        self.start(advance_error=RuntimeError("SQL down"))
        resp = self.respond("Keep this text")
        body = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Keep this text", body)
        self.assertIn("Your response was NOT submitted", body)
        self.assertNotIn("Response submitted", body)
        self.assertIn('id="rfi-response-form"', body)                 # still waiting: the controls remain
        self.m["add_transaction_comment"].assert_not_called()

    def test_documents_stored_before_a_failure_remain_and_are_listed_on_retry(self):
        self.sp_on = True
        self.start(advance_error=RuntimeError("SQL down"))
        self.sp_items = [{"filename": "Other - a.pdf", "original_filename": "a.pdf", "doc_type": "Other",
                          "uploaded_date": "2026-10-20T10:00:00Z", "web_url": "u"}]
        body = self.respond("text", [_file("b.pdf")]).get_data(as_text=True)
        self.assertIn("Already uploaded since this request was made", body)
        self.assertIn("a.pdf", body)

    def test_14_a_repeated_submission_is_recorded_once_and_reported_plainly(self):
        self.start(advance_error=db.WorkflowConflictError("already moved"))
        resp = self.respond("Second click")
        body = resp.get_data(as_text=True)
        self.assertIn("already submitted", body)
        self.assertIn("Nothing was recorded twice", body)
        self.assertNotIn("Response submitted", body)
        self.m["add_transaction_comment"].assert_not_called()

    def test_8_a_standalone_upload_never_advances_the_workflow_and_says_the_response_is_not_sent(self):
        self.sp_on = True
        self.start()
        resp = self.client.post(f"/dashboard/request/{RID}/attach", data={"extra_files": _file("proof.pdf")},
                                content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 302)
        self.m["upload_attachment"].assert_called_once()
        self.m["advance_transaction_workflow"].assert_not_called()
        (category, message), = self.flashes()
        self.assertEqual(category, "warning")
        self.assertIn("has NOT been submitted yet", message)
        self.assertIn("Submit Additional Information", message)
        self.assertNotIn("Response submitted", message)

    def test_a_standalone_upload_by_someone_else_gets_the_ordinary_message(self):
        self.sp_on = True
        self.start(role="sam", user_key=APPROVER)
        self.client.post(f"/dashboard/request/{RID}/attach", data={"extra_files": _file("note.pdf")}, content_type="multipart/form-data")
        (category, message), = self.flashes()
        self.assertEqual(category, "success")
        self.assertNotIn("NOT been submitted", message)
        self.m["advance_transaction_workflow"].assert_not_called()

    def test_a_failed_standalone_upload_is_never_followed_by_a_success_message(self):
        self.sp_on = True
        self.start(upload_error=RuntimeError("Graph error"))
        self.client.post(f"/dashboard/request/{RID}/attach", data={"extra_files": _file("proof.pdf")}, content_type="multipart/form-data")
        flashes = self.flashes()
        self.assertEqual([c for c, _ in flashes], ["danger"])
        self.assertIn("proof.pdf", flashes[0][1])

    def test_a_partly_failed_standalone_upload_reports_only_what_was_stored(self):
        self.sp_on = True
        outcomes = iter([{"correlation_id": "c"}, RuntimeError("Graph error")])

        def upload(*a, **k):
            result = next(outcomes)
            if isinstance(result, Exception):
                raise result
            return result

        self.start()
        self.m["upload_attachment"].side_effect = upload
        self.client.post(f"/dashboard/request/{RID}/attach",
                         data={"extra_files": [_file("ok.pdf"), _file("bad.pdf")]}, content_type="multipart/form-data")
        text = " ".join(m for _, m in self.flashes())
        self.assertIn("bad.pdf", text)
        self.assertIn("1 file(s) uploaded", text)


# ═══ Workflow contract, history and approvals ══════════════════════════════
class WorkflowCompatibilityTests(unittest.TestCase):
    def test_19_the_workflow_event_literals_are_unchanged(self):
        self.assertEqual(wf.EVENT_RFI_REQUESTED, "more_info")
        self.assertEqual(wf.EVENT_RFI_RESPONSE_SUBMITTED, "requester_respond")
        self.assertEqual(wf.ACTION_REQUESTER_RESPOND, "requester_respond")
        self.assertEqual(wf.events_for_action(from_status=wf.STATUS_MORE_INFO, action=wf.ACTION_REQUESTER_RESPOND,
                                              new_status=wf.STATUS_PENDING_CONTROLLER), ["requester_respond"])
        self.assertEqual(wf.STATUS_MORE_INFO, "More Information Requested")

    def test_20_routing_back_is_identical_for_corporate_and_property(self):
        for classification in ("Corporate", "Property"):
            for origin, expected in ((wf.STATUS_PENDING_APPROVER, wf.STATUS_PENDING_APPROVER),
                                     (wf.STATUS_PENDING_CONTROLLER, wf.STATUS_PENDING_CONTROLLER),
                                     (wf.STATUS_PENDING_VP, wf.STATUS_PENDING_VP), (wf.STATUS_PENDING_CFO, wf.STATUS_PENDING_CFO)):
                with self.subTest(classification=classification, origin=origin):
                    status, _, _, satisfied = wf.determine_next_step(
                        dict(_txn(entity_classification=classification), rfi_origin_status=origin), wf.ACTION_REQUESTER_RESPOND)
                    self.assertEqual((status, satisfied), (expected, []))

    def test_only_the_original_requester_may_respond(self):
        txn = _txn()
        wf.authorize_action(role="submitter", user_key=REQUESTER, txn=txn, action=wf.ACTION_REQUESTER_RESPOND)
        for role, key in (("submitter", OTHER), ("sam", APPROVER), ("controller", CONTROLLER)):
            with self.assertRaises(wf.UnauthorizedActionError):
                wf.authorize_action(role=role, user_key=key, txn=txn, action=wf.ACTION_REQUESTER_RESPOND)
        with self.assertRaises(wf.UnauthorizedActionError):
            wf.authorize_action(role="submitter", user_key=REQUESTER, txn=_txn(status=wf.STATUS_PENDING_APPROVER),
                                action=wf.ACTION_REQUESTER_RESPOND)


class AdvanceStatementTests(unittest.TestCase):
    """13/12/14 - the existing SQL transition: it never resets approvals or removes history."""

    def _run(self, rowcount=1):
        cur = MagicMock()
        cur.rowcount = rowcount
        cur.fetchone.return_value = (77,)
        conn = MagicMock()
        conn.cursor.return_value = cur
        with patch.object(db, "get_connection", return_value=conn):
            db.advance_transaction_workflow(
                8001, from_status=wf.STATUS_MORE_INFO, new_status=wf.STATUS_PENDING_CONTROLLER,
                new_owner_user_key=CONTROLLER, actor_user_key=REQUESTER, actor_role="Submitter",
                action=wf.ACTION_REQUESTER_RESPOND, workflow_role="Controller", comments="Here you go")
        return cur, conn

    def test_13_12_only_status_stage_owner_change_and_nothing_is_deleted_or_cleared(self):
        cur, conn = self._run()
        statements = [c.args[0] for c in cur.execute.call_args_list]
        update = statements[0]
        self.assertRegex(update, r"SET Current_Status = \?, Current_Workflow_Stage = \?, CurrentOwner_User_Key = \?, Modified_DateTime = \?")
        for protected in ("Requires_VP", "Requires_CFO", "VPApprover_User_Key", "CFOApprover_User_Key", "SelectedApprover_User_Key",
                          "SelectedController_User_Key", "ApprovalRule_Key", "Amount", "Beneficiary", "Request_ID"):
            self.assertNotIn(protected, update)
        for sql in statements:
            self.assertNotRegex(sql, r"(?i)\bDELETE\b")
        events = [s for s in statements if "INSERT INTO etransactions.WorkflowEvent" in s]
        self.assertEqual(len(events), 1)
        self.assertEqual(cur.execute.call_args_list[-1].args[1][3], "requester_respond")      # Event_Type
        conn.commit.assert_called_once()

    def test_14_a_second_response_to_an_already_answered_request_writes_nothing(self):
        cur = MagicMock()
        cur.rowcount = 0
        conn = MagicMock()
        conn.cursor.return_value = cur
        with patch.object(db, "get_connection", return_value=conn), self.assertRaises(db.WorkflowConflictError):
            db.advance_transaction_workflow(
                8001, from_status=wf.STATUS_MORE_INFO, new_status=wf.STATUS_PENDING_CONTROLLER, new_owner_user_key=CONTROLLER,
                actor_user_key=REQUESTER, actor_role="Submitter", action=wf.ACTION_REQUESTER_RESPOND, workflow_role="Controller")
        self.assertEqual(cur.execute.call_count, 1)                 # only the guarded UPDATE ran
        self.assertIn("Current_Status = ?", cur.execute.call_args.args[0])
        conn.rollback.assert_called_once()
        conn.commit.assert_not_called()


class RfiDetailQueryTests(unittest.TestCase):
    """The active RFI is read from existing WorkflowEvent rows - no new SQL objects."""

    def _detail(self, status, timeline):
        columns = {"status": status, "amount": 1, "request_id": RID}

        class Cursor:
            sql = ""
            description = [(k,) for k in columns]

            def execute(self, sql, params=None):
                self.sql = sql

            def fetchone(self):
                return tuple(columns.values())

            def fetchall(self):
                return timeline if "WorkflowEvent" in self.sql else []

        conn = MagicMock()
        conn.cursor.return_value = Cursor()
        with patch.object(db, "get_connection", return_value=conn), patch.object(db, "bulk_schema_ready", return_value=False):
            return db.get_request_detail(RID)

    def _row(self, event_type, when, comment, actor, from_status):
        return (event_type, "", "", "More Information Requested", when, comment, actor, from_status)

    def test_2_3_the_latest_rfi_supplies_the_reason_the_requester_and_the_date(self):
        timeline = [
            self._row("Submitted", datetime(2026, 10, 15, 8, 0), "", "Rita Requester", None),
            self._row("more_info", datetime(2026, 10, 16, 9, 0), "Old question", "Alice Approver", "Pending Approver"),
            self._row("requester_respond", datetime(2026, 10, 17, 9, 0), "Old answer", "Rita Requester", "More Information Requested"),
            self._row("more_info", RFI_AT, REASON, "Carl Controller", "Pending Controller"),
        ]
        rfi = self._detail(wf.STATUS_MORE_INFO, timeline)["rfi"]
        self.assertEqual((rfi["reason"], rfi["requested_by"], rfi["origin_status"]), (REASON, "Carl Controller", "Pending Controller"))
        self.assertEqual(rfi["requested_at"], "2026-10-19 09:30 AM")
        self.assertEqual(rfi["requested_dt"], RFI_AT)

    def test_there_is_no_rfi_unless_the_transaction_is_awaiting_the_requester(self):
        timeline = [self._row("more_info", RFI_AT, REASON, "Alice Approver", "Pending Approver")]
        self.assertIsNone(self._detail(wf.STATUS_PENDING_APPROVER, timeline)["rfi"])

    def test_a_missing_rfi_event_still_yields_a_section_with_a_clear_blank_reason(self):
        rfi = self._detail(wf.STATUS_MORE_INFO, [])["rfi"]
        self.assertEqual((rfi["reason"], rfi["requested_by"], rfi["requested_dt"]), ("", "", None))

    def test_the_timeline_history_is_unchanged(self):
        timeline = [self._row("more_info", RFI_AT, REASON, "Alice Approver", "Pending Approver")]
        detail = self._detail(wf.STATUS_MORE_INFO, timeline)
        self.assertEqual(len(detail["timeline"]), 1)
        self.assertIn("Alice Approver", detail["timeline"][0]["event"])


class AttachmentsSinceTests(unittest.TestCase):
    def test_only_files_uploaded_after_the_request_are_counted(self):
        items = [{"filename": "a", "uploaded_date": "2026-10-18T00:00:00Z"},
                 {"filename": "b", "original_filename": "B.pdf", "uploaded_date": "2026-10-21T00:00:00Z"},
                 {"filename": "c", "uploaded_date": ""}, {"filename": "d", "uploaded_date": "not a date"}]
        self.assertEqual(app_module._attachments_since(items, RFI_AT), ["B.pdf"])
        self.assertEqual(app_module._attachments_since(items, None), [])


# ═══ Dashboard ═════════════════════════════════════════════════════════════
class DashboardTests(unittest.TestCase):
    def _records(self):
        base = {"status": wf.STATUS_MORE_INFO, "property_dept": "Treasury", "assigned_approver": "Rita Requester", "amount": 500.0,
                "submitted_date": "2026-10-01", "days_pending": 1, "request_type": "ACH", "urgent": False, "urgency_reason": "",
                "currency": "USD", "prepared_by": "Rita Requester", "transaction_key": 1}
        return [dict(base, request_id="TXN-MINE", prepared_by_user_key=REQUESTER),
                dict(base, request_id="TXN-THEIRS", prepared_by_user_key=555),
                dict(base, request_id="TXN-WAITING", prepared_by_user_key=REQUESTER, status=wf.STATUS_PENDING_APPROVER)]

    def _dashboard(self, role, user_key):
        client = app_module.app.test_client()
        with client.session_transaction() as sess:
            sess["role"] = role
        with patch.object(app_module, "current_app_user_key", return_value=user_key), \
             patch.object(app_module.db, "get_dashboard_records", return_value=self._records()):
            return client.get("/dashboard")

    def test_18_the_requester_can_see_which_requests_need_their_response(self):
        body = self._dashboard("submitter", REQUESTER).get_data(as_text=True)
        self.assertIn("Your response is required", body)
        self.assertEqual(body.count("Your response is required"), 1)
        self.assertIn("1</strong>\n        request needs your response", body)
        self.assertIn("#additional-information-requested", body)
        self.assertRegex(body, r'class="btn btn-sm btn-warning text-dark response-required-action"')
        self.assertEqual(body.count("response-required-action"), 1)       # only TXN-MINE; not THEIRS, not TXN-WAITING

    def test_a_reviewer_viewing_the_same_row_gets_no_response_cue(self):
        body = self._dashboard("sam", APPROVER).get_data(as_text=True)
        self.assertNotIn("Your response is required", body)
        self.assertNotIn("response-required-banner", body)
        self.assertNotIn("response-required-action", body)

    def test_existing_status_filters_and_values_are_unchanged(self):
        client = app_module.app.test_client()
        with client.session_transaction() as sess:
            sess["role"] = "submitter"
        with patch.object(app_module, "current_app_user_key", return_value=REQUESTER), \
             patch.object(app_module.db, "get_dashboard_records", return_value=self._records()):
            body = client.get("/dashboard?status=More+Information+Requested").get_data(as_text=True)
        self.assertIn("TXN-MINE", body)
        self.assertIn("TXN-THEIRS", body)
        self.assertNotIn("TXN-WAITING", body)


class ExternalIsolationTests(unittest.TestCase):
    def test_no_external_access_was_attempted(self):
        self.assertEqual(external_guard.state.blocked, [])


if __name__ == "__main__":
    unittest.main()
