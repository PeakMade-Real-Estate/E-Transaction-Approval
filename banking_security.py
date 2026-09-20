"""
Centralized helpers for masking, revealing, and auditing sensitive banking data
(account numbers, routing numbers, tax IDs, transit/institution numbers).

The Flask application must never depend on SQL Dynamic Data Masking (DDM) as
its only protection. DDM only affects what SQL Server returns to a given
principal — the app's own principal currently has NO UNMASK grant (verified
live against Fabric SQL), so DDM already masks these values for us today, but
that is an accident of current permissions, not an application-layer
guarantee. This module makes masking authoritative and explicit at the
application layer too, so ordinary screens stay masked even if the SQL
identity is later granted narrower unmask access, and so a single reveal
surface can be added without scattering substring logic across templates.

Authorization model (per project decision): there is no separate
"BANKING_DATA_REVEAL" role. Reveal is contextual — a user may reveal sensitive
values for a record they are already authorized to VIEW:
  - Bank Account Management -> app.can_view_bank_accounts()
  - Transaction detail (originating/receiving banking) -> the same access the
    transaction detail page itself already requires (today: any authenticated
    role, enforced globally by app.require_role())
Editing remains a SEPARATE, stricter permission (app.can_edit_bank_accounts())
— being read-only for edits does not affect the ability to use Reveal.
"""

import logging

logger = logging.getLogger("banking_security")

# Allow-listed sensitive fields the reveal endpoints may serve, mapped to the
# real SQL column each request is permitted to resolve to. Client requests are
# validated against these keys only — arbitrary column names are never
# accepted from the browser.
REVEALABLE_BANK_ACCOUNT_FIELDS = {
    "account_number":    "AccountNumber",
    "routing_number":    "RoutingNumber",
    "tax_id":            "TaxIDNumber",
    "transit_number":    "TransitNumberCanada",
    "institution_number": "InstitutionNumberCanada",
}

REVEALABLE_BENEFICIARY_FIELDS = {
    "account_number": "Receiving_Account_Number",
    "routing_number": "Receiving_Routing_Number",
}

# Characters that only ever appear in a MASKED placeholder representation
# (DDM's partial()/default() output, or this module's own mask_*() output) —
# never in a genuine account/routing/tax-ID/transit/institution number.
_MASK_CHARACTERS = set("Xx*\u2022")


def mask_account_number(value: str) -> str:
    """Preserve the last 4 characters, mirroring the DB's own DDM policy (partial(0,'XXXX-XXXX-',4))."""
    if not value or len(value) < 4:
        return "********"
    return "********" + value[-4:]


def mask_routing_number(value: str) -> str:
    """Fully masked — mirrors the DB's own DDM policy (default()); no digits are ever revealed."""
    return "*********"


def mask_tax_id(value: str) -> str:
    """Fully masked — mirrors the DB's own DDM policy (default())."""
    return "*********"


def mask_transit_number(value: str) -> str:
    """Fully masked — treated as restricted, same as routing/tax ID."""
    return "****"


def mask_institution_number(value: str) -> str:
    """Fully masked — treated as restricted, same as routing/tax ID."""
    return "***"


_MASKERS = {
    "account_number": mask_account_number,
    "routing_number": mask_routing_number,
    "tax_id": mask_tax_id,
    "transit_number": mask_transit_number,
    "institution_number": mask_institution_number,
}


def mask_field(field_key: str, value: str) -> str:
    """Mask `value` using the masker registered for `field_key` (one of REVEALABLE_*_FIELDS' keys)."""
    masker = _MASKERS.get(field_key)
    return masker(value) if masker else "********"


def is_mask_placeholder(value) -> bool:
    """
    True if `value` looks like a masked/placeholder string (DDM output such as
    "XXXX-XXXX-0123"/"xxxx", or this module's own "********1234"/"***") rather
    than a genuine value a user is trying to save. Used to reject a crafted
    POST that tries to write a displayed masked placeholder back into SQL as
    if it were a real replacement value (Part 9/10 edit-safety rule).
    """
    if not value:
        return False
    return any(ch in _MASK_CHARACTERS for ch in str(value))


def audit_banking_data_reveal(*, actor_user_key, actor_role, object_type, object_key, field, success, reason=None):
    """
    Record a sensitive-data reveal attempt (successful or not) — a security-
    relevant event. NEVER includes the revealed value itself.

    No durable SQL audit table exists yet for this (etransactions has no
    generic security/audit table today — see /memories/repo/schema-facts.md).
    This is a temporary bridge for UAT: it emits a structured, explicit-field
    application log record (never a raw dict that could carry the revealed
    value alongside it) via the standard `logging` module, tagged so it's easy
    to find and migrate to a real durable table once one is provisioned.
    Durable SQL persistence of this audit trail remains a production follow-up.
    """
    logger.warning(
        "BANKING_DATA_REVEAL user_key=%s role=%s object_type=%s object_key=%s field=%s success=%s reason=%s",
        actor_user_key, actor_role, object_type, object_key, field, success, reason or "",
    )
