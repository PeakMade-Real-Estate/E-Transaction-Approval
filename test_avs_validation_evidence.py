"""
Tests for the AVS Screenshot vs. Alternative Verification distinction on the
single intake "Validation Evidence" upload field.

Previously the uploaded file was always tagged with the SharePoint DocumentType
"Validation Evidence", and the detail page's "AVS Validation Evidence" box read
a dead mock-only field (docs_checklist.wf_avs_screenshot) that was never wired
to real data — so it always showed "Not attached" even when the file was
correctly uploaded. Now the SAME upload is tagged "AVS Screenshot" (AVS Score
>= 90) or "Validation Evidence" (alternative verification) at upload time, and
the detail page reads the real SharePoint-backed attachment for each.

Run:  python -m unittest test_avs_validation_evidence -v
"""
import io
import unittest
from unittest.mock import patch

import app as app_module
import sharepoint


class ValidationEvidenceDocTypeTests(unittest.TestCase):
    def test_score_at_or_above_90_is_avs_screenshot(self):
        self.assertEqual(
            app_module._validation_evidence_doc_type({"avs_score": "90"}),
            sharepoint.DOC_TYPE_AVS_SCREENSHOT,
        )
        self.assertEqual(
            app_module._validation_evidence_doc_type({"avs_score": "100"}),
            sharepoint.DOC_TYPE_AVS_SCREENSHOT,
        )

    def test_score_below_90_is_validation_evidence(self):
        self.assertEqual(
            app_module._validation_evidence_doc_type({"avs_score": "89"}),
            sharepoint.DOC_TYPE_VALIDATION_EVIDENCE,
        )

    def test_blank_or_missing_score_is_validation_evidence(self):
        self.assertEqual(
            app_module._validation_evidence_doc_type({"avs_score": ""}),
            sharepoint.DOC_TYPE_VALIDATION_EVIDENCE,
        )
        self.assertEqual(
            app_module._validation_evidence_doc_type({}),
            sharepoint.DOC_TYPE_VALIDATION_EVIDENCE,
        )

    def test_non_numeric_score_is_validation_evidence(self):
        self.assertEqual(
            app_module._validation_evidence_doc_type({"avs_score": "bogus"}),
            sharepoint.DOC_TYPE_VALIDATION_EVIDENCE,
        )


class IntakeUploadTaggingTests(unittest.TestCase):
    """The single file_validation_evidence upload must be tagged per-submission,
    not with the field's static default DocumentType."""

    def setUp(self):
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as sess:
            sess["role"] = "submitter"

    def test_required_upload_tagged_as_avs_screenshot_when_score_high(self):
        with app_module.app.test_request_context(
            "/intake/submit", method="POST",
            data={"avs_score": "95", "file_validation_evidence": (io.BytesIO(b"x"), "e.png")},
            content_type="multipart/form-data",
        ):
            with patch.object(app_module, "sharepoint_enabled", return_value=True), \
                 patch.object(app_module, "current_roles_display", return_value="Submitter"), \
                 patch.object(app_module.sharepoint, "upload_attachment") as mock_upload:
                app_module._upload_required_intake_attachments("TXN-2026-0001")
        validation_call = next(
            c for c in mock_upload.call_args_list
            if c.kwargs.get("doc_type") in (sharepoint.DOC_TYPE_AVS_SCREENSHOT, sharepoint.DOC_TYPE_VALIDATION_EVIDENCE)
        )
        self.assertEqual(validation_call.kwargs["doc_type"], sharepoint.DOC_TYPE_AVS_SCREENSHOT)

    def test_required_upload_tagged_as_validation_evidence_when_score_low(self):
        with app_module.app.test_request_context(
            "/intake/submit", method="POST",
            data={"avs_score": "50", "file_validation_evidence": (io.BytesIO(b"x"), "e.png")},
            content_type="multipart/form-data",
        ):
            with patch.object(app_module, "sharepoint_enabled", return_value=True), \
                 patch.object(app_module, "current_roles_display", return_value="Submitter"), \
                 patch.object(app_module.sharepoint, "upload_attachment") as mock_upload:
                app_module._upload_required_intake_attachments("TXN-2026-0002")
        validation_call = next(
            c for c in mock_upload.call_args_list
            if c.kwargs.get("doc_type") in (sharepoint.DOC_TYPE_AVS_SCREENSHOT, sharepoint.DOC_TYPE_VALIDATION_EVIDENCE)
        )
        self.assertEqual(validation_call.kwargs["doc_type"], sharepoint.DOC_TYPE_VALIDATION_EVIDENCE)

    def test_other_required_fields_unaffected_by_avs_score(self):
        with app_module.app.test_request_context(
            "/intake/submit", method="POST",
            data={
                "avs_score": "95",
                "file_validation_evidence": (io.BytesIO(b"x"), "e.png"),
                "file_wire_ach_instructions": (io.BytesIO(b"x"), "w.pdf"),
                "file_payment_support": (io.BytesIO(b"x"), "p.pdf"),
            },
            content_type="multipart/form-data",
        ):
            with patch.object(app_module, "sharepoint_enabled", return_value=True), \
                 patch.object(app_module, "current_roles_display", return_value="Submitter"), \
                 patch.object(app_module.sharepoint, "upload_attachment") as mock_upload:
                app_module._upload_required_intake_attachments("TXN-2026-0003")
        doc_types = {c.kwargs["doc_type"] for c in mock_upload.call_args_list}
        self.assertIn(sharepoint.DOC_TYPE_WIRE_ACH_INSTRUCTIONS, doc_types)
        self.assertIn(sharepoint.DOC_TYPE_PAYMENT_SUPPORT, doc_types)


