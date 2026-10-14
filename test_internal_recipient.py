"""
Tests for Item 4: internal maintained-BankAccount recipient selection.

A requester may choose an existing, Open company BankAccount as the recipient of
an ACH / Intra Bank Transfer. Only the BankAccount key (plus the recipient mode)
is submitted by the browser; the server validates it and builds a transaction-
specific Beneficiary + BeneficiaryBankInstruction snapshot itself. External
recipients are unchanged.

Every test is mock-only: `external_guard` (imported first, as
test_external_isolation.py requires) swaps the database/SharePoint/network layers
for an inert fake, and the tests additionally patch the exact db/app functions
they exercise.

Run:  python -m unittest test_internal_recipient -v
"""
import io
import unittest
import external_guard  # noqa: F401  (must precede app/db/sharepoint imports)
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import app as app_module
import db
import workflow as wf

# Obviously-fake values: only ever used inside mocks, never a real account.
REAL_ACCT = "7700112233"
REAL_RTN = "021000089"
MASKED_ACCT = "XXXX-XXXX-2233"


def _form(**overrides):
    """A complete, otherwise-valid INTERNAL ACH submission (new, non-draft)."""
    data = {
        "form_mode": "submit",
        "transaction_key": "", "request_id": "",
        "request_type": "ACH",
        "treasury_service_date": "2026-10-01",
        "classification": "corporate", "property_dept": "Sunset Ridge",
        "approver_key": "2", "controller_key": "3",
        "amount": "1000.00", "currency": "USD",
        "payment_purpose": "Internal funding", "urgent": "no",
        "bank_account_key": "5",
        "recipient_mode": "internal", "recipient_bank_account_key": "9",
        "recv_payee_name": "Sunset Ridge Operating LLC",
        "verbal_confirmed_with_known": "on", "verbal_contact_name": "John Doe",
        "verbal_confirm_datetime": "2026-09-18T10:00",
        "avs_score": "95",
    }
    data.update(overrides)
    return data


def _external_form(**overrides):
    data = _form(
        recipient_mode="external", recipient_bank_account_key="",
        recv_bank_name="Chase Bank", recv_account_name="Acme Ops",
        recv_account_number="12345678", recv_routing_number="021000021",
    )
    data.pop("recipient_mode")
    data.pop("recipient_bank_account_key")
    data.update(overrides)
    return data


def _files(**omit):
    files = {
        "file_validation_evidence": (io.BytesIO(b"x"), "v.pdf"),
        "file_wire_ach_instructions": (io.BytesIO(b"x"), "w.pdf"),
        "file_payment_support": (io.BytesIO(b"x"), "p.pdf"),
    }
    for name in omit:
        files.pop(name, None)
    return files


def _summary(key=9, status="Open", snapshot_ready=True, **overrides):
    s = {
        "bank_account_key": key, "bank_name": "Wells Fargo", "account_title": "Sunset Ridge Operating",
        "last4": "2233", "status": status, "entity_name": "Sunset Ridge Apartments",
        "classification": "Property", "snapshot_ready": snapshot_ready, "routing_present": True,
    }
    s.update(overrides)
    return s


_RULE = {"approval_rule_key": 7, "requires_approver": True, "requires_controller": True,
         "requires_vp": False, "requires_cfo": False}


