"""
Batch 9 — Dashboard filtered export (CSV).

This module owns ONLY presentation concerns for the export file: the column
list, CSV-formula-injection sanitization, value formatting, and filename
generation. It has NO authorization or query logic of its own — the export
route in app.py builds the record list using the exact same authorized scope
and filter functions the dashboard view uses (db.get_dashboard_records() /
app._resolve_dashboard_authorized_scope() / app._apply_dashboard_filters()),
so authorization can never drift between the dashboard and its export.

Deliberately excluded (see Batch 9 spec Parts 9/10):
  - all banking/account/routing/tax-id data (never joined into the dashboard
    query this export reuses, so it's structurally impossible to leak here)
  - TransactionComment/RFI history, attachment file contents, SharePoint URLs
  - internal surrogate keys (Transaction_Key, *_User_Key)
"""
import csv
import io
from datetime import datetime

# (column header, dashboard-record dict key) — only safe, business-meaningful
# reporting fields already present in db.get_dashboard_records() output.
EXPORT_COLUMNS = [
    ("Request ID",             "request_id"),
    ("Current Status",         "status"),
    ("Current Workflow Stage", "current_workflow_stage"),
    ("Request Type",           "request_type"),
    ("Prepared By",            "prepared_by"),
    ("Prepared Date",          "prepared_date"),
    ("Submitted Date",         "submitted_date"),
    ("Treasury Service Date",  "treasury_service_date"),
    ("Property / Department",  "property_dept"),
    ("Entity ID",              "property_code"),
    ("Amount",                 "amount"),
    ("Currency",               "currency"),
    ("Urgent",                 "urgent"),
    ("Approver",               "approver"),
    ("Controller",             "controller"),
    ("VP Approver",            "vp_approver"),
    ("CFO Approver",           "cfo_approver"),
    ("Current Owner",          "assigned_approver"),
    ("Payment Purpose",        "payment_purpose"),
    ("Accounting Group",       "accounting_group_name"),
    ("Last Modified Date",     "last_modified_date"),
]

# OWASP CSV/Excel formula-injection guard — a leading quote forces spreadsheet
# applications to treat the value as literal text instead of evaluating it.
_DANGEROUS_LEADING_CHARS = ("=", "+", "-", "@")


def _sanitize_text(value) -> str:
    text = "" if value is None else str(value)
    if text[:1] in _DANGEROUS_LEADING_CHARS:
        return "'" + text
    return text


def _format_value(key, value):
    if key == "amount":
        try:
            return f"{float(value or 0):.2f}"
        except (TypeError, ValueError):
            return "0.00"
    if key == "urgent":
        return "Yes" if value else "No"
    return _sanitize_text(value)


def build_dashboard_export_csv(records) -> bytes:
    """
    Render already-authorized, already-filtered dashboard records as CSV
    bytes. utf-8-sig (BOM) so Excel reliably detects UTF-8 on open.
    """
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow([header for header, _ in EXPORT_COLUMNS])
    for r in records:
        writer.writerow([_format_value(key, r.get(key)) for _, key in EXPORT_COLUMNS])
    return buf.getvalue().encode("utf-8-sig")


def export_filename() -> str:
    """Safe, content-free filename — no user email, transaction IDs, or search terms (Part 14)."""
    return f"ETransaction_Export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