class DraftFilesPresentAvsAliasTests(unittest.TestCase):
    """An existing Draft's required evidence may be stored under either
    DocumentType — both must satisfy the one logical required slot."""

    def test_existing_avs_screenshot_satisfies_validation_evidence_requirement(self):
        with app_module.app.test_request_context("/intake/submit", method="POST", data={}):
            with patch.object(app_module, "sharepoint_enabled", return_value=True), \
                 patch.object(app_module.sharepoint, "list_attachments",
                               return_value=[{"doc_type": sharepoint.DOC_TYPE_AVS_SCREENSHOT, "filename": "avs.png"}]):
                present = app_module._draft_files_present("TXN-2026-0004")
        self.assertTrue(present["validation_evidence"])


class ApproverDetailAvsVsAlternativeDisplayTests(unittest.TestCase):
    """Section B must independently reflect whichever DocumentType was actually
    used, never the dead docs_checklist mock field."""

    def _fake_record(self, **overrides):
        record = {
            "request_id": "TXN-2026-999", "status": "Pending Approver", "property_dept": "Test Prop",
            "property_code": "", "request_type": "ACH", "treasury_service_date": "2026-01-01",
            "prepared_date": "2026-01-01", "submitted_date": "2026-01-01", "amount": 1000.0,
            "currency": "USD", "payment_purpose": "Test", "urgent": False, "urgency_reason": "",
            "current_workflow_stage": "Approver", "approval_tier": "Senior Accounting Manager / Assistant Controller",
            "requires_vp": False, "over_1m": False, "days_pending": 0,
            "prepared_by": "A", "assigned_approver": "B", "approver": "B", "controller": "C",
            "vp_approver": "", "cfo_approver": "",
            "orig_bank_name": "Wells Fargo", "orig_account_name": "Ops",
            "orig_account_number": "4521039812", "orig_routing_number": "121000248",
            "orig_bank_contact": "", "notes_orig": "",
            "recv_payee_name": "Acme", "recv_contact_name": "", "recv_contact_email": "", "recv_contact_phone": "",
            "recv_bank_name": "Chase", "recv_account_name": "Acme Ops",
            "recv_account_number": "9938271045", "recv_routing_number": "021000021",
            "recv_bank_address": "", "notes_recv": "",
            "verbal_confirmed": False, "verbal_confirmed_with": "", "verbal_contact_name": "",
            "verbal_confirm_datetime": "", "avs_score": "", "external_source": False, "internal_doc_not_used": False,
            "instructions_previously_used": False, "last_used_date": "",
            "prior_transaction_key": None, "prior_request_id": None,
            "entity_classification": "Corporate", "current_owner_user_key": 2, "bank_releaser_user_key": None,
            "docs_checklist": {}, "attachments": {}, "attachment_urls": {},
            "extra_attachments": [], "timeline": [], "comments": [],
        }
        record.update(overrides)
        return record

    def _get(self, record):
        client = app_module.app.test_client()
        with client.session_transaction() as sess:
            sess["role"] = "sam"
        with patch.object(app_module.db, "get_transaction_for_workflow",
                           return_value={"status": "Pending Approver", "accounting_group_key": None}), \
             patch.object(app_module.db, "get_request_detail", return_value=record), \
             patch.object(app_module, "sharepoint_enabled", return_value=False):
            return client.get("/dashboard/request/TXN-2026-999")

    def test_avs_screenshot_shown_when_avs_doc_type_uploaded(self):
        record = self._fake_record(
            attachments={"avs_screenshot": "wf_avs.png"},
            attachment_urls={"avs_screenshot": "https://example/wf_avs.png"},
        )
        body = self._get(record).get_data(as_text=True)
        self.assertIn("wf_avs.png", body)

    def test_validation_evidence_not_shown_when_only_avs_screenshot_uploaded(self):
        # The "Alternative verification" box must stay empty when the AVS path
        # (not the alternative-verification path) was actually used.
        record = self._fake_record(attachments={"avs_screenshot": "wf_avs.png"})
        body = self._get(record).get_data(as_text=True)
        self.assertIn("wf_avs.png", body)
        self.assertNotIn("alt_verification", body)
        self.assertIn("Not attached", body)  # the Validation Evidence slot itself

    def test_alternative_verification_shown_when_validation_evidence_doc_type_uploaded(self):
        record = self._fake_record(
            attachments={"validation_evidence": "alt_verification.pdf"},
            attachment_urls={"validation_evidence": "https://example/alt_verification.pdf"},
        )
        body = self._get(record).get_data(as_text=True)
        self.assertIn("alt_verification.pdf", body)


class VerificationChecklistIndicatorTests(ApproverDetailAvsVsAlternativeDisplayTests):
    """
    Section B: 'Instructions from external source' / 'Internal document NOT
    used as backup' are optional situational facts, not required-and-missing
    items — unchecked must render the neutral indicator, never the red
    'missing' one used for the genuinely required verbal confirmation.
    """

    def test_unchecked_optional_facts_use_neutral_not_missing_indicator(self):
        record = self._fake_record(external_source=False, internal_doc_not_used=False, verbal_confirmed=False)
        body = self._get(record).get_data(as_text=True)
        self.assertIn("checklist-na", body)

    def test_checked_optional_facts_use_complete_indicator(self):
        record = self._fake_record(external_source=True, internal_doc_not_used=True)
        body = self._get(record).get_data(as_text=True)
        self.assertEqual(body.count("checklist-complete"), 2)

    def test_required_verbal_confirmation_still_uses_missing_indicator_when_unchecked(self):
        record = self._fake_record(verbal_confirmed=False, external_source=True, internal_doc_not_used=True)
        body = self._get(record).get_data(as_text=True)
        self.assertIn("checklist-missing", body)


if __name__ == "__main__":
    unittest.main()
