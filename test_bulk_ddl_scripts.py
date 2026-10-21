"""
Static safety tests for the Item 6 DDL scripts and the BI handoff package.
Pure text checks: nothing here connects to, or executes anything against, a database.
(The scripts were also PARSE-checked by the engine with SET PARSEONLY ON during the review; that is evidence from a manual run,
not something this suite repeats.)

Run:  python -m unittest test_bulk_ddl_scripts -v
"""
import os
import re
import unittest
import external_guard  # noqa: F401  (must precede app/db/sharepoint imports)

import db

ROOT = os.path.dirname(os.path.abspath(__file__))
SCRIPT_DIR = os.path.join(ROOT, "db_migrations", "item6_bulk_recipient")


def _read(name):
    with open(os.path.join(SCRIPT_DIR, name), encoding="utf-8") as handle:
        return handle.read()


def _code(sql):
    """Script text without block comments, line comments and string literals."""
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return re.sub(r"N?'(?:[^']|'')*'", "''", sql)


def _code_keep_strings(sql):
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    return re.sub(r"--[^\n]*", " ", sql)


class PackageTests(unittest.TestCase):
    def test_all_scripts_and_the_readme_exist(self):
        for name in ("01_bulk_recipient_expand.sql", "02_bulk_recipient_backfill.sql", "03_bulk_recipient_harden.sql",
                     "04_bulk_recipient_validation.sql", "05_bulk_recipient_rollback.sql", "BI_EXECUTION_README.md"):
            self.assertTrue(os.path.exists(os.path.join(SCRIPT_DIR, name)), name)

    def test_the_readme_labels_ready_and_not_approved_scripts_and_covers_the_handoff_topics(self):
        text = _read("BI_EXECUTION_README.md")
        rows = {line.split("`")[1]: line for line in text.splitlines() if line.startswith("| `")}
        for script in ("01_bulk_recipient_expand.sql", "02_bulk_recipient_backfill.sql", "04_bulk_recipient_validation.sql",
                       "05_bulk_recipient_rollback.sql"):
            self.assertIn("READY FOR BI NON-PRODUCTION TESTING", rows[script])
        self.assertIn("NOT APPROVED FOR EXECUTION", rows["03_bulk_recipient_harden.sql"])
        self.assertIn("DO NOT EXECUTE", rows["03_bulk_recipient_harden.sql"])
        for topic in ("DEFERRED APPLICATION DEPENDENCY", "NOT EXECUTED - MISSING BASELINE", "OneLake", "BULK_RECIPIENT_ENABLED",
                      "Bulk Recipient Listing", "@Commit = 0", "Not proven by running", "ALTER COLUMN", "6B is informational"):
            self.assertIn(topic, text)


class ExpandScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = _read("01_bulk_recipient_expand.sql")
        cls.code = _code(cls.raw)
        cls.codes = _code_keep_strings(cls.raw)

    def test_error_handling_is_transactional_and_never_swallows_errors(self):
        for marker in ("SET XACT_ABORT ON", "BEGIN TRY", "END TRY", "BEGIN CATCH", "END CATCH", "XACT_STATE()", "ROLLBACK TRANSACTION"):
            self.assertIn(marker, self.code)
        catch = self.codes.split("BEGIN CATCH")[1]
        self.assertRegex(catch, r"ERROR_NUMBER\(\)")
        self.assertRegex(catch, r"ERROR_MESSAGE\(\)")
        self.assertRegex(catch, r"THROW;\s*END CATCH")       # the ORIGINAL error is re-raised
        self.assertIn("FAILED", catch)

    def test_success_is_only_reported_after_the_final_verification_and_commit(self):
        self.assertLess(self.code.index("THROW 50100"), self.code.index("COMMIT TRANSACTION"))
        self.assertLess(self.code.index("COMMIT TRANSACTION"), self.codes.index("SELECT N'PASS'"))
        self.assertEqual(self.code.count("COMMIT TRANSACTION"), 1)

    def test_one_verification_block_runs_before_the_changes_and_again_before_commit(self):
        self.assertEqual(self.code.count("DELETE FROM @Problems"), 1)          # a single shared block
        self.assertIn("WHILE @Phase <= 3", self.code)
        self.assertIn("SET @Strict = CASE WHEN @Phase = 3 THEN 1 ELSE 0 END", self.code)
        # phase 2 (the changes) sits between the two verification passes
        self.assertLess(self.code.index("IF @Phase = 2"), self.code.index("DELETE FROM @Problems"))

    def test_all_four_new_columns_are_verified_by_type_length_precision_scale_and_nullability(self):
        for col, typ, length, prec, scale in (("Recipient_Mode", "varchar", 20, 0, 0), ("Bulk_Listing_SHA256", "char", 64, 0, 0),
                                              ("Bulk_Recipient_Count", "int", 4, 10, 0), ("Bulk_Listing_Validated_DateTime", "datetime2", 8, 27, 7)):
            self.assertRegex(self.codes, rf"\(N'{col}',\s+N'{typ}',\s+{length},\s+{prec:>2},\s+{scale},\s+1\)".replace(r"\s+ ", r"\s+"))
        self.assertIn("c.max_length <> e.max_len", self.code)
        self.assertIn("c.precision <> e.prec", self.code)
        self.assertIn("c.scale <> e.scale", self.code)
        self.assertIn("c.is_nullable <> e.nullable", self.code)
        self.assertIn("c.default_object_id <> 0", self.code)
        self.assertIn("DIFFERENT definition", self.codes)

    def test_an_existing_child_table_must_match_in_full_and_mismatches_stop_the_script(self):
        for needle in ("expected (col, typ, max_len, prec, scale, nullable, ident)", "UNAPPROVED column", "PK_BulkRecipientAccount",
                       "i.type_desc = N'CLUSTERED'", "k.type = N'UQ'", "differs from the approved definition",
                       "FK_BulkRecipientAccount_Transaction", "FK_BulkRecipientAccount_BankAccount",
                       "UQ_BulkRecipientAccount_Transaction_Line", "UQ_BulkRecipientAccount_Transaction_BankAccount",
                       "CK_BulkRecipientAccount_Outcome", "CK_BulkRecipientAccount_Line", "CK_BulkRecipientAccount_Hash",
                       "IX_BulkRecipientAccount_BankAccount", "Unapproved index", "Unexpected default constraint", "Unexpected trigger"):
            self.assertIn(needle, self.codes)
        self.assertIn("THROW 50100", self.code)
        self.assertNotRegex(self.code, r"\bDROP\b")                       # mismatches are never dropped or recreated

    def test_mismatched_objects_are_never_silently_skipped(self):
        # Phase 1 (verification) runs first for every re-run; the creation guards below it only test whether a NAME exists.
        self.assertIn("DECLARE @Phase int = 1", self.code)
        phase_two = self.code.split("IF @Phase = 2")[1].split("ELSE")[0]
        self.assertIn("CREATE TABLE etransactions.BulkRecipientAccount", phase_two)
        self.assertNotIn("DELETE FROM @Problems", phase_two)
        self.assertIn("hardening constraint (script 03) that already exists", self.codes)

    def test_the_existing_foreign_keys_are_preserved_and_dependencies_are_inspected(self):
        for needle in ("FK_Transaction_Beneficiary", "FK_Transaction_BeneficiaryInstruction", "is_not_trusted = 0", "is_disabled = 0",
                       "delete_referential_action = 0", "sys.index_columns", "sys.check_constraints", "sys.computed_columns",
                       "sys.stats", "user_created = 1", "is_schema_bound_reference = 1", "still NOT NULL after the ALTER"):
            self.assertIn(needle, self.codes)
        alters = re.findall(r"ALTER COLUMN (\w+) (\w+) (\w+)", self.code)
        self.assertEqual(alters, [("Beneficiary_Key", "int", "NULL"), ("BeneficiaryInstruction_Key", "int", "NULL")])
        self.assertNotIn("DROP CONSTRAINT", self.code)

    def test_it_never_changes_or_removes_data_or_other_tables(self):
        without_dynamic = re.sub(r"EXEC \((?:@DdlSql|N'[^']*(?:''[^']*)*')\)", "", self.code)
        for forbidden in (r"\bUPDATE\b", r"\bINSERT\s+INTO\s+(?!@)", r"\bTRUNCATE\b", r"\bMERGE\b", r"\bTRIGGER\b", r"\bDEFAULT\b"):
            self.assertIsNone(re.search(forbidden, without_dynamic, re.I), forbidden)
        self.assertIsNone(re.search(r"DELETE\s+FROM\s+(?!@)", self.code, re.I))
        for table in ("Beneficiary", "BeneficiaryBankInstruction", "TransactionVerification", "AppUser", "WorkflowEvent", "WorkflowAssignment"):
            self.assertIsNone(re.search(rf"ALTER TABLE etransactions\.{table}\b", self.code), table)
        self.assertNotRegex(self.code, r"Recipient_Mode\s+varchar\(20\)\s+NOT NULL")
        self.assertNotIn("RecipientMode_Links", re.sub(r"N'[^']*'", "", self.codes))   # only named as a refusal, never created

    def test_the_application_receives_only_the_permissions_it_needs(self):
        grants = re.findall(r"GRANT ([A-Z, ]+) ON OBJECT::etransactions\.BulkRecipientAccount", self.codes)
        self.assertEqual(grants, ["SELECT, INSERT, DELETE"])
        for forbidden in ("GRANT ALTER", "GRANT CONTROL", "GRANT REFERENCES", "GRANT CREATE", "TAKE OWNERSHIP", "WITH GRANT OPTION", "db_owner", "ADD MEMBER"):
            self.assertNotIn(forbidden, self.codes)
        self.assertIn("unneeded permission", self.codes)        # post-check rejects extra grants
        self.assertIn("mirroring", self.raw.lower())             # BI reminder about OneLake / mirroring

    def test_dynamic_sql_avoids_the_function_call_inside_exec_that_the_engine_rejects(self):
        self.assertNotRegex(self.codes, r"EXEC\s*\(\s*N'[^']*'\s*\+\s*QUOTENAME")
        self.assertIn("EXEC (@DdlSql)", self.codes)

    def test_the_expected_objects_match_the_application_readiness_check(self):
        for column in sorted(db._BULK_REQUIRED_TRANSACTION_COLUMNS | db._BULK_REQUIRED_CHILD_COLUMNS):
            self.assertIn(column, self.codes)
        table = re.search(r"CREATE TABLE etransactions\.BulkRecipientAccount \((.*?)\);", self.code, re.S).group(1)
        for forbidden in ("AccountNumber", "RoutingNumber", "Beneficiary", "Account_Number", "Routing_Number", "Amount"):
            self.assertNotIn(forbidden, table)


class BackfillScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = _read("02_bulk_recipient_backfill.sql")
        cls.code = _code(cls.raw)
        cls.codes = _code_keep_strings(cls.raw)

    def test_dry_run_by_default_with_transactional_error_handling(self):
        self.assertIn("DECLARE @Commit bit = 0", self.code)
        for marker in ("BEGIN TRY", "BEGIN CATCH", "XACT_STATE()", "ROLLBACK TRANSACTION", "SET XACT_ABORT ON"):
            self.assertIn(marker, self.code)
        self.assertRegex(self.code, r"THROW;\s*END CATCH")

    def test_it_writes_only_recipient_mode_only_where_null_and_only_unambiguous_rows(self):
        self.assertEqual(set(re.findall(r"\bSET\s+(\w+)\s*=", self.code)), {"Recipient_Mode"})
        updates = re.findall(r"UPDATE t\b.*?(?=UPDATE t\b|IF @Rows)", self.code, re.S)
        self.assertEqual(len(updates), 2)
        internal, external = updates
        self.assertIn("t.RecipientBankAccount_Key IS NOT NULL", internal)
        self.assertIn("t.RecipientBankAccount_Key IS NULL", external)
        for block in updates:
            for needle in ("t.Recipient_Mode IS NULL", "t.Beneficiary_Key IS NOT NULL", "t.BeneficiaryInstruction_Key IS NOT NULL",
                           "NOT EXISTS", "Bulk_Listing_SHA256 IS NULL"):
                self.assertIn(needle, block)
        self.assertNotIn("Modified_DateTime =", self.code)
        for forbidden in (r"\bDELETE\b", r"\bINSERT\s+INTO\b", r"\bDROP\b", r"\bALTER\b"):
            self.assertIsNone(re.search(forbidden, self.code, re.I), forbidden)

    def test_it_refuses_a_missing_or_hardened_schema(self):
        for marker in ("THROW 50010", "THROW 50011", "THROW 50012"):
            self.assertIn(marker, self.code)


class DeferredHardeningScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = _read("03_bulk_recipient_harden.sql")
        cls.code = _code(cls.raw)

    def test_it_is_clearly_labelled_and_stops_before_any_ddl(self):
        self.assertIn("DO NOT EXECUTE - DEFERRED APPLICATION DEPENDENCY", self.raw.split("*/")[0])
        self.assertIn("NOT APPROVED FOR EXECUTION", self.raw.split("*/")[0])
        self.assertIn("DECLARE @ConfirmPrerequisites bit = 0", self.code)
        self.assertLess(self.code.index("THROW 50020"), self.code.index("ALTER TABLE"))
        self.assertIn("DEFERRED APPLICATION DEPENDENCY", self.raw.split("THROW 50020")[1].split("1;")[0])

    def test_the_application_write_order_dependency_is_documented(self):
        self.assertIn("WITHOUT Recipient_Mode", self.raw)
        self.assertIn("SAME INSERT / UPDATE", self.raw)

    def test_the_readme_never_places_it_in_the_execution_sequence(self):
        readme = _read("BI_EXECUTION_README.md")
        sequence = readme.split("## 2. Execution sequence")[1].split("## 3.")[0]
        self.assertNotRegex(sequence, r"Run \*\*03|run 03(?!\.)|03_bulk_recipient_harden\.sql\*\* -")
        self.assertIn("Do not run 03", sequence)


class ValidationScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = _read("04_bulk_recipient_validation.sql")
        cls.code = _code(cls.raw)
        cls.codes = _code_keep_strings(cls.raw)

    def test_it_is_read_only(self):
        for forbidden in (r"\bUPDATE\b", r"\bDELETE\b", r"\bDROP\b", r"\bALTER\b", r"\bCREATE\b", r"\bTRUNCATE\b",
                          r"\bGRANT\b", r"\bREVOKE\b", r"\bMERGE\b", r"\bEXEC(UTE)?\b"):
            self.assertIsNone(re.search(forbidden, self.code, re.I), forbidden)
        for target in re.findall(r"\bINSERT\s+(?:INTO\s+)?(\S+)", self.code, re.I):
            self.assertTrue(target.startswith("@"), target)         # table variables only

    def test_fingerprint_uses_an_explicit_list_and_never_checksum(self):
        self.assertNotRegex(self.code, r"(?i)CHECKSUM\s*\(\s*\*")
        self.assertNotRegex(self.code, r"(?i)BINARY_CHECKSUM")
        baseline = self.raw.split("3A. BASELINE")[1].split("4. CONSTRAINTS")[0]
        for new_column in ("Recipient_Mode", "Bulk_Listing_SHA256", "Bulk_Recipient_Count", "Bulk_Listing_Validated_DateTime"):
            self.assertNotIn(new_column, _code(baseline))
        capture = self.raw.split("3A. BASELINE")[1].split("3B. COMPARE")[0]
        compare = self.raw.split("3B. COMPARE")[1].split("4. CONSTRAINTS")[0]

        def columns(block):
            return re.findall(r"CONVERT\(nvarchar\(max\), t\.(\w+)", block)

        self.assertEqual(columns(capture), columns(compare))
        self.assertEqual(len(set(columns(capture))), 33)

    def test_a_missing_baseline_or_a_mismatch_cannot_look_like_success(self):
        compare = self.raw.split("3B. COMPARE")[1].split("4. CONSTRAINTS")[0]
        for needle in ("NOT EXECUTED - MISSING BASELINE", "N'PASS'", "N'FAIL'", "THROW 50210", "THROW 50211"):
            self.assertIn(needle, compare)
        # every baseline value is a NULL placeholder to be pasted: an untouched script must report NOT EXECUTED
        self.assertEqual(len(re.findall(r"\(N'[A-Za-z.0-9_]+', NULL\)", compare)), 9)
        self.assertIn("@missing > 0", compare)
        self.assertLess(compare.index("THROW 50210"), compare.index("THROW 50211"))

    def test_every_check_section_ends_with_pass_or_fail_and_stops_on_failure(self):
        for section, code in (("SECTION 2", "50202"), ("SECTION 4", "50204"), ("SECTION 5", "50205"), ("SECTION 6", "50206"),
                              ("SECTION 7", "50207"), ("SECTION 8", "50208")):
            self.assertIn(section, self.raw)
            self.assertIn(f"THROW {code}", self.code)
        for status in ("N'PASS'", "N'FAIL'"):
            self.assertIn(status, self.codes)
        self.assertIn("NOT EXECUTED", self.raw.split("RESULT LEGEND")[1].split("When to run what")[0])

    def test_closed_recipient_accounts_are_informational_not_integrity_violations(self):
        section6 = self.raw.split("6. DATA INTEGRITY")[1].split("7. BACKFILL")[0]
        part_a, part_b = section6.split("6B - INFORMATIONAL")
        self.assertNotIn("a.Status", _code(part_a))                       # no Closed-account check in the violations list
        self.assertIn("a.Status <> N'Open'", part_b)
        self.assertIn("informational", part_b.lower())
        self.assertIn("THROW 50206", part_b)
        self.assertIn("@Violations WHERE violations > 0", part_b)         # the verdict reads ONLY the violations table
        self.assertIn("NOT the cause", part_b)
        self.assertIn("recipient equals the transaction''s own originating account", part_a)   # real integrity rules stay

    def test_columns_are_checked_by_explicit_names_and_the_hardened_state_is_recognised(self):
        self.assertNotRegex(self.code, r"column_id\s*>\s*\d+")
        for section in ("2. COLUMN DEFINITIONS", "4. CONSTRAINTS", "8. OLD / NEW"):
            block = self.raw.split(section)[1].split("GO\n")[0]
            self.assertIn("@Hardened", block, section)
        s2 = self.raw.split("2. COLUMN DEFINITIONS")[1].split("GO\n")[0]
        self.assertIn("Unexpected ETransaction column", s2)
        self.assertIn("Original ETransaction column", s2)
        self.assertIn("CASE WHEN @Hardened = 1 THEN 0 ELSE 1 END", s2)    # Recipient_Mode NOT NULL is expected, not a defect, once hardened
        self.assertEqual(len(re.findall(r"\(N'[A-Za-z_]+'\)", s2.split("2b.")[0])), 33)   # the explicit list of 33 original columns
        s4 = self.raw.split("4. CONSTRAINTS")[1].split("GO\n")[0]
        self.assertIn("must not exist before hardening", s4)
        self.assertIn("(N'CK_ETransaction_RecipientMode_Links',               N'C',  N'hardened')", s4)

    def test_permissions_are_checked_for_excess_and_the_mirroring_reminder_is_present(self):
        s5 = self.raw.split("5. APPLICATION PRINCIPAL PERMISSIONS")[1].split("GO\n")[0]
        for needle in ("ALTER", "CONTROL", "TAKE OWNERSHIP", "db_owner", "REFERENCES", "unneeded permission", "OneLake"):
            self.assertIn(needle, s5)
        self.assertIn("MANUAL REVIEW REQUIRED", s5)


class RollbackScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = _read("05_bulk_recipient_rollback.sql")
        cls.code = _code(cls.raw)
        cls.codes = _code_keep_strings(cls.raw)

    def test_it_refuses_to_run_by_default_and_handles_errors_transactionally(self):
        self.assertIn("DECLARE @IUnderstand bit = 0", self.code)
        self.assertLess(self.code.index("THROW 50030"), self.code.index("DROP "))
        for marker in ("SET XACT_ABORT ON", "BEGIN TRY", "BEGIN CATCH", "XACT_STATE()", "ROLLBACK TRANSACTION"):
            self.assertIn(marker, self.code)
        self.assertRegex(self.code, r"THROW;\s*END CATCH")
        self.assertLess(self.code.index("COMMIT TRANSACTION"), self.codes.index("SELECT N'PASS - ROLLED BACK AND VERIFIED'"))

    def test_it_refuses_any_destructive_rollback_of_bulk_data(self):
        for reason in ("legitimate Bulk recipient row", "Recipient_Mode = Bulk", "carry a Bulk listing hash", "carry a Bulk recipient count",
                       "carry a Bulk validation timestamp", "NULL Beneficiary_Key or BeneficiaryInstruction_Key", "UNRECOGNIZED Recipient_Mode value",
                       "UNRECOGNIZED column", "UNRECOGNIZED constraint/index", "UNRECOGNIZED definition", "blocks the drop", "THROW 50035"):
            self.assertIn(reason, self.codes)
        # the same preconditions run again INSIDE the transaction (phase 2), before the drops (phase 3)
        self.assertIn("IF @Phase = 3", self.code)
        self.assertRegex(self.code, r"IF @Phase = 2\s+BEGIN TRANSACTION;")
        self.assertIn("SET @Post = CASE WHEN @Phase = 4 THEN 1 ELSE 0 END", self.code)
        phase_three = self.code.split("IF @Phase = 3")[1].split("ELSE")[0]
        self.assertLess(phase_three.index("DROP TABLE etransactions.BulkRecipientAccount"), phase_three.index("DROP COLUMN"))
        self.assertLess(phase_three.index("DROP CONSTRAINT"), phase_three.index("DROP TABLE"))

    def test_it_never_deletes_updates_or_moves_data(self):
        for forbidden in (r"\bUPDATE\b", r"\bTRUNCATE\b", r"\bMERGE\b", r"\bINSERT\s+INTO\s+(?!@)"):
            self.assertIsNone(re.search(forbidden, self.code, re.I), forbidden)
        self.assertIsNone(re.search(r"DELETE\s+FROM\s+(?!@)", self.code, re.I))

    def test_every_drop_is_guarded_so_partial_states_are_safe(self):
        for column in ("Recipient_Mode", "Bulk_Listing_SHA256", "Bulk_Recipient_Count", "Bulk_Listing_Validated_DateTime"):
            self.assertRegex(self.codes, rf"IF COL_LENGTH\(N'etransactions\.ETransaction', N'{column}'\) IS NOT NULL\s+ALTER TABLE etransactions\.ETransaction DROP COLUMN {column}")
        self.assertIn("IF OBJECT_ID(N'etransactions.BulkRecipientAccount', N'U') IS NOT NULL", self.codes)
        self.assertIn("@Nothing", self.code)
        self.assertNotIn("DROP CONSTRAINT FK_Transaction", self.code)     # the existing foreign keys are never touched

    def test_the_post_checks_prove_the_original_state_before_commit(self):
        for needle in ("still exists", "is not NOT NULL again", "after the rollback"):
            self.assertIn(needle, self.codes)


if __name__ == "__main__":
    unittest.main()
