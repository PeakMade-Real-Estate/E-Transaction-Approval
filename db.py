"""
Database connection module — Microsoft Fabric SQL Database via Entra ID service principal.

Authenticates as the "E-Transaction Approval" service principal (AZURE_CLIENT_ID /
AZURE_TENANT_ID / AZURE_CLIENT_SECRET), scoped to the `etransactions` schema only.
No SQL username/password is used. Tokens and connections are acquired lazily —
nothing here runs at import time.
"""

import os
import struct
import time
from datetime import datetime
from decimal import Decimal

import pyodbc
from azure.identity import ClientSecretCredential

from workflow import STATUS_PENDING_APPROVER, STATUS_COMPLETED, stage_for_status
import workflow
import banking_security
import authorization

# Fixed schema — all app tables live under etransactions
DB_SCHEMA = "etransactions"

# Known tables in scope for this application
TABLES = (
    "AccountingGroup",
    "ApprovalRule",
    "AppUser",
    "AppUserRole",
    "Attachment",
    "BankAccount",
    "Beneficiary",
    "BeneficiaryBankInstruction",
    "BusinessEntity",
    "ETransaction",
    "TransactionComment",
    "TransactionVerification",
    "UserAvailability",
    "WorkflowAssignment",
    "WorkflowEvent",
)

# ODBC connection-attribute key for supplying an Entra access token (SQL_COPT_SS_ACCESS_TOKEN)
_SQL_COPT_SS_ACCESS_TOKEN = 1256
_SQL_TOKEN_SCOPE = "https://database.windows.net/.default"

_credential = None  # lazily created; ClientSecretCredential itself makes no network call


def _get_credential() -> ClientSecretCredential:
    """Return (and cache) the service-principal credential used for SQL token acquisition."""
    global _credential
    if _credential is None:
        try:
            _credential = ClientSecretCredential(
                tenant_id=os.environ["AZURE_TENANT_ID"],
                client_id=os.environ["AZURE_CLIENT_ID"],
                client_secret=os.environ["AZURE_CLIENT_SECRET"],
            )
        except KeyError as exc:
            raise RuntimeError(
                f"Missing required environment variable for SQL authentication: {exc}"
            ) from None
    return _credential


def get_connection():
    """
    Return an open pyodbc connection to the Fabric SQL Database, authenticated as the
    E-Transaction Approval service principal via Microsoft Entra ID.

    Caller is responsible for closing the connection.
    """
    server   = os.environ.get("DB_SERVER", "")
    database = os.environ.get("DB_NAME", "")
    if not server or not database:
        raise RuntimeError(
            "DB_SERVER and DB_NAME must be set in the environment before connecting."
        )

    try:
        token = _get_credential().get_token(_SQL_TOKEN_SCOPE)
    except Exception as exc:
        raise RuntimeError(f"Failed to acquire an Entra ID access token: {exc}") from exc

    token_bytes  = token.token.encode("utf-16-le")
    token_struct = struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)

    conn_str = (
        "DRIVER={ODBC Driver 18 for SQL Server};"
        f"SERVER={server};"
        f"DATABASE={database};"
        "Encrypt=yes;"
        "TrustServerCertificate=no;"
    )

    try:
        return pyodbc.connect(
            conn_str,
            attrs_before={_SQL_COPT_SS_ACCESS_TOKEN: token_struct},
            timeout=120,
        )
    except pyodbc.Error as exc:
        raise RuntimeError(f"Failed to connect to the Fabric SQL Database: {exc}") from exc


def test_connection() -> bool:
    """
    Read-only connectivity check for the deployed environment. Confirms token
    acquisition, ODBC Driver 18 availability, DB_SERVER/DB_NAME reachability, and
    SELECT permission on etransactions.ETransaction. Returns True on success and
    raises on any failure. Never returns or logs the row contents.
    """
    connection = get_connection()
    try:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT TOP (1) Transaction_Key FROM etransactions.ETransaction ORDER BY Transaction_Key"
        )
        cursor.fetchone()  # existence/permission check only — value intentionally discarded
        return True
    finally:
        connection.close()


def test_permission_boundary_negative() -> bool:
    """
    One-time manual security validation — NOT for startup or health checks.
    Confirms the service principal is scoped to `etransactions` only by querying a
    table outside that schema, which is EXPECTED TO FAIL with a permission error.
    Returns True if access was correctly denied, False if the query unexpectedly succeeded.
    """
    connection = get_connection()
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT TOP (1) * FROM riskgate.user_identity")
        cursor.fetchone()
        return False  # unexpected — this should have raised a permission error
    except pyodbc.Error:
        return True  # expected outcome: permission denied outside etransactions
    finally:
        connection.close()



def table(name: str) -> str:
    """Return a fully schema-qualified table name, e.g. table('ETransaction') -> '[etransactions].[ETransaction]'"""
    return f"[{DB_SCHEMA}].[{name}]"


class WorkflowConflictError(Exception):
    """Raised when a transaction's status no longer matches the expected from_status
    at the moment of update — i.e. the action was already processed (double-click,
    retry, or a concurrent request)."""


class ApprovalRuleConfigurationError(Exception):
    """
    Raised by resolve_approval_rule() when the live ApprovalRule configuration cannot
    deterministically route a transaction amount — zero or more than one active rule
    matched. Fails safely rather than guessing/falling back to hard-coded thresholds
    (Batch 7 Part 8).
    """


def get_app_user_by_entra_object_id(entra_object_id: str):
    """Resolve the signed-in Easy Auth identity to an AppUser row, or None if not found."""
    if not entra_object_id:
        return None
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT User_Key, Display_Name, Email FROM etransactions.AppUser "
            "WHERE Entra_Object_ID = ? AND Active_Status = 1",
            [entra_object_id],
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {"user_key": row[0], "display_name": row[1], "email": row[2]}
    finally:
        conn.close()


def get_app_user_by_key(user_key):
    """
    Resolve an AppUser by User_Key (active only) — used for the local-development
    "acting as" identity selection (Batch 7 Part 5), a full-record analog of
    get_app_user_by_entra_object_id() for the dev-mode identity path.
    """
    if not user_key:
        return None
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT User_Key, Display_Name, Email FROM etransactions.AppUser "
            "WHERE User_Key = ? AND Active_Status = 1",
            [user_key],
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {"user_key": row[0], "display_name": row[1], "email": row[2]}
    finally:
        conn.close()


def resolve_approval_rule(amount):
    """
    Resolve the single active ApprovalRule that applies to `amount` — the one
    source of truth for Controller/VP/CFO routing requirements (Batch 7 Part 8).
    Uses Decimal for the comparison; never float. Raises
    ApprovalRuleConfigurationError if zero or more than one active/effective rule
    matches — never guesses using the old hard-coded Python thresholds.
    """
    amount_dec = Decimal(str(amount))
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT ApprovalRule_Key, Requires_Approver, Requires_Controller, Requires_VP, Requires_CFO "
            "FROM etransactions.ApprovalRule "
            "WHERE Is_Active = 1 AND Min_Amount <= ? AND (Max_Amount IS NULL OR Max_Amount >= ?) "
            "AND (Effective_Start_Date IS NULL OR Effective_Start_Date <= CAST(GETDATE() AS DATE)) "
            "AND (Effective_End_Date IS NULL OR Effective_End_Date >= CAST(GETDATE() AS DATE))",
            [amount_dec, amount_dec],
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    if len(rows) == 0:
        raise ApprovalRuleConfigurationError(
            f"No active ApprovalRule matches amount {amount_dec}; submission cannot be routed."
        )
    if len(rows) > 1:
        raise ApprovalRuleConfigurationError(
            f"Multiple active ApprovalRule rows match amount {amount_dec}; configuration is ambiguous."
        )
    row = rows[0]
    return {
        "approval_rule_key":   row[0],
        "requires_approver":   bool(row[1]),
        "requires_controller": bool(row[2]),
        "requires_vp":         bool(row[3]),
        "requires_cfo":        bool(row[4]),
    }


def get_transaction_for_workflow(request_id: str):
    """
    Lean fetch of the raw key/flag columns needed for workflow decisions (no
    display-name joins — see get_request_detail() for the display-oriented fetch).
    Returns None if the request is not found.

    Requires_Controller is read via the transaction's OWN snapshotted
    ApprovalRule_Key (Batch 7) — not re-resolved against the current amount —
    so routing always reflects the rule in effect at submission time, even if
    ApprovalRule configuration changes later. Defaults to True (Controller
    always required) if the transaction has no linked rule.
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                t.Transaction_Key, t.Request_ID, t.Current_Status,
                t.PreparedBy_User_Key, t.SelectedApprover_User_Key, t.SelectedController_User_Key,
                t.VPApprover_User_Key, t.CFOApprover_User_Key, t.CurrentOwner_User_Key,
                t.BankReleaser_User_Key, t.Requires_VP, t.Requires_CFO, t.Amount,
                ISNULL(be.Classification, '') AS Entity_Classification,
                be.AccountingGroup_Key,
                ar.Requires_Controller
            FROM etransactions.ETransaction t
            LEFT JOIN etransactions.BusinessEntity be ON be.Entity_Key = t.Entity_Key
            LEFT JOIN etransactions.ApprovalRule ar ON ar.ApprovalRule_Key = t.ApprovalRule_Key
            WHERE t.Request_ID = ?
            """,
            [request_id],
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [c[0] for c in cur.description]
        d = dict(zip(cols, row))
    finally:
        conn.close()

    return {
        "transaction_key":              d["Transaction_Key"],
        "request_id":                   d["Request_ID"],
        "status":                       d["Current_Status"],
        "prepared_by_user_key":         d["PreparedBy_User_Key"],
        "selected_approver_user_key":   d["SelectedApprover_User_Key"],
        "selected_controller_user_key": d["SelectedController_User_Key"],
        "vp_approver_user_key":         d["VPApprover_User_Key"],
        "cfo_approver_user_key":        d["CFOApprover_User_Key"],
        "current_owner_user_key":       d["CurrentOwner_User_Key"],
        "bank_releaser_user_key":       d["BankReleaser_User_Key"],
        "requires_vp":                  bool(d["Requires_VP"]),
        "requires_cfo":                 bool(d["Requires_CFO"]),
        "requires_controller":          bool(d["Requires_Controller"]) if d["Requires_Controller"] is not None else True,
        "amount":                       float(d["Amount"] or 0),
        "entity_classification":        d["Entity_Classification"],
        "accounting_group_key":         d["AccountingGroup_Key"],
    }


def get_last_rfi_event_datetime(transaction_key):
    """
    Return the Event_DateTime of the most recent Request More Information
    WorkflowEvent for this transaction, or None. Used to confirm the requester
    attached a new file after the RFI was raised before allowing
    requester_respond (app.py's _handle_sql_workflow_action) when no comment
    was provided either.
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT TOP (1) Event_DateTime
            FROM etransactions.WorkflowEvent
            WHERE Transaction_Key = ? AND Event_Type = ?
            ORDER BY Event_DateTime DESC
            """,
            [transaction_key, workflow.EVENT_RFI_REQUESTED],
        )
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def get_last_rfi_origin_status(transaction_key):
    """
    Return the From_Status of the most recent Request More Information WorkflowEvent
    for this transaction, or None. Used to route a requester_respond action back to
    the exact stage that asked for more information, rather than restarting at
    Pending Approver — see workflow.determine_next_step()'s ACTION_REQUESTER_RESPOND branch.
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT TOP (1) From_Status
            FROM etransactions.WorkflowEvent
            WHERE Transaction_Key = ? AND Event_Type = ?
            ORDER BY Event_DateTime DESC
            """,
            [transaction_key, workflow.EVENT_RFI_REQUESTED],
        )
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _eligible_role_holder_keys(role_code, team=None):
    """
    Distinct User_Keys of ACTIVE users holding an ACTIVE, currently effective `role_code` role. DISTINCT collapses the
    several role rows a user has (one per accounting group). When `team` is given it must equal AppUser.Team exactly -
    unlike get_reassignment_candidates(), a user with a blank Team is NOT included.
    """
    sql = (
        "SELECT DISTINCT u.User_Key FROM etransactions.AppUserRole r "
        "JOIN etransactions.AppUser u ON u.User_Key = r.User_Key "
        "WHERE r.Role_Code = ? AND r.Is_Active = 1 AND u.Active_Status = 1 "
        "AND (r.Effective_Start_Date IS NULL OR r.Effective_Start_Date <= CAST(GETDATE() AS DATE)) "
        "AND (r.Effective_End_Date IS NULL OR r.Effective_End_Date >= CAST(GETDATE() AS DATE))"
    )
    params = [role_code]
    if team is not None:
        sql += " AND u.Team = ?"
        params.append(team)
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        return sorted(row[0] for row in cur.fetchall())
    finally:
        conn.close()


def get_eligible_vp_user_keys(team):
    """Distinct active VP users whose AppUser.Team equals `team` (workflow.TEAM_CORPORATE / TEAM_PROPERTY)."""
    if not (team or "").strip():
        raise ValueError("A team is required to look up the eligible VP.")
    return _eligible_role_holder_keys("vp", team)


def get_eligible_cfo_user_keys():
    """Distinct active CFO users. The CFO is company-wide, so AppUser.Team is deliberately not considered."""
    return _eligible_role_holder_keys("cfo")


def get_requester_controller_context(user_key):
    """
    {"is_controller": bool, "team": AppUser.Team or None} for an ACTIVE AppUser, or None if there is no such user.
    is_controller is true when the user holds an active, currently effective 'controller' AppUserRole (whatever else
    they hold, and whatever team they are on); the Team is returned as stored so the caller can fail safely on a
    blank or unexpected value.
    """
    if not user_key:
        return None
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT u.Team, "
            "MAX(CASE WHEN r.Role_Code = 'controller' THEN 1 ELSE 0 END) "
            "FROM etransactions.AppUser u "
            "LEFT JOIN etransactions.AppUserRole r ON r.User_Key = u.User_Key AND r.Is_Active = 1 "
            "AND (r.Effective_Start_Date IS NULL OR r.Effective_Start_Date <= CAST(GETDATE() AS DATE)) "
            "AND (r.Effective_End_Date IS NULL OR r.Effective_End_Date >= CAST(GETDATE() AS DATE)) "
            "WHERE u.User_Key = ? AND u.Active_Status = 1 "
            "GROUP BY u.Team",
            [user_key],
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {"is_controller": bool(row[1]), "team": (row[0] or "").strip() or None}
    finally:
        conn.close()


def get_eligible_controllers(team):
    """
    Distinct active Controllers ({"user_key", "display_name"}) whose AppUser.Team equals `team` exactly. Same strict
    eligibility as the VP lookup: an active, currently effective 'controller' role; a blank Team is never eligible.
    """
    if not (team or "").strip():
        raise ValueError("A team is required to look up the eligible Controller.")
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT DISTINCT u.User_Key, u.Display_Name FROM etransactions.AppUserRole r "
            "JOIN etransactions.AppUser u ON u.User_Key = r.User_Key "
            "WHERE r.Role_Code = 'controller' AND r.Is_Active = 1 AND u.Active_Status = 1 AND u.Team = ? "
            "AND (r.Effective_Start_Date IS NULL OR r.Effective_Start_Date <= CAST(GETDATE() AS DATE)) "
            "AND (r.Effective_End_Date IS NULL OR r.Effective_End_Date >= CAST(GETDATE() AS DATE)) "
            "ORDER BY u.User_Key",
            [team],
        )
        return [{"user_key": row[0], "display_name": row[1]} for row in cur.fetchall()]
    finally:
        conn.close()


