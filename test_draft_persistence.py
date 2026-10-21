"""
Tests for the Draft persistence fix: Save Draft -> leave -> Resume must restore everything the
requester entered or uploaded.

Covers the verification section (previously used, prior-use acknowledgment, verbal confirmation,
AVS score, source flags) which was never saved with a Draft, stored-attachment recognition and
display (including AVS Screenshot), upload-failure reporting, the ownership-derived Request_ID,
and keeping the Draft's identity when a save/submit is re-rendered with errors.

Mock-only: `external_guard` (imported first) makes SQL/SharePoint/network inert; the tests also
replace the Draft db functions and SharePoint calls with small in-memory fakes.

Run:  python -m unittest test_draft_persistence -v
"""
import io
import re
import unittest
import external_guard  # noqa: F401  (must precede app/db/sharepoint imports)
from contextlib import ExitStack
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

import app as app_module
import db
import sharepoint
import test_intake_precheck as pre
import test_internal_recipient as ir

USER = {"user_key": 11, "display_name": "Owner", "email": ""}
DRAFT_RID = "TXN-2026-D055"
BANK_ACCOUNTS = [
    {"bank_account_key": 5, "bank_name": "Wells Fargo", "account_title": "Operating", "account_last4": "1111",
     "entity_name": "Acme", "classification": "Corporate"},
    {"bank_account_key": 9, "bank_name": "Wells Fargo", "account_title": "Payroll", "account_last4": "2233",
     "entity_name": "Acme", "classification": "Corporate"},
]
CANDIDATES = [{"user_key": 2, "display_name": "Alice Approver", "email": "", "team": "CA"},
              {"user_key": 3, "display_name": "Carl Controller", "email": "", "team": "CA"}]


# ── tiny HTML helpers ─────────────────────────────────────────────────────────
def _tag(body, element_id):
    for attr in ("id", "name"):
        m = re.search(r'<(?:input|textarea|select)\b[^>]*\b%s="%s"[^>]*>' % (attr, re.escape(element_id)), body, re.S)
        if m:
            return m.group(0)
    raise AssertionError(f"no element with id/name {element_id}")


def _value(body, element_id):
    tag = _tag(body, element_id)
    if tag.startswith("<textarea"):
        return re.search(r"<textarea[^>]*>(.*?)</textarea>", body[body.index(tag):], re.S).group(1)
    m = re.search(r'\bvalue="([^"]*)"', tag)
    return m.group(1) if m else ""


def _checked(body, element_id):
    return re.search(r"\bchecked\b", _tag(body, element_id)) is not None


def _selected_option(body, select_id):
    start = body.index(_tag(body, select_id))
    block = body[start: body.index("</select>", start)]
    m = re.search(r'<option value="([^"]*)"[^>]*\bselected\b', block)
    return m.group(1) if m else ""


# ── in-memory fakes ───────────────────────────────────────────────────────────
class FakeDraftStore:
    """Stands in for the Draft SQL functions, storing exactly what the app hands to db."""

    def __init__(self):
        self.drafts, self.next_key = {}, 55
        self.created, self.updated = [], []

    def create_draft(self, data):
        key, self.next_key = self.next_key, self.next_key + 1
        self.drafts[key] = dict(data, request_id=f"TXN-2026-D{key:03d}")
        self.created.append(key)
        return self.drafts[key]["request_id"], key

    def update_draft(self, key, data, *, prepared_by_user_key):
        self.drafts[key].update(data)
        self.updated.append(key)

    def get_draft_request_id(self, key, user_key):
        draft = self.drafts.get(int(key))
        return draft["request_id"] if draft else None

    def get_draft_for_edit(self, key, *, prepared_by_user_key):
        d = self.drafts.get(int(key))
        if d is None:
            return None
        verbal = d.get("verbal_confirm_datetime") or ""
        return {
            "transaction_key": key, "request_id": d["request_id"],
            "property_dept": d.get("property_dept", ""), "property_code": d.get("property_code", ""),
            "classification": (d.get("classification") or "").lower(), "property_entity_number": None,
            "bank_account_key": d["bank_account_key"], "approver_key": d["approver_key"],
            "controller_key": d["controller_key"], "request_type": d["request_type"],
            "treasury_service_date": d["treasury_service_date"], "amount": d["amount"],
            "currency": d.get("currency", "USD"), "payment_purpose": d.get("payment_purpose", ""),
            "urgent": d["urgent"], "urgency_reason": d["urgency_reason"], "prepared_date": "2026-10-14",
            "recv_payee_name": d["recv_payee_name"], "recv_contact_name": d.get("recv_contact_name", ""),
            "recv_contact_email": d.get("recv_contact_email", ""), "recv_contact_phone": d.get("recv_contact_phone", ""),
            "recv_bank_name": d.get("recv_bank_name", ""), "recv_account_name": d.get("recv_account_name", ""),
            "recv_bank_address": d.get("recv_bank_address", ""),
            "recv_account_number_masked": "****" + (d.get("recv_account_number") or "")[-4:],
            "recv_routing_number_masked": "*****" + (d.get("recv_routing_number") or "")[-4:],
            "originating_bank_account": {"bank_name": "Wells Fargo", "account_title": "Operating", "last4": "1111"},
            "recipient_bank_account_key": d.get("recipient_bank_account_key"),
            "recipient_mode": "internal" if d.get("recipient_bank_account_key") else "external",
            "recipient_account": (ir._summary(key=d["recipient_bank_account_key"])
                                  if d.get("recipient_bank_account_key") else None),
            "instructions_previously_used": d.get("instructions_previously_used", False),
            "last_used_date": d.get("last_used_date") or "",
            "prior_request_id": "TXN-2026-001" if d.get("prior_transaction_key") == 42 else "",
            "verbal_confirmed": d.get("verbal_confirmed", False),
            "verbal_known_contact": d.get("verbal_known_contact", False),
            "verbal_requester": d.get("verbal_requester", False),
            "verbal_contact_name": d.get("verbal_contact_name", ""),
            "verbal_confirm_datetime": verbal[:16].replace(" ", "T"),
            "avs_score": d.get("avs_score", ""), "external_source": d.get("external_source", False),
            "internal_doc_not_used": d.get("internal_doc_not_used", False),
        }


