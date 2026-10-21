"""
Tests for Item 6: Corporate bulk-recipient payments.

A Corporate requester attaches an Excel listing of recipient accounts to ONE ACH / Same Day ACH /
Wire request. Every row must resolve to exactly one Open maintained BankAccount; one miss blocks
the whole request. No Beneficiary rows are created, AVS / previously-used history do not apply,
approvals and the remaining banking controls (verbal confirmation, Payment Support) are unchanged.
The feature is gated (BULK_RECIPIENT_ENABLED + migrated schema) and fails closed.

Mock-only: `external_guard` (imported first) makes SQL / SharePoint / network inert.

Run:  python -m unittest test_bulk_recipients -v
"""
import hashlib
import io
import os
import unittest
import zipfile
import external_guard  # noqa: F401  (must precede app/db/sharepoint imports)
from unittest.mock import MagicMock, patch

import app as app_module
import bulk_listing
import db
import sharepoint
import test_internal_recipient as ir

# Obviously-fake banking values: only ever used inside mocks.
ACCT = {1: "9900110001", 2: "9900110002", 3: "9900110003"}
RTN = "021000089"
KEYS = {ACCT[1]: 31, ACCT[2]: 32, ACCT[3]: 33}


# ── xlsx fixture builder (stdlib only) ───────────────────────────────────────