class _RouteBase(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as sess:
            sess["role"] = "submitter"

    def _post(self, form, *, files=None, summary=None, prior_match=None, summary_effect=None,
              stored_recipient=(True, None), extra=()):
        """POST /intake/submit with every db/SharePoint call mocked. Returns
        (response, mocks-dict)."""
        mocks = {}
        with ExitStack() as stack:
            def p(target, name, **kw):
                m = stack.enter_context(patch.object(target, name, **kw))
                mocks[name] = m
                return m

            p(app_module, "current_app_user", return_value={"user_key": 11, "display_name": "U", "email": ""})
            p(app_module.db, "get_bank_account_status", return_value="Open")
            p(app_module.db, "get_app_user_role_codes", return_value=["sam", "controller"])
            p(app_module.db, "get_app_user_by_key", return_value={"user_key": 2, "display_name": "A", "email": ""})
            if summary_effect is not None:
                p(app_module.db, "get_internal_recipient_summary", side_effect=summary_effect)
            else:
                p(app_module.db, "get_internal_recipient_summary", return_value=summary)
            p(app_module.db, "get_internal_recipient_instruction_values",
              return_value={"bank_name": "Wells Fargo", "account_name": "Sunset Ridge Operating",
                            "account_number": REAL_ACCT, "routing_number": REAL_RTN, "address": ""})
            p(app_module.db, "get_draft_recipient_bank_account_key", return_value=stored_recipient)
            p(app_module.db, "get_draft_beneficiary_instruction_key", return_value=77)
            p(app_module.db, "get_draft_request_id", return_value="TXN-2026-D100")
            p(app_module.db, "find_prior_completed_beneficiary_match", return_value=prior_match)
            p(app_module.db, "resolve_approval_rule", return_value=_RULE)
            p(app_module.db, "reserve_request_id", return_value="TXN-2026-9999")
            p(app_module.db, "insert_transaction", return_value="TXN-2026-9999")
            p(app_module.db, "create_draft", return_value=("TXN-2026-D100", 55))
            p(app_module.db, "update_draft")
            p(app_module.db, "finalize_draft_submission")
            p(app_module.db, "get_bank_accounts", return_value=[])
            p(app_module.db, "get_user_list", return_value=[])
            p(app_module, "_upload_required_intake_attachments")
            p(app_module, "_upload_intake_attachments")
            for item in extra:
                stack.enter_context(item)
            data = dict(form)
            data.update(files if files is not None else _files())
            resp = self.client.post("/intake/submit", data=data, content_type="multipart/form-data",
                                     follow_redirects=False)
        return resp, mocks


# ── 1-2. External unchanged; internal Open accepted ──────────────────────────

class ExternalUnchangedAndInternalAcceptedTests(_RouteBase):
    def test_01_external_submission_is_unchanged(self):
        resp, m = self._post(_external_form())
        self.assertEqual(resp.status_code, 302)
        data = m["insert_transaction"].call_args[0][0]
        self.assertIsNone(data["recipient_bank_account_key"])
        self.assertEqual(data["recv_bank_name"], "Chase Bank")
        self.assertEqual(data["recv_account_number"], "12345678")
        self.assertEqual(data["recv_routing_number"], "021000021")
        m["get_internal_recipient_summary"].assert_not_called()

    def test_02_internal_open_account_accepted_and_only_key_is_persisted(self):
        resp, m = self._post(_form(), summary=_summary())
        self.assertEqual(resp.status_code, 302)
        data = m["insert_transaction"].call_args[0][0]
        self.assertEqual(data["recipient_bank_account_key"], 9)
        # The browser sent no bank/account/routing value; none is fabricated here —
        # the DB layer builds the snapshot from the master account.
        self.assertEqual(data["recv_bank_name"], "")
        self.assertEqual(data["recv_account_number"], "")
        self.assertEqual(data["recv_routing_number"], "")


# ── 3-8. Server-side rejections ──────────────────────────────────────────────

class InternalRecipientRejectionTests(_RouteBase):
    def _assert_rejected(self, resp, mocks, fragment):
        self.assertEqual(resp.status_code, 200)  # form re-rendered with the error
        mocks["insert_transaction"].assert_not_called()
        mocks["reserve_request_id"].assert_not_called()
        self.assertIn(fragment, resp.get_data(as_text=True))

    def test_03_invalid_nonexistent_recipient_rejected(self):
        resp, m = self._post(_form(recipient_bank_account_key="999"), summary=None)
        self._assert_rejected(resp, m, "could not be found")

    def test_04_closed_recipient_rejected(self):
        resp, m = self._post(_form(), summary=_summary(status="Closed"))
        self._assert_rejected(resp, m, "closed and cannot be used")

    def test_05_same_account_as_originating_rejected(self):
        resp, m = self._post(_form(recipient_bank_account_key="5", bank_account_key="5"),
                             summary=_summary(key=5))
        self._assert_rejected(resp, m, "cannot be the same as the originating")

    def test_06_forged_recipient_mode_rejected(self):
        resp, m = self._post(_form(recipient_mode="admin-override"), summary=_summary())
        self._assert_rejected(resp, m, "Recipient type is invalid")

    def test_06b_internal_mode_rejected_for_wire_ach_pull_and_eft(self):
        for request_type in ("Wire", "ACH Pull", "EFT"):
            with self.subTest(request_type=request_type):
                resp, m = self._post(_form(request_type=request_type), summary=_summary())
                self._assert_rejected(resp, m, "only be the recipient of an ACH or Intra Bank Transfer")

    def test_07_injected_recipient_key_in_external_mode_rejected(self):
        resp, m = self._post(_external_form(recipient_bank_account_key="9"))
        self._assert_rejected(resp, m, "cannot be combined with an external recipient")
        resp, m = self._post(_external_form(recipient_mode="external", recipient_bank_account_key="9"))
        self._assert_rejected(resp, m, "cannot be combined with an external recipient")

    def test_08_missing_or_non_numeric_key_in_internal_mode_rejected(self):
        resp, m = self._post(_form(recipient_bank_account_key=""), summary=None)
        self._assert_rejected(resp, m, "Select a maintained internal bank account")
        resp, m = self._post(_form(recipient_bank_account_key="9; DROP TABLE"), summary=None)
        self._assert_rejected(resp, m, "recipient bank account is invalid")

    def test_08b_unreadable_masked_master_account_rejected(self):
        resp, m = self._post(_form(), summary=_summary(snapshot_ready=False))
        self._assert_rejected(resp, m, "could not be read securely")

    def test_08c_internal_payee_name_still_required(self):
        resp, m = self._post(_form(recv_payee_name=""), summary=_summary())
        self._assert_rejected(resp, m, "Recipient / Payee Name is required")


# ── 9-12. Draft save / resume / finalize ─────────────────────────────────────

class InternalDraftTests(_RouteBase):
    def test_09_save_draft_internal_persists_key_without_manual_bank_fields(self):
        resp, m = self._post(_form(form_mode="draft"), summary=_summary())
        self.assertEqual(resp.status_code, 302)
        draft_data = m["create_draft"].call_args[0][0]
        self.assertEqual(draft_data["recipient_bank_account_key"], 9)
        self.assertEqual(draft_data["recv_account_number"], "")

    def test_09b_second_save_updates_same_draft_with_key(self):
        resp, m = self._post(_form(form_mode="draft", transaction_key="55", request_id="TXN-2026-D100"),
                             summary=_summary())
        self.assertEqual(resp.status_code, 302)
        m["create_draft"].assert_not_called()
        self.assertEqual(m["update_draft"].call_args[0][1]["recipient_bank_account_key"], 9)

    def test_10_save_draft_rejects_closed_or_same_account(self):
        resp, m = self._post(_form(form_mode="draft"), summary=_summary(status="Closed"))
        self.assertEqual(resp.status_code, 200)
        m["create_draft"].assert_not_called()
        resp, m = self._post(_form(form_mode="draft", recipient_bank_account_key="5"), summary=_summary(key=5))
        self.assertEqual(resp.status_code, 200)
        m["create_draft"].assert_not_called()

    def test_11_resume_renders_internal_selection_without_sensitive_values(self):
        draft = {
            "transaction_key": 5, "request_id": "TXN-2026-D010", "property_dept": "", "property_code": "",
            "bank_account_key": 5, "approver_key": 2, "controller_key": 3, "request_type": "ACH",
            "treasury_service_date": "2026-10-01", "amount": 1000.0, "currency": "USD",
            "payment_purpose": "", "urgent": False, "urgency_reason": "", "prepared_date": "2026-09-18",
            "recv_payee_name": "Sunset Ridge Operating LLC",
            "recv_contact_name": "", "recv_contact_email": "", "recv_contact_phone": "",
            "recv_bank_name": "Wells Fargo", "recv_account_name": "Sunset Ridge Operating", "recv_bank_address": "",
            "recv_account_number_masked": "********2233", "recv_routing_number_masked": "*********",
            "originating_bank_account": {"bank_name": "Chase", "account_title": "Orig", "last4": "1111"},
            "recipient_bank_account_key": 9, "recipient_mode": "internal",
            "recipient_account": _summary(),
        }
        with patch.object(app_module, "current_app_user", return_value={"user_key": 11, "display_name": "O", "email": ""}), \
             patch.object(app_module.db, "get_draft_for_edit", return_value=draft), \
             patch.object(app_module.db, "get_user_list", return_value=[]), \
             patch.object(app_module.db, "get_bank_accounts",
                          return_value=[{"bank_account_key": 9, "bank_name": "Wells Fargo",
                                         "account_title": "Sunset Ridge Operating", "account_last4": "2233",
                                         "entity_name": "Sunset Ridge Apartments", "classification": "Property"}]), \
             patch.object(app_module.sharepoint, "list_attachments", return_value=[]):
            resp = self.client.get("/intake/draft/5")
        body = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200)
        self.assertRegex(body, r'id="recipient_mode_internal"[^>]*checked')
        self.assertRegex(body, r'name="recipient_bank_account_key" id="recipient_bank_account_key"\s+value="9"')
        self.assertIn("Sunset Ridge Apartments", body)
        self.assertNotIn(REAL_ACCT, body)
        self.assertNotIn(REAL_RTN, body)
        # An internal draft must not tell the requester "leave blank to keep" an external value.
        self.assertNotIn("Leave blank to keep it", body)

    def test_12_finalize_route_revalidates_closed_recipient(self):
        form = _form(transaction_key="55", request_id="TXN-2026-D100")
        resp, m = self._post(form, summary=_summary(status="Closed"))
        self.assertEqual(resp.status_code, 200)
        m["finalize_draft_submission"].assert_not_called()

    def test_12b_finalize_route_passes_key_to_db_layer(self):
        form = _form(transaction_key="55", request_id="TXN-2026-D100")
        resp, m = self._post(form, summary=_summary())
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(m["finalize_draft_submission"].call_args[0][1]["recipient_bank_account_key"], 9)

    def test_12c_finalize_closed_after_save_is_reported_without_banking_values(self):
        form = _form(transaction_key="55", request_id="TXN-2026-D100")
        err = db.InternalRecipientError("The selected recipient bank account is closed and cannot be used.")
        with ExitStack() as stack:
            stack.enter_context(patch.object(app_module, "current_app_user",
                                              return_value={"user_key": 11, "display_name": "U", "email": ""}))
            stack.enter_context(patch.object(app_module.db, "get_bank_account_status", return_value="Open"))
            stack.enter_context(patch.object(app_module.db, "get_app_user_role_codes", return_value=["sam", "controller"]))
            stack.enter_context(patch.object(app_module.db, "get_internal_recipient_summary", return_value=_summary()))
            stack.enter_context(patch.object(app_module.db, "get_internal_recipient_instruction_values",
                                              return_value={"bank_name": "B", "account_name": "A",
                                                            "account_number": REAL_ACCT, "routing_number": REAL_RTN,
                                                            "address": ""}))
            stack.enter_context(patch.object(app_module.db, "find_prior_completed_beneficiary_match", return_value=None))
            stack.enter_context(patch.object(app_module.db, "resolve_approval_rule", return_value=_RULE))
            stack.enter_context(patch.object(app_module.db, "get_bank_accounts", return_value=[]))
            stack.enter_context(patch.object(app_module.db, "get_user_list", return_value=[]))
            stack.enter_context(patch.object(app_module, "_upload_intake_attachments"))
            stack.enter_context(patch.object(app_module.db, "get_draft_request_id", return_value="TXN-2026-D100"))
            stack.enter_context(patch.object(app_module.db, "finalize_draft_submission", side_effect=err))
            data = dict(form)
            data.update(_files())
            resp = self.client.post("/intake/submit", data=data, content_type="multipart/form-data")
        body = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("closed and cannot be used", body)
        self.assertNotIn(REAL_ACCT, body)
        self.assertNotIn(REAL_RTN, body)

    def test_12d_draft_switching_from_internal_to_external_must_reenter_numbers(self):
        form = _external_form(transaction_key="55", request_id="TXN-2026-D100",
                              recv_account_number="", recv_routing_number="")
        # Stored draft is INTERNAL (key 9): its snapshot must not silently carry over.
        resp, m = self._post(form, stored_recipient=(True, 9))
        self.assertEqual(resp.status_code, 200)
        m["finalize_draft_submission"].assert_not_called()
        # Stored draft is EXTERNAL: a blank still means "keep the stored value" (existing behavior).
        resp, m = self._post(form, stored_recipient=(True, None))
        self.assertEqual(resp.status_code, 302)