def advance_transaction_workflow(
    transaction_key, *, from_status, new_status, new_owner_user_key,
    actor_user_key, actor_role, action, workflow_role=None,
    comments=None, bank_releaser_user_key=None,
    vp_approver_user_key=None, cfo_approver_user_key=None,
):
    """
    Atomically advance a transaction's workflow state:
      1. Update ETransaction.Current_Status/Current_Workflow_Stage/CurrentOwner_User_Key,
         conditioned on the expected from_status (optimistic concurrency — guards
         against double-click/retry/duplicate processing). Current_Workflow_Stage
         is derived from new_status via workflow.stage_for_status() in this SAME
         statement, so Status/Stage/Owner can never go out of sync with each other.
      2. Close out the prior current WorkflowAssignment and insert a new one for
         the new owner (if any), preserving assignment history.
      3. Insert one WorkflowEvent row per canonical business event this action
         produces (workflow.events_for_action(), Batch 6) — e.g. an Approve at
         Pending Approver writes BOTH APPROVER_APPROVED and CONTROLLER_ASSIGNED,
         so Power Automate never has to infer the stage from a generic "approve"
         event. All rows from one call share From_Status/To_Status/timestamp; the
         human comment (if any) attaches only to the first/primary event.

    Raises WorkflowConflictError if the transaction's status no longer matches
    from_status (already advanced by another request) — no rows are written.
    """
    from datetime import datetime as _dt
    now = _dt.now()

    conn = get_connection()
    try:
        cur = conn.cursor()

        set_clauses = [
            "Current_Status = ?", "Current_Workflow_Stage = ?",
            "CurrentOwner_User_Key = ?", "Modified_DateTime = ?",
        ]
        params = [new_status, stage_for_status(new_status), new_owner_user_key, now]
        if bank_releaser_user_key is not None:
            set_clauses.append("BankReleaser_User_Key = ?")
            params.append(bank_releaser_user_key)
        # A newly resolved VP/CFO is written by the SAME statement that moves the transaction into that stage, and
        # only while the column is still empty - an existing assignment is never overwritten.
        extra_where = ""
        if vp_approver_user_key is not None:
            set_clauses.append("VPApprover_User_Key = ?")
            params.append(vp_approver_user_key)
            extra_where += " AND VPApprover_User_Key IS NULL"
        if cfo_approver_user_key is not None:
            set_clauses.append("CFOApprover_User_Key = ?")
            params.append(cfo_approver_user_key)
            extra_where += " AND CFOApprover_User_Key IS NULL"
        params.extend([transaction_key, from_status])

        cur.execute(
            f"UPDATE etransactions.ETransaction SET {', '.join(set_clauses)} "
            f"WHERE Transaction_Key = ? AND Current_Status = ?{extra_where}",
            params,
        )
        if cur.rowcount == 0:
            conn.rollback()
            raise WorkflowConflictError(
                "This transaction has already moved past the expected status; action not applied."
            )

        # Close out the prior current assignment regardless; only insert a new
        # one when there is a specific new owner (queue-based stages have none).
        cur.execute(
            "UPDATE etransactions.WorkflowAssignment SET Is_Current = 0, End_DateTime = ? "
            "WHERE Transaction_Key = ? AND Is_Current = 1",
            [now, transaction_key],
        )
        new_assignment_key = None
        if new_owner_user_key is not None and workflow_role is not None:
            cur.execute(
                "INSERT INTO etransactions.WorkflowAssignment ("
                "  Transaction_Key, Workflow_Role, Assigned_User_Key,"
                "  Assigned_By_User_Key, Assignment_Source,"
                "  Assigned_DateTime, Is_Current"
                ") OUTPUT INSERTED.Assignment_Key VALUES (?,?,?,?,?,?,1)",
                [transaction_key, workflow_role, new_owner_user_key,
                 actor_user_key, "Workflow", now],
            )
            new_assignment_key = cur.fetchone()[0]

        events = workflow.events_for_action(from_status=from_status, action=action, new_status=new_status)
        for i, event_type in enumerate(events):
            # Decision text may be stage-specific (e.g. "Controller Approved") even
            # though Event_Type stays the established "approve" literal for Power
            # Automate — see workflow.friendly_event_label().
            decision = workflow.friendly_event_label(event_type, from_status)
            related_key = new_assignment_key if event_type in workflow.ASSIGNMENT_LINKED_EVENT_TYPES else None
            cur.execute(
                "INSERT INTO etransactions.WorkflowEvent ("
                "  Transaction_Key, Actor_User_Key, Actor_Role,"
                "  Event_Type, Decision, From_Status, To_Status,"
                "  Event_DateTime, Comments_Reason, Related_Assignment_Key"
                ") VALUES (?,?,?,?,?,?,?,?,?,?)",
                [transaction_key, actor_user_key, actor_role, event_type, decision,
                 from_status, new_status, now, comments if i == 0 else None, related_key],
            )

        conn.commit()
    except WorkflowConflictError:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def add_transaction_comment(transaction_key, *, author_user_key, comment_type, comment_text):
    """
    Insert a TransactionComment row. Author_User_Key is NOT NULL in the schema —
    callers must have a resolved AppUser identity (not available in the local dev
    role-switcher bypass without Easy Auth); comment_text must be non-empty.
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO etransactions.TransactionComment ("
            "  Transaction_Key, Author_User_Key, Comment_Type,"
            "  Comment_Text, Created_DateTime"
            ") OUTPUT INSERTED.Comment_Key VALUES (?,?,?,?,?)",
            [transaction_key, author_user_key, comment_type, comment_text, datetime.now()],
        )
        comment_key = cur.fetchone()[0]
        conn.commit()
        return comment_key
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_reassignment_candidates(role_code: str, team: str = None):
    """
    Return active AppUsers holding `role_code` in AppUserRole, for the
    reassignment replacement dropdown (e.g. role_code='sam' for Approver,
    'controller' for Controller — see workflow.REASSIGNMENT_STAGE_MAP) and for
    the intake form's Approver/Controller candidate lists.

    When `team` is given (e.g. workflow.TEAM_PROPERTY/TEAM_CORPORATE), rows
    are restricted to that AppUser.Team OR a blank/NULL Team — an AppUser
    with no Team on file (e.g. local dev/test fixtures) is never excluded by
    the team filter, only a Team that actively mismatches excludes a row.
    Pass team=None (default) to return every eligible holder of role_code
    regardless of team.
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        sql = (
            "SELECT DISTINCT u.User_Key, u.Display_Name, u.Team "
            "FROM [etransactions].[AppUserRole] r "
            "JOIN [etransactions].[AppUser] u ON u.User_Key = r.User_Key "
            "WHERE r.Role_Code = ? AND r.Is_Active = 1 AND u.Active_Status = 1"
        )
        params = [role_code]
        if team:
            sql += " AND (u.Team = ? OR u.Team IS NULL OR u.Team = '')"
            params.append(team)
        sql += " ORDER BY u.Display_Name"
        cur.execute(sql, params)
        return [{"user_key": r[0], "display_name": r[1], "team": r[2] or ""} for r in cur.fetchall()]
    finally:
        conn.close()


def reassign_transaction_participant(
    transaction_key, *, expected_status, stage_field, workflow_role,
    prior_user_key, new_user_key, actor_user_key, actor_role,
    event_type, decision, reason,
):
    """
    Reassign the active Approver or Controller on a transaction — status and
    all prior approval history are preserved; this never advances the workflow.

    Conditioned on the expected current status AND current assignee (optimistic
    concurrency, same double-click/retry guard as advance_transaction_workflow).
    Closes out the prior current WorkflowAssignment and opens a new one
    (Assignment_Source='Reassignment', Reassignment_Reason=reason), then logs a
    WorkflowEvent linked to that new assignment via Related_Assignment_Key.

    Raises WorkflowConflictError if the transaction's status or the stage's
    assignee no longer match what the caller last read — no rows are written.
    """
    fk_column = {
        "selected_approver_user_key":   "SelectedApprover_User_Key",
        "selected_controller_user_key": "SelectedController_User_Key",
    }[stage_field]

    now = datetime.now()

    conn = get_connection()
    try:
        cur = conn.cursor()

        cur.execute(
            f"UPDATE etransactions.ETransaction "
            f"SET {fk_column} = ?, CurrentOwner_User_Key = ?, Modified_DateTime = ? "
            f"WHERE Transaction_Key = ? AND Current_Status = ? AND {fk_column} = ?",
            [new_user_key, new_user_key, now, transaction_key, expected_status, prior_user_key],
        )
        if cur.rowcount == 0:
            conn.rollback()
            raise WorkflowConflictError(
                "This transaction's stage or assignee has already changed; reassignment not applied."
            )

        cur.execute(
            "UPDATE etransactions.WorkflowAssignment SET Is_Current = 0, End_DateTime = ? "
            "WHERE Transaction_Key = ? AND Is_Current = 1",
            [now, transaction_key],
        )
        cur.execute(
            "INSERT INTO etransactions.WorkflowAssignment ("
            "  Transaction_Key, Workflow_Role, Assigned_User_Key,"
            "  Assigned_By_User_Key, Assignment_Source,"
            "  Assigned_DateTime, Is_Current, Reassignment_Reason"
            ") OUTPUT INSERTED.Assignment_Key VALUES (?,?,?,?,?,?,1,?)",
            [transaction_key, workflow_role, new_user_key,
             actor_user_key, "Reassignment", now, reason],
        )
        assignment_key = cur.fetchone()[0]

        def _display_name(user_key):
            cur.execute(
                "SELECT Display_Name FROM etransactions.AppUser WHERE User_Key = ?",
                [user_key],
            )
            row = cur.fetchone()
            return row[0] if row else "Unknown"

        comment = (
            f"{workflow_role} reassigned from {_display_name(prior_user_key)} "
            f"to {_display_name(new_user_key)}: {reason}"
        )

        cur.execute(
            "INSERT INTO etransactions.WorkflowEvent ("
            "  Transaction_Key, Actor_User_Key, Actor_Role,"
            "  Event_Type, Decision, From_Status, To_Status,"
            "  Event_DateTime, Comments_Reason, Related_Assignment_Key"
            ") VALUES (?,?,?,?,?,?,?,?,?,?)",
            [transaction_key, actor_user_key, actor_role,
             event_type, decision, expected_status, expected_status,
             now, comment, assignment_key],
        )

        conn.commit()
    except WorkflowConflictError:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────
#  Query helpers
# ─────────────────────────────────────────────────────────────

_DASHBOARD_SQL_BASE = """
SELECT
    t.Transaction_Key                          AS transaction_key,
    t.Request_ID                               AS request_id,
    t.Property_Department_Text                 AS property_dept,
    t.Entity_ID_Text                           AS property_code,
    t.Request_Type                             AS request_type,
    t.Treasury_Service_Date                    AS treasury_service_date,
    t.Prepared_Date                            AS prepared_date,
    t.Submitted_Date                           AS submitted_date,
    t.Modified_DateTime                        AS last_modified_date,
    t.Amount                                   AS amount,
    t.Currency                                 AS currency,
    t.Payment_Purpose                          AS payment_purpose,
    t.Urgent_Flag                              AS urgent,
    t.Urgency_Reason                           AS urgency_reason,
    t.Current_Status                           AS status,
    t.PreparedBy_User_Key                      AS prepared_by_user_key,
    t.Current_Workflow_Stage                   AS current_workflow_stage,
    t.Approval_Tier_Snapshot                   AS approval_tier,
    t.Requires_VP                              AS requires_vp,
    t.Requires_CFO                             AS over_1m,
    ISNULL(prep.Display_Name,  '')             AS prepared_by,
    ISNULL(owner.Display_Name, '')             AS assigned_approver,
    ISNULL(sam.Display_Name,   '')             AS approver,
    ISNULL(ctrl.Display_Name,  '')             AS controller,
    ISNULL(vp.Display_Name,    '')             AS vp_approver,
    ISNULL(cfo.Display_Name,   '')             AS cfo_approver,
    ISNULL(ag.AccountingGroup_Name, '')        AS accounting_group_name,
    DATEDIFF(day, t.Submitted_Date, GETDATE()) AS days_pending
FROM [etransactions].[ETransaction] t
LEFT JOIN [etransactions].[AppUser] prep  ON prep.User_Key  = t.PreparedBy_User_Key
LEFT JOIN [etransactions].[AppUser] owner ON owner.User_Key = t.CurrentOwner_User_Key
LEFT JOIN [etransactions].[AppUser] sam   ON sam.User_Key   = t.SelectedApprover_User_Key
LEFT JOIN [etransactions].[AppUser] ctrl  ON ctrl.User_Key  = t.SelectedController_User_Key
LEFT JOIN [etransactions].[AppUser] vp    ON vp.User_Key    = t.VPApprover_User_Key
LEFT JOIN [etransactions].[AppUser] cfo   ON cfo.User_Key   = t.CFOApprover_User_Key
LEFT JOIN [etransactions].[BusinessEntity] be ON be.Entity_Key = t.Entity_Key
LEFT JOIN [etransactions].[AccountingGroup] ag ON ag.AccountingGroup_Key = be.AccountingGroup_Key
"""


_DASHBOARD_MODE_MARKER = "AS days_pending\nFROM [etransactions].[ETransaction] t"


def _dashboard_sql(with_recipient_mode: bool) -> str:
    """The dashboard query; selects ETransaction.Recipient_Mode only when the migrated schema is present."""
    if not with_recipient_mode:
        return _DASHBOARD_SQL_BASE
    assert _DASHBOARD_MODE_MARKER in _DASHBOARD_SQL_BASE
    return _DASHBOARD_SQL_BASE.replace(
        _DASHBOARD_MODE_MARKER,
        "AS days_pending,\n    t.Recipient_Mode                           AS recipient_mode\nFROM [etransactions].[ETransaction] t",
        1,
    )


def _normalize_roles(role) -> set:
    """Accept a single role code or an iterable of role codes; return a set.
    Mirrors authorization.normalize_roles()/workflow.normalize_roles() — kept
    local to avoid a cross-module import (multi-role authorization refactor)."""
    if isinstance(role, str):
        return {role}
    return set(role or [])


def _dashboard_scope_where_for_role(role, user_key, accounting_group_key):
    """
    Build the (where_clauses, params) for ONE individual role — unchanged
    rule-for-rule from the original single-role _dashboard_scope_where();
    extracted so a user's full role set can each be evaluated independently
    and OR'd together (see _dashboard_scope_where()).
    """
    if user_key is None:
        # Local dev bypass — no per-user identity to scope by; same role+status
        # relaxation as authorization._dev_bypass_visible().
        if role == "submitter":
            return ["1 = 0"], []  # session-stored mock submissions cover this path instead
        if role == "sam":
            return ["t.Current_Status = ?"], [workflow.STATUS_PENDING_APPROVER]
        if role == "controller":
            return ["t.Current_Status = ?"], [workflow.STATUS_PENDING_CONTROLLER]
        if role == "vp":
            return ["t.Current_Status = ?"], [workflow.STATUS_PENDING_VP]
        if role == "cfo":
            return ["t.Current_Status = ?"], [workflow.STATUS_PENDING_CFO]
        if role == "treasury":
            statuses = list(authorization.TREASURY_VISIBLE_STATUSES)
            return [f"t.Current_Status IN ({','.join('?' for _ in statuses)})"], statuses
        if role == "business_admin":
            return [], []
        return ["1 = 0"], []

    if role in authorization.UNRESTRICTED_VISIBILITY_ROLES:
        return [], []
    if role in authorization.NO_DEFINED_VISIBILITY_ROLES:
        return ["1 = 0"], []
    if role == "submitter":
        return ["t.PreparedBy_User_Key = ?"], [user_key]
    if role == "sam":
        return ["t.SelectedApprover_User_Key = ? AND t.Current_Status <> ?"], [user_key, workflow.STATUS_DRAFT]
    if role == "controller":
        if accounting_group_key is not None:
            return ["be.AccountingGroup_Key = ? AND t.Current_Status <> ?"], [accounting_group_key, workflow.STATUS_DRAFT]
        return ["t.SelectedController_User_Key = ? AND t.Current_Status <> ?"], [user_key, workflow.STATUS_DRAFT]
    if role == "vp":
        if accounting_group_key is not None:
            return ["be.AccountingGroup_Key = ? AND t.Current_Status <> ?"], [accounting_group_key, workflow.STATUS_DRAFT]
        return ["t.VPApprover_User_Key = ? AND t.Current_Status <> ?"], [user_key, workflow.STATUS_DRAFT]
    if role == "cfo":
        if accounting_group_key is not None:
            return ["be.AccountingGroup_Key = ? AND t.Current_Status <> ?"], [accounting_group_key, workflow.STATUS_DRAFT]
        return ["t.CFOApprover_User_Key = ? AND t.Current_Status <> ?"], [user_key, workflow.STATUS_DRAFT]
    if role == "treasury":
        statuses = list(authorization.TREASURY_VISIBLE_STATUSES)
        return [f"t.Current_Status IN ({','.join('?' for _ in statuses)})"], statuses
    return ["1 = 0"], []


def _dashboard_scope_where(role, user_key, accounting_group_key):
    """
    Build the (where_clauses, params) that restrict the dashboard query to the
    caller's authorized scope — filtering happens in SQL, never by loading all
    transactions and filtering in Python. MUST stay logically equivalent to
    authorization.can_view_transaction() (which additionally re-checks single
    transactions fetched by direct URL/reveal, since dashboard scoping alone
    is not sufficient — see Batch 4 report).

    `role` may be a single role code or an iterable of role codes (multi-role
    authorization refactor) — the effective scope is the UNION of every held
    role's own individual scope, never a blended/combined condition. If ANY
    held role is unrestricted (e.g. business_admin), the whole result is
    unrestricted, since union-with-everything is everything. A single query
    with one row per Transaction_Key naturally de-duplicates a transaction
    that happens to qualify under more than one role's clause.
    """
    roles = _normalize_roles(role)
    role_clauses = []
    for r in roles:
        where, params = _dashboard_scope_where_for_role(r, user_key, accounting_group_key)
        if where == []:
            return [], []  # this role alone is unrestricted -> union is unrestricted
        role_clauses.append((where, params))

    if not role_clauses:
        return ["1 = 0"], []

    if len(role_clauses) == 1:
        # Preserve the exact single-role output shape (list of AND'd clauses,
        # no extra grouping parens) — behaviorally identical, and keeps the
        # single-role SQL text unchanged from before this refactor.
        return role_clauses[0]

    or_parts = []
    combined_params = []
    for where, params in role_clauses:
        or_parts.append("(" + " AND ".join(where) + ")")
        combined_params.extend(params)
    return [" OR ".join(or_parts)], combined_params


def get_dashboard_records(*, role, user_key, accounting_group_key=None):
    """
    Return dashboard-shaped transaction rows already filtered to the caller's
    authorized scope (Batch 4; unioned across the full role set as of the
    multi-role authorization refactor) — see _dashboard_scope_where()/
    authorization.py for the rules. `role` may be a single role code or an
    iterable of role codes. `accounting_group_key` (Controller/VP/CFO only)
    must already be validated by the caller as one of THIS user's own
    authorized groups for at least one of their group-scoped roles.
    """
    where, params = _dashboard_scope_where(role, user_key, accounting_group_key)
    # Recipient_Mode is read only once the migrated schema exists (never against an unmigrated database).
    with_mode = bulk_schema_ready()
    sql = _dashboard_sql(with_mode)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY t.Submitted_Date DESC"

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        cols = [c[0] for c in cur.description]
        rows = cur.fetchall()
    finally:
        conn.close()

    records = []
    for row in rows:
        d = dict(zip(cols, row))
        # BIT columns → bool
        for key in ("urgent", "over_1m", "requires_vp"):
            d[key] = bool(d.get(key) or False)
        # date/datetime → "YYYY-MM-DD" string
        for key in ("treasury_service_date", "prepared_date", "submitted_date", "last_modified_date"):
            val = d.get(key)
            d[key] = val.strftime("%Y-%m-%d") if hasattr(val, "strftime") else (val or "")
        # '' when Recipient_Mode was not read (unmigrated schema) or is NULL (historical row).
        d["recipient_mode"] = (d.get("recipient_mode") or "").strip().lower()
        # Decimal → float
        d["amount"] = float(d.get("amount") or 0)
        # Draft rows have no Submitted_Date, so DATEDIFF returns NULL — coerce
        # to 0 so the dashboard template's numeric comparison never breaks (Batch 8).
        d["days_pending"] = d.get("days_pending") or 0
        # alias for templates that reference current_workflow_owner
        d["current_workflow_owner"] = d.get("assigned_approver") or ""
        records.append(d)
    return records


