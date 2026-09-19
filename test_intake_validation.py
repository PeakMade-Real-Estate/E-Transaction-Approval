"""
Tests for app._validate_intake_submission() — the authoritative server-side
validation for POST /intake/submit added to close the gap where all business
rules (required fields, required attachments, AVS/verification policy,
originating-account eligibility) previously existed only in intake.html's
client-side JavaScript and could be bypassed by a direct POST.

Pure function — no Flask request context or DB connection required.

Run:  python -m unittest test_intake_validation -v
"""

import unittest

import app


def valid_form(**overrides):
    """A minimally complete, fully-valid intake submission's form fields."""
    data = {
        "request_type": "ACH",
        "treasury_service_date": "2026-10-01",
        "prepared_by_key": "1",
        "prepared_date": "2026-09-18",
        "property_dept": "Sunset Ridge Apartments",
        "approver_key": "2",
        "controller_key": "3",
        "amount": "1000.00",
        "currency": "USD",
        "payment_purpose": "Vendor payment",
        "urgent": "no",
        "urgency_reason": "",
        "bank_account_key": "5",
        "recv_payee_name": "Acme Vendor LLC",
        "recv_bank_name": "Chase Bank",
        "recv_account_name": "Acme Operating",
        "recv_account_number": "12345678",
        "recv_routing_number": "021000021",
        "recv_bank_address": "",
        "instructions_previously_used": "yes",
        "last_used_date": "2026-01-01",
        "verbal_confirmed_with_known": "",
        "verbal_contact_name": "",
        "verbal_confirm_datetime": "",
        "avs_score": "95",
    }
    data.update(overrides)
    return data


def valid_files():
    return {"validation_evidence": True, "wire_ach_instructions": True, "payment_support": True}


class BasicRequiredFieldTests(unittest.TestCase):
    def test_1_valid_submission_has_no_errors(self):
        errors = app._validate_intake_submission(valid_form(), files_present=valid_files(), bank_account_status="Open")
        self.assertEqual(errors, [])

    def test_1_missing_required_field_is_rejected(self):
        form = valid_form(property_dept="")
        errors = app._validate_intake_submission(form, files_present=valid_files(), bank_account_status="Open")
        self.assertTrue(any("Property / Department" in e for e in errors))

    def test_amount_zero_is_rejected(self):
        form = valid_form(amount="0")
        errors = app._validate_intake_submission(form, files_present=valid_files(), bank_account_status="Open")
        self.assertTrue(any("Amount" in e for e in errors))

    def test_amount_non_numeric_is_rejected(self):
        form = valid_form(amount="not-a-number")
        errors = app._validate_intake_submission(form, files_present=valid_files(), bank_account_status="Open")
        self.assertTrue(any("Amount" in e for e in errors))


class OriginatingBankAccountTests(unittest.TestCase):
    def test_2_invalid_bank_account_key_is_rejected(self):
        errors = app._validate_intake_submission(valid_form(), files_present=valid_files(), bank_account_status=None)
        self.assertTrue(any("could not be found" in e for e in errors))

    def test_3_closed_originating_account_is_rejected(self):
        errors = app._validate_intake_submission(valid_form(), files_present=valid_files(), bank_account_status="Closed")
        self.assertTrue(any("closed" in e for e in errors))

    def test_missing_bank_account_key_is_rejected(self):
        form = valid_form(bank_account_key="")
        errors = app._validate_intake_submission(form, files_present=valid_files(), bank_account_status=None)
        self.assertTrue(any("Originating Bank Account is required" in e for e in errors))

    def test_open_is_accepted(self):
        errors = app._validate_intake_submission(valid_form(), files_present=valid_files(), bank_account_status="Open")
        self.assertEqual(errors, [])

    def test_legacy_active_is_no_longer_accepted(self):
        # Batch 2 status vocabulary cleanup: 'Active' is no longer a valid
        # canonical status (all legacy rows were migrated to 'Open').
        errors = app._validate_intake_submission(valid_form(), files_present=valid_files(), bank_account_status="Active")
        self.assertTrue(any("closed" in e for e in errors))


class AttachmentTests(unittest.TestCase):
    def test_4_missing_payment_support_is_rejected(self):
        files = valid_files()
        files["payment_support"] = False
        errors = app._validate_intake_submission(valid_form(), files_present=files, bank_account_status="Open")
        self.assertTrue(any("Payment Support" in e for e in errors))

    def test_5_missing_wire_ach_instructions_is_rejected(self):
        files = valid_files()
        files["wire_ach_instructions"] = False
        errors = app._validate_intake_submission(valid_form(), files_present=files, bank_account_status="Open")
        self.assertTrue(any("External ACH/Wire Instructions" in e for e in errors))

    def test_missing_validation_evidence_is_rejected(self):
        files = valid_files()
        files["validation_evidence"] = False
        errors = app._validate_intake_submission(valid_form(), files_present=files, bank_account_status="Open")
        self.assertTrue(any("Validation Evidence" in e for e in errors))


