"""
Centralized transaction ownership / approval workflow engine.

Single source of truth for: required approval stages, the same-person
Approver/Controller rule, next-status/next-owner determination, and
server-side authorization (role + assignment). Routes in app.py report
"this user did X" to this module and persist whatever it returns via
db.advance_transaction_workflow() — no routing rules belong in app.py
or in templates.

Power Automate (not this module) is responsible for outbound notifications;
this module only ensures the database reflects the correct
Status / CurrentOwner_User_Key / WorkflowEvent / WorkflowAssignment values
for Power Automate to react to. No email is sent from here.
"""

# ── Canonical status vocabulary ──────────────────────────────
STATUS_DRAFT              = "Draft"          # Batch 8 — requester is still preparing the request
STATUS_PENDING_APPROVER   = "Pending Approver"
STATUS_PENDING_CONTROLLER = "Pending Controller"
STATUS_PENDING_VP         = "Pending VP"
STATUS_PENDING_CFO        = "Pending CFO"
STATUS_MORE_INFO          = "More Information Requested"
STATUS_READY_FOR_TREASURY = "Ready for Treasury"
STATUS_TREASURY_INITIATED = "Treasury Initiated"
STATUS_AWAITING_RELEASE   = "Awaiting Bank Release"
STATUS_TREASURY_RELEASED  = "Treasury Released"
STATUS_COMPLETED          = "Completed"
STATUS_CANCELLED          = "Cancelled"

TERMINAL_STATUSES = {STATUS_COMPLETED, STATUS_CANCELLED}

# Statuses from which a Cancel Transaction action remains eligible
CANCEL_ELIGIBLE_STATUSES = {
    STATUS_PENDING_APPROVER, STATUS_PENDING_CONTROLLER,
    STATUS_PENDING_VP, STATUS_PENDING_CFO, STATUS_MORE_INFO,
}

# ── Current_Workflow_Stage — kept in sync with Current_Status on every write ──
# A transaction's Current_Workflow_Stage must never go stale relative to its
# Current_Status (previously only set once at intake to "Approver" and never
# updated again). This map is the single source of truth for the correct stage
# label for a given status; db.advance_transaction_workflow() and
# db.insert_transaction() both derive Current_Workflow_Stage from this map in
# the SAME statement that writes Current_Status, so they can never diverge.
# STATUS_TREASURY_INITIATED is included defensively even though no code path
# currently writes that status value (see COPILOT_CONTEXT.md / prior audit).
STAGE_BY_STATUS = {
    STATUS_DRAFT:              "Draft",
    STATUS_PENDING_APPROVER:   "Approver",
    STATUS_PENDING_CONTROLLER: "Controller",
    STATUS_PENDING_VP:         "VP",
    STATUS_PENDING_CFO:        "CFO",
    STATUS_MORE_INFO:          "Requester",
    STATUS_READY_FOR_TREASURY: "Treasury",
    STATUS_TREASURY_INITIATED: "Bank Release",
    STATUS_AWAITING_RELEASE:   "Bank Release",
    STATUS_TREASURY_RELEASED:  "Treasury",
    STATUS_COMPLETED:          "Completed",
    STATUS_CANCELLED:          "Cancelled",
}


def stage_for_status(status: str) -> str:
    """Return the Current_Workflow_Stage label that must accompany a given Current_Status."""
    return STAGE_BY_STATUS.get(status, status or "")

# ── Actions a route may report to the workflow service ───────
ACTION_APPROVE            = "approve"
ACTION_MORE_INFO          = "more_info"
ACTION_CANCEL             = "cancel"
ACTION_REQUESTER_RESPOND  = "requester_respond"
ACTION_TREASURY_INITIATED = "treasury_initiated"   # Property: Treasury hands off
ACTION_TREASURY_RELEASED  = "treasury_released"    # Corporate: Treasury performs final release
ACTION_BANK_RELEASE       = "bank_release"          # Property: Controller/VP completes release
ACTION_MARK_COMPLETED     = "mark_completed"        # Corporate: Treasury confirms completion (legacy-only, see Batch 5/6)
ACTION_REASSIGN           = "reassign"               # Admin/Treasury/Controller reassigns Approver or Controller — also the WorkflowEvent.Event_Type written

PROPERTY_CLASSIFICATION = "property"

