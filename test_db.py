"""
Regression tests for db.py query-construction bugs that unittest coverage of
workflow.py's pure functions can't catch (those tests never hit get_connection()).

Run:  python -m unittest test_db -v
"""

import unittest
from unittest.mock import MagicMock, patch

import db
import workflow as wf


class RfiOriginQueryTests(unittest.TestCase):
    def test_queries_the_actual_more_info_event_type(self):
        """
        Regression: this query previously filtered on Event_Type = 'RequestMoreInfo',
        a value the app never writes (advance_transaction_workflow always writes
        workflow.ACTION_MORE_INFO = 'more_info'), so it always returned None and
        requester_respond always restarted at Pending Approver instead of returning
        to the stage that actually requested more information.
        """
        fake_cursor = MagicMock()
        fake_cursor.fetchone.return_value = ("Pending Controller",)
        fake_conn = MagicMock()
        fake_conn.cursor.return_value = fake_cursor

        with patch.object(db, "get_connection", return_value=fake_conn):
            result = db.get_last_rfi_origin_status(123)

        executed_sql, executed_params = fake_cursor.execute.call_args[0]
        self.assertIn("Event_Type = ?", executed_sql)
        self.assertNotIn("RequestMoreInfo", executed_sql)
        self.assertEqual(executed_params, [123, wf.EVENT_RFI_REQUESTED])
        self.assertEqual(result, "Pending Controller")

    def test_returns_none_when_no_prior_rfi_event(self):
        fake_cursor = MagicMock()
        fake_cursor.fetchone.return_value = None
        fake_conn = MagicMock()
        fake_conn.cursor.return_value = fake_cursor

        with patch.object(db, "get_connection", return_value=fake_conn):
            result = db.get_last_rfi_origin_status(123)

        self.assertIsNone(result)


class BankAccountStatusQueryTests(unittest.TestCase):
    def test_returns_status_for_existing_account(self):
        fake_cursor = MagicMock()
        fake_cursor.fetchone.return_value = ("Open",)
        fake_conn = MagicMock()
        fake_conn.cursor.return_value = fake_cursor

        with patch.object(db, "get_connection", return_value=fake_conn):
            result = db.get_bank_account_status(42)

        self.assertEqual(result, "Open")

    def test_returns_none_for_missing_account(self):
        fake_cursor = MagicMock()
        fake_cursor.fetchone.return_value = None
        fake_conn = MagicMock()
        fake_conn.cursor.return_value = fake_cursor

        with patch.object(db, "get_connection", return_value=fake_conn):
            result = db.get_bank_account_status(999999)

        self.assertIsNone(result)


class PriorBeneficiaryMatchQueryTests(unittest.TestCase):
    """
    Regression coverage for the Part 2 prior-instruction-matching query. The
    live-DB safety of comparing DDM-masked columns via a WHERE clause (masking
    only affects values RETURNED to the client, not internal predicate
    evaluation) was verified empirically against seed data before this was
    implemented — see /memories/repo/schema-facts.md. These tests only pin
    down the query shape/params, not DDM behavior itself.
    """

    def test_queries_completed_status_and_all_four_identity_fields(self):
        fake_cursor = MagicMock()
        fake_cursor.fetchone.return_value = (7, "TXN-2026-101", None)
        fake_conn = MagicMock()
        fake_conn.cursor.return_value = fake_cursor

        with patch.object(db, "get_connection", return_value=fake_conn):
            result = db.find_prior_completed_beneficiary_match(
                payee_name="Sunset Properties LLC",
                receiving_bank_name="Bank of America",
                receiving_account_number="11223344",
                receiving_routing_number="026009593",
            )

        executed_sql, executed_params = fake_cursor.execute.call_args[0]
        self.assertIn("t.Current_Status = ?", executed_sql)
        self.assertIn("bi.Receiving_Account_Number = ?", executed_sql)
        self.assertIn("bi.Receiving_Routing_Number = ?", executed_sql)
        self.assertIn("UPPER(bi.Receiving_Bank_Name) = UPPER(?)", executed_sql)
        self.assertIn("UPPER(b.Payee_Name) = UPPER(?)", executed_sql)
        self.assertEqual(
            executed_params,
            [wf.STATUS_COMPLETED, "11223344", "026009593", "Bank of America", "Sunset Properties LLC"],
        )
        self.assertEqual(result["transaction_key"], 7)
        self.assertEqual(result["request_id"], "TXN-2026-101")

    def test_returns_none_when_no_match(self):
        fake_cursor = MagicMock()
        fake_cursor.fetchone.return_value = None
        fake_conn = MagicMock()
        fake_conn.cursor.return_value = fake_cursor

        with patch.object(db, "get_connection", return_value=fake_conn):
            result = db.find_prior_completed_beneficiary_match(
                payee_name="Nobody", receiving_bank_name="Nowhere Bank",
                receiving_account_number="00000000", receiving_routing_number="000000000",
            )

        self.assertIsNone(result)