class AvsVerificationTests(unittest.TestCase):
    def test_6_avs_95_with_evidence_succeeds(self):
        form = valid_form(avs_score="95")
        errors = app._validate_intake_submission(form, files_present=valid_files(), bank_account_status="Open")
        self.assertEqual(errors, [])

    def test_7_avs_95_without_evidence_fails(self):
        form = valid_form(avs_score="95")
        files = valid_files()
        files["validation_evidence"] = False
        errors = app._validate_intake_submission(form, files_present=files, bank_account_status="Open")
        self.assertTrue(any("Validation Evidence" in e for e in errors))

    def test_8_avs_89_without_alternative_verification_fails(self):
        # The UI has a single Validation Evidence field that IS the alternative
        # verification document — "without alternative verification" means that
        # same file is missing.
        form = valid_form(avs_score="89")
        files = valid_files()
        files["validation_evidence"] = False
        errors = app._validate_intake_submission(form, files_present=files, bank_account_status="Open")
        self.assertTrue(any("Validation Evidence" in e for e in errors))

    def test_9_avs_89_with_valid_alternative_verification_succeeds(self):
        form = valid_form(avs_score="89")
        errors = app._validate_intake_submission(form, files_present=valid_files(), bank_account_status="Open")
        self.assertEqual(errors, [])

    def test_10_blank_avs_without_alternative_verification_fails(self):
        form = valid_form(avs_score="")
        files = valid_files()
        files["validation_evidence"] = False
        errors = app._validate_intake_submission(form, files_present=files, bank_account_status="Open")
        self.assertTrue(any("Validation Evidence" in e for e in errors))

    def test_avs_out_of_range_is_rejected(self):
        for bad in ("101", "-5"):
            with self.subTest(avs=bad):
                form = valid_form(avs_score=bad)
                errors = app._validate_intake_submission(form, files_present=valid_files(), bank_account_status="Open")
                self.assertTrue(any("between 0 and 100" in e for e in errors))

    def test_avs_non_numeric_is_rejected(self):
        form = valid_form(avs_score="abc")
        errors = app._validate_intake_submission(form, files_present=valid_files(), bank_account_status="Open")
        self.assertTrue(any("whole number" in e for e in errors))


class UrgentTests(unittest.TestCase):
    def test_11_urgent_without_reason_fails(self):
        form = valid_form(urgent="yes", urgency_reason="")
        errors = app._validate_intake_submission(form, files_present=valid_files(), bank_account_status="Open")
        self.assertTrue(any("Urgency Reason" in e for e in errors))

    def test_urgent_with_reason_succeeds(self):
        form = valid_form(urgent="yes", urgency_reason="Vendor deadline tomorrow")
        errors = app._validate_intake_submission(form, files_present=valid_files(), bank_account_status="Open")
        self.assertEqual(errors, [])

    def test_not_urgent_does_not_require_reason(self):
        form = valid_form(urgent="no", urgency_reason="")
        errors = app._validate_intake_submission(form, files_present=valid_files(), bank_account_status="Open")
        self.assertEqual(errors, [])


class NewUnverifiedInstructionTests(unittest.TestCase):
    def test_12_new_instructions_without_known_contact_confirmation_fails(self):
        form = valid_form(
            instructions_previously_used="no", last_used_date="",
            verbal_confirmed_with_known="", verbal_contact_name="Jane Doe",
            verbal_confirm_datetime="2026-09-18T10:00",
        )
        errors = app._validate_intake_submission(form, files_present=valid_files(), bank_account_status="Open")
        self.assertTrue(any("known contact" in e for e in errors))

    def test_requester_confirmation_is_not_treated_as_known_contact(self):
        # Explicitly confirms the distinction: checking "confirmed with requester"
        # must NOT satisfy the "known contact" requirement.
        form = valid_form(
            instructions_previously_used="no", last_used_date="",
            verbal_confirmed_with_known="", verbal_confirmed_with_requester="on",
            verbal_contact_name="Jane Doe", verbal_confirm_datetime="2026-09-18T10:00",
        )
        errors = app._validate_intake_submission(form, files_present=valid_files(), bank_account_status="Open")
        self.assertTrue(any("known contact" in e for e in errors))

    def test_new_instructions_with_full_known_contact_confirmation_succeeds(self):
        form = valid_form(
            instructions_previously_used="no", last_used_date="",
            verbal_confirmed_with_known="on", verbal_contact_name="Jane Doe",
            verbal_confirm_datetime="2026-09-18T10:00",
        )
        errors = app._validate_intake_submission(form, files_present=valid_files(), bank_account_status="Open")
        self.assertEqual(errors, [])

    def test_previously_used_requires_last_used_date(self):
        form = valid_form(instructions_previously_used="yes", last_used_date="")
        errors = app._validate_intake_submission(form, files_present=valid_files(), bank_account_status="Open")
        self.assertTrue(any("Last Date Used" in e for e in errors))


class WireBeneficiaryAddressTests(unittest.TestCase):
    def test_13_wire_missing_beneficiary_address_fails(self):
        form = valid_form(request_type="Wire", recv_bank_address="")
        errors = app._validate_intake_submission(form, files_present=valid_files(), bank_account_status="Open")
        self.assertTrue(any("Beneficiary Address" in e for e in errors))

    def test_wire_with_beneficiary_address_succeeds(self):
        form = valid_form(request_type="Wire", recv_bank_address="123 Main St, City, ST 00000")
        errors = app._validate_intake_submission(form, files_present=valid_files(), bank_account_status="Open")
        self.assertEqual(errors, [])

    def test_non_wire_does_not_require_beneficiary_address(self):
        form = valid_form(request_type="ACH", recv_bank_address="")
        errors = app._validate_intake_submission(form, files_present=valid_files(), bank_account_status="Open")
        self.assertEqual(errors, [])