# ── WorkflowEvent.Event_Type contract (Batch 6, compatibility-corrected) ──
# Power Automate is already built around the pre-existing literals below, so
# every event that ALREADY EXISTED keeps its EXACT established Event_Type
# string — only truly NEW information (a new actionable owner) gets a new
# Event_Type. Python constant names may read more descriptively than the
# literal they hold (e.g. EVENT_APPROVAL = "approve") — the literal is what
# matters for the external contract, not the constant name.
EVENT_REQUEST_SUBMITTED      = "Submitted"           # unchanged pre-existing literal
EVENT_APPROVAL               = "approve"              # unchanged pre-existing literal — used for EVERY approval stage; distinguish stage via From_Status, not Event_Type
EVENT_RFI_REQUESTED          = "more_info"            # unchanged pre-existing literal
EVENT_RFI_RESPONSE_SUBMITTED = "requester_respond"    # unchanged pre-existing literal
EVENT_REASSIGNED             = "reassign"             # unchanged pre-existing literal
EVENT_CANCELLED              = "cancel"               # unchanged pre-existing literal
EVENT_TREASURY_INITIATED     = "treasury_initiated"   # unchanged pre-existing literal
EVENT_TREASURY_RELEASED      = "treasury_released"    # unchanged pre-existing literal
EVENT_BANK_RELEASED          = "bank_release"          # unchanged pre-existing literal

# New ADDITIVE events (Batch 6) — these carry information Power Automate could
# not previously get from any existing event, so no existing Switch case needs
# to change; only new cases need to be added to consume them.
EVENT_APPROVER_ASSIGNED   = "APPROVER_ASSIGNED"
EVENT_CONTROLLER_ASSIGNED = "CONTROLLER_ASSIGNED"
EVENT_VP_ASSIGNED         = "VP_ASSIGNED"
EVENT_CFO_ASSIGNED        = "CFO_ASSIGNED"
EVENT_READY_FOR_TREASURY  = "READY_FOR_TREASURY"

# Friendly Decision/display text for every Event_Type the app writes.
EVENT_TYPE_LABELS = {
    EVENT_REQUEST_SUBMITTED:      "Request Submitted",
    EVENT_APPROVAL:               "Approved",
    EVENT_APPROVER_ASSIGNED:      "Assigned to Approver",
    EVENT_CONTROLLER_ASSIGNED:    "Assigned to Controller",
    EVENT_VP_ASSIGNED:            "Assigned to VP",
    EVENT_CFO_ASSIGNED:           "Assigned to CFO",
    EVENT_READY_FOR_TREASURY:     "Ready for Treasury",
    EVENT_RFI_REQUESTED:          "More Information Requested",
    EVENT_RFI_RESPONSE_SUBMITTED: "Additional Info Submitted",  # WorkflowEvent.Decision is varchar(30)
    EVENT_REASSIGNED:             "Reassigned",
    EVENT_CANCELLED:              "Cancelled",
    EVENT_TREASURY_INITIATED:     "Treasury Initiated",
    EVENT_TREASURY_RELEASED:      "Treasury Released",
    EVENT_BANK_RELEASED:          "Bank Released",
    "mark_completed":             "Marked Completed",  # legacy-only path (Batch 5) — never written for new transactions
}

# Since EVENT_APPROVAL ("approve") no longer distinguishes stage by itself,
# the timeline display derives a stage-specific friendly label from the
# event's From_Status instead (see workflow.friendly_event_label() /
# db.get_request_detail()) — Event_Type itself is unchanged for Power Automate.
_APPROVAL_STAGE_LABEL_BY_FROM_STATUS = {
    STATUS_PENDING_APPROVER:   "Approver Approved",
    STATUS_PENDING_CONTROLLER: "Controller Approved",
    STATUS_PENDING_VP:         "VP Approved",
    STATUS_PENDING_CFO:        "CFO Approved",
}


def friendly_event_label(event_type: str, from_status: str = None) -> str:
    """Display-only friendly label for a WorkflowEvent row — never affects Event_Type itself."""
    if event_type == EVENT_APPROVAL:
        stage_label = _APPROVAL_STAGE_LABEL_BY_FROM_STATUS.get(from_status)
        if stage_label:
            return stage_label
    return EVENT_TYPE_LABELS.get(event_type, event_type)