def _esc(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def make_xlsx(rows, headers=("Account Number", "Routing Number"), numeric=(), formula=()):
    """rows: [(account, routing)]. numeric/formula: {(row_index, col_index)} cells written as number/formula."""
    def cell(ref, value, kind=None):
        if kind == "n":
            return f'<c r="{ref}"><v>{value}</v></c>'
        if kind == "f":
            return f'<c r="{ref}"><f>1+1</f><v>2</v></c>'
        return f'<c r="{ref}" t="inlineStr"><is><t>{_esc(value)}</t></is></c>'

    sheet_rows = []
    if headers:
        sheet_rows.append('<row r="1">' + cell("A1", headers[0]) + cell("B1", headers[1]) + "</row>")
    for i, (acct, rtn) in enumerate(rows):
        r = i + 2
        cells = []
        for c, (col, value) in enumerate((("A", acct), ("B", rtn))):
            kind = "n" if (i, c) in numeric else ("f" if (i, c) in formula else None)
            if value is None and kind is None:
                continue
            cells.append(cell(f"{col}{r}", value, kind))
        sheet_rows.append(f'<row r="{r}">' + "".join(cells) + "</row>")
    sheet = ('<?xml version="1.0" encoding="UTF-8"?>'
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
             + "".join(sheet_rows) + "</sheetData></worksheet>")
    workbook = ('<?xml version="1.0" encoding="UTF-8"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>')
    rels = ('<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="worksheets/sheet1.xml"/></Relationships>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        zf.writestr("xl/workbook.xml", workbook)
        zf.writestr("xl/_rels/workbook.xml.rels", rels)
        zf.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


GOOD_ROWS = [(ACCT[1], RTN), (ACCT[2], RTN), (ACCT[3], RTN)]


def good_xlsx():
    return make_xlsx(GOOD_ROWS)


def matcher(book=None, status="Open", extra=None):
    """A fake db.match_bulk_recipient_accounts: known accounts match one BankAccount."""
    book = KEYS if book is None else book
    overrides = extra or {}

    def _match(pairs):
        out = []
        for acct, _rtn in pairs:
            if acct in overrides:
                out.append(overrides[acct])
            elif acct in book:
                out.append([{"bank_account_key": book[acct], "status": status}])
            else:
                out.append([])
        return out
    return _match


def _no_numbers(testcase, text):
    for value in list(ACCT.values()) + [RTN]:
        testcase.assertNotIn(value, text)


# ── 1. The workbook reader ───────────────────────────────────────────────────

class ListingParserTests(unittest.TestCase):
    def test_valid_workbook_parses_with_worksheet_row_numbers(self):
        rows, problems, total = bulk_listing.parse_listing(good_xlsx(), "r.xlsx")
        self.assertEqual((problems, total), ([], 3))
        self.assertEqual([r.line for r in rows], [2, 3, 4])
        self.assertEqual(rows[0].account, ACCT[1])

    def test_leading_zeros_are_preserved_as_text(self):
        rows, _, _ = bulk_listing.parse_listing(make_xlsx([("0012345678", "021000089")]), "r.xlsx")
        self.assertEqual(rows[0].account, "0012345678")

    def test_numeric_cells_are_rejected_because_leading_zeros_cannot_be_proven(self):
        rows, problems, _ = bulk_listing.parse_listing(make_xlsx([(ACCT[1], "21000089")], numeric={(0, 1)}), "r.xlsx")
        self.assertEqual(rows, [])
        self.assertTrue(any("not text" in p for p in problems))

    def test_formula_cells_are_rejected_and_never_evaluated(self):
        rows, problems, _ = bulk_listing.parse_listing(make_xlsx([(ACCT[1], RTN)], formula={(0, 0)}), "r.xlsx")
        self.assertEqual(rows, [])
        self.assertTrue(any("formula" in p for p in problems))

    def test_missing_required_header_is_a_listing_error(self):
        with self.assertRaises(bulk_listing.ListingError):
            bulk_listing.parse_listing(make_xlsx(GOOD_ROWS, headers=("Acct", "Routing Number")), "r.xlsx")

    def test_blank_and_malformed_values_are_reported_by_row(self):
        _, problems, _ = bulk_listing.parse_listing(make_xlsx([("", RTN), ("12 34", RTN)]), "r.xlsx")
        self.assertEqual(len(problems), 2)
        self.assertTrue(problems[0].startswith("Row 2:") and problems[1].startswith("Row 3:"))

    def test_duplicate_rows_are_reported(self):
        _, problems, _ = bulk_listing.parse_listing(make_xlsx([(ACCT[1], RTN), (ACCT[1], RTN)]), "r.xlsx")
        self.assertTrue(any("duplicate of row 2" in p for p in problems))

    def test_empty_listing_is_an_error(self):
        with self.assertRaises(bulk_listing.ListingError):
            bulk_listing.parse_listing(make_xlsx([]), "r.xlsx")

    def test_row_limit(self):
        rows = [(f"A{i:08d}", RTN) for i in range(bulk_listing.MAX_ROWS + 1)]
        with self.assertRaises(bulk_listing.ListingError):
            bulk_listing.parse_listing(make_xlsx(rows), "r.xlsx")

    def test_only_xlsx_and_readable_files_are_accepted(self):
        with self.assertRaises(bulk_listing.ListingError):
            bulk_listing.parse_listing(b"Account Number,Routing Number\n1,2", "r.csv")
        with self.assertRaises(bulk_listing.ListingError):
            bulk_listing.parse_listing(b"not a zip", "r.xlsx")
        with self.assertRaises(bulk_listing.ListingError):
            bulk_listing.check_file_basics("r.xlsx", bulk_listing.MAX_FILE_BYTES + 1)
        with self.assertRaises(bulk_listing.ListingError):
            bulk_listing.check_file_basics("r.xlsx", 0)

    def test_xml_entities_are_refused(self):
        content = good_xlsx()
        buf = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(content)) as src, zipfile.ZipFile(buf, "w") as dst:
            for item in src.infolist():
                data = src.read(item.filename)
                if item.filename.endswith("sheet1.xml"):
                    data = b'<!DOCTYPE x [<!ENTITY a "b">]>' + data
                dst.writestr(item.filename, data)
        with self.assertRaises(bulk_listing.ListingError):
            bulk_listing.parse_listing(buf.getvalue(), "r.xlsx")


# ── 2. Matching against BankAccount ──────────────────────────────────────────

class ListingValidationTests(unittest.TestCase):
    def _validate(self, content=None, **kw):
        return bulk_listing.validate_bulk_listing(
            content or good_xlsx(), "r.xlsx", originating_bank_account_key=kw.pop("origin", 5),
            matcher=kw.pop("m", matcher()))

    def test_all_rows_matching_one_open_account_each_is_accepted(self):
        result = self._validate()
        self.assertTrue(result.ok)
        self.assertEqual(result.recipients, [{"line": 2, "bank_account_key": 31},
                                             {"line": 3, "bank_account_key": 32},
                                             {"line": 4, "bank_account_key": 33}])
        self.assertEqual((result.row_count, result.matched_count), (3, 3))

    def test_hash_covers_the_exact_bytes(self):
        content = good_xlsx()
        self.assertEqual(self._validate(content).sha256, hashlib.sha256(content).hexdigest())
        self.assertNotEqual(self._validate(make_xlsx(GOOD_ROWS[:2])).sha256, hashlib.sha256(content).hexdigest())

    def test_one_unmatched_row_blocks_everything(self):
        result = self._validate(m=matcher(book={ACCT[1]: 31, ACCT[2]: 32}))
        self.assertFalse(result.ok)
        self.assertEqual(result.recipients, [])  # nothing is carried forward from a partly-valid listing
        self.assertIn("Row 4: no matching BankAccount record.", result.errors)

    def test_ambiguous_match_is_rejected(self):
        two = [{"bank_account_key": 31, "status": "Open"}, {"bank_account_key": 99, "status": "Open"}]
        result = self._validate(m=matcher(extra={ACCT[1]: two}))
        self.assertFalse(result.ok)
        self.assertTrue(any("ambiguous" in e for e in result.errors))

    def test_closed_account_is_rejected(self):
        result = self._validate(m=matcher(status="Closed"))
        self.assertFalse(result.ok)
        self.assertTrue(any("not Open" in e for e in result.errors))

    def test_originating_account_cannot_also_be_a_recipient(self):
        result = self._validate(origin=32)
        self.assertFalse(result.ok)
        self.assertTrue(any("originating account" in e for e in result.errors))

    def test_two_rows_resolving_to_the_same_account_are_rejected(self):
        result = self._validate(m=matcher(extra={ACCT[2]: [{"bank_account_key": 31, "status": "Open"}]}))
        self.assertFalse(result.ok)
        self.assertTrue(any("same BankAccount as row 2" in e for e in result.errors))

    def test_matcher_failure_fails_closed_with_a_generic_message(self):
        def boom(pairs):
            raise RuntimeError(f"driver exploded near {ACCT[1]}")
        result = self._validate(m=boom)
        self.assertFalse(result.ok)
        self.assertEqual(result.recipients, [])
        _no_numbers(self, " ".join(result.errors))

    def test_messages_never_contain_account_or_routing_numbers(self):
        for result in (self._validate(m=matcher(book={})), self._validate(m=matcher(status="Closed")),
                       self._validate(make_xlsx([(ACCT[1], RTN), (ACCT[1], RTN)]))):
            _no_numbers(self, " ".join(result.errors))


# ── 3. The feature gate ──────────────────────────────────────────────────────

class _SchemaCursor:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, params=None):
        self.sql = sql

    def fetchall(self):
        return self.rows


