/* ============================================================================
   ##########################################################################
   #  DO NOT EXECUTE - DEFERRED APPLICATION DEPENDENCY                      #
   #  NOT APPROVED FOR EXECUTION.  NOT PART OF THE INITIAL SEQUENCE.        #
   #  Future migration, usable only after the application is changed.       #
   ##########################################################################

   Item 6 - Corporate Bulk-Recipient Payments
   03_bulk_recipient_harden.sql   (DEFERRED - DO NOT RUN YET)

   *** This script CANNOT be applied to the current application build. ***

   Why (finding from reading the application code - db.py):
     (a) insert_transaction() / create_draft() INSERT into ETransaction WITHOUT Recipient_Mode and then
         set it with a separate follow-up UPDATE.  A NOT NULL Recipient_Mode would make every INSERT fail.
     (b) update_draft() / finalize_draft_submission() change Beneficiary_Key / BeneficiaryInstruction_Key /
         RecipientBankAccount_Key in the main UPDATE and set Recipient_Mode in a LATER statement.  While a Draft
         is switched External<->Bulk or Internal<->Bulk, a row-level consistency CHECK would reject the first
         statement even though the final state is valid.
     (c) While BULK_RECIPIENT_ENABLED is false the application deliberately writes no Recipient_Mode at all.

   Prerequisites before this script may be run (all must be true and verified):
     P1. The application writes Recipient_Mode IN THE SAME INSERT / UPDATE statement that sets the links
         (code change required; not part of this migration), for every request, regardless of the
         Bulk switch, once the schema is present.
     P2. Every live application instance runs that build (no old-build instance can INSERT without it).
     P3. 02_bulk_recipient_backfill.sql was committed and the AMBIGUOUS rows it reports were resolved.
     P4. 04_bulk_recipient_validation.sql section 6 returns zero violating rows.

   Status : NOT EXECUTED by the author.  Guarded by @ConfirmPrerequisites (default 0 -> script stops).

   What it does: validates, then
     - Recipient_Mode  NOT NULL  (domain CHECK re-created without the NULL escape)
     - CK_ETransaction_RecipientMode_Links : mode <-> beneficiary / instruction / recipient-account keys
     - CK_ETransaction_BulkMetadata_Mode   : listing metadata only on Bulk rows
   All constraints are added WITH CHECK so they are validated against existing rows and trusted.
   Rolling back after Bulk transactions exist is NOT automatic: see 05_bulk_recipient_rollback.sql.
   ============================================================================ */

SET NOCOUNT ON;
SET XACT_ABORT ON;

DECLARE @ConfirmPrerequisites bit = 0;   -- BI sets to 1 ONLY after P1-P4 are confirmed in writing

IF @ConfirmPrerequisites <> 1
    THROW 50020, N'DO NOT EXECUTE - DEFERRED APPLICATION DEPENDENCY. 03_bulk_recipient_harden.sql is not approved for execution until the application writes Recipient_Mode in the same INSERT/UPDATE as the keys (prerequisites P1-P4 in the header). Stopped; nothing was changed.', 1;

IF COL_LENGTH(N'etransactions.ETransaction', N'Recipient_Mode') IS NULL
    THROW 50021, N'Recipient_Mode not found. Run 01 first.', 1;

/* ---- Preflight data checks: nothing may violate what is about to be enforced */
IF EXISTS (SELECT 1 FROM etransactions.ETransaction WHERE Recipient_Mode IS NULL)
    THROW 50022, N'Rows with NULL Recipient_Mode exist (run/finish 02 backfill and resolve AMBIGUOUS rows).', 1;

IF EXISTS (SELECT 1 FROM etransactions.ETransaction
            WHERE NOT (
                   (Recipient_Mode = N'External' AND Beneficiary_Key IS NOT NULL AND BeneficiaryInstruction_Key IS NOT NULL AND RecipientBankAccount_Key IS NULL)
                OR (Recipient_Mode = N'Internal' AND Beneficiary_Key IS NOT NULL AND BeneficiaryInstruction_Key IS NOT NULL AND RecipientBankAccount_Key IS NOT NULL)
                OR (Recipient_Mode = N'Bulk'     AND Beneficiary_Key IS NULL     AND BeneficiaryInstruction_Key IS NULL     AND RecipientBankAccount_Key IS NULL)))
    THROW 50023, N'Rows exist whose Recipient_Mode disagrees with their beneficiary/recipient links.', 1;

IF EXISTS (SELECT 1 FROM etransactions.ETransaction
            WHERE Recipient_Mode <> N'Bulk'
              AND (Bulk_Listing_SHA256 IS NOT NULL OR Bulk_Recipient_Count IS NOT NULL OR Bulk_Listing_Validated_DateTime IS NOT NULL))
    THROW 50024, N'Non-Bulk rows carry bulk listing metadata.', 1;

BEGIN TRANSACTION;

/* The existing NULL-permitting domain CHECK depends on the column and would block ALTER COLUMN; drop and re-create it. */
IF EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = N'CK_ETransaction_RecipientMode'
              AND parent_object_id = OBJECT_ID(N'etransactions.ETransaction'))
    ALTER TABLE etransactions.ETransaction DROP CONSTRAINT CK_ETransaction_RecipientMode;

IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'etransactions.ETransaction')
              AND name = N'Recipient_Mode' AND is_nullable = 1)
    ALTER TABLE etransactions.ETransaction ALTER COLUMN Recipient_Mode varchar(20) NOT NULL;

ALTER TABLE etransactions.ETransaction WITH CHECK
    ADD CONSTRAINT CK_ETransaction_RecipientMode
    CHECK (Recipient_Mode IN ('External', 'Internal', 'Bulk'));

IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = N'CK_ETransaction_RecipientMode_Links'
                  AND parent_object_id = OBJECT_ID(N'etransactions.ETransaction'))
    ALTER TABLE etransactions.ETransaction WITH CHECK
        ADD CONSTRAINT CK_ETransaction_RecipientMode_Links
        CHECK (
               (Recipient_Mode = 'External' AND Beneficiary_Key IS NOT NULL AND BeneficiaryInstruction_Key IS NOT NULL AND RecipientBankAccount_Key IS NULL)
            OR (Recipient_Mode = 'Internal' AND Beneficiary_Key IS NOT NULL AND BeneficiaryInstruction_Key IS NOT NULL AND RecipientBankAccount_Key IS NOT NULL)
            OR (Recipient_Mode = 'Bulk'     AND Beneficiary_Key IS NULL     AND BeneficiaryInstruction_Key IS NULL     AND RecipientBankAccount_Key IS NULL)
        );

IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = N'CK_ETransaction_BulkMetadata_Mode'
                  AND parent_object_id = OBJECT_ID(N'etransactions.ETransaction'))
    ALTER TABLE etransactions.ETransaction WITH CHECK
        ADD CONSTRAINT CK_ETransaction_BulkMetadata_Mode
        CHECK (Recipient_Mode = 'Bulk'
               OR (Bulk_Listing_SHA256 IS NULL AND Bulk_Recipient_Count IS NULL AND Bulk_Listing_Validated_DateTime IS NULL));

COMMIT TRANSACTION;

/* Confirm all three are trusted and enabled. */
SELECT name, is_disabled, is_not_trusted
FROM sys.check_constraints
WHERE parent_object_id = OBJECT_ID(N'etransactions.ETransaction')
  AND name IN (N'CK_ETransaction_RecipientMode', N'CK_ETransaction_RecipientMode_Links', N'CK_ETransaction_BulkMetadata_Mode');