# Event types that represent a NEW actionable owner and should carry
# Related_Assignment_Key back to the WorkflowAssignment row they caused
# (db.advance_transaction_workflow()). READY_FOR_TREASURY is deliberately
# excluded — Treasury visibility is status-based, not a single CurrentOwner
# assignment (Part 7). RFI/requester-response events are also excluded —
# they are their own notification category (Part 8), not modeled as a
# generic assignment event even though a Requester WorkflowAssignment row
# happens to be created alongside them.
ASSIGNMENT_LINKED_EVENT_TYPES = {
    EVENT_CONTROLLER_ASSIGNED,
    EVENT_VP_ASSIGNED,
    EVENT_CFO_ASSIGNED,
    EVENT_TREASURY_INITIATED,
}

# Maps the resulting new_status of an approval to the next-owner
# assignment/progression event — computed from the ACTUAL next stage
# (determine_next_step() already skips a redundant Controller step when the
# same person holds both roles), so no duplicate/redundant assignment event
# is ever produced for a skipped stage.
_ASSIGNMENT_EVENT_BY_NEW_STATUS = {
    STATUS_PENDING_CONTROLLER: EVENT_CONTROLLER_ASSIGNED,
    STATUS_PENDING_VP:         EVENT_VP_ASSIGNED,
    STATUS_PENDING_CFO:        EVENT_CFO_ASSIGNED,
    STATUS_READY_FOR_TREASURY: EVENT_READY_FOR_TREASURY,
}


def events_for_action(*, from_status: str, action: str, new_status: str) -> list:
    """
    Return the ordered list of WorkflowEvent.Event_Type values
    db.advance_transaction_workflow() should write for this action — pure/
    deterministic, no DB access. An approval always writes the established
    "approve" literal (stage is inferred from From_Status, not Event_Type)
    plus, only when there is an actual new owner/stage, one additive
    assignment/progression event. Every other action produces exactly 1
    event. The human-entered comment (if any) attaches only to the first
    event in the list.
    """
    if action == ACTION_APPROVE:
        events = [EVENT_APPROVAL]
        progression = _ASSIGNMENT_EVENT_BY_NEW_STATUS.get(new_status)
        if progression:
            events.append(progression)
        return events
    if action == ACTION_MORE_INFO:
        return [EVENT_RFI_REQUESTED]
    if action == ACTION_REQUESTER_RESPOND:
        return [EVENT_RFI_RESPONSE_SUBMITTED]
    if action == ACTION_CANCEL:
        return [EVENT_CANCELLED]
    if action == ACTION_TREASURY_INITIATED:
        return [EVENT_TREASURY_INITIATED]
    if action == ACTION_TREASURY_RELEASED:
        return [EVENT_TREASURY_RELEASED]
    if action == ACTION_BANK_RELEASE:
        return [EVENT_BANK_RELEASED]
    if action == ACTION_MARK_COMPLETED:
        return ["mark_completed"]  # legacy-only path (Batch 5) — no Version 1 canonical event defined
    return [action]  # defensive fallback; should not normally be reached

# ── TransactionComment.Comment_Type vocabulary ────────────────
# Controlled values — classify the human-entered note itself, not the workflow
# action that produced it (that's WorkflowEvent.Event_Type's job).
COMMENT_TYPE_GENERAL  = "GENERAL"   # Standalone note, not tied to a specific workflow function
COMMENT_TYPE_RFI      = "RFI"       # Request More Information reason, or the requester's response to it
COMMENT_TYPE_APPROVAL = "APPROVAL"  # Approver/Controller/VP/CFO review note
COMMENT_TYPE_TREASURY = "TREASURY"  # Treasury processing note (initiation, release, completion)

# Maps a reported action to the Comment_Type its optional comment should be
# stored under when mirrored into TransactionComment. cancel has no dedicated
# category in the current spec, so it falls back to GENERAL. reassign is
# intentionally absent — its reason already lives in
# WorkflowAssignment.Reassignment_Reason and is not mirrored here.
ACTION_COMMENT_TYPE_MAP = {
    ACTION_APPROVE:            COMMENT_TYPE_APPROVAL,
    ACTION_MORE_INFO:          COMMENT_TYPE_RFI,
    ACTION_REQUESTER_RESPOND:  COMMENT_TYPE_RFI,
    ACTION_CANCEL:             COMMENT_TYPE_GENERAL,
    ACTION_TREASURY_INITIATED: COMMENT_TYPE_TREASURY,
    ACTION_TREASURY_RELEASED:  COMMENT_TYPE_TREASURY,
    ACTION_BANK_RELEASE:       COMMENT_TYPE_TREASURY,
    ACTION_MARK_COMPLETED:     COMMENT_TYPE_TREASURY,
}

