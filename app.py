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
    redirect, url_for, session, flash, jsonify, abort, Response,
)
from datetime import datetime
import copy
import os
import random

from dotenv import load_dotenv
load_dotenv()  # loads .env into os.environ; no-op if file is absent

from mock_data import MOCK_REQUESTS, get_approval_tier, tier_to_status
import db
import sharepoint
import auth
import workflow
import banking_security
import authorization
import export as export_module
from flask_wtf import CSRFProtect
from flask_wtf.csrf import CSRFError

app = Flask(__name__)


def resolve_secret_key(env_value, dev_mode_active):
    """
    Batch 10 Part 5: production (Easy Auth expected, DEV_LOGIN_ENABLED=false)
    must NEVER start with a known/hardcoded secret — raises instead of
    silently running with a guessable session-signing key. The dev-only
    fallback remains available strictly when the local role-switcher bypass
    is explicitly on. Pure function (no Flask/env access) for testability.
    """
    if env_value:
        return env_value
    if dev_mode_active:
        return "etxn-development-key-change-before-deployment"  # DEV ONLY
    raise RuntimeError(
        "SECRET_KEY environment variable is required when DEV_LOGIN_ENABLED=false "
        "(production posture). Refusing to start with no/guessable session key."
    )


app.secret_key = resolve_secret_key(os.environ.get("SECRET_KEY"), auth.dev_login_enabled())

# Batch 10 Part 26: session cookie hardening. SECURE is conditional on production
# posture only — forcing it on in local dev (plain http://) would silently break
# the session cookie/login entirely.
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = not auth.dev_login_enabled()

# Batch 10 Part 6: CSRF protection on every state-changing (POST/PUT/PATCH/
# DELETE) route. Relaxed only under the same local dev role-switcher bypass
# used everywhere else in this app (DEV_LOGIN_ENABLED=true — a single local
# developer with no real session-forgery threat model, and where the
# automated unit test suite runs) — always enforced whenever Easy Auth is the
# expected identity source (production/UAT).
app.config["WTF_CSRF_ENABLED"] = not auth.dev_login_enabled()
csrf = CSRFProtect(app)

app.jinja_env.globals["workflow"] = workflow  # lets templates reference workflow.STATUS_* directly


@app.after_request
def _set_security_headers(response):
    """Minimal, safe security headers — no CSP/frame-ancestors change here (no
    iframe usage found anywhere in this app; adding those without a full CDN/
    embedding audit risks silently breaking the UI — see Batch 10 Part 26)."""
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    return response


@app.errorhandler(CSRFError)
def _csrf_error_handler(exc):
    app.logger.warning("CSRF validation failed: %s", exc.description)
    flash("Your session expired or the form was resubmitted unsafely. Please try again.", "warning")
    return redirect(request.referrer or url_for("dashboard")), 400


def database_enabled():
    """Return whether the SQL data source is enabled for this environment."""
    return os.environ.get("DB_ENABLED", "true").lower() == "true"


def mock_data_enabled():
    """Return whether mock records are explicitly enabled for local development."""
    return os.environ.get("MOCK_DATA_ENABLED", "false").lower() == "true"


def sharepoint_enabled():
    """Return whether the SharePoint attachment library is enabled for this environment."""
    return os.environ.get("SHAREPOINT_ENABLED", "true").lower() == "true"


def dev_fallback_allowed():
    """
    Batch 10 Part 3/4 production-safety gate: the legacy mock/session-record
    fallback (MOCK_REQUESTS, session["submitted_requests"]) may only ever be
    read or written when the local developer role-switcher bypass itself is
    enabled (DEV_LOGIN_ENABLED=true) — the app's one established dev/production
    posture signal (see auth.py). In a real Easy Auth production deployment
    this is always False, so a missing/mis-set DB_ENABLED or MOCK_DATA_ENABLED
    value can never silently produce a fake "successful" session-only
    transaction or expose static demo records to every signed-in user.
    """
    return auth.dev_login_enabled()


def current_app_user():
    """
    Resolve the signed-in identity to a full AppUser record
    ({"user_key", "display_name", "email"}), or None if it cannot be resolved.

    Production (Easy Auth): real Entra Object ID -> AppUser.Entra_Object_ID lookup.
    Local dev (DEV_LOGIN_ENABLED=true, no Easy Auth headers): the developer's
    explicitly selected "Acting as" AppUser (session["dev_user_key"], set on the
    role-select screen — see role_select()) — NOT a general submit-on-behalf-of
    feature, and never sourced from the transaction form itself (Batch 7 Part 5).
    Returns None (never a silent fallback) if no identity can be resolved, so
    callers that need an authoritative Prepared By (e.g. intake) can fail safely.
    """
    if not database_enabled():
        return None
    identity = auth.current_identity()
    if identity["source"] == "easy_auth":
        try:
            return db.get_app_user_by_entra_object_id(identity["user_id"])
        except Exception:
            app.logger.exception("Unable to resolve signed-in identity to an AppUser")
            return None
    if identity["source"] == "dev":
        dev_user_key = session.get("dev_user_key")
        if not dev_user_key:
            return None
        try:
            return db.get_app_user_by_key(dev_user_key)
        except Exception:
            app.logger.exception("Unable to resolve dev-selected AppUser %s", dev_user_key)
            return None
    return None


def current_app_user_key():
    """
    Resolve the signed-in identity to an etransactions.AppUser.User_Key, or None.
    See current_app_user() — this is the key-only convenience form used by the
    many existing call sites that only need the ID (workflow actions, reveal
    endpoints, dashboard scoping, etc.).
    """
    user = current_app_user()
    return user["user_key"] if user else None


# ── Multi-role authorization (seamless — see
#    e_transaction_multi_role_authorization_refactor.md) ────────────────────
#
# A signed-in user's functional roles are a SET, not one active/selected mode.
# Production (Easy Auth): the complete set of Entra App Role claims,
# re-derived from the request every time — never cached in session, since
# Entra remains the sole authority and a stale cached set could grant a
# capability Entra no longer does. Local dev: the "Acting As" AppUser's
# AppUserRole-derived role set (simulating what Entra would grant in
# production), UNLESS the developer has explicitly chosen the DEV-ONLY
# single-role isolation override on role_select.html (session["role"]) to
# test one role in isolation — that override never applies under Easy Auth.
def current_roles() -> set:
    """Return the complete set of recognized application role codes for the
    signed-in identity. Never a single active role."""
    identity = auth.current_identity()
    if identity["source"] == "easy_auth":
        return set(identity["roles"])
    if identity["source"] == "dev":
        override = session.get("role")
        if override:
            return {override}
        dev_user_key = session.get("dev_user_key")
        if not dev_user_key:
            return set()
        try:
            return set(db.get_app_user_role_codes(dev_user_key))
        except Exception:
            app.logger.exception("Unable to resolve dev-mode role set for Acting-As user %s", dev_user_key)
            return set()
    return set()


def has_role(role_code: str) -> bool:
    return role_code in current_roles()


def has_any_role(*role_codes: str) -> bool:
    return bool(current_roles() & set(role_codes))


def has_all_roles(*role_codes: str) -> bool:
    return set(role_codes) <= current_roles()


def current_roles_display() -> str:
    """Joined display label for all current roles — used for audit-trail
    text fields (WorkflowEvent.Actor_Role, SharePoint UploadedByRole) that
    expect one string; never a single arbitrarily-picked role (Part 26)."""
    roles = sorted(current_roles())
    if not roles:
        return "Unknown"
    return ", ".join(ROLE_DISPLAY.get(r, r) for r in roles)


# Maps the three named intake upload fields to their library section/document type
_INTAKE_ATTACHMENT_FIELDS = {
    "validation_evidence":   ("file_validation_evidence",   sharepoint.SECTION_VERIFICATION,      sharepoint.DOC_TYPE_VALIDATION_EVIDENCE,   False),
    "wire_ach_instructions": ("file_wire_ach_instructions", sharepoint.SECTION_RECEIVING_BANKING,  sharepoint.DOC_TYPE_WIRE_ACH_INSTRUCTIONS, True),
    "payment_support":       ("file_payment_support",       sharepoint.SECTION_TRANSACTION,        sharepoint.DOC_TYPE_PAYMENT_SUPPORT,       True),
}

# Batch 5: workflow actions that require an evidence file (uploaded to SharePoint
# under the same request-id folder as every other attachment) before the SQL
# workflow transition may proceed — see _handle_sql_workflow_action().
_EVIDENCE_REQUIRED_ACTIONS = {
    workflow.ACTION_TREASURY_INITIATED: (sharepoint.DOC_TYPE_TREASURY_INITIATION_EVIDENCE, "Treasury initiation evidence"),
    workflow.ACTION_TREASURY_RELEASED:  (sharepoint.DOC_TYPE_TREASURY_RELEASE_EVIDENCE,    "Treasury release evidence"),
    workflow.ACTION_BANK_RELEASE:       (sharepoint.DOC_TYPE_BANK_RELEASE_EVIDENCE,        "Final bank release evidence"),
}


def _upload_intake_attachments(request_id):
    """Upload the Section B/D/E intake files to the SharePoint library, if enabled."""
    if not sharepoint_enabled():
        return
    role = current_roles_display()
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


def _upload_required_intake_attachments(request_id):
    """
    Upload the required intake attachments to SharePoint under the given
    (pre-reserved, not-yet-committed-to-SQL) request_id, raising on the first
    failure — unlike _upload_intake_attachments(), which logs and continues.

    Called BEFORE db.insert_transaction() for SQL-backed submissions so a
    required-document upload failure never leaves a "successfully submitted"
    transaction behind with missing evidence. No-op when SharePoint is
    disabled (matches existing dev-mode graceful degradation elsewhere).
    """
    if not sharepoint_enabled():
        return
    role = current_roles_display()
    for field_name, section, doc_type, is_required in _INTAKE_ATTACHMENT_FIELDS.values():
        file_storage = request.files.get(field_name)
        if not file_storage or not file_storage.filename:
            continue
        sharepoint.upload_attachment(
            request_id, file_storage,
            section=section, doc_type=doc_type,
            uploaded_by_role=role, is_required=is_required,
        )

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

BANK_ACCOUNT_VIEW_ROLES = {"treasury", "treasury_bank_admin", "vp", "cfo"}
BANK_ACCOUNT_EDIT_ROLES = {"treasury", "treasury_bank_admin"}


def can_view_bank_accounts():
    return has_any_role(*BANK_ACCOUNT_VIEW_ROLES)


def can_edit_bank_accounts():
    return has_any_role(*BANK_ACCOUNT_EDIT_ROLES)


def require_bank_account_view():
    if not can_view_bank_accounts():
        abort(403)


def _bank_account_form_data():
    # Terminology note (do not confuse these three distinct concepts):
    #   BusinessEntity.Classification   -> Property / Corporate (which entity owns this account)
    #   BankAccount.AccountType         -> banking product type, e.g. Checking / Savings
    #   BankAccount.AccountClassification -> accounting/business category, e.g. Operating,
    #                                        Security Deposit, Payroll — labeled "Account
    #                                        Category" in the UI. Physical column name is
    #                                        kept as AccountClassification to avoid an
    #                                        unnecessary/risky column rename.
    data = {
        "entity_key": request.form.get("entity_key", "").strip(),
        "bank_name": request.form.get("bank_name", "").strip(),
        "account_name_id": request.form.get("account_name_id", "").strip(),
        "account_title": request.form.get("account_title", "").strip(),
        "account_title_modifier": request.form.get("account_title_modifier", "").strip(),
        "system_account_name": request.form.get("system_account_name", "").strip(),
        "account_number": request.form.get("account_number", "").strip(),
        "routing_number": request.form.get("routing_number", "").strip(),
        "transit_number_canada": request.form.get("transit_number_canada", "").strip(),
        "institution_number_canada": request.form.get("institution_number_canada", "").strip(),
        "gl_account_number": request.form.get("gl_account_number", "").strip(),
        "gl_account_name": request.form.get("gl_account_name", "").strip(),
        "tax_id_number": request.form.get("tax_id_number", "").strip(),
        "address": request.form.get("address", "").strip(),
        "phone_number": request.form.get("phone_number", "").strip(),
        "account_type": request.form.get("account_type", "").strip(),
        "account_classification": request.form.get("account_classification", "").strip(),
        "status": request.form.get("status", "").strip(),
        "date_opened": request.form.get("date_opened", "").strip(),
        "date_closed": request.form.get("date_closed", "").strip(),
        "bank_contact_name": request.form.get("bank_contact_name", "").strip(),
        "notes": request.form.get("notes", "").strip(),
    }
    for flag in db.BANK_ACCOUNT_SERVICE_FLAGS:
        data[flag] = request.form.get(flag) == "on"
    return data