class WorkflowStageSyncQueryTests(unittest.TestCase):
    """Current_Workflow_Stage must be set in the SAME UPDATE as Current_Status."""

    def test_advance_transaction_workflow_sets_stage_atomically(self):
        fake_cursor = MagicMock()
        fake_cursor.rowcount = 1
        fake_cursor.fetchone.return_value = (99,)  # WorkflowAssignment OUTPUT INSERTED.Assignment_Key
        fake_conn = MagicMock()
        fake_conn.cursor.return_value = fake_cursor

        with patch.object(db, "get_connection", return_value=fake_conn):
            db.advance_transaction_workflow(
                1, from_status=wf.STATUS_PENDING_APPROVER, new_status=wf.STATUS_PENDING_CONTROLLER,
                new_owner_user_key=5, actor_user_key=2, actor_role="Sr. Accounting Manager",
                action=wf.ACTION_APPROVE, workflow_role="Controller",
            )

        update_sql, update_params = fake_cursor.execute.call_args_list[0][0]
        self.assertIn("Current_Status = ?", update_sql)
        self.assertIn("Current_Workflow_Stage = ?", update_sql)
        self.assertIn("CurrentOwner_User_Key = ?", update_sql)
        self.assertEqual(update_params[0], wf.STATUS_PENDING_CONTROLLER)
        self.assertEqual(update_params[1], "Controller")  # stage_for_status(STATUS_PENDING_CONTROLLER)
        self.assertEqual(update_params[2], 5)

    def test_insert_transaction_uses_reserved_request_id_and_synced_stage(self):
        fake_cursor = MagicMock()
        # insert_transaction() only calls cur.fetchone() after statements that
        # actually consume it: BusinessEntity SELECT, ApprovalRule SELECT,
        # Beneficiary INSERT, BeneficiaryBankInstruction INSERT, ETransaction
        # INSERT, and (Batch 6) the initial Approver WorkflowAssignment INSERT.
        # ApprovalRule resolution (Batch 7) now uses fetchall(), not fetchone().
        # The TransactionVerification/WorkflowEvent OUTPUT clauses are never
        # fetched by the current code, so no entries are needed for them.
        fake_cursor.fetchone.side_effect = [
            (1,),      # Entity_Key
            (10,),     # Beneficiary_Key
            (20,),     # BeneficiaryInstruction_Key
            (30,),     # Transaction_Key
            (40,),     # Approver WorkflowAssignment.Assignment_Key
        ]
        # One matching ApprovalRule: Key=7, Requires_Approver/Controller=True, VP/CFO=False.
        fake_cursor.fetchall.return_value = [(7, True, True, False, False)]
        fake_conn = MagicMock()
        fake_conn.cursor.return_value = fake_cursor

        data = {
            "request_id": "TXN-2026-9999",
            "prepared_by_key": 1, "approver_key": 2, "controller_key": 3,
            "bank_account_key": 4, "recv_payee_name": "Payee", "recv_contact_name": "",
            "recv_contact_email": "", "recv_contact_phone": "", "recv_bank_name": "Bank",
            "recv_account_name": "Acct", "recv_account_number": "123", "recv_routing_number": "456",
            "recv_bank_address": "",
            "amount": 100.0, "request_type": "ACH", "classification": "corporate",
        }
        with patch.object(db, "get_connection", return_value=fake_conn):
            result = db.insert_transaction(data)

        self.assertEqual(result, "TXN-2026-9999")
        # No "SELECT 1 FROM ETransaction WHERE Request_ID" uniqueness check should have
        # run since a request_id was already supplied.
        all_sql = " ".join(c[0][0] for c in fake_cursor.execute.call_args_list)
        self.assertNotIn("SELECT 1 FROM [etransactions].[ETransaction] WHERE Request_ID", all_sql)
        # The ETransaction INSERT's Current_Status/Current_Workflow_Stage values are synced.
        etxn_insert_sql, etxn_params = next(
            c[0] for c in fake_cursor.execute.call_args_list if "INSERT INTO [etransactions].[ETransaction]" in c[0][0]
        )
        self.assertIn(wf.STATUS_PENDING_APPROVER, etxn_params)
        self.assertIn("Approver", etxn_params)