# Roles permitted to reassign the active Approver or Controller on a transaction.
REASSIGNMENT_ROLES = ("business_admin", "treasury", "controller")

# Stages that currently support reassignment, mapped to:
#   (txn dict key holding the assignee, WorkflowAssignment.Workflow_Role label,
#    AppUserRole.Role_Code to look up eligible replacements)
# VP, CFO, and Bank Releaser reassignment are not yet an approved requirement.
REASSIGNMENT_STAGE_MAP = {
    STATUS_PENDING_APPROVER:   ("selected_approver_user_key",   "Approver",   "sam"),
    STATUS_PENDING_CONTROLLER: ("selected_controller_user_key", "Controller", "controller"),
}


class WorkflowError(Exception):
    """Base class for workflow rule violations."""


class UnauthorizedActionError(WorkflowError):
    """Raised when the actor is not permitted to perform this action right now."""


class WorkflowConfigurationError(WorkflowError):
    """Raised when required routing data (e.g. an assigned VP/CFO) is missing."""


def is_property_transaction(entity_classification: str) -> bool:
    return (entity_classification or "").strip().lower() == PROPERTY_CLASSIFICATION


def same_person_approver_controller(txn: dict) -> bool:
    approver   = txn.get("selected_approver_user_key")
    controller = txn.get("selected_controller_user_key")
    return approver is not None and approver == controller


def approval_tier_label(*, requires_vp: bool, requires_cfo: bool) -> str:
    """
    Display-only "Required Approval Tier" text derived purely from the resolved
    ApprovalRule's own Requires_VP/Requires_CFO booleans (Batch 7) — never from
    an amount threshold. Collapses the old cosmetic "Controller" mid-tier label
    (pre-Batch-7: $250k-$500k) into this same base label, since that band never
    actually required a different approval than the base tier — a deliberate
    simplification, not a routing change.
    """
    if requires_cfo:
        return "Vice President + CFO"
    if requires_vp:
        return "Vice President"
    return "Senior Accounting Manager / Assistant Controller"


def normalize_roles(role) -> set:
    """
    Accept either a single role code (str) or an iterable of role codes and
    return a plain set — mirrors authorization.normalize_roles() so both
    modules treat "one role" and "a role set" identically without importing
    each other (multi-role authorization refactor; see
    e_transaction_multi_role_authorization_refactor.md). A user's effective
    capability set is the UNION of what each individual role grants; no role
    implies or overrides another.
    """
    if isinstance(role, str):
        return {role}
    return set(role or [])


