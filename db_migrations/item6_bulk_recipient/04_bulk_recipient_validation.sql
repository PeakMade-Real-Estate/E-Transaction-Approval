/* ============================================================================
   Item 6 - Corporate Bulk-Recipient Payments
   04_bulk_recipient_validation.sql   (READ-ONLY VERIFICATION + TEST GUIDANCE)

   Every statement is a SELECT / metadata read (plus INSERTs into TABLE VARIABLES only).  Nothing writes, alters or drops.
   The few write statements in section 10 are COMMENTED OUT and labelled NON-PRODUCTION ONLY.
   Each section is its own batch (separated by GO).  Run as an administrative principal.

   RESULT LEGEND - every section ends with ONE result row:
       PASS                              the check ran and everything is as required
       FAIL                              the check ran and found a problem; the problems are listed and the batch ends with an
                                         ERROR (number 502xx) so a failed section cannot be mistaken for success
       NOT EXECUTED / MISSING BASELINE   section 3B only: the baseline was not pasted, so the comparison was not performed (ERROR 50210)
   A section you did not run is NOT EXECUTED.  The migration is validated only when every required section below returned PASS.

   When to run what
     BEFORE 01 : section 3A (baseline fingerprint) - SAVE ITS OUTPUT
     AFTER  01 : 2, 3B (paste the 3A values first), 4, 5, 6, 8, and 9 (informational)
     AFTER  02 : 3B, 6, 7
     (After the future script 03 only: 2, 4, 6 - the checks below recognise the hardened state and do not flag it.)

   Fingerprint: an EXPLICIT, STABLE list of the 33 ORIGINAL ETransaction columns with the same expression on both sides.
   CHECKSUM(*) / BINARY_CHECKSUM are NOT used (adding columns would change them).  Take the baseline in a quiet window, or use
   the same @AsOf cut-off (on Modified_DateTime) in 3A and 3B.
   ============================================================================ */

/* ============================== 1. ENVIRONMENT (INFORMATION ONLY - no PASS/FAIL) ============================== */
SELECT @@VERSION AS engine_version, DB_NAME() AS database_name;      -- SERVER_PROPERTY() is not supported on this engine
SELECT name AS database_name, compatibility_level, is_read_committed_snapshot_on, snapshot_isolation_state_desc
FROM sys.databases WHERE name = DB_NAME();

SELECT o.type_desc, o.name AS object_in_schema_that_could_depend_on_the_altered_columns
FROM sys.objects o JOIN sys.schemas s ON s.schema_id = o.schema_id AND s.name = N'etransactions'
WHERE o.type_desc IN (N'VIEW', N'SQL_STORED_PROCEDURE', N'SQL_SCALAR_FUNCTION', N'SQL_TABLE_VALUED_FUNCTION',
                      N'SQL_INLINE_TABLE_VALUED_FUNCTION', N'SQL_TRIGGER');
SELECT name AS trigger_name, parent_class_desc, OBJECT_NAME(parent_id) AS parent_object FROM sys.triggers;

SELECT OBJECT_SCHEMA_NAME(d.referencing_id) AS referencing_schema, OBJECT_NAME(d.referencing_id) AS referencing_object,
       d.referenced_entity_name, d.is_schema_bound_reference
FROM sys.sql_expression_dependencies d
WHERE d.referenced_schema_name = N'etransactions'
  AND d.referenced_entity_name IN (N'ETransaction', N'Beneficiary', N'BeneficiaryBankInstruction', N'BankAccount');
GO

/* ============================== 2. COLUMN DEFINITIONS  (PASS / FAIL) ============================== */
/* Explicit checks of the 33 original columns, the 4 new columns, the 2 relaxed links and the child table.
   Understands the three schema states: expanded (01), backfilled (02) and - in future - hardened (03): in the hardened state
   Recipient_Mode is expected NOT NULL and that is NOT reported as a problem. */
DECLARE @ET int = OBJECT_ID(N'etransactions.ETransaction', N'U'), @BR int = OBJECT_ID(N'etransactions.BulkRecipientAccount', N'U');
DECLARE @Hardened bit = CASE WHEN EXISTS (SELECT 1 FROM sys.check_constraints WHERE parent_object_id = @ET AND name = N'CK_ETransaction_RecipientMode_Links') THEN 1 ELSE 0 END;
DECLARE @Problems TABLE (id int IDENTITY(1,1) PRIMARY KEY, msg nvarchar(600) NOT NULL);

IF @ET IS NULL INSERT INTO @Problems (msg) VALUES (N'etransactions.ETransaction not found.');

/* 2a. the 33 original columns must all still exist; no other column may exist except the 4 approved new ones */
;WITH original (col) AS (
    SELECT col FROM (VALUES
        (N'Transaction_Key'), (N'Request_ID'), (N'PreparedBy_User_Key'), (N'Entity_Key'), (N'Property_Department_Text'),
        (N'Entity_ID_Text'), (N'OriginatingBankAccount_Key'), (N'Beneficiary_Key'), (N'BeneficiaryInstruction_Key'),
        (N'SelectedApprover_User_Key'), (N'SelectedController_User_Key'), (N'VPApprover_User_Key'), (N'CFOApprover_User_Key'),
        (N'CurrentOwner_User_Key'), (N'BankReleaser_User_Key'), (N'ApprovalRule_Key'), (N'Request_Type'), (N'Treasury_Service_Date'),
        (N'Prepared_Date'), (N'Submitted_Date'), (N'Amount'), (N'Currency'), (N'Payment_Purpose'), (N'Urgent_Flag'), (N'Urgency_Reason'),
        (N'Current_Status'), (N'Current_Workflow_Stage'), (N'Approval_Tier_Snapshot'), (N'Requires_VP'), (N'Requires_CFO'),
        (N'Created_DateTime'), (N'Modified_DateTime'), (N'RecipientBankAccount_Key')) v (col)
)
INSERT INTO @Problems (msg)
SELECT CONCAT(N'Original ETransaction column ', o.col, N' is missing.')
FROM original o
WHERE NOT EXISTS (SELECT 1 FROM sys.columns c WHERE c.object_id = @ET AND c.name = o.col);

