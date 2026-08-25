"""
E-Transaction Approval Dashboard — Flask Prototype
====================================================
PROTOTYPE / DEMO PURPOSES ONLY
Not connected to real systems. No real banking data stored.
For requirements-gathering and stakeholder demonstration only.

TODO (Future Integration Points):
  [AUTH]      Authentication / user roles (Azure AD / MSAL)
  [STORAGE]   SharePoint list or SQL table (replace session-based mock data)
  [UPLOAD]    Document storage (SharePoint Document Library / Azure Blob)
  [ESIGN]     E-signature integration (DocuSign / Adobe Sign)
  [EMAIL]     Email notifications (Microsoft Graph / SendGrid)
  [WORKFLOW]  Approval routing engine (Power Automate / custom)
  [AUDIT]     Immutable audit log for all status changes and actions
  [BANKING]   Banking instruction validation (Wells Fargo AVS API)
  [RELEASE]   Treasury release confirmation workflow
  [RBAC]      Permission-based access to sensitive banking details
"""

from flask import (
    Flask, render_template, request,
    redirect, url_for, session, flash,
)
from datetime import datetime
import copy
import os
import random

from dotenv import load_dotenv
load_dotenv()  # loads .env into os.environ; no-op if file is absent

from mock_data import MOCK_REQUESTS, get_approval_tier, tier_to_status, mask_account, mask_routing
import db

app = Flask(__name__)
# [AUTH] TODO: Rotate this key before any real deployment
app.secret_key = os.environ.get("SECRET_KEY") or "etxn-demo-prototype-2026-not-for-production"

# Display names used in timeline / comment author fields
ROLE_DISPLAY = {
    "submitter":  "Submitter",
    "sam":        "Sr. Accounting Manager",
    "controller": "Controller",
    "vp":         "Vice President",
    "cfo":        "CFO",
    "treasury":   "Treasury Manager",
}

# ─────────────────────────────────────────────────────────────
#  Jinja2 Filters & Context Processors
# ─────────────────────────────────────────────────────────────

STATUS_BADGE_MAP = {
    "Draft":                        "bg-secondary",
    "Submitted":                    "bg-info text-dark",
    "Pending SAM Approval":         "bg-info text-dark",
    "Pending Controller Approval":  "bg-warning text-dark",
    "Pending VP Approval":          "badge-orange",
    "Pending CFO Approval":         "bg-danger",
    "Pending Treasury Review":      "bg-primary",
    "Pending Release":              "badge-purple",
    "Released":                     "badge-teal",
    "Completed":                    "bg-success",
    "Rejected":                     "bg-danger",
    "Needs More Information":       "bg-warning text-dark",
    "Cancelled":                    "bg-secondary",
}


@app.template_filter("status_badge_class")
def status_badge_class(status):
    return STATUS_BADGE_MAP.get(status, "bg-secondary")


@app.template_filter("currency")
def currency_fmt(value):
    try:
        return f"${float(value):,.2f}"
    except (ValueError, TypeError):
        return str(value)


@app.template_filter("yesno")
def yesno(value):
    return "Yes" if value else "No"


@app.context_processor
def inject_globals():
    return {
        "is_prototype": True,
        "current_year": datetime.now().year,
        # [AUTH] TODO: Replace with Azure AD role claim from MSAL token
        "current_role": session.get("role"),
    }


# [AUTH] Prototype-only role gate — replace with Azure AD / MSAL authentication
ROLE_FREE_ENDPOINTS = {"role_select", "switch_role", "static"}

@app.before_request
def require_role():
    if request.endpoint in ROLE_FREE_ENDPOINTS or request.endpoint is None:
        return None
    if not session.get("role"):
        return redirect(url_for("role_select"))


# ─────────────────────────────────────────────────────────────
#  Routes
# ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if not session.get("role"):
        return redirect(url_for("role_select"))
    return redirect(url_for("dashboard"))


@app.route("/role-select", methods=["GET", "POST"])
def role_select():
    """Prototype role picker — simulates future Azure AD role assignment. [AUTH]"""
    if request.method == "POST":
        role = request.form.get("role", "")
        if role in ("submitter", "sam", "controller", "vp", "cfo", "treasury"):
            session["role"] = role
        return redirect(url_for("dashboard"))
    if session.get("role"):
        return redirect(url_for("dashboard"))
    return render_template("role_select.html")


@app.route("/switch-role")
def switch_role():
    """Clear active role and return to role picker."""
    session.pop("role", None)
    return redirect(url_for("role_select"))