def _validate_bank_account_form(data, *, is_new):
    errors = []
    if not data.get("entity_key"):
        errors.append("Associated Property or Corporate Entity is required.")
    if not data.get("bank_name"):
        errors.append("Bank / Financial Institution is required.")
    if is_new and not data.get("account_number"):
        errors.append("Account Number is required for new bank accounts.")
    if data.get("status") not in ("Open", "Closed"):
        errors.append("Status must be Open or Closed.")
    if is_new and not data.get("date_opened"):
        errors.append("Date Opened is required for new bank accounts.")
    if data.get("status") == "Closed" and not data.get("date_closed"):
        errors.append("Date Closed is required when closing a bank account.")

    # Reject a crafted/careless submission that copies a displayed masked
    # placeholder (e.g. "XXXX-XXXX-1234", "xxxx") back in as if it were a real
    # replacement value — a blank field is the correct way to leave a sensitive
    # value unchanged (see update_bank_account()).
    for field, label in (
        ("account_number", "Account Number"), ("routing_number", "Routing Number"),
        ("transit_number_canada", "Transit Number"), ("institution_number_canada", "Institution Number"),
        ("tax_id_number", "Tax ID Number"),
    ):
        value = data.get(field)
        if value and banking_security.is_mask_placeholder(value):
            errors.append(
                f"{label} looks like a masked placeholder, not a real value. "
                f"Leave it blank to keep the existing value, or enter the actual new {label.lower()}."
            )
    return errors


def _validate_intake_submission(frm, *, files_present: dict, bank_account_status, has_stored_receiving_bank_instruction=False):
    """
    Authoritative SERVER-SIDE validation for /intake/submit — mirrors the
    client-side checks already in intake.html so a direct POST (bypassing
    JavaScript) cannot skip required fields, required attachments, the AVS/
    verification policy, or an invalid/closed originating bank account.

    Pure function — no DB/network calls. `bank_account_status` is the raw
    BankAccount.Status value already looked up by the caller (None if the
    supplied key doesn't exist), and `files_present` is a dict of the three
    named required attachment fields to booleans (was a real file selected).

    Returns a list of user-facing error strings; empty means the submission
    may proceed.
    """
    errors = []

    def require(field, label):
        if not (frm.get(field) or "").strip():
            errors.append(f"{label} is required.")

    # A. Basic required fields
    # prepared_by_key/prepared_date are NOT validated here — they are resolved
    # entirely server-side (current_app_user() / datetime.now()) and never
    # trusted from the POST (Batch 7 Part 1/2/6/17); the caller rejects the
    # submission earlier if the authenticated identity can't be resolved.
    require("request_type", "Request Type")
    require("treasury_service_date", "Requested Treasury Service Date")
    require("property_dept", "Property / Department")
    require("approver_key", "Approver")
    require("controller_key", "Controller")
    require("payment_purpose", "Payment Purpose / Description")
    require("currency", "Currency")

    amount_raw = (frm.get("amount") or "").replace(",", "").strip()
    try:
        if not amount_raw or float(amount_raw) <= 0:
            errors.append("Amount is required and must be greater than zero.")
    except ValueError:
        errors.append("Amount must be a valid number.")

    # B. Urgent
    if frm.get("urgent") == "yes" and not (frm.get("urgency_reason") or "").strip():
        errors.append("Urgency Reason is required when the request is marked urgent.")

    # C. Originating bank account — never trust the hidden client field alone
    bank_account_key = (frm.get("bank_account_key") or "").strip()
    if not bank_account_key:
        errors.append("Originating Bank Account is required — search by last 4 digits and select an account.")
    elif bank_account_status is None:
        errors.append("The selected Originating Bank Account could not be found.")
    elif bank_account_status != "Open":
        errors.append("The selected Originating Bank Account is closed and cannot be used.")

    # D. Receiving / beneficiary bank information
    require("recv_payee_name", "Recipient / Payee Name")
    require("recv_bank_name", "Receiving Bank Name")
    require("recv_account_name", "Receiving Account Name")
    # Batch 8: a blank account/routing number is only acceptable when finalizing
    # an existing Draft that already has a real stored value — the intake form
    # never re-displays the raw DDM-masked value as editable (Batch 3), so
    # "blank" there means "keep the stored value", not "no value at all". A
    # brand-new submission (has_stored_receiving_bank_instruction=False) still
    # requires both fields exactly as before.
    if not has_stored_receiving_bank_instruction:
        require("recv_account_number", "Receiving Account Number")
        require("recv_routing_number", "Receiving Routing Number")
    if frm.get("request_type") == "Wire" and not (frm.get("recv_bank_address") or "").strip():
        errors.append("Receiving Bank / Beneficiary Address is required for Wire transactions.")

    # E/F. Required attachments (mirrors intake.html validateAttachments())
    if not files_present.get("validation_evidence"):
        errors.append("Validation Evidence attachment is required.")
    if not files_present.get("wire_ach_instructions"):
        errors.append("External ACH/Wire Instructions attachment is required.")
    if not files_present.get("payment_support"):
        errors.append("Payment Support attachment is required.")

    # G. AVS score range. Note: the UI has a single "Validation Evidence" file
    # that serves as both the AVS screenshot AND the alternative-verification
    # document — there is no separate alternative-verification field to check,
    # so the AVS>=90/below-90/unavailable branches all reduce to the same E/F
    # attachment-presence check above; only the numeric range is AVS-specific.
    avs_raw = (frm.get("avs_score") or "").strip()
    if avs_raw:
        try:
            avs_score = int(avs_raw)
        except ValueError:
            errors.append("AVS Score must be a whole number between 0 and 100.")
        else:
            if avs_score < 0 or avs_score > 100:
                errors.append("AVS Score must be between 0 and 100.")

    # H. New / unverified instructions — verbal confirmation with a KNOWN
    # CONTACT specifically (not "confirmed with the requester") is required.
    if frm.get("instructions_previously_used") != "yes":
        if frm.get("verbal_confirmed_with_known") != "on":
            errors.append("Verbal confirmation with a known contact is required for new/unverified banking instructions.")
        if not (frm.get("verbal_contact_name") or "").strip():
            errors.append("Verbal Confirmation Contact Name is required for new/unverified banking instructions.")
        if not (frm.get("verbal_confirm_datetime") or "").strip():
            errors.append("Verbal Confirmation Date/Time is required for new/unverified banking instructions.")
    else:
        if not (frm.get("last_used_date") or "").strip():
            errors.append("Last Date Used is required when banking instructions were previously used.")

    return errors


_DRAFT_ALLOWED_REQUEST_TYPES = ('Wire', 'ACH', 'ACH Pull', 'Intra Bank Transfer', 'EFT')


def _validate_draft_submission(frm, *, is_update=False):
    """
    Light DATA-VALIDITY-only validation for Save Draft (Batch 8) — deliberately
    NOT _validate_intake_submission() (Batch 1's full business-completeness
    validator, reused unchanged and unweakened at final Submit). A draft may
    omit business-completeness fields (attachments, AVS, urgency reason, wire
    address, prior-use verification, currency, property/department) — but
    whatever the live ETransaction/Beneficiary/BeneficiaryBankInstruction schema
    requires NOT NULL with no safe non-fake default (Part 1/7) must be present
    and structurally valid; supplied values must not be malformed or unsafe.

    `is_update=True` (editing an existing Draft) allows a blank
    recv_account_number — a blank value there means "keep the currently stored
    value" (Batch 3 blank-preserves-existing pattern, Part 10), not "no value
    at all", since a real value is already persisted for that Draft.
    """
    errors = []

    def require(field, label):
        if not (frm.get(field) or "").strip():
            errors.append(f"{label} is required to save a draft.")

    require("request_type", "Request Type")
    require("treasury_service_date", "Requested Treasury Service Date")
    require("approver_key", "Approver")
    require("controller_key", "Controller")
    require("bank_account_key", "Originating Bank Account")
    require("recv_payee_name", "Recipient / Payee Name")
    require("recv_bank_name", "Receiving Bank Name")
    if not is_update:
        require("recv_account_number", "Receiving Account Number")

    request_type = (frm.get("request_type") or "").strip()
    if request_type and request_type not in _DRAFT_ALLOWED_REQUEST_TYPES:
        errors.append("Unsupported Request Type.")

    amount_raw = (frm.get("amount") or "").replace(",", "").strip()
    if not amount_raw:
        errors.append("Amount is required to save a draft.")
    else:
        try:
            if float(amount_raw) <= 0:
                errors.append("Amount must be greater than zero.")
        except ValueError:
            errors.append("Amount must be a valid number.")

    service_date = (frm.get("treasury_service_date") or "").strip()
    if service_date:
        try:
            datetime.strptime(service_date, "%Y-%m-%d")
        except ValueError:
            errors.append("Requested Treasury Service Date must be a valid date.")

    for field, label in (("recv_account_number", "Receiving Account Number"),
                          ("recv_routing_number", "Receiving Routing Number")):
        value = (frm.get(field) or "").strip()
        if value and banking_security.is_mask_placeholder(value):
            errors.append(f"{label} looks like a masked placeholder, not a real value.")

    return errors

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
    # Current workflow vocabulary (workflow.py) — kept alongside the legacy
    # strings above so pre-existing mock/session demo records still render.
    "Pending Approver":             "bg-info text-dark",
    "Pending Controller":           "bg-warning text-dark",
    "Pending VP":                   "badge-orange",
    "Pending CFO":                  "bg-danger",
    "More Information Requested":   "bg-warning text-dark",
    "Ready for Treasury":           "bg-primary",
    "Treasury Initiated":           "badge-purple",
    "Awaiting Bank Release":        "badge-purple",
    "Treasury Released":            "badge-teal",
}