# ── 13-16. Precheck/final parity ─────────────────────────────────────────────

class PrecheckParityTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as sess:
            sess["role"] = "submitter"

    def _precheck(self, form, summary):
        data = dict(form)
        data.update({"has_validation_evidence": "true", "has_wire_ach_instructions": "true",
                     "has_payment_support": "true"})
        with patch.object(app_module, "current_app_user", return_value={"user_key": 11, "display_name": "U", "email": ""}), \
             patch.object(app_module.db, "get_bank_account_status", return_value="Open"), \
             patch.object(app_module.db, "get_app_user_role_codes", return_value=["sam", "controller"]), \
             patch.object(app_module.db, "get_internal_recipient_summary", return_value=summary), \
             patch.object(app_module.db, "get_internal_recipient_instruction_values",
                          return_value={"bank_name": "WF", "account_name": "A", "account_number": REAL_ACCT,
                                        "routing_number": REAL_RTN, "address": ""}), \
             patch.object(app_module.db, "find_prior_completed_beneficiary_match", return_value=None), \
             patch.object(app_module.db, "resolve_approval_rule", return_value=_RULE):
            return self.client.post("/intake/precheck", data=data).get_json()

    def test_13_precheck_accepts_valid_internal_recipient(self):
        self.assertEqual(self._precheck(_form(), _summary())["errors"], [])

    def test_14_precheck_and_final_report_the_same_recipient_errors(self):
        cases = (
            (_form(), _summary(status="Closed"), "closed and cannot be used"),
            (_form(recipient_mode="bogus"), _summary(), "Recipient type is invalid"),
            (_form(request_type="Wire"), _summary(), "only be the recipient of an ACH or Intra Bank Transfer"),
            (_external_form(recipient_bank_account_key="9"), None, "cannot be combined with an external recipient"),
        )
        for form, summary, fragment in cases:
            with self.subTest(fragment=fragment):
                errors = self._precheck(form, summary)["errors"]
                self.assertTrue(any(fragment in e for e in errors), errors)
                final_errors = app_module._validate_intake_submission(
                    form, files_present={"validation_evidence": True, "wire_ach_instructions": True,
                                         "payment_support": True},
                    bank_account_status="Open", prepared_by_user_key=11, recipient_summary=summary)
                self.assertTrue(any(fragment in e for e in final_errors), final_errors)
                # Same recipient-related messages from both entry points.
                self.assertEqual([e for e in errors if fragment in e], [e for e in final_errors if fragment in e])

    def test_14b_precheck_keeps_external_validation_for_external_mode(self):
        errors = self._precheck(_external_form(recv_account_number=""), None)["errors"]
        self.assertIn("Receiving Account Number is required.", errors)