@app.route("/intake")
def intake():
    """Treasury Request Intake Form."""
    if session.get("role") == "treasury":
        flash("Treasury role does not submit payment requests.", "warning")
        return redirect(url_for("dashboard"))
    return render_template("intake.html")


@app.route("/intake/submit", methods=["POST"])
def intake_submit():
    """
    Handle intake form submission (demo — stores in Flask session).

    [STORAGE]  TODO: Write record to SharePoint list or SQL table.
    [WORKFLOW] TODO: Trigger approval routing after submission.
    [EMAIL]    TODO: Notify assigned approver via Microsoft Graph.
    [UPLOAD]   TODO: Process and store attached documents.
    [AUDIT]    TODO: Write submission event to audit log.
    """
    if session.get("role") == "treasury":
        flash("Treasury role does not submit payment requests.", "warning")
        return redirect(url_for("dashboard"))
    frm = request.form

    try:
        amount = float(frm.get("amount", "0").replace(",", "") or 0)
    except ValueError:
        amount = 0.0

    # Verbal confirmation composite value
    verbal_parts = []
    if frm.get("verbal_confirmed_with_known") == "on":
        verbal_parts.append("Known Contact")
    if frm.get("verbal_confirmed_with_requester") == "on":
        verbal_parts.append("Requesting Person")

    approval_tier = get_approval_tier(amount)
    request_id = f"TXN-{datetime.now().year}-{random.randint(100, 999)}"

    record = {
        "request_id":            request_id,
        "submitted_date":        datetime.now().strftime("%Y-%m-%d"),
        "request_type":          frm.get("request_type", ""),
        "property_dept":         frm.get("property_dept", ""),
        "property_code":         frm.get("property_code", ""),
        "prepared_by":           frm.get("prepared_by", ""),
        "prepared_date":         frm.get("prepared_date", ""),
        "approver":              frm.get("approver", ""),
        "controller":            frm.get("controller", ""),
        "treasury_service_date":         frm.get("treasury_service_date", ""),
        "instructions_previously_used":  frm.get("instructions_previously_used") == "yes",
        "last_used_date":                frm.get("last_used_date", ""),
        "amount":                amount,
        "currency":              frm.get("currency", "USD"),
        "approval_tier":         approval_tier,
        # [WORKFLOW] Auto-routed to the correct approval tier on submission
        "status":                tier_to_status(approval_tier),
        "urgent":                frm.get("urgent") == "yes",
        "urgency_reason":        frm.get("urgency_reason", ""),
        "payment_purpose":       frm.get("payment_purpose", ""),
        "over_1m":               amount > 1_000_000,
        "assigned_approver":     "Pending Assignment",
        "days_pending":          0,
        # Originating bank
        "orig_bank_name":        frm.get("orig_bank_name", ""),
        "orig_account_name":     frm.get("orig_account_name", ""),
        "orig_account_number":   frm.get("orig_account_number", ""),
        "orig_routing_number":   frm.get("orig_routing_number", ""),
        "orig_bank_contact":     frm.get("orig_bank_contact", ""),
        "notes_orig":            frm.get("notes_orig", ""),
        # Receiving bank
        "recv_payee_name":       frm.get("recv_payee_name", ""),
        "recv_bank_name":        frm.get("recv_bank_name", ""),
        "recv_account_name":     frm.get("recv_account_name", ""),
        "recv_account_number":   frm.get("recv_account_number", ""),
        "recv_routing_number":   frm.get("recv_routing_number", ""),
        "recv_bank_address":     frm.get("recv_bank_address", ""),
        "recv_contact_name":     frm.get("recv_contact_name", ""),
        "recv_contact_email":    frm.get("recv_contact_email", ""),
        "recv_contact_phone":    frm.get("recv_contact_phone", ""),
        "notes_recv":            frm.get("notes_recv", ""),
        # Verification
        "verbal_confirmed":          frm.get("verbal_confirmed") == "on",
        "verbal_confirmed_with":     ", ".join(verbal_parts),
        "verbal_contact_name":       frm.get("verbal_contact_name", ""),
        "verbal_confirm_datetime":   frm.get("verbal_confirm_datetime", ""),
        "avs_score":                 frm.get("avs_score", ""),
        "external_source":           frm.get("external_source") == "on",
        "internal_doc_not_used":     frm.get("internal_doc_not_used") == "on",
        # Attachments (demo — filenames recorded; no files stored in prototype)
        # [UPLOAD] TODO: Store files in SharePoint Document Library or Azure Blob Storage
        "attachments": {
            "validation_evidence":   (request.files["file_validation_evidence"].filename
                                      if "file_validation_evidence" in request.files
                                      and request.files["file_validation_evidence"].filename else ""),
            "wire_ach_instructions": (request.files["file_wire_ach_instructions"].filename
                                      if "file_wire_ach_instructions" in request.files
                                      and request.files["file_wire_ach_instructions"].filename else ""),
            "payment_support":       (request.files["file_payment_support"].filename
                                      if "file_payment_support" in request.files
                                      and request.files["file_payment_support"].filename else ""),
        },
        # Timeline & comments
        "timeline": [
            {
                "date":   datetime.now().strftime("%Y-%m-%d"),
                "event":  f"Submitted by {frm.get('prepared_by', 'Unknown User')}",
                "actor":  frm.get("prepared_by", "Unknown User"),
                "status": "Submitted",
                "type":   "submitted",
            },
            {
                "date":   datetime.now().strftime("%Y-%m-%d"),
                "event":  f"Routed for {approval_tier} Approval",
                "actor":  "System",
                "status": tier_to_status(approval_tier),
                "type":   "routed",
            },
        ],
        "comments": [],
    }

    # [STORAGE] TODO: Replace with database / SharePoint write
    submitted = session.get("submitted_requests", [])
    submitted.append(record)
    session["submitted_requests"] = submitted

    return redirect(url_for("confirmation", request_id=request_id))