# Groups legacy and current status strings that represent the same workflow
# stage, so dashboard status-filter links/bookmarks work regardless of which
# vocabulary a given record actually stores.
_STATUS_EQUIVALENTS = {
    "Pending SAM Approval":        {"Pending SAM Approval", workflow.STATUS_PENDING_APPROVER},
    workflow.STATUS_PENDING_APPROVER:   {"Pending SAM Approval", workflow.STATUS_PENDING_APPROVER},
    "Pending Controller Approval": {"Pending Controller Approval", workflow.STATUS_PENDING_CONTROLLER},
    workflow.STATUS_PENDING_CONTROLLER: {"Pending Controller Approval", workflow.STATUS_PENDING_CONTROLLER},
    "Pending VP Approval":         {"Pending VP Approval", workflow.STATUS_PENDING_VP},
    workflow.STATUS_PENDING_VP:         {"Pending VP Approval", workflow.STATUS_PENDING_VP},
    "Pending CFO Approval":        {"Pending CFO Approval", workflow.STATUS_PENDING_CFO},
    workflow.STATUS_PENDING_CFO:        {"Pending CFO Approval", workflow.STATUS_PENDING_CFO},
    "Pending Treasury Review":     {"Pending Treasury Review", workflow.STATUS_READY_FOR_TREASURY},
    workflow.STATUS_READY_FOR_TREASURY: {"Pending Treasury Review", workflow.STATUS_READY_FOR_TREASURY},
    "Pending Release":             {"Pending Release", workflow.STATUS_AWAITING_RELEASE},
    workflow.STATUS_AWAITING_RELEASE:   {"Pending Release", workflow.STATUS_AWAITING_RELEASE},
    "Needs More Information":      {"Needs More Information", workflow.STATUS_MORE_INFO},
    workflow.STATUS_MORE_INFO:          {"Needs More Information", workflow.STATUS_MORE_INFO},
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


@app.template_filter("any_of")
def any_of(roles, *codes):
    """Jinja helper for role-SET-aware template conditionals: {{ current_roles | any_of('vp','cfo') }}
    — True if the given iterable of role codes intersects `codes` (multi-role authorization refactor,
    Part 13: templates must evaluate the full role set, never a single value)."""
    return bool(set(roles or []) & set(codes))


@app.context_processor
def inject_globals():
    identity = auth.current_identity()
    roles = current_roles()
    return {
        "is_prototype": False,
        "current_year": datetime.now().year,
        "current_roles": sorted(roles),
        "current_roles_display": current_roles_display(),
        "auth_source":  identity["source"],
        "can_switch_role": identity["source"] == "dev",
        "can_view_bank_accounts": can_view_bank_accounts(),
        "can_edit_bank_accounts": can_edit_bank_accounts(),
    }


# [AUTH] Development role gate — replace with Azure AD / MSAL authentication
ROLE_FREE_ENDPOINTS = {"role_select", "switch_role", "dev_set_acting_as_user", "static"}

@app.before_request
def require_role():
    """
    Seamless multi-role gate (e_transaction_multi_role_authorization_refactor.md
    Part 30) — requires an authenticated identity with at least one recognized
    role, then lets the request through with its FULL role set intact. Never
    reduces a multi-role user to a single active role, and never redirects a
    production (Easy Auth) user to a role-selection page.
    """
    if request.endpoint in ROLE_FREE_ENDPOINTS or request.endpoint is None:
        return None

    identity = auth.current_identity()

    if identity["source"] == "none":
        return (
            "Access denied: no authenticated identity was found and the local "
            "developer login is disabled (DEV_LOGIN_ENABLED=false).",
            403,
        )

    if identity["source"] == "easy_auth" and not identity["roles"]:
        return (
            "Your account is signed in but has not been assigned an "
            "E-Transaction application role. Contact an administrator.",
            403,
        )

    # dev source: no recognized role yet (no isolation override, no Acting-As
    # user set, or Acting-As user has no AppUserRole rows) — send to the
    # DEV-ONLY picker/Acting-As screen. easy_auth is never sent here (already
    # validated non-empty above).
    if not current_roles():
        return redirect(url_for("role_select"))
    return None


# ─────────────────────────────────────────────────────────────
#  Routes
# ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if not current_roles():
        return redirect(url_for("role_select"))
    return redirect(url_for("dashboard"))


@app.route("/role-select", methods=["GET", "POST"])
def role_select():
    """
    DEV-ONLY: single-role isolation-test picker + "Acting As" identity selector.
    Production (Easy Auth) never uses this page for authorization (multi-role
    authorization refactor, Part 27/28) — a signed-in Easy Auth user is
    redirected straight to the dashboard with their full role set already
    resolved from Entra App Role claims; visiting this route directly does
    nothing for them. [AUTH]
    """
    identity = auth.current_identity()
    if identity["source"] == "easy_auth":
        return redirect(url_for("dashboard"))

    available_roles = list(ROLE_DISPLAY.keys())

    if request.method == "POST":
        role = request.form.get("role", "")
        if role in available_roles:
            session["role"] = role
        return redirect(url_for("dashboard"))
    if current_roles():
        return redirect(url_for("dashboard"))

    # Local-dev-only "Acting as" AppUser selector (Batch 7 Part 5) — lets
    # current_app_user()/current_app_user_key() resolve a real identity in dev
    # mode too, and (as of the multi-role refactor) also drives current_roles()
    # automatically from that user's AppUserRole rows, without weakening
    # production (Easy Auth is unaffected).
    dev_users = []
    if identity["source"] == "dev" and database_enabled():
        try:
            dev_users = db.get_user_list()
        except Exception:
            dev_users = []
    return render_template(
        "role_select.html", available_roles=available_roles,
        dev_mode=(identity["source"] == "dev"), dev_users=dev_users,
        dev_user_key=session.get("dev_user_key"),
    )


@app.route("/switch-role")
def switch_role():
    """DEV-ONLY: clear the single-role isolation override and return to the picker.
    No effect under Easy Auth — there is nothing to switch in production."""
    if auth.current_identity()["source"] != "dev":
        return redirect(url_for("dashboard"))
    session.pop("role", None)
    return redirect(url_for("role_select"))


@app.route("/dev/acting-as", methods=["POST"])
def dev_set_acting_as_user():
    """
    Local-development-only: set which AppUser current_app_user()/
    current_app_user_key() resolve to (e.g. Prepared By on intake), and
    (multi-role refactor) which AppUserRole-derived role set current_roles()
    resolves to by default. No effect against a real Easy Auth identity, and
    never reachable from/via the transaction form itself (Batch 7 Part 5) —
    this is NOT a submit-on-behalf-of feature.
    """
    if auth.current_identity()["source"] != "dev":
        abort(404)
    user_key_raw = request.form.get("dev_user_key", "").strip()
    if user_key_raw:
        try:
            session["dev_user_key"] = int(user_key_raw)
        except ValueError:
            session.pop("dev_user_key", None)
    else:
        session.pop("dev_user_key", None)
    # Reset any prior single-role isolation override so switching Acting-As
    # defaults to that user's FULL simulated role set, not a stale one-role pick.
    session.pop("role", None)
    return redirect(url_for("role_select"))


def _draft_files_present(request_id):
    """
    Merge files newly attached in THIS POST with attachments already persisted
    for an existing Draft's Request_ID (Batch 8 Part 11) — final submission
    validation must see previously-uploaded draft attachments, not just files
    re-selected in the same POST.
    """
    present = {
        "validation_evidence":   bool(request.files.get("file_validation_evidence") and request.files["file_validation_evidence"].filename),
        "wire_ach_instructions": bool(request.files.get("file_wire_ach_instructions") and request.files["file_wire_ach_instructions"].filename),
        "payment_support":       bool(request.files.get("file_payment_support") and request.files["file_payment_support"].filename),
    }
    if request_id and not all(present.values()) and sharepoint_enabled():
        try:
            existing = sharepoint.list_attachments(request_id)
        except Exception:
            app.logger.exception("Unable to check existing draft attachments for %s", request_id)
            existing = []
        existing_keys = {sharepoint.DOC_TYPE_TO_ATTACHMENT_KEY.get(item["doc_type"]) for item in existing}
        for key in present:
            if not present[key] and key in existing_keys:
                present[key] = True
    return present


def _handle_save_draft(frm, prepared_by_user):
    """
    Save Draft (Batch 8) — light data-validity-only validation, never the full
    Batch 1 business-completeness validator. Creates a new Draft on first save,
    or updates the same Transaction_Key on subsequent saves (never a duplicate
    row). No workflow/WorkflowEvent is started — see db.create_draft()/update_draft().
    """
    draft_transaction_key_raw = frm.get("transaction_key", "").strip()
    errors = _validate_draft_submission(frm, is_update=bool(draft_transaction_key_raw))

    bank_account_key_raw = frm.get("bank_account_key", "").strip()
    if bank_account_key_raw and not errors:
        try:
            if db.get_bank_account_status(int(bank_account_key_raw)) is None:
                errors.append("The selected Originating Bank Account could not be found.")
        except ValueError:
            errors.append("Originating Bank Account is invalid.")

    for field, label in (("approver_key", "Approver"), ("controller_key", "Controller")):
        raw = frm.get(field, "").strip()
        if raw and not errors:
            try:
                if db.get_app_user_by_key(int(raw)) is None:
                    errors.append(f"The selected {label} could not be found.")
            except ValueError:
                errors.append(f"{label} is invalid.")

    users, bank_accounts = [], []
    try:
        users         = db.get_user_list()
        bank_accounts = db.get_bank_accounts()
    except Exception:
        pass

    if errors:
        for err in errors:
            flash(err, "warning")
        return render_template("intake.html", users=users, bank_accounts=bank_accounts,
                                prepared_by_user=prepared_by_user, errors=errors)

    draft_data = {
        "prepared_by_key":  prepared_by_user["user_key"],
        "request_type":     frm.get("request_type", "").strip(),
        "treasury_service_date": frm.get("treasury_service_date", "").strip(),
        "property_dept":    frm.get("property_dept", ""),
        "property_code":    frm.get("entity_id", ""),
        "amount":           float((frm.get("amount") or "0").replace(",", "") or 0),
        "currency":         frm.get("currency", "USD"),
        "payment_purpose":  frm.get("payment_purpose", ""),
        "urgent":           frm.get("urgent") == "yes",
        "urgency_reason":   frm.get("urgency_reason", ""),
        "bank_account_key": int(bank_account_key_raw),
        "approver_key":     int(frm.get("approver_key")),
        "controller_key":   int(frm.get("controller_key")),
        "recv_payee_name":    frm.get("recv_payee_name", ""),
        "recv_contact_name":  frm.get("recv_contact_name", ""),
        "recv_contact_email": frm.get("recv_contact_email", ""),
        "recv_contact_phone": frm.get("recv_contact_phone", ""),
        "recv_bank_name":     frm.get("recv_bank_name", ""),
        "recv_account_name":  frm.get("recv_account_name", ""),
        "recv_account_number": frm.get("recv_account_number", "").strip(),
        "recv_routing_number": frm.get("recv_routing_number", "").strip(),
        "recv_bank_address":  frm.get("recv_bank_address", ""),
    }

    try:
        if draft_transaction_key_raw:
            transaction_key = int(draft_transaction_key_raw)
            db.update_draft(transaction_key, draft_data, prepared_by_user_key=prepared_by_user["user_key"])
            request_id = frm.get("request_id", "").strip()
            flash("Draft updated.", "success")
        else:
            request_id, transaction_key = db.create_draft(draft_data)
            flash("Draft saved.", "success")
        _upload_intake_attachments(request_id)  # best-effort; nothing required for a Draft
    except db.DraftNotEditableError:
        flash("This draft is no longer editable (it may have already been submitted).", "danger")
        return redirect(url_for("dashboard"))
    except Exception:
        app.logger.exception("Draft save failed")
        flash("Unable to save this draft. Please try again.", "danger")
        return render_template("intake.html", users=users, bank_accounts=bank_accounts,
                                prepared_by_user=prepared_by_user, errors=[])

    return redirect(url_for("intake_draft_edit", transaction_key=transaction_key))


@app.route("/intake")
def intake():
    """Treasury Request Intake Form."""
    if not has_role("submitter"):
        flash("Your role does not include submitting payment requests.", "warning")
        return redirect(url_for("dashboard"))
    _db_on = database_enabled()
    users             = []
    bank_accounts     = []
    prepared_by_user  = None
    identity_unmapped = False
    if _db_on:
        try:
            users         = db.get_user_list()
            bank_accounts = db.get_bank_accounts()
        except Exception:
            pass
        # Prepared By is always the authenticated user, never a form selection
        # (Batch 7 Part 1/2) — resolved here only for read-only display.
        prepared_by_user = current_app_user()
        identity_unmapped = prepared_by_user is None
        if identity_unmapped:
            flash(
                "Your signed-in account could not be matched to an active AppUser record, "
                "so a new request cannot be prepared. Contact an administrator.",
                "danger",
            )
    return render_template(
        "intake.html", users=users, bank_accounts=bank_accounts,
        prepared_by_user=prepared_by_user, identity_unmapped=identity_unmapped, draft=None,
    )


@app.route("/intake/draft/<int:transaction_key>")
def intake_draft_edit(transaction_key):
    """
    Resume/edit an existing Draft (Batch 8). Object-level authorization is
    enforced entirely server-side in db.get_draft_for_edit() (ownership +
    Current_Status='Draft') — a crafted URL to another user's Draft, or to a
    transaction that is no longer a Draft (submitted/cancelled/completed),
    returns nothing to edit here (Part 14).
    """
    if not has_role("submitter"):
        flash("Your role does not include submitting payment requests.", "warning")
        return redirect(url_for("dashboard"))
    if not database_enabled():
        flash("Drafts require the SQL data source and are not available for mock/session records.", "warning")
        return redirect(url_for("dashboard"))
    prepared_by_user = current_app_user()
    if prepared_by_user is None:
        flash(
            "Your signed-in account could not be matched to an active AppUser record.",
            "danger",
        )
        return redirect(url_for("dashboard"))
    draft = db.get_draft_for_edit(transaction_key, prepared_by_user_key=prepared_by_user["user_key"])
    if draft is None:
        flash("Draft not found, not yours, or no longer editable.", "warning")
        return redirect(url_for("dashboard"))
    users, bank_accounts = [], []
    try:
        users         = db.get_user_list()
        bank_accounts = db.get_bank_accounts()
    except Exception:
        pass
    # Show which required attachments are already on file for this Draft
    # (Part 4) — reuses the same SharePoint architecture/Request_ID, no new
    # storage; the requester is never asked to re-upload something already saved.
    existing_attachments = {}
    if sharepoint_enabled():
        try:
            for item in sharepoint.list_attachments(draft["request_id"]):
                key = sharepoint.DOC_TYPE_TO_ATTACHMENT_KEY.get(item["doc_type"])
                if key:
                    existing_attachments[key] = item["filename"]
        except Exception:
            app.logger.exception("Unable to load existing draft attachments for %s", draft["request_id"])
    return render_template(
        "intake.html", users=users, bank_accounts=bank_accounts,
        prepared_by_user=prepared_by_user, identity_unmapped=False, draft=draft,
        existing_attachments=existing_attachments,
    )


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
    if not has_role("submitter"):
        flash("Your role does not include submitting payment requests.", "warning")
        return redirect(url_for("dashboard"))
    frm = request.form
    _db_on = database_enabled()

    # Batch 7 Part 1/2/4/17: Prepared By is NEVER trusted from the browser for a
    # SQL-backed submission — the server re-resolves the authenticated AppUser
    # here regardless of any prepared_by_key the POST might contain, and rejects
    # the submission outright if that identity cannot be mapped (fail safe, no
    # silent fallback to another user or a hard-coded default).
    prepared_by_user = current_app_user() if _db_on else None
    if _db_on and prepared_by_user is None:
        flash(
            "Your signed-in account could not be matched to an active AppUser record; "
            "this request cannot be submitted. Contact an administrator.",
            "danger",
        )
        users, bank_accounts = [], []
        try:
            users         = db.get_user_list()
            bank_accounts = db.get_bank_accounts()
        except Exception:
            pass
        return render_template("intake.html", users=users, bank_accounts=bank_accounts,
                                prepared_by_user=None, identity_unmapped=True, errors=[])

    # Batch 8: Save Draft is a distinct operation from Submit Request — handled
    # entirely separately (light validation only, no workflow/events started).
    if _db_on and frm.get("form_mode") == "draft":
        return _handle_save_draft(frm, prepared_by_user)

    draft_transaction_key_raw = frm.get("transaction_key", "").strip() if _db_on else ""

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
        # Every transaction starts at Pending Approver; tier only determines
        # later VP/CFO requirements (see workflow.py for the additive routing model).
        "status":                workflow.STATUS_PENDING_APPROVER,
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
                "event":  "Routed to Approver",
                "actor":  "System",
                "status": workflow.STATUS_PENDING_APPROVER,
                "type":   "routed",
            },
        ],
        "comments": [],
    }

    if _db_on:
        files_present = _draft_files_present(frm.get("request_id", "")) if draft_transaction_key_raw else {
            "validation_evidence":   bool(request.files.get("file_validation_evidence") and request.files["file_validation_evidence"].filename),
            "wire_ach_instructions": bool(request.files.get("file_wire_ach_instructions") and request.files["file_wire_ach_instructions"].filename),
            "payment_support":       bool(request.files.get("file_payment_support") and request.files["file_payment_support"].filename),
        }
        bank_account_key_raw = frm.get("bank_account_key", "").strip()
        bank_account_status = None
        if bank_account_key_raw:
            try:
                bank_account_status = db.get_bank_account_status(int(bank_account_key_raw))
            except Exception:
                bank_account_status = None

        errors = _validate_intake_submission(
            frm, files_present=files_present, bank_account_status=bank_account_status,
            has_stored_receiving_bank_instruction=bool(draft_transaction_key_raw),
        )

        # Part 2 — validate the "Previously Used" claim against completed history.
        # Only meaningful once the basic receiving-bank fields themselves are valid.
        prior_transaction_key = None
        previously_used_claimed = frm.get("instructions_previously_used") == "yes"
        if not errors:
            try:
                prior_match = db.find_prior_completed_beneficiary_match(
                    payee_name=frm.get("recv_payee_name", ""),
                    receiving_bank_name=frm.get("recv_bank_name", ""),
                    receiving_account_number=frm.get("recv_account_number", ""),
                    receiving_routing_number=frm.get("recv_routing_number", ""),
                )
            except Exception:
                app.logger.exception("Unable to check prior beneficiary instruction history")
                prior_match = None

            if previously_used_claimed and prior_match is None:
                errors.append(
                    "These receiving banking instructions could not be verified as previously used and "
                    "completed. Please follow the new/unverified instructions process (verbal confirmation "
                    "with a known contact) instead of marking them as previously used."
                )
            elif not previously_used_claimed and prior_match is not None:
                errors.append(
                    f"These exact receiving banking instructions were already used on a completed transaction "
                    f"({prior_match['request_id']}, {prior_match['last_used_date']}). Please review and check "
                    f"\u201cBanking Instructions Previously Used?\u201d if this is correct, or correct the "
                    f"receiving bank details if this is actually a new payee/account."
                )
            elif previously_used_claimed and prior_match is not None:
                prior_transaction_key = prior_match["transaction_key"]

        if errors:
            for err in errors:
                flash(err, "warning")
            users, bank_accounts = [], []
            try:
                users         = db.get_user_list()
                bank_accounts = db.get_bank_accounts()
            except Exception:
                pass
            return render_template("intake.html", users=users, bank_accounts=bank_accounts,
                                    prepared_by_user=prepared_by_user, errors=errors)

        # Resolve the ApprovalRule BEFORE reserving a Request_ID / uploading
        # required evidence (Part 8) — a configuration failure should never
        # leave an orphaned SharePoint upload or reserved ID behind.
        try:
            resolved_rule = db.resolve_approval_rule(amount)
        except db.ApprovalRuleConfigurationError as exc:
            app.logger.error("ApprovalRule resolution failed: %s", exc)
            flash(f"Unable to route this request: {exc}", "danger")
            users, bank_accounts = [], []
            try:
                users         = db.get_user_list()
                bank_accounts = db.get_bank_accounts()
            except Exception:
                pass
            return render_template("intake.html", users=users, bank_accounts=bank_accounts,
                                    prepared_by_user=prepared_by_user, errors=[])

        if draft_transaction_key_raw:
            # Batch 8 Part 15: finalize an EXISTING Draft — same Transaction_Key/
            # Request_ID, same validation/rule-resolution as any new submission.
            try:
                transaction_key = int(draft_transaction_key_raw)
                request_id = frm.get("request_id", "").strip()
                _upload_intake_attachments(request_id)  # any new files attached in this POST
                _vdt_raw = frm.get("verbal_confirm_datetime", "")
                _vdt = (_vdt_raw.replace("T", " ") + ":00") if _vdt_raw else ""
                finalize_data = {
                    "approver_key":     int(frm.get("approver_key", 0)),
                    "controller_key":   int(frm.get("controller_key", 0)),
                    "bank_account_key": int(frm.get("bank_account_key", 0)),
                    "request_type":          frm.get("request_type", ""),
                    "property_dept":         frm.get("property_dept", ""),
                    "property_code":         frm.get("entity_id", ""),
                    "treasury_service_date": frm.get("treasury_service_date", ""),
                    "amount":                amount,
                    "currency":              frm.get("currency", "USD"),
                    "payment_purpose":       frm.get("payment_purpose", ""),
                    "urgent":                frm.get("urgent") == "yes",
                    "urgency_reason":        frm.get("urgency_reason", ""),
                    "instructions_previously_used": frm.get("instructions_previously_used") == "yes",
                    "last_used_date":        frm.get("last_used_date", ""),
                    "prior_transaction_key": prior_transaction_key,
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
                    "approval_rule_key": resolved_rule["approval_rule_key"],
                    "requires_vp":        resolved_rule["requires_vp"],
                    "requires_cfo":       resolved_rule["requires_cfo"],
                    "approval_tier": workflow.approval_tier_label(
                        requires_vp=resolved_rule["requires_vp"], requires_cfo=resolved_rule["requires_cfo"],
                    ),
                }
                db.finalize_draft_submission(transaction_key, finalize_data, prepared_by_user_key=prepared_by_user["user_key"])
                flash("Request submitted successfully.", "success")
                return redirect(url_for("confirmation", request_id=request_id))
            except db.WorkflowConflictError:
                flash("This draft has already been submitted or is no longer available.", "info")
                return redirect(url_for("dashboard"))
            except Exception:
                app.logger.exception("Draft finalization failed for transaction_key=%s", draft_transaction_key_raw)
                flash("Unable to submit this request. Please try again.", "danger")
                users, bank_accounts = [], []
                try:
                    users         = db.get_user_list()
                    bank_accounts = db.get_bank_accounts()
                except Exception:
                    pass
                return render_template("intake.html", users=users, bank_accounts=bank_accounts,
                                        prepared_by_user=prepared_by_user, errors=[])

        try:
            request_id = db.reserve_request_id()
            # Required attachments are uploaded to SharePoint BEFORE the SQL
            # transaction exists — if a required upload fails, no ETransaction
            # row is ever created (see _upload_required_intake_attachments()).
            _upload_required_intake_attachments(request_id)

            _vdt_raw = frm.get("verbal_confirm_datetime", "")
            _vdt = (_vdt_raw.replace("T", " ") + ":00") if _vdt_raw else ""
            db_data = {
                "request_id":       request_id,
                # Prepared By/Prepared Date are server-resolved, never taken from the
                # POST (Batch 7 Part 1/2/6/17) — prepared_by_user was already
                # validated non-None above.
                "prepared_by_key":  prepared_by_user["user_key"],
                "approver_key":     int(frm.get("approver_key", 0)),
                "controller_key":   int(frm.get("controller_key", 0)),
                "bank_account_key": int(frm.get("bank_account_key", 0)),
                "request_type":          frm.get("request_type", ""),
                "property_dept":         frm.get("property_dept", ""),
                "property_code":         frm.get("entity_id", ""),
                "treasury_service_date": frm.get("treasury_service_date", ""),
                "prepared_date":         datetime.now().strftime("%Y-%m-%d"),
                "amount":                amount,
                "currency":              frm.get("currency", "USD"),
                "payment_purpose":       frm.get("payment_purpose", ""),
                "status":                workflow.STATUS_PENDING_APPROVER,
                "urgent":                frm.get("urgent") == "yes",
                "urgency_reason":        frm.get("urgency_reason", ""),
                "instructions_previously_used": frm.get("instructions_previously_used") == "yes",
                "last_used_date":        frm.get("last_used_date", ""),
                "prior_transaction_key": prior_transaction_key,
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
            return redirect(url_for("confirmation", request_id=request_id))
        except db.ApprovalRuleConfigurationError as exc:
            # Controlled configuration error (Part 8) — no active/unambiguous
            # ApprovalRule matches this amount. Safe to show verbatim: it never
            # contains banking values, only the amount and rule-count mismatch.
            app.logger.error("ApprovalRule resolution failed: %s", exc)
            flash(f"Unable to route this request: {exc}", "danger")
            users, bank_accounts = [], []
            try:
                users         = db.get_user_list()
                bank_accounts = db.get_bank_accounts()
            except Exception:
                pass
            return render_template("intake.html", users=users, bank_accounts=bank_accounts,
                                    prepared_by_user=prepared_by_user, errors=[])
        except Exception:
            # Never echo the raw exception to the user — it can contain SQL error
            # text that references sensitive banking values (Part 14 safety rule).
            app.logger.exception("Intake submission failed")
            flash("Unable to submit this request. Please try again.", "danger")
            users, bank_accounts = [], []
            try:
                users         = db.get_user_list()
                bank_accounts = db.get_bank_accounts()
            except Exception:
                pass
            return render_template("intake.html", users=users, bank_accounts=bank_accounts,
                                    prepared_by_user=prepared_by_user, errors=[])

    # Batch 10 Part 3/4: this session-only fallback path is only ever reached
    # when the SQL data source is disabled (_db_on is False above) — it must
    # NEVER silently produce a fake "successful" submission in production if
    # DB_ENABLED is mis-set. Requires the same explicit dev posture as every
    # other legacy/mock fallback in this app (see dev_fallback_allowed()).
    if not dev_fallback_allowed():
        app.logger.error("Intake submission rejected: SQL data source is unavailable (DB_ENABLED=false) and dev fallback is disabled")
        flash("This application's data source is currently unavailable. Please contact an administrator before submitting.", "danger")
        return render_template("intake.html", users=[], bank_accounts=[],
                                prepared_by_user=None, identity_unmapped=False, errors=[])

    submitted = session.get("submitted_requests", [])
    submitted.append(record)
    session["submitted_requests"] = submitted
    _upload_intake_attachments(request_id)

    return redirect(url_for("confirmation", request_id=request_id))


def _authorized_group_keys_by_role(user_key, roles):
    """
    Per-role AccountingGroup_Key authorization: {role_code: [group_keys]} for
    each of the user's group-scoped roles (controller/vp/cfo) they actually
    hold — each role's own mapping is fetched and kept separate, never
    blended into another role's list (multi-role refactor Part 23: "VP access
    to Group A + Controller access to Group B" must never become
    "VP+Controller access to Groups A and B").
    """
    result = {}
    if user_key is None:
        return result
    for r in (set(roles) & authorization.GROUP_SCOPED_ROLES):
        try:
            result[r] = db.get_user_accounting_group_keys(user_key, role_code=r)
        except Exception:
            app.logger.exception("Unable to load accounting groups for user %s role %s", user_key, r)
            result[r] = []
    return result


@app.route("/confirmation/<request_id>")
def confirmation(request_id):
    """
    Post-submission confirmation screen. Batch 10 Part 2/24: this is a direct
    Request_ID URL just like request_detail() — it must not let any signed-in
    user view another user's confirmation summary merely by knowing/guessing
    a Request_ID, so it applies the SAME can_view_transaction() gate.
    """
    record = None
    if dev_fallback_allowed():
        submitted = session.get("submitted_requests", [])
        record = next((r for r in submitted if r["request_id"] == request_id), None)
        if not record:
            record = next((r for r in MOCK_REQUESTS if r["request_id"] == request_id), None)
    _db_on = database_enabled()
    if record is None and _db_on:
        roles = current_roles()
        user_key = current_app_user_key()
        try:
            txn_summary = db.get_transaction_for_workflow(request_id)
        except Exception:
            txn_summary = None
        if txn_summary is not None:
            authorized_groups = _authorized_group_keys_by_role(user_key, roles)
            if authorization.can_view_transaction(
                role=roles, user_key=user_key, txn=txn_summary, authorized_group_keys=authorized_groups,
            ):
                try:
                    record = db.get_request_detail(request_id)
                except Exception:
                    record = None
    return render_template("confirmation.html", record=record, request_id=request_id)


def _resolve_dashboard_authorized_scope(roles, user_key):
    """
    Server-side authorized transaction scope for `roles`/`user_key` (Batch 4;
    unioned across the full role set as of the multi-role authorization
    refactor), shared by the dashboard view AND the filtered export (Batch 9)
    so authorization can never drift between the two surfaces — the export
    route calls this exact function instead of re-implementing any scoping rule.

    `roles`/`user_key` MUST already be server-resolved (current_roles() /
    current_app_user_key()) — never browser-supplied. Any accounting-group
    broadening is validated against this user's own authorized groups before
    being applied (Part 4) — an unrecognized/unauthorized group never widens
    the scope, it is simply ignored.

    Returns (scoped_records, authorized_group_keys, accounting_group_key).
    `authorized_group_keys` is the flat UNION of every held group-scoped
    role's own authorized groups (used only to populate/validate the Scope
    dropdown's choices) — db.get_dashboard_records()/can_view_transaction()
    still evaluate each role's own group mapping separately.
    """
    submitted = session.get("submitted_requests", [])
    _db_on = database_enabled()

    authorized_group_keys = []
    accounting_group_key = None
    if _db_on and user_key is not None and (roles & authorization.GROUP_SCOPED_ROLES):
        by_role = _authorized_group_keys_by_role(user_key, roles)
        authorized_group_keys = sorted(set().union(*by_role.values())) if by_role else []
        requested_group_raw = request.args.get("accounting_group_key", "").strip()
        if requested_group_raw:
            try:
                requested_group = int(requested_group_raw)
            except ValueError:
                requested_group = None
            if requested_group is not None and requested_group in authorized_group_keys:
                accounting_group_key = requested_group

    try:
        db_records = db.get_dashboard_records(role=roles, user_key=user_key,
                                               accounting_group_key=accounting_group_key) if _db_on else []
    except Exception:
        app.logger.exception("Unable to load dashboard records from the database")
        db_records = []

    # Session/mock records have no real ownership keys to scope by, so they
    # keep the pre-Batch-4 status-only filtering (dev/demo path only — real
    # SQL-backed data above is now properly authorized). Batch 10 Part 3/4:
    # never surfaced outside an explicit dev posture (see dev_fallback_allowed()).
    # Unioned across the full role set (multi-role refactor Part 9/10) and
    # de-duplicated by Request_ID so a record matching more than one role's
    # legacy scope is never shown twice.
    legacy_records = (list(MOCK_REQUESTS) if mock_data_enabled() else []) + submitted if dev_fallback_allowed() else []
    legacy_scoped_ids = set()
    legacy_scoped = []
    for r in legacy_records:
        matches = (
            ("sam" in roles and r["status"] in ("Pending SAM Approval", workflow.STATUS_PENDING_APPROVER))
            or ("controller" in roles and r["status"] in ("Pending Controller Approval", workflow.STATUS_PENDING_CONTROLLER))
            or bool(roles & {"submitter", "business_admin", "vp", "cfo", "treasury"})
        )
        if matches and r["request_id"] not in legacy_scoped_ids:
            legacy_scoped_ids.add(r["request_id"])
            legacy_scoped.append(r)

    combined = db_records + legacy_scoped
    seen_keys = set()
    deduped = []
    for r in combined:
        # Request_ID is the natural globally-unique business key across both
        # SQL-backed and legacy/mock records (Transaction_Key only exists for
        # SQL rows) — prefer it for de-duplication.
        dedup_key = r.get("request_id") or r.get("transaction_key")
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)
        deduped.append(r)

    return deduped, authorized_group_keys, accounting_group_key


