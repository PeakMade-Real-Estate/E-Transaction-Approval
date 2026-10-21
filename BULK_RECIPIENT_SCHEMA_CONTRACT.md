# Bulk Recipient Payments — SQL Schema Contract (Item 6)

**Status:** application code is complete, gated and regression-tested against mocks. **No SQL in this
document has been executed**, and the feature has not been tested against live SQL or SharePoint.
BI owns and applies the schema. This document is the exact contract the code in this repository depends
on: every name, type, nullability and statement below is what `db.py` / `app.py` read or write today. If BI
deviates, the application must change with it.

Companion document: [BULK_RECIPIENT_LISTING_FORMAT.md](BULK_RECIPIENT_LISTING_FORMAT.md) (the spreadsheet
requesters upload, and which controls are Treasury-approved vs. application safeguards).

## Feature summary

* A **Corporate** requester submits **one** ETransaction of type **ACH, Same Day ACH or Wire** whose recipients are
  many maintained internal `BankAccount` rows, identified by an attached `.xlsx` listing.
* Every listing row must resolve to **exactly one Open** `BankAccount` (full `AccountNumber` **and**
  `RoutingNumber`, compared inside SQL; no last-four matching; the numbers never leave SQL). One miss blocks everything.
* **No** `Beneficiary` / `BeneficiaryBankInstruction` rows are created for a bulk request.
* One ETransaction, one workflow, the same approvers / thresholds / rules as any other request.
* The listing is stored in SharePoint (section **D - Receiving Banking**, new DocumentType **Bulk Recipient Listing**).
  SharePoint is not modified by this change (section O).

## Two switches (read this first)

| Switch | Defined by | Controls |
|---|---|---|
| **Read gate** `db.bulk_schema_ready()` | The migrated schema exists (section K). No environment variable. | Whether the application *reads* `Recipient_Mode` / `BulkRecipientAccount`: dashboard, CSV export, request detail, Draft view. Existing Bulk transactions therefore stay accurately visible. |
| **Create gate** `db.bulk_recipient_enabled()` | `BULK_RECIPIENT_ENABLED=true` **and** the read gate. Default OFF. | Whether new Bulk requests may be **created, saved as a Draft, finalized, or opened for editing**, and whether the Bulk choice is rendered. Also whether `Recipient_Mode` / child rows are *written* for any request. |

With the create gate OFF:

* New Bulk submissions, Draft saves and Draft finalization are rejected ("Bulk recipient payments are not available.").
  An existing Bulk **Draft** cannot be opened for editing (redirected to the dashboard with a message).
* Existing **submitted** Bulk transactions keep displaying as Bulk (read gate), and approvals, reassignment, Treasury
  evidence/actions and authorization behave exactly as for any other transaction (none of those queries touch
  beneficiary rows or the new columns).
* Single-recipient requests behave as before and **do not write** `Recipient_Mode` (it stays NULL; NULL is
  interpreted from the existing relationships — section I).

With the read gate false (schema absent or unreadable — fail closed):

* **No statement references `Recipient_Mode`, `Bulk_*` or `BulkRecipientAccount`** (dashboard, export, detail, Draft view,
  writes). An unmigrated database is never asked about them.
* The request-detail page cannot read the mode. A row whose `BeneficiaryInstruction_Key IS NULL` (it can exist only in a
  partly-migrated database) is shown as **"Recipient details unavailable"** with no beneficiary fields — never as an
  External Payee. Other rows display exactly as before.

---

## A. Nullable changes to existing columns

| Table | Column | Today | Required | Why |
|---|---|---|---|---|
| `etransactions.ETransaction` | `Beneficiary_Key` | `NOT NULL` | `NULL` allowed (same data type, same FK) | A bulk request has no single beneficiary. |
| `etransactions.ETransaction` | `BeneficiaryInstruction_Key` | `NOT NULL` | `NULL` allowed (same data type, same FK) | Same. |

`Beneficiary.Payee_Name`, `BeneficiaryBankInstruction.Receiving_Bank_Name` and `Receiving_Account_Number` **stay NOT NULL**
(no placeholder rows are ever created). `ETransaction.RecipientBankAccount_Key` is unchanged (NULL for bulk).
`TransactionVerification` is unchanged. `bulk_schema_ready()` requires both columns to report `IS_NULLABLE = 'YES'`.

## B. New ETransaction columns

