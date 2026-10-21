# Bulk Recipient Listing — spreadsheet format and control classification

The requester attaches one Excel workbook to a Corporate **ACH, Same Day ACH or Wire** bulk-recipient request.
A ready-made template is in `static/templates/Bulk_Recipient_Listing_Template.xlsx` (also linked from the intake page).

## What the application reads

| Item | Rule |
|---|---|
| File type | `.xlsx` only. CSV, PDF and other formats are not accepted. The workbook must not be password-protected or encrypted by a sensitivity label (rights-managed workbooks cannot be read); such files, and legacy `.xls` files renamed `.xlsx`, are rejected with an explanatory message. |
| Sheet | **First sheet only.** Other sheets (the template's "Instructions" sheet) are ignored. |
| Header row | The first non-empty row must contain the headers **Account Number** and **Routing Number** (case and extra spaces ignored). Other columns are ignored. A header that appears twice is an error. |
| Data rows | One recipient account per row, from the row below the header. Fully blank rows are skipped; a row with only one of the two values is an error. |
| Cell type | Both columns must be **Text**. The template pre-formats them as Text. **Numeric cells are rejected** (Excel drops leading zeros from numbers), as are formula cells and error cells. Formulas are never evaluated. |
| Value format | 3–34 letters or digits; no spaces or symbols. Leading and trailing spaces are trimmed. |
| Matching | Every row must match **exactly one** `BankAccount` on the **full** account number **and** routing number (compared inside SQL; never last-four). |
| Limits (operational) | 500 rows and 5 MB by default; configurable via `BULK_LISTING_MAX_ROWS` / `BULK_LISTING_MAX_FILE_MB` (hard ceilings 5000 rows / 10 MB). These are IT operational restrictions, **not** Treasury-approved business limits. |

Any problem in any row blocks the whole request. Messages name only the **row number** and the problem (for example
"Row 7: no matching BankAccount record.") — never an account or routing number.

## Control classification

**Treasury-approved requirements**

* Corporate requests only; request types ACH, Same Day ACH, Wire only (not ACH Pull, Intra Bank Transfer, EFT).
* Recipients are internal maintained BankAccounts; every row matches exactly one; one miss blocks everything.
* The listing counts as the External ACH/Wire Instructions; AVS and previously-used matching do not apply.
* Approvals, thresholds, approvers and workflow are unchanged; one transaction and one workflow.
* No beneficiary rows; banking data always masked and never in HTML, logs or CSV.

**Existing controls kept**

* Payment Support attachment required.
* Verbal confirmation with a known contact (name and date/time) required.
* Same Day ACH is automatically urgent; the originating account must be Open.

**Application safeguards (need Treasury confirmation; none was removed silently)**

* A matched account that is not Open blocks the listing.
* The originating account in the listing blocks it.
* Duplicate rows, or two rows that resolve to the same BankAccount, block it.
* Text-only cells, 3–34 alphanumeric characters, `.xlsx` only, first sheet only.
* Row and file-size limits (operational, configurable).
* Verbal confirmation is also required for Bulk (see above) — Treasury may later want different wording or an exemption.

## What is stored

Only the file's SHA-256, the verified row count and time, and one row per recipient (`Line_Number`, matched `BankAccount_Key`, outcome, time, hash) —
never the account or routing numbers. See `BULK_RECIPIENT_SCHEMA_CONTRACT.md`.