def get_user_accounting_group_keys(user_key, role_code=None):
    """
    Return the AccountingGroup_Key values `user_key` is authorized for, from
    AppUserRole (Is_Active=1, currently effective). Returns [] if user_key is
    None. NOTE: AppUserRole is currently EMPTY in this database (verified
    live) — this always returns [] today for every user; the query is real
    and ready for when rows exist (see /memories/repo/schema-facts.md).
    """
    if not user_key:
        return []
    conn = get_connection()
    try:
        cur = conn.cursor()
        sql = (
            "SELECT DISTINCT AccountingGroup_Key FROM etransactions.AppUserRole "
            "WHERE User_Key = ? AND Is_Active = 1 AND AccountingGroup_Key IS NOT NULL "
            "AND (Effective_Start_Date IS NULL OR Effective_Start_Date <= CAST(GETDATE() AS DATE)) "
            "AND (Effective_End_Date IS NULL OR Effective_End_Date >= CAST(GETDATE() AS DATE))"
        )
        params = [user_key]
        if role_code:
            sql += " AND Role_Code = ?"
            params.append(role_code)
        cur.execute(sql, params)
        return [r[0] for r in cur.fetchall() if r[0] is not None]
    finally:
        conn.close()


def get_accounting_groups_by_keys(group_keys):
    """Return {AccountingGroup_Key, AccountingGroup_Name} for the given keys, for dashboard scope UI."""
    if not group_keys:
        return []
    conn = get_connection()
    try:
        cur = conn.cursor()
        placeholders = ",".join("?" for _ in group_keys)
        cur.execute(
            f"SELECT AccountingGroup_Key, AccountingGroup_Name FROM etransactions.AccountingGroup "
            f"WHERE AccountingGroup_Key IN ({placeholders}) ORDER BY AccountingGroup_Name",
            list(group_keys),
        )
        return [{"accounting_group_key": r[0], "name": r[1]} for r in cur.fetchall()]
    finally:
        conn.close()


def get_app_user_role_codes(user_key):
    """
    Return the distinct, currently-effective Role_Code values `user_key` holds
    in AppUserRole (Is_Active=1, within any Effective_Start/End_Date window).
    Used by the local dev "Acting As" simulation (app.current_roles()) to
    approximate the full Entra App Role set a real user would have in
    production (Entra is the sole functional-role authority for production
    role grants; see e_transaction_multi_role_authorization_refactor.md Part
    3/8), and also by the intake form's server-side Approver/Controller
    eligibility check (a selected Approver/Controller must actually hold the
    'sam'/'controller' AppUserRole).
    """
    if not user_key:
        return []
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT DISTINCT Role_Code FROM etransactions.AppUserRole "
            "WHERE User_Key = ? AND Is_Active = 1 "
            "AND (Effective_Start_Date IS NULL OR Effective_Start_Date <= CAST(GETDATE() AS DATE)) "
            "AND (Effective_End_Date IS NULL OR Effective_End_Date >= CAST(GETDATE() AS DATE))",
            [user_key],
        )
        return [r[0] for r in cur.fetchall() if r[0]]
    finally:
        conn.close()


_DETAIL_SQL = """
SELECT
    t.Request_ID                                AS request_id,
    t.Property_Department_Text                  AS property_dept,
    t.Entity_ID_Text                            AS property_code,
    t.Request_Type                              AS request_type,
    t.Treasury_Service_Date                     AS treasury_service_date,
    t.Prepared_Date                             AS prepared_date,
    t.Submitted_Date                            AS submitted_date,
    t.Amount                                    AS amount,
    t.Currency                                  AS currency,
    t.Payment_Purpose                           AS payment_purpose,
    t.Urgent_Flag                               AS urgent,
    t.Urgency_Reason                            AS urgency_reason,
    t.Current_Status                            AS status,
    t.Current_Workflow_Stage                    AS current_workflow_stage,
    t.Approval_Tier_Snapshot                    AS approval_tier,
    t.Requires_VP                               AS requires_vp,
    t.Requires_CFO                              AS over_1m,
    DATEDIFF(day, t.Submitted_Date, GETDATE())  AS days_pending,
    ISNULL(prep.Display_Name,  '')              AS prepared_by,
    ISNULL(owner.Display_Name, '')              AS assigned_approver,
    ISNULL(sam.Display_Name,   '')              AS approver,
    ISNULL(ctrl.Display_Name,  '')              AS controller,
    ISNULL(vp.Display_Name,    '')              AS vp_approver,
    ISNULL(cfo.Display_Name,   '')              AS cfo_approver,
    ISNULL(ba.BankName,        '')              AS orig_bank_name,
    ISNULL(ba.AccountTitle,    '')              AS orig_account_name,
    ISNULL(ba.AccountNumber,   '')              AS orig_account_number,
    ISNULL(ba.RoutingNumber,   '')              AS orig_routing_number,
    ISNULL(ba.BankContactName, '')              AS orig_bank_contact,
    ISNULL(ba.Notes,           '')              AS notes_orig,
    ISNULL(ben.Payee_Name,    '')               AS recv_payee_name,
    ISNULL(ben.Contact_Name,  '')               AS recv_contact_name,
    ISNULL(ben.Contact_Email, '')               AS recv_contact_email,
    ISNULL(ben.Contact_Phone, '')               AS recv_contact_phone,
    ISNULL(bi.Receiving_Bank_Name,      '')     AS recv_bank_name,
    ISNULL(bi.Receiving_Account_Name,   '')     AS recv_account_name,
    ISNULL(bi.Receiving_Account_Number, '')     AS recv_account_number,
    ISNULL(bi.Receiving_Routing_Number, '')     AS recv_routing_number,
    ISNULL(bi.Bank_Beneficiary_Address, '')     AS recv_bank_address,
    ISNULL(v.Verbal_Confirmed,                  0) AS verbal_confirmed,
    ISNULL(v.Confirmed_With_KnownContact_Flag,  0) AS _verbal_known_contact,
    ISNULL(v.Confirmed_With_Requester_Flag,     0) AS _verbal_requester,
    ISNULL(v.Verbal_Contact_Name,              '') AS verbal_contact_name,
    v.Verbal_Confirm_DateTime                      AS verbal_confirm_datetime,
    v.AVS_Score                                    AS avs_score,
    ISNULL(v.External_Source_Flag,         0)      AS external_source,
    ISNULL(v.Internal_Doc_Not_Used_Flag,   0)      AS internal_doc_not_used,
    ISNULL(v.Instructions_Previously_Used, 0)      AS instructions_previously_used,
    v.Last_Used_Date                               AS last_used_date,
    v.Prior_Transaction_Key                         AS prior_transaction_key,
    priorTxn.Request_ID                             AS prior_request_id,
    t.RecipientBankAccount_Key                      AS recipient_bank_account_key,
    t.BeneficiaryInstruction_Key                    AS _instruction_key,
    t.OriginatingBankAccount_Key                    AS originating_bank_account_key,
    ba.Status                                       AS originating_bank_account_status,
    rba.Status                                      AS recipient_bank_account_status,
    ISNULL(rbe.Property_Department_Name, '')        AS recipient_entity_name,
    ISNULL(rbe.Classification, '')                  AS recipient_classification,
    ISNULL(be.Classification, '')                   AS entity_classification,
    t.CurrentOwner_User_Key                         AS current_owner_user_key,
    t.BankReleaser_User_Key                          AS bank_releaser_user_key
FROM [etransactions].[ETransaction] t
LEFT JOIN [etransactions].[AppUser] prep   ON prep.User_Key  = t.PreparedBy_User_Key
LEFT JOIN [etransactions].[AppUser] owner  ON owner.User_Key = t.CurrentOwner_User_Key
LEFT JOIN [etransactions].[AppUser] sam    ON sam.User_Key   = t.SelectedApprover_User_Key
LEFT JOIN [etransactions].[AppUser] ctrl   ON ctrl.User_Key  = t.SelectedController_User_Key
LEFT JOIN [etransactions].[AppUser] vp     ON vp.User_Key    = t.VPApprover_User_Key
LEFT JOIN [etransactions].[AppUser] cfo    ON cfo.User_Key   = t.CFOApprover_User_Key
LEFT JOIN [etransactions].[BankAccount] ba
      ON ba.BankAccount_Key = t.OriginatingBankAccount_Key
LEFT JOIN [etransactions].[Beneficiary] ben
      ON ben.Beneficiary_Key = t.Beneficiary_Key
LEFT JOIN [etransactions].[BeneficiaryBankInstruction] bi
      ON bi.BeneficiaryInstruction_Key = t.BeneficiaryInstruction_Key
LEFT JOIN [etransactions].[TransactionVerification] v
      ON v.Transaction_Key = t.Transaction_Key
LEFT JOIN [etransactions].[ETransaction] priorTxn
      ON priorTxn.Transaction_Key = v.Prior_Transaction_Key
LEFT JOIN [etransactions].[BankAccount] rba
      ON rba.BankAccount_Key = t.RecipientBankAccount_Key
LEFT JOIN [etransactions].[BusinessEntity] rbe
      ON rbe.Entity_Key = rba.Entity_Key
LEFT JOIN [etransactions].[BusinessEntity] be
      ON be.Entity_Key = t.Entity_Key
WHERE t.Request_ID = ?
"""

_TIMELINE_SQL = """
SELECT
    we.Event_Type,
    we.Decision,
    we.Actor_Role,
    we.To_Status,
    we.Event_DateTime,
    we.Comments_Reason,
    ISNULL(u.Display_Name, we.Actor_Role) AS actor_name,
    we.From_Status
FROM [etransactions].[WorkflowEvent] we
LEFT JOIN [etransactions].[AppUser] u ON u.User_Key = we.Actor_User_Key
WHERE we.Transaction_Key = (
    SELECT Transaction_Key FROM [etransactions].[ETransaction] WHERE Request_ID = ?
)
ORDER BY we.Event_DateTime ASC
"""

_COMMENTS_SQL = """
SELECT
    tc.Comment_Text,
    tc.Created_DateTime,
    ISNULL(u.Display_Name, '') AS author_name
FROM [etransactions].[TransactionComment] tc
LEFT JOIN [etransactions].[AppUser] u ON u.User_Key = tc.Author_User_Key
WHERE tc.Transaction_Key = (
    SELECT Transaction_Key FROM [etransactions].[ETransaction] WHERE Request_ID = ?
)
ORDER BY tc.Created_DateTime ASC
"""

# Maps a normalized (lowercased, spaces/underscores stripped — see the lookup
# call in get_request_detail()) Event_Type to a template dot CSS class name.
# Anything unrecognized falls back to the neutral "routed" dot so old/
# unexpected values never break rendering.
_EVENT_TYPE_MAP = {
    # Established Event_Type literals (unchanged since before Batch 6), normalized
    "submitted":          "submitted",
    "approve":            "approve",
    "cancel":             "reject",
    "moreinfo":           "more_info",
    "requesterrespond":   "more_info",
    "reassign":           "reassign",
    "treasuryinitiated":  "treasury_reviewed",
    "treasuryreleased":   "mark_completed",
    "bankrelease":        "mark_completed",
    "markcompleted":      "mark_completed",
    # New additive Event_Type values (Batch 6), normalized
    "approverassigned":   "routed",
    "controllerassigned": "routed",
    "vpassigned":         "routed",
    "cfoassigned":        "routed",
    "readyfortreasury":   "treasury_reviewed",
}


def get_request_detail(request_id: str):
    """Return full single-transaction detail dict for the detail view, or None if not found."""
    conn = get_connection()
    try:
        cur = conn.cursor()

        cur.execute(_DETAIL_SQL, [request_id])
        cols = [c[0] for c in cur.description]
        row  = cur.fetchone()
        if row is None:
            return None
        d = dict(zip(cols, row))

        cur.execute(_TIMELINE_SQL, [request_id])
        timeline_rows = cur.fetchall()

        cur.execute(_COMMENTS_SQL, [request_id])
        comment_rows = cur.fetchall()
    finally:
        conn.close()

    # BIT → bool
    for key in ("urgent", "over_1m", "requires_vp", "verbal_confirmed",
                "external_source", "internal_doc_not_used",
                "instructions_previously_used",
                "_verbal_known_contact", "_verbal_requester"):
        d[key] = bool(d.get(key) or False)

    # date → "YYYY-MM-DD" string
    for key in ("treasury_service_date", "prepared_date", "submitted_date", "last_used_date"):
        val = d.get(key)
        d[key] = val.strftime("%Y-%m-%d") if hasattr(val, "strftime") else (val or "")

    # datetime → readable string
    dt = d.get("verbal_confirm_datetime")
    d["verbal_confirm_datetime"] = (
        dt.strftime("%Y-%m-%d %I:%M %p") if hasattr(dt, "strftime") else (dt or "")
    )

    # Decimal → float
    d["amount"] = float(d.get("amount") or 0)

    # Derive verbal_confirmed_with string from the two boolean flags
    known = d.pop("_verbal_known_contact", False)
    req   = d.pop("_verbal_requester", False)
    d["verbal_confirmed_with"] = "Known Contact" if known else ("Requester" if req else "")

    # Recipient type: derived from the persisted relationships; a row with no beneficiary instruction and
    # no readable Recipient_Mode is reported as "unavailable" rather than guessed to be an External payee.
    has_instruction = d.pop("_instruction_key", "present") is not None
    d["recipient_mode"] = effective_recipient_mode(None, d.get("recipient_bank_account_key"), has_instruction)
    d["bulk_recipient_rows"] = d["bulk_matched_rows"] = 0
    d["bulk_validated_at"] = d["bulk_listing_sha256"] = ""
    if bulk_schema_ready():
        # READ gate (schema present), independent of the BULK_RECIPIENT_ENABLED create switch: existing
        # Bulk transactions stay accurately visible after the feature is turned off.
        state = get_bulk_state(request_id=request_id)
        d["recipient_mode"] = effective_recipient_mode(
            state["mode"], d.get("recipient_bank_account_key"), has_instruction)
        d["bulk_recipient_rows"], d["bulk_matched_rows"] = state["recipient_rows"], state["matched_rows"]
        d["bulk_validated_at"], d["bulk_listing_sha256"] = state["validated_at"], state["listing_sha256"]

    d["current_workflow_owner"] = d.get("assigned_approver") or ""
    d["notes_recv"]        = ""   # not yet stored per-instruction
    d["docs_checklist"]    = {}   # attachment checklist not yet in DB
    d["attachments"]       = {}   # file attachments not yet in DB
    d["extra_attachments"] = []

    # Build timeline list
    timeline = []
    for r in timeline_rows:
        evt_type    = r[0] or ""
        actor       = r[6]        # actor_name (Display_Name or Actor_Role fallback)
        to_status   = r[3] or ""
        from_status = r[7] or ""
        evt_dt      = r[4]
        evt_date  = evt_dt.strftime("%Y-%m-%d") if hasattr(evt_dt, "strftime") else str(evt_dt or "")
        dot_type  = _EVENT_TYPE_MAP.get(evt_type.lower().replace(" ", "").replace("_", ""), "routed")
        # Stage-aware friendly label (e.g. "Controller Approved") even though
        # Event_Type itself stays the established "approve" literal for Power
        # Automate — see workflow.friendly_event_label().
        friendly_type = workflow.friendly_event_label(evt_type, from_status)
        event_label = f"{friendly_type} by {actor}" if actor else friendly_type
        timeline.append({"date": evt_date, "event": event_label, "status": to_status, "type": dot_type})
    d["timeline"] = timeline

    # The active Request More Information (only while the transaction awaits the requester): the latest
    # 'more_info' WorkflowEvent already holds the reason, the person who asked, when, and the stage it came from.
    d["rfi"] = None
    if d.get("status") == workflow.STATUS_MORE_INFO:
        asked = [r for r in timeline_rows if (r[0] or "") == workflow.EVENT_RFI_REQUESTED]
        latest = asked[-1] if asked else None
        asked_dt = latest[4] if latest else None
        d["rfi"] = {
            "reason":        (latest[5] or "").strip() if latest else "",
            "requested_by":  (latest[6] or "") if latest else "",
            "requested_at":  asked_dt.strftime("%Y-%m-%d %I:%M %p") if hasattr(asked_dt, "strftime") else "",
            "requested_dt":  asked_dt if hasattr(asked_dt, "strftime") else None,
            "origin_status": (latest[7] or "") if latest else "",
        }

    # Build comments list
    comments = []
    for r in comment_rows:
        c_dt   = r[1]
        c_date = c_dt.strftime("%Y-%m-%d") if hasattr(c_dt, "strftime") else str(c_dt or "")
        comments.append({"author": r[2], "date": c_date, "text": r[0] or ""})
    d["comments"] = comments

    return d


