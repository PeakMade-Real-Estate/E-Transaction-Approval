/* ============================================================================
   Item 6 - Corporate Bulk-Recipient Payments
   05_bulk_recipient_rollback.sql   (RECOVERY REFERENCE - GUARDED, REFUSES TO DESTROY BULK DATA)

   Status : NOT EXECUTED by the author.  Stops immediately unless @IUnderstand = 1.  Run as ONE batch (no GO inside).

   READ THIS FIRST - reverting is NOT automatically safe once Bulk transactions exist.
     * A Bulk transaction has NULL Beneficiary_Key / BeneficiaryInstruction_Key and its recipients exist only in
       BulkRecipientAccount.  Dropping the table or columns, or re-imposing NOT NULL, would destroy or orphan the only record of who
       was paid.  This script therefore REFUSES to run if ANY Bulk data exists.  There is no "force" option.
     * It never deletes, updates or moves any transaction, recipient row or historical data.  It only drops EMPTY, RECOGNIZED objects
       that script 01 (and the future script 03) created.

   Preferred recovery (none of these needs this script):
     1. Application switch: set BULK_RECIPIENT_ENABLED=false.  New Bulk requests stop immediately; existing Bulk transactions keep displaying.
     2. Leave the additive schema in place (it is backward compatible with the old application build).
     3. Use this script only if the feature was NEVER used (zero Bulk data), e.g. after a failed non-production trial.
   If Bulk data exists and the schema must still be reversed: archive it under Treasury/Compliance sign-off and design a dedicated migration.

   Safe in every partial state: if 01 only partly ran (some new columns exist, others not), the child table exists or not, or constraints
   differ from expectations, the script inspects what is actually there, drops only recognized objects, and stops with a clear list of
   reasons (changing nothing) when anything is unrecognized, non-empty or unexpected.

   Phases:  1 preflight (read-only)  ->  2 BEGIN TRANSACTION and re-check the same preconditions inside it  ->  3 drops  ->  4 verify, COMMIT.
   TRY/CATCH rolls back and re-raises the ORIGINAL error.  Success is reported only after COMMIT.
   ============================================================================ */

SET NOCOUNT ON;
SET XACT_ABORT ON;

DECLARE @IUnderstand bit = 0;            -- BI/DBA sets to 1 deliberately
DECLARE @AppPrincipal sysname = N'E-Transaction Approval';

DECLARE @Problems TABLE (id int IDENTITY(1,1) PRIMARY KEY, msg nvarchar(600) NOT NULL);
DECLARE @Phase int = 1, @Post bit = 0, @Msg nvarchar(2048), @cnt int, @Nothing bit = 0, @DdlSql nvarchar(400);
DECLARE @ET int, @BR int;