def _schema_rows(*, migrated=True, links_nullable=True):
    rows = [("ETransaction", c, "YES") for c in ("Recipient_Mode", "Bulk_Listing_SHA256",
                                                 "Bulk_Recipient_Count", "Bulk_Listing_Validated_DateTime")] if migrated else []
    rows += [("ETransaction", "Beneficiary_Key", "YES" if links_nullable else "NO"),
             ("ETransaction", "BeneficiaryInstruction_Key", "YES" if links_nullable else "NO")]
    if migrated:
        rows += [("BulkRecipientAccount", c, "NO") for c in sorted(db._BULK_REQUIRED_CHILD_COLUMNS)]
    return rows


class FeatureGateTests(unittest.TestCase):
    def setUp(self):
        db._bulk_schema_state.update(ready=False, checked_at=None)
        self.addCleanup(db._bulk_schema_state.update, ready=False, checked_at=None)

    def _ready(self, rows):
        conn = MagicMock()
        conn.cursor.return_value = _SchemaCursor(rows)
        with patch.object(db, "get_connection", return_value=conn):
            return db.bulk_schema_ready()

    def test_default_is_off_and_never_touches_the_database(self):
        with patch.dict(os.environ, {}, clear=False), patch.object(db, "get_connection") as conn:
            os.environ.pop("BULK_RECIPIENT_ENABLED", None)
            self.assertFalse(db.bulk_recipient_enabled())
            conn.assert_not_called()

    def test_env_on_but_schema_missing_fails_closed(self):
        with patch.dict(os.environ, {"BULK_RECIPIENT_ENABLED": "true"}):
            conn = MagicMock()
            conn.cursor.return_value = _SchemaCursor(_schema_rows(migrated=False))
            with patch.object(db, "get_connection", return_value=conn):
                self.assertFalse(db.bulk_recipient_enabled())

    def test_not_null_beneficiary_links_mean_not_ready(self):
        self.assertFalse(self._ready(_schema_rows(links_nullable=False)))

    def test_env_on_and_schema_migrated_enables(self):
        with patch.dict(os.environ, {"BULK_RECIPIENT_ENABLED": "true"}):
            conn = MagicMock()
            conn.cursor.return_value = _SchemaCursor(_schema_rows())
            with patch.object(db, "get_connection", return_value=conn):
                self.assertTrue(db.bulk_recipient_enabled())

    def test_schema_check_error_fails_closed_and_is_not_retried_immediately(self):
        with patch.object(db, "get_connection", side_effect=RuntimeError("down")) as conn:
            self.assertFalse(db.bulk_schema_ready())
            self.assertFalse(db.bulk_schema_ready())
            self.assertEqual(conn.call_count, 1)

    def test_env_off_wins_even_when_the_schema_is_present(self):
        with patch.dict(os.environ, {"BULK_RECIPIENT_ENABLED": "false"}):
            conn = MagicMock()
            conn.cursor.return_value = _SchemaCursor(_schema_rows())
            with patch.object(db, "get_connection", return_value=conn):
                self.assertFalse(db.bulk_recipient_enabled())

    def test_historical_rows_without_a_stored_mode_are_interpreted_from_relationships(self):
        self.assertEqual(db.effective_recipient_mode(None, None), "external")
        self.assertEqual(db.effective_recipient_mode(None, 9), "internal")
        self.assertEqual(db.effective_recipient_mode("Bulk", None), "bulk")
        self.assertEqual(db.effective_recipient_mode("External", 9), "external")


# ── 4. Request submission ────────────────────────────────────────────────────

def _bulk_form(**overrides):
    data = ir._form(recipient_mode="bulk", recipient_bank_account_key="", recv_payee_name="", avs_score="")
    data.pop("recipient_bank_account_key")
    data.pop("recv_payee_name")
    data.pop("avs_score")
    data.update(overrides)
    return data


def _bulk_files(content=None, **omit):
    files = {
        "file_bulk_recipient_listing": (io.BytesIO(content if content is not None else good_xlsx()), "recipients.xlsx"),
        "file_payment_support": (io.BytesIO(b"x"), "p.pdf"),
    }
    for name in omit:
        files.pop(name, None)
    return files


class _BulkRouteBase(ir._RouteBase):
    gate = True

    def _bulk_post(self, form, *, files=None, m=None, confirm=True, **kw):
        extra = (
            patch.object(app_module, "bulk_recipient_enabled", return_value=self.gate),
            patch.object(app_module.db, "match_bulk_recipient_accounts", side_effect=m or matcher()),
        ) + ((patch.object(app_module, "_stored_listing_matches", return_value=True),) if confirm else ()) \
          + tuple(kw.pop("extra", ()))
        return self._post(form, files=_bulk_files() if files is None else files, extra=extra, **kw)

    def tearDown(self):
        self.assertEqual(external_guard.state.blocked, [])

    def _check(self, *, gate=True, content=None, m=None, role="submitter"):
        with self.client.session_transaction() as sess:
            sess["role"] = role
        with patch.object(app_module, "bulk_recipient_enabled", return_value=gate), \
             patch.object(app_module, "current_app_user", return_value={"user_key": 11, "display_name": "U", "email": ""}), \
             patch.object(app_module.db, "match_bulk_recipient_accounts", side_effect=m or matcher()):
            return self.client.post(
                "/intake/bulk-listing/check", content_type="multipart/form-data",
                data={"bank_account_key": "5",
                      "file_bulk_recipient_listing": (io.BytesIO(content or good_xlsx()), "r.xlsx")})


