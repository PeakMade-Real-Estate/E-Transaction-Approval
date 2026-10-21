"""
Item 7 - Entity ID controlled dropdown with a manual fallback.

The list comes from the existing active etransactions.BusinessEntity rows (db.get_business_entities); the
server re-validates every listed choice, a manual entry is validated as free text, and the Draft lifecycle
restores either form of the value. No database schema or master data is written.

Mock-only: `external_guard` (imported first) makes SQL/SharePoint/network inert.

Run:  python -m unittest test_entity_dropdown -v
"""
import inspect
import os
import re
import unittest
import external_guard  # noqa: F401  (must precede app/db/sharepoint imports)
from unittest.mock import MagicMock, patch

from werkzeug.datastructures import MultiDict

import app as app_module
import db
import export
import test_draft_persistence as dp
import test_internal_recipient as ir
import test_prior_use_verification as pu
import workflow as wf

MANUAL = app_module.ENTITY_CHOICE_MANUAL
_REAL_GET_BUSINESS_ENTITIES = db.get_business_entities
MANUAL_LABEL = app_module.ENTITY_MANUAL_LABEL
ENTITIES = [
    {"entity_key": 3, "entity_id": "CORP-001", "name": "Treasury / Corporate Finance", "classification": "Corporate"},
    {"entity_key": 4, "entity_id": "245", "name": "BEACH CLUB", "classification": "Property"},
    {"entity_key": 7, "entity_id": "124", "name": "THE TIMBERS", "classification": "Property"},
    {"entity_key": 9, "entity_id": "1000", "name": "AVANT", "classification": "Property"},
]
ROOT = os.path.dirname(os.path.abspath(__file__))


def _form(**data):
    return MultiDict(data)


class EntityOptionsTests(unittest.TestCase):
    def _options(self, rows):
        with patch.object(app_module.db, "get_business_entities", return_value=rows) as q:
            return app_module._get_entity_options_safe(), q

    def test_options_come_from_the_active_sql_entities_with_id_and_name(self):
        options, query = self._options(ENTITIES)
        query.assert_called_once_with(active_only=True)
        self.assertEqual({o["entity_id"] for o in options}, {"CORP-001", "245", "124", "1000"})
        beach = next(o for o in options if o["entity_id"] == "245")
        self.assertEqual(beach["label"], "245 \u2014 BEACH CLUB")
        self.assertEqual(beach["name"], "BEACH CLUB")

    def test_numeric_ids_sort_numerically_before_text_ids(self):
        options, _ = self._options(ENTITIES)
        self.assertEqual([o["entity_id"] for o in options], ["124", "245", "1000", "CORP-001"])

    def test_blank_and_duplicate_ids_are_left_out_and_whitespace_is_trimmed(self):
        rows = [{"entity_id": "  ", "name": "No ID"}, {"entity_id": None, "name": "Null ID"},
                {"entity_id": " 245 ", "name": "BEACH CLUB"}, {"entity_id": "245", "name": "Duplicate"},
                {"entity_id": "300", "name": ""}]
        options, _ = self._options(rows)
        self.assertEqual([(o["entity_id"], o["label"]) for o in options], [("245", "245 \u2014 BEACH CLUB"), ("300", "300")])

    def test_an_unavailable_database_gives_an_empty_list_not_an_error(self):
        with patch.object(app_module.db, "get_business_entities", side_effect=RuntimeError("db down")):
            self.assertEqual(app_module._get_entity_options_safe(), [])
        with patch.object(app_module, "database_enabled", return_value=False), \
             patch.object(app_module.db, "get_business_entities") as query:
            self.assertEqual(app_module._get_entity_options_safe(), [])
        query.assert_not_called()

    def test_the_sql_query_reads_only_active_business_entities(self):
        cur = MagicMock()
        cur.fetchall.return_value = [(3, "CORP-001", "Treasury", "Corporate")]
        conn = MagicMock()
        conn.cursor.return_value = cur
        with patch.object(db, "get_connection", return_value=conn):
            rows = db.get_business_entities(active_only=True)
        sql, params = cur.execute.call_args[0]
        self.assertIn("FROM etransactions.BusinessEntity", sql)
        self.assertIn("Active_Status = ?", sql)
        self.assertEqual(params, [1])
        self.assertRegex(sql.strip(), r"^SELECT ")
        self.assertEqual(rows[0]["entity_id"], "CORP-001")
        self.assertEqual(cur.execute.call_count, 1)


class ResolveEntityIdTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(app_module.db, "get_business_entities", return_value=ENTITIES)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_listed_choice_resolves_to_the_canonical_id_and_ignores_the_manual_text(self):
        self.assertEqual(app_module._resolve_entity_id(_form(entity_choice="245", entity_id="HACK")), ("245", None))
        self.assertEqual(app_module._resolve_entity_id(_form(entity_choice=" CORP-001 ")), ("CORP-001", None))

    def test_a_forged_or_unknown_choice_is_rejected_and_never_converted(self):
        for forged in ("999", "245 \u2014 BEACH CLUB", "corp-001", "0245", "'; DROP TABLE x;--"):
            with self.subTest(forged=forged):
                value, error = app_module._resolve_entity_id(_form(entity_choice=forged, entity_id="245"))
                self.assertEqual(value, "")
                self.assertIn("not a valid entity", error)

    def test_an_inactive_or_unlisted_entity_cannot_be_chosen_as_a_listed_choice(self):
        with patch.object(app_module.db, "get_business_entities", return_value=[]):
            value, error = app_module._resolve_entity_id(_form(entity_choice="245"))
        self.assertEqual(value, "")
        self.assertIn("not a valid entity", error)

    def test_no_selection_is_allowed_and_stores_nothing(self):
        self.assertEqual(app_module._resolve_entity_id(_form(entity_choice="", entity_id="stray")), ("", None))

    def test_a_manual_entry_is_trimmed_and_does_not_need_to_be_listed(self):
        self.assertEqual(app_module._resolve_entity_id(_form(entity_choice=MANUAL, entity_id="  FORMER-17 ")), ("FORMER-17", None))

    def test_a_manual_choice_needs_a_value_within_the_column_limits(self):
        for bad, message in (("", "Enter the Entity ID"), ("   ", "Enter the Entity ID"),
                             ("X" * 101, "100 characters"), ("AB\x00C", "invalid characters"), ("AB\nC", "invalid characters")):
            with self.subTest(bad=bad[:8]):
                value, error = app_module._resolve_entity_id(_form(entity_choice=MANUAL, entity_id=bad))
                self.assertEqual(value, "")
                self.assertIn(message, error)
        self.assertEqual(app_module._resolve_entity_id(_form(entity_choice=MANUAL, entity_id="X" * 100))[1], None)

    def test_a_post_without_the_choice_field_is_treated_as_manual_text_as_before(self):
        self.assertEqual(app_module._resolve_entity_id(_form(entity_id=" SRA-007 ")), ("SRA-007", None))
        self.assertEqual(app_module._resolve_entity_id(_form()), ("", None))

    def test_property_requests_are_not_subject_to_the_entity_selection(self):
        errors = []
        app_module._validate_entity_selection(_form(classification="property", entity_choice="999"), errors)
        self.assertEqual(errors, [])
        app_module._validate_entity_selection(_form(classification="corporate", entity_choice="999"), errors)
        self.assertEqual(len(errors), 1)

    def test_the_persisted_value_is_never_an_unvalidated_one(self):
        self.assertEqual(app_module._intake_property_fields(_form(classification="corporate", entity_choice="999",
                                                                  entity_id="245"))["property_code"], "")
        self.assertEqual(app_module._intake_property_fields(_form(classification="corporate", entity_choice="245"))["property_code"], "245")

    def test_resume_state_reflects_listed_manual_historical_and_blank_values(self):
        options = app_module._get_entity_options_safe()
        with app_module.app.test_request_context("/intake"):
            state = app_module._entity_form_state
            self.assertEqual(state({"classification": "corporate", "property_code": "245"}, options), {"choice": "245", "manual": ""})
            self.assertEqual(state({"classification": "corporate", "property_code": "OLD-9"}, options),
                             {"choice": MANUAL, "manual": "OLD-9"})
            self.assertEqual(state({"classification": "", "property_code": "OLD-9"}, options), {"choice": MANUAL, "manual": "OLD-9"})
            self.assertEqual(state({"classification": "corporate", "property_code": ""}, options), {"choice": "", "manual": ""})
            self.assertEqual(state({"classification": "property", "property_code": "124"}, options), {"choice": "", "manual": ""})
            self.assertEqual(state(None, options), {"choice": "", "manual": ""})