# ─────────────────────────────────────────────────────────────
#  Reference data helpers (used to populate form dropdowns)
# ─────────────────────────────────────────────────────────────

def get_user_list():
    """Return all active AppUsers as a list of dicts for form dropdowns."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT User_Key, Display_Name FROM [etransactions].[AppUser] "
            "WHERE Active_Status = 1 ORDER BY Display_Name"
        )
        return [{"user_key": r[0], "display_name": r[1]} for r in cur.fetchall()]
    finally:
        conn.close()


def get_user_directory():
    """
    Return active AppUsers (name/team/phone/email) for the user directory
    reference page. Restricted to rows with a Team value — the business
    roster (Team/Phone_Number) is only populated for real users, so this
    naturally excludes internal dev/test AppUser rows (Patrick Batson,
    Test Approver/Controller/VP/CFO/Treasury) without hardcoding their names.
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT Display_Name, Team, Phone_Number, Email FROM [etransactions].[AppUser] "
            "WHERE Active_Status = 1 AND Team IS NOT NULL AND Team <> '' "
            "ORDER BY Team, Display_Name"
        )
        return [
            {"display_name": r[0], "team": r[1], "phone_number": r[2] or "", "email": r[3]}
            for r in cur.fetchall()
        ]
    finally:
        conn.close()


def get_bank_account_status(bank_account_key: int):
    """
    Return the raw Status ('Open'/'Active'/'Closed') of a BankAccount, or None
    if the key doesn't exist. Used to authoritatively re-validate the
    originating-account selection server-side at intake — the client only
    supplies a hidden bank_account_key that must never be trusted as-is.
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT Status FROM [etransactions].[BankAccount] WHERE BankAccount_Key = ?",
            [bank_account_key],
        )
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def find_prior_completed_beneficiary_match(*, payee_name, receiving_bank_name,
                                            receiving_account_number, receiving_routing_number):
    """
    Return {"transaction_key", "request_id", "last_used_date"} for the most
    recent COMPLETED transaction whose beneficiary identity (payee + receiving
    bank name) and banking identity (account + routing number) exactly match
    the given values, or None if no such transaction exists.

    Receiving_Account_Number/Receiving_Routing_Number are Dynamic Data Masking
    columns and this app's principal has no UNMASK grant — but the equality
    comparison below runs entirely inside SQL Server's WHERE clause against the
    real underlying values (DDM only masks values RETURNED to the client, not
    internal predicate evaluation — verified empirically against seed data
    before relying on this). Only a non-sensitive Transaction_Key/Request_ID/
    date are ever returned; the real account/routing numbers are never read
    back into the application.
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT TOP (1) t.Transaction_Key, t.Request_ID, t.Submitted_Date
            FROM [etransactions].[ETransaction] t
            JOIN [etransactions].[BeneficiaryBankInstruction] bi
                ON bi.BeneficiaryInstruction_Key = t.BeneficiaryInstruction_Key
            JOIN [etransactions].[Beneficiary] b
                ON b.Beneficiary_Key = t.Beneficiary_Key
            WHERE t.Current_Status = ?
              AND bi.Receiving_Account_Number = ?
              AND bi.Receiving_Routing_Number = ?
              AND UPPER(bi.Receiving_Bank_Name) = UPPER(?)
              AND UPPER(b.Payee_Name) = UPPER(?)
            ORDER BY t.Submitted_Date DESC
            """,
            [
                STATUS_COMPLETED,
                (receiving_account_number or "").strip(),
                (receiving_routing_number or "").strip(),
                (receiving_bank_name or "").strip(),
                (payee_name or "").strip(),
            ],
        )
        row = cur.fetchone()
        if row is None:
            return None
        txn_key, request_id, submitted_date = row
        return {
            "transaction_key": txn_key,
            "request_id": request_id,
            "last_used_date": submitted_date.strftime("%Y-%m-%d") if hasattr(submitted_date, "strftime") else str(submitted_date or ""),
        }
    finally:
        conn.close()


def get_bank_accounts():
    """
    Return all Open company bank accounts for the originating-account dropdown
    AND the internal-recipient picker — safe identification only (bank, title,
    last 4, entity/property name, classification); never a full account or
    routing number.
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        # AccountNumber is Dynamic Data Masking partial() masked (e.g. "XXXX-XXXX-0123"),
        # which intentionally preserves the real last 4 characters. Wrapping it in a T-SQL
        # scalar function (RIGHT/SUBSTRING) breaks that partial reveal and returns a generic
        # "xxxx" instead — so the last-4 digits are sliced here in Python from the raw
        # (still masked-but-partially-visible) value SQL Server returns, not in the query.
        cur.execute(
            "SELECT ba.BankAccount_Key, ba.BankName, ba.AccountTitle, ba.AccountNumber, "
            "       ISNULL(be.Property_Department_Name, ''), ISNULL(be.Classification, '') "
            "FROM [etransactions].[BankAccount] ba "
            "LEFT JOIN [etransactions].[BusinessEntity] be ON be.Entity_Key = ba.Entity_Key "
            "WHERE ba.Status = 'Open' ORDER BY ba.AccountTitle"
        )
        return [
            {
                "bank_account_key": r[0],
                "bank_name": r[1],
                "account_title": r[2],
                "account_last4": (r[3] or "")[-4:],
                "entity_name": r[4],
                "classification": r[5],
            }
            for r in cur.fetchall()
        ]
    finally:
        conn.close()


BANK_ACCOUNT_SERVICE_FLAGS = (
    "AnalysisComposite",
    "ElectronicAnalysisStatementEDI822",
    "GatewayPremiumReporting",
    "EnhancedImaging7YearArchive",
    "ACHOnlineOriginationReporting",
    "AccountTransferEnabled",
    "StopPaymentEnabled",
    "ACHModuleEnabled",
    "SameDayACHEnabled",
    "WireModuleEnabled",
    "ElectronicDataInterchangeEnabled",
    "RemoteDepositCaptureEnabled",
    "ACHPositivePayEnabled",
    "ACHDebitBlockEnabled",
    "CheckPositivePayEnabled",
    "PayeeVerificationEnabled",
    "CheckBlockEnabled",
)


# ─────────────────────────────────────────────────────────────
#  Bank Account lifecycle audit (BankAccountEvent)
#
#  etransactions.BankAccountEvent was created live 2026-09-22 (schema below,
#  confirmed via INFORMATION_SCHEMA/sys.foreign_keys). create_bank_account()
#  and update_bank_account() now write BankAccountEvent rows atomically (same
#  connection/cursor, single commit) — create_bank_account() always writes
#  BANK_ACCOUNT_CREATED; update_bank_account() writes BANK_ACCOUNT_CLOSED/
#  BANK_ACCOUNT_REOPENED only on an actual Status transition (via
#  bank_account_lifecycle_event_for_status_change()) — an ordinary field edit
#  that doesn't change Status writes no event at all. BANK_ACCOUNT_UPDATED is
#  defined but intentionally not yet wired in (out of scope for this pass).
#
#    CREATE TABLE etransactions.BankAccountEvent (
#        BankAccountEvent_Key INT IDENTITY PRIMARY KEY,
#        BankAccount_Key INT NOT NULL REFERENCES etransactions.BankAccount(BankAccount_Key),
#        Event_Type VARCHAR(50) NOT NULL,
#        PerformedBy_User_Key INT NOT NULL REFERENCES etransactions.AppUser(User_Key),
#        Event_DateTime DATETIME2 NOT NULL,
#        Reason NVARCHAR(500) NULL,
#        Previous_Status VARCHAR(20) NULL,
#        New_Status VARCHAR(20) NULL,
#        Change_Detail NVARCHAR(MAX) NULL
#    );
# ─────────────────────────────────────────────────────────────

BANK_ACCOUNT_EVENT_CREATED  = "BANK_ACCOUNT_CREATED"
BANK_ACCOUNT_EVENT_UPDATED  = "BANK_ACCOUNT_UPDATED"
BANK_ACCOUNT_EVENT_CLOSED   = "BANK_ACCOUNT_CLOSED"
BANK_ACCOUNT_EVENT_REOPENED = "BANK_ACCOUNT_REOPENED"

# Sensitive BankAccount fields — never written to Change_Detail with their real
# values, only whether they changed (e.g. "AccountNumber changed: Yes").
_BANK_ACCOUNT_SENSITIVE_FIELDS = {
    "account_number", "routing_number", "transit_number_canada",
    "institution_number_canada", "tax_id_number",
}

# (form-data key, get_bank_account_record() key, display label)
_BANK_ACCOUNT_DIFF_FIELDS = [
    ("bank_name", "bankname", "BankName"),
    ("account_name_id", "accountnameid", "AccountNameID"),
    ("account_title", "accounttitle", "AccountTitle"),
    ("account_title_modifier", "accounttitlemodifier", "AccountTitleModifier"),
    ("system_account_name", "systemaccountname", "SystemAccountName"),
    ("account_number", "accountnumber", "AccountNumber"),
    ("routing_number", "routingnumber", "RoutingNumber"),
    ("transit_number_canada", "transitnumbercanada", "TransitNumberCanada"),
    ("institution_number_canada", "institutionnumbercanada", "InstitutionNumberCanada"),
    ("gl_account_number", "glaccountnumber", "GLAccountNumber"),
    ("gl_account_name", "glaccountname", "GLAccountName"),
    ("tax_id_number", "taxidnumber", "TaxIDNumber"),
    ("address", "address", "Address"),
    ("phone_number", "phonenumber", "PhoneNumber"),
    ("account_type", "accounttype", "AccountType"),
    ("account_classification", "accountclassification", "AccountClassification (Account Category)"),
    ("bank_contact_name", "bankcontactname", "BankContactName"),
    ("notes", "notes", "Notes"),
] + [(flag, flag.lower(), flag) for flag in BANK_ACCOUNT_SERVICE_FLAGS]


def summarize_bank_account_changes(previous: dict, new_data: dict) -> str | None:
    """
    Compare a previous BankAccount record (as returned by get_bank_account_record(),
    lower-cased keys) against a new submission's form data (as built by
    app._bank_account_form_data()) and return a human-readable Change_Detail
    string, or None if nothing actually changed (no audit row for a no-op save).

    Sensitive fields (account/routing/transit/institution/tax ID numbers) are
    handled specially: update_bank_account() only ever overwrites them when the
    submitted value is non-blank (blank = "keep existing masked value"), so a
    non-blank submission IS the change signal — we never compare against the
    masked previous value, and never record real values, only "changed: Yes".
    """
    changes = []
    for form_key, record_key, label in _BANK_ACCOUNT_DIFF_FIELDS:
        new_val = new_data.get(form_key)

        if form_key in _BANK_ACCOUNT_SENSITIVE_FIELDS:
            if isinstance(new_val, str) and new_val.strip():
                changes.append(f"{label} changed: Yes")
            continue

        old_val = previous.get(record_key)
        if isinstance(new_val, bool):
            if bool(old_val) != new_val:
                changes.append(f"{label}: {bool(old_val)} \u2192 {new_val}")
            continue

        old_norm = (old_val or "").strip() if isinstance(old_val, str) else (old_val or "")
        new_norm = (new_val or "").strip() if isinstance(new_val, str) else (new_val or "")
        if str(old_norm) != str(new_norm):
            changes.append(f"{label}: '{old_norm}' \u2192 '{new_norm}'")

    return "; ".join(changes) if changes else None


def bank_account_lifecycle_event_for_status_change(previous_status: str, new_status: str):
    """
    Return the BANK_ACCOUNT_* event type for a Status transition, or None if
    this transition doesn't warrant a distinct lifecycle event on its own
    (covered by BANK_ACCOUNT_EVENT_UPDATED instead) — e.g. Closed->Closed must
    never produce a second/duplicate close event.
    """
    prev = (previous_status or "").strip().lower()
    new = (new_status or "").strip().lower()
    if prev == new:
        return None
    if new == "closed":
        return BANK_ACCOUNT_EVENT_CLOSED
    if prev == "closed":
        return BANK_ACCOUNT_EVENT_REOPENED
    return None


def record_bank_account_event(cur, bank_account_key, *, event_type, performed_by_user_key,
                               reason=None, previous_status=None, new_status=None, change_detail=None):
    """
    Insert a BankAccountEvent row using the CALLER's own cursor (not a new
    connection), so it commits atomically as part of whatever BankAccount
    INSERT/UPDATE the caller is already performing. Called from
    create_bank_account() and update_bank_account() — see the module-level
    note above this section.
    """
    cur.execute(
        "INSERT INTO etransactions.BankAccountEvent ("
        "  BankAccount_Key, Event_Type, PerformedBy_User_Key, Event_DateTime,"
        "  Reason, Previous_Status, New_Status, Change_Detail"
        ") VALUES (?,?,?,?,?,?,?,?)",
        [bank_account_key, event_type, performed_by_user_key, datetime.now(),
         reason, previous_status, new_status, change_detail],
    )


def _status_for_display(status: str) -> str:
    # Defensive display-only fallback: canonical values are now strictly 'Open'/'Closed'
    # (legacy 'Active' rows were migrated 2026-09-18 — see /memories/repo/schema-facts.md).
    # New writes never produce 'Active'; this only guards against stray historical data.
    return "Open" if status == "Active" else (status or "")


def get_business_entities(active_only=True):
    """Return BusinessEntity rows for Bank Account Management dropdowns/filters."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        sql = (
            "SELECT Entity_Key, Entity_ID, Property_Department_Name, Classification "
            "FROM etransactions.BusinessEntity"
        )
        params = []
        if active_only:
            sql += " WHERE Active_Status = ?"
            params.append(1)
        sql += " ORDER BY Classification, Property_Department_Name"
        cur.execute(sql, params)
        return [
            {
                "entity_key": r[0],
                "entity_id": r[1] or "",
                "name": r[2] or "",
                "classification": r[3] or "",
            }
            for r in cur.fetchall()
        ]
    finally:
        conn.close()


def search_bank_account_records(filters=None):
    """
    Search the consolidated BankAccount master using safe, non-sensitive result fields.

    Returns both `classification` (BusinessEntity.Classification — Property/Corporate,
    the account's true ownership classification) and `account_type` (BankAccount.AccountType
    — the banking product type, e.g. Checking/Savings) as SEPARATE fields. Do not conflate
    them — see app.py's _bank_account_form_data() docstring for the full field-naming note.
    """
    filters = filters or {}
    where = []
    params = []

    if filters.get("bank_name"):
        where.append("ba.BankName LIKE ?")
        params.append(f"%{filters['bank_name']}%")
    if filters.get("classification"):
        where.append("be.Classification = ?")
        params.append(filters["classification"])
    if filters.get("entity_key"):
        where.append("ba.Entity_Key = ?")
        params.append(int(filters["entity_key"]))
    if filters.get("status"):
        where.append("ba.Status = ?")
        params.append(filters["status"])
    if filters.get("opened_from"):
        where.append("ba.DateOpened >= ?")
        params.append(filters["opened_from"])
    if filters.get("opened_to"):
        where.append("ba.DateOpened <= ?")
        params.append(filters["opened_to"])
    if filters.get("closed_from"):
        where.append("ba.DateClosed >= ?")
        params.append(filters["closed_from"])
    if filters.get("closed_to"):
        where.append("ba.DateClosed <= ?")
        params.append(filters["closed_to"])

    sql = """
        SELECT
            ba.BankAccount_Key,
            ba.BankName,
            ba.AccountTitle,
            ba.AccountNameID,
            ba.AccountNumber,
            ba.AccountType,
            ba.AccountClassification,
            ba.Status,
            ba.DateOpened,
            ba.DateClosed,
            ba.Entity_Key,
            ISNULL(be.Property_Department_Name, '') AS EntityName,
            ISNULL(be.Entity_ID, '') AS EntityID,
            ISNULL(be.Classification, '') AS EntityClassification
        FROM etransactions.BankAccount ba
        INNER JOIN etransactions.BusinessEntity be ON be.Entity_Key = ba.Entity_Key
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ba.BankName, ba.AccountTitle"

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        records = []
        for r in cur.fetchall():
            last4 = (r[4] or "")[-4:]
            if filters.get("last4") and last4 != filters["last4"]:
                continue
            records.append({
                "bank_account_key": r[0],
                "bank_name": r[1] or "",
                "account_title": r[2] or "",
                "account_name_id": r[3] or "",
                "account_last4": last4,
                "account_type": r[5] or "",
                "account_category": r[6] or "",
                "status": _status_for_display(r[7]),
                "date_opened": r[8].strftime("%Y-%m-%d") if hasattr(r[8], "strftime") else (r[8] or ""),
                "date_closed": r[9].strftime("%Y-%m-%d") if hasattr(r[9], "strftime") else (r[9] or ""),
                "entity_key": r[10],
                "entity_name": r[11] or "",
                "entity_id": r[12] or "",
                "classification": r[13] or "",
            })
        return records
    finally:
        conn.close()