class FakeSharePoint:
    def __init__(self):
        self.items, self.uploads, self.retags, self.fail_for = [], [], [], set()

    def upload_attachment(self, request_id, file_storage, *, section, doc_type, uploaded_by_role, **kw):
        if file_storage.filename in self.fail_for:
            raise RuntimeError("Graph error")
        file_storage.read()
        self.uploads.append((request_id, file_storage.filename, doc_type))
        self.items.append({
            "request_id": request_id, "filename": f"{doc_type} - {file_storage.filename}",
            "original_filename": file_storage.filename, "doc_type": doc_type,
            "web_url": f"https://sp.example/{request_id}/{file_storage.filename}",
            "uploaded_date": f"2026-10-14T00:00:{len(self.items):02d}Z",
        })

    def list_attachments(self, request_id):
        return [dict(i) for i in self.items if i["request_id"] == request_id]

    def retag_validation_evidence(self, request_id, new_doc_type):
        self.retags.append((request_id, new_doc_type))
        for item in self.items:
            if item["request_id"] == request_id and item["doc_type"] in (
                    sharepoint.DOC_TYPE_VALIDATION_EVIDENCE, sharepoint.DOC_TYPE_AVS_SCREENSHOT):
                item["doc_type"] = new_doc_type
                return True
        return False

    def seed(self, request_id, **docs):
        for slot, (name, doc_type) in docs.items():
            self.upload_attachment(request_id, MagicMock(filename=name, read=lambda: b"x"),
                                   section="s", doc_type=doc_type, uploaded_by_role="r")
        self.uploads.clear()


def _draft_form(**overrides):
    """A complete, valid EXTERNAL ACH Draft as the browser posts it (verification included)."""
    data = ir._external_form(
        form_mode="draft", currency="USD", recv_bank_address="1 Main St", recv_account_name="Acme Ops",
        recv_contact_name="Jane Contact", recv_contact_email="jane@example.com", recv_contact_phone="555-000-1111",
        verbal_confirmed="on", verbal_confirmed_with_known="on", verbal_confirmed_with_requester="on",
        verbal_contact_name="John Doe", verbal_confirm_datetime="2026-09-18T10:30", avs_score="95",
        external_source="on", internal_doc_not_used="on",
        instructions_previously_used="", last_used_date="", prior_use_ack="",
        urgent="yes", urgency_reason="Vendor deadline", payment_purpose="Rent",
    )
    data.update(overrides)
    return data