class _EntityBase(dp._Base):
    def setUp(self):
        super().setUp()
        self.entities = list(ENTITIES)
        self.m["get_business_entities"] = self._start(app_module.db, "get_business_entities",
                                                      side_effect=lambda active_only=True: list(self.entities))
        self._start(app_module.sharepoint, "get_properties", return_value=[{"entity_number": 124, "name": "THE TIMBERS"}])

    def _start(self, target, name, **kw):
        patcher = patch.object(target, name, **kw)
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    def entity_form(self, **overrides):
        return dp._draft_form(**overrides)

    def stored_code(self):
        return self.store.drafts[55]["property_code"]


class IntakePageTests(_EntityBase):
    def _page(self):
        return self.client.get("/intake").get_data(as_text=True)

    def test_the_dropdown_is_populated_from_sql_with_canonical_values_and_readable_labels(self):
        body = self._page()
        self.assertRegex(body, r'<label[^>]*for="entity_choice">Entity ID</label>')
        for entity_id, label in (("245", "245 \u2014 BEACH CLUB"), ("124", "124 \u2014 THE TIMBERS"),
                                 ("CORP-001", "CORP-001 \u2014 Treasury / Corporate Finance")):
            self.assertRegex(body, r'<option value="%s"[^>]*>\s*%s</option>' % (re.escape(entity_id), re.escape(label)))
        self.assertNotIn("Property Code", body)
        self.assertNotRegex(body, r'<option value="[^"]*BEACH CLUB')          # the label is never the submitted value

    def test_each_option_carries_its_entity_name_for_the_department_autofill(self):
        body = self._page()
        self.assertIn('data-name="BEACH CLUB"', body)
        self.assertIn('data-name="Treasury / Corporate Finance"', body)
        self.assertIn('id="dept-from-entity-note"', body)
        script = body.split("<script>", 1)[1].split("function syncEntityChoice", 1)[1].split("document.getElementById('entity_search')", 1)[0]
        self.assertIn("dept.value = listedName", script)
        self.assertIn("dept.readOnly = true", script)
        self.assertIn("dept.dataset.autofilled === '1'", script)   # an autofilled name is cleared again when the entity is dropped

    def test_each_option_carries_lowercase_search_text_for_id_and_name(self):
        body = self._page()
        self.assertIn('data-search="245 beach club"', body)
        self.assertIn('data-search="corp-001 treasury / corporate finance"', body)
        self.assertRegex(body, r'<input type="search"[^>]*id="entity_search"')
        self.assertNotRegex(re.search(r'<input type="search"[^>]*>', body).group(0), r'\bname=')   # the search box is never submitted

    def test_the_manual_fallback_option_and_input_are_offered(self):
        body = self._page()
        self.assertRegex(body, r'<option value="%s"[^>]*>%s</option>' % (re.escape(MANUAL), re.escape(MANUAL_LABEL)))
        manual = dp._tag(body, "entity_id")
        self.assertIn('name="entity_id"', manual)
        self.assertIn('maxlength="100"', manual)
        self.assertIn("display:none", manual)
        self.assertEqual(dp._selected_option(body, "entity_choice"), "")

    def test_the_list_is_never_hard_coded_in_the_template(self):
        with open(os.path.join(ROOT, "templates", "intake.html"), encoding="utf-8") as handle:
            source = handle.read()
        for hard_coded in ("BEACH CLUB", "THE TIMBERS", "CORP-001", "SRA-001"):
            self.assertNotIn(hard_coded, source)
        self.entities.clear()
        body = self._page()
        self.assertNotIn("data-search=", body)
        self.assertIn("list is not available right now", body)
        self.assertIn(MANUAL_LABEL, body)

    def test_the_intake_script_keeps_its_structure_around_the_entity_logic(self):
        # Guards against a spliced edit: no JS engine is available to the suite, so check the anchors textually.
        script = self._page().split("<script>", 1)[1]
        self.assertIn("function updateInstructionsRequirement() {\n        const reqType = document.getElementById('request_type')", script)
        validator = script.split("function collectClientSideErrors() {", 1)[1].split("// Section C", 1)[0]
        self.assertIn("entity_choice')?.value === '__manual__'", validator)
        self.assertEqual(validator.count("{"), validator.count("}"))
        entity_js = script.split("function filterEntityOptions", 1)[1].split("// ── Urgent", 1)[0]
        self.assertEqual(entity_js.count("{"), entity_js.count("}"))
        self.assertEqual(entity_js.count("("), entity_js.count(")"))
        self.assertEqual(script.count("{"), script.count("}"))

    def test_a_failed_entity_query_still_renders_the_form_with_the_manual_fallback(self):
        self.m["get_business_entities"].side_effect = RuntimeError("db down")
        resp = self.client.get("/intake")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(MANUAL_LABEL, resp.get_data(as_text=True))

    def test_a_rejected_post_re_renders_the_manual_entry_and_the_listed_choice(self):
        resp = self.save(self.entity_form(entity_choice=MANUAL, entity_id="OLD-9", payment_purpose="", urgent="no", classification="corporate"),
                         files=None)
        self.assertEqual(resp.status_code, 302)   # a draft only needs valid data, so it saved
        resp = self.client.post("/intake/submit", data=dp._final_form(entity_choice=MANUAL, entity_id="OLD-9", payment_purpose="",
                                                                      request_id=self.store.drafts[55]["request_id"]),
                                content_type="multipart/form-data")
        body = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(dp._selected_option(body, "entity_choice"), MANUAL)
        self.assertEqual(dp._value(body, "entity_id"), "OLD-9")
        resp = self.client.post("/intake/submit", data=dp._final_form(entity_choice="245", payment_purpose="",
                                                                      request_id=self.store.drafts[55]["request_id"]),
                                content_type="multipart/form-data")
        self.assertEqual(dp._selected_option(resp.get_data(as_text=True), "entity_choice"), "245")