All nullable, no default, on `etransactions.ETransaction`:

| Column | Type | Meaning |
|---|---|---|
| `Recipient_Mode` | `varchar(20) NULL` | `'External'`, `'Internal'` or `'Bulk'`. `NULL` for historical rows and for single-recipient requests created while the create gate is OFF. |
| `Bulk_Listing_SHA256` | `char(64) NULL` | Lower-case hex SHA-256 of the exact listing bytes that were verified. NULL unless Bulk and verified. |
| `Bulk_Recipient_Count` | `int NULL` | Verified recipient rows (> 0 when set). |
| `Bulk_Listing_Validated_DateTime` | `datetime2(7) NULL` | When the application verified the listing (same type as the existing `Created_DateTime`). |

## C. New child table `etransactions.BulkRecipientAccount`

One row per verified listing row of a Bulk transaction. **It holds no account or routing numbers** (the INSERT lists exactly the columns below).

| Column | Type | Null | Notes |
|---|---|---|---|
| `BulkRecipient_Key` | `int IDENTITY(1,1)` | NOT NULL | Primary key `PK_BulkRecipientAccount` (clustered, like every existing table). |
| `Transaction_Key` | `int` | NOT NULL | FK → `ETransaction.Transaction_Key` (no cascade). |
| `Line_Number` | `int` | NOT NULL | Worksheet row number as the requester sees it (first data row = 2). |
| `BankAccount_Key` | `int` | NOT NULL | FK → `BankAccount.BankAccount_Key`; the single matched maintained account. |
| `Verification_Outcome` | `varchar(20)` | NOT NULL | Only `'Matched'` is written (a request with any non-match is never persisted). |
| `Verified_DateTime` | `datetime2(7)` | NOT NULL | When the row was verified. |
| `Listing_SHA256` | `char(64)` | NOT NULL | Equals the owning ETransaction's `Bulk_Listing_SHA256`. |

The exact DDL is in `db_migrations/item6_bulk_recipient/01_bulk_recipient_expand.sql` (section P).

## D. Foreign keys

```sql
ALTER TABLE etransactions.BulkRecipientAccount ADD CONSTRAINT FK_BulkRecipientAccount_ETransaction
    FOREIGN KEY (Transaction_Key) REFERENCES etransactions.ETransaction (Transaction_Key);
ALTER TABLE etransactions.BulkRecipientAccount ADD CONSTRAINT FK_BulkRecipientAccount_BankAccount
    FOREIGN KEY (BankAccount_Key) REFERENCES etransactions.BankAccount (BankAccount_Key);
```

No `ON DELETE CASCADE`: the application replaces a Draft's child rows itself; a submitted transaction's rows are permanent history.
Existing FKs on the two relaxed beneficiary columns are unchanged.

## E. Indexes and unique constraints
Named `FK_BulkRecipientAccount_Transaction` (→ `ETransaction.Transaction_Key`) and `FK_BulkRecipientAccount_BankAccount`
(→ `BankAccount.BankAccount_Key`), both `WITH CHECK`, no cascade.

No `ON DELETE CASCADE`: the application replaces a Draft's child rows itself; a submitted transaction's rows are permanent history.
Existing FKs on the two relaxed beneficiary columns (`FK_Transaction_Beneficiary`, `FK_Transaction_BeneficiaryInstruction`) are retained, untouched and stay trusted.

## E. Unique constraints and indexes

* `UQ_BulkRecipientAccount_Transaction_Line` — `UNIQUE (Transaction_Key, Line_Number)`
* `UQ_BulkRecipientAccount_Transaction_BankAccount` — `UNIQUE (Transaction_Key, BankAccount_Key)`
* `IX_BulkRecipientAccount_BankAccount` — nonclustered on `(BankAccount_Key)`

Unique **constraints** (not bare unique indexes) are used because the existing schema already uses them (`UQ_Transaction_RequestID`,
`UQ_AppUser_EntraObjectID`, `UQ_ApprovalRule_Version`), proving the mechanism on this engine.
The application already rejects duplicate rows and two rows resolving to the same account before writing (and re-checks
distinct keys inside the submitting transaction); the constraints are the database-level backstop.

## F. CHECK constraints

**Created by script 01 (all permit NULL, so the old application build is unaffected):**