class BulkSubmissionTests(_BulkRouteBase):
    def test_corporate_ach_with_a_verified_listing_creates_one_bulk_transaction(self):
        resp, mk = self._bulk_post(_bulk_form())
        self.assertEqual(resp.status_code, 302)
        mk["insert_transaction"].assert_called_once()
        data = mk["insert_transaction"].call_args[0][0]
        self.assertEqual(data["recipient_mode"], "Bulk")
        self.assertEqual([r["bank_account_key"] for r in data["bulk_recipients"]], [31, 32, 33])
        self.assertEqual(data["bulk_listing_sha256"], hashlib.sha256(good_xlsx()).hexdigest())
        self.assertIsNone(data["recipient_bank_account_key"])
        mk["_upload_required_intake_attachments"].assert_called_once()
        mk["resolve_approval_rule"].assert_called_once_with(1000.0)  # approvals/thresholds unchanged

    def test_same_day_ach_and_wire_are_allowed_and_same_day_urgency_is_kept(self):
        resp, mk = self._bulk_post(_bulk_form(request_type="Same Day ACH", urgent="no"))
        self.assertEqual(resp.status_code, 302)
        data = mk["insert_transaction"].call_args[0][0]
        self.assertTrue(data["urgent"])
        self.assertEqual(data["urgency_reason"], "Same Day ACH")
        resp, mk = self._bulk_post(_bulk_form(request_type="Wire"))
        self.assertEqual(resp.status_code, 302)
        mk["insert_transaction"].assert_called_once()

    def test_unsupported_request_types_are_rejected(self):
        for request_type in ("ACH Pull", "Intra Bank Transfer", "EFT"):
            with self.subTest(request_type=request_type):
                resp, mk = self._bulk_post(_bulk_form(request_type=request_type))
                self.assertEqual(resp.status_code, 200)
                mk["insert_transaction"].assert_not_called()
                self.assertIn("ACH, Same Day ACH and Wire", resp.get_data(as_text=True))

    def test_property_requests_are_rejected_even_if_the_browser_forces_bulk(self):
        resp, mk = self._bulk_post(_bulk_form(classification="property", property_entity_number="1234"))
        self.assertEqual(resp.status_code, 200)
        mk["insert_transaction"].assert_not_called()
        self.assertIn("Corporate requests only", resp.get_data(as_text=True))

    def test_a_forged_single_recipient_key_is_rejected(self):
        resp, mk = self._bulk_post(_bulk_form(recipient_bank_account_key="9"))
        self.assertEqual(resp.status_code, 200)
        mk["insert_transaction"].assert_not_called()

    def test_one_unmatched_row_blocks_the_whole_request_without_leaking_numbers(self):
        resp, mk = self._bulk_post(_bulk_form(), m=matcher(book={ACCT[1]: 31, ACCT[2]: 32}))
        body = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200)
        mk["insert_transaction"].assert_not_called()
        mk["reserve_request_id"].assert_not_called()
        mk["_upload_required_intake_attachments"].assert_not_called()  # nothing orphaned in SharePoint
        self.assertIn("Row 4: no matching BankAccount record.", body)
        _no_numbers(self, body)

    def test_closed_ambiguous_and_originating_matches_block(self):
        for label, m, origin in (("closed", matcher(status="Closed"), "5"),
                                 ("originating", matcher(), "32"),
                                 ("ambiguous", matcher(extra={ACCT[1]: [{"bank_account_key": 1, "status": "Open"},
                                                                         {"bank_account_key": 2, "status": "Open"}]}), "5")):
            with self.subTest(label):
                resp, mk = self._bulk_post(_bulk_form(bank_account_key=origin), m=m)
                self.assertEqual(resp.status_code, 200)
                mk["insert_transaction"].assert_not_called()

    def test_invalid_or_unreadable_listing_blocks(self):
        for content in (b"not an excel file", make_xlsx(GOOD_ROWS, headers=("A", "B")), make_xlsx([])):
            resp, mk = self._bulk_post(_bulk_form(), files=_bulk_files(content))
            self.assertEqual(resp.status_code, 200)
            mk["insert_transaction"].assert_not_called()

    def test_the_listing_is_required(self):
        resp, mk = self._bulk_post(_bulk_form(), files=_bulk_files(file_bulk_recipient_listing=True))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Bulk Recipient Listing attachment is required.", resp.get_data(as_text=True))
        mk["insert_transaction"].assert_not_called()

    def test_payment_support_and_verbal_confirmation_are_still_required(self):
        resp, mk = self._bulk_post(_bulk_form(), files=_bulk_files(file_payment_support=True))
        self.assertIn("Payment Support attachment is required.", resp.get_data(as_text=True))
        resp, mk = self._bulk_post(_bulk_form(verbal_confirmed_with_known=""))
        self.assertIn("Verbal confirmation with a known contact is required", resp.get_data(as_text=True))
        mk["insert_transaction"].assert_not_called()

    def test_avs_evidence_and_wire_instructions_are_not_required_and_prior_use_is_never_consulted(self):
        resp, mk = self._bulk_post(_bulk_form(avs_score="12", instructions_previously_used="yes", last_used_date="2026-01-01"))
        self.assertEqual(resp.status_code, 302)
        data = mk["insert_transaction"].call_args[0][0]
        # Whatever the browser posted, the "not applicable" values are what is stored.
        self.assertFalse(data["instructions_previously_used"])
        self.assertEqual((data["avs_score"], data["last_used_date"]), ("", ""))
        self.assertIsNone(data["prior_transaction_key"])
        mk["find_prior_completed_beneficiary_match"].assert_not_called()
        mk["get_internal_recipient_summary"].assert_not_called()

    def test_unrelated_banking_fields_are_not_persisted_for_bulk(self):
        resp, mk = self._bulk_post(_bulk_form(recv_account_number=ACCT[1], recv_routing_number=RTN))
        data = mk["insert_transaction"].call_args[0][0]
        self.assertEqual(data["recipient_mode"], "Bulk")  # db skips Beneficiary rows for this mode (see db tests)

    def test_a_forged_validation_evidence_upload_is_not_stored_in_bulk_mode(self):
        calls = []
        with patch.object(app_module, "sharepoint_enabled", return_value=True), \
             patch.object(app_module.sharepoint, "upload_attachment", side_effect=lambda *a, **k: calls.append(k["doc_type"])), \
             patch.object(app_module.sharepoint, "retag_validation_evidence") as retag, \
             app_module.app.test_request_context(
                 "/intake/submit", method="POST",
                 data={"recipient_mode": "bulk", **{k: v for k, v in _bulk_files().items()},
                       "file_validation_evidence": (io.BytesIO(b"x"), "v.pdf"),
                       "file_wire_ach_instructions": (io.BytesIO(b"x"), "w.pdf")},
                 content_type="multipart/form-data"):
            app_module._upload_required_intake_attachments("TXN-2026-1")
        self.assertEqual(sorted(calls), sorted([sharepoint.DOC_TYPE_BULK_RECIPIENT_LISTING,
                                                sharepoint.DOC_TYPE_PAYMENT_SUPPORT]))
        retag.assert_not_called()

    def test_error_responses_contain_no_banking_numbers(self):
        resp, _ = self._bulk_post(_bulk_form(), m=matcher(book={}))
        _no_numbers(self, resp.get_data(as_text=True))