def _parse_dashboard_filters():
    """
    Read the dashboard's filter query parameters — the SAME parameter names
    the dashboard filter form posts (GET) — shared by dashboard() and the
    export route so a filter can never behave differently between the two
    (Part 11).
    """
    return {
        "status":       request.args.get("status", "").strip(),
        "request_type": request.args.get("request_type", "").strip(),
        "property":     request.args.get("property", "").strip(),
        "approver":     request.args.get("approver", "").strip(),
        "urgent_only":  request.args.get("urgent_only", ""),
        "over_1m_only": request.args.get("over_1m_only", ""),
        "amount_min":   request.args.get("amount_min", "").strip(),
        "amount_max":   request.args.get("amount_max", "").strip(),
    }


def _apply_dashboard_filters(scoped, filters):
    """
    Narrow an already-authorized `scoped` record list by the dashboard's
    filter parameters — never widens it. Shared by dashboard() and the
    export route (Part 5/11) so the two can never drift apart.
    """
    filtered = list(scoped)
    if filters["status"]:
        # Legacy and current vocabulary strings represent the same stage — treat
        # links/bookmarks using either as equivalent when filtering.
        status_group = _STATUS_EQUIVALENTS.get(filters["status"], {filters["status"]})
        filtered = [r for r in filtered if r["status"] in status_group]
    if filters["request_type"]:
        filtered = [r for r in filtered if r["request_type"] == filters["request_type"]]
    if filters["property"]:
        needle = filters["property"].lower()
        filtered = [r for r in filtered if needle in r.get("property_dept", "").lower()]
    if filters["approver"]:
        needle = filters["approver"].lower()
        filtered = [r for r in filtered if needle in r.get("assigned_approver", "").lower()]
    if filters["urgent_only"]:
        filtered = [r for r in filtered if r.get("urgent", False)]
    if filters["over_1m_only"]:
        filtered = [r for r in filtered if r.get("amount", 0) > 1_000_000]
    if filters["amount_min"]:
        try:
            filtered = [r for r in filtered if r.get("amount", 0) >= float(filters["amount_min"])]
        except ValueError:
            pass
    if filters["amount_max"]:
        try:
            filtered = [r for r in filtered if r.get("amount", 0) <= float(filters["amount_max"])]
        except ValueError:
            pass
    return filtered