INSERT INTO @Problems (msg)
SELECT CONCAT(N'Unexpected ETransaction column ', c.name, N' (not one of the 33 original or 4 approved new columns).')
FROM sys.columns c
WHERE c.object_id = @ET
  AND c.name NOT IN (
        N'Transaction_Key', N'Request_ID', N'PreparedBy_User_Key', N'Entity_Key', N'Property_Department_Text', N'Entity_ID_Text',
        N'OriginatingBankAccount_Key', N'Beneficiary_Key', N'BeneficiaryInstruction_Key', N'SelectedApprover_User_Key',
        N'SelectedController_User_Key', N'VPApprover_User_Key', N'CFOApprover_User_Key', N'CurrentOwner_User_Key', N'BankReleaser_User_Key',
        N'ApprovalRule_Key', N'Request_Type', N'Treasury_Service_Date', N'Prepared_Date', N'Submitted_Date', N'Amount', N'Currency',
        N'Payment_Purpose', N'Urgent_Flag', N'Urgency_Reason', N'Current_Status', N'Current_Workflow_Stage', N'Approval_Tier_Snapshot',
        N'Requires_VP', N'Requires_CFO', N'Created_DateTime', N'Modified_DateTime', N'RecipientBankAccount_Key',
        N'Recipient_Mode', N'Bulk_Listing_SHA256', N'Bulk_Recipient_Count', N'Bulk_Listing_Validated_DateTime');

/* 2b. new columns + relaxed links: exact definition */
;WITH expected (col, typ, max_len, prec, scale, nullable) AS (
    SELECT * FROM (VALUES
        (N'Recipient_Mode',                  N'varchar',   20,  0, 0, CASE WHEN @Hardened = 1 THEN 0 ELSE 1 END),
        (N'Bulk_Listing_SHA256',             N'char',      64,  0, 0, 1),
        (N'Bulk_Recipient_Count',            N'int',        4, 10, 0, 1),
        (N'Bulk_Listing_Validated_DateTime', N'datetime2',  8, 27, 7, 1),
        (N'Beneficiary_Key',                 N'int',        4, 10, 0, 1),
        (N'BeneficiaryInstruction_Key',      N'int',        4, 10, 0, 1)
    ) v (col, typ, max_len, prec, scale, nullable)
)
INSERT INTO @Problems (msg)
SELECT CASE WHEN c.column_id IS NULL THEN CONCAT(N'ETransaction.', e.col, N' is missing.')
            ELSE CONCAT(N'ETransaction.', e.col, N' definition differs: found ', ty.name, N' len ', c.max_length, N' prec ', c.precision, N' scale ', c.scale,
                        N' nullable ', c.is_nullable, N'; required ', e.typ, N' len ', e.max_len, N' prec ', e.prec, N' scale ', e.scale,
                        N' nullable ', e.nullable, CASE WHEN @Hardened = 1 THEN N' (hardened state)' ELSE N' (expanded state)' END, N'.') END
FROM expected e
LEFT JOIN sys.columns c ON c.object_id = @ET AND c.name = e.col
LEFT JOIN sys.types ty ON ty.user_type_id = c.user_type_id
WHERE c.column_id IS NULL
   OR ty.name <> e.typ OR c.max_length <> e.max_len OR c.precision <> e.prec OR c.scale <> e.scale OR c.is_nullable <> e.nullable
   OR c.is_identity = 1 OR c.is_computed = 1
   OR (e.col LIKE N'Bulk%' AND c.default_object_id <> 0) OR (e.col = N'Recipient_Mode' AND c.default_object_id <> 0);

/* 2c. child table: exact columns, nothing extra */
IF @BR IS NULL
    INSERT INTO @Problems (msg) VALUES (N'etransactions.BulkRecipientAccount not found.');
ELSE
BEGIN
    ;WITH expected (col, typ, max_len, prec, scale, nullable, ident) AS (
        SELECT * FROM (VALUES
            (N'BulkRecipient_Key',    N'int',      4, 10, 0, 0, 1),
            (N'Transaction_Key',      N'int',      4, 10, 0, 0, 0),
            (N'Line_Number',          N'int',      4, 10, 0, 0, 0),
            (N'BankAccount_Key',      N'int',      4, 10, 0, 0, 0),
            (N'Verification_Outcome', N'varchar', 20,  0, 0, 0, 0),
            (N'Verified_DateTime',    N'datetime2', 8, 27, 7, 0, 0),
            (N'Listing_SHA256',       N'char',     64,  0, 0, 0, 0)
        ) v (col, typ, max_len, prec, scale, nullable, ident)
    )
    INSERT INTO @Problems (msg)
    SELECT CASE WHEN c.column_id IS NULL THEN CONCAT(N'BulkRecipientAccount.', e.col, N' is missing.')
                ELSE CONCAT(N'BulkRecipientAccount.', e.col, N' definition differs: found ', ty.name, N' len ', c.max_length, N' prec ', c.precision,
                            N' scale ', c.scale, N' nullable ', c.is_nullable, N' identity ', c.is_identity, N'.') END
    FROM expected e
    LEFT JOIN sys.columns c ON c.object_id = @BR AND c.name = e.col
    LEFT JOIN sys.types ty ON ty.user_type_id = c.user_type_id
    WHERE c.column_id IS NULL OR ty.name <> e.typ OR c.max_length <> e.max_len OR c.precision <> e.prec OR c.scale <> e.scale
       OR c.is_nullable <> e.nullable OR c.is_identity <> e.ident OR c.is_computed = 1 OR c.default_object_id <> 0;

    INSERT INTO @Problems (msg)
    SELECT CONCAT(N'BulkRecipientAccount has an UNAPPROVED column ', c.name, N'.')
    FROM sys.columns c
    WHERE c.object_id = @BR
      AND c.name NOT IN (N'BulkRecipient_Key', N'Transaction_Key', N'Line_Number', N'BankAccount_Key', N'Verification_Outcome', N'Verified_DateTime', N'Listing_SHA256');
END;

SELECT N'SECTION 2 - column definitions' AS section, CASE WHEN COUNT(*) = 0 THEN N'PASS' ELSE N'FAIL' END AS status,
       CASE WHEN @Hardened = 1 THEN N'hardened state detected' ELSE N'expanded (not hardened) state' END AS schema_state, COUNT(*) AS problems
FROM @Problems;
IF EXISTS (SELECT 1 FROM @Problems)
BEGIN
    SELECT id, msg AS problem FROM @Problems ORDER BY id;
    THROW 50202, N'SECTION 2 FAILED: column definitions do not match the approved schema (see the problem list).', 1;
END;
GO

