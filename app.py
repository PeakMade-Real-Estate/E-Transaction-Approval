"""
E-Transaction Approval Dashboard — Flask Application
====================================================
Development application for the treasury e-transaction approval workflow.

TODO (Future Integration Points):
  [AUTH]      Authentication / user roles (Azure AD / MSAL)
    [STORAGE]   Extend the SQL persistence layer as additional workflow fields are implemented
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
    redirect, url_for, session, flash, jsonify,
)
from datetime import datetime
import copy
import os
import random

from dotenv import load_dotenv
load_dotenv()  # loads .env into os.environ; no-op if file is absent

from mock_data import MOCK_REQUESTS, get_approval_tier, tier_to_status, mask_account, mask_routing
import db
import sharepoint
import auth

app = Flask(__name__)
# [AUTH] TODO: Rotate this key before any real deployment
app.secret_key = os.environ.get("SECRET_KEY") or "etxn-development-key-change-before-deployment"


def database_enabled():
    """Return whether the SQL data source is enabled for this environment."""
    return os.environ.get("DB_ENABLED", "true").lower() == "true"


def mock_data_enabled():
    """Return whether mock records are explicitly enabled for local development."""
    return os.environ.get("MOCK_DATA_ENABLED", "false").lower() == "true"


def sharepoint_enabled():
    """Return whether the SharePoint attachment library is enabled for this environment."""
    return os.environ.get("SHAREPOINT_ENABLED", "true").lower() == "true"


# Maps the three named intake upload fields to their library section/document type
_INTAKE_ATTACHMENT_FIELDS = {
    "validation_evidence":   ("file_validation_evidence",   sharepoint.SECTION_VERIFICATION,      sharepoint.DOC_TYPE_VALIDATION_EVIDENCE,   False),
    "wire_ach_instructions": ("file_wire_ach_instructions", sharepoint.SECTION_RECEIVING_BANKING,  sharepoint.DOC_TYPE_WIRE_ACH_INSTRUCTIONS, True),
    "payment_support":       ("file_payment_support",       sharepoint.SECTION_TRANSACTION,        sharepoint.DOC_TYPE_PAYMENT_SUPPORT,       True),
}


def _upload_intake_attachments(request_id):
    """Upload the Section B/D/E intake files to the SharePoint library, if enabled."""
    if not sharepoint_enabled():
        return
    role = ROLE_DISPLAY.get(session.get("role"), "Submitter")
    for field_name, section, doc_type, is_required in _INTAKE_ATTACHMENT_FIELDS.values():
        file_storage = request.files.get(field_name)
        if not file_storage or not file_storage.filename:
            continue
        try:
            sharepoint.upload_attachment(
                request_id, file_storage,
                section=section, doc_type=doc_type,
                uploaded_by_role=role, is_required=is_required,
            )
        except Exception:
            app.logger.exception("SharePoint upload failed for %s on %s", field_name, request_id)

# Display names used in timeline / comment author fields
ROLE_DISPLAY = {
    "submitter":           "Submitter",
    "sam":                 "Sr. Accounting Manager",
    "controller":          "Controller",
    "vp":                  "Vice President",
    "cfo":                 "CFO",
    "treasury":            "Treasury Manager",
    "business_admin":      "Business Administrator",
    "it_admin":            "IT Administrator",
    "treasury_bank_admin": "Treasury Backup (Bank Maintenance)",
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
    identity = auth.current_identity()
    return {
        "is_prototype": False,
        "current_year": datetime.now().year,
        "current_role": session.get("role"),
        "auth_source":  identity["source"],
        "can_switch_role": identity["source"] == "dev" or len(identity["roles"]) > 1,
    }


# [AUTH] Development role gate — replace with Azure AD / MSAL authentication
ROLE_FREE_ENDPOINTS = {"role_select", "switch_role", "static"}

@app.before_request
def require_role():
    if request.endpoint in ROLE_FREE_ENDPOINTS or request.endpoint is None:
        return None

    identity = auth.current_identity()

    if identity["source"] == "none":
        return (
            "Access denied: no authenticated identity was found and the local "
            "developer login is disabled (DEV_LOGIN_ENABLED=false).",
            403,
        )

    if identity["source"] == "easy_auth":
        if not identity["roles"]:
            return (
                "Your account is signed in but has not been assigned an "
                "E-Transaction application role. Contact an administrator.",
                403,
            )
        if session.get("role") not in identity["roles"]:
            if len(identity["roles"]) == 1:
                session["role"] = identity["roles"][0]
            else:
                return redirect(url_for("role_select"))
        return None

    # source == "dev" — unchanged local role-switcher behavior
    if not session.get("role"):
        return redirect(url_for("role_select"))
    return None


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
    """
    Role picker. In production (Easy Auth), this only offers the roles Azure has
    actually granted the signed-in user. Locally (DEV_LOGIN_ENABLED=true), it
    offers every role for unrestricted development testing. [AUTH]
    """
    identity = auth.current_identity()
    available_roles = identity["roles"] if identity["source"] == "easy_auth" else list(ROLE_DISPLAY.keys())

    if request.method == "POST":
        role = request.form.get("role", "")
        if role in available_roles:
            session["role"] = role
        return redirect(url_for("dashboard"))
    if session.get("role") in available_roles and session.get("role"):
        return redirect(url_for("dashboard"))
    return render_template("role_select.html", available_roles=available_roles)


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
    _db_on = database_enabled()
    users         = []
    bank_accounts = []
    if _db_on:
        try:
            users         = db.get_user_list()
            bank_accounts = db.get_bank_accounts()
        except Exception:
            pass
    return render_template("intake.html", users=users, bank_accounts=bank_accounts)


@app.route("/intake/submit", methods=["POST"])
def intake_submit():
    """
    Handle intake form submission.

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
        "verbal_confirm_datetime":   frm.get("verbal_confirm_datetime", "").replace("T", " "),
        "avs_score":                 frm.get("avs_score", ""),
        "external_source":           frm.get("external_source") == "on",
        "internal_doc_not_used":     frm.get("internal_doc_not_used") == "on",
        # Attachments currently record filenames; file persistence remains under development.
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

    _db_on = database_enabled()
    if _db_on:
        try:
            _vdt_raw = frm.get("verbal_confirm_datetime", "")
            _vdt = (_vdt_raw.replace("T", " ") + ":00") if _vdt_raw else ""
            db_data = {
                "prepared_by_key":  int(frm.get("prepared_by_key", 0)),
                "approver_key":     int(frm.get("approver_key", 0)),
                "controller_key":   int(frm.get("controller_key", 0)),
                "bank_account_key": int(frm.get("bank_account_key", 0)),
                "request_type":          frm.get("request_type", ""),
                "property_dept":         frm.get("property_dept", ""),
                "property_code":         frm.get("entity_id", ""),
                "treasury_service_date": frm.get("treasury_service_date", ""),
                "prepared_date":         frm.get("prepared_date", ""),
                "amount":                amount,
                "currency":              frm.get("currency", "USD"),
                "payment_purpose":       frm.get("payment_purpose", ""),
                "approval_tier":         approval_tier,
                "status":                tier_to_status(approval_tier),
                "urgent":                frm.get("urgent") == "yes",
                "urgency_reason":        frm.get("urgency_reason", ""),
                "instructions_previously_used": frm.get("instructions_previously_used") == "yes",
                "last_used_date":        frm.get("last_used_date", ""),
                "verbal_confirmed":      frm.get("verbal_confirmed") == "on",
                "verbal_known_contact":  frm.get("verbal_confirmed_with_known") == "on",
                "verbal_requester":      frm.get("verbal_confirmed_with_requester") == "on",
                "verbal_contact_name":   frm.get("verbal_contact_name", ""),
                "verbal_confirm_datetime": _vdt,
                "avs_score":             frm.get("avs_score", ""),
                "external_source":       frm.get("external_source") == "on",
                "internal_doc_not_used": frm.get("internal_doc_not_used") == "on",
                "recv_payee_name":       frm.get("recv_payee_name", ""),
                "recv_bank_name":        frm.get("recv_bank_name", ""),
                "recv_account_name":     frm.get("recv_account_name", ""),
                "recv_account_number":   frm.get("recv_account_number", ""),
                "recv_routing_number":   frm.get("recv_routing_number", ""),
                "recv_bank_address":     frm.get("recv_bank_address", ""),
                "recv_contact_name":     frm.get("recv_contact_name", ""),
                "recv_contact_email":    frm.get("recv_contact_email", ""),
                "recv_contact_phone":    frm.get("recv_contact_phone", ""),
            }
            request_id = db.insert_transaction(db_data)
            _upload_intake_attachments(request_id)
            return redirect(url_for("confirmation", request_id=request_id))
        except Exception as e:
            flash(f"Database error — submission saved locally only. ({e})", "danger")

    submitted = session.get("submitted_requests", [])
    submitted.append(record)
    session["submitted_requests"] = submitted
    _upload_intake_attachments(request_id)

    return redirect(url_for("confirmation", request_id=request_id))