@app.route("/dashboard")
def dashboard():
    """
    Dashboard — server-side authorized scope, unioned across the caller's
    complete role set (multi-role authorization refactor; Batch 4 individual
    scopes unchanged). Dashboard filters only ever NARROW this authorized
    scope, never expand it — see authorization.py / db.get_dashboard_records()
    for the scoping rules, which are the same rules request_detail()/reveal
    endpoints enforce per-object (Part 2/11/12). The filtered export route
    (Batch 9) reuses _resolve_dashboard_authorized_scope()/_apply_dashboard_filters() below.
    """
    roles     = current_roles()
    user_key  = current_app_user_key()

    scoped, authorized_group_keys, accounting_group_key = _resolve_dashboard_authorized_scope(roles, user_key)

    stats = {
        "total":              len(scoped),
        "pending_sam":        sum(1 for r in scoped if r["status"] in ("Pending SAM Approval", workflow.STATUS_PENDING_APPROVER)),
        "pending_controller": sum(1 for r in scoped if r["status"] in ("Pending Controller Approval", workflow.STATUS_PENDING_CONTROLLER)),
        "pending_vp":         sum(1 for r in scoped if r["status"] in ("Pending VP Approval", workflow.STATUS_PENDING_VP)),
        "pending_cfo":        sum(1 for r in scoped if r["status"] in ("Pending CFO Approval", workflow.STATUS_PENDING_CFO)),
        "pending_treasury":   sum(1 for r in scoped if r["status"] in ("Pending Treasury Review", workflow.STATUS_READY_FOR_TREASURY)),
        "pending_release":    sum(1 for r in scoped if r["status"] in ("Pending Release", workflow.STATUS_AWAITING_RELEASE)),
        "completed":          sum(1 for r in scoped if r["status"] in ("Completed", "Released", workflow.STATUS_TREASURY_RELEASED, workflow.STATUS_COMPLETED)),
        "needs_more_info":    sum(1 for r in scoped if r["status"] in ("Needs More Information", workflow.STATUS_MORE_INFO)),
        "rejected":           sum(1 for r in scoped if r["status"] in ("Rejected", workflow.STATUS_CANCELLED)),
        "urgent":             sum(1 for r in scoped if r.get("urgent", False)),
        "over_1m":            sum(1 for r in scoped if r.get("amount", 0) > 1_000_000),
    }

    filters  = _parse_dashboard_filters()
    filtered = _apply_dashboard_filters(scoped, filters)
    filtered.sort(key=lambda r: r["submitted_date"], reverse=True)

    all_statuses  = sorted({r["status"] for r in scoped})
    all_types     = sorted({r["request_type"] for r in scoped})
    all_approvers = sorted({r.get("assigned_approver", "") for r in scoped if r.get("assigned_approver")})

    accounting_groups = []
    if authorized_group_keys:
        try:
            accounting_groups = db.get_accounting_groups_by_keys(authorized_group_keys)
        except Exception:
            app.logger.exception("Unable to load accounting group names")

    return render_template(
        "dashboard.html",
        requests=filtered,
        stats=stats,
        all_statuses=all_statuses,
        all_types=all_types,
        all_approvers=all_approvers,
        accounting_groups=accounting_groups,
        selected_accounting_group_key=accounting_group_key,
        filters=filters,
    )