/* ============================== 3A. BASELINE FINGERPRINT - RUN BEFORE 01 AND SAVE THE OUTPUT (capture only; no PASS/FAIL) ============================== */
/* Uses ONLY original columns, so it runs both before and after the migration. */
DECLARE @AsOf datetime2(7) = NULL;    -- optional cut-off; use the SAME value in 3A and 3B
SELECT metric, value FROM (
    SELECT N'ETransaction.rows' AS metric, CONVERT(decimal(38,2), COUNT(*)) AS value
      FROM etransactions.ETransaction t WHERE (@AsOf IS NULL OR t.Modified_DateTime <= @AsOf)
    UNION ALL SELECT N'ETransaction.total_amount', CONVERT(decimal(38,2), SUM(t.Amount))
      FROM etransactions.ETransaction t WHERE (@AsOf IS NULL OR t.Modified_DateTime <= @AsOf)
    UNION ALL SELECT N'ETransaction.fingerprint_33_original_columns',
        SUM(CONVERT(decimal(38,0), CONVERT(bigint, SUBSTRING(HASHBYTES('SHA2_256', CONCAT_WS(N'|',
            ISNULL(CONVERT(nvarchar(max), t.Transaction_Key), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.Request_ID), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.PreparedBy_User_Key), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.Entity_Key), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.Property_Department_Text), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.Entity_ID_Text), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.OriginatingBankAccount_Key), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.Beneficiary_Key), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.BeneficiaryInstruction_Key), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.SelectedApprover_User_Key), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.SelectedController_User_Key), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.VPApprover_User_Key), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.CFOApprover_User_Key), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.CurrentOwner_User_Key), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.BankReleaser_User_Key), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.ApprovalRule_Key), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.Request_Type), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.Treasury_Service_Date, 126), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.Prepared_Date, 126), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.Submitted_Date, 126), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.Amount), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.Currency), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.Payment_Purpose), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.Urgent_Flag), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.Urgency_Reason), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.Current_Status), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.Current_Workflow_Stage), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.Approval_Tier_Snapshot), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.Requires_VP), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.Requires_CFO), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.Created_DateTime, 126), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.Modified_DateTime, 126), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.RecipientBankAccount_Key), N'~')
        )), 1, 7))))
      FROM etransactions.ETransaction t WHERE (@AsOf IS NULL OR t.Modified_DateTime <= @AsOf)
    UNION ALL SELECT N'ETransaction.beneficiary_references',
        SUM(CONVERT(decimal(38,0), CONVERT(bigint, SUBSTRING(HASHBYTES('SHA2_256', CONCAT_WS(N'|',
            CONVERT(nvarchar(max), t.Transaction_Key), ISNULL(CONVERT(nvarchar(max), t.Beneficiary_Key), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.BeneficiaryInstruction_Key), N'~'))), 1, 7))))
      FROM etransactions.ETransaction t WHERE (@AsOf IS NULL OR t.Modified_DateTime <= @AsOf)
    UNION ALL SELECT N'ETransaction.recipient_account_references',
        SUM(CONVERT(decimal(38,0), CONVERT(bigint, SUBSTRING(HASHBYTES('SHA2_256', CONCAT_WS(N'|',
            CONVERT(nvarchar(max), t.Transaction_Key), ISNULL(CONVERT(nvarchar(max), t.RecipientBankAccount_Key), N'~'),
            CONVERT(nvarchar(max), t.OriginatingBankAccount_Key))), 1, 7))))
      FROM etransactions.ETransaction t WHERE (@AsOf IS NULL OR t.Modified_DateTime <= @AsOf)
    UNION ALL SELECT N'Beneficiary.rows', CONVERT(decimal(38,2), COUNT(*)) FROM etransactions.Beneficiary
    UNION ALL SELECT N'BeneficiaryBankInstruction.rows', CONVERT(decimal(38,2), COUNT(*)) FROM etransactions.BeneficiaryBankInstruction
    UNION ALL SELECT N'TransactionVerification.rows', CONVERT(decimal(38,2), COUNT(*)) FROM etransactions.TransactionVerification
    UNION ALL SELECT N'BankAccount.rows', CONVERT(decimal(38,2), COUNT(*)) FROM etransactions.BankAccount
) m
ORDER BY metric;
GO

/* ============================== 3B. COMPARE AFTER MIGRATION  (PASS / FAIL / NOT EXECUTED - MISSING BASELINE) ============================== */
/* PASTE: replace each NULL below with the matching 3A value.  Left as NULL, the section reports NOT EXECUTED and stops with an error. */
DECLARE @AsOf datetime2(7) = NULL;                       -- must equal the value used in 3A
DECLARE @baseline TABLE (metric nvarchar(80) PRIMARY KEY, value decimal(38,2) NULL);
INSERT @baseline (metric, value) VALUES
    (N'ETransaction.rows', NULL),
    (N'ETransaction.total_amount', NULL),
    (N'ETransaction.fingerprint_33_original_columns', NULL),
    (N'ETransaction.beneficiary_references', NULL),
    (N'ETransaction.recipient_account_references', NULL),
    (N'Beneficiary.rows', NULL),
    (N'BeneficiaryBankInstruction.rows', NULL),
    (N'TransactionVerification.rows', NULL),
    (N'BankAccount.rows', NULL);