@app.route("/confirmation/<request_id>")
def confirmation(request_id):
    """Post-submission confirmation screen."""
    submitted = session.get("submitted_requests", [])
    record = next((r for r in submitted if r["request_id"] == request_id), None)
    if not record:
        record = next((r for r in MOCK_REQUESTS if r["request_id"] == request_id), None)
    _db_on = database_enabled()
    if record is None and _db_on:
        try:
            record = db.get_request_detail(request_id)
        except Exception:
            pass
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

    _db_on = database_enabled()
    try:
        db_records = db.get_dashboard_records() if _db_on else []
    except Exception:
        app.logger.exception("Unable to load dashboard records from the database")
        db_records = []

    if mock_data_enabled():
        db_records = list(MOCK_REQUESTS) + db_records

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
    _db_on = database_enabled()
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

    if sharepoint_enabled():
        try:
            sp_items = sharepoint.list_attachments(request_id)
        except Exception:
            app.logger.exception("Unable to load SharePoint attachments for %s", request_id)
            sp_items = []

        attachments       = dict(display.get("attachments") or {})
        attachment_urls   = {}
        extra_attachments = list(display.get("extra_attachments") or [])

        for item in sp_items:
            key = sharepoint.DOC_TYPE_TO_ATTACHMENT_KEY.get(item["doc_type"])
            if key:
                attachments[key]     = item["filename"]
                attachment_urls[key] = item["web_url"]
            else:
                extra_attachments.append({
                    "filename":    item["filename"],
                    "description": item.get("description", ""),
                    "uploaded_by": item.get("uploaded_by_role", ""),
                    "date":        item.get("uploaded_date", "")[:10],
                    "web_url":     item.get("web_url", ""),
                })

        display["attachments"]       = attachments
        display["attachment_urls"]   = attachment_urls
        display["extra_attachments"] = extra_attachments

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
            "Status changes for mock records are not persisted unless mock data is explicitly enabled.",
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
    uploader = ROLE_DISPLAY.get(role, role.capitalize())
    today    = datetime.now().strftime("%Y-%m-%d")

    if sharepoint_enabled():
        for file_storage in uploaded_files:
            if not file_storage.filename:
                continue
            try:
                sharepoint.upload_attachment(
                    request_id, file_storage,
                    section=sharepoint.SECTION_ADDITIONAL, doc_type=sharepoint.DOC_TYPE_OTHER,
                    uploaded_by_role=uploader, description=description,
                )
            except Exception:
                app.logger.exception("SharePoint upload failed for extra_files on %s", request_id)
                flash("One or more files could not be uploaded to SharePoint.", "danger")

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
            "Attachments cannot be persisted for mock records unless mock data is explicitly enabled.",
            "info",
        )

    return redirect(url_for("request_detail", request_id=request_id))


# Temporary diagnostic endpoint — reuses the existing business_admin/it_admin role
# codes as the authorization gate; no separate admin system is introduced.
# [STORAGE] Remove once Fabric SQL connectivity has been verified in Azure.
@app.route("/admin/database-test")
def admin_database_test():
    if session.get("role") not in ("business_admin", "it_admin"):
        return jsonify({"status": "error", "message": "Not authorized."}), 403
    try:
        db.test_connection()
        return jsonify({
            "status": "success",
            "database": "connected",
            "table": "etransactions.ETransaction",
        })
    except Exception:
        app.logger.exception("Admin database connectivity test failed")
        return jsonify({"status": "error", "message": "Database connection failed."}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5000)