@app.route("/dashboard/export")
def export_dashboard():
    """
    Filtered CSV export of the CURRENT authorized dashboard scope (Batch 9).
    Export is NOT a separate authorization model — it resolves the same
    server-side role/user_key, applies the same _resolve_dashboard_authorized_scope()
    and _apply_dashboard_filters() the dashboard view uses, and never trusts
    any browser-supplied role/user/group value (Part 4/5/6). If filters/search
    would show the requester nothing on the dashboard, export contains
    nothing either (Part 13/18) — it never falls back to a broader dataset.
    """
    role     = current_roles()
    user_key = current_app_user_key()

    if not database_enabled():
        flash("Export requires the SQL data source and is not available for mock/session data.", "warning")
        return redirect(url_for("dashboard"))

    scoped, _authorized_group_keys, _accounting_group_key = _resolve_dashboard_authorized_scope(role, user_key)
    filters  = _parse_dashboard_filters()
    filtered = _apply_dashboard_filters(scoped, filters)
    filtered.sort(key=lambda r: r["submitted_date"], reverse=True)

    csv_bytes = export_module.build_dashboard_export_csv(filtered)
    # Minimal audit trail (Part 17) — never log row contents/banking values.
    app.logger.info("Dashboard export: user_key=%s roles=%s rows=%d", user_key, sorted(role), len(filtered))

    return Response(
        csv_bytes,
        mimetype="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{export_module.export_filename()}"',
            "Cache-Control": "no-store",
        },
    )


@app.route("/bank-accounts")
def bank_accounts():
    """Bank Account Management dashboard."""
    require_bank_account_view()
    filters = {
        "bank_name": request.args.get("bank_name", "").strip(),
        "classification": request.args.get("classification", "").strip(),
        "entity_key": request.args.get("entity_key", "").strip(),
        "status": request.args.get("status", "").strip(),
        "opened_from": request.args.get("opened_from", "").strip(),
        "opened_to": request.args.get("opened_to", "").strip(),
        "closed_from": request.args.get("closed_from", "").strip(),
        "closed_to": request.args.get("closed_to", "").strip(),
        "last4": request.args.get("last4", "").strip(),
    }
    try:
        records = db.search_bank_account_records(filters)
        entities = db.get_business_entities(active_only=False)
    except Exception:
        app.logger.exception("Unable to load Bank Account Management dashboard")
        records = []
        entities = []
        flash("Unable to load bank accounts right now.", "danger")

    stats = {
        "open": sum(1 for r in records if r["status"] == "Open"),
        "closed": sum(1 for r in records if r["status"] == "Closed"),
        "corporate": sum(1 for r in records if r["classification"].lower() == "corporate"),
        "property": sum(1 for r in records if r["classification"].lower() == "property"),
    }
    return render_template(
        "bank_accounts.html",
        records=records,
        entities=entities,
        filters=filters,
        stats=stats,
    )


@app.route("/bank-accounts/new", methods=["GET", "POST"])
def bank_account_new():
    if not can_edit_bank_accounts():
        abort(403)
    entities = db.get_business_entities(active_only=True)
    if request.method == "POST":
        data = _bank_account_form_data()
        errors = _validate_bank_account_form(data, is_new=True)
        actor_key = current_app_user_key()
        if actor_key is None:
            errors.append("Your signed-in user could not be matched to an active AppUser record.")
        if not errors:
            try:
                bank_account_key = db.create_bank_account(data, actor_key)
                flash("Bank account created successfully.", "success")
                return redirect(url_for("bank_account_edit", bank_account_key=bank_account_key))
            except Exception:
                app.logger.exception("Unable to create bank account")
                errors.append("Unable to save the bank account. Please try again.")
        for error in errors:
            flash(error, "warning")
        return render_template("bank_account_form.html", mode="new", record=data, entities=entities)
    return render_template("bank_account_form.html", mode="new", record={}, entities=entities)


@app.route("/bank-accounts/<int:bank_account_key>/edit", methods=["GET", "POST"])
def bank_account_edit(bank_account_key):
    require_bank_account_view()
    record = db.get_bank_account_record(bank_account_key)
    if record is None:
        flash("Bank account not found.", "warning")
        return redirect(url_for("bank_accounts"))
    entities = db.get_business_entities(active_only=False)
    if request.method == "POST":
        if not can_edit_bank_accounts():
            abort(403)
        data = _bank_account_form_data()
        errors = _validate_bank_account_form(data, is_new=False)
        actor_key = current_app_user_key()
        if actor_key is None:
            errors.append("Your signed-in user could not be matched to an active AppUser record.")
        if not errors:
            try:
                db.update_bank_account(bank_account_key, data, actor_key)
                flash("Bank account updated successfully.", "success")
                return redirect(url_for("bank_account_edit", bank_account_key=bank_account_key))
            except Exception:
                app.logger.exception("Unable to update bank account %s", bank_account_key)
                errors.append("Unable to save the bank account. Please try again.")
        for error in errors:
            flash(error, "warning")
        data["bankaccount_key"] = bank_account_key
        return render_template("bank_account_form.html", mode="edit", record=data, entities=entities)
    return render_template("bank_account_form.html", mode="edit", record=record, entities=entities)


@app.route("/bank-accounts/<int:bank_account_key>/close", methods=["POST"])
def bank_account_close(bank_account_key):
    if not can_edit_bank_accounts():
        abort(403)
    record = db.get_bank_account_record(bank_account_key)
    if record is None:
        flash("Bank account not found.", "warning")
        return redirect(url_for("bank_accounts"))
    data = dict(record)
    data.update({
        "entity_key": str(record.get("entity_key") or ""),
        "bank_name": record.get("bankname", ""),
        "account_name_id": record.get("accountnameid", ""),
        "account_title": record.get("accounttitle", ""),
        "account_title_modifier": record.get("accounttitlemodifier", ""),
        "system_account_name": record.get("systemaccountname", ""),
        "account_number": "",
        "routing_number": "",
        "transit_number_canada": "",
        "institution_number_canada": "",
        "gl_account_number": record.get("glaccountnumber", ""),
        "gl_account_name": record.get("glaccountname", ""),
        "tax_id_number": "",
        "address": record.get("address", ""),
        "phone_number": record.get("phonenumber", ""),
        "account_type": record.get("accounttype", ""),
        "account_classification": record.get("accountclassification", ""),
        "status": "Closed",
        "date_opened": record.get("dateopened", ""),
        "date_closed": request.form.get("date_closed", "").strip(),
        "bank_contact_name": record.get("bankcontactname", ""),
        "notes": record.get("notes", ""),
    })
    errors = _validate_bank_account_form(data, is_new=False)
    actor_key = current_app_user_key()
    if actor_key is None:
        errors.append("Your signed-in user could not be matched to an active AppUser record.")
    if errors:
        for error in errors:
            flash(error, "warning")
        return redirect(url_for("bank_account_edit", bank_account_key=bank_account_key))
    db.update_bank_account(bank_account_key, data, actor_key)
    flash("Bank account closed successfully.", "success")
    return redirect(url_for("bank_accounts"))


@app.route("/bank-accounts/<int:bank_account_key>/reveal", methods=["POST"])
def bank_account_reveal(bank_account_key):
    """
    Server-side controlled reveal for one sensitive BankAccount field.
    Authorization = the same as viewing Bank Account Management (reveal is
    contextual to record access, not a separate role — see banking_security.py).
    """
    role = current_roles_display()
    actor_user_key = current_app_user_key()
    field_key = request.form.get("field", "")

    if not can_view_bank_accounts():
        banking_security.audit_banking_data_reveal(
            actor_user_key=actor_user_key, actor_role=role, object_type="BankAccount",
            object_key=bank_account_key, field=field_key, success=False, reason="unauthorized",
        )
        abort(403)

    if field_key not in banking_security.REVEALABLE_BANK_ACCOUNT_FIELDS:
        return jsonify({"success": False, "reason": "Unknown field."}), 400

    column = banking_security.REVEALABLE_BANK_ACCOUNT_FIELDS[field_key]
    try:
        raw_value = db.get_bank_account_sensitive_field(bank_account_key, column)
    except Exception:
        app.logger.exception("Reveal query failed for BankAccount %s field %s", bank_account_key, field_key)
        return jsonify({"success": False, "reason": "Unable to retrieve value."}), 500

    if raw_value is None:
        banking_security.audit_banking_data_reveal(
            actor_user_key=actor_user_key, actor_role=role, object_type="BankAccount",
            object_key=bank_account_key, field=field_key, success=False, reason="not_found",
        )
        return jsonify({"success": False, "reason": "Bank account not found."}), 404

    if banking_security.is_mask_placeholder(raw_value):
        banking_security.audit_banking_data_reveal(
            actor_user_key=actor_user_key, actor_role=role, object_type="BankAccount",
            object_key=bank_account_key, field=field_key, success=False, reason="ddm_masked",
        )
        return jsonify({
            "success": False,
            "reason": "Full value is not currently available through the application's database identity.",
        })

    banking_security.audit_banking_data_reveal(
        actor_user_key=actor_user_key, actor_role=role, object_type="BankAccount",
        object_key=bank_account_key, field=field_key, success=True,
    )
    return jsonify({"success": True, "value": raw_value})


@app.route("/dashboard/request/<request_id>")
def request_detail(request_id):
    """
    Request detail view with masked banking information.

    Object-level authorization (Batch 4): a transaction that exists in SQL but
    falls outside the signed-in user's authorized scope is rejected with 403
    BEFORE any transaction data is fetched/rendered — a user cannot bypass
    dashboard scoping by guessing/typing a Request_ID directly (Part 12).
    """
    roles = current_roles()
    user_key = current_app_user_key()
    record = None
    _db_on = database_enabled()
    if _db_on:
        try:
            txn_summary = db.get_transaction_for_workflow(request_id)
        except Exception:
            app.logger.exception("Unable to load transaction %s for authorization check", request_id)
            txn_summary = None

        if txn_summary is not None:
            authorized_groups = _authorized_group_keys_by_role(user_key, roles)
            if not authorization.can_view_transaction(
                role=roles, user_key=user_key, txn=txn_summary, authorized_group_keys=authorized_groups,
            ):
                abort(403)
            try:
                record = db.get_request_detail(request_id)
            except Exception:
                app.logger.exception("Unable to load request detail for %s", request_id)
                record = None

    from_sql = record is not None

    # Batch 10 Part 3/4: the legacy mock/session fallback must never be
    # reachable outside an explicit dev posture — otherwise ANY signed-in
    # user could view static MOCK_REQUESTS demo data (or another dev session's
    # own submitted_requests) for a request_id that simply isn't in SQL, with
    # no object-level authorization check at all.
    if record is None and dev_fallback_allowed():
        submitted = session.get("submitted_requests", [])
        all_requests = MOCK_REQUESTS + submitted
        record = next((r for r in all_requests if r["request_id"] == request_id), None)

    if not record:
        flash("Request not found.", "warning")
        return redirect(url_for("dashboard"))

    # Application-layer masking (never rely on DDM alone — see banking_security.py).
    # The raw values are popped out entirely so they never reach the template/page
    # source; Reveal is a separate, server-side-authorized request (request_reveal_banking()).
    display = copy.deepcopy(record)
    display["orig_account_number_masked"] = banking_security.mask_account_number(display.pop("orig_account_number", ""))
    display["orig_routing_number_masked"]  = banking_security.mask_routing_number(display.pop("orig_routing_number", ""))
    display["recv_account_number_masked"]  = banking_security.mask_account_number(display.pop("recv_account_number", ""))
    display["recv_routing_number_masked"]  = banking_security.mask_routing_number(display.pop("recv_routing_number", ""))

    display["can_add_comment"] = from_sql

    display["can_reassign"] = False
    display["reassignment_candidates"] = []
    stage = workflow.REASSIGNMENT_STAGE_MAP.get(record.get("status"))
    if from_sql and stage and (set(workflow.REASSIGNMENT_ROLES) & roles):
        _stage_field, workflow_role, role_code = stage
        display["can_reassign"] = True
        display["reassignment_workflow_role"] = workflow_role
        try:
            display["reassignment_candidates"] = db.get_reassignment_candidates(role_code)
        except Exception:
            app.logger.exception("Unable to load reassignment candidates for %s", request_id)

    if sharepoint_enabled():
        try:
            sp_items = sharepoint.list_attachments(request_id)
        except Exception:
            app.logger.exception("Unable to load SharePoint attachments for %s", request_id)
            sp_items = []

        attachments       = dict(display.get("attachments") or {})
        attachment_urls   = {}
        extra_attachments = list(display.get("extra_attachments") or [])
        treasury_evidence = {}

        for item in sp_items:
            key = sharepoint.DOC_TYPE_TO_ATTACHMENT_KEY.get(item["doc_type"])
            if key:
                attachments[key]     = item["filename"]
                attachment_urls[key] = item["web_url"]
                continue
            evidence_key = sharepoint.DOC_TYPE_TO_EVIDENCE_KEY.get(item["doc_type"])
            if evidence_key:
                # Batch 5: Treasury/release evidence gets its own display section
                # (request_detail.html "Treasury Processing / Release Evidence")
                # rather than falling into the generic Additional Attachments list.
                treasury_evidence[evidence_key] = {
                    "filename":    item["filename"],
                    "web_url":     item["web_url"],
                    "uploaded_by": item.get("uploaded_by_role", ""),
                    "date":        item.get("uploaded_date", "")[:10],
                }
                continue
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
        display["treasury_evidence"] = treasury_evidence

    return render_template("request_detail.html", record=display)