def get_bank_account_record(bank_account_key: int):
    """Return a full BankAccount row for the add/edit form, or None if not found."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        columns = [
            "BankAccount_Key", "Entity_Key", "AccountClassification", "BankName",
            "AccountNameID", "AccountTitle", "AccountTitleModifier", "SystemAccountName",
            "AccountNumber", "RoutingNumber", "TransitNumberCanada", "InstitutionNumberCanada",
            "GLAccountNumber", "GLAccountName", "TaxIDNumber", "Address", "PhoneNumber",
            "AccountType", "Status", "DateOpened", "DateClosed", "BankContactName", "Notes",
            *BANK_ACCOUNT_SERVICE_FLAGS,
        ]
        cur.execute(
            f"SELECT {', '.join(columns)} FROM etransactions.BankAccount WHERE BankAccount_Key = ?",
            [bank_account_key],
        )
        row = cur.fetchone()
        if row is None:
            return None
        record = dict(zip([c.lower() for c in columns], row))
        for key in ("dateopened", "dateclosed"):
            val = record.get(key)
            record[key] = val.strftime("%Y-%m-%d") if hasattr(val, "strftime") else (val or "")
        record["status"] = _status_for_display(record.get("status"))
        # Application-layer masking — do not depend on DDM alone (banking_security.py).
        record["accountnumber"]          = banking_security.mask_field("account_number", record.get("accountnumber"))
        record["routingnumber"]          = banking_security.mask_field("routing_number", record.get("routingnumber"))
        record["taxidnumber"]            = banking_security.mask_field("tax_id", record.get("taxidnumber"))
        record["transitnumbercanada"]    = banking_security.mask_field("transit_number", record.get("transitnumbercanada"))
        record["institutionnumbercanada"] = banking_security.mask_field("institution_number", record.get("institutionnumbercanada"))
        return record
    finally:
        conn.close()


_REVEALABLE_BANK_ACCOUNT_COLUMNS = frozenset(banking_security.REVEALABLE_BANK_ACCOUNT_FIELDS.values())
_REVEALABLE_BENEFICIARY_COLUMNS = frozenset(banking_security.REVEALABLE_BENEFICIARY_FIELDS.values())


def get_bank_account_sensitive_field(bank_account_key: int, column_name: str):
    """
    Return the single raw column value for one allow-listed sensitive
    BankAccount field, or None if the account doesn't exist. `column_name`
    MUST be one of banking_security.REVEALABLE_BANK_ACCOUNT_FIELDS' values —
    callers must never pass a client-supplied column name straight through;
    this is re-validated here as defense in depth even though app.py's route
    already validates against the same allow-list.
    """
    if column_name not in _REVEALABLE_BANK_ACCOUNT_COLUMNS:
        raise ValueError(f"Unsupported column for reveal: {column_name}")
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT {column_name} FROM etransactions.BankAccount WHERE BankAccount_Key = ?",
            [bank_account_key],
        )
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def get_beneficiary_instruction_sensitive_field(beneficiary_instruction_key: int, column_name: str):
    """Same as get_bank_account_sensitive_field(), for BeneficiaryBankInstruction's two revealable fields."""
    if column_name not in _REVEALABLE_BENEFICIARY_COLUMNS:
        raise ValueError(f"Unsupported column for reveal: {column_name}")
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT {column_name} FROM etransactions.BeneficiaryBankInstruction "
            "WHERE BeneficiaryInstruction_Key = ?",
            [beneficiary_instruction_key],
        )
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def get_draft_beneficiary_instruction_key(transaction_key: int, prepared_by_user_key: int):
    """
    Return the BeneficiaryInstruction_Key for a Draft owned by `prepared_by_user_key`,
    or None if it doesn't exist/isn't owned/isn't still a Draft. Lets the final-submit
    prior-use check resolve the REAL already-stored receiving account/routing number
    when the finalize form leaves those fields blank (meaning "keep the existing
    masked value" — see _validate_intake_submission()'s has_stored_receiving_bank_instruction) —
    never substitutes a blank form value for the real one in the match comparison.
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT BeneficiaryInstruction_Key FROM [etransactions].[ETransaction] "
            "WHERE Transaction_Key = ? AND PreparedBy_User_Key = ? AND Current_Status = ?",
            [transaction_key, prepared_by_user_key, workflow.STATUS_DRAFT],
        )
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


class InternalRecipientError(Exception):
    """The selected maintained internal recipient BankAccount cannot be used for a
    transaction snapshot (missing, closed, same as originator, or its banking
    values cannot be read as real values). Messages are safe to show the requester."""


def _load_internal_recipient_snapshot(cur, recipient_bank_account_key, originating_bank_account_key=None, *, require_routing=False):
    """
    Read the CURRENT real banking values of a maintained BankAccount, server-side
    only, on the caller's cursor (so it is part of the caller's transaction), to
    copy into a transaction-specific Beneficiary/BeneficiaryBankInstruction
    snapshot. Raises InternalRecipientError unless the account exists, is Open,
    is not the originating account, and its account/routing values are REAL —
    a DDM-masked placeholder (e.g. "XXXX-XXXX-0123") is never accepted, so one can
    never be stored as if it were the genuine instructions.

    Returns {"bank_name", "account_name", "account_number", "routing_number",
    "address"}. The values never leave the server (never rendered/returned to a browser).
    """
    try:
        key = int(recipient_bank_account_key)
    except (TypeError, ValueError):
        raise InternalRecipientError("The selected recipient bank account is invalid.")
    if originating_bank_account_key not in (None, "") and key == int(originating_bank_account_key):
        raise InternalRecipientError("The recipient bank account cannot be the same as the originating bank account.")

    cur.execute(
        "SELECT BankName, AccountTitle, AccountNameID, SystemAccountName, "
        "       AccountNumber, RoutingNumber, Address, Status "
        "FROM [etransactions].[BankAccount] WHERE BankAccount_Key = ?",
        [key],
    )
    row = cur.fetchone()
    if row is None:
        raise InternalRecipientError("The selected recipient bank account could not be found.")
    bank_name, title, name_id, system_name, account_number, routing_number, address, status = row
    if status != "Open":
        raise InternalRecipientError("The selected recipient bank account is closed and cannot be used.")
    account_number = (account_number or "").strip()
    routing_number = (routing_number or "").strip()
    if (not account_number or banking_security.is_mask_placeholder(account_number)
            or (routing_number and banking_security.is_mask_placeholder(routing_number))):
        raise InternalRecipientError(
            "The selected recipient account's banking details could not be read securely. "
            "Contact an administrator."
        )
    if require_routing and not routing_number:
        raise InternalRecipientError(
            "The selected recipient account has no routing number on file; an ACH requires one. "
            "Contact Treasury to complete the account record."
        )
    return {
        "bank_name":      (bank_name or "").strip(),
        "account_name":   (title or name_id or system_name or "").strip(),
        "account_number": account_number,
        "routing_number": routing_number,
        "address":        (address or "").strip(),
    }


def get_internal_recipient_summary(bank_account_key):
    """
    Safe, non-sensitive summary of a maintained BankAccount for validating/
    displaying an internal-recipient selection: None if it doesn't exist.
    Never returns the account/routing numbers — `snapshot_ready` only reports
    whether real (non-masked) values are readable server-side.
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT ba.BankAccount_Key, ba.BankName, ba.AccountTitle, ba.AccountNameID, "
            "       ba.AccountNumber, ba.RoutingNumber, ba.Status, "
            "       ISNULL(be.Property_Department_Name, ''), ISNULL(be.Classification, '') "
            "FROM [etransactions].[BankAccount] ba "
            "LEFT JOIN [etransactions].[BusinessEntity] be ON be.Entity_Key = ba.Entity_Key "
            "WHERE ba.BankAccount_Key = ?",
            [bank_account_key],
        )
        row = cur.fetchone()
        if row is None:
            return None
        key, bank_name, title, name_id, account_number, routing_number, status, entity_name, classification = row
        account_number = (account_number or "").strip()
        routing_number = (routing_number or "").strip()
        snapshot_ready = bool(
            account_number and not banking_security.is_mask_placeholder(account_number)
            and not (routing_number and banking_security.is_mask_placeholder(routing_number))
        )
        return {
            "bank_account_key": key,
            "bank_name": bank_name or "",
            "account_title": title or name_id or "",
            "routing_present": bool(routing_number),
            "last4": account_number[-4:] if snapshot_ready else "",
            "status": status,
            "entity_name": entity_name,
            "classification": classification,
            "snapshot_ready": snapshot_ready,
        }
    finally:
        conn.close()


def get_internal_recipient_instruction_values(bank_account_key):
    """
    Server-side only: the real bank name/account/routing of an Open maintained
    BankAccount, for prior-use exact matching of an internal recipient (the
    values are compared inside SQL and never sent to a browser). Raises
    InternalRecipientError if the account can't be used.
    """
    conn = get_connection()
    try:
        return _load_internal_recipient_snapshot(conn.cursor(), bank_account_key)
    finally:
        conn.close()


def get_draft_recipient_bank_account_key(transaction_key, prepared_by_user_key):
    """
    Return (found, recipient_bank_account_key) for a Draft owned by
    `prepared_by_user_key`: found is False if it doesn't exist/isn't owned/isn't
    still a Draft; the key is None for an external recipient. Lets the app tell
    whether a blank receiving account/routing field means "keep the stored
    EXTERNAL value" or must be re-entered (a Draft switching internal -> external
    must never silently keep the internal snapshot's account number).
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT RecipientBankAccount_Key, BeneficiaryInstruction_Key FROM [etransactions].[ETransaction] "
            "WHERE Transaction_Key = ? AND PreparedBy_User_Key = ? AND Current_Status = ?",
            [transaction_key, prepared_by_user_key, workflow.STATUS_DRAFT],
        )
        row = cur.fetchone()
        # A bulk-recipient Draft has no stored instruction, so nothing is "kept" if it switches to external.
        return (False, None) if row is None or row[1] is None else (True, row[0])
    finally:
        conn.close()


def get_transaction_banking_keys(request_id: str):
    """
    Return {"originating_bank_account_key", "beneficiary_instruction_key"} for a
    transaction, or None if not found. Lets reveal routes resolve the real
    BankAccount_Key/BeneficiaryInstruction_Key server-side from Request_ID —
    never trusting a client-supplied key directly.
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT OriginatingBankAccount_Key, BeneficiaryInstruction_Key "
            "FROM etransactions.ETransaction WHERE Request_ID = ?",
            [request_id],
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {"originating_bank_account_key": row[0], "beneficiary_instruction_key": row[1]}
    finally:
        conn.close()


def create_bank_account(data: dict, actor_user_key: int) -> int:
    """Insert a BankAccount row. Caller validates permissions and required fields."""
    now = datetime.now()
    service_values = [1 if data.get(flag) else 0 for flag in BANK_ACCOUNT_SERVICE_FLAGS]
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO etransactions.BankAccount (
                Entity_Key, AccountClassification, BankName, AccountNameID, AccountTitle,
                AccountTitleModifier, SystemAccountName, AccountNumber, RoutingNumber,
                TransitNumberCanada, InstitutionNumberCanada, GLAccountNumber, GLAccountName,
                TaxIDNumber, Address, PhoneNumber, AccountType, Status, DateOpened,
                DateClosed, BankContactName, Notes,
                AnalysisComposite, ElectronicAnalysisStatementEDI822, GatewayPremiumReporting,
                EnhancedImaging7YearArchive, ACHOnlineOriginationReporting,
                AccountTransferEnabled, StopPaymentEnabled, ACHModuleEnabled, SameDayACHEnabled,
                WireModuleEnabled, ElectronicDataInterchangeEnabled, RemoteDepositCaptureEnabled,
                ACHPositivePayEnabled, ACHDebitBlockEnabled, CheckPositivePayEnabled,
                PayeeVerificationEnabled, CheckBlockEnabled,
                CreatedDate, CreatedBy_User_Key, ModifiedDate, ModifiedBy_User_Key
            ) OUTPUT INSERTED.BankAccount_Key
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                int(data["entity_key"]), data.get("account_classification") or "Operating",
                data["bank_name"], data.get("account_name_id", ""), data.get("account_title", ""),
                data.get("account_title_modifier", ""), data.get("system_account_name", ""),
                data["account_number"], data.get("routing_number", ""), data.get("transit_number_canada", ""),
                data.get("institution_number_canada", ""), data.get("gl_account_number", ""),
                data.get("gl_account_name", ""), data.get("tax_id_number", ""), data.get("address", ""),
                data.get("phone_number", ""), data.get("account_type", ""), data["status"],
                data.get("date_opened") or None, data.get("date_closed") or None,
                data.get("bank_contact_name", ""), data.get("notes", ""),
                *service_values, now, actor_user_key, now, actor_user_key,
            ],
        )
        key = cur.fetchone()[0]
        record_bank_account_event(
            cur, key, event_type=BANK_ACCOUNT_EVENT_CREATED, performed_by_user_key=actor_user_key,
            new_status=data["status"],
        )
        conn.commit()
        return key
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def update_bank_account(bank_account_key: int, data: dict, actor_user_key: int) -> None:
    """Update a BankAccount row; blank sensitive inputs preserve existing values."""
    now = datetime.now()
    assignments = [
        "Entity_Key = ?", "AccountClassification = ?", "BankName = ?", "AccountNameID = ?",
        "AccountTitle = ?", "AccountTitleModifier = ?", "SystemAccountName = ?",
        "GLAccountNumber = ?", "GLAccountName = ?", "Address = ?", "PhoneNumber = ?",
        "AccountType = ?", "Status = ?", "DateOpened = ?", "DateClosed = ?",
        "BankContactName = ?", "Notes = ?",
    ]
    params = [
        int(data["entity_key"]), data.get("account_classification") or "Operating",
        data["bank_name"], data.get("account_name_id", ""), data.get("account_title", ""),
        data.get("account_title_modifier", ""), data.get("system_account_name", ""),
        data.get("gl_account_number", ""), data.get("gl_account_name", ""), data.get("address", ""),
        data.get("phone_number", ""), data.get("account_type", ""), data["status"],
        data.get("date_opened") or None, data.get("date_closed") or None,
        data.get("bank_contact_name", ""), data.get("notes", ""),
    ]
    for field, column in (
        ("account_number", "AccountNumber"),
        ("routing_number", "RoutingNumber"),
        ("transit_number_canada", "TransitNumberCanada"),
        ("institution_number_canada", "InstitutionNumberCanada"),
        ("tax_id_number", "TaxIDNumber"),
    ):
        if data.get(field):
            assignments.append(f"{column} = ?")
            params.append(data[field])
    for flag in BANK_ACCOUNT_SERVICE_FLAGS:
        assignments.append(f"{flag} = ?")
        params.append(1 if data.get(flag) else 0)
    assignments.extend(["ModifiedDate = ?", "ModifiedBy_User_Key = ?"])
    params.extend([now, actor_user_key, bank_account_key])

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT Status FROM etransactions.BankAccount WHERE BankAccount_Key = ?",
            [bank_account_key],
        )
        row = cur.fetchone()
        previous_status = row[0] if row else None

        cur.execute(
            f"UPDATE etransactions.BankAccount SET {', '.join(assignments)} WHERE BankAccount_Key = ?",
            params,
        )

        new_status = data["status"]
        lifecycle_event = bank_account_lifecycle_event_for_status_change(previous_status, new_status)
        if lifecycle_event is not None:
            record_bank_account_event(
                cur, bank_account_key, event_type=lifecycle_event, performed_by_user_key=actor_user_key,
                previous_status=previous_status, new_status=new_status,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────
#  Write path — new transaction submission
# ─────────────────────────────────────────────────────────────

def _generate_unique_request_id(cur) -> str:
    from datetime import datetime as _dt
    import random as _random

    for _ in range(10):
        candidate = f"TXN-{_dt.now().year}-{_random.randint(1000, 9999)}"
        cur.execute(
            "SELECT 1 FROM [etransactions].[ETransaction] WHERE Request_ID = ?",
            [candidate],
        )
        if not cur.fetchone():
            return candidate
    raise RuntimeError("Unable to generate a unique Request_ID.")


def reserve_request_id() -> str:
    """
    Return a not-yet-used Request_ID without creating any ETransaction row.

    Lets the intake flow upload required SharePoint attachments under the final
    Request_ID BEFORE the SQL transaction is inserted, so a failed required
    upload never leaves an orphaned/incomplete transaction behind in SQL.

    Small inherent race window (the ID isn't actually reserved anywhere until
    insert_transaction() inserts it) — no worse than the pre-existing generate-
    then-insert loop this was extracted from.
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        return _generate_unique_request_id(cur)
    finally:
        conn.close()


def resolve_entity_key(classification, *, property_entity_number=None, property_name=None):
    """
    Resolve the BusinessEntity.Entity_Key for a new/edited transaction from the
    requester's Property/Corporate choice on intake (get-or-create for
    Property; a single shared Corporate entity for now — see intake.html).

    Property: get-or-create matched on BusinessEntity.Entity_ID = str(int(
    property_entity_number)) — the SharePoint Properties_0 list's ENTITY_NUMBER
    — so the same property reuses the same Entity_Key on every future
    submission instead of creating a duplicate row each time.

    Corporate: returns the existing single active Corporate BusinessEntity —
    no per-department entity yet; the department itself stays free text
    (Property_Department_Text).

    Raises ValueError (a user-facing validation error, not a system failure)
    if classification isn't recognized, Property is chosen without a
    resolvable property_entity_number/property_name, or no active Corporate
    BusinessEntity exists in the current environment.
    """
    classification = (classification or "").strip().lower()
    conn = get_connection()
    try:
        cur = conn.cursor()

        if classification == "property":
            if not property_entity_number or not (property_name or "").strip():
                raise ValueError("A property must be selected.")
            entity_id = str(int(property_entity_number))
            cur.execute(
                "SELECT Entity_Key FROM etransactions.BusinessEntity "
                "WHERE Entity_ID = ? AND Classification = 'Property'",
                [entity_id],
            )
            row = cur.fetchone()
            if row:
                return row[0]
            cur.execute(
                "INSERT INTO etransactions.BusinessEntity "
                "(Entity_ID, Property_Department_Name, Classification, Active_Status) "
                "OUTPUT INSERTED.Entity_Key VALUES (?, ?, 'Property', 1)",
                [entity_id, property_name.strip()],
            )
            entity_key = cur.fetchone()[0]
            conn.commit()
            return entity_key

        if classification == "corporate":
            cur.execute(
                "SELECT TOP 1 Entity_Key FROM etransactions.BusinessEntity "
                "WHERE Active_Status = 1 AND Classification = 'Corporate' "
                "ORDER BY Entity_Key"
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError("No active Corporate entity is configured. Contact an administrator.")
            return row[0]

        raise ValueError("Select whether this request is for a Property or Corporate.")
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────
#  Corporate bulk recipients (staged migration — see BULK_RECIPIENT_SCHEMA_CONTRACT.md)
#
#  Everything below that touches the new columns/table runs ONLY when the caller passes
#  data["recipient_mode"] (the app does that only when bulk_recipient_enabled() is true), so an
#  unmigrated database is never asked about Recipient_Mode / BulkRecipientAccount.
# ─────────────────────────────────────────────────────────────

RECIPIENT_MODE_DB_EXTERNAL = "External"
RECIPIENT_MODE_DB_INTERNAL = "Internal"
RECIPIENT_MODE_DB_BULK = "Bulk"
BULK_RECIPIENT_ENV = "BULK_RECIPIENT_ENABLED"

_BULK_REQUIRED_TRANSACTION_COLUMNS = frozenset({
    "Recipient_Mode", "Bulk_Listing_SHA256", "Bulk_Recipient_Count", "Bulk_Listing_Validated_DateTime",
})
_BULK_REQUIRED_CHILD_COLUMNS = frozenset({
    "BulkRecipient_Key", "Transaction_Key", "Line_Number", "BankAccount_Key",
    "Verification_Outcome", "Verified_DateTime", "Listing_SHA256",
})
_BULK_SCHEMA_RECHECK_SECONDS = 60
_bulk_schema_state = {"ready": False, "checked_at": None}


def bulk_schema_ready() -> bool:
    """
    True only if the live database already has the bulk-recipient structures: the four new
    ETransaction columns, the BulkRecipientAccount table, and nullable beneficiary links. A positive
    answer is cached; a negative or failed check is retried at most once a minute. Fails closed.
    """
    now = time.monotonic()
    if _bulk_schema_state["ready"]:
        return True
    last = _bulk_schema_state["checked_at"]
    if last is not None and now - last < _BULK_SCHEMA_RECHECK_SECONDS:
        return False
    _bulk_schema_state["checked_at"] = now
    try:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT TABLE_NAME, COLUMN_NAME, IS_NULLABLE FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = 'etransactions' AND TABLE_NAME IN ('ETransaction', 'BulkRecipientAccount')"
            )
            rows = cur.fetchall()
        finally:
            conn.close()
    except Exception:
        return False
    txn_cols = {r[1] for r in rows if r[0] == "ETransaction"}
    child_cols = {r[1] for r in rows if r[0] == "BulkRecipientAccount"}
    nullable_links = {r[1] for r in rows if r[0] == "ETransaction" and r[2] == "YES"}
    ready = (
        _BULK_REQUIRED_TRANSACTION_COLUMNS <= txn_cols
        and _BULK_REQUIRED_CHILD_COLUMNS <= child_cols
        and {"Beneficiary_Key", "BeneficiaryInstruction_Key"} <= nullable_links
    )
    _bulk_schema_state["ready"] = ready
    return ready


def bulk_recipient_enabled() -> bool:
    """
    CREATE gate: BULK_RECIPIENT_ENABLED=true AND the migrated schema is present (default OFF). Governs only
    whether NEW bulk requests may be created or finalized; reading existing Bulk rows needs only
    bulk_schema_ready().
    """
    if (os.environ.get(BULK_RECIPIENT_ENV, "false") or "").strip().lower() != "true":
        return False
    return bulk_schema_ready()


def effective_recipient_mode(stored_mode, recipient_bank_account_key, has_instruction=True) -> str:
    """
    'external' | 'internal' | 'bulk' | 'unavailable'. A NULL stored mode (historical rows) is read from the
    existing relationships; a row with no beneficiary instruction and no stored mode is 'unavailable' —
    never guessed to be External.
    """
    stored = (stored_mode or "").strip().lower()
    if stored in ("external", "internal", "bulk"):
        return stored
    if not has_instruction and not recipient_bank_account_key:
        return "unavailable"
    return "internal" if recipient_bank_account_key else "external"


def match_bulk_recipient_accounts(pairs):
    """
    For each (account_number, routing_number) return the BankAccount rows it matches, as
    [{"bank_account_key", "status"}, ...]. The comparison runs inside SQL (the account/routing columns
    are Dynamic Data Masking columns; only keys and status ever leave the database).
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        results = []
        for account, routing in pairs:
            cur.execute(
                "SELECT BankAccount_Key, Status FROM [etransactions].[BankAccount] "
                "WHERE AccountNumber = ? AND RoutingNumber = ?",
                [account, routing],
            )
            results.append([{"bank_account_key": r[0], "status": r[1]} for r in cur.fetchall()])
        return results
    finally:
        conn.close()


