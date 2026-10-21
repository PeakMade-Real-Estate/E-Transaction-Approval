# Item 6 (Corporate Bulk-Recipient Payments) - BI Execution README

Target: Fabric SQL Database `DB_APP_SUPPORT`, schema `etransactions`. **Nothing here has been executed by the author.**
Run everything on a **non-production copy first**, as an administrative principal (ALTER / CREATE TABLE / GRANT / VIEW DEFINITION).

| Script | Status |
|---|---|
| `01_bulk_recipient_expand.sql` | **READY FOR BI NON-PRODUCTION TESTING** |
| `02_bulk_recipient_backfill.sql` | **READY FOR BI NON-PRODUCTION TESTING (optional)** |
| `04_bulk_recipient_validation.sql` | **READY FOR BI NON-PRODUCTION TESTING** |
| `05_bulk_recipient_rollback.sql` | **READY FOR BI NON-PRODUCTION TESTING (recovery reference only)** |
| `03_bulk_recipient_harden.sql` | **NOT APPROVED FOR EXECUTION - DO NOT EXECUTE - DEFERRED APPLICATION DEPENDENCY** |

## 1. Purpose
Allow one `ETransaction` to pay many internal `BankAccount` recipients (Corporate ACH / Same Day ACH / Wire) without dummy beneficiary rows:
four new nullable `ETransaction` columns (`Recipient_Mode varchar(20)`, `Bulk_Listing_SHA256 char(64)`, `Bulk_Recipient_Count int`,
`Bulk_Listing_Validated_DateTime datetime2(7)`), `Beneficiary_Key` / `BeneficiaryInstruction_Key` made nullable, and a new child table
`BulkRecipientAccount` (transaction -> BankAccount keys only; no account/routing numbers). Existing rows, foreign keys, workflows and the
External / Internal recipient flows are unchanged. Full design: `BULK_RECIPIENT_SCHEMA_CONTRACT.md`.

## 2. Execution sequence (initial release = steps 1-5)
Run each script as **one batch** (01, 02, 05 contain no `GO`). Run 04 one section (one `GO` batch) at a time.
1. Confirm `@AppPrincipal` in 01 (live catalog says `E-Transaction Approval`). Record the database name.
2. **04 section 3A** (baseline fingerprint) - **save the output** (nine values).
3. **01_bulk_recipient_expand.sql** - atomic; on any error it rolls back, prints the original error and stops. Success = result row `PASS`.
4. **04 sections 2, 3B, 4, 5, 6, 8** (paste the 3A values into 3B first). Each returns `PASS`; a failure ends with an error (502xx).
5. Smoke-test the application **with `BULK_RECIPIENT_ENABLED` unset/false**: dashboard, detail, CSV export, a new External and a new Internal request, Draft save/resume/finalize.
6. *(Optional)* **02_bulk_recipient_backfill.sql**, see section 3. Then 04 sections 3B, 6, 7.
- **Do not run 03.** 05 only per section 7.

## 3. Optional backfill (02)
Classifies only unambiguous historical rows (`RecipientBankAccount_Key` NOT NULL -> `Internal`, NULL -> `External`; never overwrites an explicit mode;
skips and reports rows with no beneficiary, child rows or bulk metadata as AMBIGUOUS). The application does not need it (it reads NULL itself).
1. Run as delivered: `@Commit = 0` = **dry run**, rolled back, shows what would change.
2. Review the counts; resolve any AMBIGUOUS rows. 3. Set `@Commit = 1`, run again. 4. Run 04 sections 3B, 6, 7 -> `PASS`.

## 4. Required validation results
| 04 section | Required result |
|---|---|
| 2 column definitions (explicit 33 original + 4 new + links + child) | `PASS` |
| 3B original-data fingerprint (33 original columns, counts, references) | `PASS`. `NOT EXECUTED - MISSING BASELINE` (error 50210) means the comparison was **not performed** - it is not a pass. `FAIL` (50211) = investigate. |
| 4 constraints / keys / indexes / trust | `PASS` (all trusted, enabled) |
| 5 application permissions | `PASS` (SELECT/INSERT/DELETE on the new table; no ALTER/CREATE/CONTROL/db_owner) |
| 6 data integrity | `PASS` for **6A**. **6B is informational**: a recipient account that is Closed *now* on an already-submitted transaction is normal history, not a failure. |
| 7 backfill (after 02) | `PASS` |
| 8 old/new application insert compatibility | `PASS` |
Any section not run is **NOT EXECUTED**, not a pass. Report each section's result to the project team.