@app.route("/dashboard/request/<request_id>/reveal-banking/<side>", methods=["POST"])
def request_reveal_banking(request_id, side):
    """
    Server-side controlled reveal for one originating/receiving banking field
    on a transaction. Authorization reuses the SAME centralized
    authorization.can_view_transaction() check as request_detail() (Part 13)
    — a user cannot reveal banking data for a transaction they cannot
    otherwise view. Never trusts a client-supplied BankAccount_Key/
    BeneficiaryInstruction_Key — both are resolved server-side from Request_ID.
    """
    roles = current_roles()
    actor_role = current_roles_display()
    actor_user_key = current_app_user_key()
    field_key = request.form.get("field", "")

    if side not in ("originating", "receiving"):
        return jsonify({"success": False, "reason": "Unknown section."}), 400
    if field_key not in ("account_number", "routing_number"):
        return jsonify({"success": False, "reason": "Unknown field."}), 400

    try:
        txn_summary = db.get_transaction_for_workflow(request_id)
    except Exception:
        app.logger.exception("Reveal authorization lookup failed for %s", request_id)
        return jsonify({"success": False, "reason": "Unable to retrieve value."}), 500

    if txn_summary is None:
        banking_security.audit_banking_data_reveal(
            actor_user_key=actor_user_key, actor_role=actor_role, object_type="Transaction",
            object_key=request_id, field=field_key, success=False, reason="not_found",
        )
        return jsonify({"success": False, "reason": "Request not found."}), 404

    authorized_groups = _authorized_group_keys_by_role(actor_user_key, roles)

    if not authorization.can_view_transaction(
        role=roles, user_key=actor_user_key, txn=txn_summary, authorized_group_keys=authorized_groups,
    ):
        banking_security.audit_banking_data_reveal(
            actor_user_key=actor_user_key, actor_role=actor_role, object_type="Transaction",
            object_key=request_id, field=field_key, success=False, reason="unauthorized",
        )
        abort(403)

    try:
        keys = db.get_transaction_banking_keys(request_id)
    except Exception:
        app.logger.exception("Reveal lookup failed for %s", request_id)
        return jsonify({"success": False, "reason": "Unable to retrieve value."}), 500

    object_type = "BankAccount" if side == "originating" else "BeneficiaryBankInstruction"
    object_key = None
    if keys is not None:
        object_key = keys["originating_bank_account_key"] if side == "originating" else keys["beneficiary_instruction_key"]

    if keys is None or object_key is None:
        banking_security.audit_banking_data_reveal(
            actor_user_key=actor_user_key, actor_role=actor_role, object_type=object_type,
            object_key=request_id, field=field_key, success=False, reason="not_found",
        )
        return jsonify({"success": False, "reason": "Request not found."}), 404

    try:
        if side == "originating":
            column = banking_security.REVEALABLE_BANK_ACCOUNT_FIELDS[field_key]
            raw_value = db.get_bank_account_sensitive_field(object_key, column)
        else:
            column = banking_security.REVEALABLE_BENEFICIARY_FIELDS[field_key]
            raw_value = db.get_beneficiary_instruction_sensitive_field(object_key, column)
    except Exception:
        app.logger.exception("Reveal query failed for %s side=%s field=%s", request_id, side, field_key)
        return jsonify({"success": False, "reason": "Unable to retrieve value."}), 500

    if raw_value is None or banking_security.is_mask_placeholder(raw_value):
        banking_security.audit_banking_data_reveal(
            actor_user_key=actor_user_key, actor_role=actor_role, object_type=object_type,
            object_key=object_key, field=field_key, success=False, reason="ddm_masked",
        )
        return jsonify({
            "success": False,
            "reason": "Full value is not currently available through the application's database identity.",
        })

    banking_security.audit_banking_data_reveal(
        actor_user_key=actor_user_key, actor_role=actor_role, object_type=object_type,
        object_key=object_key, field=field_key, success=True,
    )
    return jsonify({"success": True, "value": raw_value})


@app.route("/dashboard/request/<request_id>/action", methods=["POST"])
def request_action(request_id):
    """
    Process a workflow action. SQL-backed transactions are routed through the
    centralized workflow engine (workflow.py + db.advance_transaction_workflow),
    which performs real server-side authorization, ownership routing, and audit
    history. Session/mock records (no real AppUser keys to authorize against)
    keep their existing simplified session-based behavior.

    [EMAIL] Not implemented here — Power Automate reacts to the Status/
            CurrentOwner_User_Key/WorkflowEvent values this route writes.
    """
    if database_enabled():
        try:
            txn = db.get_transaction_for_workflow(request_id)
        except Exception:
            app.logger.exception("Unable to load transaction %s for workflow action", request_id)
            txn = None
        if txn is not None:
            return _handle_sql_workflow_action(request_id, txn)

    # Batch 10 Part 3/4: never let the legacy mock/session action handler run
    # outside an explicit dev posture — otherwise any signed-in user could
    # mutate static MOCK_REQUESTS/session demo state for a request_id that
    # simply isn't a real SQL-backed transaction, with no authorization at all.
    if not dev_fallback_allowed():
        flash("Request not found.", "warning")
        return redirect(url_for("dashboard"))

    return _handle_legacy_session_action(request_id)


def _handle_sql_workflow_action(request_id, txn):
    """Authorize and apply a workflow action against a SQL-backed transaction."""
    action  = request.form.get("action", "")
    comment = request.form.get("comment", "").strip()
    roles   = current_roles()
    user_key = current_app_user_key()
    evidence_correlation_id = None  # set once Batch 5 required evidence is successfully uploaded

    action_labels = {
        workflow.ACTION_APPROVE:            ("Approved", "success"),
        workflow.ACTION_MORE_INFO:          ("Returned \u2013 More Information Needed", "warning"),
        workflow.ACTION_CANCEL:             ("Cancelled", "secondary"),
        workflow.ACTION_REQUESTER_RESPOND:  ("Resubmitted with Additional Information", "info"),
        workflow.ACTION_TREASURY_INITIATED: ("Treasury Initiated", "info"),
        workflow.ACTION_TREASURY_RELEASED:  ("Treasury Released", "success"),
        workflow.ACTION_BANK_RELEASE:       ("Bank Release Completed", "success"),
        workflow.ACTION_MARK_COMPLETED:     ("Marked Completed", "success"),
    }
    if action not in action_labels:
        flash("Unknown action.", "danger")
        return redirect(url_for("request_detail", request_id=request_id))

    if action == workflow.ACTION_CANCEL and not comment:
        flash("A cancellation reason is required.", "warning")
        return redirect(url_for("request_detail", request_id=request_id))

    try:
        # Visibility vs. action authorization (Part 14): workflow.authorize_action()
        # remains the sole authority for who may PERFORM an action and already
        # enforces a real per-object assignee check whenever a user_key is
        # resolved. A blanket can_view_transaction() gate was deliberately NOT
        # added here — Treasury's approved ability to cancel a transaction at
        # early stages (Pending Approver/Controller/VP/CFO/More Info) is
        # broader than Treasury's default dashboard visibility scope (Ready
        # for Treasury onward), so adding that gate here would silently break
        # an already-approved capability. See authorization.py's module
        # docstring and the Batch 4 report for the full rationale.
        workflow.authorize_action(role=roles, user_key=user_key, txn=txn, action=action)

        if action == workflow.ACTION_REQUESTER_RESPOND:
            txn["rfi_origin_status"] = db.get_last_rfi_origin_status(txn["transaction_key"])

        # determine_next_step() also validates Property-vs-Corporate action
        # eligibility (e.g. Treasury Initiated only for Property) — must run
        # BEFORE any evidence is uploaded, so a misapplied action is rejected
        # before anything is persisted to SharePoint (Batch 5 Part 8 ordering).
        new_status, new_owner, owner_role, satisfied = workflow.determine_next_step(txn, action)
        label, cat = action_labels[action]

        # Batch 5: required Treasury/release evidence — server-side authoritative,
        # never rely on the form's required attribute alone. Uploaded AFTER
        # authorization/state validation but BEFORE the SQL workflow transition,
        # so a missing/failed upload never advances Current_Status/Stage/Owner
        # and never produces a WorkflowEvent.
        if action in _EVIDENCE_REQUIRED_ACTIONS:
            doc_type, evidence_label = _EVIDENCE_REQUIRED_ACTIONS[action]
            evidence_file = request.files.get("evidence_file")
            if not evidence_file or not evidence_file.filename:
                flash(f"{evidence_label} is required to complete this action.", "warning")
                return redirect(url_for("request_detail", request_id=request_id))
            if not sharepoint_enabled():
                app.logger.error("Evidence upload unavailable (SharePoint disabled) for %s action=%s", request_id, action)
                flash("Evidence upload is not available right now; the action was not completed.", "danger")
                return redirect(url_for("request_detail", request_id=request_id))
            try:
                uploaded = sharepoint.upload_attachment(
                    request_id, evidence_file,
                    section=sharepoint.SECTION_TRANSACTION, doc_type=doc_type,
                    uploaded_by_role=current_roles_display(),
                    transaction_key=txn.get("transaction_key"), is_required=True,
                )
            except Exception:
                app.logger.exception("Evidence upload failed for %s action=%s", request_id, action)
                flash(f"Unable to upload {evidence_label.lower()}; the action was not completed.", "danger")
                return redirect(url_for("request_detail", request_id=request_id))
            if uploaded is None:
                # upload_attachment() returns None for an empty/blank file — treat as missing evidence.
                flash(f"{evidence_label} is required to complete this action.", "warning")
                return redirect(url_for("request_detail", request_id=request_id))
            evidence_correlation_id = uploaded.get("correlation_id")

        if satisfied and len(satisfied) > 1:
            note = f"One approval satisfied: {', '.join(satisfied)} (same employee)."
            comment = f"{comment} {note}".strip() if comment else note

        db.advance_transaction_workflow(
            txn["transaction_key"],
            from_status=txn["status"],
            new_status=new_status,
            new_owner_user_key=new_owner,
            actor_user_key=user_key,
            actor_role=current_roles_display(),
            action=action,
            workflow_role=owner_role,
            comments=comment or None,
            bank_releaser_user_key=new_owner if action == workflow.ACTION_TREASURY_INITIATED else None,
        )
        flash(f"Action recorded: <strong>{label}</strong> for {request_id}.", cat)

        if comment and user_key is not None:
            comment_type = workflow.ACTION_COMMENT_TYPE_MAP.get(action)
            if comment_type:
                try:
                    db.add_transaction_comment(
                        txn["transaction_key"],
                        author_user_key=user_key,
                        comment_type=comment_type,
                        comment_text=comment,
                    )
                except Exception:
                    app.logger.exception("Unable to record comment for %s", request_id)

    except workflow.UnauthorizedActionError as exc:
        flash(str(exc), "warning")
    except workflow.WorkflowConfigurationError as exc:
        app.logger.error("Workflow configuration error for %s: %s", request_id, exc)
        flash(str(exc), "danger")
    except db.WorkflowConflictError:
        if evidence_correlation_id:
            # SQL and SharePoint cannot share one transaction (Part 8) — evidence is already
            # uploaded and durable; log the orphan condition (no banking/file-content details).
            app.logger.warning(
                "Evidence uploaded but SQL workflow transition conflicted (possible orphaned "
                "SharePoint file) request_id=%s action=%s correlation_id=%s",
                request_id, action, evidence_correlation_id,
            )
        flash("This action was already processed for this transaction.", "info")
    except Exception:
        if evidence_correlation_id:
            app.logger.warning(
                "Evidence uploaded but the workflow action failed (possible orphaned "
                "SharePoint file) request_id=%s action=%s correlation_id=%s",
                request_id, action, evidence_correlation_id,
            )
        app.logger.exception("Workflow action failed for %s", request_id)
        flash("Unable to process this action. Please try again.", "danger")

    return redirect(url_for("request_detail", request_id=request_id))