def authorize_action(*, role, user_key, txn: dict, action: str) -> None:
    """
    Raise UnauthorizedActionError unless the signed-in user's role SET
    (a single role code or an iterable of role codes — see
    authorization.normalize_roles()) authorizes `action` on `txn` right now.
    Authorized if ANY ONE held role provides a valid path AND that role's own
    assignment requirement is satisfied — a union of independent paths, never
    a combined/blended check across roles (Part 14/16 of the multi-role
    authorization refactor). No role implies or overrides another.

    `user_key` may be None (local dev bypass without a resolved AppUser identity),
    in which case only the role-level check is enforced — assignment-specific
    checks are enforced whenever a real user_key is available (Easy Auth/production).
    """
    roles = normalize_roles(role)
    status = txn.get("status")

    if status in TERMINAL_STATUSES:
        raise UnauthorizedActionError(f"This transaction is {status.lower()} and no longer accepts actions.")

    if action == ACTION_CANCEL:
        allowed = {"submitter", "sam", "controller", "treasury"} & roles
        if not allowed:
            raise UnauthorizedActionError("Your role cannot cancel this transaction.")
        if status not in CANCEL_ELIGIBLE_STATUSES:
            raise UnauthorizedActionError("This transaction can no longer be cancelled at its current stage.")
        if user_key is None:
            return

        def _cancel_assignee_ok(r):
            if r == "submitter":
                return user_key == txn.get("prepared_by_user_key")
            if r in ("sam", "treasury"):
                return user_key == txn.get("selected_approver_user_key")
            if r == "controller":
                return user_key == txn.get("selected_controller_user_key")
            return False

        if not any(_cancel_assignee_ok(r) for r in allowed):
            raise UnauthorizedActionError("You are not the assigned participant for this transaction.")
        return

    if action == ACTION_REQUESTER_RESPOND:
        if "submitter" not in roles:
            raise UnauthorizedActionError("Only the original requester can respond to this request.")
        if status != STATUS_MORE_INFO:
            raise UnauthorizedActionError("This transaction is not awaiting requester information.")
        if user_key is not None and user_key != txn.get("prepared_by_user_key"):
            raise UnauthorizedActionError("Only the original requester can respond to this request.")
        return

    if action in (ACTION_APPROVE, ACTION_MORE_INFO):
        # Treasury Manager may also act as the assigned Approver (confirmed org
        # requirement) — accepted alongside sam at the Pending Approver stage.
        stage_map = {
            STATUS_PENDING_APPROVER:   (("sam", "treasury"), "selected_approver_user_key"),
            STATUS_PENDING_CONTROLLER: (("controller",), "selected_controller_user_key"),
            STATUS_PENDING_VP:         (("vp",), "vp_approver_user_key"),
            STATUS_PENDING_CFO:        (("cfo",), "cfo_approver_user_key"),
        }
        expected = stage_map.get(status)
        if not expected:
            raise UnauthorizedActionError("This transaction is not awaiting an approval action right now.")
        expected_roles, assignee_field = expected
        if not (set(expected_roles) & roles):
            raise UnauthorizedActionError("Your role is not authorized to act at this stage.")
        if user_key is not None and user_key != txn.get(assignee_field):
            raise UnauthorizedActionError("You are not the assigned participant for this stage.")
        return

    if action in (ACTION_TREASURY_INITIATED, ACTION_TREASURY_RELEASED):
        if "treasury" not in roles:
            raise UnauthorizedActionError("Only Treasury may perform this action.")
        if status != STATUS_READY_FOR_TREASURY:
            raise UnauthorizedActionError("This transaction is not ready for Treasury.")
        return

    if action == ACTION_BANK_RELEASE:
        if status != STATUS_AWAITING_RELEASE:
            raise UnauthorizedActionError("This transaction is not awaiting bank release.")
        if not ({"controller", "vp"} & roles):
            raise UnauthorizedActionError("Only the designated Controller or VP may complete a bank release.")
        if user_key is not None and user_key != txn.get("bank_releaser_user_key"):
            raise UnauthorizedActionError("You are not the designated bank releaser for this transaction.")
        return

    if action == ACTION_MARK_COMPLETED:
        if "treasury" not in roles:
            raise UnauthorizedActionError("Only Treasury may mark this transaction completed.")
        if status != STATUS_TREASURY_RELEASED:
            raise UnauthorizedActionError("This transaction has not yet been released.")
        return

    raise UnauthorizedActionError("Unknown action.")


def authorize_reassignment(*, role, txn: dict) -> None:
    """
    Raise UnauthorizedActionError unless any role in `role` (a single role
    code or an iterable — see authorization.normalize_roles()) may reassign
    the transaction's currently active Approver/Controller stage.

    Enforced server-side regardless of what the UI shows/hides. Only Business
    Administrator, Treasury, and Controller may reassign, and only while the
    transaction is at a stage in REASSIGNMENT_STAGE_MAP (Pending Approver or
    Pending Controller) — reassignment never requires resubmission and does not
    change Current_Status.
    """
    roles = normalize_roles(role)
    status = txn.get("status")

    if status in TERMINAL_STATUSES:
        raise UnauthorizedActionError(f"This transaction is {status.lower()} and no longer accepts actions.")

    if not (set(REASSIGNMENT_ROLES) & roles):
        raise UnauthorizedActionError("Your role cannot reassign this transaction.")

    if status not in REASSIGNMENT_STAGE_MAP:
        raise UnauthorizedActionError("This transaction is not at a stage that supports reassignment.")