class GateOffTests(_BulkRouteBase):
    gate = False

    def test_bulk_posts_are_refused_and_the_listing_is_never_read(self):
        with patch.object(bulk_listing, "validate_bulk_listing") as validate:
            resp, mk = self._bulk_post(_bulk_form())
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Bulk recipient payments are not available.", resp.get_data(as_text=True))
        mk["insert_transaction"].assert_not_called()
        validate.assert_not_called()

    def test_existing_modes_persist_no_recipient_mode_while_the_gate_is_off(self):
        resp, mk = self._post(ir._external_form())
        self.assertEqual(resp.status_code, 302)
        data = mk["insert_transaction"].call_args[0][0]
        self.assertNotIn("recipient_mode", data)
        self.assertNotIn("bulk_recipients", data)

    def test_check_endpoint_refuses(self):
        resp = self._check(gate=False)
        self.assertEqual(resp.status_code, 400)


class ModeFieldsWhenEnabledTests(_BulkRouteBase):
    def test_external_and_internal_requests_record_their_mode_without_other_changes(self):
        resp, mk = self._bulk_post(ir._external_form(), files=ir._files())
        data = mk["insert_transaction"].call_args[0][0]
        self.assertEqual(data["recipient_mode"], "External")
        self.assertNotIn("bulk_recipients", data)
        self.assertEqual(data["recv_account_number"], "12345678")
        resp, mk = self._bulk_post(ir._form(), files=ir._files(), summary=ir._summary())
        data = mk["insert_transaction"].call_args[0][0]
        self.assertEqual((data["recipient_mode"], data["recipient_bank_account_key"]), ("Internal", 9))

    def test_normal_mode_avs_and_prior_use_rules_are_unchanged(self):
        resp, mk = self._bulk_post(ir._external_form(), files=ir._files(file_validation_evidence=True))
        self.assertIn("Validation Evidence attachment is required.", resp.get_data(as_text=True))
        resp, mk = self._bulk_post(ir._external_form(), files=ir._files(), prior_match=None,
                                   )
        mk["find_prior_completed_beneficiary_match"].assert_called()


class BulkCheckEndpointTests(_BulkRouteBase):
    def test_a_good_listing_reports_counts_only(self):
        resp = self._check()
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual((data["ok"], data["row_count"], data["matched_count"]), (True, 3, 3))
        _no_numbers(self, resp.get_data(as_text=True))

    def test_a_bad_listing_reports_rows_not_numbers(self):
        resp = self._check(m=matcher(book={ACCT[1]: 31}))
        data = resp.get_json()
        self.assertFalse(data["ok"])
        self.assertIn("Row 3: no matching BankAccount record.", data["errors"])
        _no_numbers(self, resp.get_data(as_text=True))

    def test_only_submitters_may_check(self):
        self.assertEqual(self._check(role="sam").status_code, 403)

    def test_nothing_is_stored_or_uploaded_by_a_check(self):
        with patch.object(app_module.sharepoint, "upload_attachment") as upload, \
             patch.object(app_module.db, "set_draft_bulk_recipients") as store:
            self._check()
        upload.assert_not_called()
        store.assert_not_called()


# ── 5. Drafts ────────────────────────────────────────────────────────────────

def _draft_form(**overrides):
    return _bulk_form(form_mode="draft", **overrides)