* `CK_ETransaction_RecipientMode` — `Recipient_Mode IS NULL OR Recipient_Mode IN ('External','Internal','Bulk')`
* `CK_ETransaction_BulkCount` — `Bulk_Recipient_Count IS NULL OR Bulk_Recipient_Count > 0`
* `CK_ETransaction_BulkMetadata` — hash, count and validated-time are all NULL or all NOT NULL (the app writes them in one UPDATE)
* `CK_BulkRecipientAccount_Outcome` — `Verification_Outcome IN ('Matched')`
* `CK_BulkRecipientAccount_Line` — `Line_Number >= 2`
* `CK_BulkRecipientAccount_Hash` — `LEN(Listing_SHA256) = 64`

**Deferred to script 03 (NOT applicable to the current application build — see "Write-order finding" below):**

* `Recipient_Mode NOT NULL` (with `CK_ETransaction_RecipientMode` re-created without the NULL escape)
* `CK_ETransaction_RecipientMode_Links` — External: beneficiary + instruction NOT NULL, recipient account NULL; Internal: all three NOT NULL; Bulk: all three NULL
* `CK_ETransaction_BulkMetadata_Mode` — listing metadata only on Bulk rows

**Write-order finding (from `db.py`).** The application INSERTs `ETransaction` *without* `Recipient_Mode` and sets it in a later
UPDATE; when a Draft changes between External/Internal and Bulk it changes the keys first and `Recipient_Mode` afterwards.
A `NOT NULL` mode or a mode-vs-link CHECK would therefore reject valid operations. Script 03 may only be run after the application is changed to write
`Recipient_Mode` in the same INSERT/UPDATE statement as the keys, for every request, on every live instance.

**Invariants that table-level constraints cannot guarantee** (no triggers are proposed): a CHECK constraint sees only its own row.

