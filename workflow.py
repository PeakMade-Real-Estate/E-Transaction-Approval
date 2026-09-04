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

# ── Actions a route may report to the workflow service ───────
ACTION_APPROVE            = "approve"
ACTION_MORE_INFO          = "more_info"
ACTION_CANCEL             = "cancel"
ACTION_REQUESTER_RESPOND  = "requester_respond"
ACTION_TREASURY_INITIATED = "treasury_initiated"   # Property: Treasury hands off
ACTION_TREASURY_RELEASED  = "treasury_released"    # Corporate: Treasury performs final release
ACTION_BANK_RELEASE       = "bank_release"          # Property: Controller/VP completes release
ACTION_MARK_COMPLETED     = "mark_completed"        # Corporate: Treasury confirms completion

PROPERTY_CLASSIFICATION = "property"


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


def authorize_action(*, role: str, user_key, txn: dict, action: str) -> None:
    """
    Raise UnauthorizedActionError if the signed-in user is not both role- and
    assignment-eligible to perform `action` on `txn` right now.

    `user_key` may be None (local dev bypass without a resolved AppUser identity),
    in which case only the role-level check is enforced — assignment-specific
    checks are enforced whenever a real user_key is available (Easy Auth/production).
    """
    status = txn.get("status")

    if status in TERMINAL_STATUSES:
        raise UnauthorizedActionError(f"This transaction is {status.lower()} and no longer accepts actions.")

    if action == ACTION_CANCEL:
        if role not in ("submitter", "sam", "controller"):
            raise UnauthorizedActionError("Your role cannot cancel this transaction.")
        if status not in CANCEL_ELIGIBLE_STATUSES:
            raise UnauthorizedActionError("This transaction can no longer be cancelled at its current stage.")
        if user_key is not None:
            if role == "submitter" and user_key != txn.get("prepared_by_user_key"):
                raise UnauthorizedActionError("Only the original requester can cancel their own request.")
            if role == "sam" and user_key != txn.get("selected_approver_user_key"):
                raise UnauthorizedActionError("You are not the assigned Approver for this transaction.")
            if role == "controller" and user_key != txn.get("selected_controller_user_key"):
                raise UnauthorizedActionError("You are not the assigned Controller for this transaction.")
        return

    if action == ACTION_REQUESTER_RESPOND:
        if role != "submitter":
            raise UnauthorizedActionError("Only the original requester can respond to this request.")
        if status != STATUS_MORE_INFO:
            raise UnauthorizedActionError("This transaction is not awaiting requester information.")
        if user_key is not None and user_key != txn.get("prepared_by_user_key"):
            raise UnauthorizedActionError("Only the original requester can respond to this request.")
        return

    if action in (ACTION_APPROVE, ACTION_MORE_INFO):
        stage_map = {
            STATUS_PENDING_APPROVER:   ("sam", "selected_approver_user_key"),
            STATUS_PENDING_CONTROLLER: ("controller", "selected_controller_user_key"),
            STATUS_PENDING_VP:         ("vp", "vp_approver_user_key"),
            STATUS_PENDING_CFO:        ("cfo", "cfo_approver_user_key"),
        }
        expected = stage_map.get(status)
        if not expected:
            raise UnauthorizedActionError("This transaction is not awaiting an approval action right now.")
        expected_role, assignee_field = expected
        if role != expected_role:
            raise UnauthorizedActionError("Your role is not authorized to act at this stage.")
        if user_key is not None and user_key != txn.get(assignee_field):
            raise UnauthorizedActionError("You are not the assigned participant for this stage.")
        return

    if action in (ACTION_TREASURY_INITIATED, ACTION_TREASURY_RELEASED):
        if role != "treasury":
            raise UnauthorizedActionError("Only Treasury may perform this action.")
        if status != STATUS_READY_FOR_TREASURY:
            raise UnauthorizedActionError("This transaction is not ready for Treasury.")
        return

    if action == ACTION_BANK_RELEASE:
        if status != STATUS_AWAITING_RELEASE:
            raise UnauthorizedActionError("This transaction is not awaiting bank release.")
        if role not in ("controller", "vp"):
            raise UnauthorizedActionError("Only the designated Controller or VP may complete a bank release.")
        if user_key is not None and user_key != txn.get("bank_releaser_user_key"):
            raise UnauthorizedActionError("You are not the designated bank releaser for this transaction.")
        return

    if action == ACTION_MARK_COMPLETED:
        if role != "treasury":
            raise UnauthorizedActionError("Only Treasury may mark this transaction completed.")
        if status != STATUS_TREASURY_RELEASED:
            raise UnauthorizedActionError("This transaction has not yet been released.")
        return

    raise UnauthorizedActionError("Unknown action.")


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
            # Same-person rule: one Approve satisfies both Approver and Controller.
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
        return STATUS_TREASURY_RELEASED, None, None, []

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