class BulkDraftTests(_BulkRouteBase):
    def test_draft_save_with_a_verified_listing_stores_the_recipients(self):
        resp, mk = self._bulk_post(_draft_form())
        self.assertEqual(resp.status_code, 302)
        data = mk["create_draft"].call_args[0][0]
        self.assertEqual(data["recipient_mode"], "Bulk")
        self.assertEqual(len(data["bulk_recipients"]), 3)
        mk["_upload_intake_attachments"].assert_called_once()

    def test_a_newly_attached_invalid_listing_saves_and_uploads_nothing(self):
        resp, mk = self._bulk_post(_draft_form(), m=matcher(book={ACCT[1]: 31}))
        self.assertEqual(resp.status_code, 200)
        mk["create_draft"].assert_not_called()
        mk["_upload_intake_attachments"].assert_not_called()
        _no_numbers(self, resp.get_data(as_text=True))

    def test_a_draft_may_be_saved_before_any_listing_is_attached(self):
        resp, mk = self._bulk_post(_draft_form(), files={})
        self.assertEqual(resp.status_code, 302)
        data = mk["create_draft"].call_args[0][0]
        self.assertEqual((data["recipient_mode"], data["bulk_recipients"]), ("Bulk", []))

    def test_unsupported_type_or_property_is_rejected_for_drafts_too(self):
        resp, mk = self._bulk_post(_draft_form(request_type="EFT"))
        self.assertEqual(resp.status_code, 200)
        resp, mk = self._bulk_post(_draft_form(classification="property", property_entity_number="1234"))
        self.assertEqual(resp.status_code, 200)
        mk["create_draft"].assert_not_called()

    def test_resave_with_an_unchanged_stored_listing_does_not_upload_again_and_reverifies(self):
        stored = ("recipients.xlsx", good_xlsx())
        with patch.object(app_module, "sharepoint_enabled", return_value=True), \
             patch.object(app_module.sharepoint, "read_latest_bulk_listing", return_value=stored) as read:
            resp, mk = self._bulk_post(_draft_form(transaction_key="55"), files={})
        self.assertEqual(resp.status_code, 302)
        read.assert_called_once()
        data = mk["update_draft"].call_args[0][1]
        self.assertEqual(len(data["bulk_recipients"]), 3)

    def test_a_stored_listing_that_no_longer_verifies_clears_recipients_and_warns(self):
        stored = ("recipients.xlsx", good_xlsx())
        with patch.object(app_module, "sharepoint_enabled", return_value=True), \
             patch.object(app_module.sharepoint, "read_latest_bulk_listing", return_value=stored):
            resp, mk = self._bulk_post(_draft_form(transaction_key="55"), files={}, m=matcher(status="Closed"))
        self.assertEqual(resp.status_code, 302)
        data = mk["update_draft"].call_args[0][1]
        self.assertEqual(data["bulk_recipients"], [])
        with self.client.session_transaction() as sess:
            flashes = " ".join(msg for _cat, msg in sess.get("_flashes", []))
        self.assertIn("does not currently verify", flashes)
        _no_numbers(self, flashes)

    def test_finalizing_a_draft_re_reads_and_re_verifies_the_stored_listing(self):
        item = {"doc_type": sharepoint.DOC_TYPE_BULK_RECIPIENT_LISTING, "filename": "x"}
        pay = {"doc_type": sharepoint.DOC_TYPE_PAYMENT_SUPPORT, "filename": "y"}
        stored = ("recipients.xlsx", good_xlsx())
        with patch.object(app_module, "sharepoint_enabled", return_value=True), \
             patch.object(app_module.sharepoint, "list_attachments", return_value=[item, pay]), \
             patch.object(app_module.sharepoint, "read_latest_bulk_listing", return_value=stored) as read:
            resp, mk = self._bulk_post(_bulk_form(transaction_key="55"), files={})
        self.assertEqual(resp.status_code, 302)
        read.assert_called_once()
        mk["finalize_draft_submission"].assert_called_once()
        data = mk["finalize_draft_submission"].call_args[0][1]
        self.assertEqual(data["recipient_mode"], "Bulk")
        self.assertEqual(len(data["bulk_recipients"]), 3)
        self.assertEqual(data["bulk_listing_sha256"], hashlib.sha256(good_xlsx()).hexdigest())

    def test_finalizing_with_an_unreadable_stored_listing_is_blocked(self):
        item = {"doc_type": sharepoint.DOC_TYPE_BULK_RECIPIENT_LISTING, "filename": "x"}
        pay = {"doc_type": sharepoint.DOC_TYPE_PAYMENT_SUPPORT, "filename": "y"}
        with patch.object(app_module, "sharepoint_enabled", return_value=True), \
             patch.object(app_module.sharepoint, "list_attachments", return_value=[item, pay]), \
             patch.object(app_module.sharepoint, "read_latest_bulk_listing", side_effect=RuntimeError("graph")):
            resp, mk = self._bulk_post(_bulk_form(transaction_key="55"), files={})
        self.assertEqual(resp.status_code, 200)
        mk["finalize_draft_submission"].assert_not_called()

    def test_finalizing_without_any_stored_listing_is_blocked(self):
        pay = {"doc_type": sharepoint.DOC_TYPE_PAYMENT_SUPPORT, "filename": "y"}
        with patch.object(app_module, "sharepoint_enabled", return_value=True), \
             patch.object(app_module.sharepoint, "list_attachments", return_value=[pay]):
            resp, mk = self._bulk_post(_bulk_form(transaction_key="55"), files={})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Bulk Recipient Listing attachment is required.", resp.get_data(as_text=True))
        mk["finalize_draft_submission"].assert_not_called()

    def test_a_bulk_draft_switched_to_external_must_re_enter_the_account_number(self):
        # get_draft_recipient_bank_account_key reports (False, None) for a bulk draft: nothing to keep.
        resp, mk = self._bulk_post(
            ir._external_form(transaction_key="55", recv_account_number="", recv_routing_number=""),
            files=ir._files(), stored_recipient=(False, None))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Receiving Account Number is required.", resp.get_data(as_text=True))
        mk["finalize_draft_submission"].assert_not_called()


# ── 6. Precheck ──────────────────────────────────────────────────────────────