# ── 15-17, 25. DB layer: snapshot, history, masked placeholders, explicit SQL ─

class _FakeCursor:
    """Minimal cursor: answers the BankAccount snapshot SELECT and the draft guard
    SELECT by SQL text, and a surrogate key for everything else (OUTPUT INSERTED)."""

    def __init__(self, bank_row=None, guard_row=(10, 20)):
        self.executed = []
        self._last = ""
        self.rowcount = 1
        self.bank_row = bank_row
        self.guard_row = guard_row

    def execute(self, sql, params=None):
        self.executed.append((sql, list(params) if params is not None else None))
        self._last = sql

    def fetchone(self):
        if "FROM [etransactions].[BankAccount] WHERE BankAccount_Key" in self._last:
            return self.bank_row
        if "SELECT Beneficiary_Key, BeneficiaryInstruction_Key" in self._last:
            return self.guard_row
        return (99,)

    def statements(self, needle):
        return [(s, p) for s, p in self.executed if needle in s]


def _bank_row(account=REAL_ACCT, routing=REAL_RTN, status="Open", title="Sunset Ridge Operating",
              bank="Wells Fargo", name_id="SRO", system_name="SYS", address="1 Main St"):
    # (BankName, AccountTitle, AccountNameID, SystemAccountName, AccountNumber, RoutingNumber, Address, Status)
    return (bank, title, name_id, system_name, account, routing, address, status)


def _conn(cursor):
    conn = MagicMock()
    conn.cursor.return_value = cursor
    return conn


_INSERT_DATA = {
    "request_id": "TXN-2026-9999", "prepared_by_key": 11, "approver_key": 2, "controller_key": 3,
    "bank_account_key": 5, "recipient_bank_account_key": 9, "request_type": "ACH",
    "classification": "corporate", "property_dept": "Sunset Ridge", "property_code": "",
    "treasury_service_date": "2026-10-01", "prepared_date": "2026-09-18", "amount": 1000.0, "currency": "USD",
    "payment_purpose": "Internal funding", "urgent": False, "urgency_reason": "",
    "recv_payee_name": "Sunset Ridge Operating LLC",
    "recv_contact_name": "", "recv_contact_email": "", "recv_contact_phone": "",
    "recv_bank_name": "", "recv_account_name": "", "recv_account_number": "", "recv_routing_number": "",
    "recv_bank_address": "",
}


