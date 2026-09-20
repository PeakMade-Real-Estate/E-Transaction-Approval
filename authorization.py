"""
Centralized transaction-VISIBILITY authorization — the single source of truth
for "can this signed-in user see this transaction at all". Used by the
dashboard query, transaction detail route, and banking reveal endpoints
(Batch 4) so the same rule is never re-implemented differently in each place.

Distinct from workflow.py's authorize_action()/authorize_reassignment(),
which govern who may PERFORM a workflow action right now. Visibility and
action authorization are related but separate — e.g. Treasury may VIEW a
Property transaction awaiting Controller/VP bank release without being able
to ACT on that release step.

Dev-mode note: current_app_user_key() only resolves a real AppUser_Key via
Easy Auth (production) — the local dev role-switcher bypass has no per-user
identity at all (session only stores a role string), so user_key is None in
that mode. Per the SAME established convention already used by
workflow.authorize_action() ("user_key may be None ... assignment-specific
checks are enforced whenever a real user_key is available"), this module
falls back to the pre-Batch-4 role+status-only visibility when user_key is
None, since there is no per-user identity to scope by in local dev. Real
per-user/per-accounting-group scoping applies whenever a real user_key is
resolved (production, via Easy Auth).
"""

import workflow

# Business Admin is the one role given unrestricted visibility, matching its
# documented "monitors queues, reassigns participants, maintains routing"
# function (templates/role_select.html) — an admin/monitoring role, not a
# processing role tied to a personal assignment or accounting group.
UNRESTRICTED_VISIBILITY_ROLES = {"business_admin"}

# Roles with no visibility requirement defined in Batch 4 (IT Administrator,
# Treasury Bank Maintenance backup) default to NO transaction visibility —
# the safe, non-invented default, not an assumption of broad access.
NO_DEFINED_VISIBILITY_ROLES = {"it_admin", "treasury_bank_admin"}

# Treasury sees a transaction once it enters Treasury's processing phase,
# regardless of who currently owns the actionable next step — e.g. Property
# transactions Awaiting Bank Release remain visible to Treasury even after
# ownership transfers to Controller/VP for the final release action.
TREASURY_VISIBLE_STATUSES = {
    workflow.STATUS_READY_FOR_TREASURY,
    workflow.STATUS_TREASURY_INITIATED,
    workflow.STATUS_AWAITING_RELEASE,
    workflow.STATUS_TREASURY_RELEASED,
    workflow.STATUS_COMPLETED,
}

# Roles whose accounting-group scoping can broaden their view (in addition to
# their own personal assignment). Requester/Approver/Treasury/Business Admin
# do not use accounting-group scoping.
GROUP_SCOPED_ROLES = {"controller", "vp", "cfo"}


def normalize_roles(role) -> set:
    """
    Accept either a single role code (str) or an iterable of role codes and
    return a plain set — the multi-role authorization refactor's core
    normalization so every caller (old single-role, new full role-set) works
    identically. A user's effective capability set is the UNION of what each
    individual role grants (see e_transaction_multi_role_authorization_refactor.md);
    no role implies or overrides another.
    """
    if isinstance(role, str):
        return {role}
    return set(role or [])


def _dev_bypass_visible(role, status: str) -> bool:
    """
    Role+status-only visibility used when there is no resolved AppUser
    identity (local dev role switcher). Mirrors the pre-Batch-4 dashboard
    scoping exactly — the only thing that changes for a real Easy Auth
    identity is that real per-user/per-group scoping applies instead.
    `role` may be a single role code or an iterable of role codes — visible
    if ANY held role would make it visible (union, not precedence).
    """
    roles = normalize_roles(role)

    def _visible_for(r):
        if r == "submitter":
            return True  # no way to know "whose" session request this is beyond role, in dev bypass
        if r == "sam":
            return status == workflow.STATUS_PENDING_APPROVER
        if r == "controller":
            return status == workflow.STATUS_PENDING_CONTROLLER
        if r == "vp":
            return status == workflow.STATUS_PENDING_VP
        if r == "cfo":
            return status == workflow.STATUS_PENDING_CFO
        if r == "treasury":
            return status in TREASURY_VISIBLE_STATUSES
        return False

    return any(_visible_for(r) for r in roles)


def _can_view_as_role(role: str, user_key, txn: dict, authorized_group_keys) -> bool:
    """One individual role's visibility predicate — unchanged rule-for-rule
    from the original single-role can_view_transaction(); extracted so the
    full role set can each be evaluated independently and unioned.

    `authorized_group_keys` may be a flat list/set (applies to whichever role
    is being checked — fine when only one role is ever in play) or a dict
    {role_code: [group_keys]} for precise multi-role callers, so one role's
    AccountingGroup mapping is never applied to a different role's check
    (multi-role refactor Part 23)."""
    if role in UNRESTRICTED_VISIBILITY_ROLES:
        return True
    if role in NO_DEFINED_VISIBILITY_ROLES:
        return False

    # Batch 8: a Draft (requester still preparing the request) is never visible
    # to a processing role even if a real Selected*/VP/CFO_User_Key happens to
    # already be stored on the row (required by NOT NULL schema constraints —
    # see db.create_draft()) — only the owning submitter (and Business Admin,
    # already unrestricted above) may see a Draft.
    if txn.get("status") == workflow.STATUS_DRAFT and role != "submitter":
        return False

    authorized_group_keys = authorized_group_keys or []
    if isinstance(authorized_group_keys, dict):
        groups_for_role = authorized_group_keys.get(role, [])
    else:
        groups_for_role = authorized_group_keys
    group_key = txn.get("accounting_group_key")
    in_group = role in GROUP_SCOPED_ROLES and group_key is not None and group_key in groups_for_role

    if role == "submitter":
        return txn.get("prepared_by_user_key") == user_key
    if role == "sam":
        return txn.get("selected_approver_user_key") == user_key
    if role == "controller":
        return txn.get("selected_controller_user_key") == user_key or in_group
    if role == "vp":
        return txn.get("vp_approver_user_key") == user_key or in_group
    if role == "cfo":
        return txn.get("cfo_approver_user_key") == user_key or in_group
    if role == "treasury":
        return txn.get("status") in TREASURY_VISIBLE_STATUSES
    return False


def can_view_transaction(*, role, user_key, txn: dict, authorized_group_keys=None) -> bool:
    """
    Return True if any role in `role` (a single role code or an iterable of
    role codes — see normalize_roles()) may view `txn` — a UNION across the
    caller's full role set, not a single active role. Must be enforced as a
    real server-side object check on every direct route (dashboard rows being
    properly scoped is not sufficient by itself — see Part 12).

    Expected txn keys: prepared_by_user_key, selected_approver_user_key,
    selected_controller_user_key, vp_approver_user_key, cfo_approver_user_key,
    status, accounting_group_key (BusinessEntity.AccountingGroup_Key via
    ETransaction.Entity_Key — may be None).
    """
    roles = normalize_roles(role)
    if user_key is None:
        return _dev_bypass_visible(roles, txn.get("status"))
    return any(_can_view_as_role(r, user_key, txn, authorized_group_keys) for r in roles)