class BulkPrecheckTests(_BulkRouteBase):
    def _precheck(self, form, *, stored=None):
        with self.client.session_transaction() as sess:
            sess["role"] = "submitter"
        patches = [
            patch.object(app_module, "current_app_user", return_value={"user_key": 11, "display_name": "U", "email": ""}),
            patch.object(app_module.db, "get_bank_account_status", return_value="Open"),
            patch.object(app_module.db, "get_app_user_role_codes", return_value=["sam", "controller"]),
            patch.object(app_module.db, "resolve_approval_rule", return_value=ir._RULE),
            patch.object(app_module.db, "get_draft_request_id", return_value="TXN-2026-D100"),
            patch.object(app_module, "bulk_recipient_enabled", return_value=True),
            patch.object(app_module.db, "match_bulk_recipient_accounts", side_effect=matcher()),
            patch.object(app_module.db, "find_prior_completed_beneficiary_match", return_value=None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.prior = app_module.db.find_prior_completed_beneficiary_match
        return self.client.post("/intake/precheck", data=form).get_json()

    def test_bulk_precheck_needs_no_payee_and_skips_prior_use(self):
        form = _bulk_form(has_bulk_recipient_listing="true", has_payment_support="true")
        result = self._precheck(form)
        self.assertEqual(result["errors"], [])
        self.prior.assert_not_called()

    def test_bulk_precheck_requires_the_listing_slot(self):
        result = self._precheck(_bulk_form(has_payment_support="true"))
        self.assertIn("Bulk Recipient Listing attachment is required.", result["errors"])

    def test_bulk_precheck_does_not_demand_avs_evidence_or_wire_instructions(self):
        result = self._precheck(_bulk_form(has_bulk_recipient_listing="true", has_payment_support="true"))
        self.assertFalse(any("Validation Evidence" in e or "External ACH/Wire" in e for e in result["errors"]))


# ── 7. Persistence SQL ───────────────────────────────────────────────────────

class _EligibilityCursor(ir._FakeCursor):
    """Answers the in-transaction eligibility COUNT(*) as the database would: `closed` = one account no longer Open."""

    def __init__(self, *a, closed=False, **kw):
        super().__init__(*a, **kw)
        self.closed = closed
        self._params = []

    def execute(self, sql, params=None):
        super().execute(sql, params)
        self._params = list(params or [])

    def fetchone(self):
        if "SELECT COUNT(*) FROM [etransactions].[BankAccount]" in self._last:
            n_keys = len(self._params) - 1
            return (n_keys - 1 if self.closed else n_keys,)
        return super().fetchone()

class BulkDbWriteTests(unittest.TestCase):
    def _insert(self, data, closed=False):
        cur = _EligibilityCursor(bank_row=ir._bank_row(), closed=closed)
        with patch.object(db, "get_connection", return_value=ir._conn(cur)), \
             patch.object(db, "resolve_approval_rule", return_value=ir._RULE), \
             patch.object(db, "resolve_entity_key", return_value=3):
            db.insert_transaction(dict(data))
        return cur

    def _bulk_data(self):
        return dict(ir._INSERT_DATA, recipient_bank_account_key=None, recv_payee_name="", recipient_mode="Bulk",
                    bulk_recipients=[{"line": 2, "bank_account_key": 31}, {"line": 3, "bank_account_key": 32}],
                    bulk_listing_sha256="a" * 64)

    def test_bulk_insert_creates_no_beneficiary_rows_and_one_transaction(self):
        cur = self._insert(self._bulk_data())
        self.assertFalse(cur.statements("INSERT INTO [etransactions].[Beneficiary]"))
        self.assertFalse(cur.statements("INSERT INTO [etransactions].[BeneficiaryBankInstruction]"))
        self.assertEqual(len(cur.statements("INSERT INTO [etransactions].[ETransaction]")), 1)
        sql, params = cur.statements("INSERT INTO [etransactions].[ETransaction]")[0]
        columns = [c.strip() for c in sql[sql.index("(") + 1: sql.index(") OUTPUT")].split(",")]
        self.assertIsNone(params[columns.index("Beneficiary_Key")])
        self.assertIsNone(params[columns.index("BeneficiaryInstruction_Key")])
        self.assertIsNone(params[columns.index("RecipientBankAccount_Key")])
        # One workflow: the same assignment and event rows as for any other request (no per-recipient workflow).
        baseline = self._insert(ir._INSERT_DATA)
        for table in ("WorkflowEvent", "WorkflowAssignment"):
            needle = f"INSERT INTO [etransactions].[{table}]"
            self.assertEqual(len(cur.statements(needle)), len(baseline.statements(needle)))

    def test_bulk_insert_stores_mode_matched_rows_and_batch_metadata(self):
        cur = self._insert(self._bulk_data())
        mode_sql, mode_params = cur.statements("SET Recipient_Mode = ?")[0]
        self.assertEqual(mode_params[0], "Bulk")
        children = cur.statements("INSERT INTO [etransactions].[BulkRecipientAccount]")
        self.assertEqual([p[1:4] for _, p in children], [[2, 31, "Matched"], [3, 32, "Matched"]])
        self.assertTrue(all(p[5] == "a" * 64 for _, p in children))
        _, meta = cur.statements("SET Bulk_Listing_SHA256 = ?")[0]
        self.assertEqual(meta[:2], ["a" * 64, 2])

    def test_the_sql_never_carries_account_or_routing_numbers_for_bulk(self):
        cur = self._insert(self._bulk_data())
        flat = repr(cur.executed)
        _no_numbers(self, flat)

    def test_without_a_recipient_mode_an_unmigrated_database_is_never_asked_about_the_new_columns(self):
        cur = self._insert(ir._INSERT_DATA)
        flat = " ".join(sql for sql, _ in cur.executed)
        self.assertNotIn("Recipient_Mode", flat)
        self.assertNotIn("BulkRecipientAccount", flat)
        self.assertNotIn("Bulk_Listing", flat)

    def test_external_and_internal_modes_clear_any_bulk_rows_when_recorded(self):
        cur = self._insert(dict(ir._INSERT_DATA, recipient_mode="Internal"))
        self.assertEqual(cur.statements("SET Recipient_Mode = ?")[0][1][0], "Internal")
        self.assertFalse(cur.statements("INSERT INTO [etransactions].[BulkRecipientAccount]"))

    def test_draft_listing_replacement_is_owner_and_draft_guarded(self):
        cur = ir._FakeCursor(guard_row=None)
        cur.fetchone = lambda: None
        conn = ir._conn(cur)
        with patch.object(db, "get_connection", return_value=conn):
            with self.assertRaises(db.DraftNotEditableError):
                db.set_draft_bulk_recipients(55, 11, [{"line": 2, "bank_account_key": 31}], "a" * 64)
        self.assertFalse(cur.statements("INSERT INTO [etransactions].[BulkRecipientAccount]"))

    def test_finalizing_a_bulk_draft_detaches_and_deletes_its_single_recipient_rows(self):
        cur = _EligibilityCursor(guard_row=(10, 20))
        data = dict(ir._INSERT_DATA, recipient_mode="Bulk", recipient_bank_account_key=None,
                    bulk_recipients=[{"line": 2, "bank_account_key": 31}], bulk_listing_sha256="b" * 64,
                    approval_rule_key=7, requires_vp=False, requires_cfo=False, approval_tier="Sr. Accounting Manager",
                    treasury_service_date="2026-10-01")
        with patch.object(db, "get_connection", return_value=ir._conn(cur)), \
             patch.object(db, "resolve_entity_key", return_value=3):
            db.finalize_draft_submission(55, data, prepared_by_user_key=11)
        self.assertTrue(cur.statements("DELETE FROM [etransactions].[BeneficiaryBankInstruction]"))
        self.assertTrue(cur.statements("DELETE FROM [etransactions].[Beneficiary]"))
        self.assertEqual(cur.statements("SET Recipient_Mode = ?")[0][1][0], "Bulk")


# ── 8. Pages ─────────────────────────────────────────────────────────────────

class PageTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as sess:
            sess["role"] = "submitter"

    def _intake(self, enabled):
        accounts = [{"bank_account_key": 5, "bank_name": "Wells Fargo", "account_title": "Operating",
                     "account_last4": "1111", "entity_name": "Acme", "classification": "Corporate"}]
        with patch.object(app_module, "current_app_user", return_value={"user_key": 11, "display_name": "U", "email": ""}), \
             patch.object(app_module.db, "get_bank_accounts", return_value=accounts), \
             patch.object(app_module.db, "get_user_list", return_value=[]), \
             patch.object(app_module.db, "get_reassignment_candidates", return_value=[]), \
             patch.object(app_module, "bulk_recipient_enabled", return_value=enabled):
            return self.client.get("/intake").get_data(as_text=True)

    def test_the_bulk_choice_and_panel_exist_only_when_the_feature_is_on(self):
        on, off = self._intake(True), self._intake(False)
        for marker in ('id="recipient_mode_bulk"', 'id="bulk-recipient-panel"', 'name="file_bulk_recipient_listing"'):
            self.assertIn(marker, on)
            self.assertNotIn(marker, off)
        self.assertIn('id="recipient_mode_external"', off)  # existing choices untouched
        self.assertIn("Corporate only", on)

    def test_bulk_page_script_never_embeds_listing_numbers_and_wires_the_check_endpoint(self):
        on = self._intake(True)
        self.assertIn("/intake/bulk-listing/check", on)
        self.assertIn("BULK_RECIPIENT_TYPES = ['ACH', 'Same Day ACH', 'Wire']", on)
        _no_numbers(self, on)

    def _detail(self, **overrides):
        record = ir.DetailPageTests("test_24_legacy_transaction_without_recipient_fields_renders_as_external")._record(**overrides)
        case = ir.DetailPageTests("test_24_legacy_transaction_without_recipient_fields_renders_as_external")
        case.client = self.client
        with self.client.session_transaction() as sess:
            sess["role"] = "sam"
        return case._render(record).get_data(as_text=True)

    def test_detail_shows_a_bulk_summary_without_any_account_numbers(self):
        body = self._detail(recipient_mode="bulk", recv_payee_name="", recv_bank_name="", recv_account_name="",
                            recv_account_number="", recv_routing_number="", request_type="ACH",
                            bulk_recipient_rows=3, bulk_matched_rows=3, bulk_validated_at="2026-10-16 09:00",
                            attachments={"bulk_recipient_listing": "recipients.xlsx"},
                            attachment_urls={"bulk_recipient_listing": "https://sp.example/recipients.xlsx"})
        self.assertIn("Bulk Recipients", body)
        self.assertIn('id="bulk-row-count">3<', body)
        self.assertIn("All rows matched", body)
        self.assertIn("recipients.xlsx", body)
        self.assertIn("Not applicable", body)  # AVS / prior use
        self.assertNotIn("recv-acct-masked", body)  # no single-recipient account block
        self.assertNotIn(">Missing<", body.split("Recipient Account Listing")[1][:400])
        _no_numbers(self, body)

    def test_detail_for_historical_rows_is_unchanged(self):
        body = self._detail()
        self.assertIn("External Payee", body)
        self.assertNotIn("Bulk Recipients", body)

    def test_a_bulk_request_without_a_listing_shows_missing_and_unverified(self):
        body = self._detail(recipient_mode="bulk", bulk_recipient_rows=0, bulk_matched_rows=0,
                            recv_account_number="", recv_routing_number="")
        self.assertIn("Not verified", body)
        self.assertIn("Missing", body)


class IsolationTests(unittest.TestCase):
    def test_no_external_access_was_attempted(self):
        self.assertEqual(external_guard.state.blocked, [])


if __name__ == "__main__":
    unittest.main()