BEGIN TRY

    IF @IUnderstand <> 1
        THROW 50030, N'05_bulk_recipient_rollback.sql is a guarded recovery reference. Set @IUnderstand = 1 only after reading the header. Nothing was changed.', 1;

    SET @ET = OBJECT_ID(N'etransactions.ETransaction', N'U');
    IF @ET IS NULL
        THROW 50031, N'etransactions.ETransaction not found. Wrong database? Nothing was changed.', 1;

    /* Nothing to roll back at all? (no new column, no child table, links already NOT NULL, no approved constraint) */
    IF OBJECT_ID(N'etransactions.BulkRecipientAccount', N'U') IS NULL
       AND COL_LENGTH(N'etransactions.ETransaction', N'Recipient_Mode') IS NULL
       AND COL_LENGTH(N'etransactions.ETransaction', N'Bulk_Listing_SHA256') IS NULL
       AND COL_LENGTH(N'etransactions.ETransaction', N'Bulk_Recipient_Count') IS NULL
       AND COL_LENGTH(N'etransactions.ETransaction', N'Bulk_Listing_Validated_DateTime') IS NULL
       AND NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = @ET AND name IN (N'Beneficiary_Key', N'BeneficiaryInstruction_Key') AND is_nullable = 1)
        SET @Nothing = 1;

    IF @Nothing = 1
    BEGIN
        PRINT N'Nothing to roll back: no Item 6 object exists and the beneficiary links are already NOT NULL.';
        SELECT N'PASS - NOTHING TO ROLL BACK' AS result, N'05_bulk_recipient_rollback.sql' AS script;
    END
    ELSE
    BEGIN
        WHILE @Phase <= 4
        BEGIN
            IF @Phase = 3
            BEGIN
                /* ====================================================== PHASE 3 : DROPS (only empty, recognized objects) */
                /* 3.1 ETransaction constraints that belong to this feature (recognized by name; verified above) */
                IF EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = N'CK_ETransaction_BulkMetadata_Mode' AND parent_object_id = @ET)
                    ALTER TABLE etransactions.ETransaction DROP CONSTRAINT CK_ETransaction_BulkMetadata_Mode;
                IF EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = N'CK_ETransaction_RecipientMode_Links' AND parent_object_id = @ET)
                    ALTER TABLE etransactions.ETransaction DROP CONSTRAINT CK_ETransaction_RecipientMode_Links;
                IF EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = N'CK_ETransaction_BulkMetadata' AND parent_object_id = @ET)
                    ALTER TABLE etransactions.ETransaction DROP CONSTRAINT CK_ETransaction_BulkMetadata;
                IF EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = N'CK_ETransaction_BulkCount' AND parent_object_id = @ET)
                    ALTER TABLE etransactions.ETransaction DROP CONSTRAINT CK_ETransaction_BulkCount;
                IF EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = N'CK_ETransaction_RecipientMode' AND parent_object_id = @ET)
                    ALTER TABLE etransactions.ETransaction DROP CONSTRAINT CK_ETransaction_RecipientMode;

                /* 3.2 the (verified empty) child table and the application's grants on it */
                IF OBJECT_ID(N'etransactions.BulkRecipientAccount', N'U') IS NOT NULL
                BEGIN
                    IF DATABASE_PRINCIPAL_ID(@AppPrincipal) IS NOT NULL
                    BEGIN
                        SET @DdlSql = N'REVOKE SELECT, INSERT, DELETE ON OBJECT::etransactions.BulkRecipientAccount FROM ' + QUOTENAME(@AppPrincipal) + N';';
                        EXEC (@DdlSql);
                    END;
                    DROP TABLE etransactions.BulkRecipientAccount;
                END;

                /* 3.3 new columns that exist (verified recognized and holding no Bulk data) */
                IF COL_LENGTH(N'etransactions.ETransaction', N'Bulk_Listing_Validated_DateTime') IS NOT NULL
                    ALTER TABLE etransactions.ETransaction DROP COLUMN Bulk_Listing_Validated_DateTime;
                IF COL_LENGTH(N'etransactions.ETransaction', N'Bulk_Recipient_Count') IS NOT NULL
                    ALTER TABLE etransactions.ETransaction DROP COLUMN Bulk_Recipient_Count;
                IF COL_LENGTH(N'etransactions.ETransaction', N'Bulk_Listing_SHA256') IS NOT NULL
                    ALTER TABLE etransactions.ETransaction DROP COLUMN Bulk_Listing_SHA256;
                IF COL_LENGTH(N'etransactions.ETransaction', N'Recipient_Mode') IS NOT NULL
                    ALTER TABLE etransactions.ETransaction DROP COLUMN Recipient_Mode;

                /* 3.4 restore the original NOT NULL links.  The existing foreign keys are never touched. */
                IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = @ET AND name = N'Beneficiary_Key' AND is_nullable = 1)
                    ALTER TABLE etransactions.ETransaction ALTER COLUMN Beneficiary_Key int NOT NULL;
                IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = @ET AND name = N'BeneficiaryInstruction_Key' AND is_nullable = 1)
                    ALTER TABLE etransactions.ETransaction ALTER COLUMN BeneficiaryInstruction_Key int NOT NULL;
            END
            ELSE
            BEGIN
                /* ====================================================== PHASES 1, 2, 4 : CHECKS (read-only) */
                IF @Phase = 2
                    BEGIN TRANSACTION;
                SET @Post = CASE WHEN @Phase = 4 THEN 1 ELSE 0 END;
                SET @BR = OBJECT_ID(N'etransactions.BulkRecipientAccount', N'U');
                DELETE FROM @Problems;

                IF @Post = 0
                BEGIN
                    /* ---- P1. child table: recognized, empty, no dependents */
                    IF @BR IS NOT NULL
                    BEGIN
                        INSERT INTO @Problems (msg)
                        SELECT CONCAT(N'BulkRecipientAccount has an UNRECOGNIZED column ', c.name, N'; it will not be dropped.')
                        FROM sys.columns c
                        WHERE c.object_id = @BR
                          AND c.name NOT IN (N'BulkRecipient_Key', N'Transaction_Key', N'Line_Number', N'BankAccount_Key',
                                             N'Verification_Outcome', N'Verified_DateTime', N'Listing_SHA256');

                        INSERT INTO @Problems (msg)
                        SELECT CONCAT(N'BulkRecipientAccount has an UNRECOGNIZED constraint/index ', x.name, N'; it will not be dropped.')
                        FROM (SELECT k.name FROM sys.key_constraints k WHERE k.parent_object_id = @BR
                              UNION SELECT f.name FROM sys.foreign_keys f WHERE f.parent_object_id = @BR
                              UNION SELECT cc.name FROM sys.check_constraints cc WHERE cc.parent_object_id = @BR
                              UNION SELECT i.name FROM sys.indexes i WHERE i.object_id = @BR AND i.type > 0
                              UNION SELECT dc.name FROM sys.default_constraints dc WHERE dc.parent_object_id = @BR) x
                        WHERE x.name NOT IN (N'PK_BulkRecipientAccount', N'FK_BulkRecipientAccount_Transaction', N'FK_BulkRecipientAccount_BankAccount',
                                             N'UQ_BulkRecipientAccount_Transaction_Line', N'UQ_BulkRecipientAccount_Transaction_BankAccount',
                                             N'IX_BulkRecipientAccount_BankAccount', N'CK_BulkRecipientAccount_Outcome',
                                             N'CK_BulkRecipientAccount_Line', N'CK_BulkRecipientAccount_Hash');

                        SET @cnt = NULL;
                        EXEC sp_executesql N'SELECT @c = COUNT(*) FROM etransactions.BulkRecipientAccount', N'@c int OUTPUT', @c = @cnt OUTPUT;
                        IF @cnt > 0
                            INSERT INTO @Problems (msg) VALUES (CONCAT(N'BulkRecipientAccount holds ', @cnt, N' legitimate Bulk recipient row(s). They will NOT be deleted: rollback refused.'));

                        INSERT INTO @Problems (msg)
                        SELECT CONCAT(N'Foreign key ', f.name, N' from ', OBJECT_NAME(f.parent_object_id), N' references BulkRecipientAccount.')
                        FROM sys.foreign_keys f WHERE f.referenced_object_id = @BR;

                        INSERT INTO @Problems (msg)
                        SELECT CONCAT(N'Schema-bound object ', OBJECT_NAME(d.referencing_id), N' depends on BulkRecipientAccount.')
                        FROM sys.sql_expression_dependencies d WHERE d.referenced_id = @BR AND d.is_schema_bound_reference = 1;
                    END;

                    /* ---- P2. the new ETransaction columns that exist: recognized definition (else they are not ours to drop) */
                    ;WITH expected (col, typ, max_len) AS (
                        SELECT * FROM (VALUES
                            (N'Recipient_Mode', N'varchar', 20), (N'Bulk_Listing_SHA256', N'char', 64),
                            (N'Bulk_Recipient_Count', N'int', 4), (N'Bulk_Listing_Validated_DateTime', N'datetime2', 8)
                        ) v (col, typ, max_len)
                    )
                    INSERT INTO @Problems (msg)
                    SELECT CONCAT(N'ETransaction.', e.col, N' exists with an UNRECOGNIZED definition (found ', ty.name, N' len ', c.max_length, N'); it will not be dropped.')
                    FROM expected e
                    JOIN sys.columns c ON c.object_id = @ET AND c.name = e.col
                    JOIN sys.types ty ON ty.user_type_id = c.user_type_id
                    WHERE ty.name <> e.typ OR c.max_length <> e.max_len OR c.is_identity = 1 OR c.is_computed = 1;

                    /* ---- P3. data: any Bulk data (or unrecognized data) refuses the rollback */
                    IF COL_LENGTH(N'etransactions.ETransaction', N'Recipient_Mode') IS NOT NULL
                    BEGIN
                        SET @cnt = NULL;
                        EXEC sp_executesql N'SELECT @c = COUNT(*) FROM etransactions.ETransaction WHERE Recipient_Mode = N''Bulk''', N'@c int OUTPUT', @c = @cnt OUTPUT;
                        IF @cnt > 0
                            INSERT INTO @Problems (msg) VALUES (CONCAT(@cnt, N' transaction(s) have Recipient_Mode = Bulk (including Bulk drafts). They will NOT be touched: rollback refused.'));
                        SET @cnt = NULL;
                        EXEC sp_executesql N'SELECT @c = COUNT(*) FROM etransactions.ETransaction WHERE Recipient_Mode IS NOT NULL AND Recipient_Mode NOT IN (N''External'', N''Internal'', N''Bulk'')',
                                           N'@c int OUTPUT', @c = @cnt OUTPUT;
                        IF @cnt > 0
                            INSERT INTO @Problems (msg) VALUES (CONCAT(@cnt, N' transaction(s) have an UNRECOGNIZED Recipient_Mode value: rollback refused.'));
                    END;
                    IF COL_LENGTH(N'etransactions.ETransaction', N'Bulk_Listing_SHA256') IS NOT NULL
                    BEGIN
                        SET @cnt = NULL;
                        EXEC sp_executesql N'SELECT @c = COUNT(*) FROM etransactions.ETransaction WHERE Bulk_Listing_SHA256 IS NOT NULL', N'@c int OUTPUT', @c = @cnt OUTPUT;
                        IF @cnt > 0 INSERT INTO @Problems (msg) VALUES (CONCAT(@cnt, N' transaction(s) carry a Bulk listing hash: rollback refused.'));
                    END;
                    IF COL_LENGTH(N'etransactions.ETransaction', N'Bulk_Recipient_Count') IS NOT NULL
                    BEGIN
                        SET @cnt = NULL;
                        EXEC sp_executesql N'SELECT @c = COUNT(*) FROM etransactions.ETransaction WHERE Bulk_Recipient_Count IS NOT NULL', N'@c int OUTPUT', @c = @cnt OUTPUT;
                        IF @cnt > 0 INSERT INTO @Problems (msg) VALUES (CONCAT(@cnt, N' transaction(s) carry a Bulk recipient count: rollback refused.'));
                    END;
                    IF COL_LENGTH(N'etransactions.ETransaction', N'Bulk_Listing_Validated_DateTime') IS NOT NULL
                    BEGIN
                        SET @cnt = NULL;
                        EXEC sp_executesql N'SELECT @c = COUNT(*) FROM etransactions.ETransaction WHERE Bulk_Listing_Validated_DateTime IS NOT NULL', N'@c int OUTPUT', @c = @cnt OUTPUT;
                        IF @cnt > 0 INSERT INTO @Problems (msg) VALUES (CONCAT(@cnt, N' transaction(s) carry a Bulk validation timestamp: rollback refused.'));
                    END;

                    /* a row with no beneficiary / instruction is a Bulk-style row whatever its mode says; NOT NULL could not be restored anyway */
                    INSERT INTO @Problems (msg)
                    SELECT CONCAT(COUNT(*), N' transaction(s) have a NULL Beneficiary_Key or BeneficiaryInstruction_Key: NOT NULL cannot be restored and the rows must not be orphaned. Rollback refused.')
                    FROM etransactions.ETransaction t
                    WHERE t.Beneficiary_Key IS NULL OR t.BeneficiaryInstruction_Key IS NULL
                    HAVING COUNT(*) > 0;

                    /* ---- P4. constraints on ETransaction that refer to the new columns: only the recognized ones, each as expected */
                    ;WITH tokens (name, token) AS (
                        SELECT * FROM (VALUES
                            (N'CK_ETransaction_RecipientMode',       N'recipient_mode'),
                            (N'CK_ETransaction_RecipientMode',       N'''bulk'''),
                            (N'CK_ETransaction_BulkCount',           N'bulk_recipient_count>0'),
                            (N'CK_ETransaction_BulkMetadata',        N'bulk_listing_sha256'),
                            (N'CK_ETransaction_BulkMetadata',        N'bulk_recipient_count'),
                            (N'CK_ETransaction_BulkMetadata',        N'bulk_listing_validated_datetime'),
                            (N'CK_ETransaction_RecipientMode_Links', N'recipient_mode'),
                            (N'CK_ETransaction_RecipientMode_Links', N'beneficiary_key'),
                            (N'CK_ETransaction_RecipientMode_Links', N'beneficiaryinstruction_key'),
                            (N'CK_ETransaction_RecipientMode_Links', N'recipientbankaccount_key'),
                            (N'CK_ETransaction_BulkMetadata_Mode',   N'bulk_listing_sha256'),
                            (N'CK_ETransaction_BulkMetadata_Mode',   N'recipient_mode=''bulk''')
                        ) v (name, token)
                    ),
                    actual AS (
                        SELECT cc.name,
                               LOWER(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(cc.definition, N'[', N''), N']', N''), N' ', N''), N'(', N''), N')', N'')) AS def
                        FROM sys.check_constraints cc WHERE cc.parent_object_id = @ET
                    )
                    INSERT INTO @Problems (msg)
                    SELECT CONCAT(N'ETransaction check constraint ', a.name,
                                  CASE WHEN a.name NOT IN (SELECT name FROM tokens) THEN N' is not part of this feature but refers to a Bulk column; it blocks the drop. Review manually.'
                                       ELSE N' differs from the expected definition; it will not be dropped automatically.' END)
                    FROM actual a
                    WHERE (a.name NOT IN (SELECT name FROM tokens)
                           AND (a.def LIKE N'%recipient_mode%' OR a.def LIKE N'%bulk_listing_%' OR a.def LIKE N'%bulk_recipient_count%'))
                       OR EXISTS (SELECT 1 FROM tokens t WHERE t.name = a.name AND CHARINDEX(t.token, a.def) = 0);

                    /* ---- P5. other dependents of the new columns (never changed automatically) */
                    INSERT INTO @Problems (msg)
                    SELECT CONCAT(N'Index ', i.name, N' includes ETransaction.', c.name, N'; it blocks DROP COLUMN. Review manually.')
                    FROM sys.index_columns ic
                    JOIN sys.indexes i ON i.object_id = ic.object_id AND i.index_id = ic.index_id
                    JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
                    WHERE ic.object_id = @ET AND c.name IN (N'Recipient_Mode', N'Bulk_Listing_SHA256', N'Bulk_Recipient_Count', N'Bulk_Listing_Validated_DateTime');

                    INSERT INTO @Problems (msg)
                    SELECT CONCAT(N'Default constraint ', dc.name, N' exists on ETransaction.', c.name, N'.')
                    FROM sys.default_constraints dc JOIN sys.columns c ON c.object_id = dc.parent_object_id AND c.column_id = dc.parent_column_id
                    WHERE dc.parent_object_id = @ET AND c.name IN (N'Recipient_Mode', N'Bulk_Listing_SHA256', N'Bulk_Recipient_Count', N'Bulk_Listing_Validated_DateTime');

                    INSERT INTO @Problems (msg)
                    SELECT CONCAT(N'User-created statistics ', s.name, N' include ETransaction.', c.name, N'.')
                    FROM sys.stats s
                    JOIN sys.stats_columns sc ON sc.object_id = s.object_id AND sc.stats_id = s.stats_id
                    JOIN sys.columns c ON c.object_id = sc.object_id AND c.column_id = sc.column_id
                    WHERE s.object_id = @ET AND s.user_created = 1
                      AND c.name IN (N'Recipient_Mode', N'Bulk_Listing_SHA256', N'Bulk_Recipient_Count', N'Bulk_Listing_Validated_DateTime',
                                     N'Beneficiary_Key', N'BeneficiaryInstruction_Key');

                    INSERT INTO @Problems (msg)
                    SELECT CONCAT(N'Computed column ', cc.name, N' references a Bulk column or a beneficiary link.')
                    FROM sys.computed_columns cc
                    WHERE cc.object_id = @ET
                      AND (cc.definition LIKE N'%Recipient_Mode%' OR cc.definition LIKE N'%Bulk_%' OR cc.definition LIKE N'%Beneficiary%');

                    INSERT INTO @Problems (msg)
                    SELECT CONCAT(N'Schema-bound object ', OBJECT_NAME(d.referencing_id), N' depends on ETransaction.')
                    FROM sys.sql_expression_dependencies d WHERE d.referenced_id = @ET AND d.is_schema_bound_reference = 1;

                    /* ---- P6. the existing beneficiary foreign keys must still be present and trusted (they are never modified) */
                    INSERT INTO @Problems (msg)
                    SELECT CONCAT(N'Existing foreign key ', n.name, N' is missing, disabled or not trusted; rollback refused until it is reviewed.')
                    FROM (VALUES (N'FK_Transaction_Beneficiary'), (N'FK_Transaction_BeneficiaryInstruction')) n (name)
                    WHERE NOT EXISTS (SELECT 1 FROM sys.foreign_keys f WHERE f.name = n.name AND f.parent_object_id = @ET AND f.is_disabled = 0 AND f.is_not_trusted = 0);
                END
                ELSE
                BEGIN
                    /* ---- POST-CONDITIONS (phase 4, inside the transaction, before COMMIT) */
                    IF @BR IS NOT NULL
                        INSERT INTO @Problems (msg) VALUES (N'BulkRecipientAccount still exists.');
                    INSERT INTO @Problems (msg)
                    SELECT CONCAT(N'ETransaction.', n.col, N' still exists.')
                    FROM (VALUES (N'Recipient_Mode'), (N'Bulk_Listing_SHA256'), (N'Bulk_Recipient_Count'), (N'Bulk_Listing_Validated_DateTime')) n (col)
                    WHERE COL_LENGTH(N'etransactions.ETransaction', n.col) IS NOT NULL;
                    INSERT INTO @Problems (msg)
                    SELECT CONCAT(N'ETransaction.', c.name, N' is not NOT NULL again.')
                    FROM sys.columns c WHERE c.object_id = @ET AND c.name IN (N'Beneficiary_Key', N'BeneficiaryInstruction_Key') AND c.is_nullable = 1;
                    INSERT INTO @Problems (msg)
                    SELECT CONCAT(N'Check constraint ', cc.name, N' still exists.')
                    FROM sys.check_constraints cc
                    WHERE cc.parent_object_id = @ET
                      AND cc.name IN (N'CK_ETransaction_RecipientMode', N'CK_ETransaction_BulkCount', N'CK_ETransaction_BulkMetadata',
                                      N'CK_ETransaction_RecipientMode_Links', N'CK_ETransaction_BulkMetadata_Mode');
                    INSERT INTO @Problems (msg)
                    SELECT CONCAT(N'Existing foreign key ', n.name, N' is missing, disabled or not trusted after the rollback.')
                    FROM (VALUES (N'FK_Transaction_Beneficiary'), (N'FK_Transaction_BeneficiaryInstruction')) n (name)
                    WHERE NOT EXISTS (SELECT 1 FROM sys.foreign_keys f WHERE f.name = n.name AND f.parent_object_id = @ET AND f.is_disabled = 0 AND f.is_not_trusted = 0);
                END;

                IF EXISTS (SELECT 1 FROM @Problems)
                BEGIN
                    SELECT id, msg AS reason FROM @Problems ORDER BY id;
                    SET @Msg = LEFT(CONCAT(N'05 ROLLBACK REFUSED / STOPPED in phase ', @Phase, N': ', (SELECT COUNT(*) FROM @Problems), N' reason(s). ',
                                           CASE WHEN @Phase = 4 THEN N'Everything is being rolled back. ' ELSE N'Nothing was changed. ' END,
                                           N'First: ', (SELECT TOP (1) msg FROM @Problems ORDER BY id)), 2000);
                    THROW 50035, @Msg, 1;
                END;

                IF @Phase = 4
                    COMMIT TRANSACTION;
            END;

            SET @Phase = @Phase + 1;
        END;

        PRINT N'Schema reverted. Restart the application: a positive schema check is cached for the life of the process.';
        SELECT N'PASS - ROLLED BACK AND VERIFIED' AS result, N'05_bulk_recipient_rollback.sql' AS script, SYSDATETIME() AS completed_at;
    END;
END TRY
BEGIN CATCH
    DECLARE @ErrNumber int = ERROR_NUMBER(), @ErrLine int = ERROR_LINE(), @ErrMessage nvarchar(2048) = ERROR_MESSAGE();
    IF XACT_STATE() <> 0
        ROLLBACK TRANSACTION;
    PRINT N'*** 05_bulk_recipient_rollback.sql STOPPED. Any open transaction was rolled back; no partial rollback was committed. ***';
    PRINT CONCAT(N'Original error ', @ErrNumber, N' (line ', @ErrLine, N'): ', @ErrMessage);
    SELECT N'STOPPED' AS result, @ErrNumber AS error_number, @ErrLine AS error_line, @ErrMessage AS original_error;
    THROW;
END CATCH;