def _insert_beneficiary_and_instruction(cur, data, today, *, require_routing):
    """
    Create the Beneficiary + BeneficiaryBankInstruction rows for a SINGLE-recipient transaction and
    return (beneficiary_key, instruction_key, recipient_bank_account_key). Internal recipient: the
    instruction is a transaction-time snapshot of the maintained BankAccount, read server-side.
    """
    recipient_key = data.get("recipient_bank_account_key") or None
    if recipient_key:
        snap = _load_internal_recipient_snapshot(
            cur, recipient_key, data.get("bank_account_key"), require_routing=require_routing)
        recv_bank_name, recv_account_name = snap["bank_name"], snap["account_name"]
        recv_account_number, recv_routing_number = snap["account_number"], snap["routing_number"]
        recv_bank_address = snap["address"]
    else:
        recv_bank_name, recv_account_name = data["recv_bank_name"], data.get("recv_account_name", "")
        recv_account_number, recv_routing_number = data["recv_account_number"], data.get("recv_routing_number", "")
        recv_bank_address = data.get("recv_bank_address", "")

    cur.execute(
        "INSERT INTO [etransactions].[Beneficiary] "
        "(Payee_Name, Contact_Name, Contact_Email, Contact_Phone) "
        "OUTPUT INSERTED.Beneficiary_Key VALUES (?, ?, ?, ?)",
        [data["recv_payee_name"], data.get("recv_contact_name", ""),
         data.get("recv_contact_email", ""), data.get("recv_contact_phone", "")],
    )
    ben_key = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO [etransactions].[BeneficiaryBankInstruction] "
        "(Beneficiary_Key, Receiving_Bank_Name, Receiving_Account_Name, "
        " Receiving_Account_Number, Receiving_Routing_Number, "
        " Bank_Beneficiary_Address, Is_Current, Effective_From) "
        "OUTPUT INSERTED.BeneficiaryInstruction_Key VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
        [ben_key, recv_bank_name, recv_account_name,
         recv_account_number, recv_routing_number,
         recv_bank_address, today],
    )
    return ben_key, cur.fetchone()[0], recipient_key


def _delete_beneficiary_rows(cur, ben_key, bi_key):
    """Remove a Draft's own single-recipient rows once the transaction no longer references them."""
    if bi_key is not None:
        cur.execute("DELETE FROM [etransactions].[BeneficiaryBankInstruction] WHERE BeneficiaryInstruction_Key = ?", [bi_key])
    if ben_key is not None:
        cur.execute("DELETE FROM [etransactions].[Beneficiary] WHERE Beneficiary_Key = ?", [ben_key])


def _replace_bulk_recipients(cur, transaction_key, recipients, sha256, now):
    """Replace the transaction's verified bulk recipients (empty list = clear, which also clears the batch metadata)."""
    cur.execute("DELETE FROM [etransactions].[BulkRecipientAccount] WHERE Transaction_Key = ?", [transaction_key])
    for r in recipients:
        cur.execute(
            "INSERT INTO [etransactions].[BulkRecipientAccount] "
            "(Transaction_Key, Line_Number, BankAccount_Key, Verification_Outcome, Verified_DateTime, Listing_SHA256) "
            "VALUES (?,?,?,?,?,?)",
            [transaction_key, r["line"], r["bank_account_key"], "Matched", now, sha256],
        )
    cur.execute(
        "UPDATE [etransactions].[ETransaction] SET Bulk_Listing_SHA256 = ?, Bulk_Recipient_Count = ?, "
        "Bulk_Listing_Validated_DateTime = ? WHERE Transaction_Key = ?",
        [sha256 if recipients else None, len(recipients) or None, now if recipients else None, transaction_key],
    )


def _assert_bulk_recipients_eligible(cur, recipients, sha256, originating_key):
    """
    Last check inside the submitting SQL transaction, before any workflow row exists: the verified recipients
    must be present, distinct, still Open and not the originating account, so a BankAccount closed (or made the
    originating account) after the listing was verified cannot be finalized on a stale verification. The
    transaction's isolation (snapshot) cannot stop a change committed after this read; see the schema contract.
    """
    keys = [r["bank_account_key"] for r in recipients or []]
    if not keys or not sha256 or len(set(keys)) != len(keys):
        raise InternalRecipientError("The bulk recipient listing has no verified recipients. Please verify the listing again.")
    cur.execute(
        "SELECT COUNT(*) FROM [etransactions].[BankAccount] "
        f"WHERE BankAccount_Key IN ({','.join('?' for _ in keys)}) AND Status = 'Open' AND BankAccount_Key <> ?",
        [*keys, originating_key],
    )
    if cur.fetchone()[0] != len(keys):
        raise InternalRecipientError(
            "One or more bulk recipient accounts are no longer eligible (closed or changed). "
            "Please verify the listing again.")


def _save_recipient_mode_state(cur, transaction_key, data, now, *, submit=False):
    """Persist Recipient_Mode and the verified bulk recipients — only when the app supplied a mode."""
    mode = data.get("recipient_mode")
    if not mode:
        return
    if submit and mode == RECIPIENT_MODE_DB_BULK:
        _assert_bulk_recipients_eligible(
            cur, data.get("bulk_recipients"), data.get("bulk_listing_sha256"), data.get("bank_account_key"))
    cur.execute(
        "UPDATE [etransactions].[ETransaction] SET Recipient_Mode = ? WHERE Transaction_Key = ?",
        [mode, transaction_key],
    )
    recipients = data.get("bulk_recipients") if mode == RECIPIENT_MODE_DB_BULK else []
    _replace_bulk_recipients(cur, transaction_key, recipients or [], data.get("bulk_listing_sha256"), now)


def set_draft_bulk_recipients(transaction_key, prepared_by_user_key, recipients, sha256):
    """Store (or, with an empty list, clear) a Draft's verified bulk recipients. Owner- and Draft-guarded."""
    now = datetime.now()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM [etransactions].[ETransaction] "
            "WHERE Transaction_Key = ? AND PreparedBy_User_Key = ? AND Current_Status = ?",
            [transaction_key, prepared_by_user_key, workflow.STATUS_DRAFT],
        )
        if cur.fetchone() is None:
            raise DraftNotEditableError("This draft could not be found, is not yours, or is no longer editable.")
        _replace_bulk_recipients(cur, transaction_key, recipients, sha256, now)
        conn.commit()
    except DraftNotEditableError:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_bulk_state(transaction_key=None, *, request_id=None):
    """
    {"mode", "listing_sha256", "recipient_rows", "matched_rows", "validated_at"} for a transaction (by
    Transaction_Key or Request_ID), read only when the bulk schema exists (callers check
    bulk_recipient_enabled() first). mode is the stored Recipient_Mode ('' for historical rows).
    """
    where, param = ("t.Transaction_Key = ?", transaction_key) if transaction_key is not None else ("t.Request_ID = ?", request_id)
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT t.Recipient_Mode, t.Bulk_Listing_SHA256, t.Bulk_Recipient_Count, t.Bulk_Listing_Validated_DateTime, "
            "       (SELECT COUNT(*) FROM [etransactions].[BulkRecipientAccount] b "
            "        WHERE b.Transaction_Key = t.Transaction_Key AND b.Verification_Outcome = 'Matched') "
            f"FROM [etransactions].[ETransaction] t WHERE {where}",
            [param],
        )
        row = cur.fetchone()
    finally:
        conn.close()
    if row is None:
        return {"mode": "", "listing_sha256": "", "recipient_rows": 0, "matched_rows": 0, "validated_at": ""}
    validated = row[3]
    return {
        "mode": row[0] or "",
        "listing_sha256": row[1] or "",
        "recipient_rows": int(row[2] or 0),
        "matched_rows": int(row[4] or 0),
        "validated_at": validated.strftime("%Y-%m-%d %I:%M %p") if hasattr(validated, "strftime") else "",
    }



def insert_transaction(data: dict) -> str:
    """
    Insert all rows for a new transaction in a single transaction.
    Inserts: Beneficiary, BeneficiaryBankInstruction, ETransaction,
             TransactionVerification, WorkflowEvent (Submitted).

    If data['request_id'] is set (e.g. via reserve_request_id(), called earlier
    so required attachments could be uploaded first), that exact ID is used;
    otherwise one is generated here. Returns the Request_ID actually used.
    """
    from datetime import datetime as _dt

    now   = _dt.now()
    today = now.date()

    # Resolve the applicable ApprovalRule BEFORE opening the transactional
    # connection below — a pure read; raises ApprovalRuleConfigurationError if
    # the live configuration cannot deterministically route this amount
    # (Batch 7 Part 8) — no fallback to hard-coded Python thresholds.
    rule = resolve_approval_rule(data["amount"])

    # Resolve/get-or-create the BusinessEntity for the requester's Property/
    # Corporate choice BEFORE opening the transactional connection below — see
    # resolve_entity_key(). Raises ValueError (caller surfaces as a validation
    # error) if the choice can't be resolved.
    entity_key = resolve_entity_key(
        data.get("classification"),
        property_entity_number=data.get("property_entity_number"),
        property_name=data.get("property_dept"),
    )

    conn = get_connection()
    try:
        cur = conn.cursor()

        # Internal recipient: the banking instructions are a transaction-time
        # SNAPSHOT of the maintained BankAccount, read server-side inside this
        # transaction (Open status re-checked, masked placeholders rejected).
        # External recipient: the requester's entered values, exactly as before.
        # Bulk recipients have no single beneficiary: no Beneficiary/instruction rows at all.
        if data.get("recipient_mode") == RECIPIENT_MODE_DB_BULK:
            recipient_key = ben_key = bi_key = None
        else:
            ben_key, bi_key, recipient_key = _insert_beneficiary_and_instruction(
                cur, data, today, require_routing=data["request_type"] == "ACH")

        # Use the caller's pre-reserved Request_ID if provided, else generate one now.
        request_id = data.get("request_id") or _generate_unique_request_id(cur)

        # Every transaction starts at Pending Approver regardless of the resolved
        # rule — the rule only determines which LATER stages (Controller/VP/CFO)
        # are additionally required. Single source of truth: the ApprovalRule row
        # resolved above (rule), never data.get("approval_tier")/mock_data thresholds.
        initial_status  = STATUS_PENDING_APPROVER
        rule_key        = rule["approval_rule_key"]
        requires_vp     = rule["requires_vp"]
        requires_cfo    = rule["requires_cfo"]
        tier            = workflow.approval_tier_label(
            requires_vp=requires_vp, requires_cfo=requires_cfo,
            entity_classification=data.get("classification"),
        )

        # Insert ETransaction
        cur.execute(
            "INSERT INTO [etransactions].[ETransaction] ("
            "  Request_ID, PreparedBy_User_Key,"
            "  Entity_Key, Property_Department_Text, Entity_ID_Text,"
            "  OriginatingBankAccount_Key, RecipientBankAccount_Key,"
            "  Beneficiary_Key, BeneficiaryInstruction_Key,"
            "  SelectedApprover_User_Key, SelectedController_User_Key,"
            "  VPApprover_User_Key, CFOApprover_User_Key,"
            "  CurrentOwner_User_Key, BankReleaser_User_Key, ApprovalRule_Key,"
            "  Request_Type, Treasury_Service_Date, Prepared_Date, Submitted_Date,"
            "  Amount, Currency, Payment_Purpose,"
            "  Urgent_Flag, Urgency_Reason,"
            "  Current_Status, Current_Workflow_Stage,"
            "  Approval_Tier_Snapshot, Requires_VP, Requires_CFO,"
            "  Created_DateTime, Modified_DateTime"
            ") OUTPUT INSERTED.Transaction_Key"
            "  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                request_id,
                data["prepared_by_key"],
                entity_key,
                data.get("property_dept", ""),
                data.get("property_code", ""),
                data["bank_account_key"],
                recipient_key,
                ben_key,
                bi_key,
                data["approver_key"],
                data["controller_key"],
                None,   # VP — assigned during workflow routing
                None,   # CFO — assigned during workflow routing
                data["approver_key"],   # approver is the first current owner
                None,   # bank releaser — set by treasury
                rule_key,
                data["request_type"],
                data.get("treasury_service_date") or None,
                data.get("prepared_date") or None,
                now,
                data["amount"],
                data.get("currency", "USD"),
                data.get("payment_purpose", ""),
                1 if data.get("urgent") else 0,
                data.get("urgency_reason", ""),
                initial_status,
                stage_for_status(initial_status),
                tier,
                1 if requires_vp  else 0,
                1 if requires_cfo else 0,
                now,
                now,
            ],
        )
        txn_key = cur.fetchone()[0]

        # Recipient mode + verified bulk recipients (only when the bulk schema is in use).
        _save_recipient_mode_state(cur, txn_key, data, now, submit=True)

        # Insert TransactionVerification
        cur.execute(
            "INSERT INTO [etransactions].[TransactionVerification] ("
            "  Transaction_Key,"
            "  Instructions_Previously_Used, Last_Used_Date, Prior_Transaction_Key,"
            "  Verbal_Confirmed,"
            "  Confirmed_With_KnownContact_Flag, Confirmed_With_Requester_Flag,"
            "  Verbal_Contact_Name, Verbal_Confirm_DateTime,"
            "  AVS_Score, External_Source_Flag, Internal_Doc_Not_Used_Flag,"
            "  Verified_By_User_Key, Created_DateTime"
            ") OUTPUT INSERTED.Verification_Key VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                txn_key,
                1 if data.get("instructions_previously_used") else 0,
                data.get("last_used_date") or None,
                data.get("prior_transaction_key") or None,
                1 if data.get("verbal_confirmed") else 0,
                1 if data.get("verbal_known_contact") else 0,
                1 if data.get("verbal_requester") else 0,
                data.get("verbal_contact_name", ""),
                data.get("verbal_confirm_datetime") or None,
                data.get("avs_score") or None,
                1 if data.get("external_source") else 0,
                1 if data.get("internal_doc_not_used") else 0,
                data["prepared_by_key"],
                now,
            ],
        )

        # Insert initial WorkflowAssignment — Approver is the first actionable owner
        # (Batch 6: previously no assignment row existed until the first workflow
        # transition; APPROVER_ASSIGNED now has a real Related_Assignment_Key).
        cur.execute(
            "INSERT INTO [etransactions].[WorkflowAssignment] ("
            "  Transaction_Key, Workflow_Role, Assigned_User_Key,"
            "  Assigned_By_User_Key, Assignment_Source,"
            "  Assigned_DateTime, Is_Current"
            ") OUTPUT INSERTED.Assignment_Key VALUES (?,?,?,?,?,?,1)",
            [txn_key, "Approver", data["approver_key"], data["prepared_by_key"], "Workflow", now],
        )
        approver_assignment_key = cur.fetchone()[0]

        # Insert WorkflowEvent — REQUEST_SUBMITTED
        cur.execute(
            "INSERT INTO [etransactions].[WorkflowEvent] ("
            "  Transaction_Key, Actor_User_Key, Actor_Role,"
            "  Event_Type, Decision, From_Status, To_Status,"
            "  Event_DateTime, Comments_Reason, Related_Assignment_Key"
            ") OUTPUT INSERTED.WorkflowEvent_Key VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                txn_key,
                data["prepared_by_key"],
                "Submitter",
                workflow.EVENT_REQUEST_SUBMITTED, workflow.EVENT_TYPE_LABELS[workflow.EVENT_REQUEST_SUBMITTED],
                None, initial_status,
                now, None, None,
            ],
        )

        # Insert WorkflowEvent — APPROVER_ASSIGNED (Batch 6: Approver is the first
        # actionable owner; Controller/VP/CFO are not assigned yet, so no
        # corresponding assignment event is created for them at submission).
        cur.execute(
            "INSERT INTO [etransactions].[WorkflowEvent] ("
            "  Transaction_Key, Actor_User_Key, Actor_Role,"
            "  Event_Type, Decision, From_Status, To_Status,"
            "  Event_DateTime, Comments_Reason, Related_Assignment_Key"
            ") OUTPUT INSERTED.WorkflowEvent_Key VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                txn_key,
                data["prepared_by_key"],
                "Submitter",
                workflow.EVENT_APPROVER_ASSIGNED, workflow.EVENT_TYPE_LABELS[workflow.EVENT_APPROVER_ASSIGNED],
                None, initial_status,
                now, None, approver_assignment_key,
            ],
        )

        conn.commit()
        return request_id

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