DECLARE @now TABLE (metric nvarchar(80) PRIMARY KEY, value decimal(38,2) NULL);
INSERT @now (metric, value)
SELECT metric, value FROM (
    SELECT N'ETransaction.rows' AS metric, CONVERT(decimal(38,2), COUNT(*)) AS value
      FROM etransactions.ETransaction t WHERE (@AsOf IS NULL OR t.Modified_DateTime <= @AsOf)
    UNION ALL SELECT N'ETransaction.total_amount', CONVERT(decimal(38,2), SUM(t.Amount))
      FROM etransactions.ETransaction t WHERE (@AsOf IS NULL OR t.Modified_DateTime <= @AsOf)
    UNION ALL SELECT N'ETransaction.fingerprint_33_original_columns',
        SUM(CONVERT(decimal(38,0), CONVERT(bigint, SUBSTRING(HASHBYTES('SHA2_256', CONCAT_WS(N'|',
            ISNULL(CONVERT(nvarchar(max), t.Transaction_Key), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.Request_ID), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.PreparedBy_User_Key), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.Entity_Key), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.Property_Department_Text), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.Entity_ID_Text), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.OriginatingBankAccount_Key), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.Beneficiary_Key), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.BeneficiaryInstruction_Key), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.SelectedApprover_User_Key), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.SelectedController_User_Key), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.VPApprover_User_Key), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.CFOApprover_User_Key), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.CurrentOwner_User_Key), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.BankReleaser_User_Key), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.ApprovalRule_Key), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.Request_Type), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.Treasury_Service_Date, 126), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.Prepared_Date, 126), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.Submitted_Date, 126), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.Amount), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.Currency), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.Payment_Purpose), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.Urgent_Flag), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.Urgency_Reason), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.Current_Status), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.Current_Workflow_Stage), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.Approval_Tier_Snapshot), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.Requires_VP), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.Requires_CFO), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.Created_DateTime, 126), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.Modified_DateTime, 126), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.RecipientBankAccount_Key), N'~')
        )), 1, 7))))
      FROM etransactions.ETransaction t WHERE (@AsOf IS NULL OR t.Modified_DateTime <= @AsOf)
    UNION ALL SELECT N'ETransaction.beneficiary_references',
        SUM(CONVERT(decimal(38,0), CONVERT(bigint, SUBSTRING(HASHBYTES('SHA2_256', CONCAT_WS(N'|',
            CONVERT(nvarchar(max), t.Transaction_Key), ISNULL(CONVERT(nvarchar(max), t.Beneficiary_Key), N'~'),
            ISNULL(CONVERT(nvarchar(max), t.BeneficiaryInstruction_Key), N'~'))), 1, 7))))
      FROM etransactions.ETransaction t WHERE (@AsOf IS NULL OR t.Modified_DateTime <= @AsOf)
    UNION ALL SELECT N'ETransaction.recipient_account_references',
        SUM(CONVERT(decimal(38,0), CONVERT(bigint, SUBSTRING(HASHBYTES('SHA2_256', CONCAT_WS(N'|',
            CONVERT(nvarchar(max), t.Transaction_Key), ISNULL(CONVERT(nvarchar(max), t.RecipientBankAccount_Key), N'~'),
            CONVERT(nvarchar(max), t.OriginatingBankAccount_Key))), 1, 7))))
      FROM etransactions.ETransaction t WHERE (@AsOf IS NULL OR t.Modified_DateTime <= @AsOf)
    UNION ALL SELECT N'Beneficiary.rows', CONVERT(decimal(38,2), COUNT(*)) FROM etransactions.Beneficiary
    UNION ALL SELECT N'BeneficiaryBankInstruction.rows', CONVERT(decimal(38,2), COUNT(*)) FROM etransactions.BeneficiaryBankInstruction
    UNION ALL SELECT N'TransactionVerification.rows', CONVERT(decimal(38,2), COUNT(*)) FROM etransactions.TransactionVerification
    UNION ALL SELECT N'BankAccount.rows', CONVERT(decimal(38,2), COUNT(*)) FROM etransactions.BankAccount
) m;

DECLARE @missing int = (SELECT COUNT(*) FROM @baseline WHERE value IS NULL);
DECLARE @different int = (SELECT COUNT(*) FROM @baseline b LEFT JOIN @now n ON n.metric = b.metric
                           WHERE b.value IS NOT NULL AND (n.metric IS NULL OR n.value IS NULL OR n.value <> b.value));

SELECT b.metric, b.value AS baseline_value, n.value AS current_value,
       CASE WHEN b.value IS NULL THEN N'NOT EXECUTED - MISSING BASELINE'
            WHEN n.value IS NOT NULL AND n.value = b.value THEN N'PASS'
            ELSE N'FAIL' END AS status
FROM @baseline b LEFT JOIN @now n ON n.metric = b.metric
ORDER BY b.metric;

SELECT N'SECTION 3B - original-data fingerprint' AS section,
       CASE WHEN @missing > 0 THEN N'NOT EXECUTED - MISSING BASELINE'
            WHEN @different > 0 THEN N'FAIL'
            ELSE N'PASS' END AS status,
       @missing AS metrics_missing_baseline, @different AS metrics_different;

IF @missing > 0
    THROW 50210, N'SECTION 3B NOT EXECUTED - MISSING BASELINE: paste ALL nine 3A values into @baseline (captured BEFORE script 01). Nothing was compared; this is NOT a pass.', 1;
IF @different > 0
    THROW 50211, N'SECTION 3B FAILED: the 33 original ETransaction columns / row counts / references differ from the baseline. Investigate before proceeding (rows modified by the application after 3A are legitimate: use a quiet window or @AsOf).', 1;
GO

/* ============================== 4. CONSTRAINTS, FOREIGN KEYS, INDEXES, TRUST  (PASS / FAIL) ============================== */
DECLARE @ET int = OBJECT_ID(N'etransactions.ETransaction', N'U'), @BR int = OBJECT_ID(N'etransactions.BulkRecipientAccount', N'U');
DECLARE @Hardened bit = CASE WHEN EXISTS (SELECT 1 FROM sys.check_constraints WHERE parent_object_id = @ET AND name = N'CK_ETransaction_RecipientMode_Links') THEN 1 ELSE 0 END;
DECLARE @Problems TABLE (id int IDENTITY(1,1) PRIMARY KEY, msg nvarchar(600) NOT NULL);

/* 4a. required objects: existence, enabled, trusted.  Hardening objects (03) are required ONLY in the hardened state, and are all-or-nothing. */
;WITH expected (name, kind, required_when) AS (
    SELECT * FROM (VALUES
        (N'PK_BulkRecipientAccount',                           N'PK', N'always'),
        (N'FK_BulkRecipientAccount_Transaction',               N'FK', N'always'),
        (N'FK_BulkRecipientAccount_BankAccount',               N'FK', N'always'),
        (N'UQ_BulkRecipientAccount_Transaction_Line',          N'UQ', N'always'),
        (N'UQ_BulkRecipientAccount_Transaction_BankAccount',   N'UQ', N'always'),
        (N'CK_BulkRecipientAccount_Outcome',                   N'C',  N'always'),
        (N'CK_BulkRecipientAccount_Line',                      N'C',  N'always'),
        (N'CK_BulkRecipientAccount_Hash',                      N'C',  N'always'),
        (N'CK_ETransaction_RecipientMode',                     N'C',  N'always'),
        (N'CK_ETransaction_BulkCount',                         N'C',  N'always'),
        (N'CK_ETransaction_BulkMetadata',                      N'C',  N'always'),
        (N'FK_Transaction_Beneficiary',                        N'FK', N'always'),
        (N'FK_Transaction_BeneficiaryInstruction',             N'FK', N'always'),
        (N'FK_Transaction_RecipientBankAccount',               N'FK', N'always'),
        (N'FK_Transaction_OriginatingBank',                    N'FK', N'always'),
        (N'CK_ETransaction_RecipientMode_Links',               N'C',  N'hardened'),
        (N'CK_ETransaction_BulkMetadata_Mode',                 N'C',  N'hardened')
    ) v (name, kind, required_when)
)
INSERT INTO @Problems (msg)
SELECT CONCAT(e.name, CASE WHEN o.object_id IS NULL THEN N' is MISSING'
                           WHEN COALESCE(fk.is_disabled, cc.is_disabled, 0) = 1 THEN N' is DISABLED'
                           WHEN COALESCE(fk.is_not_trusted, cc.is_not_trusted, 0) = 1 THEN N' is NOT TRUSTED'
                           ELSE N' must not exist before hardening (script 03 is deferred)' END, N'.')