@app.route("/dashboard/request/<request_id>/reassign", methods=["POST"])
def request_reassign(request_id):
    """
    Reassign the active Approver or Controller on a SQL-backed transaction.
    Does not change Current_Status and never requires resubmission — see
    workflow.authorize_reassignment() / db.reassign_transaction_participant().

    Batch 4 note: deliberately NOT gated by authorization.can_view_transaction().
    Reassignment's whole purpose (covering an absent Approver/Controller) needs
    Business Admin/Treasury/Controller to reach transactions outside their own
    default visibility scope (e.g. Treasury's default scope only starts at
    Ready for Treasury, well after the Approver/Controller stages reassignment
    applies to). workflow.authorize_reassignment()'s existing role+stage check
    remains the sole authority here, unchanged.
    """
    if not database_enabled():
        flash("Reassignment requires the SQL data source and is not available for mock/session records.", "warning")
        return redirect(url_for("request_detail", request_id=request_id))

    role           = current_roles()
    actor_user_key = current_app_user_key()
    reason         = request.form.get("reason", "").strip()
    new_user_key_raw = request.form.get("new_user_key", "").strip()

    if not reason:
        flash("A reassignment reason is required.", "warning")
        return redirect(url_for("request_detail", request_id=request_id))

    try:
        new_user_key = int(new_user_key_raw)
    except (TypeError, ValueError):
        flash("Please select a replacement.", "warning")
        return redirect(url_for("request_detail", request_id=request_id))

    try:
        txn = db.get_transaction_for_workflow(request_id)
        if txn is None:
            flash("Request not found.", "warning")
            return redirect(url_for("dashboard"))

        workflow.authorize_reassignment(role=role, txn=txn)

        stage_field, workflow_role, _role_code = workflow.REASSIGNMENT_STAGE_MAP[txn["status"]]
        prior_user_key = txn[stage_field]

        if new_user_key == prior_user_key:
            flash("Please select a different replacement.", "warning")
            return redirect(url_for("request_detail", request_id=request_id))

        db.reassign_transaction_participant(
            txn["transaction_key"],
            expected_status=txn["status"],
            stage_field=stage_field,
            workflow_role=workflow_role,
            prior_user_key=prior_user_key,
            new_user_key=new_user_key,
            actor_user_key=actor_user_key,
            actor_role=current_roles_display(),
            event_type=workflow.ACTION_REASSIGN,
            decision="Reassigned",
            reason=reason,
        )
        flash(f"{workflow_role} reassigned for {request_id}.", "success")

    except workflow.UnauthorizedActionError as exc:
        flash(str(exc), "warning")
    except db.WorkflowConflictError:
        flash("This transaction's assignment already changed; please refresh and try again.", "info")
    except Exception:
        app.logger.exception("Reassignment failed for %s", request_id)
        flash("Unable to complete reassignment. Please try again.", "danger")

    return redirect(url_for("request_detail", request_id=request_id))


@app.route("/dashboard/request/<request_id>/comment", methods=["POST"])
def request_comment(request_id):
    """Add a standalone General note to a SQL-backed transaction's comment history."""
    if not database_enabled():
        flash("Comments require the SQL data source and are not available for mock/session records.", "warning")
        return redirect(url_for("request_detail", request_id=request_id))

    comment_text = request.form.get("comment_text", "").strip()
    if not comment_text:
        flash("Please enter a comment before submitting.", "warning")
        return redirect(url_for("request_detail", request_id=request_id))

    user_key = current_app_user_key()
    if user_key is None:
        flash("Adding a comment requires a signed-in identity and is not available in the local dev role switcher.", "warning")
        return redirect(url_for("request_detail", request_id=request_id))

    try:
        txn = db.get_transaction_for_workflow(request_id)
        if txn is None:
            flash("Request not found.", "warning")
            return redirect(url_for("dashboard"))

        db.add_transaction_comment(
            txn["transaction_key"],
            author_user_key=user_key,
            comment_type=workflow.COMMENT_TYPE_GENERAL,
            comment_text=comment_text,
        )
        flash("Comment added.", "success")
    except Exception:
        app.logger.exception("Unable to add comment for %s", request_id)
        flash("Unable to add comment. Please try again.", "danger")

    return redirect(url_for("request_detail", request_id=request_id))


def _handle_legacy_session_action(request_id):
    """
    Simplified role+status action handling for session/mock records, which have
    no real AppUser keys to authorize an assignment against. Preserved as-is for
    local demo/mock use; SQL-backed transactions never reach this path.
    """
    action  = request.form.get("action", "")
    comment = request.form.get("comment", "").strip()
    # DEV-ONLY simplified demo path (never reached in production) — keyed to a
    # single representative role: the explicit isolation override if chosen,
    # else an arbitrary-but-deterministic pick from the Acting-As role set.
    role    = session.get("role") or next(iter(sorted(current_roles())), None)

    action_map = {
        "approve":           ("Pending Treasury Review",   "Approved",                           "success"),
        "cancel":            ("Cancelled",                 "Cancelled",                          "secondary"),
        "more_info":         ("Needs More Information",    "Returned \u2013 More Information Needed", "warning"),
        "treasury_reviewed": ("Pending Release",           "Marked as Treasury Reviewed",         "info"),
        "mark_released":     ("Released",                  "Marked as Released",                  "success"),
        "mark_completed":    ("Completed",                 "Marked as Completed",                 "success"),
    }

    # requester_respond (resubmit) target status is computed from the record's tier, so handled separately
    if action not in action_map and action not in ("resubmit", "requester_respond"):
        flash("Unknown action.", "danger")
        return redirect(url_for("request_detail", request_id=request_id))

    # Look up current status for authorization check
    submitted    = session.get("submitted_requests", [])
    all_requests = MOCK_REQUESTS + submitted
    record_ref   = next((r for r in all_requests if r["request_id"] == request_id), None)
    cur_status   = record_ref["status"] if record_ref else ""

    if action == "cancel" and not comment:
        flash("A cancellation reason is required.", "warning")
        return redirect(url_for("request_detail", request_id=request_id))

    # [RBAC] Role+status authorization — simulates permission gates that Azure AD will enforce.
    # Accepts both the legacy status strings (static MOCK_REQUESTS) and the
    # current workflow.py vocabulary (newly submitted session records).
    ALLOWED: dict[str, dict[str, bool]] = {
        "submitter": {
            "cancel":   cur_status in ("Submitted", "Pending SAM Approval", "Needs More Information",
                                        workflow.STATUS_PENDING_APPROVER, workflow.STATUS_MORE_INFO),
            "resubmit":          cur_status in ("Needs More Information", workflow.STATUS_MORE_INFO),
            "requester_respond": cur_status in ("Needs More Information", workflow.STATUS_MORE_INFO),
        },
        "sam": {
            "approve":   cur_status in ("Pending SAM Approval", workflow.STATUS_PENDING_APPROVER),
            "cancel":    cur_status in ("Pending SAM Approval", workflow.STATUS_PENDING_APPROVER),
            "more_info": cur_status in ("Pending SAM Approval", workflow.STATUS_PENDING_APPROVER),
        },
        "controller": {
            "approve":   cur_status in ("Pending Controller Approval", workflow.STATUS_PENDING_CONTROLLER),
            "cancel":    cur_status in ("Pending Controller Approval", workflow.STATUS_PENDING_CONTROLLER),
            "more_info": cur_status in ("Pending Controller Approval", workflow.STATUS_PENDING_CONTROLLER),
        },
        "vp": {
            "approve":   cur_status in ("Pending VP Approval", workflow.STATUS_PENDING_VP),
            "more_info": cur_status in ("Pending VP Approval", workflow.STATUS_PENDING_VP),
        },
        "cfo": {
            "approve":   cur_status in ("Pending CFO Approval", workflow.STATUS_PENDING_CFO),
            "more_info": cur_status in ("Pending CFO Approval", workflow.STATUS_PENDING_CFO),
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
    if action in ("resubmit", "requester_respond"):
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
    Attach additional supporting files to a request. SQL-backed transactions
    are persisted to SharePoint under the request's own folder; legacy mock/
    session records keep their existing dev-only in-session bookkeeping.

    Batch 10 Part 2/24: object-level authorization — the SAME
    can_view_transaction() gate as request_detail()/reveal. A client-supplied
    Request_ID must not grant an upload merely because a matching row exists;
    this route previously had NO authorization check at all.
    """
    uploaded_files = request.files.getlist("extra_files")
    description    = request.form.get("attachment_description", "").strip()

    filenames = [f.filename for f in uploaded_files if f.filename]
    if not filenames:
        flash("No files selected.", "warning")
        return redirect(url_for("request_detail", request_id=request_id))

    roles    = current_roles()
    user_key = current_app_user_key()
    uploader = current_roles_display()
    today    = datetime.now().strftime("%Y-%m-%d")

    is_sql_backed = False
    if database_enabled():
        try:
            txn_summary = db.get_transaction_for_workflow(request_id)
        except Exception:
            app.logger.exception("Unable to load transaction %s for attach authorization", request_id)
            txn_summary = None
        if txn_summary is not None:
            is_sql_backed = True
            authorized_groups = _authorized_group_keys_by_role(user_key, roles)
            if not authorization.can_view_transaction(
                role=roles, user_key=user_key, txn=txn_summary, authorized_group_keys=authorized_groups,
            ):
                abort(403)

    if not is_sql_backed and not dev_fallback_allowed():
        flash("Request not found.", "warning")
        return redirect(url_for("dashboard"))

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

    updated = is_sql_backed  # SharePoint IS the persistence for a real transaction — see request_detail()
    if dev_fallback_allowed():
        submitted = session.get("submitted_requests", [])
        for r in submitted:
            if r["request_id"] == request_id:
                r.setdefault("extra_attachments", []).extend(new_entries)
                updated = True
        session["submitted_requests"] = submitted

    if updated:
        flash(f"{len(filenames)} file(s) attached to {request_id}.", "success")
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
    if not has_any_role("business_admin", "it_admin"):
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
    # Batch 10 Part 4/8: the Werkzeug debugger/tracebacks must never be
    # reachable in production — only enabled when the dev role-switcher
    # bypass itself is explicitly on. Azure App Service runs this app via
    # Gunicorn (app:app), not this __main__ block, but it's hardened anyway.
    app.run(debug=auth.dev_login_enabled(), port=int(os.environ.get("PORT", 5000)))
