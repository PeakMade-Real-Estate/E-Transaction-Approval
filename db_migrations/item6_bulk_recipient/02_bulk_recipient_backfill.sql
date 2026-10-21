/* ============================================================================
   Item 6 - Corporate Bulk-Recipient Payments
   02_bulk_recipient_backfill.sql   (OPTIONAL - GUARDED HISTORICAL CLASSIFICATION)

   Run AFTER 01 has PASSED and 04 sections 2 and 4 pass.  The application interprets a NULL Recipient_Mode itself, so this backfill
   is OPTIONAL (reporting convenience, and a prerequisite for the deferred script 03).

   Status : NOT EXECUTED by the author.  DRY RUN BY DEFAULT: @Commit = 0 rolls everything back.  Run as one batch (no GO inside).

   Rules (unambiguous historical rows only)
     Recipient_Mode IS NULL                           -- never overwrites an explicit External / Internal / Bulk
     AND Beneficiary_Key IS NOT NULL                  -- has a single beneficiary ...
     AND BeneficiaryInstruction_Key IS NOT NULL       -- ... and instruction (a bulk row has neither)
     AND no BulkRecipientAccount rows                 -- not a bulk transaction
     AND Bulk_Listing_SHA256 / _Count / _Validated_DateTime are all NULL
       RecipientBankAccount_Key IS NOT NULL  -> 'Internal'
       RecipientBankAccount_Key IS NULL      -> 'External'
   Every other NULL-mode row is reported as AMBIGUOUS and left NULL for human review.

   Only Recipient_Mode is written.  Modified_DateTime and every other original column are untouched, so the explicit-column
   fingerprint in 04 section 3B must still PASS afterwards.

   Safety: definition preflight, one transaction, TRY/CATCH with rollback and the ORIGINAL error re-raised, row-count
   assertions, and a final PASS / DRY RUN result.  Concurrency: touches only NULL-mode rows and only that column; prefer a quiet period.
   ============================================================================ */

SET NOCOUNT ON;
SET XACT_ABORT ON;

DECLARE @Commit bit = 0;     -- 0 = DRY RUN (rolled back) | 1 = keep the changes
DECLARE @ExpectedInternal int, @ExpectedExternal int, @Rows int, @Msg nvarchar(2048);
DECLARE @ET int = OBJECT_ID(N'etransactions.ETransaction', N'U');
DECLARE @BR int = OBJECT_ID(N'etransactions.BulkRecipientAccount', N'U');