FROM expected e
LEFT JOIN sys.objects o ON o.name = e.name AND o.schema_id = SCHEMA_ID(N'etransactions')
LEFT JOIN sys.foreign_keys fk ON fk.object_id = o.object_id
LEFT JOIN sys.check_constraints cc ON cc.object_id = o.object_id
WHERE (e.required_when = N'always' AND (o.object_id IS NULL OR COALESCE(fk.is_disabled, cc.is_disabled, 0) = 1 OR COALESCE(fk.is_not_trusted, cc.is_not_trusted, 0) = 1))
   OR (e.required_when = N'hardened' AND @Hardened = 1 AND (o.object_id IS NULL OR cc.is_disabled = 1 OR cc.is_not_trusted = 1))
   OR (e.required_when = N'hardened' AND @Hardened = 0 AND o.object_id IS NOT NULL);

/* 4b. key columns of the child table's indexes and keys */
IF @BR IS NOT NULL
BEGIN
    ;WITH expected (name, cols) AS (
        SELECT * FROM (VALUES
            (N'PK_BulkRecipientAccount',                         N'BulkRecipient_Key'),
            (N'UQ_BulkRecipientAccount_Transaction_Line',        N'Transaction_Key,Line_Number'),
            (N'UQ_BulkRecipientAccount_Transaction_BankAccount', N'Transaction_Key,BankAccount_Key'),
            (N'IX_BulkRecipientAccount_BankAccount',             N'BankAccount_Key')
        ) v (name, cols)
    ),
    actual AS (
        SELECT i.name, STRING_AGG(c.name, N',') WITHIN GROUP (ORDER BY ic.key_ordinal) AS cols
        FROM sys.indexes i
        JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id AND ic.is_included_column = 0
        JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
        WHERE i.object_id = @BR AND i.type > 0
        GROUP BY i.name
    )
    INSERT INTO @Problems (msg)
    SELECT CONCAT(N'Index/key ', COALESCE(e.name, a.name),
                  CASE WHEN a.name IS NULL THEN N' is missing' WHEN e.name IS NULL THEN N' is not approved'
                       ELSE CONCAT(N' has different key columns (', a.cols, N')') END, N'.')
    FROM expected e FULL JOIN actual a ON a.name = e.name
    WHERE a.name IS NULL OR e.name IS NULL OR a.cols <> e.cols;
END;

/* 4c. the original ETransaction indexes are unchanged (only the PK and the Request_ID unique constraint) */
INSERT INTO @Problems (msg)
SELECT CONCAT(N'Unexpected index on ETransaction: ', i.name, N' (the migration creates none).')
FROM sys.indexes i
WHERE i.object_id = @ET AND i.type > 0 AND i.name NOT IN (N'PK_Transaction', N'UQ_Transaction_RequestID');

SELECT N'SECTION 4 - constraints, keys, indexes, trust' AS section, CASE WHEN COUNT(*) = 0 THEN N'PASS' ELSE N'FAIL' END AS status,
       CASE WHEN @Hardened = 1 THEN N'hardened state detected' ELSE N'expanded (not hardened) state' END AS schema_state, COUNT(*) AS problems
FROM @Problems;

/* Definitions for human review (SQL Server stores check expressions in a normalised form). */
SELECT OBJECT_NAME(cc.parent_object_id) AS table_name, cc.name AS check_constraint, cc.definition, cc.is_disabled, cc.is_not_trusted
FROM sys.check_constraints cc
WHERE cc.parent_object_id IN (@ET, @BR)
ORDER BY table_name, cc.name;

IF EXISTS (SELECT 1 FROM @Problems)
BEGIN
    SELECT id, msg AS problem FROM @Problems ORDER BY id;
    THROW 50204, N'SECTION 4 FAILED: constraints / keys / indexes / trust do not match the approved schema (see the problem list).', 1;
END;
GO

/* ============================== 5. APPLICATION PRINCIPAL PERMISSIONS  (PASS / FAIL) ============================== */
/* The application must be able to SELECT, INSERT and DELETE on BulkRecipientAccount, and must hold NO ALTER / CREATE / CONTROL /
   REFERENCES / ownership rights and no db_owner / db_ddladmin membership.  Direct, schema-level and database-level grants are
   considered (effective permissions of another principal cannot be read without impersonation, which this script never does). */
DECLARE @AppPrincipal sysname = N'E-Transaction Approval';
DECLARE @pid int = DATABASE_PRINCIPAL_ID(@AppPrincipal), @BR int = OBJECT_ID(N'etransactions.BulkRecipientAccount', N'U'), @schema int = SCHEMA_ID(N'etransactions');
DECLARE @Problems TABLE (id int IDENTITY(1,1) PRIMARY KEY, msg nvarchar(600) NOT NULL);

IF @pid IS NULL INSERT INTO @Problems (msg) VALUES (N'Application principal not found - set @AppPrincipal.');
IF @BR IS NULL INSERT INTO @Problems (msg) VALUES (N'BulkRecipientAccount not found.');

IF @pid IS NOT NULL AND @BR IS NOT NULL
BEGIN
    INSERT INTO @Problems (msg)
    SELECT CONCAT(N'Application principal lacks ', p.perm, N' on BulkRecipientAccount (object, schema or database level).')
    FROM (VALUES (N'SELECT'), (N'INSERT'), (N'DELETE')) p (perm)
    WHERE NOT EXISTS (SELECT 1 FROM sys.database_permissions dp
                       WHERE dp.grantee_principal_id = @pid AND dp.permission_name = p.perm AND dp.state IN (N'G', N'W')
                         AND ((dp.class = 1 AND dp.major_id = @BR AND dp.minor_id = 0) OR (dp.class = 3 AND dp.major_id = @schema) OR dp.class = 0));

    INSERT INTO @Problems (msg)
    SELECT CONCAT(N'Application principal holds a permission it must not have: ', dp.permission_name, N' (', dp.class_desc, N').')
    FROM sys.database_permissions dp
    WHERE dp.grantee_principal_id = @pid AND dp.state IN (N'G', N'W')
      AND (dp.permission_name IN (N'ALTER', N'CONTROL', N'TAKE OWNERSHIP', N'REFERENCES', N'ALTER ANY SCHEMA', N'ALTER ANY USER', N'ALTER ANY ROLE',
                                  N'CREATE TABLE', N'CREATE SCHEMA', N'CREATE VIEW', N'CREATE PROCEDURE', N'CREATE FUNCTION', N'VIEW ANY DEFINITION')
           OR dp.permission_name LIKE N'CREATE %' OR dp.permission_name LIKE N'ALTER %');

    INSERT INTO @Problems (msg)
    SELECT CONCAT(N'Application principal has an unneeded permission on BulkRecipientAccount: ', dp.permission_name, N'.')
    FROM sys.database_permissions dp
    WHERE dp.class = 1 AND dp.major_id = @BR AND dp.grantee_principal_id = @pid AND dp.state IN (N'G', N'W')
      AND dp.permission_name NOT IN (N'SELECT', N'INSERT', N'DELETE');

    INSERT INTO @Problems (msg)
    SELECT CONCAT(N'Application principal is a member of role ', r.name, N'.')
    FROM sys.database_role_members m
    JOIN sys.database_principals r ON r.principal_id = m.role_principal_id
    WHERE m.member_principal_id = @pid AND r.name IN (N'db_owner', N'db_ddladmin', N'db_securityadmin');