class SnapshotCreationDbTests(unittest.TestCase):
    def _insert(self, data=None, bank_row=None):
        cur = _FakeCursor(bank_row=bank_row if bank_row is not None else _bank_row())
        with patch.object(db, "get_connection", return_value=_conn(cur)), \
             patch.object(db, "resolve_approval_rule", return_value=_RULE), \
             patch.object(db, "resolve_entity_key", return_value=3):
            db.insert_transaction(dict(data or _INSERT_DATA))
        return cur

    def test_15_snapshot_is_built_from_the_master_account_server_side(self):
        cur = self._insert()
        sql, params = cur.statements("INSERT INTO [etransactions].[BeneficiaryBankInstruction]")[0]
        # [ben_key, bank, account_name, account_number, routing, address, today]
        self.assertEqual(params[1], "Wells Fargo")
        self.assertEqual(params[2], "Sunset Ridge Operating")  # AccountTitle, then AccountNameID, then SystemAccountName
        self.assertEqual(params[3], REAL_ACCT)
        self.assertEqual(params[4], REAL_RTN)
        self.assertEqual(params[5], "1 Main St")
        # Payee comes from the requester-confirmed field, never from the account record.
        _, ben_params = cur.statements("INSERT INTO [etransactions].[Beneficiary] ")[0]
        self.assertEqual(ben_params[0], "Sunset Ridge Operating LLC")

    def test_15b_account_name_falls_back_to_name_id_then_system_name(self):
        cur = self._insert(bank_row=_bank_row(title=None))
        self.assertEqual(cur.statements("INSERT INTO [etransactions].[BeneficiaryBankInstruction]")[0][1][2], "SRO")
        cur = self._insert(bank_row=_bank_row(title=None, name_id=None))
        self.assertEqual(cur.statements("INSERT INTO [etransactions].[BeneficiaryBankInstruction]")[0][1][2], "SYS")

    def test_15c_etransaction_keeps_required_beneficiary_keys_and_records_recipient(self):
        cur = self._insert()
        sql, params = cur.statements("INSERT INTO [etransactions].[ETransaction]")[0]
        columns = [c.strip() for c in sql[sql.index("(") + 1: sql.index(") OUTPUT")].split(",")]
        self.assertEqual(params[columns.index("RecipientBankAccount_Key")], 9)
        self.assertEqual(params[columns.index("OriginatingBankAccount_Key")], 5)
        self.assertEqual(params[columns.index("Beneficiary_Key")], 99)
        self.assertEqual(params[columns.index("BeneficiaryInstruction_Key")], 99)

    def test_16_history_preserved_master_account_and_other_rows_never_modified(self):
        cur = self._insert()
        all_sql = " ".join(s for s, _ in cur.executed)
        self.assertNotIn("UPDATE [etransactions].[BankAccount]", all_sql)
        self.assertNotIn("DELETE", all_sql)
        # Each transaction gets its OWN new snapshot rows (INSERT, never UPDATE of an
        # existing instruction), so editing the master account later can't alter them.
        self.assertEqual(len(cur.statements("INSERT INTO [etransactions].[BeneficiaryBankInstruction]")), 1)
        self.assertFalse(cur.statements("UPDATE [etransactions].[BeneficiaryBankInstruction]"))

    def test_17_masked_placeholder_account_or_routing_is_rejected_and_nothing_is_written(self):
        for bad_row in (_bank_row(account=MASKED_ACCT), _bank_row(routing="XXXXX0089"),
                        _bank_row(account="****2233"), _bank_row(account="")):
            with self.subTest(row=bad_row[4:6]):
                cur = _FakeCursor(bank_row=bad_row)
                conn = _conn(cur)
                with patch.object(db, "get_connection", return_value=conn), \
                     patch.object(db, "resolve_approval_rule", return_value=_RULE), \
                     patch.object(db, "resolve_entity_key", return_value=3):
                    with self.assertRaises(db.InternalRecipientError):
                        db.insert_transaction(dict(_INSERT_DATA))
                self.assertFalse(cur.statements("INSERT INTO"))
                conn.commit.assert_not_called()
                conn.rollback.assert_called()

    def test_17b_blank_master_routing_is_copied_faithfully_for_non_ach_never_invented(self):
        # Intra Bank Transfer has no routing requirement of its own, so no new one is imposed.
        cur = self._insert(data=dict(_INSERT_DATA, request_type="Intra Bank Transfer"),
                           bank_row=_bank_row(routing=None))
        self.assertEqual(cur.statements("INSERT INTO [etransactions].[BeneficiaryBankInstruction]")[0][1][4], "")

    def test_17c_closed_missing_or_same_account_is_rejected_at_the_db_layer_too(self):
        for row, data in ((_bank_row(status="Closed"), _INSERT_DATA),
                          (None, _INSERT_DATA),
                          (_bank_row(), dict(_INSERT_DATA, recipient_bank_account_key=5))):
            cur = _FakeCursor(bank_row=row)
            with patch.object(db, "get_connection", return_value=_conn(cur)), \
                 patch.object(db, "resolve_approval_rule", return_value=_RULE), \
                 patch.object(db, "resolve_entity_key", return_value=3):
                with self.assertRaises(db.InternalRecipientError):
                    db.insert_transaction(dict(data))
            self.assertFalse(cur.statements("INSERT INTO"))

    def test_25_inserts_use_explicit_column_lists_with_balanced_placeholders(self):
        cur = self._insert()
        sql, params = cur.statements("INSERT INTO [etransactions].[ETransaction]")[0]
        columns = sql[sql.index("(") + 1: sql.index(") OUTPUT")]
        self.assertIn("RecipientBankAccount_Key", columns)
        self.assertEqual(len([c for c in columns.split(",") if c.strip()]), sql.count("?"))
        self.assertEqual(sql.count("?"), len(params))

        draft_cur = _FakeCursor(bank_row=_bank_row())
        draft = dict(_INSERT_DATA, request_id="TXN-2026-D100")
        with patch.object(db, "get_connection", return_value=_conn(draft_cur)), \
             patch.object(db, "resolve_entity_key", return_value=3):
            db.create_draft(draft)
        sql, params = draft_cur.statements("INSERT INTO [etransactions].[ETransaction]")[0]
        columns = sql[sql.index("(") + 1: sql.index(") OUTPUT")]
        self.assertIn("RecipientBankAccount_Key", columns)
        self.assertEqual(len([c for c in columns.split(",") if c.strip()]), sql.count("?"))
        self.assertEqual(sql.count("?"), len(params))

    def test_25b_external_insert_has_null_recipient_and_uses_entered_values(self):
        data = dict(_INSERT_DATA, recipient_bank_account_key=None, recv_bank_name="Chase",
                    recv_account_name="Acme", recv_account_number="12345678", recv_routing_number="021000021")
        cur = _FakeCursor(bank_row=None)
        with patch.object(db, "get_connection", return_value=_conn(cur)), \
             patch.object(db, "resolve_approval_rule", return_value=_RULE), \
             patch.object(db, "resolve_entity_key", return_value=3):
            db.insert_transaction(data)
        self.assertFalse(cur.statements("FROM [etransactions].[BankAccount] WHERE BankAccount_Key"))
        _, params = cur.statements("INSERT INTO [etransactions].[BeneficiaryBankInstruction]")[0]
        self.assertEqual(params[1:5], ["Chase", "Acme", "12345678", "021000021"])
        sql, eparams = cur.statements("INSERT INTO [etransactions].[ETransaction]")[0]
        columns = [c.strip() for c in sql[sql.index("(") + 1: sql.index(") OUTPUT")].split(",")]
        self.assertIsNone(eparams[columns.index("RecipientBankAccount_Key")])