class DraftNotEditableError(Exception):
    """
    Raised by update_draft()/get_draft_for_edit()/finalize_draft_submission()
    when the target transaction cannot be edited: it does not exist, is not
    owned by the requesting AppUser, or is no longer Current_Status='Draft'
    (e.g. already submitted, cancelled, completed). Both existence/ownership
    and status are checked together server-side — never inferred from a
    client-supplied transaction_key alone (Batch 8 Part 3/14).
    """


# Fields intake.html may submit for a Draft — everything the live ETransaction/
# Beneficiary/BeneficiaryBankInstruction schema requires NOT NULL with no safe
# non-fake default (see create_draft() docstring for the schema limitation this
# reflects). Amount/dates are parsed defensively; FK fields are validated to
# actually exist by the caller (app.py) before reaching here.
_DRAFT_REQUIRED_FIELDS = (
    "recv_payee_name", "recv_bank_name", "recv_account_number",
    "bank_account_key", "approver_key", "controller_key",
    "request_type", "treasury_service_date", "amount",
)


def _verification_values(data: dict) -> list:
    """TransactionVerification column values (in table order after Transaction_Key) from caller data."""
    return [
        1 if data.get("instructions_previously_used") else 0,
        data.get("last_used_date") or None,
        data.get("prior_transaction_key") or None,
        1 if data.get("verbal_confirmed") else 0,
        1 if data.get("verbal_known_contact") else 0,
        1 if data.get("verbal_requester") else 0,
        data.get("verbal_contact_name", ""),
        data.get("verbal_confirm_datetime") or None,
        data.get("avs_score") or None,
        1 if data.get("external_source") else 0,
        1 if data.get("internal_doc_not_used") else 0,
    ]


def _upsert_transaction_verification(cur, transaction_key, data: dict, user_key, now) -> None:
    """
    Keep exactly ONE TransactionVerification row per transaction on the caller's cursor:
    update the existing row (a Draft may already have one) and insert only when none
    exists yet. A Draft row is simply the saved-so-far verification state; finalize
    overwrites it with the validated final values.
    """
    values = _verification_values(data)
    cur.execute(
        "UPDATE [etransactions].[TransactionVerification] SET"
        "  Instructions_Previously_Used = ?, Last_Used_Date = ?, Prior_Transaction_Key = ?,"
        "  Verbal_Confirmed = ?, Confirmed_With_KnownContact_Flag = ?, Confirmed_With_Requester_Flag = ?,"
        "  Verbal_Contact_Name = ?, Verbal_Confirm_DateTime = ?,"
        "  AVS_Score = ?, External_Source_Flag = ?, Internal_Doc_Not_Used_Flag = ?,"
        "  Verified_By_User_Key = ?"
        " WHERE Transaction_Key = ?",
        [*values, user_key, transaction_key],
    )
    cur.execute(
        "IF NOT EXISTS (SELECT 1 FROM [etransactions].[TransactionVerification] WHERE Transaction_Key = ?) "
        "INSERT INTO [etransactions].[TransactionVerification] ("
        "  Transaction_Key,"
        "  Instructions_Previously_Used, Last_Used_Date, Prior_Transaction_Key,"
        "  Verbal_Confirmed,"
        "  Confirmed_With_KnownContact_Flag, Confirmed_With_Requester_Flag,"
        "  Verbal_Contact_Name, Verbal_Confirm_DateTime,"
        "  AVS_Score, External_Source_Flag, Internal_Doc_Not_Used_Flag,"
        "  Verified_By_User_Key, Created_DateTime"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [transaction_key, transaction_key, *values, user_key, now],
    )