END;

SELECT N'SECTION 5 - application permissions' AS section, CASE WHEN COUNT(*) = 0 THEN N'PASS' ELSE N'FAIL' END AS status, COUNT(*) AS problems FROM @Problems;

SELECT pr.name AS principal, p.class_desc, CASE WHEN p.class = 1 THEN OBJECT_NAME(p.major_id) ELSE N'' END AS object_name, p.permission_name, p.state_desc
FROM sys.database_permissions p JOIN sys.database_principals pr ON pr.principal_id = p.grantee_principal_id
WHERE pr.name = @AppPrincipal ORDER BY p.class_desc, object_name, p.permission_name;

SELECT N'MANUAL REVIEW REQUIRED (not checkable from SQL): Fabric mirroring / OneLake / SQL analytics endpoint / reporting access to etransactions.BulkRecipientAccount. '
       + N'The table links transactions to bank-account keys; restrict it to authorized consumers.' AS reminder;

IF EXISTS (SELECT 1 FROM @Problems)
BEGIN
    SELECT id, msg AS problem FROM @Problems ORDER BY id;
    THROW 50205, N'SECTION 5 FAILED: application permissions are not exactly what the current implementation needs (see the problem list).', 1;
END;
GO

/* ============================== 6. DATA INTEGRITY  (A = violations -> PASS / FAIL;  B = informational only) ============================== */
/* 6A - ACTUAL DATA-INTEGRITY VIOLATIONS.  Every check_name must show violations = 0.  A non-zero count FAILS the section. */
DECLARE @Violations TABLE (check_name nvarchar(300) NOT NULL, violations int NOT NULL);
INSERT @Violations (check_name, violations)
SELECT check_name, violations FROM (
    SELECT N'child rows whose parent is not a Bulk transaction' AS check_name, COUNT(*) AS violations
      FROM etransactions.BulkRecipientAccount b JOIN etransactions.ETransaction t ON t.Transaction_Key = b.Transaction_Key
      WHERE t.Recipient_Mode IS NULL OR t.Recipient_Mode <> N'Bulk'
    UNION ALL SELECT N'Bulk parents whose Bulk_Recipient_Count <> number of child rows', COUNT(*)
      FROM etransactions.ETransaction t
      WHERE t.Bulk_Recipient_Count IS NOT NULL
        AND t.Bulk_Recipient_Count <> (SELECT COUNT(*) FROM etransactions.BulkRecipientAccount b WHERE b.Transaction_Key = t.Transaction_Key)
    UNION ALL SELECT N'child Listing_SHA256 differs from the parent Bulk_Listing_SHA256', COUNT(*)
      FROM etransactions.BulkRecipientAccount b JOIN etransactions.ETransaction t ON t.Transaction_Key = b.Transaction_Key
      WHERE t.Bulk_Listing_SHA256 IS NULL OR b.Listing_SHA256 <> t.Bulk_Listing_SHA256
    UNION ALL SELECT N'SUBMITTED Bulk transactions with zero recipient rows (a table constraint cannot enforce this)', COUNT(*)
      FROM etransactions.ETransaction t
      WHERE t.Recipient_Mode = N'Bulk' AND t.Current_Status <> N'Draft'
        AND NOT EXISTS (SELECT 1 FROM etransactions.BulkRecipientAccount b WHERE b.Transaction_Key = t.Transaction_Key)
    UNION ALL SELECT N'Bulk rows that still carry a beneficiary / instruction / recipient-account link', COUNT(*)
      FROM etransactions.ETransaction t
      WHERE t.Recipient_Mode = N'Bulk'
        AND (t.Beneficiary_Key IS NOT NULL OR t.BeneficiaryInstruction_Key IS NOT NULL OR t.RecipientBankAccount_Key IS NOT NULL)
    UNION ALL SELECT N'External/Internal rows missing a beneficiary or instruction', COUNT(*)
      FROM etransactions.ETransaction t
      WHERE t.Recipient_Mode IN (N'External', N'Internal') AND (t.Beneficiary_Key IS NULL OR t.BeneficiaryInstruction_Key IS NULL)
    UNION ALL SELECT N'rows with NULL Beneficiary_Key / BeneficiaryInstruction_Key that are not Bulk', COUNT(*)
      FROM etransactions.ETransaction t
      WHERE (t.Beneficiary_Key IS NULL OR t.BeneficiaryInstruction_Key IS NULL) AND ISNULL(t.Recipient_Mode, N'') <> N'Bulk'
    UNION ALL SELECT N'orphaned child rows (no parent transaction)', COUNT(*)
      FROM etransactions.BulkRecipientAccount b
      WHERE NOT EXISTS (SELECT 1 FROM etransactions.ETransaction t WHERE t.Transaction_Key = b.Transaction_Key)
    UNION ALL SELECT N'orphaned child rows (no BankAccount)', COUNT(*)
      FROM etransactions.BulkRecipientAccount b
      WHERE NOT EXISTS (SELECT 1 FROM etransactions.BankAccount a WHERE a.BankAccount_Key = b.BankAccount_Key)
    UNION ALL SELECT N'recipient equals the transaction''s own originating account', COUNT(*)
      FROM etransactions.BulkRecipientAccount b JOIN etransactions.ETransaction t ON t.Transaction_Key = b.Transaction_Key
      WHERE b.BankAccount_Key = t.OriginatingBankAccount_Key
    UNION ALL SELECT N'listing metadata on non-Bulk rows', COUNT(*)
      FROM etransactions.ETransaction t
      WHERE ISNULL(t.Recipient_Mode, N'') <> N'Bulk'
        AND (t.Bulk_Listing_SHA256 IS NOT NULL OR t.Bulk_Recipient_Count IS NOT NULL OR t.Bulk_Listing_Validated_DateTime IS NOT NULL)
) x;