@app.route("/confirmation/<request_id>")
def confirmation(request_id):
    """Post-submission confirmation screen."""
    submitted = session.get("submitted_requests", [])
    record = next((r for r in submitted if r["request_id"] == request_id), None)
    if not record:
        record = next((r for r in MOCK_REQUESTS if r["request_id"] == request_id), None)
    return render_template("confirmation.html", record=record, request_id=request_id)


@app.route("/dashboard")
def dashboard():
    """
    Dashboard — filtered by role; all requests for VP/CFO/Treasury, tier queue for others.

    [AUTH]    TODO: Filter results by real user identity and permissions from Azure AD.
    [STORAGE] TODO: Query live data from SharePoint list / SQL.
    """
    submitted = session.get("submitted_requests", [])
    role      = session.get("role")

    # [STORAGE] Set DB_ENABLED=true in .env when the database is ready
    _db_on = os.environ.get("DB_ENABLED", "false").lower() == "true"
    try:
        db_records = db.get_dashboard_records() if _db_on else list(MOCK_REQUESTS)
    except Exception:
        db_records = list(MOCK_REQUESTS)

    all_requests = db_records + submitted

    # [AUTH] Role-based scoping — replace with real RBAC when Azure AD is integrated
    if role == "submitter":
        scoped = list(submitted)
    elif role == "sam":
        scoped = [r for r in all_requests if r["status"] == "Pending SAM Approval"]
    elif role == "controller":
        scoped = [r for r in all_requests if r["status"] == "Pending Controller Approval"]
    else:  # vp, cfo, treasury — full visibility
        scoped = list(all_requests)

    stats = {
        "total":              len(scoped),
        "pending_sam":        sum(1 for r in scoped if r["status"] == "Pending SAM Approval"),
        "pending_controller": sum(1 for r in scoped if r["status"] == "Pending Controller Approval"),
        "pending_vp":         sum(1 for r in scoped if r["status"] == "Pending VP Approval"),
        "pending_cfo":        sum(1 for r in scoped if r["status"] == "Pending CFO Approval"),
        "pending_treasury":   sum(1 for r in scoped if r["status"] == "Pending Treasury Review"),
        "pending_release":    sum(1 for r in scoped if r["status"] == "Pending Release"),
        "completed":          sum(1 for r in scoped if r["status"] in ("Completed", "Released")),
        "needs_more_info":    sum(1 for r in scoped if r["status"] == "Needs More Information"),
        "rejected":           sum(1 for r in scoped if r["status"] == "Rejected"),
        "urgent":             sum(1 for r in scoped if r.get("urgent", False)),
        "over_1m":            sum(1 for r in scoped if r.get("amount", 0) > 1_000_000),
    }

    f_status   = request.args.get("status", "").strip()
    f_type     = request.args.get("request_type", "").strip()
    f_property = request.args.get("property", "").strip()
    f_approver = request.args.get("approver", "").strip()
    f_urgent   = request.args.get("urgent_only", "")
    f_over1m   = request.args.get("over_1m_only", "")
    f_amt_min  = request.args.get("amount_min", "").strip()
    f_amt_max  = request.args.get("amount_max", "").strip()

    filtered = list(scoped)
    if f_status:
        filtered = [r for r in filtered if r["status"] == f_status]
    if f_type:
        filtered = [r for r in filtered if r["request_type"] == f_type]
    if f_property:
        filtered = [r for r in filtered if f_property.lower() in r.get("property_dept", "").lower()]
    if f_approver:
        filtered = [r for r in filtered if f_approver.lower() in r.get("assigned_approver", "").lower()]
    if f_urgent:
        filtered = [r for r in filtered if r.get("urgent", False)]
    if f_over1m:
        filtered = [r for r in filtered if r.get("amount", 0) > 1_000_000]
    if f_amt_min:
        try:
            filtered = [r for r in filtered if r.get("amount", 0) >= float(f_amt_min)]
        except ValueError:
            pass
    if f_amt_max:
        try:
            filtered = [r for r in filtered if r.get("amount", 0) <= float(f_amt_max)]
        except ValueError:
            pass

    filtered.sort(key=lambda r: r["submitted_date"], reverse=True)

    all_statuses  = sorted({r["status"] for r in scoped})
    all_types     = sorted({r["request_type"] for r in scoped})
    all_approvers = sorted({r.get("assigned_approver", "") for r in scoped if r.get("assigned_approver")})

    return render_template(
        "dashboard.html",
        requests=filtered,
        stats=stats,
        all_statuses=all_statuses,
        all_types=all_types,
        all_approvers=all_approvers,
        filters={
            "status":       f_status,
            "request_type": f_type,
            "property":     f_property,
            "approver":     f_approver,
            "urgent_only":  f_urgent,
            "over_1m_only": f_over1m,
            "amount_min":   f_amt_min,
            "amount_max":   f_amt_max,
        },
    )