def get_draft_request_id(transaction_key, prepared_by_user_key):
    """Request_ID of a Draft owned by prepared_by_user_key, or None (missing / not owned / not a Draft)."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT Request_ID FROM [etransactions].[ETransaction] "
            "WHERE Transaction_Key = ? AND PreparedBy_User_Key = ? AND Current_Status = ?",
            [transaction_key, prepared_by_user_key, workflow.STATUS_DRAFT],
        )
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def create_draft(data: dict):
    """
    Create a new Draft transaction (Batch 8) — persists only what the requester
    has entered so far. Does NOT create TransactionVerification, an initial
    WorkflowAssignment, or any WorkflowEvent — a Draft has not entered the
    approval workflow and must never trigger a Power Automate notification.
    Returns (request_id, transaction_key).

    SCHEMA LIMITATION (see Batch 8 report): OriginatingBankAccount_Key,
    Beneficiary_Key/BeneficiaryInstruction_Key (and their NOT NULL Payee_Name/
    Receiving_Bank_Name/Receiving_Account_Number columns), SelectedApprover_User_Key,
    SelectedController_User_Key, Treasury_Service_Date, and Amount are all
    NOT NULL on ETransaction with no safe non-fake default available — a Draft
    cannot be saved before _DRAFT_REQUIRED_FIELDS are all supplied. This is the
    practical minimum the CURRENT live schema allows without inventing
    placeholder business data (e.g. Amount=0, an arbitrary BankAccount, a "TBD"
    payee). Everything else (attachments, urgency reason, wire address, AVS/
    verification, currency, property/department, payment purpose, VP/CFO/
    contact fields) may be blank.
    """
    now = datetime.now()
    today = now.date()

    # Entity_Key is nullable on ETransaction — a Draft may be saved before the
    # requester has chosen Property/Corporate (classification blank means "not
    # chosen yet", not an error, unlike insert_transaction()/
    # finalize_draft_submission() where it's already been validated as required).
    classification = (data.get("classification") or "").strip().lower()
    entity_key = None
    if classification:
        entity_key = resolve_entity_key(
            classification,
            property_entity_number=data.get("property_entity_number"),
            property_name=data.get("property_dept"),
        )

    conn = get_connection()
    try:
        cur = conn.cursor()

        # Internal recipient: snapshot the maintained BankAccount now (the Draft's
        # NOT NULL Beneficiary/Instruction rows need real values); finalize
        # re-reads the account and refreshes this snapshot before submission.
        # Bulk recipients have no single beneficiary: no Beneficiary/instruction rows at all.
        if data.get("recipient_mode") == RECIPIENT_MODE_DB_BULK:
            recipient_key = ben_key = bi_key = None
        else:
            ben_key, bi_key, recipient_key = _insert_beneficiary_and_instruction(cur, data, today, require_routing=False)

        request_id = data.get("request_id") or _generate_unique_request_id(cur)

        cur.execute(
            "INSERT INTO [etransactions].[ETransaction] ("
            "  Request_ID, PreparedBy_User_Key,"
            "  Entity_Key, Property_Department_Text, Entity_ID_Text,"
            "  OriginatingBankAccount_Key, RecipientBankAccount_Key,"
            "  Beneficiary_Key, BeneficiaryInstruction_Key,"
            "  SelectedApprover_User_Key, SelectedController_User_Key,"
            "  CurrentOwner_User_Key,"
            "  Request_Type, Treasury_Service_Date, Prepared_Date,"
            "  Amount, Currency, Payment_Purpose,"
            "  Urgent_Flag, Urgency_Reason,"
            "  Current_Status, Current_Workflow_Stage,"
            "  Requires_VP, Requires_CFO,"
            "  Created_DateTime, Modified_DateTime"
            ") OUTPUT INSERTED.Transaction_Key"
            "  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                request_id, data["prepared_by_key"],
                entity_key, data.get("property_dept", ""), data.get("property_code", ""),
                data["bank_account_key"], recipient_key, ben_key, bi_key,
                data["approver_key"], data["controller_key"],
                data["prepared_by_key"],  # requester owns the actionable work while drafting
                data["request_type"], data["treasury_service_date"], today,
                data["amount"], data.get("currency", "USD"), data.get("payment_purpose", ""),
                1 if data.get("urgent") else 0, data.get("urgency_reason", ""),
                workflow.STATUS_DRAFT, stage_for_status(workflow.STATUS_DRAFT),
                0, 0,
                now, now,
            ],
        )
        txn_key = cur.fetchone()[0]

        _save_recipient_mode_state(cur, txn_key, data, now)

        # Verification entered so far travels with the Draft (no workflow side effects).
        if "instructions_previously_used" in data:
            _upsert_transaction_verification(cur, txn_key, data, data["prepared_by_key"], now)

        conn.commit()
        return request_id, txn_key
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def update_draft(transaction_key, data: dict, *, prepared_by_user_key) -> None:
    """
    Update an existing Draft in place (Batch 8) — never inserts a new
    Beneficiary/BeneficiaryBankInstruction/ETransaction row; Transaction_Key
    remains the durable identity across repeated Save Draft clicks.

    Guarded by ownership + Current_Status='Draft' — raises DraftNotEditableError
    if the transaction doesn't exist, isn't owned by prepared_by_user_key, or is
    no longer a Draft (already submitted/cancelled/completed).

    Receiving account/routing numbers use the same blank-preserves-existing
    pattern as bank_account edits (Batch 3): a blank value here means "keep the
    currently stored value" — the caller (app.py) is responsible for never
    passing back a DDM-masked placeholder as if it were a real edited value.
    """
    now = datetime.now()

    # See create_draft() — classification may still be blank while editing an
    # in-progress Draft; Entity_Key stays NULL/unchanged until it's chosen.
    classification = (data.get("classification") or "").strip().lower()
    entity_key = None
    if classification:
        entity_key = resolve_entity_key(
            classification,
            property_entity_number=data.get("property_entity_number"),
            property_name=data.get("property_dept"),
        )

    conn = get_connection()
    try:
        cur = conn.cursor()

        cur.execute(
            "SELECT Beneficiary_Key, BeneficiaryInstruction_Key FROM [etransactions].[ETransaction] "
            "WHERE Transaction_Key = ? AND PreparedBy_User_Key = ? AND Current_Status = ?",
            [transaction_key, prepared_by_user_key, workflow.STATUS_DRAFT],
        )
        row = cur.fetchone()
        if row is None:
            raise DraftNotEditableError("This draft could not be found, is not yours, or is no longer editable.")
        ben_key, bi_key = row

        bulk = data.get("recipient_mode") == RECIPIENT_MODE_DB_BULK
        new_links = None       # (beneficiary_key, instruction_key) to store when the single-recipient rows change
        orphaned_rows = None   # this Draft's own rows to delete once ETransaction no longer references them
        recipient_key = data.get("recipient_bank_account_key") or None

        if bulk:
            # Bulk recipients have no single beneficiary: detach (and later delete) any rows an earlier save made.
            recipient_key = None
            if ben_key is not None or bi_key is not None:
                new_links, orphaned_rows = (None, None), (ben_key, bi_key)
        elif ben_key is None or bi_key is None:
            # A bulk Draft switching to a single recipient: it has no rows yet, so create them.
            new_ben, new_bi, recipient_key = _insert_beneficiary_and_instruction(
                cur, data, now.date(), require_routing=False)
            new_links = (new_ben, new_bi)
        else:
            cur.execute(
                "UPDATE [etransactions].[Beneficiary] "
                "SET Payee_Name = ?, Contact_Name = ?, Contact_Email = ?, Contact_Phone = ? "
                "WHERE Beneficiary_Key = ?",
                [data["recv_payee_name"], data.get("recv_contact_name", ""),
                 data.get("recv_contact_email", ""), data.get("recv_contact_phone", ""), ben_key],
            )

            if recipient_key:
                # Internal recipient: always refresh the whole snapshot from the
                # maintained account (never "keep existing") — also covers a Draft
                # switching from an external to an internal recipient.
                snap = _load_internal_recipient_snapshot(cur, recipient_key, data.get("bank_account_key"))
                bi_set = ["Receiving_Bank_Name = ?", "Receiving_Account_Name = ?", "Bank_Beneficiary_Address = ?",
                          "Receiving_Account_Number = ?", "Receiving_Routing_Number = ?"]
                bi_params = [snap["bank_name"], snap["account_name"], snap["address"],
                             snap["account_number"], snap["routing_number"]]
            else:
                bi_set = ["Receiving_Bank_Name = ?", "Receiving_Account_Name = ?", "Bank_Beneficiary_Address = ?"]
                bi_params = [data["recv_bank_name"], data.get("recv_account_name", ""), data.get("recv_bank_address", "")]
                # Blank account/routing number means "keep the existing stored value" —
                # never overwrite a real value with a blank/placeholder (Batch 3 principle).
                # app.py never lets a blank through for a Draft that was previously an
                # INTERNAL recipient (its stored values are a snapshot, not the requester's).
                if (data.get("recv_account_number") or "").strip():
                    bi_set.append("Receiving_Account_Number = ?")
                    bi_params.append(data["recv_account_number"])
                if (data.get("recv_routing_number") or "").strip():
                    bi_set.append("Receiving_Routing_Number = ?")
                    bi_params.append(data["recv_routing_number"])
            bi_params.append(bi_key)
            cur.execute(
                f"UPDATE [etransactions].[BeneficiaryBankInstruction] SET {', '.join(bi_set)} "
                "WHERE BeneficiaryInstruction_Key = ?",
                bi_params,
            )

        cur.execute(
            "UPDATE [etransactions].[ETransaction] SET"
            "  Property_Department_Text = ?, Entity_ID_Text = ?,"
            + ("  Entity_Key = ?," if classification else "")
            + ("  Beneficiary_Key = ?, BeneficiaryInstruction_Key = ?," if new_links else "")
            + "  OriginatingBankAccount_Key = ?, RecipientBankAccount_Key = ?,"
            "  SelectedApprover_User_Key = ?, SelectedController_User_Key = ?,"
            "  Request_Type = ?, Treasury_Service_Date = ?,"
            "  Amount = ?, Currency = ?, Payment_Purpose = ?,"
            "  Urgent_Flag = ?, Urgency_Reason = ?, Modified_DateTime = ?"
            " WHERE Transaction_Key = ? AND PreparedBy_User_Key = ? AND Current_Status = ?",
            [
                data.get("property_dept", ""), data.get("property_code", ""),
                *([entity_key] if classification else []),
                *(list(new_links) if new_links else []),
                data["bank_account_key"], recipient_key, data["approver_key"], data["controller_key"],
                data["request_type"], data["treasury_service_date"],
                data["amount"], data.get("currency", "USD"), data.get("payment_purpose", ""),
                1 if data.get("urgent") else 0, data.get("urgency_reason", ""), now,
                transaction_key, prepared_by_user_key, workflow.STATUS_DRAFT,
            ],
        )
        if cur.rowcount == 0:
            conn.rollback()
            raise DraftNotEditableError("This draft could not be found, is not yours, or is no longer editable.")

        if orphaned_rows:
            _delete_beneficiary_rows(cur, *orphaned_rows)

        _save_recipient_mode_state(cur, transaction_key, data, now)

        if "instructions_previously_used" in data:
            _upsert_transaction_verification(cur, transaction_key, data, prepared_by_user_key, now)

        conn.commit()
    except DraftNotEditableError:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _avs_score_text(value) -> str:
    """AVS_Score (decimal) as the whole-number text the intake input uses; blank when not entered."""
    if value is None:
        return ""
    return str(int(value)) if value == int(value) else str(value)


def get_draft_for_edit(transaction_key, *, prepared_by_user_key):
    """
    Return a Draft's stored fields for populating the intake form on Resume, or
    None if not found/not owned/not a Draft. Receiving account/routing numbers
    are only ever returned masked (banking_security) — never raw — consistent
    with Batch 3; the intake form must treat a blank input as "keep existing"
    rather than re-saving a masked placeholder as if it were real (see
    update_draft()).
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT t.Transaction_Key, t.Request_ID, t.Property_Department_Text, t.Entity_ID_Text,
                   t.OriginatingBankAccount_Key, t.RecipientBankAccount_Key,
                   t.SelectedApprover_User_Key, t.SelectedController_User_Key,
                   t.Request_Type, t.Treasury_Service_Date, t.Amount, t.Currency, t.Payment_Purpose,
                   t.Urgent_Flag, t.Urgency_Reason, t.Prepared_Date,
                   b.Payee_Name, b.Contact_Name, b.Contact_Email, b.Contact_Phone,
                   bi.Receiving_Bank_Name, bi.Receiving_Account_Name,
                   bi.Receiving_Account_Number, bi.Receiving_Routing_Number, bi.Bank_Beneficiary_Address,
                   be.Classification, be.Entity_ID,
                   v.Instructions_Previously_Used, v.Last_Used_Date, priorTxn.Request_ID AS Prior_Request_ID,
                   v.Verbal_Confirmed, v.Confirmed_With_KnownContact_Flag, v.Confirmed_With_Requester_Flag,
                   v.Verbal_Contact_Name, v.Verbal_Confirm_DateTime, v.AVS_Score,
                   v.External_Source_Flag, v.Internal_Doc_Not_Used_Flag
            FROM [etransactions].[ETransaction] t
            LEFT JOIN [etransactions].[Beneficiary] b ON b.Beneficiary_Key = t.Beneficiary_Key
            LEFT JOIN [etransactions].[BeneficiaryBankInstruction] bi ON bi.BeneficiaryInstruction_Key = t.BeneficiaryInstruction_Key
            LEFT JOIN [etransactions].[BusinessEntity] be ON be.Entity_Key = t.Entity_Key
            LEFT JOIN [etransactions].[TransactionVerification] v ON v.Transaction_Key = t.Transaction_Key
            LEFT JOIN [etransactions].[ETransaction] priorTxn ON priorTxn.Transaction_Key = v.Prior_Transaction_Key
            WHERE t.Transaction_Key = ? AND t.PreparedBy_User_Key = ? AND t.Current_Status = ?
            """,
            [transaction_key, prepared_by_user_key, workflow.STATUS_DRAFT],
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [c[0] for c in cur.description]
        d = dict(zip(cols, row))
    finally:
        conn.close()

    classification = (d["Classification"] or "").strip().lower()
    draft = {
        "transaction_key":  d["Transaction_Key"],
        "request_id":       d["Request_ID"],
        "property_dept":    d["Property_Department_Text"] or "",
        "property_code":    d["Entity_ID_Text"] or "",
        "classification":   classification,
        "property_entity_number": int(d["Entity_ID"]) if classification == "property" and d["Entity_ID"] else None,
        "bank_account_key": d["OriginatingBankAccount_Key"],
        "approver_key":     d["SelectedApprover_User_Key"],
        "controller_key":   d["SelectedController_User_Key"],
        "request_type":     d["Request_Type"] or "",
        "treasury_service_date": d["Treasury_Service_Date"].strftime("%Y-%m-%d") if d["Treasury_Service_Date"] else "",
        "amount":           float(d["Amount"] or 0),
        "currency":         d["Currency"] or "USD",
        "payment_purpose":  d["Payment_Purpose"] or "",
        "urgent":           bool(d["Urgent_Flag"]),
        "urgency_reason":   d["Urgency_Reason"] or "",
        "prepared_date":    d["Prepared_Date"].strftime("%Y-%m-%d") if d["Prepared_Date"] else "",
        "recv_payee_name":    d["Payee_Name"] or "",
        "recv_contact_name":  d["Contact_Name"] or "",
        "recv_contact_email": d["Contact_Email"] or "",
        "recv_contact_phone": d["Contact_Phone"] or "",
        "recv_bank_name":     d["Receiving_Bank_Name"] or "",
        "recv_account_name":  d["Receiving_Account_Name"] or "",
        "recv_bank_address":  d["Bank_Beneficiary_Address"] or "",
        "recv_account_number_masked": banking_security.mask_account_number(d["Receiving_Account_Number"] or ""),
        "recv_routing_number_masked": banking_security.mask_routing_number(d["Receiving_Routing_Number"] or ""),
        "originating_bank_account": _draft_originating_bank_account_summary(d["OriginatingBankAccount_Key"]),
        "recipient_bank_account_key": d["RecipientBankAccount_Key"],
        "recipient_mode": "internal" if d["RecipientBankAccount_Key"] else "external",
        "recipient_account": (
            get_internal_recipient_summary(d["RecipientBankAccount_Key"]) if d["RecipientBankAccount_Key"] else None
        ),
        # Verification entered so far, in the formats the intake form's inputs use.
        "instructions_previously_used": bool(d["Instructions_Previously_Used"]),
        "last_used_date":   d["Last_Used_Date"].strftime("%Y-%m-%d") if d["Last_Used_Date"] else "",
        "prior_request_id": d["Prior_Request_ID"] or "",
        "verbal_confirmed": bool(d["Verbal_Confirmed"]),
        "verbal_known_contact": bool(d["Confirmed_With_KnownContact_Flag"]),
        "verbal_requester": bool(d["Confirmed_With_Requester_Flag"]),
        "verbal_contact_name": d["Verbal_Contact_Name"] or "",
        "verbal_confirm_datetime": (
            d["Verbal_Confirm_DateTime"].strftime("%Y-%m-%dT%H:%M") if d["Verbal_Confirm_DateTime"] else ""
        ),
        "avs_score":        _avs_score_text(d["AVS_Score"]),
        "external_source":  bool(d["External_Source_Flag"]),
        "internal_doc_not_used": bool(d["Internal_Doc_Not_Used_Flag"]),
        "bulk_recipient_rows": 0, "bulk_matched_rows": 0, "bulk_validated": False,
    }
    if bulk_schema_ready():
        state = get_bulk_state(draft["transaction_key"])
        draft["recipient_mode"] = effective_recipient_mode(state["mode"], d["RecipientBankAccount_Key"])
        draft["bulk_recipient_rows"] = state["recipient_rows"]
        draft["bulk_matched_rows"] = state["matched_rows"]
        draft["bulk_validated"] = state["matched_rows"] > 0 and state["matched_rows"] == state["recipient_rows"]
    return draft


def _draft_originating_bank_account_summary(bank_account_key):
    """
    Safe, masked display summary of a Draft's already-selected originating
    BankAccount for Resume (Batch 8 follow-up Part 2) — bank name/account
    title/last 4 digits only, never the full account/routing number.
    """
    if not bank_account_key:
        return None
    record = get_bank_account_record(bank_account_key)
    if record is None:
        return None
    masked = record.get("accountnumber") or ""
    last4 = masked[-4:] if len(masked) >= 4 else ""
    title = record.get("accounttitle") or record.get("accountnameid") or record.get("systemaccountname") or ""
    return {
        "bank_name": record.get("bankname") or "",
        "account_title": title,
        "last4": last4,
    }


def finalize_draft_submission(transaction_key, data: dict, *, prepared_by_user_key):
    """
    Convert an existing Draft into a normal submitted transaction (Batch 8) —
    the SAME Transaction_Key/Request_ID, never a second row. Caller (app.py)
    must have already run the full Batch 1 final-submission validator and
    Batch 7 resolve_approval_rule() before calling this — this function only
    persists the already-validated result, atomically:
      1. Update Beneficiary/BeneficiaryBankInstruction with final values.
      2. Update ETransaction: Status/Stage -> Pending Approver, CurrentOwner ->
         Approver, ApprovalRule_Key/Requires_VP/Requires_CFO/Approval_Tier_Snapshot,
         Submitted_Date = now. Guarded by PreparedBy_User_Key + Current_Status='Draft'
         (also protects against double-submit — a second call after the first
         succeeds finds Current_Status no longer 'Draft' and raises
         WorkflowConflictError, same optimistic-concurrency pattern as
         advance_transaction_workflow()).
      3. Write TransactionVerification (updates the Draft's saved verification row,
         or inserts one if the Draft never had one — always exactly one row).
      4. Insert the initial "Approver" WorkflowAssignment.
      5. Insert WorkflowEvent rows: Submitted, APPROVER_ASSIGNED (Batch 6
         contract, unchanged — no DRAFT_SAVED event exists or is needed).

    Raises WorkflowConflictError if the transaction is not (still) an owned
    Draft — no rows are written.
    """
    now = datetime.now()
    today = now.date()
    initial_status = workflow.STATUS_PENDING_APPROVER

    # classification is required by app.py's full final-submission validator by
    # this point, unlike create_draft()/update_draft() where it may still be
    # blank — always resolved here so Entity_Key reflects the final choice even
    # if it was changed after the draft was first saved.
    entity_key = resolve_entity_key(
        data.get("classification"),
        property_entity_number=data.get("property_entity_number"),
        property_name=data.get("property_dept"),
    )

    conn = get_connection()
    try:
        cur = conn.cursor()

        cur.execute(
            "SELECT Beneficiary_Key, BeneficiaryInstruction_Key FROM [etransactions].[ETransaction] "
            "WHERE Transaction_Key = ? AND PreparedBy_User_Key = ? AND Current_Status = ?",
            [transaction_key, prepared_by_user_key, workflow.STATUS_DRAFT],
        )
        row = cur.fetchone()
        if row is None:
            raise WorkflowConflictError(
                "This draft could not be found, is not yours, or has already been submitted."
            )
        ben_key, bi_key = row

        bulk = data.get("recipient_mode") == RECIPIENT_MODE_DB_BULK
        new_links = None
        orphaned_rows = None
        recipient_key = data.get("recipient_bank_account_key") or None

        if bulk:
            recipient_key = None
            if ben_key is not None or bi_key is not None:
                new_links, orphaned_rows = (None, None), (ben_key, bi_key)
        elif ben_key is None or bi_key is None:
            # A bulk Draft finalized as a single recipient: create the rows now.
            new_ben, new_bi, recipient_key = _insert_beneficiary_and_instruction(
                cur, data, today, require_routing=data["request_type"] == "ACH")
            new_links = (new_ben, new_bi)
        else:
            cur.execute(
                "UPDATE [etransactions].[Beneficiary] "
                "SET Payee_Name = ?, Contact_Name = ?, Contact_Email = ?, Contact_Phone = ? "
                "WHERE Beneficiary_Key = ?",
                [data["recv_payee_name"], data.get("recv_contact_name", ""),
                 data.get("recv_contact_email", ""), data.get("recv_contact_phone", ""), ben_key],
            )

            if recipient_key:
                # Internal recipient: re-read the CURRENT maintained account inside
                # this transaction (Open status re-validated, real values only) and
                # make it the final snapshot. After this commit nothing ever updates
                # these rows again (both draft writers are guarded by Current_Status='Draft'),
                # so later BankAccount edits cannot change the historical instructions.
                snap = _load_internal_recipient_snapshot(
                    cur, recipient_key, data.get("bank_account_key"), require_routing=data["request_type"] == "ACH")
                bi_set = ["Receiving_Bank_Name = ?", "Receiving_Account_Name = ?", "Bank_Beneficiary_Address = ?",
                          "Receiving_Account_Number = ?", "Receiving_Routing_Number = ?"]
                bi_params = [snap["bank_name"], snap["account_name"], snap["address"],
                             snap["account_number"], snap["routing_number"]]
            else:
                bi_set = ["Receiving_Bank_Name = ?", "Receiving_Account_Name = ?", "Bank_Beneficiary_Address = ?"]
                bi_params = [data["recv_bank_name"], data.get("recv_account_name", ""), data.get("recv_bank_address", "")]
                if (data.get("recv_account_number") or "").strip():
                    bi_set.append("Receiving_Account_Number = ?")
                    bi_params.append(data["recv_account_number"])
                if (data.get("recv_routing_number") or "").strip():
                    bi_set.append("Receiving_Routing_Number = ?")
                    bi_params.append(data["recv_routing_number"])
            bi_params.append(bi_key)
            cur.execute(
                f"UPDATE [etransactions].[BeneficiaryBankInstruction] SET {', '.join(bi_set)} "
                "WHERE BeneficiaryInstruction_Key = ?",
                bi_params,
            )

        cur.execute(
            "UPDATE [etransactions].[ETransaction] SET"
            "  Property_Department_Text = ?, Entity_ID_Text = ?, Entity_Key = ?,"
            + ("  Beneficiary_Key = ?, BeneficiaryInstruction_Key = ?," if new_links else "")
            + "  OriginatingBankAccount_Key = ?, RecipientBankAccount_Key = ?,"
            "  SelectedApprover_User_Key = ?, SelectedController_User_Key = ?,"
            "  CurrentOwner_User_Key = ?, ApprovalRule_Key = ?,"
            "  Request_Type = ?, Treasury_Service_Date = ?, Submitted_Date = ?,"
            "  Amount = ?, Currency = ?, Payment_Purpose = ?,"
            "  Urgent_Flag = ?, Urgency_Reason = ?,"
            "  Current_Status = ?, Current_Workflow_Stage = ?,"
            "  Approval_Tier_Snapshot = ?, Requires_VP = ?, Requires_CFO = ?,"
            "  Modified_DateTime = ?"
            " WHERE Transaction_Key = ? AND PreparedBy_User_Key = ? AND Current_Status = ?",
            [
                data.get("property_dept", ""), data.get("property_code", ""), entity_key,
                *(list(new_links) if new_links else []),
                data["bank_account_key"], recipient_key, data["approver_key"], data["controller_key"],
                data["approver_key"], data["approval_rule_key"],
                data["request_type"], data["treasury_service_date"], now,
                data["amount"], data.get("currency", "USD"), data.get("payment_purpose", ""),
                1 if data.get("urgent") else 0, data.get("urgency_reason", ""),
                initial_status, stage_for_status(initial_status),
                data["approval_tier"], 1 if data["requires_vp"] else 0, 1 if data["requires_cfo"] else 0,
                now,
                transaction_key, prepared_by_user_key, workflow.STATUS_DRAFT,
            ],
        )
        if cur.rowcount == 0:
            conn.rollback()
            raise WorkflowConflictError(
                "This draft could not be found, is not yours, or has already been submitted."
            )

        if orphaned_rows:
            _delete_beneficiary_rows(cur, *orphaned_rows)

        # Recipient mode + the bulk recipients verified against the listing just re-read by the caller.
        _save_recipient_mode_state(cur, transaction_key, data, now, submit=True)

        _upsert_transaction_verification(cur, transaction_key, data, prepared_by_user_key, now)

        cur.execute(
            "INSERT INTO [etransactions].[WorkflowAssignment] ("
            "  Transaction_Key, Workflow_Role, Assigned_User_Key,"
            "  Assigned_By_User_Key, Assignment_Source,"
            "  Assigned_DateTime, Is_Current"
            ") OUTPUT INSERTED.Assignment_Key VALUES (?,?,?,?,?,?,1)",
            [transaction_key, "Approver", data["approver_key"], prepared_by_user_key, "Workflow", now],
        )
        approver_assignment_key = cur.fetchone()[0]

        cur.execute(
            "INSERT INTO [etransactions].[WorkflowEvent] ("
            "  Transaction_Key, Actor_User_Key, Actor_Role,"
            "  Event_Type, Decision, From_Status, To_Status,"
            "  Event_DateTime, Comments_Reason, Related_Assignment_Key"
            ") VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                transaction_key, prepared_by_user_key, "Submitter",
                workflow.EVENT_REQUEST_SUBMITTED, workflow.EVENT_TYPE_LABELS[workflow.EVENT_REQUEST_SUBMITTED],
                workflow.STATUS_DRAFT, initial_status, now, None, None,
            ],
        )
        cur.execute(
            "INSERT INTO [etransactions].[WorkflowEvent] ("
            "  Transaction_Key, Actor_User_Key, Actor_Role,"
            "  Event_Type, Decision, From_Status, To_Status,"
            "  Event_DateTime, Comments_Reason, Related_Assignment_Key"
            ") VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                transaction_key, prepared_by_user_key, "Submitter",
                workflow.EVENT_APPROVER_ASSIGNED, workflow.EVENT_TYPE_LABELS[workflow.EVENT_APPROVER_ASSIGNED],
                workflow.STATUS_DRAFT, initial_status, now, None, approver_assignment_key,
            ],
        )

        conn.commit()
    except WorkflowConflictError:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