SELECT check_name, violations, CASE WHEN violations = 0 THEN N'PASS' ELSE N'FAIL' END AS status FROM @Violations ORDER BY check_name;

/* 6B - INFORMATIONAL FINDINGS (never fail the validation).  A recipient account that was Open when the transaction was validated and
   finalized and is Closed NOW is normal history: the application requires Open only when a NEW bulk transaction is validated and finalized. */
SELECT N'6B informational: SUBMITTED Bulk transactions listing a recipient account that is Closed NOW (historical; review only)' AS info_item,
       COUNT(*) AS rows_found
FROM etransactions.BulkRecipientAccount b
JOIN etransactions.ETransaction t ON t.Transaction_Key = b.Transaction_Key
JOIN etransactions.BankAccount a ON a.BankAccount_Key = b.BankAccount_Key
WHERE t.Current_Status <> N'Draft' AND a.Status <> N'Open'
UNION ALL SELECT N'6B informational: Bulk DRAFTS whose listed recipients are not all Open now (re-verified at submission)', COUNT(DISTINCT b.Transaction_Key)
FROM etransactions.BulkRecipientAccount b
JOIN etransactions.ETransaction t ON t.Transaction_Key = b.Transaction_Key
JOIN etransactions.BankAccount a ON a.BankAccount_Key = b.BankAccount_Key
WHERE t.Current_Status = N'Draft' AND a.Status <> N'Open'
UNION ALL SELECT N'6B informational: Bulk DRAFTS with no verified listing yet', COUNT(*)
FROM etransactions.ETransaction t
WHERE t.Recipient_Mode = N'Bulk' AND t.Current_Status = N'Draft' AND NOT EXISTS (SELECT 1 FROM etransactions.BulkRecipientAccount b WHERE b.Transaction_Key = t.Transaction_Key)
UNION ALL SELECT N'6B informational: transactions with NULL Recipient_Mode (historical, or created while the create switch was off)', COUNT(*)
FROM etransactions.ETransaction t WHERE t.Recipient_Mode IS NULL
UNION ALL SELECT N'6B informational: duplicate (AccountNumber, RoutingNumber) pairs in BankAccount (the app blocks ambiguous matches)', COUNT(*)
FROM (SELECT AccountNumber, RoutingNumber FROM etransactions.BankAccount GROUP BY AccountNumber, RoutingNumber HAVING COUNT(*) > 1) d;

SELECT N'SECTION 6 - data integrity (6A violations only)' AS section,
       CASE WHEN EXISTS (SELECT 1 FROM @Violations WHERE violations > 0) THEN N'FAIL' ELSE N'PASS' END AS status,
       (SELECT COUNT(*) FROM @Violations WHERE violations > 0) AS failing_checks;

IF EXISTS (SELECT 1 FROM @Violations WHERE violations > 0)
    THROW 50206, N'SECTION 6 FAILED: data-integrity violations exist (6A). Closed-account findings in 6B are informational and are NOT the cause.', 1;
GO

/* ============================== 7. BACKFILL CORRECTNESS  (PASS / FAIL; run after 02 with @Commit = 1) ============================== */
SELECT ISNULL(Recipient_Mode, N'(NULL)') AS recipient_mode, COUNT(*) AS txn_count,
       SUM(CASE WHEN RecipientBankAccount_Key IS NOT NULL THEN 1 ELSE 0 END) AS with_recipient_account,
       SUM(CASE WHEN Beneficiary_Key IS NULL THEN 1 ELSE 0 END) AS without_beneficiary
FROM etransactions.ETransaction GROUP BY Recipient_Mode ORDER BY recipient_mode;

DECLARE @Violations TABLE (check_name nvarchar(300) NOT NULL, violations int NOT NULL);
INSERT @Violations (check_name, violations)
SELECT N'Internal rows without a recipient account', COUNT(*) FROM etransactions.ETransaction WHERE Recipient_Mode = N'Internal' AND RecipientBankAccount_Key IS NULL
UNION ALL SELECT N'External rows with a recipient account', COUNT(*) FROM etransactions.ETransaction WHERE Recipient_Mode = N'External' AND RecipientBankAccount_Key IS NOT NULL
UNION ALL SELECT N'Bulk rows with a beneficiary, instruction or recipient account', COUNT(*) FROM etransactions.ETransaction
          WHERE Recipient_Mode = N'Bulk' AND (Beneficiary_Key IS NOT NULL OR BeneficiaryInstruction_Key IS NOT NULL OR RecipientBankAccount_Key IS NOT NULL);

SELECT check_name, violations, CASE WHEN violations = 0 THEN N'PASS' ELSE N'FAIL' END AS status FROM @Violations;
SELECT N'SECTION 7 - backfill correctness' AS section,
       CASE WHEN EXISTS (SELECT 1 FROM @Violations WHERE violations > 0) THEN N'FAIL' ELSE N'PASS' END AS status,
       (SELECT COUNT(*) FROM etransactions.ETransaction WHERE Recipient_Mode IS NULL) AS null_mode_rows_informational;
/* NULL-mode rows are expected for rows 02 reported as AMBIGUOUS and for rows created while the create switch was off. */
IF EXISTS (SELECT 1 FROM @Violations WHERE violations > 0)
    THROW 50207, N'SECTION 7 FAILED: a stored Recipient_Mode disagrees with the beneficiary / recipient-account relationships.', 1;
GO

/* ============================== 8. OLD / NEW APPLICATION INSERT COMPATIBILITY  (PASS / FAIL) ============================== */
/* The old build INSERTs ETransaction with an explicit column list that omits the new columns.  It keeps working iff every column it omits
   is nullable or defaulted.  Explicit column names are used (no column_id arithmetic); the hardened state is recognised. */
DECLARE @ET int = OBJECT_ID(N'etransactions.ETransaction', N'U');
DECLARE @Hardened bit = CASE WHEN EXISTS (SELECT 1 FROM sys.check_constraints WHERE parent_object_id = @ET AND name = N'CK_ETransaction_RecipientMode_Links') THEN 1 ELSE 0 END;
DECLARE @Problems TABLE (id int IDENTITY(1,1) PRIMARY KEY, msg nvarchar(600) NOT NULL);

INSERT INTO @Problems (msg)
SELECT CONCAT(N'ETransaction.', c.name, N' is NOT NULL without a default: an INSERT that omits it fails.',
              CASE WHEN c.name = N'Recipient_Mode' THEN N' (Recipient_Mode is only allowed to be NOT NULL in the hardened state, which also requires an application build that writes it.)' ELSE N'' END)