class DraftSnapshotDbTests(unittest.TestCase):
    def _finalize_data(self, **overrides):
        data = dict(_INSERT_DATA, transaction_key=55)
        data.update({"approval_rule_key": 7, "requires_vp": False, "requires_cfo": False,
                     "approval_tier": "Senior Accounting Manager / Assistant Controller"})
        data.update(overrides)
        return data

    def test_12e_finalize_rereads_master_account_and_overwrites_snapshot_atomically(self):
        cur = _FakeCursor(bank_row=_bank_row(bank="Renamed Bank"))
        conn = _conn(cur)
        with patch.object(db, "get_connection", return_value=conn), \
             patch.object(db, "resolve_entity_key", return_value=3):
            db.finalize_draft_submission(55, self._finalize_data(), prepared_by_user_key=11)
        _, params = cur.statements("UPDATE [etransactions].[BeneficiaryBankInstruction]")[0]
        self.assertIn("Renamed Bank", params)  # the CURRENT master values at submit time
        self.assertIn(REAL_ACCT, params)
        _, eparams = cur.statements("UPDATE [etransactions].[ETransaction]")[0]
        self.assertIn(9, eparams)
        conn.commit.assert_called_once()

    def test_12f_finalize_after_account_closed_raises_and_rolls_back_without_events(self):
        cur = _FakeCursor(bank_row=_bank_row(status="Closed"))
        conn = _conn(cur)
        with patch.object(db, "get_connection", return_value=conn), \
             patch.object(db, "resolve_entity_key", return_value=3):
            with self.assertRaises(db.InternalRecipientError):
                db.finalize_draft_submission(55, self._finalize_data(), prepared_by_user_key=11)
        conn.commit.assert_not_called()
        conn.rollback.assert_called()
        self.assertFalse(cur.statements("INSERT INTO [etransactions].[WorkflowEvent]"))
        self.assertFalse(cur.statements("UPDATE [etransactions].[ETransaction]"))

    def test_12g_update_draft_internal_refreshes_whole_snapshot_external_keeps_blank_preserve(self):
        data = dict(_INSERT_DATA)
        cur = _FakeCursor(bank_row=_bank_row())
        with patch.object(db, "get_connection", return_value=_conn(cur)), \
             patch.object(db, "resolve_entity_key", return_value=3):
            db.update_draft(55, data, prepared_by_user_key=11)
        sql, params = cur.statements("UPDATE [etransactions].[BeneficiaryBankInstruction]")[0]
        self.assertIn("Receiving_Account_Number = ?", sql)
        self.assertIn("Receiving_Routing_Number = ?", sql)
        self.assertIn(REAL_ACCT, params)

        ext = dict(_INSERT_DATA, recipient_bank_account_key=None, recv_bank_name="Chase",
                   recv_account_number="", recv_routing_number="")
        cur = _FakeCursor(bank_row=None)
        with patch.object(db, "get_connection", return_value=_conn(cur)), \
             patch.object(db, "resolve_entity_key", return_value=3):
            db.update_draft(55, ext, prepared_by_user_key=11)
        sql, _ = cur.statements("UPDATE [etransactions].[BeneficiaryBankInstruction]")[0]
        self.assertNotIn("Receiving_Account_Number = ?", sql)
        _, eparams = cur.statements("UPDATE [etransactions].[ETransaction]")[0]
        self.assertIn(None, eparams)  # RecipientBankAccount_Key cleared for an external recipient

    def test_12h_get_draft_recipient_key_reports_found_and_key(self):
        cur = MagicMock()
        cur.fetchone.return_value = (9,)
        with patch.object(db, "get_connection", return_value=_conn(cur)):
            self.assertEqual(db.get_draft_recipient_bank_account_key(55, 11), (True, 9))
        cur.fetchone.return_value = (None,)
        with patch.object(db, "get_connection", return_value=_conn(cur)):
            self.assertEqual(db.get_draft_recipient_bank_account_key(55, 11), (True, None))
        cur.fetchone.return_value = None
        with patch.object(db, "get_connection", return_value=_conn(cur)):
            self.assertEqual(db.get_draft_recipient_bank_account_key(55, 11), (False, None))


# ── 18. No sensitive values in browser-facing data/HTML ──────────────────────

class NoSensitiveValuesToBrowserTests(_RouteBase):
    def test_18_picker_data_and_summary_never_contain_full_numbers(self):
        cur = MagicMock()
        cur.fetchall.return_value = [(9, "Wells Fargo", "Sunset Ridge Operating", REAL_ACCT, "Sunset Ridge", "Property")]
        with patch.object(db, "get_connection", return_value=_conn(cur)):
            accounts = db.get_bank_accounts()
        self.assertEqual(accounts[0]["account_last4"], "2233")
        self.assertNotIn(REAL_ACCT, repr(accounts))

        cur = MagicMock()
        cur.fetchone.return_value = (9, "Wells Fargo", "Sunset Ridge Operating", None, REAL_ACCT, REAL_RTN,
                                     "Open", "Sunset Ridge", "Property")
        with patch.object(db, "get_connection", return_value=_conn(cur)):
            summary = db.get_internal_recipient_summary(9)
        self.assertTrue(summary["snapshot_ready"])
        self.assertEqual(summary["last4"], "2233")
        self.assertNotIn(REAL_ACCT, repr(summary))
        self.assertNotIn(REAL_RTN, repr(summary))

    def test_18b_summary_flags_masked_values_as_not_ready_and_hides_last4(self):
        cur = MagicMock()
        cur.fetchone.return_value = (9, "Wells Fargo", "T", None, MASKED_ACCT, REAL_RTN, "Open", "", "")
        with patch.object(db, "get_connection", return_value=_conn(cur)):
            summary = db.get_internal_recipient_summary(9)
        self.assertFalse(summary["snapshot_ready"])
        self.assertEqual(summary["last4"], "")

    def test_18c_new_intake_page_with_internal_picker_contains_no_full_numbers(self):
        with patch.object(app_module, "current_app_user", return_value={"user_key": 11, "display_name": "U", "email": ""}), \
             patch.object(app_module.db, "get_user_list", return_value=[]), \
             patch.object(app_module.db, "get_bank_accounts",
                          return_value=[{"bank_account_key": 9, "bank_name": "Wells Fargo",
                                         "account_title": "Sunset Ridge Operating", "account_last4": "2233",
                                         "entity_name": "Sunset Ridge Apartments", "classification": "Property"}]):
            resp = self.client.get("/intake")
        body = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('id="recipient_mode_internal"', body)
        self.assertIn('id="recipient_mode_external"', body)
        self.assertRegex(body, r'id="recipient_mode_external"[^>]*checked')  # External stays the default
        self.assertNotIn(REAL_ACCT, body)
        self.assertNotIn(REAL_RTN, body)


