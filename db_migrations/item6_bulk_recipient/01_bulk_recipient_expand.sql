/* ============================================================================
   Item 6 - Corporate Bulk-Recipient Payments
   01_bulk_recipient_expand.sql   (BACKWARD-COMPATIBLE EXPANSION)

   Target   : Fabric SQL Database (DB_APP_SUPPORT), schema [etransactions]
   Run as   : an administrative principal with ALTER / CREATE TABLE / GRANT rights and VIEW DEFINITION
              (the application principal [E-Transaction Approval] has none of these - verified read-only).
   Status   : NOT EXECUTED by the author. Run in NON-PRODUCTION first. Run as ONE batch (no GO inside).

   What it does
     1. ETransaction: adds 4 NULLABLE columns (no defaults):
          Recipient_Mode                  varchar(20)  NULL
          Bulk_Listing_SHA256             char(64)     NULL
          Bulk_Recipient_Count            int          NULL
          Bulk_Listing_Validated_DateTime datetime2(7) NULL
     2. ETransaction: Beneficiary_Key and BeneficiaryInstruction_Key  NOT NULL -> NULL (type stays int;
        the two existing foreign keys are never dropped, recreated or modified).
     3. Creates etransactions.BulkRecipientAccount (internal-only child table; NO banking numbers).
     4. Adds NULL-permitting CHECK constraints on the new ETransaction columns.
     5. Grants the application principal SELECT, INSERT, DELETE on the new table - nothing else.

   What it never does
     - No NOT NULL on Recipient_Mode, no default, no mode-vs-link CHECK (see 03, DEFERRED).
     - No INSERT/UPDATE/DELETE of any row, no DROP of anything, no trigger.
     - No change to Beneficiary, BeneficiaryBankInstruction, TransactionVerification, BankAccount, AppUser or workflow tables.
     - No ALTER / CREATE / schema-ownership permission to the application.

   Safety design
     PHASE 1  read-only DEFINITION VERIFICATION (before anything changes).  Every object that already exists is compared
              with the approved definition (type, length, precision, scale, nullability, identity, defaults, keys, foreign keys,
              unique/check constraints, indexes).  Any mismatch, any unapproved extra object, and any dependency that could
              affect ALTER COLUMN stops the script with a clear error.  Mismatched objects are NEVER skipped, dropped or recreated.
     PHASE 2  the changes, inside ONE explicit transaction (guarded, so a clean re-run changes nothing).
     PHASE 3  the same verification, now requiring every object to exist, BEFORE COMMIT.  If anything is off, everything rolls back.
     TRY/CATCH: any error rolls the transaction back, prints the ORIGINAL error (number, line, message) and re-raises it with THROW;
              the script never reports success after a failure.  SET XACT_ABORT ON is kept.

   Existing-object dependency facts (catalog, read-only): ETransaction has no CHECK/DEFAULT constraints, no triggers, only
   PK_Transaction and UQ_Transaction_RequestID as indexes; the two link columns are covered only by FK_Transaction_Beneficiary and
   FK_Transaction_BeneficiaryInstruction.  PHASE 1/3 re-prove this at run time.

   NOT engine-tested by running (no DDL was run by the author): explicit DDL transactions, ALTER COLUMN NOT NULL->NULL on a foreign-key
   column, WITH CHECK constraints, CREATE NONCLUSTERED INDEX and GRANT on this engine.  See BI_EXECUTION_README.md.

   MIRRORING / REPORTING NOTE (BI): BulkRecipientAccount links transactions to bank-account keys.  Before enabling the feature,
   review whether Fabric mirroring / OneLake / SQL analytics endpoints expose this table, and restrict it to authorized consumers.
   ============================================================================ */

SET NOCOUNT ON;
SET XACT_ABORT ON;

/* >>> BI: confirm this is the database principal the Flask application connects as.
       The application's own catalog lookup (USER_NAME()) returned: E-Transaction Approval */
DECLARE @AppPrincipal sysname = N'E-Transaction Approval';

DECLARE @Problems TABLE (id int IDENTITY(1,1) PRIMARY KEY, msg nvarchar(600) NOT NULL);
DECLARE @Phase int = 1, @Strict bit = 0, @Msg nvarchar(2048), @DdlSql nvarchar(400);
DECLARE @ET int, @BR int;