@app.route("/dashboard/request/<request_id>")
def request_detail(request_id):
    """
    Request detail view with masked banking information.

    [AUTH]  TODO: Enforce role-based access — only treasury/approvers see banking details.
    [RBAC]  TODO: Mask account numbers by user role, not a demo toggle.
    [AUDIT] TODO: Log each access to sensitive banking data.
    """
    record = None
    _db_on = os.environ.get("DB_ENABLED", "false").lower() == "true"
    if _db_on:
        try:
            record = db.get_request_detail(request_id)
        except Exception:
            pass

    if record is None:
        submitted = session.get("submitted_requests", [])
        all_requests = MOCK_REQUESTS + submitted
        record = next((r for r in all_requests if r["request_id"] == request_id), None)

    if not record:
        flash("Request not found.", "warning")
        return redirect(url_for("dashboard"))

    # [RBAC] TODO: Only unmask for authorized roles
    display = copy.deepcopy(record)
    display["orig_account_number_masked"] = mask_account(record.get("orig_account_number", ""))
    display["orig_routing_number_masked"]  = mask_routing(record.get("orig_routing_number", ""))
    display["recv_account_number_masked"]  = mask_account(record.get("recv_account_number", ""))
    display["recv_routing_number_masked"]  = mask_routing(record.get("recv_routing_number", ""))

    return render_template("request_detail.html", record=display)