## 5. What BI must verify in non-production
- 01 runs to `PASS`, and a **second run** of 01 also ends `PASS` (re-run safety). Pre-existing objects with a wrong definition must **stop** it (try one deliberate mismatch on a scratch copy).
- The two `ALTER COLUMN ... NULL` statements succeed and `FK_Transaction_Beneficiary` / `FK_Transaction_BeneficiaryInstruction` stay present, enabled and trusted (01 checks this before COMMIT).
- 04 sections 2-8 `PASS`; section 3B compares identical values before/after (use a quiet window or the same `@AsOf`).
- Application tests in section 10 of 04 (old-build inserts, new-build bulk, switch off/on, closing a listed account mid-flight, concurrent finalize).
- **Masked-column predicate**: as the *application principal*, `WHERE AccountNumber = @a AND RoutingNumber = @r` on `BankAccount` returns the row although the columns are masked. Not yet proven on this database; the feature depends on it.
- **Mirroring / OneLake / analytics endpoint**: review whether Fabric mirroring or the SQL analytics endpoint exposes `BulkRecipientAccount` (it links transactions to bank-account keys) and restrict it to authorized consumers.
- Run the application principal check: it must **not** have ALTER / CREATE / ownership rights (01 grants only SELECT, INSERT, DELETE on the new table).

## 6. Known Fabric compatibility uncertainties (not proven by execution)
Catalog queries proved: Azure SQL engine 12.0.2000.8, compatibility 170; `IDENTITY`, `UNIQUE` constraints, nullable FK columns, `datetime2(7)`, `STRING_AGG`,
the `sys.*` views. `SERVER_PROPERTY()` is **not** supported (not used). The scripts were additionally **parse-checked by the engine** (no execution) - see the final report.
**Not proven by running:** DDL inside an explicit transaction and `TRY/CATCH` around DDL; `ALTER COLUMN NOT NULL -> NULL` on a foreign-key column;
`WITH CHECK` constraints; `CREATE NONCLUSTERED INDEX`; `GRANT` on the new table; `HASHBYTES`/`CONCAT_WS` in section 3 on large data;
`sys.sql_expression_dependencies` (needs VIEW DEFINITION; the application principal was denied). If any statement is unsupported, stop and report the exact error.
CHECK-constraint definitions are compared by tokens (SQL Server normalises them); 04 section 4 prints the stored definitions for review.

## 7. Rollback restrictions
`05_bulk_recipient_rollback.sql` is a **guarded recovery reference** (`@IUnderstand = 0` by default). It **refuses** if any Bulk transaction, Bulk draft, listing metadata
or `BulkRecipientAccount` row exists, if any object is unrecognized, or if NOT NULL cannot be restored - it never deletes or orphans Bulk data. It handles partial 01 runs.
Normal recovery is **not** this script: set `BULK_RECIPIENT_ENABLED=false` and leave the additive schema in place. Existing Bulk transactions keep displaying while the schema exists.

## 8. Application deployment dependencies
- Deploy the application build that contains Item 6 **before** enabling anything; with `BULK_RECIPIENT_ENABLED` unset it behaves exactly as today.
- The application reads the new columns only after its own schema check passes (`db.bulk_schema_ready`): all four columns, the child table and nullable beneficiary links. A positive check is cached per process: **restart the application after 01** (and after any rollback).
- **03 (hardening) needs an application change that is NOT part of this release**: `Recipient_Mode` must be written in the same INSERT/UPDATE as the keys, on every instance.
  The current build inserts without it and sets it afterwards, so `NOT NULL` or a mode-vs-link CHECK would reject valid operations.

## 9. SharePoint DocumentType dependency
A SharePoint administrator must add the choice **`Bulk Recipient Listing`** to the document library's strict **DocumentType** column (section "D - Receiving Banking").
Until it exists, listing uploads cannot be tagged. The application does not modify SharePoint.

## 10. The feature stays disabled until the database and SharePoint are ready
`BULK_RECIPIENT_ENABLED` is false by default and must stay unset until: (1) 01 passed and 04 sections 2-8 are `PASS` (non-prod, then prod), (2) the SharePoint choice exists,
(3) the masked-column predicate and the application tests in 04 section 10 passed in non-production. Then enable in non-production first.