BEGIN TRY

    PRINT CONCAT(N'01_bulk_recipient_expand.sql   Database: ', DB_NAME(), N'   Started: ', CONVERT(nvarchar(30), SYSDATETIME(), 126));

    /* ---------------------------------------------------------------- 0. BASIC PREFLIGHT */
    SET @ET = OBJECT_ID(N'etransactions.ETransaction', N'U');
    IF @ET IS NULL
        THROW 50001, N'Preflight: etransactions.ETransaction not found. Wrong database?', 1;
    IF OBJECT_ID(N'etransactions.BankAccount', N'U') IS NULL
        THROW 50002, N'Preflight: etransactions.BankAccount not found. Wrong database?', 1;
    IF NOT EXISTS (SELECT 1 FROM sys.columns c JOIN sys.types ty ON ty.user_type_id = c.user_type_id
                    WHERE c.object_id = @ET AND c.name = N'Transaction_Key' AND ty.name = N'int')
        THROW 50003, N'Preflight: ETransaction.Transaction_Key is not int (child foreign key would not match).', 1;
    IF NOT EXISTS (SELECT 1 FROM sys.columns c JOIN sys.types ty ON ty.user_type_id = c.user_type_id
                    WHERE c.object_id = OBJECT_ID(N'etransactions.BankAccount') AND c.name = N'BankAccount_Key' AND ty.name = N'int')
        THROW 50004, N'Preflight: BankAccount.BankAccount_Key is not int (child foreign key would not match).', 1;
    IF DATABASE_PRINCIPAL_ID(@AppPrincipal) IS NULL
        THROW 50006, N'Preflight: @AppPrincipal does not exist in this database. Set it to the application principal.', 1;

    WHILE @Phase <= 3
    BEGIN
        IF @Phase = 2
        BEGIN
            /* ============================================================ PHASE 2 : THE CHANGES (one transaction) */
            BEGIN TRANSACTION;

            /* ---- 1. New nullable columns (no defaults) */
            IF COL_LENGTH(N'etransactions.ETransaction', N'Recipient_Mode') IS NULL
                ALTER TABLE etransactions.ETransaction ADD Recipient_Mode varchar(20) NULL;
            IF COL_LENGTH(N'etransactions.ETransaction', N'Bulk_Listing_SHA256') IS NULL
                ALTER TABLE etransactions.ETransaction ADD Bulk_Listing_SHA256 char(64) NULL;
            IF COL_LENGTH(N'etransactions.ETransaction', N'Bulk_Recipient_Count') IS NULL
                ALTER TABLE etransactions.ETransaction ADD Bulk_Recipient_Count int NULL;
            IF COL_LENGTH(N'etransactions.ETransaction', N'Bulk_Listing_Validated_DateTime') IS NULL
                ALTER TABLE etransactions.ETransaction ADD Bulk_Listing_Validated_DateTime datetime2(7) NULL;

            /* ---- 2. Relax the single-beneficiary links: NOT NULL -> NULL only; type stays int.
                   FK_Transaction_Beneficiary / FK_Transaction_BeneficiaryInstruction are NOT touched; PHASE 3 re-proves they are
                   still present, enabled, trusted and unchanged. */
            IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = @ET AND name = N'Beneficiary_Key' AND is_nullable = 0)
                ALTER TABLE etransactions.ETransaction ALTER COLUMN Beneficiary_Key int NULL;
            IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = @ET AND name = N'BeneficiaryInstruction_Key' AND is_nullable = 0)
                ALTER TABLE etransactions.ETransaction ALTER COLUMN BeneficiaryInstruction_Key int NULL;

            /* ---- 3. Child table (internal recipients only; no banking numbers, no external-recipient targets) */
            IF OBJECT_ID(N'etransactions.BulkRecipientAccount', N'U') IS NULL
            BEGIN
                CREATE TABLE etransactions.BulkRecipientAccount (
                    BulkRecipient_Key    int IDENTITY(1,1) NOT NULL,
                    Transaction_Key      int          NOT NULL,
                    Line_Number          int          NOT NULL,
                    BankAccount_Key      int          NOT NULL,
                    Verification_Outcome varchar(20)  NOT NULL,
                    Verified_DateTime    datetime2(7) NOT NULL,
                    Listing_SHA256       char(64)     NOT NULL,
                    CONSTRAINT PK_BulkRecipientAccount PRIMARY KEY CLUSTERED (BulkRecipient_Key)
                );
            END;

            IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = N'FK_BulkRecipientAccount_Transaction'
                              AND parent_object_id = OBJECT_ID(N'etransactions.BulkRecipientAccount'))
                ALTER TABLE etransactions.BulkRecipientAccount WITH CHECK
                    ADD CONSTRAINT FK_BulkRecipientAccount_Transaction
                    FOREIGN KEY (Transaction_Key) REFERENCES etransactions.ETransaction (Transaction_Key);

            IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = N'FK_BulkRecipientAccount_BankAccount'
                              AND parent_object_id = OBJECT_ID(N'etransactions.BulkRecipientAccount'))
                ALTER TABLE etransactions.BulkRecipientAccount WITH CHECK
                    ADD CONSTRAINT FK_BulkRecipientAccount_BankAccount
                    FOREIGN KEY (BankAccount_Key) REFERENCES etransactions.BankAccount (BankAccount_Key);

            IF NOT EXISTS (SELECT 1 FROM sys.key_constraints WHERE name = N'UQ_BulkRecipientAccount_Transaction_Line'
                              AND parent_object_id = OBJECT_ID(N'etransactions.BulkRecipientAccount'))
                ALTER TABLE etransactions.BulkRecipientAccount
                    ADD CONSTRAINT UQ_BulkRecipientAccount_Transaction_Line UNIQUE (Transaction_Key, Line_Number);

            IF NOT EXISTS (SELECT 1 FROM sys.key_constraints WHERE name = N'UQ_BulkRecipientAccount_Transaction_BankAccount'
                              AND parent_object_id = OBJECT_ID(N'etransactions.BulkRecipientAccount'))
                ALTER TABLE etransactions.BulkRecipientAccount
                    ADD CONSTRAINT UQ_BulkRecipientAccount_Transaction_BankAccount UNIQUE (Transaction_Key, BankAccount_Key);

            IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'IX_BulkRecipientAccount_BankAccount'
                              AND object_id = OBJECT_ID(N'etransactions.BulkRecipientAccount'))
                CREATE NONCLUSTERED INDEX IX_BulkRecipientAccount_BankAccount
                    ON etransactions.BulkRecipientAccount (BankAccount_Key);

            IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = N'CK_BulkRecipientAccount_Outcome'
                              AND parent_object_id = OBJECT_ID(N'etransactions.BulkRecipientAccount'))
                ALTER TABLE etransactions.BulkRecipientAccount WITH CHECK
                    ADD CONSTRAINT CK_BulkRecipientAccount_Outcome CHECK (Verification_Outcome IN ('Matched'));

            IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = N'CK_BulkRecipientAccount_Line'
                              AND parent_object_id = OBJECT_ID(N'etransactions.BulkRecipientAccount'))
                ALTER TABLE etransactions.BulkRecipientAccount WITH CHECK
                    ADD CONSTRAINT CK_BulkRecipientAccount_Line CHECK (Line_Number >= 2);

            IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = N'CK_BulkRecipientAccount_Hash'
                              AND parent_object_id = OBJECT_ID(N'etransactions.BulkRecipientAccount'))
                ALTER TABLE etransactions.BulkRecipientAccount WITH CHECK
                    ADD CONSTRAINT CK_BulkRecipientAccount_Hash CHECK (LEN(Listing_SHA256) = 64);

            /* ---- 4. ETransaction CHECKs (NULL-permitting).  Dynamic SQL so they bind to columns added earlier in this batch. */
            IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = N'CK_ETransaction_RecipientMode' AND parent_object_id = @ET)
                EXEC (N'ALTER TABLE etransactions.ETransaction WITH CHECK
                        ADD CONSTRAINT CK_ETransaction_RecipientMode
                        CHECK (Recipient_Mode IS NULL OR Recipient_Mode IN (''External'', ''Internal'', ''Bulk''))');

            IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = N'CK_ETransaction_BulkCount' AND parent_object_id = @ET)
                EXEC (N'ALTER TABLE etransactions.ETransaction WITH CHECK
                        ADD CONSTRAINT CK_ETransaction_BulkCount
                        CHECK (Bulk_Recipient_Count IS NULL OR Bulk_Recipient_Count > 0)');

            /* The application writes hash, count and timestamp in ONE UPDATE, so they are all NULL or all set. */
            IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = N'CK_ETransaction_BulkMetadata' AND parent_object_id = @ET)
                EXEC (N'ALTER TABLE etransactions.ETransaction WITH CHECK
                        ADD CONSTRAINT CK_ETransaction_BulkMetadata
                        CHECK ((Bulk_Listing_SHA256 IS NULL AND Bulk_Recipient_Count IS NULL AND Bulk_Listing_Validated_DateTime IS NULL)
                            OR (Bulk_Listing_SHA256 IS NOT NULL AND Bulk_Recipient_Count IS NOT NULL AND Bulk_Listing_Validated_DateTime IS NOT NULL))');

            /* ---- 5. Permissions: only what the current application needs on the NEW table (no ALTER, CREATE, REFERENCES,
                   UPDATE, CONTROL or ownership).  The application already holds table-level UPDATE on ETransaction, so it can
                   write the new columns; SELECT also makes the columns visible to its INFORMATION_SCHEMA readiness check. */
            SET @DdlSql = N'GRANT SELECT, INSERT, DELETE ON OBJECT::etransactions.BulkRecipientAccount TO ' + QUOTENAME(@AppPrincipal) + N';';
            EXEC (@DdlSql);
        END
        ELSE
        BEGIN
            /* ============================================================ PHASES 1 and 3 : DEFINITION VERIFICATION (read-only)
               @Phase 1 = before the changes: existing objects must match; missing objects are fine.
               @Phase 3 = before COMMIT: every approved object must exist and match.
               Nothing below changes data or objects. */
            SET @Strict = CASE WHEN @Phase = 3 THEN 1 ELSE 0 END;
            SET @BR = OBJECT_ID(N'etransactions.BulkRecipientAccount', N'U');
            DELETE FROM @Problems;

            /* -- A. the four new ETransaction columns: name, type, length, precision, scale, nullability, identity/computed/default */
            ;WITH expected (col, typ, max_len, prec, scale, nullable) AS (
                SELECT * FROM (VALUES
                    (N'Recipient_Mode',                  N'varchar',   20,  0, 0, 1),
                    (N'Bulk_Listing_SHA256',             N'char',      64,  0, 0, 1),
                    (N'Bulk_Recipient_Count',            N'int',        4, 10, 0, 1),
                    (N'Bulk_Listing_Validated_DateTime', N'datetime2',  8, 27, 7, 1)
                ) v (col, typ, max_len, prec, scale, nullable)
            )
            INSERT INTO @Problems (msg)
            SELECT CASE WHEN c.column_id IS NULL
                        THEN CONCAT(N'ETransaction.', e.col, N' is missing after the migration.')
                        ELSE CONCAT(N'ETransaction.', e.col, N' already exists with a DIFFERENT definition (found ', ty.name, N' len ', c.max_length,
                                    N' prec ', c.precision, N' scale ', c.scale, N' nullable ', c.is_nullable, N' identity ', c.is_identity,
                                    N' computed ', c.is_computed, N' default ', CASE WHEN c.default_object_id <> 0 THEN N'yes' ELSE N'no' END,
                                    N'; approved ', e.typ, N' len ', e.max_len, N' prec ', e.prec, N' scale ', e.scale, N' nullable ', e.nullable,
                                    N' no default). A hardened database (script 03) is also refused here.') END
            FROM expected e
            LEFT JOIN sys.columns c ON c.object_id = @ET AND c.name = e.col
            LEFT JOIN sys.types ty ON ty.user_type_id = c.user_type_id
            WHERE (c.column_id IS NULL AND @Strict = 1)
               OR (c.column_id IS NOT NULL AND (ty.name <> e.typ OR c.max_length <> e.max_len OR c.precision <> e.prec OR c.scale <> e.scale
                                                OR c.is_nullable <> e.nullable OR c.is_identity = 1 OR c.is_computed = 1 OR c.default_object_id <> 0));

            /* -- B. the two link columns: must be plain int; must be nullable once the ALTER has run */
            INSERT INTO @Problems (msg)
            SELECT CONCAT(N'ETransaction.', n.col, CASE WHEN c.column_id IS NULL THEN N' is missing.'
                                                        ELSE CONCAT(N' is not a plain int column (found ', ty.name, N').') END)
            FROM (VALUES (N'Beneficiary_Key'), (N'BeneficiaryInstruction_Key')) n (col)
            LEFT JOIN sys.columns c ON c.object_id = @ET AND c.name = n.col
            LEFT JOIN sys.types ty ON ty.user_type_id = c.user_type_id
            WHERE c.column_id IS NULL OR ty.name <> N'int' OR c.is_identity = 1 OR c.is_computed = 1 OR c.default_object_id <> 0;

            INSERT INTO @Problems (msg)
            SELECT CONCAT(N'ETransaction.', c.name, N' is still NOT NULL after the ALTER.')
            FROM sys.columns c
            WHERE c.object_id = @ET AND c.name IN (N'Beneficiary_Key', N'BeneficiaryInstruction_Key') AND c.is_nullable = 0 AND @Strict = 1;

            /* -- C. the existing foreign keys must be present, on the same columns/targets, enabled, trusted, no cascade */
            ;WITH expected_fk (fk, parent_col, ref_table, ref_col) AS (
                SELECT * FROM (VALUES
                    (N'FK_Transaction_Beneficiary',            N'Beneficiary_Key',          N'Beneficiary',                 N'Beneficiary_Key'),
                    (N'FK_Transaction_BeneficiaryInstruction', N'BeneficiaryInstruction_Key', N'BeneficiaryBankInstruction', N'BeneficiaryInstruction_Key')
                ) v (fk, parent_col, ref_table, ref_col)
            )
            INSERT INTO @Problems (msg)
            SELECT CONCAT(N'Existing foreign key ', e.fk, N' is missing, changed, disabled or not trusted. It must be retained exactly as is.')
            FROM expected_fk e
            WHERE NOT EXISTS (
                SELECT 1
                FROM sys.foreign_keys f
                JOIN sys.foreign_key_columns fc ON fc.constraint_object_id = f.object_id
                JOIN sys.columns pc ON pc.object_id = fc.parent_object_id AND pc.column_id = fc.parent_column_id
                JOIN sys.columns rc ON rc.object_id = fc.referenced_object_id AND rc.column_id = fc.referenced_column_id
                WHERE f.name = e.fk AND f.parent_object_id = @ET AND pc.name = e.parent_col
                  AND f.referenced_object_id = OBJECT_ID(N'etransactions.' + e.ref_table) AND rc.name = e.ref_col
                  AND f.is_disabled = 0 AND f.is_not_trusted = 0
                  AND f.delete_referential_action = 0 AND f.update_referential_action = 0
                  AND (SELECT COUNT(*) FROM sys.foreign_key_columns x WHERE x.constraint_object_id = f.object_id) = 1);

            INSERT INTO @Problems (msg)
            SELECT CONCAT(N'Unexpected foreign key ', f.name, N' uses ETransaction.', pc.name, N' (review; nothing is changed automatically).')
            FROM sys.foreign_keys f
            JOIN sys.foreign_key_columns fc ON fc.constraint_object_id = f.object_id
            JOIN sys.columns pc ON pc.object_id = fc.parent_object_id AND pc.column_id = fc.parent_column_id
            WHERE f.parent_object_id = @ET AND pc.name IN (N'Beneficiary_Key', N'BeneficiaryInstruction_Key')
              AND f.name NOT IN (N'FK_Transaction_Beneficiary', N'FK_Transaction_BeneficiaryInstruction');

            INSERT INTO @Problems (msg)
            SELECT CONCAT(N'Foreign key ', f.name, N' (from ', OBJECT_NAME(f.parent_object_id), N') references ETransaction.', rc.name, N'.')
            FROM sys.foreign_keys f
            JOIN sys.foreign_key_columns fc ON fc.constraint_object_id = f.object_id
            JOIN sys.columns rc ON rc.object_id = fc.referenced_object_id AND rc.column_id = fc.referenced_column_id
            WHERE f.referenced_object_id = @ET AND rc.name IN (N'Beneficiary_Key', N'BeneficiaryInstruction_Key');

            /* -- D. anything else that depends on the two link columns and could affect ALTER COLUMN (reported, never changed) */
            INSERT INTO @Problems (msg)
            SELECT CONCAT(N'Index ', i.name, N' includes ETransaction.', c.name, N' (review before ALTER COLUMN; not changed automatically).')
            FROM sys.index_columns ic
            JOIN sys.indexes i ON i.object_id = ic.object_id AND i.index_id = ic.index_id
            JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
            WHERE ic.object_id = @ET AND c.name IN (N'Beneficiary_Key', N'BeneficiaryInstruction_Key');

            INSERT INTO @Problems (msg)
            SELECT CONCAT(N'Check constraint ', cc.name, N' references a beneficiary link column.')
            FROM sys.check_constraints cc
            WHERE cc.parent_object_id = @ET
              AND (cc.definition LIKE N'%Beneficiary_Key%' OR cc.definition LIKE N'%BeneficiaryInstruction_Key%');

            INSERT INTO @Problems (msg)
            SELECT CONCAT(N'Computed column ', cc.name, N' references a beneficiary link column.')
            FROM sys.computed_columns cc
            WHERE cc.object_id = @ET
              AND (cc.definition LIKE N'%Beneficiary_Key%' OR cc.definition LIKE N'%BeneficiaryInstruction_Key%');

            INSERT INTO @Problems (msg)
            SELECT CONCAT(N'User-created statistics ', s.name, N' include ETransaction.', c.name, N'.')
            FROM sys.stats s
            JOIN sys.stats_columns sc ON sc.object_id = s.object_id AND sc.stats_id = s.stats_id
            JOIN sys.columns c ON c.object_id = sc.object_id AND c.column_id = sc.column_id
            WHERE s.object_id = @ET AND s.user_created = 1 AND c.name IN (N'Beneficiary_Key', N'BeneficiaryInstruction_Key');

            INSERT INTO @Problems (msg)
            SELECT CONCAT(N'Schema-bound object ', OBJECT_NAME(d.referencing_id), N' depends on ETransaction.')
            FROM sys.sql_expression_dependencies d
            WHERE d.referenced_id = @ET AND d.is_schema_bound_reference = 1;

            /* -- E. ETransaction CHECK constraints: approved ones must match; hardening ones (script 03) must not exist yet */
            ;WITH tokens (name, token) AS (
                SELECT * FROM (VALUES
                    (N'CK_ETransaction_RecipientMode', N'recipient_modeisnull'),
                    (N'CK_ETransaction_RecipientMode', N'recipient_mode=''external'''),
                    (N'CK_ETransaction_RecipientMode', N'recipient_mode=''internal'''),
                    (N'CK_ETransaction_RecipientMode', N'recipient_mode=''bulk'''),
                    (N'CK_ETransaction_BulkCount',     N'bulk_recipient_countisnull'),
                    (N'CK_ETransaction_BulkCount',     N'bulk_recipient_count>0'),
                    (N'CK_ETransaction_BulkMetadata',  N'bulk_listing_sha256isnull'),
                    (N'CK_ETransaction_BulkMetadata',  N'bulk_recipient_countisnull'),
                    (N'CK_ETransaction_BulkMetadata',  N'bulk_listing_validated_datetimeisnull'),
                    (N'CK_ETransaction_BulkMetadata',  N'bulk_listing_sha256isnotnull'),
                    (N'CK_ETransaction_BulkMetadata',  N'bulk_recipient_countisnotnull'),
                    (N'CK_ETransaction_BulkMetadata',  N'bulk_listing_validated_datetimeisnotnull')
                ) v (name, token)
            ),
            actual AS (
                SELECT cc.name, cc.is_disabled, cc.is_not_trusted,
                       LOWER(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(cc.definition, N'[', N''), N']', N''), N' ', N''), N'(', N''), N')', N'')) AS def
                FROM sys.check_constraints cc
                WHERE cc.parent_object_id = @ET
            )
            INSERT INTO @Problems (msg)
            SELECT CONCAT(N'ETransaction check constraint ', a.name, N' differs from the approved definition, is disabled or is not trusted.')
            FROM actual a
            WHERE a.name IN (SELECT name FROM tokens)
              AND (a.is_disabled = 1 OR a.is_not_trusted = 1
                   OR EXISTS (SELECT 1 FROM tokens t WHERE t.name = a.name AND CHARINDEX(t.token, a.def) = 0));

            INSERT INTO @Problems (msg)
            SELECT CONCAT(N'ETransaction check constraint ', cc.name, N' is not part of this migration but refers to a bulk column, or is a hardening constraint (script 03) that already exists.')
            FROM sys.check_constraints cc
            WHERE cc.parent_object_id = @ET
              AND cc.name NOT IN (N'CK_ETransaction_RecipientMode', N'CK_ETransaction_BulkCount', N'CK_ETransaction_BulkMetadata')
              AND (cc.name IN (N'CK_ETransaction_RecipientMode_Links', N'CK_ETransaction_BulkMetadata_Mode')
                   OR cc.definition LIKE N'%Recipient_Mode%' OR cc.definition LIKE N'%Bulk_Listing_%' OR cc.definition LIKE N'%Bulk_Recipient_Count%');

            INSERT INTO @Problems (msg)
            SELECT CONCAT(N'ETransaction check constraint ', n.name, N' is missing after the migration.')
            FROM (VALUES (N'CK_ETransaction_RecipientMode'), (N'CK_ETransaction_BulkCount'), (N'CK_ETransaction_BulkMetadata')) n (name)
            WHERE @Strict = 1
              AND NOT EXISTS (SELECT 1 FROM sys.check_constraints cc WHERE cc.parent_object_id = @ET AND cc.name = n.name);

            /* -- F. the child table, if it exists (or must exist, in phase 3) */
            IF @BR IS NULL
            BEGIN
                IF @Strict = 1
                    INSERT INTO @Problems (msg) VALUES (N'etransactions.BulkRecipientAccount is missing after the migration.');
            END
            ELSE
            BEGIN
                /* F1. columns: exact set, types, lengths, nullability, identity; nothing extra (e.g. a banking-number column) */
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
                SELECT CASE WHEN c.column_id IS NULL THEN CONCAT(N'BulkRecipientAccount.', e.col, N' is missing from the existing table.')
                            ELSE CONCAT(N'BulkRecipientAccount.', e.col, N' exists with a DIFFERENT definition (found ', ty.name, N' len ', c.max_length,
                                        N' prec ', c.precision, N' scale ', c.scale, N' nullable ', c.is_nullable, N' identity ', c.is_identity, N').') END
                FROM expected e
                LEFT JOIN sys.columns c ON c.object_id = @BR AND c.name = e.col
                LEFT JOIN sys.types ty ON ty.user_type_id = c.user_type_id
                WHERE c.column_id IS NULL
                   OR ty.name <> e.typ OR c.max_length <> e.max_len OR c.precision <> e.prec OR c.scale <> e.scale
                   OR c.is_nullable <> e.nullable OR c.is_identity <> e.ident OR c.is_computed = 1 OR c.default_object_id <> 0;

                INSERT INTO @Problems (msg)
                SELECT CONCAT(N'BulkRecipientAccount has an UNAPPROVED column ', c.name, N'.')
                FROM sys.columns c
                WHERE c.object_id = @BR
                  AND c.name NOT IN (N'BulkRecipient_Key', N'Transaction_Key', N'Line_Number', N'BankAccount_Key',
                                     N'Verification_Outcome', N'Verified_DateTime', N'Listing_SHA256');

                /* F2. primary key: PK_BulkRecipientAccount, clustered, on BulkRecipient_Key only */
                INSERT INTO @Problems (msg)
                SELECT N'BulkRecipientAccount primary key is missing or differs (approved: PK_BulkRecipientAccount, clustered, on BulkRecipient_Key only).'
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM sys.key_constraints k
                    JOIN sys.indexes i ON i.object_id = k.parent_object_id AND i.index_id = k.unique_index_id
                    JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
                    JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
                    WHERE k.parent_object_id = @BR AND k.type = N'PK' AND k.name = N'PK_BulkRecipientAccount' AND i.type_desc = N'CLUSTERED'
                    GROUP BY k.object_id
                    HAVING COUNT(*) = 1 AND MIN(c.name) = N'BulkRecipient_Key');

                /* F3. unique constraints: approved names and key columns; no others */
                ;WITH expected (name, cols) AS (
                    SELECT * FROM (VALUES
                        (N'UQ_BulkRecipientAccount_Transaction_Line',        N'Transaction_Key,Line_Number'),
                        (N'UQ_BulkRecipientAccount_Transaction_BankAccount', N'Transaction_Key,BankAccount_Key')
                    ) v (name, cols)
                ),
                actual AS (
                    SELECT k.name, STRING_AGG(c.name, N',') WITHIN GROUP (ORDER BY ic.key_ordinal) AS cols
                    FROM sys.key_constraints k
                    JOIN sys.index_columns ic ON ic.object_id = k.parent_object_id AND ic.index_id = k.unique_index_id
                    JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
                    WHERE k.parent_object_id = @BR AND k.type = N'UQ'
                    GROUP BY k.name
                )
                INSERT INTO @Problems (msg)
                SELECT CONCAT(N'Unique constraint ', COALESCE(e.name, a.name),
                              CASE WHEN a.name IS NULL THEN N' is missing'
                                   WHEN e.name IS NULL THEN N' is not approved'
                                   ELSE CONCAT(N' has different key columns (', a.cols, N')') END, N'.')
                FROM expected e FULL JOIN actual a ON a.name = e.name
                WHERE (a.name IS NOT NULL AND (e.name IS NULL OR a.cols <> e.cols))
                   OR (a.name IS NULL AND @Strict = 1);

                INSERT INTO @Problems (msg)
                SELECT CONCAT(N'Unapproved key constraint ', k.name, N' on BulkRecipientAccount.')
                FROM sys.key_constraints k
                WHERE k.parent_object_id = @BR
                  AND k.name NOT IN (N'PK_BulkRecipientAccount', N'UQ_BulkRecipientAccount_Transaction_Line', N'UQ_BulkRecipientAccount_Transaction_BankAccount');

                /* F4. foreign keys: approved names, columns, targets (ETransaction / BankAccount only), trusted, enabled, no cascade */
                ;WITH expected (name, pcol, rtab, rcol) AS (
                    SELECT * FROM (VALUES
                        (N'FK_BulkRecipientAccount_Transaction', N'Transaction_Key', N'ETransaction', N'Transaction_Key'),
                        (N'FK_BulkRecipientAccount_BankAccount', N'BankAccount_Key', N'BankAccount',  N'BankAccount_Key')
                    ) v (name, pcol, rtab, rcol)
                ),
                actual AS (
                    SELECT f.name, pc.name AS pcol, OBJECT_NAME(f.referenced_object_id) AS rtab, rc.name AS rcol,
                           OBJECT_SCHEMA_NAME(f.referenced_object_id) AS rschema, f.is_disabled, f.is_not_trusted,
                           f.delete_referential_action AS da, f.update_referential_action AS ua,
                           (SELECT COUNT(*) FROM sys.foreign_key_columns x WHERE x.constraint_object_id = f.object_id) AS ncols
                    FROM sys.foreign_keys f
                    JOIN sys.foreign_key_columns fc ON fc.constraint_object_id = f.object_id
                    JOIN sys.columns pc ON pc.object_id = fc.parent_object_id AND pc.column_id = fc.parent_column_id
                    JOIN sys.columns rc ON rc.object_id = fc.referenced_object_id AND rc.column_id = fc.referenced_column_id
                    WHERE f.parent_object_id = @BR
                )
                INSERT INTO @Problems (msg)
                SELECT CONCAT(N'Foreign key ', COALESCE(e.name, a.name),
                              CASE WHEN a.name IS NULL THEN N' is missing'
                                   WHEN e.name IS NULL THEN N' is not approved'
                                   ELSE N' differs from the approved definition (columns, target, enabled, trusted or cascade)' END, N'.')
                FROM expected e FULL JOIN actual a ON a.name = e.name
                WHERE (a.name IS NOT NULL AND (e.name IS NULL OR a.pcol <> e.pcol OR a.rtab <> e.rtab OR a.rcol <> e.rcol
                                               OR a.rschema <> N'etransactions' OR a.is_disabled = 1 OR a.is_not_trusted = 1
                                               OR a.da <> 0 OR a.ua <> 0 OR a.ncols <> 1))
                   OR (a.name IS NULL AND @Strict = 1);

                /* F5. check constraints: approved names with required tokens, enabled, trusted; no others */
                ;WITH tokens (name, token) AS (
                    SELECT * FROM (VALUES
                        (N'CK_BulkRecipientAccount_Outcome', N'verification_outcome=''matched'''),
                        (N'CK_BulkRecipientAccount_Line',    N'line_number>=2'),
                        (N'CK_BulkRecipientAccount_Hash',    N'lenlisting_sha256=64')
                    ) v (name, token)
                ),
                actual AS (
                    SELECT cc.name, cc.is_disabled, cc.is_not_trusted,
                           LOWER(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(cc.definition, N'[', N''), N']', N''), N' ', N''), N'(', N''), N')', N'')) AS def
                    FROM sys.check_constraints cc
                    WHERE cc.parent_object_id = @BR
                )
                INSERT INTO @Problems (msg)
                SELECT CONCAT(N'BulkRecipientAccount check constraint ', a.name,
                              CASE WHEN a.name NOT IN (SELECT name FROM tokens) THEN N' is not approved.'
                                   ELSE N' differs from the approved definition, is disabled or is not trusted.' END)
                FROM actual a
                WHERE a.name NOT IN (SELECT name FROM tokens)
                   OR a.is_disabled = 1 OR a.is_not_trusted = 1
                   OR EXISTS (SELECT 1 FROM tokens t WHERE t.name = a.name AND CHARINDEX(t.token, a.def) = 0);

                INSERT INTO @Problems (msg)
                SELECT CONCAT(N'BulkRecipientAccount check constraint ', n.name, N' is missing after the migration.')
                FROM (VALUES (N'CK_BulkRecipientAccount_Outcome'), (N'CK_BulkRecipientAccount_Line'), (N'CK_BulkRecipientAccount_Hash')) n (name)
                WHERE @Strict = 1
                  AND NOT EXISTS (SELECT 1 FROM sys.check_constraints cc WHERE cc.parent_object_id = @BR AND cc.name = n.name);

                /* F6. indexes: only the approved ones; IX_BulkRecipientAccount_BankAccount = nonclustered, non-unique, unfiltered, (BankAccount_Key) */
                INSERT INTO @Problems (msg)
                SELECT CONCAT(N'Unapproved index ', i.name, N' on BulkRecipientAccount.')
                FROM sys.indexes i
                WHERE i.object_id = @BR AND i.type > 0
                  AND i.name NOT IN (N'PK_BulkRecipientAccount', N'UQ_BulkRecipientAccount_Transaction_Line',
                                     N'UQ_BulkRecipientAccount_Transaction_BankAccount', N'IX_BulkRecipientAccount_BankAccount');

                INSERT INTO @Problems (msg)
                SELECT N'IX_BulkRecipientAccount_BankAccount differs from the approved definition (nonclustered, non-unique, unfiltered, key BankAccount_Key only).'
                FROM sys.indexes i
                WHERE i.object_id = @BR AND i.name = N'IX_BulkRecipientAccount_BankAccount'
                  AND (i.type_desc <> N'NONCLUSTERED' OR i.is_unique = 1 OR i.has_filter = 1
                       OR (SELECT COUNT(*) FROM sys.index_columns x WHERE x.object_id = i.object_id AND x.index_id = i.index_id) <> 1
                       OR NOT EXISTS (SELECT 1 FROM sys.index_columns x JOIN sys.columns c ON c.object_id = x.object_id AND c.column_id = x.column_id
                                       WHERE x.object_id = i.object_id AND x.index_id = i.index_id AND x.is_included_column = 0 AND c.name = N'BankAccount_Key'));

                INSERT INTO @Problems (msg)
                SELECT N'BulkRecipientAccount index IX_BulkRecipientAccount_BankAccount is missing after the migration.'
                WHERE @Strict = 1
                  AND NOT EXISTS (SELECT 1 FROM sys.indexes i WHERE i.object_id = @BR AND i.name = N'IX_BulkRecipientAccount_BankAccount');

                /* F7. no default constraints, no triggers */
                INSERT INTO @Problems (msg)
                SELECT CONCAT(N'Unexpected default constraint ', dc.name, N' on BulkRecipientAccount.')
                FROM sys.default_constraints dc WHERE dc.parent_object_id = @BR;

                INSERT INTO @Problems (msg)
                SELECT CONCAT(N'Unexpected trigger ', tr.name, N' on BulkRecipientAccount.')
                FROM sys.triggers tr WHERE tr.parent_id = @BR;
            END;

            /* -- G. permissions (phase 3 only): exactly SELECT, INSERT, DELETE on the new table for the application principal */
            IF @Strict = 1 AND @BR IS NOT NULL
            BEGIN
                INSERT INTO @Problems (msg)
                SELECT CONCAT(N'Application principal lacks ', p.perm, N' on BulkRecipientAccount.')
                FROM (VALUES (N'SELECT'), (N'INSERT'), (N'DELETE')) p (perm)
                WHERE NOT EXISTS (SELECT 1 FROM sys.database_permissions dp
                                   WHERE dp.class = 1 AND dp.major_id = @BR AND dp.minor_id = 0
                                     AND dp.grantee_principal_id = DATABASE_PRINCIPAL_ID(@AppPrincipal)
                                     AND dp.permission_name = p.perm AND dp.state = N'G');

                INSERT INTO @Problems (msg)
                SELECT CONCAT(N'Application principal holds an unneeded permission (', dp.permission_name, N') on BulkRecipientAccount.')
                FROM sys.database_permissions dp
                WHERE dp.class = 1 AND dp.major_id = @BR
                  AND dp.grantee_principal_id = DATABASE_PRINCIPAL_ID(@AppPrincipal)
                  AND dp.state IN (N'G', N'W')
                  AND (dp.permission_name NOT IN (N'SELECT', N'INSERT', N'DELETE') OR dp.state = N'W');
            END;

            /* -- verdict */
            IF EXISTS (SELECT 1 FROM @Problems)
            BEGIN
                SELECT id, msg AS problem FROM @Problems ORDER BY id;
                SET @Msg = LEFT(CONCAT(N'01 STOPPED in verification phase ', @Phase, N': ', (SELECT COUNT(*) FROM @Problems),
                                       N' problem(s); ', CASE WHEN @Phase = 3 THEN N'everything is being rolled back. ' ELSE N'nothing was changed. ' END,
                                       N'First: ', (SELECT TOP (1) msg FROM @Problems ORDER BY id), N' (full list in the result set / BI_EXECUTION_README.md)'), 2000);
                THROW 50100, @Msg, 1;
            END;

            IF @Phase = 3
            BEGIN
                COMMIT TRANSACTION;
            END;
        END;

        SET @Phase = @Phase + 1;
    END;

    /* ---------------------------------------------------------------- SUCCESS (only reachable after COMMIT) */
    PRINT N'01_bulk_recipient_expand.sql completed: all objects verified and committed.';
    PRINT N'The application feature stays OFF until BULK_RECIPIENT_ENABLED=true AND the SharePoint DocumentType exists.';
    SELECT N'PASS' AS result, N'01_bulk_recipient_expand.sql' AS script, DB_NAME() AS database_name, SYSDATETIME() AS completed_at,
           N'Next: 04_bulk_recipient_validation.sql sections 2, 4, 5, 6, 8' AS next_step;
END TRY
BEGIN CATCH
    DECLARE @ErrNumber int = ERROR_NUMBER(), @ErrSeverity int = ERROR_SEVERITY(), @ErrLine int = ERROR_LINE(), @ErrMessage nvarchar(2048) = ERROR_MESSAGE();
    IF XACT_STATE() <> 0
        ROLLBACK TRANSACTION;
    PRINT N'*** 01_bulk_recipient_expand.sql FAILED. Any open transaction was rolled back; no partial migration was committed. ***';
    PRINT CONCAT(N'Original error ', @ErrNumber, N' (severity ', @ErrSeverity, N', line ', @ErrLine, N'): ', @ErrMessage);
    SELECT N'FAILED' AS result, N'01_bulk_recipient_expand.sql' AS script, @ErrNumber AS error_number, @ErrLine AS error_line, @ErrMessage AS original_error;
    THROW;
END CATCH;
