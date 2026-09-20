"""
Tests for Batch 5: Treasury/release evidence — required evidence uploads for
Treasury Initiated (Property), Treasury Released (Corporate), and Bank Release
(Property), enforced server-side before the corresponding workflow action
succeeds. Reuses the existing SharePoint attachment architecture (sharepoint.py)
and the Batch 4 authorization gates (visibility vs. action authority).

Run:  python -m unittest test_treasury_evidence -v
"""
import io
import unittest
from unittest.mock import patch

import app as app_module
import sharepoint
import workflow


def _txn(**overrides):
    base = {
        "transaction_key": 1,
        "request_id": "TXN-2026-700",
        "status": workflow.STATUS_READY_FOR_TREASURY,
        "prepared_by_user_key": 100,
        "selected_approver_user_key": 200,
        "selected_controller_user_key": 300,
        "vp_approver_user_key": None,
        "cfo_approver_user_key": None,
        "current_owner_user_key": None,
        "bank_releaser_user_key": None,
        "requires_vp": False,
        "requires_cfo": False,
        "amount": 1000.0,
        "entity_classification": "Property",
        "accounting_group_key": None,
    }
    base.update(overrides)
    return base


def _evidence_file(filename="evidence.png"):
    return (io.BytesIO(b"fake evidence bytes"), filename)