FROM sys.columns c
WHERE c.object_id = @ET
  AND c.name IN (N'Recipient_Mode', N'Bulk_Listing_SHA256', N'Bulk_Recipient_Count', N'Bulk_Listing_Validated_DateTime')
  AND c.is_nullable = 0 AND c.default_object_id = 0
  AND NOT (c.name = N'Recipient_Mode' AND @Hardened = 1);

INSERT INTO @Problems (msg)
SELECT CONCAT(N'ETransaction.', c.name, N' has a default constraint (the approved design has none).')
FROM sys.columns c
WHERE c.object_id = @ET AND c.name IN (N'Recipient_Mode', N'Bulk_Listing_SHA256', N'Bulk_Recipient_Count', N'Bulk_Listing_Validated_DateTime') AND c.default_object_id <> 0;

INSERT INTO @Problems (msg)
SELECT CONCAT(N'ETransaction.', c.name, N' is NOT NULL: a bulk transaction cannot be stored.')
FROM sys.columns c
WHERE c.object_id = @ET AND c.name IN (N'Beneficiary_Key', N'BeneficiaryInstruction_Key') AND c.is_nullable = 0;

SELECT N'SECTION 8 - old/new application insert compatibility' AS section, CASE WHEN COUNT(*) = 0 THEN N'PASS' ELSE N'FAIL' END AS status,
       CASE WHEN @Hardened = 1 THEN N'hardened state detected' ELSE N'expanded (not hardened) state' END AS schema_state, COUNT(*) AS problems
FROM @Problems;
IF EXISTS (SELECT 1 FROM @Problems)
BEGIN
    SELECT id, msg AS problem FROM @Problems ORDER BY id;
    THROW 50208, N'SECTION 8 FAILED: an INSERT from the current or old application build could fail (see the problem list).', 1;
END;
GO

/* ============================== 9. FABRIC SQL CAPABILITY EVIDENCE (INFORMATION ONLY) ============================== */
/* Proven present on this database by read-only catalog queries: IDENTITY, UNIQUE constraints, nullable FK columns, datetime2(7), STRING_AGG,
   HAS_PERMS_BY_NAME, sys.foreign_keys / key_constraints / check_constraints.  SERVER_PROPERTY() is NOT supported (error 195) and is not used.
   NOT proven by executing DDL: DDL inside an explicit transaction, TRY/CATCH around DDL, ALTER COLUMN NOT NULL->NULL on a foreign-key column,
   WITH CHECK constraints, CREATE NONCLUSTERED INDEX, GRANT on the new table.  See BI_EXECUTION_README.md section 6. */
SELECT N'sys.check_constraints readable' AS probe, COUNT(*) AS n FROM sys.check_constraints
UNION ALL SELECT N'sys.default_constraints readable', COUNT(*) FROM sys.default_constraints
UNION ALL SELECT N'sys.key_constraints readable', COUNT(*) FROM sys.key_constraints;
GO

/* ============================== 10. NON-PRODUCTION TEST CASES (guidance - NOTHING BELOW RUNS) ==============================
   Run ONLY on a NON-PRODUCTION copy of DB_APP_SUPPORT.  Prefer driving cases A-C through the APPLICATION (non-prod instance).
   Raw-SQL probes must be inside BEGIN TRAN ... ROLLBACK and are shown only as text.

   A. Schema state tests
     A1. Before 01: start the app with BULK_RECIPIENT_ENABLED=false. Dashboard, detail, CSV export, Draft, intake submit work.
     A2. After 01 only (switch still false): repeat A1. A new External and a new Internal request submit successfully; Recipient_Mode stays NULL for them.
     A3. After 02 (@Commit = 1): sections 3B, 6, 7 return PASS.
   B. Old-application compatibility (build WITHOUT Recipient_Mode, against the expanded schema)
     B1. Submit External ACH and Internal ACH, save/resume/finalize a Draft: all succeed; section 6 stays PASS.
   C. New application, create switch ON (BULK_RECIPIENT_ENABLED=true; SharePoint DocumentType 'Bulk Recipient Listing' present)
     C1. Corporate ACH, Same Day ACH, Wire bulk submission with a valid listing -> one ETransaction, Recipient_Mode='Bulk', Beneficiary_Key and
         BeneficiaryInstruction_Key NULL, N child rows, hash/count/timestamp set, ONE workflow (same WorkflowAssignment/WorkflowEvent rows as a normal request).
     C2. Listing with one unmatched / ambiguous / closed / originating / duplicate row -> blocked; no ETransaction, no child rows, no workflow rows.
     C3. Save Draft -> Resume -> replace the listing -> Submit: child rows and hash belong to the replacement file.
     C4. Close one listed BankAccount between the listing check and submit (non-prod) -> submission refused ("no longer eligible"); rolled back.
     C5. Switch the create gate OFF: existing Bulk transactions still display as Bulk; approvals/Treasury actions work; new Bulk is refused.
     C6. Sections 4, 5, 6, 8 return PASS after C1-C5.  (6B may list Closed recipient accounts: informational.)
   D. Constraint probes (raw SQL, NON-PRODUCTION ONLY; each must FAIL, inside a transaction that is rolled back):
         BEGIN TRAN;
           UPDATE etransactions.ETransaction SET Recipient_Mode = 'Other' WHERE Transaction_Key = <a test key>;               -- CK_ETransaction_RecipientMode
           UPDATE etransactions.ETransaction SET Bulk_Listing_SHA256 = REPLICATE('a',64) WHERE Transaction_Key = <a test key>; -- CK_ETransaction_BulkMetadata
           -- duplicate (Transaction_Key, Line_Number) or (Transaction_Key, BankAccount_Key)  -> UQ_BulkRecipientAccount_*
           -- child row with a non-existent BankAccount_Key                                  -> FK_BulkRecipientAccount_BankAccount
         ROLLBACK;
   E. Masked-column predicate (as the APPLICATION principal, non-prod): with a known non-production account/routing number,
         SELECT BankAccount_Key, Status FROM etransactions.BankAccount WHERE AccountNumber = @acct AND RoutingNumber = @rtn;
      must return that row although the columns are masked on output.  NOT yet proven for BankAccount - verify before enabling the feature.
   F. Concurrency (non-prod): two sessions finalize the same Bulk Draft -> exactly one succeeds; a second session closes a listed account just
      before commit -> record the observed outcome (snapshot isolation: see the schema contract, section H).
   ============================================================================================================== */