class DraftLifecycleTests(_EntityBase):
    def test_a_listed_entity_is_saved_and_restored_on_resume(self):
        self.assertEqual(self.save(self.entity_form(entity_choice="245", entity_id="")).status_code, 302)
        self.assertEqual(self.stored_code(), "245")
        body = self.resume()
        self.assertEqual(dp._selected_option(body, "entity_choice"), "245")
        self.assertEqual(dp._value(body, "entity_id"), "")

    def test_a_manual_entity_is_saved_and_restored_as_manual_entry(self):
        self.assertEqual(self.save(self.entity_form(entity_choice=MANUAL, entity_id=" FORMER-17 ")).status_code, 302)
        self.assertEqual(self.stored_code(), "FORMER-17")
        body = self.resume()
        self.assertEqual(dp._selected_option(body, "entity_choice"), MANUAL)
        self.assertEqual(dp._value(body, "entity_id"), "FORMER-17")

    def test_a_forged_listed_choice_is_rejected_and_nothing_is_saved(self):
        resp = self.save(self.entity_form(entity_choice="999", entity_id=""))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("not a valid entity", resp.get_data(as_text=True))
        self.assertEqual(self.store.created, [])
        self.assertNotIn("success", {cat for cat, _ in self.flashes()})

    def test_a_forged_choice_cannot_overwrite_a_stored_draft_value(self):
        self.save(self.entity_form(entity_choice="245"))
        resp = self.save(self.entity_form(transaction_key="55", request_id="x", entity_choice="777"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.stored_code(), "245")
        self.assertEqual(self.store.updated, [])

    def test_manual_choice_without_a_value_or_with_too_long_a_value_is_rejected(self):
        for value in ("", "  ", "Z" * 101):
            with self.subTest(value=value[:5]):
                self.assertEqual(self.save(self.entity_form(entity_choice=MANUAL, entity_id=value)).status_code, 200)
        self.assertEqual(self.store.created, [])

    def test_no_entity_selection_saves_cleanly_and_stays_empty(self):
        self.assertEqual(self.save(self.entity_form(entity_choice="", entity_id="stray text")).status_code, 302)
        self.assertEqual(self.stored_code(), "")
        self.assertEqual(dp._selected_option(self.resume(), "entity_choice"), "")

    def test_a_listed_choice_always_stores_the_canonical_id_not_the_posted_manual_text(self):
        self.save(self.entity_form(entity_choice="245", entity_id="SOMETHING-ELSE"))
        self.assertEqual(self.stored_code(), "245")

    def test_repeated_saves_keep_the_selected_value(self):
        for choice, manual, expected in (("124", "", "124"), (MANUAL, "OLD-9", "OLD-9")):
            with self.subTest(choice=choice):
                self.store.drafts.clear()
                self.store.next_key = 55
                self.save(self.entity_form(entity_choice=choice, entity_id=manual))
                for _ in range(4):
                    body = self.resume()
                    form = self.entity_form(transaction_key=dp._value(body, "transaction_key"), request_id=dp._value(body, "request_id"),
                                            entity_choice=dp._selected_option(body, "entity_choice"), entity_id=dp._value(body, "entity_id"))
                    self.assertEqual(self.save(form).status_code, 302)
                self.assertEqual(len(self.store.drafts), 1)
                self.assertEqual(self.stored_code(), expected)

    def test_editing_the_entity_keeps_every_unrelated_field_and_document(self):
        self.save(self.entity_form(entity_choice="245"), files={"file_payment_support": dp._file("inv.pdf")})
        before = {k: v for k, v in self.store.drafts[55].items() if k not in ("property_code", "property_dept")}
        form = self.entity_form(transaction_key="55", request_id=self.store.drafts[55]["request_id"], entity_choice="124")
        self.assertEqual(self.save(form).status_code, 302)
        after = self.store.drafts[55]
        self.assertEqual(after["property_code"], "124")
        self.assertEqual(after["property_dept"], "THE TIMBERS")      # follows the newly selected entity
        for key, value in before.items():
            self.assertEqual(after[key], value, key)
        self.assertEqual(len(self.sp.uploads), 1)                  # the stored document was not re-uploaded
        body = self.resume()
        self.assertIn("<strong>inv.pdf</strong>", body)
        self.assertEqual(dp._value(body, "avs_score"), "95")
        self.assertEqual(dp._value(body, "verbal_contact_name"), "John Doe")

    def test_a_historical_value_that_is_no_longer_listed_is_kept_as_manual_entry(self):
        self.save(self.entity_form(entity_choice="245"))
        self.entities[:] = [e for e in self.entities if e["entity_id"] != "245"]       # deactivated since
        body = self.resume()
        self.assertEqual(dp._selected_option(body, "entity_choice"), MANUAL)
        self.assertEqual(dp._value(body, "entity_id"), "245")
        self.assertEqual(self.save(self.entity_form(transaction_key="55", request_id="x", entity_choice=MANUAL, entity_id="245")).status_code, 302)
        self.assertEqual(self.stored_code(), "245")

    def test_a_property_request_keeps_its_property_code_and_ignores_the_entity_controls(self):
        form = self.entity_form(classification="property", property_entity_number="124", property_dept="", entity_choice="999")
        self.assertEqual(self.save(form).status_code, 302)
        self.assertEqual(self.stored_code(), "124")
        self.assertEqual(self.store.drafts[55]["property_dept"], "THE TIMBERS")
        self.assertEqual(dp._selected_option(self.resume(), "entity_choice"), "")

    def test_the_corporate_department_is_typed_for_manual_or_no_entity_but_comes_from_sql_for_a_listed_one(self):
        self.save(self.entity_form(entity_choice=MANUAL, entity_id="OLD-9", property_dept="Treasury"))
        self.assertEqual(dp._value(self.resume(), "property_dept"), "Treasury")
        self.save(self.entity_form(transaction_key="55", request_id="x", entity_choice="", property_dept="Accounting"))
        self.assertEqual(dp._value(self.resume(), "property_dept"), "Accounting")
        self.save(self.entity_form(transaction_key="55", request_id="x", entity_choice="245", property_dept="Forged Dept"))
        self.assertEqual(self.store.drafts[55]["property_dept"], "BEACH CLUB")
        self.assertEqual(dp._value(self.resume(), "property_dept"), "BEACH CLUB")

    def test_a_listed_entity_supplies_the_department_even_when_none_is_posted(self):
        self.assertEqual(self.save(self.entity_form(entity_choice="CORP-001", property_dept="")).status_code, 302)
        self.assertEqual(self.store.drafts[55]["property_dept"], "Treasury / Corporate Finance")

    def test_repeated_saves_of_a_listed_entity_keep_the_same_department(self):
        self.save(self.entity_form(entity_choice="124", property_dept=""))
        for _ in range(3):
            body = self.resume()
            form = self.entity_form(transaction_key="55", request_id="x", entity_choice=dp._selected_option(body, "entity_choice"),
                                    property_dept=dp._value(body, "property_dept"))
            self.assertEqual(self.save(form).status_code, 302)
        self.assertEqual((self.stored_code(), self.store.drafts[55]["property_dept"]), ("124", "THE TIMBERS"))


class FinalSubmissionTests(_EntityBase):
    def _ready(self):
        self.save()
        rid = self.store.drafts[55]["request_id"]
        self.sp.seed(rid, ve=("avs.png", dp.sharepoint.DOC_TYPE_AVS_SCREENSHOT),
                     wire=("w.pdf", dp.sharepoint.DOC_TYPE_WIRE_ACH_INSTRUCTIONS), pay=("inv.pdf", dp.sharepoint.DOC_TYPE_PAYMENT_SUPPORT))
        return rid

    def _finalize(self, **overrides):
        form = dp._final_form(request_id=self.store.drafts[55]["request_id"], **overrides)
        return self.client.post("/intake/submit", data=form, content_type="multipart/form-data")

    def test_a_listed_entity_is_submitted_with_the_canonical_id_and_the_normal_workflow_data(self):
        self._ready()
        self.assertEqual(self._finalize(entity_choice="245").status_code, 302)
        (key, data), kwargs = self.m["finalize_draft_submission"].call_args
        self.assertEqual((key, data["property_code"], data["classification"]), (55, "245", "corporate"))
        self.assertEqual(data["approval_rule_key"], ir._RULE["approval_rule_key"])
        self.assertEqual(kwargs, {"prepared_by_user_key": dp.USER["user_key"]})

    def test_a_manual_entity_is_submitted_without_being_listed(self):
        self._ready()
        self.assertEqual(self._finalize(entity_choice=MANUAL, entity_id="FORMER-17").status_code, 302)
        self.assertEqual(self.m["finalize_draft_submission"].call_args[0][1]["property_code"], "FORMER-17")

    def test_a_listed_entity_satisfies_the_required_department_and_supplies_its_name(self):
        self._ready()
        self.assertEqual(self._finalize(entity_choice="245", property_dept="").status_code, 302)
        data = self.m["finalize_draft_submission"].call_args[0][1]
        self.assertEqual((data["property_code"], data["property_dept"]), ("245", "BEACH CLUB"))

    def test_a_forged_department_is_replaced_by_the_listed_entitys_name_at_submit(self):
        self._ready()
        self._finalize(entity_choice="124", property_dept="Someone Else")
        self.assertEqual(self.m["finalize_draft_submission"].call_args[0][1]["property_dept"], "THE TIMBERS")

    def test_the_department_is_still_required_for_manual_or_no_entity(self):
        self._ready()
        for overrides in ({"entity_choice": MANUAL, "entity_id": "FORMER-17"}, {"entity_choice": ""}, {}):
            with self.subTest(overrides=overrides):
                resp = self._finalize(property_dept="", **overrides)
                self.assertEqual(resp.status_code, 200)
                self.assertIn("Department is required", resp.get_data(as_text=True))
        self.m["finalize_draft_submission"].assert_not_called()

    def test_a_forged_listed_choice_gets_no_department_from_the_entity_list(self):
        self._ready()
        resp = self._finalize(entity_choice="999", property_dept="")
        body = resp.get_data(as_text=True)
        self.assertIn("not a valid entity", body)
        self.assertIn("Department is required", body)
        self.m["finalize_draft_submission"].assert_not_called()

    def test_a_forged_listed_choice_blocks_final_submission(self):
        self._ready()
        resp = self._finalize(entity_choice="999")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("not a valid entity", resp.get_data(as_text=True))
        self.m["finalize_draft_submission"].assert_not_called()
        self.m["insert_transaction"].assert_not_called()

    def test_an_empty_manual_choice_blocks_final_submission(self):
        self._ready()
        resp = self._finalize(entity_choice=MANUAL, entity_id="")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Enter the Entity ID", resp.get_data(as_text=True))
        self.m["finalize_draft_submission"].assert_not_called()

    def test_no_entity_selection_remains_allowed_because_the_field_was_always_optional(self):
        self._ready()
        self.assertEqual(self._finalize(entity_choice="").status_code, 302)
        self.assertEqual(self.m["finalize_draft_submission"].call_args[0][1]["property_code"], "")

    def test_a_direct_new_submission_stores_the_canonical_entity_too(self):
        form = ir._external_form(entity_choice="124", verbal_confirmed_with_known="on")
        resp = self.client.post("/intake/submit", data={**form, **ir._files()}, content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.m["insert_transaction"].call_args[0][0]["property_code"], "124")

    def test_selecting_an_entity_does_not_change_the_authorization_or_entity_key_inputs(self):
        source = inspect.getsource(app_module._resolve_entity_id) + inspect.getsource(app_module._entity_form_state)
        for sensitive in ("current_roles", "has_role", "Entity_Key", "resolve_entity_key", "approver", "WorkflowEvent"):
            self.assertNotIn(sensitive, source)
        self._ready()
        self._finalize(entity_choice="245")
        data = self.m["finalize_draft_submission"].call_args[0][1]
        self.assertNotIn("entity_key", data)


class ItemSixIsolationTests(_EntityBase):
    def test_the_entity_code_never_touches_the_bulk_recipient_feature(self):
        for function in (app_module._get_entity_options_safe, app_module._check_manual_entity_id, app_module._resolve_entity_id,
                         app_module._validate_entity_selection, app_module._entity_form_state):
            source = inspect.getsource(function).lower()
            for bulk in ("bulk", "recipient_mode", "bulkrecipient"):
                self.assertNotIn(bulk, source, function.__name__)

    def test_the_entity_dropdown_renders_the_same_with_the_bulk_gate_on_or_off(self):
        for gate in (False, True):
            with self.subTest(gate=gate), patch.object(app_module, "bulk_recipient_enabled", return_value=gate):
                body = self.client.get("/intake").get_data(as_text=True)
                self.assertIn('id="entity_choice"', body)
                self.assertIn(MANUAL_LABEL, body)
                self.assertEqual("BULK_RECIPIENT_ENABLED" in body, False)

    def test_a_draft_saves_with_the_bulk_gate_off_and_an_entity_selected(self):
        with patch.object(app_module, "bulk_recipient_enabled", return_value=False):
            self.assertEqual(self.save(self.entity_form(entity_choice="245")).status_code, 302)
        self.assertEqual(self.stored_code(), "245")

    def test_no_entity_value_is_ever_written_to_the_entity_master_table(self):
        source = inspect.getsource(app_module._resolve_entity_id)
        self.assertNotIn("INSERT", source.upper())
        db_source = inspect.getsource(_REAL_GET_BUSINESS_ENTITIES)
        self.assertNotRegex(db_source, r"\b(INSERT|UPDATE|DELETE|MERGE)\b")


class DisplayAndReportingTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def test_a_historical_entity_id_missing_from_the_master_list_still_shows_on_the_detail_page(self):
        case = pu.ApproverViewPriorUseBannerRenderTests("test_new_instructions_shows_no_prior_use_banner")
        record = case._fake_record(property_code="LEGACY-42", property_dept="Old Property")
        body = case._get(record).get_data(as_text=True)
        self.assertIn("Entity ID: LEGACY-42", body)
        self.assertNotIn("Code: LEGACY-42", body.replace("Entity ID: LEGACY-42", ""))

    def test_the_dashboard_still_shows_a_historical_entity_id(self):
        base = {"status": wf.STATUS_PENDING_APPROVER, "property_dept": "Old Property", "assigned_approver": "Alice",
                "amount": 500.0, "submitted_date": "2026-01-01", "days_pending": 1, "request_type": "ACH",
                "urgent": False, "urgency_reason": ""}
        records = [dict(base, request_id="TXN-OLD", property_code="LEGACY-42"),
                   dict(base, request_id="TXN-NEW", property_code="245")]
        with self.client.session_transaction() as sess:
            sess["role"] = "sam"
        with patch.object(app_module, "current_app_user_key", return_value=200), \
             patch.object(app_module.db, "get_dashboard_records", return_value=records):
            body = self.client.get("/dashboard").get_data(as_text=True)
        self.assertIn("LEGACY-42", body)
        self.assertIn(">245<", body.replace(" ", "").replace("\n", ""))

    def test_the_csv_export_keeps_a_historical_or_manual_entity_id(self):
        import csv
        import io
        records = [{"request_id": "TXN-1", "property_dept": "Old", "property_code": "LEGACY-42"},
                   {"request_id": "TXN-2", "property_dept": "New", "property_code": "245"}]
        rows = list(csv.reader(io.StringIO(export.build_dashboard_export_csv(records).decode("utf-8-sig"))))
        column = [h for h, _ in export.EXPORT_COLUMNS].index("Entity ID")
        self.assertEqual([r[column] for r in rows[1:]], ["LEGACY-42", "245"])

    def test_no_code_path_rewrites_or_validates_stored_entity_ids_against_the_list(self):
        for source in (inspect.getsource(db.get_request_detail), inspect.getsource(db.get_dashboard_records)):
            self.assertNotIn("get_business_entities", source)


class ExternalIsolationTests(unittest.TestCase):
    def test_no_external_access_was_attempted(self):
        self.assertEqual(external_guard.state.blocked, [])


if __name__ == "__main__":
    unittest.main()
