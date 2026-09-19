"""
Tests for Batch 2: Bank Account lifecycle audit (BankAccountEvent), status
vocabulary cleanup (Open/Closed only), and classification/type field clarity.

etransactions.BankAccountEvent does not exist yet (see db.py's module-level
note above BANK_ACCOUNT_EVENT_CREATED) — these tests exercise the pure
decision/diff logic and the mocked-cursor insert helper, all of which are
correct and ready regardless of whether the table exists yet. No live DB
connection or the new table is required to run this file.

Run:  python -m unittest test_bank_account_events -v
"""

import unittest
from unittest.mock import MagicMock, patch

import db


class LifecycleEventDecisionTests(unittest.TestCase):
    """Part 6: prevent duplicate close events, detect reopen, ignore no-op status."""

    def test_7_open_to_closed_is_closed_event(self):
        self.assertEqual(
            db.bank_account_lifecycle_event_for_status_change("Open", "Closed"),
            db.BANK_ACCOUNT_EVENT_CLOSED,
        )

    def test_8_closed_to_closed_is_no_event(self):
        self.assertIsNone(db.bank_account_lifecycle_event_for_status_change("Closed", "Closed"))

    def test_open_to_open_is_no_event(self):
        self.assertIsNone(db.bank_account_lifecycle_event_for_status_change("Open", "Open"))

    def test_closed_to_open_is_reopened_event(self):
        self.assertEqual(
            db.bank_account_lifecycle_event_for_status_change("Closed", "Open"),
            db.BANK_ACCOUNT_EVENT_REOPENED,
        )

    def test_legacy_active_to_closed_is_still_closed_event(self):
        # Defensive: even if a stray legacy 'Active' value were ever encountered,
        # transitioning to Closed must still be recognized.
        self.assertEqual(
            db.bank_account_lifecycle_event_for_status_change("Active", "Closed"),
            db.BANK_ACCOUNT_EVENT_CLOSED,
        )


class ChangeSummaryTests(unittest.TestCase):
    """Part 1: ordinary edits summarize changed fields; sensitive fields are redacted."""

    def base_previous(self):
        return {
            "bankname": "Wells Fargo Bank", "accounttitle": "Operating Account",
            "accountnameid": "", "accounttitlemodifier": "", "systemaccountname": "",
            "accountnumber": "XXXX-XXXX-0123", "routingnumber": "xxxx",
            "transitnumbercanada": "", "institutionnumbercanada": "",
            "glaccountnumber": "", "glaccountname": "", "taxidnumber": "",
            "address": "", "phonenumber": "", "accounttype": "Checking",
            "accountclassification": "Operating", "bankcontactname": "", "notes": "",
        }

    def base_new_data(self):
        return {
            "bank_name": "Wells Fargo Bank", "account_title": "Operating Account",
            "account_name_id": "", "account_title_modifier": "", "system_account_name": "",
            "account_number": "", "routing_number": "", "transit_number_canada": "",
            "institution_number_canada": "", "gl_account_number": "", "gl_account_name": "",
            "tax_id_number": "", "address": "", "phone_number": "", "account_type": "Checking",
            "account_classification": "Operating", "bank_contact_name": "", "notes": "",
        }

    def test_15_unchanged_save_produces_no_audit_row(self):
        # A no-op save (nothing actually different) must return None so the
        # caller never creates a BANK_ACCOUNT_UPDATED row for it.
        result = db.summarize_bank_account_changes(self.base_previous(), self.base_new_data())
        self.assertIsNone(result)

    def test_5_ordinary_field_edit_is_detected(self):
        new_data = self.base_new_data()
        new_data["bank_contact_name"] = "Jane Smith"
        result = db.summarize_bank_account_changes(self.base_previous(), new_data)
        self.assertIsNotNone(result)
        self.assertIn("BankContactName", result)

    def test_sensitive_field_change_does_not_leak_real_value(self):
        new_data = self.base_new_data()
        new_data["account_number"] = "9876543210"  # a real, non-blank new value
        result = db.summarize_bank_account_changes(self.base_previous(), new_data)
        self.assertIn("AccountNumber changed: Yes", result)
        self.assertNotIn("9876543210", result)
        self.assertNotIn("XXXX-XXXX-0123", result)

    def test_sensitive_field_left_blank_is_not_a_change(self):
        # Blank sensitive field = "keep existing" per update_bank_account()'s
        # own convention — must not be reported as a change.
        result = db.summarize_bank_account_changes(self.base_previous(), self.base_new_data())
        self.assertIsNone(result)

    def test_service_flag_boolean_change_is_detected(self):
        new_data = self.base_new_data()
        new_data["WireModuleEnabled"] = True
        previous = self.base_previous()
        previous["wiremoduleenabled"] = False
        result = db.summarize_bank_account_changes(previous, new_data)
        self.assertIn("WireModuleEnabled", result)


