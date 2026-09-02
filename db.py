"""
Database connection module — Microsoft Fabric SQL endpoint via Entra ID.

[STORAGE] TODO: Switch authentication to 'ActiveDirectoryMsi' when deployed to Azure App Service
                so no credentials are needed in the environment at all.
"""

import os
import mssql_python

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


def get_connection():
    """
    Return an open mssql_python connection using Entra Interactive (MFA) auth.
    Caller is responsible for closing the connection.

    [STORAGE] TODO: Replace Authentication value with 'ActiveDirectoryMsi' for Azure deployment.
    """
    server   = os.environ.get("DB_SERVER", "")
    database = os.environ.get("DB_NAME", "")

    if not server or not database:
        raise RuntimeError(
            "DB_SERVER and DB_NAME must be set in .env before connecting."
        )

    conn_str = (
        f"SERVER={server};"
        f"DATABASE={database};"
        f"Authentication=ActiveDirectoryInteractive;"
        f"Encrypt=yes;"
        f"TrustServerCertificate=no;"
    )
    return mssql_python.connect(conn_str)


def table(name: str) -> str:
    """Return a fully schema-qualified table name, e.g. table('ETransaction') -> '[etransactions].[ETransaction]'"""
    return f"[{DB_SCHEMA}].[{name}]"


# ─────────────────────────────────────────────────────────────
#  Query helpers
# ─────────────────────────────────────────────────────────────

_DASHBOARD_SQL = """
SELECT
    t.Request_ID                               AS request_id,
    t.Property_Department_Text                 AS property_dept,
    t.Entity_ID_Text                           AS property_code,
    t.Request_Type                             AS request_type,
    t.Treasury_Service_Date                    AS treasury_service_date,
    t.Prepared_Date                            AS prepared_date,
    t.Submitted_Date                           AS submitted_date,
    t.Amount                                   AS amount,
    t.Currency                                 AS currency,
    t.Payment_Purpose                          AS payment_purpose,
    t.Urgent_Flag                              AS urgent,
    t.Urgency_Reason                           AS urgency_reason,
    t.Current_Status                           AS status,
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
    DATEDIFF(day, t.Submitted_Date, GETDATE()) AS days_pending
FROM [etransactions].[ETransaction] t
LEFT JOIN [etransactions].[AppUser] prep  ON prep.User_Key  = t.PreparedBy_User_Key
LEFT JOIN [etransactions].[AppUser] owner ON owner.User_Key = t.CurrentOwner_User_Key
LEFT JOIN [etransactions].[AppUser] sam   ON sam.User_Key   = t.SelectedApprover_User_Key
LEFT JOIN [etransactions].[AppUser] ctrl  ON ctrl.User_Key  = t.SelectedController_User_Key
LEFT JOIN [etransactions].[AppUser] vp    ON vp.User_Key    = t.VPApprover_User_Key
LEFT JOIN [etransactions].[AppUser] cfo   ON cfo.User_Key   = t.CFOApprover_User_Key
ORDER BY t.Submitted_Date DESC
"""


def get_dashboard_records():
    """[STORAGE] Return all transaction rows shaped for the dashboard. Replaces MOCK_REQUESTS."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(_DASHBOARD_SQL)
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
        for key in ("treasury_service_date", "prepared_date", "submitted_date"):
            val = d.get(key)
            d[key] = val.strftime("%Y-%m-%d") if hasattr(val, "strftime") else (val or "")
        # Decimal → float
        d["amount"] = float(d.get("amount") or 0)
        # alias for templates that reference current_workflow_owner
        d["current_workflow_owner"] = d.get("assigned_approver") or ""
        records.append(d)
    return records


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
    v.Last_Used_Date                               AS last_used_date
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
    ISNULL(u.Display_Name, we.Actor_Role) AS actor_name
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

# Maps lowercase/stripped Event_Type values to template dot CSS class names
_EVENT_TYPE_MAP = {
    "submitted":        "submitted",
    "approved":         "approve",
    "rejected":         "reject",
    "moreinfo":         "more_info",
    "moreinformation":  "more_info",
    "more_info":        "more_info",
    "treasuryreviewed": "treasury_reviewed",
    "released":         "mark_released",
    "completed":        "mark_completed",
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

    d["current_workflow_owner"] = d.get("assigned_approver") or ""
    d["notes_recv"]        = ""   # not yet stored per-instruction
    d["docs_checklist"]    = {}   # attachment checklist not yet in DB
    d["attachments"]       = {}   # file attachments not yet in DB
    d["extra_attachments"] = []

    # Build timeline list
    timeline = []
    for r in timeline_rows:
        evt_type  = r[0] or ""
        actor     = r[6]        # actor_name (Display_Name or Actor_Role fallback)
        to_status = r[3] or ""
        evt_dt    = r[4]
        evt_date  = evt_dt.strftime("%Y-%m-%d") if hasattr(evt_dt, "strftime") else str(evt_dt or "")
        dot_type  = _EVENT_TYPE_MAP.get(evt_type.lower().replace(" ", "").replace("_", ""), "routed")
        event_label = f"{evt_type} by {actor}" if actor else evt_type
        timeline.append({"date": evt_date, "event": event_label, "status": to_status, "type": dot_type})
    d["timeline"] = timeline

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


def get_bank_accounts():
    """Return all active company bank accounts for the originating-account dropdown."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT BankAccount_Key, BankName, AccountTitle, RIGHT(AccountNumber, 4) AS account_last4 "
            "FROM [etransactions].[BankAccount] "
            "WHERE Status = 'Active' ORDER BY AccountTitle"
        )
        return [{"bank_account_key": r[0], "bank_name": r[1], "account_title": r[2], "account_last4": r[3]}
                for r in cur.fetchall()]
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────
#  Write path — new transaction submission
# ─────────────────────────────────────────────────────────────