# ── 19. Reveal authorization retained ────────────────────────────────────────

class RevealAuthorizationRetainedTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as sess:
            sess["role"] = "treasury"

    def test_19_unauthorized_viewer_cannot_reveal_internal_snapshot_values(self):
        with patch.object(app_module.db, "get_transaction_for_workflow",
                          return_value={"status": wf.STATUS_READY_FOR_TREASURY, "accounting_group_key": None}), \
             patch.object(app_module.authorization, "can_view_transaction", return_value=False), \
             patch.object(app_module.db, "get_beneficiary_instruction_sensitive_field") as mock_reveal:
            resp = self.client.post("/dashboard/request/TXN-2026-15/reveal-banking/receiving",
                                    data={"field": "account_number"})
        self.assertEqual(resp.status_code, 403)
        mock_reveal.assert_not_called()

    def test_19b_authorized_reveal_reads_the_transactions_own_snapshot_instruction(self):
        with patch.object(app_module.db, "get_transaction_for_workflow",
                          return_value={"status": wf.STATUS_READY_FOR_TREASURY, "accounting_group_key": None}), \
             patch.object(app_module.db, "get_transaction_banking_keys",
                          return_value={"originating_bank_account_key": 5, "beneficiary_instruction_key": 17}), \
             patch.object(app_module.db, "get_beneficiary_instruction_sensitive_field",
                          return_value=REAL_ACCT) as mock_reveal:
            resp = self.client.post("/dashboard/request/TXN-2026-15/reveal-banking/receiving",
                                    data={"field": "account_number"})
        self.assertTrue(resp.get_json()["success"])
        # The snapshot instruction row, never the live master BankAccount row.
        mock_reveal.assert_called_once_with(17, "Receiving_Account_Number")


# ── 20. Prior-use / AVS / attachments / workflow unchanged ───────────────────

class PriorUseAndOtherRulesUnchangedTests(_RouteBase):
    def test_20_prior_use_uses_server_resolved_values_and_never_returns_them(self):
        match = {"transaction_key": 42, "request_id": "TXN-2026-001", "last_used_date": "2026-01-01"}
        with patch.object(app_module, "current_app_user", return_value={"user_key": 11, "display_name": "U", "email": ""}), \
             patch.object(app_module.db, "get_internal_recipient_instruction_values",
                          return_value={"bank_name": "Wells Fargo", "account_name": "T",
                                        "account_number": REAL_ACCT, "routing_number": REAL_RTN, "address": ""}), \
             patch.object(app_module.db, "find_prior_completed_beneficiary_match", return_value=match) as mock_match:
            resp = self.client.post("/intake/prior-use-check", data={
                "recipient_mode": "internal", "recipient_bank_account_key": "9",
                "recv_payee_name": "Sunset Ridge Operating LLC",
                # a hostile browser-supplied value must be ignored for an internal recipient
                "recv_bank_name": "Evil Bank", "recv_account_number": "0000", "recv_routing_number": "1111",
            })
        kwargs = mock_match.call_args.kwargs
        self.assertEqual(kwargs["receiving_account_number"], REAL_ACCT)
        self.assertEqual(kwargs["receiving_routing_number"], REAL_RTN)
        self.assertEqual(kwargs["receiving_bank_name"], "Wells Fargo")
        body = resp.get_data(as_text=True)
        self.assertNotIn(REAL_ACCT, body)
        self.assertNotIn(REAL_RTN, body)
        self.assertEqual(resp.get_json()["request_id"], "TXN-2026-001")

    def test_20b_unclaimed_but_matching_internal_instructions_are_still_blocked_not_bypassed(self):
        match = {"transaction_key": 42, "request_id": "TXN-2026-001", "last_used_date": "2026-01-01"}
        resp, m = self._post(_form(), summary=_summary(), prior_match=match)
        self.assertEqual(resp.status_code, 200)
        m["insert_transaction"].assert_not_called()
        self.assertIn("already used on a completed transaction", resp.get_data(as_text=True))

    def test_20c_previously_used_claim_still_needs_matching_acknowledgment(self):
        match = {"transaction_key": 42, "request_id": "TXN-2026-001", "last_used_date": "2026-01-01"}
        form = _form(instructions_previously_used="yes", last_used_date="2026-01-01")
        resp, m = self._post(form, summary=_summary(), prior_match=match)
        self.assertEqual(resp.status_code, 200)
        m["insert_transaction"].assert_not_called()
        resp, m = self._post(dict(form, prior_use_ack="TXN-2026-001"), summary=_summary(), prior_match=match)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(m["insert_transaction"].call_args[0][0]["prior_transaction_key"], 42)

    def test_21_avs_and_verbal_confirmation_requirements_unchanged(self):
        form = _form()
        for key in ("verbal_confirmed_with_known", "verbal_contact_name", "verbal_confirm_datetime"):
            form.pop(key)
        resp, m = self._post(form, summary=_summary())
        self.assertEqual(resp.status_code, 200)
        m["insert_transaction"].assert_not_called()
        self.assertIn("Verbal confirmation with a known contact is required", resp.get_data(as_text=True))

    def test_22_attachment_requirements_unchanged_for_external_recipients_and_payment_support(self):
        resp, m = self._post(_external_form(), files=_files(file_wire_ach_instructions=True))
        self.assertEqual(resp.status_code, 200)
        m["insert_transaction"].assert_not_called()
        self.assertIn("External ACH/Wire Instructions attachment is required", resp.get_data(as_text=True))
        resp, m = self._post(_form(), files=_files(file_payment_support=True), summary=_summary())
        self.assertEqual(resp.status_code, 200)
        m["insert_transaction"].assert_not_called()
        self.assertIn("Payment Support attachment is required", resp.get_data(as_text=True))

    def test_23_workflow_writes_are_identical_for_internal_and_external(self):
        def run(data, bank_row):
            cur = _FakeCursor(bank_row=bank_row)
            with patch.object(db, "get_connection", return_value=_conn(cur)), \
                 patch.object(db, "resolve_approval_rule", return_value=_RULE), \
                 patch.object(db, "resolve_entity_key", return_value=3):
                db.insert_transaction(dict(data))
            targets = []
            for sql, _ in cur.executed:
                if sql.lstrip().startswith(("INSERT INTO", "UPDATE")):
                    targets.append(sql.split("]")[1].strip(" .[") if "]" in sql else sql[:40])
            events = [p[3] for s, p in cur.executed if "[etransactions].[WorkflowEvent]" in s and p]
            status = [p for s, p in cur.executed if "INSERT INTO [etransactions].[ETransaction]" in s][0]
            return targets, events, status

        ext = dict(_INSERT_DATA, recipient_bank_account_key=None, recv_bank_name="Chase", recv_account_name="A",
                   recv_account_number="12345678", recv_routing_number="021000021")
        ext_targets, ext_events, ext_params = run(ext, None)
        int_targets, int_events, int_params = run(_INSERT_DATA, _bank_row())
        self.assertEqual(int_targets, ext_targets)
        self.assertEqual(int_events, ext_events)
        self.assertIn(wf.STATUS_PENDING_APPROVER, int_params)
        self.assertIn(7, int_params)  # same ApprovalRule_Key resolution

    def test_23b_approval_rule_resolution_and_routing_inputs_unchanged_in_route(self):
        resp, m = self._post(_form(), summary=_summary())
        self.assertEqual(resp.status_code, 302)
        data = m["insert_transaction"].call_args[0][0]
        self.assertEqual(data["approver_key"], 2)
        self.assertEqual(data["controller_key"], 3)
        self.assertEqual(data["status"], wf.STATUS_PENDING_APPROVER)
        m["resolve_approval_rule"].assert_called_once_with(1000.0)