class _Base(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as sess:
            sess["role"] = "submitter"
        self.store, self.sp = FakeDraftStore(), FakeSharePoint()
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.m = {}

        def p(target, name, **kw):
            self.m[name] = stack.enter_context(patch.object(target, name, **kw))
            return self.m[name]

        d = app_module.db
        p(app_module, "current_app_user", return_value=USER)
        p(d, "get_bank_account_status", return_value="Open")
        p(d, "get_app_user_by_key", return_value={"user_key": 2, "display_name": "A", "email": ""})
        p(d, "get_app_user_role_codes", return_value=["sam", "controller"])
        p(d, "get_reassignment_candidates", return_value=CANDIDATES)
        p(d, "get_user_list", return_value=[])
        p(d, "get_bank_accounts", return_value=BANK_ACCOUNTS)
        p(d, "get_internal_recipient_summary", return_value=ir._summary())
        p(d, "get_internal_recipient_instruction_values",
          return_value={"bank_name": "B", "account_name": "A", "account_number": ir.REAL_ACCT,
                        "routing_number": ir.REAL_RTN, "address": ""})
        p(d, "get_draft_recipient_bank_account_key", return_value=(True, None))
        p(d, "get_draft_beneficiary_instruction_key", return_value=77)
        p(d, "find_prior_completed_beneficiary_match", return_value=None)
        p(d, "resolve_approval_rule", return_value=ir._RULE)
        p(d, "create_draft", side_effect=self.store.create_draft)
        p(d, "update_draft", side_effect=self.store.update_draft)
        p(d, "get_draft_request_id", side_effect=self.store.get_draft_request_id)
        p(d, "get_draft_for_edit", side_effect=self.store.get_draft_for_edit)
        p(d, "finalize_draft_submission")
        p(d, "reserve_request_id", return_value="TXN-2026-9999")
        p(d, "insert_transaction", return_value="TXN-2026-9999")
        p(app_module.sharepoint, "upload_attachment", side_effect=self.sp.upload_attachment)
        p(app_module.sharepoint, "list_attachments", side_effect=self.sp.list_attachments)
        p(app_module.sharepoint, "retag_validation_evidence", side_effect=self.sp.retag_validation_evidence)

    def save(self, form=None, files=None):
        data = dict(form if form is not None else _draft_form())
        data.update(files or {})
        return self.client.post("/intake/submit", data=data, content_type="multipart/form-data")

    def resume(self, key=55):
        resp = self.client.get(f"/intake/draft/{key}")
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def flashes(self):
        with self.client.session_transaction() as sess:
            return [(cat, msg) for cat, msg in sess.get("_flashes", [])]


def _file(name):
    return (io.BytesIO(b"x"), name)


# ═══ SQL layer ════════════════════════════════════════════════════════════════
VERIFICATION = {
    "instructions_previously_used": True, "last_used_date": "2026-01-01", "prior_transaction_key": 42,
    "verbal_confirmed": True, "verbal_known_contact": True, "verbal_requester": False,
    "verbal_contact_name": "John Doe", "verbal_confirm_datetime": "2026-09-18 10:30:00",
    "avs_score": "95", "external_source": True, "internal_doc_not_used": False,
}
EXPECTED_VALUES = [1, "2026-01-01", 42, 1, 1, 0, "John Doe", "2026-09-18 10:30:00", "95", 1, 0]


def _tv_calls(cursor):
    return [c[0] for c in cursor.execute.call_args_list if "TransactionVerification" in c[0][0]]


class SqlLayerTests(unittest.TestCase):
    def _cursor(self, fetch):
        cur = MagicMock()
        cur.fetchone.side_effect = fetch
        cur.rowcount = 1
        conn = MagicMock()
        conn.cursor.return_value = cur
        return conn, cur

    def _base(self, **extra):
        return {"prepared_by_key": 11, "request_type": "ACH", "treasury_service_date": "2026-10-01",
                "amount": 1000.0, "bank_account_key": 5, "approver_key": 2, "controller_key": 3,
                "recv_payee_name": "Payee", "recv_bank_name": "Bank", "recv_account_number": "123",
                "request_id": "TXN-2026-D001", **extra}

    def test_create_draft_stores_the_verification_state_and_nothing_workflow(self):
        conn, cur = self._cursor([(10,), (20,), (99,)])
        with patch.object(db, "get_connection", return_value=conn):
            db.create_draft(self._base(**VERIFICATION))
        update, insert = _tv_calls(cur)
        self.assertIn("UPDATE [etransactions].[TransactionVerification]", update[0])
        self.assertEqual(update[1], EXPECTED_VALUES + [11, 99])
        self.assertIn("IF NOT EXISTS", insert[0])
        self.assertEqual(insert[1][:2], [99, 99])
        self.assertEqual(insert[1][2:13], EXPECTED_VALUES)
        all_sql = " ".join(c[0][0] for c in cur.execute.call_args_list)
        for forbidden in ("WorkflowEvent", "WorkflowAssignment"):
            self.assertNotIn(forbidden, all_sql)

    def test_update_draft_overwrites_the_stored_verification_so_edits_and_clears_persist(self):
        conn, cur = self._cursor([(10, 20)])
        cleared = dict(VERIFICATION, instructions_previously_used=False, last_used_date="", prior_transaction_key=None,
                       avs_score="", verbal_contact_name="")
        with patch.object(db, "get_connection", return_value=conn):
            db.update_draft(99, self._base(**cleared), prepared_by_user_key=11)
        update = _tv_calls(cur)[0]
        self.assertEqual(update[1][:3], [0, None, None])  # blank dates/keys are NULL, never ""
        self.assertIsNone(update[1][8])  # AVS cleared -> NULL
        self.assertNotIn("WorkflowEvent", " ".join(c[0][0] for c in cur.execute.call_args_list))

    def test_drafts_without_verification_keys_touch_no_verification_row(self):
        conn, cur = self._cursor([(10,), (20,), (99,)])
        with patch.object(db, "get_connection", return_value=conn):
            db.create_draft(self._base())
        self.assertEqual(_tv_calls(cur), [])

    def test_finalize_keeps_exactly_one_verification_row(self):
        conn, cur = self._cursor([(3,), (10, 20), (77,)])
        data = {"approver_key": 2, "controller_key": 3, "bank_account_key": 5, "request_type": "ACH",
                "classification": "corporate", "property_dept": "", "property_code": "",
                "treasury_service_date": "2026-10-01", "amount": 1000.0, "recv_payee_name": "P",
                "recv_bank_name": "B", "approval_rule_key": 7, "requires_vp": False, "requires_cfo": False,
                "approval_tier": "t", **VERIFICATION}
        with patch.object(db, "get_connection", return_value=conn):
            db.finalize_draft_submission(99, data, prepared_by_user_key=11)
        update, insert = _tv_calls(cur)
        self.assertIn("UPDATE", update[0])                        # overwrites the Draft's saved row
        self.assertIn("IF NOT EXISTS", insert[0])                  # inserts only when none exists
        self.assertEqual(sum("INSERT INTO [etransactions].[TransactionVerification]" in c[0] for c in _tv_calls(cur)), 1)

    def _row(self, **over):
        values = {
            "Transaction_Key": 99, "Request_ID": "TXN-2026-D099", "Property_Department_Text": "Dept",
            "Entity_ID_Text": "E1", "OriginatingBankAccount_Key": 5, "RecipientBankAccount_Key": None,
            "SelectedApprover_User_Key": 2, "SelectedController_User_Key": 3, "Request_Type": "ACH",
            "Treasury_Service_Date": date(2026, 10, 1), "Amount": Decimal("1000.00"), "Currency": "USD",
            "Payment_Purpose": "Rent", "Urgent_Flag": 1, "Urgency_Reason": "Vendor deadline",
            "Prepared_Date": date(2026, 9, 18), "Payee_Name": "Acme", "Contact_Name": "Jane",
            "Contact_Email": "j@example.com", "Contact_Phone": "555", "Receiving_Bank_Name": "Chase",
            "Receiving_Account_Name": "Ops", "Receiving_Account_Number": ir.REAL_ACCT,
            "Receiving_Routing_Number": ir.REAL_RTN, "Bank_Beneficiary_Address": "1 Main St",
            "Classification": "Corporate", "Entity_ID": "CORP-001",
            "Instructions_Previously_Used": 1, "Last_Used_Date": date(2026, 1, 1),
            "Prior_Request_ID": "TXN-2026-001", "Verbal_Confirmed": 1, "Confirmed_With_KnownContact_Flag": 1,
            "Confirmed_With_Requester_Flag": 0, "Verbal_Contact_Name": "John Doe",
            "Verbal_Confirm_DateTime": datetime(2026, 9, 18, 10, 30), "AVS_Score": Decimal("95.00"),
            "External_Source_Flag": 1, "Internal_Doc_Not_Used_Flag": 0,
        }
        values.update(over)
        return values

    def _read(self, **over):
        row = self._row(**over)
        cur = MagicMock()
        cur.description = [(name,) for name in row]
        cur.fetchone.return_value = tuple(row.values())
        conn = MagicMock()
        conn.cursor.return_value = cur
        with patch.object(db, "get_connection", return_value=conn), \
             patch.object(db, "_draft_originating_bank_account_summary", return_value=None), \
             patch.object(db, "get_internal_recipient_summary", return_value=None):
            return db.get_draft_for_edit(99, prepared_by_user_key=11), cur

    def test_resume_query_returns_the_saved_verification_in_form_formats(self):
        draft, cur = self._read()
        self.assertEqual(draft["request_id"], "TXN-2026-D099")  # not overwritten by the prior transaction's id
        self.assertEqual(draft["prior_request_id"], "TXN-2026-001")
        self.assertTrue(draft["instructions_previously_used"])
        self.assertEqual(draft["last_used_date"], "2026-01-01")
        self.assertEqual(draft["verbal_confirm_datetime"], "2026-09-18T10:30")   # datetime-local input format
        self.assertEqual(draft["avs_score"], "95")
        self.assertEqual((draft["verbal_confirmed"], draft["verbal_known_contact"], draft["verbal_requester"]),
                         (True, True, False))
        self.assertEqual((draft["external_source"], draft["internal_doc_not_used"]), (True, False))
        sql = cur.execute.call_args[0][0]
        self.assertIn("Prior_Request_ID", sql)
        self.assertIn("PreparedBy_User_Key = ?", sql)  # still owner-guarded

    def test_resume_query_blank_verification_and_never_returns_raw_banking_values(self):
        draft, _ = self._read(
            Instructions_Previously_Used=None, Last_Used_Date=None, Prior_Request_ID=None, Verbal_Confirmed=None,
            Confirmed_With_KnownContact_Flag=None, Confirmed_With_Requester_Flag=None, Verbal_Contact_Name=None,
            Verbal_Confirm_DateTime=None, AVS_Score=None, External_Source_Flag=None, Internal_Doc_Not_Used_Flag=None)
        self.assertEqual((draft["last_used_date"], draft["verbal_confirm_datetime"], draft["avs_score"]), ("", "", ""))
        self.assertFalse(draft["instructions_previously_used"])
        self.assertNotIn(ir.REAL_ACCT, repr(draft))
        self.assertNotIn(ir.REAL_RTN, repr(draft))

    def test_avs_score_text(self):
        self.assertEqual([db._avs_score_text(v) for v in (None, Decimal("90.00"), Decimal("0"), Decimal("87.5"))],
                         ["", "90", "0", "87.5"])

    def test_draft_request_id_lookup_is_owner_and_draft_guarded(self):
        conn, cur = self._cursor([("TXN-2026-D099",), None])
        with patch.object(db, "get_connection", return_value=conn):
            self.assertEqual(db.get_draft_request_id(99, 11), "TXN-2026-D099")
            self.assertIsNone(db.get_draft_request_id(99, 12))
        sql, params = cur.execute.call_args_list[0][0]
        self.assertIn("PreparedBy_User_Key = ?", sql)
        self.assertIn("Current_Status = ?", sql)
        self.assertEqual(params[:2], [99, 11])


# ═══ Save Draft -> Resume ═════════════════════════════════════════════════════
class SaveAndResumeFieldTests(_Base):
    def test_1_2_3_every_entered_field_round_trips(self):
        resp = self.save(files={"file_payment_support": _file("inv.pdf")})
        self.assertEqual(resp.status_code, 302)
        data = self.store.drafts[55]
        for key, expected in (("request_type", "ACH"), ("treasury_service_date", "2026-10-01"), ("currency", "USD"),
                              ("payment_purpose", "Rent"), ("urgent", True), ("urgency_reason", "Vendor deadline"),
                              ("approver_key", 2), ("controller_key", 3), ("bank_account_key", 5),
                              ("recv_payee_name", "Sunset Ridge Operating LLC"), ("amount", 1000.0)):
            self.assertEqual(data[key], expected)
        body = self.resume()
        self.assertEqual(_value(body, "treasury_service_date"), "2026-10-01")
        self.assertEqual(_value(body, "payment_purpose"), "Rent")
        self.assertEqual(float(_value(body, "amount")), 1000.0)
        self.assertTrue(_checked(body, "urgent_yes"))
        self.assertEqual(_value(body, "urgency_reason"), "Vendor deadline")
        self.assertEqual(_selected_option(body, "request_type"), "ACH")
        self.assertEqual(_selected_option(body, "approver"), "2")
        self.assertEqual(_selected_option(body, "controller"), "3")
        self.assertEqual(_value(body, "bank_account_key"), "5")
        self.assertIn("Currently selected", body)
        self.assertEqual(_value(body, "transaction_key"), "55")
        self.assertEqual(_value(body, "request_id"), self.store.drafts[55]["request_id"])

    def test_external_recipient_fields_restore_without_exposing_full_numbers(self):
        self.save()
        body = self.resume()
        for field, expected in (("recv_payee_name", "Sunset Ridge Operating LLC"), ("recv_bank_name", "Chase Bank"),
                                ("recv_account_name", "Acme Ops"), ("recv_bank_address", "1 Main St"),
                                ("recv_contact_name", "Jane Contact"), ("recv_contact_email", "jane@example.com"),
                                ("recv_contact_phone", "555-000-1111")):
            self.assertEqual(_value(body, field), expected, field)
        self.assertTrue(_checked(body, "recipient_mode_external"))
        self.assertEqual(_value(body, "recv_account_number"), "")  # blank = keep stored value
        self.assertIn("****5678", body)
        self.assertNotIn("12345678", body)
        self.assertNotIn("021000021", body)

    def test_verification_section_round_trips(self):
        self.save()
        body = self.resume()
        self.assertTrue(_checked(body, "verbal_confirmed"))
        self.assertTrue(_checked(body, "verbal_with_known"))
        self.assertTrue(_checked(body, "verbal_with_requester"))
        self.assertEqual(_value(body, "verbal_contact_name"), "John Doe")
        self.assertEqual(_value(body, "verbal_confirm_datetime"), "2026-09-18T10:30")
        self.assertEqual(_value(body, "avs_score"), "95")
        self.assertTrue(_checked(body, "external_source"))
        self.assertTrue(_checked(body, "internal_doc_not_used"))
        self.assertFalse(_checked(body, "instructions_previously_used"))

    def test_18_previously_used_fields_and_acknowledgment_round_trip(self):
        match = {"transaction_key": 42, "request_id": "TXN-2026-001", "last_used_date": "2026-01-01"}
        self.m["find_prior_completed_beneficiary_match"].return_value = match
        self.save(_draft_form(instructions_previously_used="yes", last_used_date="2026-01-01",
                              prior_use_ack="TXN-2026-001"))
        self.assertEqual(self.store.drafts[55]["prior_transaction_key"], 42)
        body = self.resume()
        self.assertTrue(_checked(body, "instructions_previously_used"))
        self.assertEqual(_value(body, "last_used_date"), "2026-01-01")
        self.assertEqual(_value(body, "prior_use_ack"), "TXN-2026-001")

    def test_18b_a_forged_or_stale_acknowledgment_is_never_stored(self):
        self.m["find_prior_completed_beneficiary_match"].return_value = {
            "transaction_key": 42, "request_id": "TXN-2026-001", "last_used_date": "2026-01-01"}
        self.save(_draft_form(instructions_previously_used="yes", last_used_date="2026-01-01",
                              prior_use_ack="TXN-FORGED-999"))
        self.assertIsNone(self.store.drafts[55]["prior_transaction_key"])
        self.assertEqual(_value(self.resume(), "prior_use_ack"), "")
        self.m["find_prior_completed_beneficiary_match"].return_value = None  # no current match at all
        self.save(_draft_form(transaction_key="55", request_id="x", instructions_previously_used="yes",
                              last_used_date="2026-01-01", prior_use_ack="TXN-2026-001"))
        self.assertIsNone(self.store.drafts[55]["prior_transaction_key"])

    def test_19_date_fields_round_trip_in_the_inputs_formats(self):
        self.save(_draft_form(instructions_previously_used="yes", last_used_date="2026-02-03",
                              verbal_confirm_datetime="2026-09-18T07:05", treasury_service_date="2026-11-30"))
        self.assertEqual(self.store.drafts[55]["verbal_confirm_datetime"], "2026-09-18 07:05:00")
        body = self.resume()
        self.assertEqual(_value(body, "treasury_service_date"), "2026-11-30")
        self.assertEqual(_value(body, "last_used_date"), "2026-02-03")
        self.assertEqual(_value(body, "verbal_confirm_datetime"), "2026-09-18T07:05")

    def test_4_5_edit_and_save_keeps_unchanged_values_and_applies_changes(self):
        self.save()
        form = _draft_form(transaction_key="55", request_id=self.store.drafts[55]["request_id"],
                           payment_purpose="Rent (revised)", avs_score="80", verbal_contact_name="Jane Roe")
        self.assertEqual(self.save(form).status_code, 302)
        self.assertEqual(self.store.updated, [55])
        self.assertEqual(self.store.created, [55])  # same Draft, never a second one
        body = self.resume()
        self.assertEqual(_value(body, "payment_purpose"), "Rent (revised)")
        self.assertEqual(_value(body, "avs_score"), "80")
        self.assertEqual(_value(body, "verbal_contact_name"), "Jane Roe")
        self.assertEqual(_value(body, "recv_bank_name"), "Chase Bank")          # untouched
        self.assertEqual(_value(body, "verbal_confirm_datetime"), "2026-09-18T10:30")  # untouched
        self.assertEqual(_value(body, "urgency_reason"), "Vendor deadline")           # untouched

    def test_multiple_consecutive_save_resume_cycles_never_lose_or_duplicate(self):
        self.save()
        for cycle in range(4):
            body = self.resume()
            form = _draft_form(
                transaction_key=_value(body, "transaction_key"), request_id=_value(body, "request_id"),
                payment_purpose=_value(body, "payment_purpose"), avs_score=_value(body, "avs_score"),
                verbal_contact_name=_value(body, "verbal_contact_name"),
                verbal_confirm_datetime=_value(body, "verbal_confirm_datetime"),
                treasury_service_date=_value(body, "treasury_service_date"),
                urgency_reason=_value(body, "urgency_reason"),
                recv_payee_name=_value(body, "recv_payee_name"), recv_bank_name=_value(body, "recv_bank_name"),
                recv_account_number="", recv_routing_number="")
            self.assertEqual(self.save(form).status_code, 302, cycle)
        self.assertEqual(len(self.store.drafts), 1)
        final = self.resume()
        self.assertEqual(_value(final, "avs_score"), "95")
        self.assertEqual(_value(final, "verbal_confirm_datetime"), "2026-09-18T10:30")
        self.assertEqual(_value(final, "payment_purpose"), "Rent")

    def test_6_an_incomplete_draft_with_no_verification_or_documents_still_saves(self):
        sparse = _draft_form(verbal_confirmed="", verbal_confirmed_with_known="", verbal_confirmed_with_requester="",
                             verbal_contact_name="", verbal_confirm_datetime="", avs_score="", external_source="",
                             internal_doc_not_used="", urgent="no", urgency_reason="", payment_purpose="")
        self.assertEqual(self.save(sparse).status_code, 302)
        self.assertEqual(self.sp.uploads, [])
        body = self.resume()
        self.assertEqual(_value(body, "avs_score"), "")
        self.assertFalse(_checked(body, "verbal_confirmed"))

    def test_malformed_verification_input_is_rejected_not_silently_dropped(self):
        for field, bad in (("avs_score", "abc"), ("avs_score", "150"), ("last_used_date", "13/45/2026"),
                           ("verbal_confirm_datetime", "yesterday")):
            with self.subTest(field=field, bad=bad):
                resp = self.save(_draft_form(**{field: bad}))
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(self.store.created, [])

    def test_20_approver_and_controller_selections_persist(self):
        # Unclassified: a Corporate draft always gets the automatic Corporate Controller instead of a picked one.
        self.save(_draft_form(approver_key="3", controller_key="2", classification=""))
        body = self.resume()
        self.assertEqual(_selected_option(body, "approver"), "3")
        self.assertEqual(_selected_option(body, "controller"), "2")

    def test_14_same_day_ach_draft_keeps_its_automatic_urgency(self):
        self.save(_draft_form(request_type="Same Day ACH", urgent="no", urgency_reason="forged"))
        body = self.resume()
        self.assertEqual(_selected_option(body, "request_type"), "Same Day ACH")
        self.assertTrue(_checked(body, "urgent_yes"))
        self.assertEqual(_value(body, "urgency_reason"), "Same Day ACH")

    def test_15_regular_ach_draft_keeps_its_manual_urgency(self):
        self.save(_draft_form(urgent="yes", urgency_reason="Penalty avoidance"))
        body = self.resume()
        self.assertEqual(_selected_option(body, "request_type"), "ACH")
        self.assertTrue(_checked(body, "urgent_yes"))
        self.assertEqual(_value(body, "urgency_reason"), "Penalty avoidance")
        self.save(_draft_form(transaction_key="55", request_id="x", urgent="no", urgency_reason=""))
        self.assertFalse(_checked(self.resume(), "urgent_yes"))

    def test_16_21_internal_recipient_restores_safely(self):
        form = ir._form(form_mode="draft", recv_payee_name="Payroll LLC", verbal_confirmed_with_known="on")
        self.assertEqual(self.save(form).status_code, 302)
        self.assertEqual(self.store.drafts[55]["recipient_bank_account_key"], 9)
        body = self.resume()
        self.assertTrue(_checked(body, "recipient_mode_internal"))
        self.assertEqual(_value(body, "recipient_bank_account_key"), "9")
        self.assertEqual(_value(body, "recv_payee_name"), "Payroll LLC")
        self.assertIn("recipient-account-draft-summary", body)
        for secret in (ir.REAL_ACCT, ir.REAL_RTN):
            self.assertNotIn(secret, body)

    def test_resume_of_another_users_or_missing_draft_returns_nothing(self):
        self.client.get("/intake/draft/999", follow_redirects=False)
        resp = self.client.get("/intake/draft/999")
        self.assertEqual(resp.status_code, 302)


# ═══ Attachments ══════════════════════════════════════════════════════════════
class AttachmentTests(_Base):
    def test_7_8_9_uploaded_documents_are_stored_under_the_drafts_request_id_and_shown_on_resume(self):
        resp = self.save(files={"file_validation_evidence": _file("avs.png"), "file_wire_ach_instructions": _file("w.pdf"),
                                "file_payment_support": _file("inv.pdf")})
        self.assertEqual(resp.status_code, 302)
        rid = self.store.drafts[55]["request_id"]
        self.assertEqual({(u[0], u[1]) for u in self.sp.uploads}, {(rid, "avs.png"), (rid, "w.pdf"), (rid, "inv.pdf")})
        self.assertEqual(self.flashes(), [("success", "Draft saved.")])
        body = self.resume()
        for name in ("avs.png", "w.pdf", "inv.pdf"):
            self.assertIn(f"<strong>{name}</strong>", body)
            self.assertIn(f"https://sp.example/{rid}/{name}", body)
        self.assertEqual(body.count("Existing attachment:"), 3)
        self.assertEqual(body.count('data-existing="true"'), 3)
        self.assertEqual(body.count("View Attachment"), 3)

    def test_avs_screenshot_tagged_evidence_is_recognized_as_the_validation_evidence(self):
        self.save(files={"file_validation_evidence": _file("avs.png")})   # avs 95 -> AVS Screenshot
        self.assertEqual(self.sp.uploads[0][2], sharepoint.DOC_TYPE_AVS_SCREENSHOT)
        body = self.resume()
        self.assertIn("<strong>avs.png</strong>", body)
        self.assertEqual(_tag(body, "file_validation_evidence").count('data-existing="true"'), 1)

    def test_no_attachment_indicator_when_nothing_is_stored(self):
        self.save()
        body = self.resume()
        self.assertNotIn("Existing attachment:", body)
        self.assertNotIn('data-existing="true"', body)

    def test_12_a_resave_never_reuploads_stored_files(self):
        self.save(files={"file_payment_support": _file("inv.pdf")})
        key_form = _draft_form(transaction_key="55", request_id=self.store.drafts[55]["request_id"])
        self.save(key_form)
        self.save(key_form)
        self.assertEqual(len(self.sp.uploads), 1)
        self.assertEqual(len([i for i in self.sp.items]), 1)

    def test_only_the_newly_selected_replacement_is_uploaded(self):
        self.save(files={"file_payment_support": _file("inv.pdf"), "file_wire_ach_instructions": _file("w.pdf")})
        self.save(_draft_form(transaction_key="55", request_id="x"), files={"file_payment_support": _file("inv2.pdf")})
        self.assertEqual([u[1] for u in self.sp.uploads], ["w.pdf", "inv.pdf", "inv2.pdf"])
        body = self.resume()
        self.assertIn("<strong>inv2.pdf</strong>", body)   # latest document is the one shown
        self.assertNotIn("<strong>inv.pdf</strong>", body)

    def test_13_avs_classification_follows_the_score_and_is_retagged_without_reupload(self):
        self.save(_draft_form(avs_score=""), files={"file_validation_evidence": _file("ev.pdf")})
        rid = self.store.drafts[55]["request_id"]
        self.assertEqual(self.sp.uploads[0][2], sharepoint.DOC_TYPE_VALIDATION_EVIDENCE)   # not yet an AVS screenshot
        self.save(_draft_form(transaction_key="55", request_id=rid, avs_score="95"))
        self.assertEqual(self.sp.retags[-1], (rid, sharepoint.DOC_TYPE_AVS_SCREENSHOT))
        self.assertEqual(self.sp.items[0]["doc_type"], sharepoint.DOC_TYPE_AVS_SCREENSHOT)
        self.assertEqual(len(self.sp.uploads), 1)   # the original file was reused
        self.save(_draft_form(transaction_key="55", request_id=rid, avs_score="60"))
        self.assertEqual(self.sp.items[0]["doc_type"], sharepoint.DOC_TYPE_VALIDATION_EVIDENCE)

    def test_23_24_an_upload_failure_is_reported_and_never_shown_as_success(self):
        self.sp.fail_for = {"inv.pdf"}
        resp = self.save(files={"file_payment_support": _file("inv.pdf"), "file_wire_ach_instructions": _file("w.pdf")})
        self.assertEqual(resp.status_code, 302)                     # the Draft itself was stored
        self.assertEqual(self.store.created, [55])
        categories = {cat for cat, _ in self.flashes()}
        text = " ".join(msg for _, msg in self.flashes())
        self.assertNotIn("success", categories)
        self.assertEqual(categories, {"warning"})
        self.assertIn("Payment Support", text)
        self.assertIn("NOT attached", text)
        self.assertNotIn("External ACH/Wire Instructions", text)   # the other file did upload
        self.assertEqual([u[1] for u in self.sp.uploads], ["w.pdf"])
        body = self.resume()
        self.assertIn("<strong>w.pdf</strong>", body)
        self.assertNotIn("<strong>inv.pdf</strong>", body)

    def test_a_failed_update_upload_is_reported_the_same_way(self):
        self.save()
        self.sp.fail_for = {"inv.pdf"}
        self.client.get("/dashboard")  # drain flashes
        self.save(_draft_form(transaction_key="55", request_id="x"), files={"file_payment_support": _file("inv.pdf")})
        self.assertTrue(any("NOT attached" in msg for _, msg in self.flashes()))

    def test_a_forged_request_id_never_redirects_uploads_or_listings(self):
        self.save()
        rid = self.store.drafts[55]["request_id"]
        self.save(_draft_form(transaction_key="55", request_id="TXN-OTHER-1"),
                  files={"file_payment_support": _file("inv.pdf")})
        self.assertEqual(self.sp.uploads[0][0], rid)

    def test_a_draft_that_is_not_the_callers_is_not_editable(self):
        self.save()
        resp = self.save(_draft_form(transaction_key="777", request_id="TXN-OTHER-1"),
                         files={"file_payment_support": _file("inv.pdf")})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/dashboard", resp.headers["Location"])
        self.assertEqual(self.sp.uploads, [])
        self.assertEqual(self.store.updated, [])


# ═══ Final validation, precheck and finalize ═════════════════════════════════
def _final_form(**overrides):
    data = ir._external_form(transaction_key="55", request_id="TXN-2026-D055", verbal_confirmed_with_known="on")
    data.update(overrides)
    return data


class StoredAttachmentValidationTests(_Base):
    def _seed_all(self):
        self.save()
        self.sp.seed(self.store.drafts[55]["request_id"],
                     ve=("avs.png", sharepoint.DOC_TYPE_AVS_SCREENSHOT),
                     wire=("w.pdf", sharepoint.DOC_TYPE_WIRE_ACH_INSTRUCTIONS),
                     pay=("inv.pdf", sharepoint.DOC_TYPE_PAYMENT_SUPPORT))
        return self.store.drafts[55]["request_id"]

    def _finalize(self, form=None, files=None):
        data = dict(form or _final_form(request_id=self.store.drafts[55]["request_id"]))
        data.update(files or {})
        return self.client.post("/intake/submit", data=data, content_type="multipart/form-data")

    def test_10_28_stored_documents_satisfy_final_submission_without_reselecting(self):
        self._seed_all()
        resp = self._finalize()
        self.assertEqual(resp.status_code, 302)
        self.m["finalize_draft_submission"].assert_called_once()
        self.assertEqual(self.sp.uploads, [])

    def test_stored_documents_are_only_recognized_in_their_own_category(self):
        self.save()
        rid = self.store.drafts[55]["request_id"]
        self.sp.seed(rid, pay=("inv.pdf", sharepoint.DOC_TYPE_PAYMENT_SUPPORT))
        body = self._finalize().get_data(as_text=True)
        self.assertIn("Validation Evidence attachment is required", body)
        self.assertIn("External ACH/Wire Instructions attachment is required", body)
        self.assertNotIn("Payment Support attachment is required", body)
        self.m["finalize_draft_submission"].assert_not_called()

    def test_without_stored_or_new_documents_final_submission_is_still_blocked(self):
        self.save()
        body = self._finalize().get_data(as_text=True)
        for message in ("Validation Evidence attachment is required",
                        "External ACH/Wire Instructions attachment is required",
                        "Payment Support attachment is required"):
            self.assertIn(message, body)
        self.m["finalize_draft_submission"].assert_not_called()

    def test_11_newly_selected_files_also_count_and_are_uploaded_once(self):
        self.save()
        resp = self._finalize(files={"file_validation_evidence": _file("e.pdf"),
                                     "file_wire_ach_instructions": _file("w.pdf"),
                                     "file_payment_support": _file("p.pdf")})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(len(self.sp.uploads), 3)

    def test_a_forged_request_id_cannot_borrow_another_requests_documents(self):
        self.save()
        self.sp.seed("TXN-OTHER-1", ve=("x.png", sharepoint.DOC_TYPE_VALIDATION_EVIDENCE),
                     wire=("x.pdf", sharepoint.DOC_TYPE_WIRE_ACH_INSTRUCTIONS),
                     pay=("x2.pdf", sharepoint.DOC_TYPE_PAYMENT_SUPPORT))
        resp = self._finalize(_final_form(request_id="TXN-OTHER-1"))
        self.assertEqual(resp.status_code, 200)
        self.m["finalize_draft_submission"].assert_not_called()

    def test_finalizing_a_draft_that_is_not_the_callers_is_refused(self):
        self._seed_all()
        resp = self._finalize(_final_form(transaction_key="777"), files={
            "file_validation_evidence": _file("e.pdf"), "file_wire_ach_instructions": _file("w.pdf"),
            "file_payment_support": _file("p.pdf")})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/dashboard", resp.headers["Location"])
        self.assertEqual(self.sp.uploads, [])
        self.m["finalize_draft_submission"].assert_not_called()

    def test_finalize_upload_failure_stops_the_submission(self):
        self.save()
        self.sp.fail_for = {"p.pdf"}
        resp = self._finalize(files={"file_validation_evidence": _file("e.pdf"),
                                     "file_wire_ach_instructions": _file("w.pdf"),
                                     "file_payment_support": _file("p.pdf")})
        body = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Your request was NOT submitted", body)
        self.assertIn("Payment Support", body)
        self.m["finalize_draft_submission"].assert_not_called()

    def test_27_precheck_recognizes_saved_attachments(self):
        rid = self._seed_all()
        form = pre._valid_precheck_form(transaction_key="55", request_id=rid, has_validation_evidence="false",
                                        has_wire_ach_instructions="false", has_payment_support="false")
        errors = self.client.post("/intake/precheck", data=form).get_json()["errors"]
        self.assertFalse([e for e in errors if "attachment" in e], errors)

    def test_27b_precheck_without_stored_documents_still_requires_them(self):
        self.save()
        form = pre._valid_precheck_form(transaction_key="55", request_id="x", has_validation_evidence="false",
                                        has_wire_ach_instructions="false", has_payment_support="false")
        errors = self.client.post("/intake/precheck", data=form).get_json()["errors"]
        self.assertEqual(len([e for e in errors if "attachment is required" in e]), 3)

    def test_precheck_and_final_submit_agree_about_stored_documents(self):
        rid = self._seed_all()
        precheck = self.client.post("/intake/precheck", data=pre._valid_precheck_form(
            transaction_key="55", request_id=rid, has_validation_evidence="false",
            has_wire_ach_instructions="false", has_payment_support="false")).get_json()["errors"]
        self.assertEqual(precheck, [])
        self.assertEqual(self._finalize().status_code, 302)

    def test_29_same_day_ach_banking_validation_still_applies_at_finalize(self):
        self._seed_all()
        body = self._finalize(_final_form(request_type="Same Day ACH", recv_bank_name="",
                                          request_id=self.store.drafts[55]["request_id"])).get_data(as_text=True)
        self.assertIn("Receiving Bank Name is required", body)

    def test_30_the_internal_ach_exemption_stays_ach_only_for_drafts(self):
        self.save()
        self.sp.seed(self.store.drafts[55]["request_id"], ve=("e.png", sharepoint.DOC_TYPE_VALIDATION_EVIDENCE),
                     pay=("p.pdf", sharepoint.DOC_TYPE_PAYMENT_SUPPORT))
        internal = ir._form(transaction_key="55", request_id="x", verbal_confirmed_with_known="on")
        self.assertEqual(self._finalize(internal).status_code, 302)                 # internal ACH: no instructions needed
        external = _final_form()
        self.assertIn("External ACH/Wire Instructions attachment is required",
                      self._finalize(external).get_data(as_text=True))

    def test_22_a_recipient_closed_after_the_draft_was_saved_is_revalidated_at_finalize(self):
        self.save(ir._form(form_mode="draft", recv_payee_name="Payroll LLC"))
        self.sp.seed(self.store.drafts[55]["request_id"], ve=("e.png", sharepoint.DOC_TYPE_VALIDATION_EVIDENCE),
                     pay=("p.pdf", sharepoint.DOC_TYPE_PAYMENT_SUPPORT))
        self.m["get_internal_recipient_summary"].return_value = ir._summary(status="Closed")
        body = self._finalize(ir._form(transaction_key="55", request_id="x")).get_data(as_text=True)
        self.assertIn("closed and cannot be used", body)
        self.m["finalize_draft_submission"].assert_not_called()


# ═══ Re-render keeps the Draft's identity ════════════════════════════════════
class ReRenderKeepsDraftIdentityTests(_Base):
    def test_a_rejected_draft_save_keeps_the_transaction_key_request_id_and_stored_documents(self):
        self.save(files={"file_payment_support": _file("inv.pdf")})
        rid = self.store.drafts[55]["request_id"]
        resp = self.save(_draft_form(transaction_key="55", request_id=rid, avs_score="abc"))
        body = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(_value(body, "transaction_key"), "55")
        self.assertEqual(_value(body, "request_id"), rid)
        self.assertIn("<strong>inv.pdf</strong>", body)
        # ...so the corrected re-save updates the same Draft instead of creating a duplicate.
        self.assertEqual(self.save(_draft_form(transaction_key=_value(body, "transaction_key"), request_id=rid)).status_code, 302)
        self.assertEqual(self.store.created, [55])

    def test_a_rejected_final_submit_keeps_the_drafts_identity_and_documents(self):
        self.save(files={"file_payment_support": _file("inv.pdf")})
        rid = self.store.drafts[55]["request_id"]
        data = dict(_final_form(request_id=rid, payment_purpose=""))
        resp = self.client.post("/intake/submit", data=data, content_type="multipart/form-data")
        body = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(_value(body, "transaction_key"), "55")
        self.assertIn("<strong>inv.pdf</strong>", body)
        self.assertIn('data-existing="true"', _tag(body, "file_payment_support"))

    def test_a_brand_new_form_has_no_draft_identity(self):
        body = self.client.get("/intake").get_data(as_text=True)
        self.assertEqual(_value(body, "transaction_key"), "")


# ═══ Page behavior markers (browser behavior is verified separately) ════════
class PageInitializationTests(_Base):
    def test_26_resume_renders_saved_state_in_markup_not_via_script_defaults(self):
        self.save()
        body = self.resume()
        script = body[body.index("<script>"):]
        # The initialization code must only reflect state; it must not blank saved verification inputs.
        for field in ("avs_score", "verbal_contact_name", "verbal_confirm_datetime"):
            self.assertNotRegex(script, r"getElementById\('%s'\)\.value\s*=\s*''" % field)
        self.assertIn("if (!checked && dateIn) dateIn.value = '';", script)   # only clears when unchecked
        self.assertIn("applyAutomaticUrgency(true)", script)

    def test_client_side_validation_honors_stored_files(self):
        self.save()
        script = self.resume().split("function collectClientSideErrors", 1)[1][:5000]
        for slot in ("valEv", "wireInstr", "paySup"):
            self.assertRegex(script, slot + r"\.dataset\.existing")


class NoSideEffectsTests(_Base):
    def test_25_saving_and_resuming_a_draft_creates_no_workflow_records(self):
        self.save(files={"file_payment_support": _file("inv.pdf")})
        self.resume()
        for name in ("finalize_draft_submission", "insert_transaction", "reserve_request_id"):
            self.m[name].assert_not_called()

    def test_no_external_access_was_attempted(self):
        self.assertEqual(external_guard.state.blocked, [])


if __name__ == "__main__":
    unittest.main()