class TreasuryEvidenceTestCase(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def _set_role(self, role):
        with self.client.session_transaction() as sess:
            sess["role"] = role

    def _post_action(self, request_id, action, *, evidence=True, filename="evidence.png", comment=""):
        data = {"action": action, "comment": comment}
        if evidence:
            data["evidence_file"] = _evidence_file(filename)
        return self.client.post(
            f"/dashboard/request/{request_id}/action",
            data=data,
            content_type="multipart/form-data",
            follow_redirects=False,
        )


class PropertyTreasuryInitiationTests(TreasuryEvidenceTestCase):
    def test_1_valid_evidence_succeeds(self):
        self._set_role("treasury")
        txn = _txn(status=workflow.STATUS_READY_FOR_TREASURY, entity_classification="Property",
                    selected_controller_user_key=300)
        with patch.object(app_module, "current_app_user_key", return_value=None), \
             patch.object(app_module.db, "get_transaction_for_workflow", return_value=txn), \
             patch.object(app_module, "sharepoint_enabled", return_value=True), \
             patch.object(app_module.sharepoint, "upload_attachment",
                          return_value={"filename": "evidence.png", "web_url": "https://sp/e.png", "correlation_id": "c-1"}) as mock_upload, \
             patch.object(app_module.db, "advance_transaction_workflow") as mock_advance:
            resp = self._post_action(txn["request_id"], workflow.ACTION_TREASURY_INITIATED)
        self.assertEqual(resp.status_code, 302)
        mock_advance.assert_called_once()
        mock_upload.assert_called_once()

    def test_2_no_evidence_fails(self):
        self._set_role("treasury")
        txn = _txn(status=workflow.STATUS_READY_FOR_TREASURY, entity_classification="Property")
        with patch.object(app_module, "current_app_user_key", return_value=None), \
             patch.object(app_module.db, "get_transaction_for_workflow", return_value=txn), \
             patch.object(app_module.sharepoint, "upload_attachment") as mock_upload, \
             patch.object(app_module.db, "advance_transaction_workflow") as mock_advance:
            resp = self._post_action(txn["request_id"], workflow.ACTION_TREASURY_INITIATED, evidence=False)
        self.assertEqual(resp.status_code, 302)
        mock_advance.assert_not_called()
        mock_upload.assert_not_called()

    def test_3_4_failed_upload_prevents_transition_and_workflow_event(self):
        self._set_role("treasury")
        txn = _txn(status=workflow.STATUS_READY_FOR_TREASURY, entity_classification="Property")
        with patch.object(app_module, "current_app_user_key", return_value=None), \
             patch.object(app_module.db, "get_transaction_for_workflow", return_value=txn), \
             patch.object(app_module, "sharepoint_enabled", return_value=True), \
             patch.object(app_module.sharepoint, "upload_attachment", side_effect=RuntimeError("Graph error")), \
             patch.object(app_module.db, "advance_transaction_workflow") as mock_advance:
            resp = self._post_action(txn["request_id"], workflow.ACTION_TREASURY_INITIATED)
        self.assertEqual(resp.status_code, 302)
        mock_advance.assert_not_called()  # no status change / no WorkflowEvent

    def test_5_success_uses_treasury_initiation_evidence_doc_type(self):
        self._set_role("treasury")
        txn = _txn(status=workflow.STATUS_READY_FOR_TREASURY, entity_classification="Property")
        with patch.object(app_module, "current_app_user_key", return_value=None), \
             patch.object(app_module, "sharepoint_enabled", return_value=True), \
             patch.object(app_module.db, "get_transaction_for_workflow", return_value=txn), \
             patch.object(app_module.sharepoint, "upload_attachment",
                          return_value={"filename": "evidence.png", "web_url": "", "correlation_id": "c-1"}) as mock_upload, \
             patch.object(app_module.db, "advance_transaction_workflow"):
            self._post_action(txn["request_id"], workflow.ACTION_TREASURY_INITIATED)
        _, kwargs = mock_upload.call_args
        self.assertEqual(kwargs["doc_type"], sharepoint.DOC_TYPE_TREASURY_INITIATION_EVIDENCE)

    def test_6_transaction_becomes_awaiting_bank_release(self):
        self._set_role("treasury")
        txn = _txn(status=workflow.STATUS_READY_FOR_TREASURY, entity_classification="Property")
        with patch.object(app_module, "current_app_user_key", return_value=None), \
             patch.object(app_module, "sharepoint_enabled", return_value=True), \
             patch.object(app_module.db, "get_transaction_for_workflow", return_value=txn), \
             patch.object(app_module.sharepoint, "upload_attachment",
                          return_value={"filename": "e.png", "web_url": "", "correlation_id": "c-1"}), \
             patch.object(app_module.db, "advance_transaction_workflow") as mock_advance:
            self._post_action(txn["request_id"], workflow.ACTION_TREASURY_INITIATED)
        _, kwargs = mock_advance.call_args
        self.assertEqual(kwargs["new_status"], workflow.STATUS_AWAITING_RELEASE)

    def test_7_correct_vp_becomes_owner_when_assigned(self):
        self._set_role("treasury")
        txn = _txn(status=workflow.STATUS_READY_FOR_TREASURY, entity_classification="Property",
                    vp_approver_user_key=400, selected_controller_user_key=300)
        with patch.object(app_module, "current_app_user_key", return_value=None), \
             patch.object(app_module, "sharepoint_enabled", return_value=True), \
             patch.object(app_module.db, "get_transaction_for_workflow", return_value=txn), \
             patch.object(app_module.sharepoint, "upload_attachment",
                          return_value={"filename": "e.png", "web_url": "", "correlation_id": "c-1"}), \
             patch.object(app_module.db, "advance_transaction_workflow") as mock_advance:
            self._post_action(txn["request_id"], workflow.ACTION_TREASURY_INITIATED)
        _, kwargs = mock_advance.call_args
        self.assertEqual(kwargs["new_owner_user_key"], 400)  # VP, never CFO


class CorporateTreasuryReleaseTests(TreasuryEvidenceTestCase):
    def test_8_valid_evidence_succeeds(self):
        self._set_role("treasury")
        txn = _txn(status=workflow.STATUS_READY_FOR_TREASURY, entity_classification="Corporate")
        with patch.object(app_module, "current_app_user_key", return_value=None), \
             patch.object(app_module, "sharepoint_enabled", return_value=True), \
             patch.object(app_module.db, "get_transaction_for_workflow", return_value=txn), \
             patch.object(app_module.sharepoint, "upload_attachment",
                          return_value={"filename": "e.png", "web_url": "", "correlation_id": "c-1"}) as mock_upload, \
             patch.object(app_module.db, "advance_transaction_workflow") as mock_advance:
            resp = self._post_action(txn["request_id"], workflow.ACTION_TREASURY_RELEASED)
        self.assertEqual(resp.status_code, 302)
        mock_advance.assert_called_once()
        mock_upload.assert_called_once()

    def test_9_no_evidence_fails(self):
        self._set_role("treasury")
        txn = _txn(status=workflow.STATUS_READY_FOR_TREASURY, entity_classification="Corporate")
        with patch.object(app_module, "current_app_user_key", return_value=None), \
             patch.object(app_module.db, "get_transaction_for_workflow", return_value=txn), \
             patch.object(app_module.sharepoint, "upload_attachment") as mock_upload, \
             patch.object(app_module.db, "advance_transaction_workflow") as mock_advance:
            resp = self._post_action(txn["request_id"], workflow.ACTION_TREASURY_RELEASED, evidence=False)
        self.assertEqual(resp.status_code, 302)
        mock_advance.assert_not_called()
        mock_upload.assert_not_called()

    def test_10_failed_upload_prevents_completed_status(self):
        self._set_role("treasury")
        txn = _txn(status=workflow.STATUS_READY_FOR_TREASURY, entity_classification="Corporate")
        with patch.object(app_module, "current_app_user_key", return_value=None), \
             patch.object(app_module, "sharepoint_enabled", return_value=True), \
             patch.object(app_module.db, "get_transaction_for_workflow", return_value=txn), \
             patch.object(app_module.sharepoint, "upload_attachment", side_effect=RuntimeError("Graph error")), \
             patch.object(app_module.db, "advance_transaction_workflow") as mock_advance:
            self._post_action(txn["request_id"], workflow.ACTION_TREASURY_RELEASED)
        mock_advance.assert_not_called()

    def test_11_success_uses_treasury_release_evidence_doc_type(self):
        self._set_role("treasury")
        txn = _txn(status=workflow.STATUS_READY_FOR_TREASURY, entity_classification="Corporate")
        with patch.object(app_module, "current_app_user_key", return_value=None), \
             patch.object(app_module, "sharepoint_enabled", return_value=True), \
             patch.object(app_module.db, "get_transaction_for_workflow", return_value=txn), \
             patch.object(app_module.sharepoint, "upload_attachment",
                          return_value={"filename": "e.png", "web_url": "", "correlation_id": "c-1"}) as mock_upload, \
             patch.object(app_module.db, "advance_transaction_workflow"):
            self._post_action(txn["request_id"], workflow.ACTION_TREASURY_RELEASED)
        _, kwargs = mock_upload.call_args
        self.assertEqual(kwargs["doc_type"], sharepoint.DOC_TYPE_TREASURY_RELEASE_EVIDENCE)

    def test_12_transaction_becomes_completed_directly(self):
        self._set_role("treasury")
        txn = _txn(status=workflow.STATUS_READY_FOR_TREASURY, entity_classification="Corporate")
        with patch.object(app_module, "current_app_user_key", return_value=None), \
             patch.object(app_module, "sharepoint_enabled", return_value=True), \
             patch.object(app_module.db, "get_transaction_for_workflow", return_value=txn), \
             patch.object(app_module.sharepoint, "upload_attachment",
                          return_value={"filename": "e.png", "web_url": "", "correlation_id": "c-1"}), \
             patch.object(app_module.db, "advance_transaction_workflow") as mock_advance:
            self._post_action(txn["request_id"], workflow.ACTION_TREASURY_RELEASED)
        _, kwargs = mock_advance.call_args
        self.assertEqual(kwargs["new_status"], workflow.STATUS_COMPLETED)
        self.assertIsNone(kwargs["new_owner_user_key"])

    def test_13_only_one_advance_call_no_separate_bank_release_step(self):
        self._set_role("treasury")
        txn = _txn(status=workflow.STATUS_READY_FOR_TREASURY, entity_classification="Corporate")
        with patch.object(app_module, "current_app_user_key", return_value=None), \
             patch.object(app_module, "sharepoint_enabled", return_value=True), \
             patch.object(app_module.db, "get_transaction_for_workflow", return_value=txn), \
             patch.object(app_module.sharepoint, "upload_attachment",
                          return_value={"filename": "e.png", "web_url": "", "correlation_id": "c-1"}), \
             patch.object(app_module.db, "advance_transaction_workflow") as mock_advance:
            self._post_action(txn["request_id"], workflow.ACTION_TREASURY_RELEASED)
        self.assertEqual(mock_advance.call_count, 1)


class PropertyFinalBankReleaseTests(TreasuryEvidenceTestCase):
    def test_14_valid_evidence_succeeds(self):
        self._set_role("controller")
        txn = _txn(status=workflow.STATUS_AWAITING_RELEASE, entity_classification="Property",
                    selected_controller_user_key=300, bank_releaser_user_key=300)
        with patch.object(app_module, "current_app_user_key", return_value=None), \
             patch.object(app_module, "sharepoint_enabled", return_value=True), \
             patch.object(app_module.db, "get_transaction_for_workflow", return_value=txn), \
             patch.object(app_module.sharepoint, "upload_attachment",
                          return_value={"filename": "e.png", "web_url": "", "correlation_id": "c-1"}) as mock_upload, \
             patch.object(app_module.db, "advance_transaction_workflow") as mock_advance:
            resp = self._post_action(txn["request_id"], workflow.ACTION_BANK_RELEASE)
        self.assertEqual(resp.status_code, 302)
        mock_advance.assert_called_once()
        mock_upload.assert_called_once()

    def test_15_no_evidence_fails(self):
        self._set_role("controller")
        txn = _txn(status=workflow.STATUS_AWAITING_RELEASE, entity_classification="Property",
                    selected_controller_user_key=300, bank_releaser_user_key=300)
        with patch.object(app_module, "current_app_user_key", return_value=None), \
             patch.object(app_module.db, "get_transaction_for_workflow", return_value=txn), \
             patch.object(app_module.sharepoint, "upload_attachment") as mock_upload, \
             patch.object(app_module.db, "advance_transaction_workflow") as mock_advance:
            resp = self._post_action(txn["request_id"], workflow.ACTION_BANK_RELEASE, evidence=False)
        self.assertEqual(resp.status_code, 302)
        mock_advance.assert_not_called()
        mock_upload.assert_not_called()

    def test_16_failed_upload_keeps_awaiting_bank_release(self):
        self._set_role("controller")
        txn = _txn(status=workflow.STATUS_AWAITING_RELEASE, entity_classification="Property",
                    selected_controller_user_key=300, bank_releaser_user_key=300)
        with patch.object(app_module, "current_app_user_key", return_value=None), \
             patch.object(app_module, "sharepoint_enabled", return_value=True), \
             patch.object(app_module.db, "get_transaction_for_workflow", return_value=txn), \
             patch.object(app_module.sharepoint, "upload_attachment", side_effect=RuntimeError("Graph error")), \
             patch.object(app_module.db, "advance_transaction_workflow") as mock_advance:
            self._post_action(txn["request_id"], workflow.ACTION_BANK_RELEASE)
        mock_advance.assert_not_called()

    def test_17_success_uses_bank_release_evidence_doc_type(self):
        self._set_role("controller")
        txn = _txn(status=workflow.STATUS_AWAITING_RELEASE, entity_classification="Property",
                    selected_controller_user_key=300, bank_releaser_user_key=300)
        with patch.object(app_module, "current_app_user_key", return_value=None), \
             patch.object(app_module, "sharepoint_enabled", return_value=True), \
             patch.object(app_module.db, "get_transaction_for_workflow", return_value=txn), \
             patch.object(app_module.sharepoint, "upload_attachment",
                          return_value={"filename": "e.png", "web_url": "", "correlation_id": "c-1"}) as mock_upload, \
             patch.object(app_module.db, "advance_transaction_workflow"):
            self._post_action(txn["request_id"], workflow.ACTION_BANK_RELEASE)
        _, kwargs = mock_upload.call_args
        self.assertEqual(kwargs["doc_type"], sharepoint.DOC_TYPE_BANK_RELEASE_EVIDENCE)

    def test_18_success_results_in_completed(self):
        self._set_role("controller")
        txn = _txn(status=workflow.STATUS_AWAITING_RELEASE, entity_classification="Property",
                    selected_controller_user_key=300, bank_releaser_user_key=300)
        with patch.object(app_module, "current_app_user_key", return_value=None), \
             patch.object(app_module, "sharepoint_enabled", return_value=True), \
             patch.object(app_module.db, "get_transaction_for_workflow", return_value=txn), \
             patch.object(app_module.sharepoint, "upload_attachment",
                          return_value={"filename": "e.png", "web_url": "", "correlation_id": "c-1"}), \
             patch.object(app_module.db, "advance_transaction_workflow") as mock_advance:
            self._post_action(txn["request_id"], workflow.ACTION_BANK_RELEASE)
        _, kwargs = mock_advance.call_args
        self.assertEqual(kwargs["new_status"], workflow.STATUS_COMPLETED)

    def test_19_cfo_cannot_perform_final_property_release(self):
        self._set_role("cfo")
        txn = _txn(status=workflow.STATUS_AWAITING_RELEASE, entity_classification="Property",
                    bank_releaser_user_key=300)
        with patch.object(app_module, "current_app_user_key", return_value=None), \
             patch.object(app_module, "sharepoint_enabled", return_value=True), \
             patch.object(app_module.db, "get_transaction_for_workflow", return_value=txn), \
             patch.object(app_module.sharepoint, "upload_attachment") as mock_upload, \
             patch.object(app_module.db, "advance_transaction_workflow") as mock_advance:
            self._post_action(txn["request_id"], workflow.ACTION_BANK_RELEASE)
        mock_advance.assert_not_called()
        mock_upload.assert_not_called()  # blocked before evidence is ever touched

    def test_20_incorrect_controller_cannot_release(self):
        self._set_role("controller")
        txn = _txn(status=workflow.STATUS_AWAITING_RELEASE, entity_classification="Property",
                    bank_releaser_user_key=555)
        with patch.object(app_module, "current_app_user_key", return_value=999), \
             patch.object(app_module, "sharepoint_enabled", return_value=True), \
             patch.object(app_module.db, "get_transaction_for_workflow", return_value=txn), \
             patch.object(app_module.sharepoint, "upload_attachment") as mock_upload, \
             patch.object(app_module.db, "advance_transaction_workflow") as mock_advance:
            self._post_action(txn["request_id"], workflow.ACTION_BANK_RELEASE)
        mock_advance.assert_not_called()
        mock_upload.assert_not_called()


class SecurityAndAssociationTests(TreasuryEvidenceTestCase):
    def test_21_evidence_associates_with_correct_transaction(self):
        self._set_role("treasury")
        txn = _txn(status=workflow.STATUS_READY_FOR_TREASURY, entity_classification="Property",
                    transaction_key=42, request_id="TXN-2026-777")
        with patch.object(app_module, "current_app_user_key", return_value=None), \
             patch.object(app_module, "sharepoint_enabled", return_value=True), \
             patch.object(app_module.db, "get_transaction_for_workflow", return_value=txn), \
             patch.object(app_module.sharepoint, "upload_attachment",
                          return_value={"filename": "e.png", "web_url": "", "correlation_id": "c-1"}) as mock_upload, \
             patch.object(app_module.db, "advance_transaction_workflow"):
            self._post_action(txn["request_id"], workflow.ACTION_TREASURY_INITIATED)
        args, kwargs = mock_upload.call_args
        self.assertEqual(args[0], "TXN-2026-777")
        self.assertEqual(kwargs["transaction_key"], 42)

    def test_22_cannot_bypass_workflow_authorization_via_evidence_upload(self):
        # sam (Approver) is not authorized to perform bank_release at all —
        # attaching evidence must not change that outcome.
        self._set_role("sam")
        txn = _txn(status=workflow.STATUS_AWAITING_RELEASE, entity_classification="Property",
                    bank_releaser_user_key=300)
        with patch.object(app_module, "current_app_user_key", return_value=None), \
             patch.object(app_module, "sharepoint_enabled", return_value=True), \
             patch.object(app_module.db, "get_transaction_for_workflow", return_value=txn), \
             patch.object(app_module.sharepoint, "upload_attachment") as mock_upload, \
             patch.object(app_module.db, "advance_transaction_workflow") as mock_advance:
            self._post_action(txn["request_id"], workflow.ACTION_BANK_RELEASE)
        mock_advance.assert_not_called()
        mock_upload.assert_not_called()

    def test_23_filename_cannot_control_arbitrary_sharepoint_path(self):
        # Pure unit test on the existing sanitizer — illegal/path characters neutralized.
        self.assertNotIn("/", sharepoint._sanitize_filename("../../evil.txt"))
        self.assertNotIn("\\", sharepoint._sanitize_filename("..\\..\\evil.txt"))
        for ch in '"*:<>?/\\|':
            self.assertNotIn(ch, sharepoint._sanitize_filename(f"name{ch}file.txt"))

    def test_24_25_direct_url_authorization_still_enforced_for_evidence_display(self):
        self._set_role("sam")  # not the assigned approver for this transaction
        txn = _txn(status=workflow.STATUS_PENDING_APPROVER, selected_approver_user_key=999)
        with patch.object(app_module, "current_app_user_key", return_value=1), \
             patch.object(app_module.db, "get_transaction_for_workflow", return_value=txn), \
             patch.object(app_module.sharepoint, "list_attachments") as mock_list, \
             patch.object(app_module.db, "get_request_detail") as mock_detail:
            resp = self.client.get(f"/dashboard/request/{txn['request_id']}")
        self.assertEqual(resp.status_code, 403)
        mock_detail.assert_not_called()
        mock_list.assert_not_called()  # evidence is never even fetched for an unauthorized viewer


class ExistingAttachmentRegressionTests(unittest.TestCase):
    """Part 17/18 #26-29: pre-existing attachment categories must still upload unchanged."""

    def setUp(self):
        self.client = app_module.app.test_client()

    def test_26_avs_validation_evidence_upload_unchanged(self):
        with self.client.session_transaction() as sess:
            sess["role"] = "submitter"
        with app_module.app.test_request_context(
            "/", data={"file_validation_evidence": _evidence_file("avs.png")},
            content_type="multipart/form-data",
        ):
            with patch.object(app_module, "sharepoint_enabled", return_value=True):
                with patch.object(app_module.sharepoint, "upload_attachment") as mock_upload:
                    app_module._upload_required_intake_attachments("TXN-2026-800")
        _, kwargs = mock_upload.call_args
        self.assertEqual(kwargs["doc_type"], sharepoint.DOC_TYPE_VALIDATION_EVIDENCE)
        self.assertEqual(kwargs["section"], sharepoint.SECTION_VERIFICATION)

    def test_27_payment_support_upload_unchanged(self):
        with app_module.app.test_request_context(
            "/", data={"file_payment_support": _evidence_file("support.pdf")},
            content_type="multipart/form-data",
        ):
            with patch.object(app_module, "sharepoint_enabled", return_value=True):
                with patch.object(app_module.sharepoint, "upload_attachment") as mock_upload:
                    app_module._upload_required_intake_attachments("TXN-2026-801")
        _, kwargs = mock_upload.call_args
        self.assertEqual(kwargs["doc_type"], sharepoint.DOC_TYPE_PAYMENT_SUPPORT)

    def test_28_wire_ach_instructions_upload_unchanged(self):
        with app_module.app.test_request_context(
            "/", data={"file_wire_ach_instructions": _evidence_file("wire.pdf")},
            content_type="multipart/form-data",
        ):
            with patch.object(app_module, "sharepoint_enabled", return_value=True):
                with patch.object(app_module.sharepoint, "upload_attachment") as mock_upload:
                    app_module._upload_required_intake_attachments("TXN-2026-802")
        _, kwargs = mock_upload.call_args
        self.assertEqual(kwargs["doc_type"], sharepoint.DOC_TYPE_WIRE_ACH_INSTRUCTIONS)
        self.assertEqual(kwargs["section"], sharepoint.SECTION_RECEIVING_BANKING)

    def test_29_rfi_additional_attachment_upload_unchanged(self):
        with self.client.session_transaction() as sess:
            sess["role"] = "controller"
        with patch.object(app_module, "sharepoint_enabled", return_value=True):
            with patch.object(app_module.sharepoint, "upload_attachment") as mock_upload:
                resp = self.client.post(
                    "/dashboard/request/TXN-2026-803/attach",
                    data={"extra_files": _evidence_file("extra.pdf"), "attachment_description": "note"},
                    content_type="multipart/form-data",
                )
        self.assertEqual(resp.status_code, 302)
        _, kwargs = mock_upload.call_args
        self.assertEqual(kwargs["doc_type"], sharepoint.DOC_TYPE_OTHER)
        self.assertEqual(kwargs["section"], sharepoint.SECTION_ADDITIONAL)


if __name__ == "__main__":
    unittest.main()