BEGIN TRY

    /* ---- Preflight: 01 must have completed, and the schema must not be hardened */
    IF @ET IS NULL OR @BR IS NULL
        THROW 50010, N'Preflight: ETransaction or BulkRecipientAccount not found. Run 01_bulk_recipient_expand.sql first.', 1;

    IF EXISTS (SELECT 1 FROM (VALUES (N'Recipient_Mode', N'varchar', 20), (N'Bulk_Listing_SHA256', N'char', 64),
                                     (N'Bulk_Recipient_Count', N'int', 4), (N'Bulk_Listing_Validated_DateTime', N'datetime2', 8)) e (col, typ, len)
                LEFT JOIN sys.columns c ON c.object_id = @ET AND c.name = e.col
                LEFT JOIN sys.types ty ON ty.user_type_id = c.user_type_id
                WHERE c.column_id IS NULL OR ty.name <> e.typ OR c.max_length <> e.len OR c.is_nullable <> 1)
        THROW 50011, N'Preflight: a new ETransaction column is missing or differs from the approved definition (see 04 section 2). Nothing was changed.', 1;

    IF EXISTS (SELECT 1 FROM sys.check_constraints WHERE parent_object_id = @ET AND name = N'CK_ETransaction_RecipientMode_Links')
        THROW 50012, N'Preflight: the schema is already hardened (script 03); a backfill is not applicable. Nothing was changed.', 1;

    /* ---- A. What the backfill WILL do (read-only; dynamic SQL so the batch compiles even before 01 has run) */
    EXEC (N'
    SELECT classification, COUNT(*) AS txn_count, SUM(Amount) AS total_amount
    FROM (
        SELECT t.Amount,
               CASE
                 WHEN t.Recipient_Mode IS NOT NULL THEN CONCAT(N''already '', t.Recipient_Mode, N'' (untouched)'')
                 WHEN t.Beneficiary_Key IS NULL OR t.BeneficiaryInstruction_Key IS NULL
                   OR EXISTS (SELECT 1 FROM etransactions.BulkRecipientAccount b WHERE b.Transaction_Key = t.Transaction_Key)
                   OR t.Bulk_Listing_SHA256 IS NOT NULL OR t.Bulk_Recipient_Count IS NOT NULL
                   OR t.Bulk_Listing_Validated_DateTime IS NOT NULL                       THEN N''AMBIGUOUS (left NULL - review)''
                 WHEN t.RecipientBankAccount_Key IS NOT NULL                              THEN N''will set Internal''
                 ELSE                                                                          N''will set External''
               END AS classification
        FROM etransactions.ETransaction t
    ) x
    GROUP BY classification
    ORDER BY classification');

    /* ---- B. Apply inside a transaction that is rolled back unless @Commit = 1 */
    BEGIN TRANSACTION;

    SELECT @ExpectedInternal = COUNT(*)
    FROM etransactions.ETransaction t
    WHERE t.Recipient_Mode IS NULL AND t.RecipientBankAccount_Key IS NOT NULL
      AND t.Beneficiary_Key IS NOT NULL AND t.BeneficiaryInstruction_Key IS NOT NULL
      AND t.Bulk_Listing_SHA256 IS NULL AND t.Bulk_Recipient_Count IS NULL AND t.Bulk_Listing_Validated_DateTime IS NULL
      AND NOT EXISTS (SELECT 1 FROM etransactions.BulkRecipientAccount b WHERE b.Transaction_Key = t.Transaction_Key);

    SELECT @ExpectedExternal = COUNT(*)
    FROM etransactions.ETransaction t
    WHERE t.Recipient_Mode IS NULL AND t.RecipientBankAccount_Key IS NULL
      AND t.Beneficiary_Key IS NOT NULL AND t.BeneficiaryInstruction_Key IS NOT NULL
      AND t.Bulk_Listing_SHA256 IS NULL AND t.Bulk_Recipient_Count IS NULL AND t.Bulk_Listing_Validated_DateTime IS NULL
      AND NOT EXISTS (SELECT 1 FROM etransactions.BulkRecipientAccount b WHERE b.Transaction_Key = t.Transaction_Key);

    UPDATE t
       SET Recipient_Mode = N'Internal'
      FROM etransactions.ETransaction t
     WHERE t.Recipient_Mode IS NULL AND t.RecipientBankAccount_Key IS NOT NULL
       AND t.Beneficiary_Key IS NOT NULL AND t.BeneficiaryInstruction_Key IS NOT NULL
       AND t.Bulk_Listing_SHA256 IS NULL AND t.Bulk_Recipient_Count IS NULL AND t.Bulk_Listing_Validated_DateTime IS NULL
       AND NOT EXISTS (SELECT 1 FROM etransactions.BulkRecipientAccount b WHERE b.Transaction_Key = t.Transaction_Key);
    SET @Rows = @@ROWCOUNT;
    IF @Rows <> @ExpectedInternal
        THROW 50013, N'Internal backfill touched an unexpected number of rows; rolled back.', 1;
    PRINT CONCAT(N'Internal set on ', @Rows, N' row(s).');

    UPDATE t
       SET Recipient_Mode = N'External'
      FROM etransactions.ETransaction t
     WHERE t.Recipient_Mode IS NULL AND t.RecipientBankAccount_Key IS NULL
       AND t.Beneficiary_Key IS NOT NULL AND t.BeneficiaryInstruction_Key IS NOT NULL
       AND t.Bulk_Listing_SHA256 IS NULL AND t.Bulk_Recipient_Count IS NULL AND t.Bulk_Listing_Validated_DateTime IS NULL
       AND NOT EXISTS (SELECT 1 FROM etransactions.BulkRecipientAccount b WHERE b.Transaction_Key = t.Transaction_Key);
    SET @Rows = @@ROWCOUNT;
    IF @Rows <> @ExpectedExternal
        THROW 50014, N'External backfill touched an unexpected number of rows; rolled back.', 1;
    PRINT CONCAT(N'External set on ', @Rows, N' row(s).');

    /* Reconciliation: no backfilled mode may disagree with RecipientBankAccount_Key; explicit Bulk rows are unchanged by construction. */
    IF EXISTS (SELECT 1 FROM etransactions.ETransaction
                WHERE (Recipient_Mode = N'Internal' AND RecipientBankAccount_Key IS NULL)
                   OR (Recipient_Mode = N'External' AND RecipientBankAccount_Key IS NOT NULL))
        THROW 50015, N'Reconciliation failed: a backfilled mode disagrees with RecipientBankAccount_Key; rolled back.', 1;

    IF @Commit = 1
    BEGIN
        COMMIT TRANSACTION;
        SELECT N'PASS - COMMITTED' AS result, @ExpectedInternal AS internal_rows_set, @ExpectedExternal AS external_rows_set;
    END
    ELSE
    BEGIN
        ROLLBACK TRANSACTION;
        SELECT N'DRY RUN - ROLLED BACK (nothing changed). Set @Commit = 1 to apply.' AS result,
               @ExpectedInternal AS internal_rows_would_be_set, @ExpectedExternal AS external_rows_would_be_set;
    END;

    /* ---- C. Result (read-only). */
    SELECT ISNULL(Recipient_Mode, N'(NULL)') AS recipient_mode, COUNT(*) AS txn_count, SUM(Amount) AS total_amount
    FROM etransactions.ETransaction
    GROUP BY Recipient_Mode
    ORDER BY recipient_mode;

END TRY
BEGIN CATCH
    DECLARE @ErrNumber int = ERROR_NUMBER(), @ErrLine int = ERROR_LINE(), @ErrMessage nvarchar(2048) = ERROR_MESSAGE();
    IF XACT_STATE() <> 0
        ROLLBACK TRANSACTION;
    PRINT N'*** 02_bulk_recipient_backfill.sql FAILED. Rolled back; no change was committed. ***';
    PRINT CONCAT(N'Original error ', @ErrNumber, N' (line ', @ErrLine, N'): ', @ErrMessage);
    SELECT N'FAILED' AS result, @ErrNumber AS error_number, @ErrLine AS error_line, @ErrMessage AS original_error;
    THROW;
END CATCH;