def determine_next_step(txn: dict, action: str):
    """
    Pure decision function. Given the transaction's current workflow-relevant
    fields and the action being reported, return:
        (new_status, new_owner_user_key, new_owner_role_label, satisfied_roles)

    Does not touch the database. Expected txn keys: status,
    selected_approver_user_key, selected_controller_user_key, vp_approver_user_key,
    cfo_approver_user_key, prepared_by_user_key, bank_releaser_user_key,
    requires_vp, requires_cfo, entity_classification, and (only for
    ACTION_REQUESTER_RESPOND) rfi_origin_status.
    """
    status = txn.get("status")

    if action == ACTION_CANCEL:
        return STATUS_CANCELLED, None, None, []

    if action == ACTION_MORE_INFO:
        return STATUS_MORE_INFO, txn.get("prepared_by_user_key"), "Requester", []

    if action == ACTION_REQUESTER_RESPOND:
        # Return to the exact stage/person that requested the information —
        # never restart the transaction from the beginning.
        origin = txn.get("rfi_origin_status")
        if origin == STATUS_PENDING_CONTROLLER:
            return STATUS_PENDING_CONTROLLER, txn.get("selected_controller_user_key"), "Controller", []
        if origin == STATUS_PENDING_VP:
            return STATUS_PENDING_VP, txn.get("vp_approver_user_key"), "VP", []
        if origin == STATUS_PENDING_CFO:
            return STATUS_PENDING_CFO, txn.get("cfo_approver_user_key"), "CFO", []
        return STATUS_PENDING_APPROVER, txn.get("selected_approver_user_key"), "Approver", []

    if action == ACTION_APPROVE:
        if status == STATUS_PENDING_APPROVER:
            satisfied = ["Approver"]
            # Batch 7: honor the transaction's resolved ApprovalRule.Requires_Controller
            # (defaults to True — Controller is the base requirement for every rule in
            # the current live configuration). Same-person rule: one Approve also
            # satisfies Controller when they're the same assignee.
            if not txn.get("requires_controller", True):
                return _route_after_controller(txn, satisfied)
            if same_person_approver_controller(txn):
                satisfied.append("Controller")
                return _route_after_controller(txn, satisfied)
            return STATUS_PENDING_CONTROLLER, txn.get("selected_controller_user_key"), "Controller", satisfied

        if status == STATUS_PENDING_CONTROLLER:
            return _route_after_controller(txn, ["Controller"])

        if status == STATUS_PENDING_VP:
            if txn.get("requires_cfo"):
                cfo_key = txn.get("cfo_approver_user_key")
                if not cfo_key:
                    raise WorkflowConfigurationError(
                        "CFO approval is required but no CFO is assigned to this transaction."
                    )
                return STATUS_PENDING_CFO, cfo_key, "CFO", ["VP"]
            return STATUS_READY_FOR_TREASURY, None, "Treasury", ["VP"]

        if status == STATUS_PENDING_CFO:
            return STATUS_READY_FOR_TREASURY, None, "Treasury", ["CFO"]

        raise UnauthorizedActionError("This transaction is not awaiting an approval action right now.")

    if action == ACTION_TREASURY_INITIATED:
        if not is_property_transaction(txn.get("entity_classification")):
            raise UnauthorizedActionError("Treasury Initiated only applies to Property transactions.")
        return STATUS_AWAITING_RELEASE, _bank_releaser(txn), "Bank Releaser", []

    if action == ACTION_TREASURY_RELEASED:
        if is_property_transaction(txn.get("entity_classification")):
            raise UnauthorizedActionError("Property transactions use Treasury Initiated, not Treasury Released.")
        # Batch 5: Corporate release completes directly — no subsequent Bank
        # Release or manual Mark Completed step for transactions reaching this
        # action going forward (ACTION_MARK_COMPLETED/STATUS_TREASURY_RELEASED
        # remain defined only for any pre-existing transaction already sitting
        # in that status).
        return STATUS_COMPLETED, None, None, []

    if action == ACTION_BANK_RELEASE:
        return STATUS_COMPLETED, None, None, []

    if action == ACTION_MARK_COMPLETED:
        return STATUS_COMPLETED, None, None, []

    raise UnauthorizedActionError("Unknown action.")


def _route_after_controller(txn: dict, satisfied: list):
    if txn.get("requires_vp"):
        vp_key = txn.get("vp_approver_user_key")
        if not vp_key:
            raise WorkflowConfigurationError(
                "VP approval is required but no VP is assigned to this transaction."
            )
        return STATUS_PENDING_VP, vp_key, "VP", satisfied
    if txn.get("requires_cfo"):
        cfo_key = txn.get("cfo_approver_user_key")
        if not cfo_key:
            raise WorkflowConfigurationError(
                "CFO approval is required but no CFO is assigned to this transaction."
            )
        return STATUS_PENDING_CFO, cfo_key, "CFO", satisfied
    return STATUS_READY_FOR_TREASURY, None, "Treasury", satisfied


def _bank_releaser(txn: dict):
    """CFO must never become the final Property bank releaser — VP if assigned, else Controller."""
    vp_key = txn.get("vp_approver_user_key")
    if vp_key:
        return vp_key
    return txn.get("selected_controller_user_key")