# ── 24. Detail page / Transaction 15 compatibility ───────────────────────────

class DetailPageTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as sess:
            sess["role"] = "sam"

    def _record(self, **overrides):
        rec = {
            "request_id": "TXN-2026-7253", "status": "Pending Approver", "property_dept": "Test Prop",
            "property_code": "", "request_type": "Intra Bank Transfer", "treasury_service_date": "2026-01-01",
            "prepared_date": "2026-01-01", "submitted_date": "2026-01-01", "amount": 1000.0,
            "currency": "USD", "payment_purpose": "Test", "urgent": False, "urgency_reason": "",
            "current_workflow_stage": "Approver", "approval_tier": "Senior Accounting Manager / Assistant Controller",
            "requires_vp": False, "over_1m": False, "days_pending": 0,
            "prepared_by": "A", "assigned_approver": "B", "approver": "B", "controller": "C",
            "vp_approver": "", "cfo_approver": "",
            "orig_bank_name": "Wells Fargo", "orig_account_name": "Ops",
            "orig_account_number": "4521039812", "orig_routing_number": "121000248",
            "orig_bank_contact": "", "notes_orig": "",
            "recv_payee_name": "Sunset Ridge Operating LLC", "recv_contact_name": "", "recv_contact_email": "",
            "recv_contact_phone": "", "recv_bank_name": "Wells Fargo", "recv_account_name": "Sunset Ridge Operating",
            "recv_account_number": REAL_ACCT, "recv_routing_number": REAL_RTN,
            "recv_bank_address": "", "notes_recv": "",
            "verbal_confirmed": False, "verbal_confirmed_with": "", "verbal_contact_name": "",
            "verbal_confirm_datetime": "", "avs_score": "", "external_source": False, "internal_doc_not_used": False,
            "instructions_previously_used": False, "last_used_date": "",
            "entity_classification": "Corporate", "current_owner_user_key": 2, "bank_releaser_user_key": None,
            "docs_checklist": {}, "attachments": {}, "extra_attachments": [], "timeline": [], "comments": [],
        }
        rec.update(overrides)
        return rec

    def _render(self, record):
        with patch.object(app_module.db, "get_transaction_for_workflow",
                          return_value={"status": "Pending Approver", "accounting_group_key": None}), \
             patch.object(app_module.db, "get_request_detail", return_value=record), \
             patch.object(app_module, "sharepoint_enabled", return_value=False):
            return self.client.get("/dashboard/request/TXN-2026-7253")

    def test_24_legacy_transaction_without_recipient_fields_renders_as_external(self):
        # Transaction 15 (and all 12 existing rows) have RecipientBankAccount_Key NULL; a record
        # lacking the new keys entirely must also render.
        resp = self._render(self._record())
        body = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("External Payee", body)
        self.assertNotIn("Internal Company Account", body)

    def test_24b_internal_transaction_shows_type_entity_and_only_masked_numbers(self):
        resp = self._render(self._record(recipient_mode="internal", recipient_bank_account_key=9,
                                          recipient_entity_name="Sunset Ridge Apartments",
                                          recipient_classification="Property"))
        body = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Internal Company Account", body)
        self.assertIn("Sunset Ridge Apartments", body)
        self.assertIn("Wells Fargo", body)
        self.assertIn("********2233", body)  # masked, last 4 only
        self.assertNotIn(REAL_ACCT, body)
        self.assertNotIn(REAL_RTN, body)
        self.assertIn("reveal-banking/receiving", body)  # existing authorized reveal retained

    def test_24c_get_request_detail_derives_mode_from_the_persisted_key(self):
        sql = db._DETAIL_SQL
        self.assertIn("RecipientBankAccount_Key", sql)
        self.assertNotIn("recipient_mode", sql)  # derived in Python from the key, never stored


# ── 26. Suite isolation ──────────────────────────────────────────────────────

class IsolationTests(unittest.TestCase):
    def test_26_unpatched_db_helpers_hit_only_the_inert_guard(self):
        # With no patches, the guard's empty in-memory database answers: nothing found,
        # nothing written, no network/driver involved.
        self.assertIsNone(db.get_internal_recipient_summary(9))
        self.assertEqual(db.get_draft_recipient_bank_account_key(1, 1), (False, None))
        with self.assertRaises(db.InternalRecipientError):
            db.get_internal_recipient_instruction_values(9)


if __name__ == "__main__":
    unittest.main()
