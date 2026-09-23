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


class LiveWiringTests(unittest.TestCase):
    """
    2026-09-22: etransactions.BankAccountEvent now exists live. These tests
    confirm create_bank_account()/update_bank_account() actually write the
    lifecycle event atomically (same cursor/connection, before commit), not
    just that the pure helper functions above are correct in isolation.
    """

    def _new_account_data(self, entity_key, status="Open"):
        return {
            "entity_key": entity_key, "account_classification": "Operating",
            "bank_name": "Wells Fargo Bank", "account_name_id": "", "account_title": "Operating",
            "account_title_modifier": "", "system_account_name": "",
            "account_number": "12345678", "routing_number": "021000021",
            "transit_number_canada": "", "institution_number_canada": "",
            "gl_account_number": "", "gl_account_name": "", "tax_id_number": "",
            "address": "", "phone_number": "", "account_type": "Checking",
            "status": status, "date_opened": "2026-01-01", "date_closed": "",
            "bank_contact_name": "", "notes": "",
        }

    def _fake_conn_for_create(self, new_key=99):
        fake_cursor = MagicMock()
        fake_cursor.fetchone.return_value = [new_key]
        fake_conn = MagicMock()
        fake_conn.cursor.return_value = fake_cursor
        return fake_conn, fake_cursor

    def _fake_conn_for_update(self, previous_status):
        fake_cursor = MagicMock()
        fake_cursor.fetchone.return_value = (previous_status,)
        fake_conn = MagicMock()
        fake_conn.cursor.return_value = fake_cursor
        return fake_conn, fake_cursor

    def test_new_property_account_writes_created_event(self):
        fake_conn, fake_cursor = self._fake_conn_for_create(new_key=201)
        with patch.object(db, "get_connection", return_value=fake_conn):
            key = db.create_bank_account(self._new_account_data(entity_key=7), actor_user_key=20)
        self.assertEqual(key, 201)
        insert_calls = [c for c in fake_cursor.execute.call_args_list
                        if "INSERT INTO etransactions.BankAccountEvent" in c[0][0]]
        self.assertEqual(len(insert_calls), 1)
        params = insert_calls[0][0][1]
        self.assertEqual(params[0], 201)  # BankAccount_Key
        self.assertEqual(params[1], db.BANK_ACCOUNT_EVENT_CREATED)
        self.assertEqual(params[2], 20)  # PerformedBy_User_Key
        fake_conn.commit.assert_called_once()

    def test_new_corporate_account_writes_created_event(self):
        fake_conn, fake_cursor = self._fake_conn_for_create(new_key=202)
        with patch.object(db, "get_connection", return_value=fake_conn):
            key = db.create_bank_account(self._new_account_data(entity_key=3), actor_user_key=20)
        self.assertEqual(key, 202)
        insert_calls = [c for c in fake_cursor.execute.call_args_list
                        if "INSERT INTO etransactions.BankAccountEvent" in c[0][0]]
        self.assertEqual(len(insert_calls), 1)
        self.assertEqual(insert_calls[0][0][1][1], db.BANK_ACCOUNT_EVENT_CREATED)
        fake_conn.commit.assert_called_once()

    def test_closed_property_account_writes_closed_event(self):
        fake_conn, fake_cursor = self._fake_conn_for_update(previous_status="Open")
        data = self._new_account_data(entity_key=7, status="Closed")
        data["date_closed"] = "2026-09-22"
        with patch.object(db, "get_connection", return_value=fake_conn):
            db.update_bank_account(55, data, actor_user_key=21)
        insert_calls = [c for c in fake_cursor.execute.call_args_list
                        if "INSERT INTO etransactions.BankAccountEvent" in c[0][0]]
        self.assertEqual(len(insert_calls), 1)
        params = insert_calls[0][0][1]
        self.assertEqual(params[0], 55)
        self.assertEqual(params[1], db.BANK_ACCOUNT_EVENT_CLOSED)
        self.assertEqual(params[2], 21)
        self.assertEqual(params[5], "Open")     # Previous_Status
        self.assertEqual(params[6], "Closed")   # New_Status
        fake_conn.commit.assert_called_once()

    def test_closed_corporate_account_writes_closed_event(self):
        fake_conn, fake_cursor = self._fake_conn_for_update(previous_status="Open")
        data = self._new_account_data(entity_key=3, status="Closed")
        data["date_closed"] = "2026-09-22"
        with patch.object(db, "get_connection", return_value=fake_conn):
            db.update_bank_account(56, data, actor_user_key=21)
        insert_calls = [c for c in fake_cursor.execute.call_args_list
                        if "INSERT INTO etransactions.BankAccountEvent" in c[0][0]]
        self.assertEqual(len(insert_calls), 1)
        self.assertEqual(insert_calls[0][0][1][1], db.BANK_ACCOUNT_EVENT_CLOSED)
        fake_conn.commit.assert_called_once()

    def test_ordinary_edit_writes_no_lifecycle_event(self):
        # Status unchanged (Open -> Open) — no BankAccountEvent row at all.
        fake_conn, fake_cursor = self._fake_conn_for_update(previous_status="Open")
        data = self._new_account_data(entity_key=7, status="Open")
        data["notes"] = "Updated contact info"
        with patch.object(db, "get_connection", return_value=fake_conn):
            db.update_bank_account(57, data, actor_user_key=22)
        insert_calls = [c for c in fake_cursor.execute.call_args_list
                        if "INSERT INTO etransactions.BankAccountEvent" in c[0][0]]
        self.assertEqual(insert_calls, [])
        fake_conn.commit.assert_called_once()

    def test_event_insert_happens_before_commit_same_transaction(self):
        # #8: BankAccount write and BankAccountEvent write are on the same
        # cursor/connection and commit together — Power Automate never sees a
        # partially-applied state (event without the underlying account change,
        # or vice versa).
        fake_conn, fake_cursor = self._fake_conn_for_create(new_key=301)
        with patch.object(db, "get_connection", return_value=fake_conn):
            db.create_bank_account(self._new_account_data(entity_key=7), actor_user_key=20)
        insert_sql_calls = [c[0][0] for c in fake_cursor.execute.call_args_list]
        self.assertIn("INSERT INTO etransactions.BankAccount", insert_sql_calls[0])
        self.assertIn("INSERT INTO etransactions.BankAccountEvent", insert_sql_calls[1])
        fake_conn.commit.assert_called_once()