def insert_transaction(data: dict) -> str:
    """
    Insert all rows for a new transaction in a single transaction.
    Inserts: Beneficiary, BeneficiaryBankInstruction, ETransaction,
             TransactionVerification, WorkflowEvent (Submitted).
    Returns the generated Request_ID.
    """
    from datetime import datetime as _dt
    import random as _random

    now   = _dt.now()
    today = now.date()

    conn = get_connection()
    try:
        cur = conn.cursor()

        # Look up the only/first active BusinessEntity
        cur.execute(
            "SELECT TOP 1 Entity_Key FROM [etransactions].[BusinessEntity] WHERE Active_Status = 1"
        )
        entity_key = cur.fetchone()[0]

        # Look up ApprovalRule by amount thresholds
        cur.execute(
            "SELECT ApprovalRule_Key FROM [etransactions].[ApprovalRule] "
            "WHERE Is_Active = 1 AND Min_Amount <= ? AND (Max_Amount IS NULL OR Max_Amount >= ?)",
            [data["amount"], data["amount"]],
        )
        rule_row = cur.fetchone()
        rule_key = rule_row[0] if rule_row else None

        # Insert Beneficiary (payee)
        cur.execute(
            "INSERT INTO [etransactions].[Beneficiary] "
            "(Payee_Name, Contact_Name, Contact_Email, Contact_Phone) "
            "OUTPUT INSERTED.Beneficiary_Key VALUES (?, ?, ?, ?)",
            [data["recv_payee_name"], data.get("recv_contact_name", ""),
             data.get("recv_contact_email", ""), data.get("recv_contact_phone", "")],
        )
        ben_key = cur.fetchone()[0]

        # Insert BeneficiaryBankInstruction (receiving bank)
        cur.execute(
            "INSERT INTO [etransactions].[BeneficiaryBankInstruction] "
            "(Beneficiary_Key, Receiving_Bank_Name, Receiving_Account_Name, "
            " Receiving_Account_Number, Receiving_Routing_Number, "
            " Bank_Beneficiary_Address, Is_Current, Effective_From) "
            "OUTPUT INSERTED.BeneficiaryInstruction_Key VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
            [ben_key, data["recv_bank_name"], data["recv_account_name"],
             data["recv_account_number"], data["recv_routing_number"],
             data.get("recv_bank_address", ""), today],
        )
        bi_key = cur.fetchone()[0]

        # Generate a unique Request_ID
        request_id = None
        for _ in range(10):
            candidate = f"TXN-{now.year}-{_random.randint(1000, 9999)}"
            cur.execute(
                "SELECT 1 FROM [etransactions].[ETransaction] WHERE Request_ID = ?",
                [candidate],
            )
            if not cur.fetchone():
                request_id = candidate
                break
        if not request_id:
            raise RuntimeError("Unable to generate a unique Request_ID.")

        tier           = data["approval_tier"]
        initial_status = data["status"]
        requires_vp    = tier in ("Vice President", "Vice President + CFO")
        requires_cfo   = tier == "Vice President + CFO"

        # Insert ETransaction
        cur.execute(
            "INSERT INTO [etransactions].[ETransaction] ("
            "  Request_ID, PreparedBy_User_Key,"
            "  Entity_Key, Property_Department_Text, Entity_ID_Text,"
            "  OriginatingBankAccount_Key, Beneficiary_Key, BeneficiaryInstruction_Key,"
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
            "  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                request_id,
                data["prepared_by_key"],
                entity_key,
                data.get("property_dept", ""),
                data.get("property_code", ""),
                data["bank_account_key"],
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
                "Approver",
                tier,
                1 if requires_vp  else 0,
                1 if requires_cfo else 0,
                now,
                now,
            ],
        )
        txn_key = cur.fetchone()[0]

        # Insert TransactionVerification
        cur.execute(
            "INSERT INTO [etransactions].[TransactionVerification] ("
            "  Transaction_Key,"
            "  Instructions_Previously_Used, Last_Used_Date,"
            "  Verbal_Confirmed,"
            "  Confirmed_With_KnownContact_Flag, Confirmed_With_Requester_Flag,"
            "  Verbal_Contact_Name, Verbal_Confirm_DateTime,"
            "  AVS_Score, External_Source_Flag, Internal_Doc_Not_Used_Flag,"
            "  Verified_By_User_Key, Created_DateTime"
            ") OUTPUT INSERTED.Verification_Key VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                txn_key,
                1 if data.get("instructions_previously_used") else 0,
                data.get("last_used_date") or None,
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

        # Insert WorkflowEvent — Submitted
        cur.execute(
            "INSERT INTO [etransactions].[WorkflowEvent] ("
            "  Transaction_Key, Actor_User_Key, Actor_Role,"
            "  Event_Type, Decision, From_Status, To_Status,"
            "  Event_DateTime, Comments_Reason"
            ") OUTPUT INSERTED.WorkflowEvent_Key VALUES (?,?,?,?,?,?,?,?,?)",
            [
                txn_key,
                data["prepared_by_key"],
                "Submitter",
                "Submitted", "Submitted",
                None, initial_status,
                now, None,
            ],
        )

        conn.commit()
        return request_id

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