* A submitted Bulk transaction has **at least one** verified recipient row (the rows live in another table; a Draft legitimately has zero) — enforced by the application inside the submitting transaction, monitored by `04` section 6.
* `Bulk_Recipient_Count` equals the number of child rows; child `Listing_SHA256` equals the parent's hash; child rows exist only for `Recipient_Mode = 'Bulk'` parents.
* A recipient `BankAccount` is **Open** (at verification time), is not the originating account, and belongs to the Corporate scope — cross-table facts; Open status is re-read in the submitting transaction (section H limits).
* Each `(AccountNumber, RoutingNumber)` identifies exactly one `BankAccount`: `BankAccount` has **no** unique constraint on that pair (verified; currently zero duplicates). The application blocks ambiguous matches.
* The SharePoint file whose SHA-256 is stored is the file actually kept in SharePoint.
* Immutability after submission (the application's writers are guarded by `Current_Status = 'Draft'`; the database does not prevent a privileged user changing rows).H. Attachment fingerprint, integrity and concurrency

**Fingerprint.** The SQL record of *what was verified* is the SHA-256 of the exact bytes (`Bulk_Listing_SHA256`, repeated on every child row).

**What the code guarantees (all verified by tests):**

1. *Replacing the file invalidates earlier verification.* Every Save Draft / Submit re-verifies the listing it is given (a newly
   posted file, else the newest stored one) and, in one SQL transaction, deletes the old child rows and writes the new rows,
   hash, count and timestamp. A Draft whose newly posted listing is invalid is not saved at all; a stored listing that no longer
   verifies clears the Draft's recipients.
2. *The stored file is the verified file.* After any upload and immediately before the SQL write (Save Draft, new Submit, and Draft finalize), the application re-reads the newest
   `Bulk Recipient Listing` from SharePoint and compares its SHA-256 with the verified one. A mismatch, a missing file or a
   read failure blocks the submission (and clears a Draft's recipients). (Skipped only when SharePoint is disabled in a development configuration.)
3. *Final submission verifies independently.* The browser's check is never trusted; `/intake/submit` re-parses the file and re-matches every row.
4. *Eligibility is re-checked inside the submitting transaction* (`insert_transaction` and `finalize_draft_submission`), before any
   workflow row: `SELECT COUNT(*) FROM etransactions.BankAccount WHERE BankAccount_Key IN (<recipient keys>) AND Status = 'Open' AND BankAccount_Key <> <originating key>`
   must equal the number of verified recipients, the list must be non-empty, distinct and hashed. Otherwise the transaction rolls back (no ETransaction commit, no `WorkflowAssignment`, no `WorkflowEvent`) and the requester sees "no longer eligible".
5. *Rows belong to the right transaction.* Child rows are written with the surrogate key of the transaction created/updated in the same
   transaction; Draft writers are guarded by `PreparedBy_User_Key` and `Current_Status = 'Draft'`.
6. *Double submission:* finalize updates `WHERE ... Current_Status = 'Draft'` and aborts if no row is updated.

**Limitations BI and the business should know about:**

* *Isolation.* Fabric Warehouse transactions use snapshot isolation and there are no lock hints. The eligibility check in item 4
  reads a snapshot, so a `BankAccount` closed (or made the originating account) *after that snapshot but before commit* is not seen.
  The window is milliseconds, but it is not zero.
* *Number edits are not re-matched in the transaction.* Between verification and commit only key, `Status` and "not the originating
  account" are re-checked. If an account's *number* is edited in that window the stored key could refer to a record that no longer carries the listed number.
  Re-matching in the transaction would require carrying the numbers into the write path, which was deliberately avoided.
* *SQL and SharePoint are not atomic.* The hash re-read narrows, but cannot close, the window in which a different listing is uploaded to the same request between the re-read and the commit.
  After submission the listing is not changed by the application; BI can detect later tampering by hashing the SharePoint file (or a SharePoint version) against `Bulk_Listing_SHA256`.
* *Latest wins.* If several listings exist in a request folder, "newest" is by SharePoint created time.
* *Performance.* Verification issues one SQL lookup per row (default ceiling 500 rows, hard ceiling 5000).

No separate attachment table is required. If BI later wants SharePoint item identity in SQL, add a nullable
`Bulk_Listing_SharePoint_ItemId varchar(100)` — **not required** by the current code.

## I. Historical compatibility

* Historical rows keep `Recipient_Mode = NULL`. Where it is read, NULL is interpreted from the existing relationships:
  `RecipientBankAccount_Key IS NOT NULL` → Internal, otherwise External (`db.effective_recipient_mode`); a NULL-mode row with
  no beneficiary instruction is "unavailable", never External. No historical row is rewritten by the application.
* Detail view and Draft view already use `LEFT JOIN` for `Beneficiary` / `BeneficiaryBankInstruction`; the prior-use history match uses
  inner joins and so intentionally never matches a Bulk transaction. Workflow/Treasury queries do not join beneficiary tables.
* Receiving-side reveal for a Bulk transaction returns "not found" (there is no single instruction); originating-side reveal is unchanged.
* `WorkflowEvent.Event_Type` / `Decision` literals are unchanged.
* Existing consumers (Power Automate, reports) that assume `Beneficiary_Key IS NOT NULL` will see NULL for Bulk rows — review before enabling (section O).

## J. SQL the application issues

**Reads** (all parameterised; every read of the new objects is behind the read gate):

| # | Statement (shape) | Used by |
|---|---|---|
| R1 | `SELECT TABLE_NAME, COLUMN_NAME, IS_NULLABLE FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA='etransactions' AND TABLE_NAME IN ('ETransaction','BulkRecipientAccount')` | `bulk_schema_ready()` — cached once ready; while not ready, at most once per minute. The principal must be able to see these columns (SELECT or VIEW DEFINITION). |
| R2 | Dashboard query with the extra select item `t.Recipient_Mode AS recipient_mode` | `get_dashboard_records` **only when the read gate is true**; otherwise the original query text is used unchanged. The same records feed the CSV export column "Recipient Type". |
| R3 | `... t.BeneficiaryInstruction_Key AS _instruction_key ...` (an existing column) added to the detail query | `get_request_detail` (always; existing column). |
| R4 | `SELECT t.Recipient_Mode, t.Bulk_Listing_SHA256, t.Bulk_Recipient_Count, t.Bulk_Listing_Validated_DateTime, (SELECT COUNT(*) FROM BulkRecipientAccount b WHERE b.Transaction_Key = t.Transaction_Key AND b.Verification_Outcome = 'Matched') FROM ETransaction t WHERE t.Transaction_Key = ?` (or `t.Request_ID = ?`) | `get_bulk_state` — request detail and Draft view, **only when the read gate is true**. |
| R5 | `SELECT BankAccount_Key, Status FROM etransactions.BankAccount WHERE AccountNumber = ? AND RoutingNumber = ?` once per listing row | `match_bulk_recipient_accounts` (create gate; Save Draft / Submit / listing check). Only keys and status are returned. |

**Writes** (only when the **create** gate is true *and* the request carries a recipient mode):

| # | Statement | When |
|---|---|---|
| W1 | `INSERT/UPDATE etransactions.ETransaction` with `Beneficiary_Key`, `BeneficiaryInstruction_Key`, `RecipientBankAccount_Key` = NULL | Bulk create / Draft save / finalize |
| W2 | `UPDATE etransactions.ETransaction SET Recipient_Mode = ? WHERE Transaction_Key = ?` | every request while the create gate is on (External / Internal / Bulk) |
| W3 | `DELETE FROM etransactions.BulkRecipientAccount WHERE Transaction_Key = ?`, then per verified row `INSERT INTO etransactions.BulkRecipientAccount (Transaction_Key, Line_Number, BankAccount_Key, Verification_Outcome, Verified_DateTime, Listing_SHA256) VALUES (?,?,?,?,?,?)` | same transaction as W2 |
| W4 | `UPDATE etransactions.ETransaction SET Bulk_Listing_SHA256 = ?, Bulk_Recipient_Count = ?, Bulk_Listing_Validated_DateTime = ? WHERE Transaction_Key = ?` (NULLs when clearing) | same |
| W5 | `SELECT COUNT(*) FROM etransactions.BankAccount WHERE BankAccount_Key IN (...) AND Status = 'Open' AND BankAccount_Key <> ?` | submit/finalize only, before W2–W4 and before any workflow row |
| W6 | `SELECT 1 FROM etransactions.ETransaction WHERE Transaction_Key = ? AND PreparedBy_User_Key = ? AND Current_Status = 'Draft'` then W3/W4 with an empty list | `set_draft_bulk_recipients` (clears a Draft's recipients) |
| W7 | `DELETE FROM etransactions.BeneficiaryBankInstruction WHERE BeneficiaryInstruction_Key = ?` then `DELETE FROM etransactions.Beneficiary WHERE Beneficiary_Key = ?` (after `ETransaction` no longer references them) / or `INSERT` of new rows | a Draft that changes between single-recipient and bulk |

**Permissions for the application principal:** `SELECT, INSERT, DELETE` on `BulkRecipientAccount`; `UPDATE` on the four new ETransaction
columns; **`DELETE` on `Beneficiary` and `BeneficiaryBankInstruction`** (new — only to discard a Draft's own rows); existing `SELECT` on
`BankAccount` columns. The equality predicate in R5 needs no UNMASK (Dynamic Data Masking does not mask predicate evaluation — the same
property the existing prior-use match relies on). Verify this against Fabric before go-live.

## K. What the application checks at runtime (`db.bulk_schema_ready`)

Ready only if all of these appear in `INFORMATION_SCHEMA.COLUMNS` (schema `etransactions`):

* `ETransaction`: `Recipient_Mode`, `Bulk_Listing_SHA256`, `Bulk_Recipient_Count`, `Bulk_Listing_Validated_DateTime`; and
  `Beneficiary_Key`, `BeneficiaryInstruction_Key` with `IS_NULLABLE = 'YES'`.
* `BulkRecipientAccount`: `BulkRecipient_Key`, `Transaction_Key`, `Line_Number`, `BankAccount_Key`, `Verification_Outcome`, `Verified_DateTime`, `Listing_SHA256`.

It does **not** verify constraints, indexes, foreign keys or permissions — BI must apply them. Any error reading the metadata is "not ready" (fails closed).
A positive result is cached for the life of the process; changing the schema back requires an application restart.

## L. Migration order: expand → backfill → harden

1. **Expand (additive; safe while the current app is live):** script `01_bulk_recipient_expand.sql` — section B columns, the section C table with its FKs, unique constraints and CHECKs, the NULL-permitting ETransaction CHECKs, the section A relaxation of the two beneficiary links, and the grant (section J). One atomic, re-runnable transaction.
   Until it completes the read gate stays false (the readiness check needs the columns, the table and nullable links).
2. **(Included in 01)** `ALTER COLUMN Beneficiary_Key` / `BeneficiaryInstruction_Key` to `NULL`. After this the read gate turns true (within a minute); the dashboard/detail/export start reading `Recipient_Mode` (all NULL) with no visible change.
3. **Backfill (optional, recommended for reporting):** script `02_bulk_recipient_backfill.sql` — dry run by default (`@Commit = 0`); classifies only unambiguous historical rows
   (`RecipientBankAccount_Key IS NOT NULL` → Internal, else External; never overwrites an explicit mode; skips rows with no beneficiary, with child rows or with bulk metadata, and reports them as AMBIGUOUS).
   The application does not need it (it interprets NULL).
4. **Harden (DEFERRED, blocked by the write-order finding in section F):** script `03_bulk_recipient_harden.sql` — stops unless `@ConfirmPrerequisites = 1`.
   Do not run it until the application writes `Recipient_Mode` in the same INSERT/UPDATE as the keys on every live instance.

## M. Deployment and rollback

**Safe activation:**

1. Deploy the application with `BULK_RECIPIENT_ENABLED` unset/false. Behavior is identical to today.
2. BI applies script 01 (section L) and verifies with `04` sections 1–6, 8. The read gate is now true; the create gate is still false.
3. A SharePoint administrator adds `Bulk Recipient Listing` to the library's strict **DocumentType** choice column (section O). Until then uploads of this category cannot be tagged.
4. Set `BULK_RECIPIENT_ENABLED=true` in a **non-production** environment; run an end-to-end test (Corporate ACH, Same Day ACH, Wire; one unmatched row blocks;
   Save Draft → Resume → Submit; approvals; CSV export; dashboard badge).
5. Enable in production.

```sql
SELECT COLUMN_NAME, IS_NULLABLE FROM INFORMATION_SCHEMA.COLUMNS
 WHERE TABLE_SCHEMA='etransactions' AND TABLE_NAME='ETransaction'
   AND COLUMN_NAME IN ('Recipient_Mode','Bulk_Listing_SHA256','Bulk_Recipient_Count',
                       'Bulk_Listing_Validated_DateTime','Beneficiary_Key','BeneficiaryInstruction_Key');
SELECT COUNT(*) FROM etransactions.BulkRecipientAccount;   -- 0 before first use
```

**Rollback:**

* *Fast, no schema change:* set `BULK_RECIPIENT_ENABLED=false`. New Bulk creation stops immediately. **Existing Bulk transactions keep displaying accurately and
  keep moving through approvals and Treasury actions** (read gate). Existing Bulk Drafts cannot be edited until the switch is turned back on.
* *Schema rollback* is safe only while no Bulk row exists: `SELECT COUNT(*) FROM etransactions.ETransaction WHERE Recipient_Mode = 'Bulk'` must be 0 before
  re-imposing `NOT NULL` on the two beneficiary columns; then drop the added constraints, the child table and the four columns (reverse of section L).
  If the schema is removed while Bulk rows exist, the application (restarted) reads the gate as false and shows those rows as "Recipient details unavailable".
* The application never deletes a submitted transaction's rows.

## N. Dashboard and export dependencies

* **Dashboard:** `recipient_mode` is added to each record when the read gate is true; the Request Type cell shows a **Bulk Recipients** badge beside the unchanged request-type badge.
  Urgent indicators, filters (status, type, property, approver, urgent, amount) and role-based scoping (`_dashboard_scope_where`) are untouched.
* **CSV export:** one new column **Recipient Type** (`recipient_mode`) after Request Type: `Bulk Recipients` or empty. The request type and Urgent columns are unchanged.
  Allow-listed columns only: no account/routing numbers, no listing contents, no hash, no recipient keys.
* Both use the same authorized record set (`get_dashboard_records`); export is never broader than the dashboard.

## O. Remaining business decisions, dependencies and control classification

**Treasury-approved requirements (implemented as given):** Corporate only; ACH / Same Day ACH / Wire only; recipients are maintained internal BankAccounts;
every listing row must match exactly one BankAccount and one miss blocks everything; the listing counts as the External ACH/Wire Instructions;
AVS and previously-used matching do not apply; approvals and thresholds unchanged; one ETransaction / one workflow; no Beneficiary rows;
SharePoint section "D - Receiving Banking" / DocumentType "Bulk Recipient Listing"; Recipient_Mode External/Internal/Bulk, NULL historically; all banking data masked and never in HTML/logs/CSV.

**Existing controls deliberately kept (not new policy):** Payment Support attachment required; verbal confirmation with a known contact (contact name and date/time) required;
Same Day ACH forced urgent; originating account must be Open.

**Application safeguards that need Treasury confirmation:**

1. A matched BankAccount that is not `Open` blocks the listing.
2. The originating account appearing in the listing blocks it.
3. Duplicate rows, or two rows resolving to the same BankAccount, block it.
4. Verbal confirmation is required for Bulk (the listing is new, unverified instruction material).
5. Account and routing cells must be **Text** (numeric cells are rejected: leading zeros cannot be proven); values are 3–34 letters/digits, no spaces or symbols.
6. `.xlsx` only (CSV is not approved); only the first sheet is read.
7. **Operational limits, not business limits:** 500 rows and 5 MB by default, configurable with `BULK_LISTING_MAX_ROWS` / `BULK_LISTING_MAX_FILE_MB` (hard ceilings 5000 rows / 10 MB).
8. The request carries one total `Amount`; the listing carries no per-recipient amounts.

**Dependencies / open items:**

* **SharePoint administrator step:** add `Bulk Recipient Listing` to the DocumentType choice list (the app does not modify SharePoint).
* **Power Automate / reports** that read `Beneficiary_Key`, payee or receiving-bank values must handle Bulk rows (no payee, no single account); `Recipient_Mode` is the field to read.
* **Receiving-side reveal** for Bulk is not offered; a controlled per-account reveal would be a separate feature.
* **Property bulk payments** are out of scope (Corporate only).
* Whether `Recipient_Mode` should become `NOT NULL` after a backfill (section L) — blocked until the application's write order changes (section F).

## P. DDL scripts and catalog findings

Scripts (in `db_migrations/item6_bulk_recipient/`, none executed by the author):

| Script | Purpose |
|---|---|
| `01_bulk_recipient_expand.sql` | Backward-compatible expansion (sections A–F, J grant). Atomic, guarded, re-runnable. |
| `02_bulk_recipient_backfill.sql` | Guarded historical classification; dry run by default. |
| `03_bulk_recipient_harden.sql` | DO NOT EXECUTE - DEFERRED APPLICATION DEPENDENCY (the app writes `Recipient_Mode` after the INSERT); stops unless prerequisites are confirmed. |
| `04_bulk_recipient_validation.sql` | Read-only verification (baseline fingerprint over 33 explicit original columns, constraint trust, integrity, permissions, compatibility) and non-production test cases. |
| `05_bulk_recipient_rollback.sql` | Guarded recovery reference; refuses to run if any Bulk data exists. |
| `BI_EXECUTION_README.md` | BI handoff: sequence, result legend, uncertainties, rollback limits. |

Facts read from the live catalog (SELECT-only) that the scripts rely on:

* Engine: `Microsoft SQL Azure (RTM) 12.0.2000.8`, compatibility level 170, read-committed snapshot ON and snapshot isolation ON. `SERVER_PROPERTY()` is **not** supported; `STRING_AGG`, `HAS_PERMS_BY_NAME` and the `sys.*` catalog views are.
* `ETransaction` has 33 columns; `Beneficiary_Key` and `BeneficiaryInstruction_Key` are `int NOT NULL` with one trusted FK each; `Transaction_Key`, `BankAccount_Key` are `int`; timestamps are `datetime2(7)`.
* `ETransaction` has **no** CHECK or DEFAULT constraints, no triggers, and no indexes besides `PK_Transaction` (clustered) and `UQ_Transaction_RequestID`; nothing depends on the two altered columns. No views/procedures/functions exist in `etransactions` (`sys.sql_expression_dependencies` was not readable by the application principal — BI should confirm).
* `BankAccount.AccountNumber` / `RoutingNumber` are Dynamic Data Masking columns and are **not** unique as a pair (zero duplicates today).
* The application principal `E-Transaction Approval` has no `ALTER` / `CREATE TABLE`, has `UPDATE` on `ETransaction`, `DELETE` on `Beneficiary` and `BeneficiaryBankInstruction`, `SELECT` on `BankAccount`, and belongs to no database role.
* An empty `etransactions.Attachment` table exists (with `Version_Number`, `Is_Current`); the application does not use it and the design deliberately does not (SharePoint remains the only document store).

Not verified by execution (no DDL was run): DDL inside an explicit transaction, `ALTER COLUMN ... NULL` on a foreign-key column, `WITH CHECK` constraints, `CREATE NONCLUSTERED INDEX`, `GRANT` on the new table, and the Dynamic-Data-Masking equality predicate on `BankAccount`. Run the scripts on a non-production copy first.