class RecordBankAccountEventTests(unittest.TestCase):
    """
    Part 3: BankAccountEvent must carry BankAccount_Key, actor, timestamp, and
    (for status transitions) Previous_Status/New_Status — enough for Power
    Automate to resolve BankAccount -> BusinessEntity.Classification itself.
    """

    def test_12_13_14_event_carries_key_actor_timestamp(self):
        fake_cursor = MagicMock()
        db.record_bank_account_event(
            fake_cursor, 42,
            event_type=db.BANK_ACCOUNT_EVENT_CLOSED,
            performed_by_user_key=7,
            previous_status="Open", new_status="Closed",
            change_detail=None,
        )
        sql, params = fake_cursor.execute.call_args[0]
        self.assertIn("INSERT INTO etransactions.BankAccountEvent", sql)
        self.assertEqual(params[0], 42)  # BankAccount_Key
        self.assertEqual(params[1], db.BANK_ACCOUNT_EVENT_CLOSED)
        self.assertEqual(params[2], 7)  # PerformedBy_User_Key
        self.assertIsNotNone(params[3])  # Event_DateTime populated
        self.assertEqual(params[5], "Open")
        self.assertEqual(params[6], "Closed")

    def test_does_not_open_its_own_connection(self):
        # Atomicity requirement: this function must use the CALLER's cursor,
        # never db.get_connection() itself, so it can share the caller's
        # transaction once wired into create/update/close.
        with patch.object(db, "get_connection") as mock_get_connection:
            fake_cursor = MagicMock()
            db.record_bank_account_event(
                fake_cursor, 1, event_type=db.BANK_ACCOUNT_EVENT_CREATED, performed_by_user_key=1,
            )
        mock_get_connection.assert_not_called()


class StatusVocabularyTests(unittest.TestCase):
    """Part 4: canonical status is Open/Closed only; 'Active' is no longer written or queried for."""

    def test_get_bank_accounts_query_no_longer_references_active(self):
        fake_cursor = MagicMock()
        fake_cursor.fetchall.return_value = []
        fake_conn = MagicMock()
        fake_conn.cursor.return_value = fake_cursor

        with patch.object(db, "get_connection", return_value=fake_conn):
            db.get_bank_accounts()

        sql = fake_cursor.execute.call_args[0][0]
        self.assertIn("Status = 'Open'", sql)
        self.assertNotIn("Active", sql)

    def test_search_bank_account_records_status_filter_no_longer_references_active(self):
        fake_cursor = MagicMock()
        fake_cursor.fetchall.return_value = []
        fake_conn = MagicMock()
        fake_conn.cursor.return_value = fake_cursor

        with patch.object(db, "get_connection", return_value=fake_conn):
            db.search_bank_account_records({"status": "Open"})

        sql, params = fake_cursor.execute.call_args[0]
        self.assertIn("ba.Status = ?", sql)
        self.assertNotIn("Active", sql)
        self.assertIn("Open", params)

    def test_search_bank_account_records_resolves_classification_for_power_automate(self):
        # #16 — Power Automate needs Property/Corporate resolvable from the
        # BankAccount/BusinessEntity relationship.
        fake_cursor = MagicMock()
        fake_cursor.fetchall.return_value = []
        fake_conn = MagicMock()
        fake_conn.cursor.return_value = fake_cursor

        with patch.object(db, "get_connection", return_value=fake_conn):
            db.search_bank_account_records({})

        sql = fake_cursor.execute.call_args[0][0]
        self.assertIn("BusinessEntity", sql)
        self.assertIn("be.Classification", sql)

    def test_no_delete_statements_in_bank_account_write_paths(self):
        # #10 — closing/editing must never delete historical BankAccount rows.
        import inspect
        for fn in (db.create_bank_account, db.update_bank_account):
            source = inspect.getsource(fn)
            self.assertNotIn("DELETE", source.upper())