@app.route("/dashboard/request/<request_id>/action", methods=["POST"])
def request_action(request_id):
    """
    Process approval actions (demo — updates session data only).

    [WORKFLOW] TODO: Trigger real approval workflow engine.
    [EMAIL]    TODO: Send status-change notification email.
    [AUDIT]    TODO: Write action to immutable audit log.
    [ESIGN]    TODO: Trigger e-signature flow if required.
    """
    action  = request.form.get("action", "")
    comment = request.form.get("comment", "").strip()
    role    = session.get("role")

    action_map = {
        "approve":           ("Pending Treasury Review",   "Approved",                           "success"),
        "reject":            ("Rejected",                  "Rejected",                           "danger"),
        "more_info":         ("Needs More Information",    "Returned \u2013 More Information Needed", "warning"),
        "cancel":            ("Cancelled",                 "Cancelled by Submitter",              "secondary"),
        "treasury_reviewed": ("Pending Release",           "Marked as Treasury Reviewed",         "info"),
        "mark_released":     ("Released",                  "Marked as Released",                  "success"),
        "mark_completed":    ("Completed",                 "Marked as Completed",                 "success"),
    }

    # resubmit target status is computed from the record's tier, so handled separately
    if action not in action_map and action != "resubmit":
        flash("Unknown action.", "danger")
        return redirect(url_for("request_detail", request_id=request_id))

    # Look up current status for authorization check
    submitted    = session.get("submitted_requests", [])
    all_requests = MOCK_REQUESTS + submitted
    record_ref   = next((r for r in all_requests if r["request_id"] == request_id), None)
    cur_status   = record_ref["status"] if record_ref else ""

    # [RBAC] Role+status authorization — simulates permission gates that Azure AD will enforce
    ALLOWED: dict[str, dict[str, bool]] = {
        "submitter": {
            "cancel":   cur_status in ("Submitted", "Pending SAM Approval", "Needs More Information"),
            "resubmit": cur_status == "Needs More Information",
        },
        "sam": {
            "approve":   cur_status == "Pending SAM Approval",
            "reject":    cur_status == "Pending SAM Approval",
            "more_info": cur_status == "Pending SAM Approval",
        },
        "controller": {
            "approve":   cur_status == "Pending Controller Approval",
            "reject":    cur_status == "Pending Controller Approval",
            "more_info": cur_status == "Pending Controller Approval",
        },
        "vp": {
            "approve":   cur_status == "Pending VP Approval",
            "reject":    cur_status == "Pending VP Approval",
            "more_info": cur_status == "Pending VP Approval",
        },
        "cfo": {
            "approve":   cur_status == "Pending CFO Approval",
            "reject":    cur_status == "Pending CFO Approval",
            "more_info": cur_status == "Pending CFO Approval",
        },
        "treasury": {
            "treasury_reviewed": cur_status == "Pending Treasury Review",
            "mark_released":     cur_status == "Pending Release",
            "mark_completed":    cur_status == "Released",
        },
    }
    if not ALLOWED.get(role, {}).get(action, False):
        flash("Your role is not authorized to take this action at the current stage.", "warning")
        return redirect(url_for("request_detail", request_id=request_id))

    # Resolve new status and message
    if action == "resubmit":
        tier       = record_ref.get("approval_tier", "") if record_ref else ""
        new_status = tier_to_status(tier)
        label      = "Resubmitted with Additional Information"
        cat        = "info"
    else:
        new_status, label, cat = action_map[action]

    actor = ROLE_DISPLAY.get(role, "Demo User")

    updated = False
    for r in submitted:
        if r["request_id"] == request_id:
            r["status"] = new_status
            if comment:
                r.setdefault("comments", []).append({
                    "author": actor,
                    "date":   datetime.now().strftime("%Y-%m-%d"),
                    "text":   comment,
                })
            r.setdefault("timeline", []).append({
                "date":   datetime.now().strftime("%Y-%m-%d"),
                "event":  f"{label} \u2014 {actor}",
                "actor":  actor,
                "status": new_status,
                "type":   action,
            })
            updated = True
    session["submitted_requests"] = submitted

    if updated:
        flash(f"Action recorded: <strong>{label}</strong> for {request_id}. (Demo only — no real workflow triggered.)", cat)
    else:
        flash(
            f"Note: {request_id} is pre-loaded mock data. "
            "Status changes for mock records are not persisted in this prototype.",
            "info",
        )

    return redirect(url_for("request_detail", request_id=request_id))


@app.route("/dashboard/request/<request_id>/attach", methods=["POST"])
def request_attach(request_id):
    """
    Attach additional supporting files to a request (demo — filenames recorded only).

    [UPLOAD] TODO: Store files in SharePoint Document Library or Azure Blob Storage.
    [AUDIT]  TODO: Log file attachment events to immutable audit log.
    """
    uploaded_files = request.files.getlist("extra_files")
    description    = request.form.get("attachment_description", "").strip()

    filenames = [f.filename for f in uploaded_files if f.filename]
    if not filenames:
        flash("No files selected.", "warning")
        return redirect(url_for("request_detail", request_id=request_id))

    role     = session.get("role", "demo")
    uploader = role.capitalize()
    today    = datetime.now().strftime("%Y-%m-%d")

    new_entries = [
        {"filename": fn, "description": description, "uploaded_by": uploader, "date": today}
        for fn in filenames
    ]

    submitted = session.get("submitted_requests", [])
    updated   = False
    for r in submitted:
        if r["request_id"] == request_id:
            r.setdefault("extra_attachments", []).extend(new_entries)
            updated = True
    session["submitted_requests"] = submitted

    if updated:
        flash(
            f"{len(filenames)} file(s) attached to {request_id}. "
            "(Demo only — filenames recorded, no files stored.)",
            "success",
        )
    else:
        flash(
            f"Note: {request_id} is pre-loaded mock data. "
            "Attachments cannot be persisted for mock records in this prototype.",
            "info",
        )

    return redirect(url_for("request_detail", request_id=request_id))


if __name__ == "__main__":
    app.run(debug=True, port=5000)
