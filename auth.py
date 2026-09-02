"""
Identity/role resolution — Azure App Service Easy Auth (production) with a local
development bypass (session-based role switcher).

Easy Auth (App Service Authentication with Entra ID) injects these request headers
after a user signs in:
    X-MS-CLIENT-PRINCIPAL-ID    — the user's object ID
    X-MS-CLIENT-PRINCIPAL-NAME  — usually the UPN/email
    X-MS-CLIENT-PRINCIPAL       — base64 JSON with a "claims" array; App Role
                                   assignments appear as role claims once the
                                   corresponding Entra app roles exist and are
                                   assigned to the user/group.

DEV_LOGIN_ENABLED (.env) controls the local bypass:
    true  (default) — when no Easy Auth headers are present, fall back to the
                       session-based role switcher (role_select/switch_role),
                       letting a developer act as any role at any time.
    false            — no Easy Auth headers means no access (403). Set this in
                       Azure once Easy Auth app roles are configured and verified.

Easy Auth headers always take priority over the local bypass when present.
"""

import base64
import json
import os

from flask import request, session

_ROLE_CLAIM_TYPES = {
    "roles",
    "http://schemas.microsoft.com/ws/2008/06/identity/claims/role",
}

# Entra ID App Role "value" -> internal application role code.
# Keep in sync with the App Roles configured on the app registration.
AZURE_ROLE_TO_APP_ROLE = {
    "Submitter":               "submitter",
    "Approver":                "sam",
    "Controller":              "controller",
    "VP":                      "vp",
    "CFO":                     "cfo",
    "TreasuryManager":         "treasury",
    "BusinessAdministrator":   "business_admin",
    "ITAdministrator":         "it_admin",
    "TreasuryBankMaintenance": "treasury_bank_admin",
}


def dev_login_enabled() -> bool:
    return os.environ.get("DEV_LOGIN_ENABLED", "true").lower() == "true"


def _parse_easy_auth_principal():
    """Return {"user_id", "display_name", "roles"} from Easy Auth headers, or None if absent."""
    principal_id = request.headers.get("X-MS-CLIENT-PRINCIPAL-ID")
    if not principal_id:
        return None

    display_name = request.headers.get("X-MS-CLIENT-PRINCIPAL-NAME", "")
    roles = []

    encoded = request.headers.get("X-MS-CLIENT-PRINCIPAL")
    if encoded:
        try:
            claims = json.loads(base64.b64decode(encoded)).get("claims", [])
        except Exception:
            claims = []
        for claim in claims:
            if claim.get("typ") in _ROLE_CLAIM_TYPES:
                mapped = AZURE_ROLE_TO_APP_ROLE.get(claim.get("val"))
                if mapped and mapped not in roles:
                    roles.append(mapped)

    return {"user_id": principal_id, "display_name": display_name, "roles": roles}


def current_identity() -> dict:
    """
    Return the effective identity for this request:
        {"user_id": str|None, "display_name": str, "roles": [app_role_code, ...], "source": str}

    source is one of "easy_auth", "dev", or "none".
    """
    principal = _parse_easy_auth_principal()
    if principal:
        return {**principal, "source": "easy_auth"}

    if dev_login_enabled():
        role = session.get("role")
        return {
            "user_id": "dev-local",
            "display_name": "Local Developer",
            "roles": [role] if role else [],
            "source": "dev",
        }

    return {"user_id": None, "display_name": "", "roles": [], "source": "none"}
