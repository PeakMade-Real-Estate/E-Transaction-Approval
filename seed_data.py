"""
seed_data.py — Insert four test transactions (one per approval tier) into the live DB.

Run once:  .venv\\Scripts\\python.exe seed_data.py

Tiers covered:
  TXN-2026-101  $85,000    ACH   → Pending SAM Approval
  TXN-2026-102  $325,000   Wire  → Pending Controller Approval
  TXN-2026-103  $750,000   Wire  → Pending VP Approval  (urgent)
  TXN-2026-104  $1,250,000 Wire  → Pending CFO Approval
"""

from datetime import date, datetime
from dotenv import load_dotenv
load_dotenv()
import db

S = "etransactions"
TODAY = date.today().isoformat()
NOW   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ins(cur, sql, params=None):
    """Run INSERT ... OUTPUT INSERTED.key and return the generated PK."""
    cur.execute(sql, params or [])
    return cur.fetchone()[0]


conn = db.get_connection()
cur  = conn.cursor()

try:
    # ── AccountingGroup ───────────────────────────────────────────────────────
    print("Inserting AccountingGroup...")
    ag_key = ins(cur, f"""
        INSERT INTO [{S}].[AccountingGroup] (AccountingGroup_Name, Classification, Active_Status)
        OUTPUT INSERTED.AccountingGroup_Key VALUES (?, ?, 1)
    """, ["Treasury / Corporate Finance", "Corporate"])
    print(f"  AccountingGroup_Key={ag_key}")

    # ── BusinessEntity ────────────────────────────────────────────────────────
    print("Inserting BusinessEntity...")
    be_key = ins(cur, f"""
        INSERT INTO [{S}].[BusinessEntity]
            (Entity_ID, Property_Department_Name, Classification, AccountingGroup_Key, Active_Status)
        OUTPUT INSERTED.Entity_Key VALUES (?, ?, ?, ?, 1)
    """, ["CORP-001", "Treasury / Corporate Finance", "Corporate", ag_key])
    print(f"  Entity_Key={be_key}")

    # ── ApprovalRule ──────────────────────────────────────────────────────────
    print("Inserting ApprovalRule rows...")
    rule_rows = [
        ("sam",        "v1-SAM",        0,        249999.99, 1, 1, 0, 0, "SAM / Assistant Controller tier"),
        ("controller", "v1-Controller", 250000,   499999.99, 1, 1, 0, 0, "Controller tier"),
        ("vp",         "v1-VP",         500000,   999999.99, 1, 1, 1, 0, "VP threshold"),
        ("cfo",        "v1-CFO",        1000000,  None,      1, 1, 1, 1, "VP + CFO threshold"),
    ]
    rules = {}
    for label, version, mn, mx, ra, rc, rv, rcfo, note in rule_rows:
        rk = ins(cur, f"""
            INSERT INTO [{S}].[ApprovalRule] (
                Rule_Version, Min_Amount, Max_Amount,
                Requires_Approver, Requires_Controller, Requires_VP, Requires_CFO,
                Effective_Start_Date, Is_Active, Notes
            )
            OUTPUT INSERTED.ApprovalRule_Key VALUES (?,?,?,?,?,?,?,?,1,?)
        """, [version, mn, mx, ra, rc, rv, rcfo, TODAY, note])
        rules[label] = rk
        print(f"  {label}: ApprovalRule_Key={rk}")

    # tier snapshot → rule key lookup
    tier_rules = {
        "Senior Accounting Manager / Assistant Controller": rules["sam"],
        "Controller":          rules["controller"],
        "Vice President":      rules["vp"],
        "Vice President + CFO": rules["cfo"],
    }

    # ── AppUser ───────────────────────────────────────────────────────────────
    print("Inserting AppUser rows...")
    user_rows = [
        ("oid-seed-001", "pbatson@company.com",   "Patricia Batson"),   # submitter
        ("oid-seed-002", "smitchell@company.com", "Sarah Mitchell"),    # SAM approver
        ("oid-seed-003", "rchen@company.com",     "Robert Chen"),       # controller
        ("oid-seed-004", "jwalsh@company.com",    "Jennifer Walsh"),    # VP
        ("oid-seed-005", "dthornton@company.com", "David Thornton"),    # CFO
        ("oid-seed-006", "mevans@company.com",    "Marcus Evans"),      # treasury
    ]
    users = {}
    for oid, email, name in user_rows:
        key = ins(cur, f"""
            INSERT INTO [{S}].[AppUser] (Entra_Object_ID, Email, Display_Name, Active_Status)
            OUTPUT INSERTED.User_Key VALUES (?, ?, ?, 1)
        """, [oid, email, name])
        users[name] = key
        print(f"  {name}: User_Key={key}")

    # ── BankAccount ───────────────────────────────────────────────────────────
    print("Inserting BankAccount...")
    ba_key = ins(cur, f"""
        INSERT INTO [{S}].[BankAccount] (
            Entity_Key, AccountClassification, BankName, AccountTitle,
            AccountNumber, RoutingNumber, BankContactName,
            AccountType, Status, Notes,
            CreatedDate, CreatedBy_User_Key, ModifiedDate, ModifiedBy_User_Key
        )
        OUTPUT INSERTED.BankAccount_Key
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [be_key, "Operating", "Wells Fargo Bank", "Acme Corp Operating Account",
          "4567890123", "121000248", "John Davis", "Checking", "Active", "",
          TODAY, users["Patricia Batson"], TODAY, users["Patricia Batson"]])
    print(f"  BankAccount_Key={ba_key}")

    # ── Beneficiary + BeneficiaryBankInstruction ──────────────────────────────
    print("Inserting Beneficiaries and instructions...")
    bene_data = [
        {
            "payee":    "Sunset Properties LLC",
            "contact":  "Tom Baker",
            "email":    "tbaker@sunset.com",
            "phone":    "555-100-2001",
            "bank":     "Bank of America",
            "acct_name":"Sunset Properties LLC",
            "acct_num": "11223344",
            "routing":  "026009593",
            "address":  "123 Main St, Los Angeles, CA 90001",
        },
        {
            "payee":    "Harbor View Capital",
            "contact":  "Lisa Torres",
            "email":    "ltorres@hvc.com",
            "phone":    "555-200-3002",
            "bank":     "Chase Bank",
            "acct_name":"Harbor View Capital",
            "acct_num": "22334455",
            "routing":  "021000021",
            "address":  "456 Harbor Blvd, San Francisco, CA 94105",
        },
        {
            "payee":    "Pacific Coast Funding",
            "contact":  "Mark Johnson",
            "email":    "mjohnson@pcf.com",
            "phone":    "555-300-4003",
            "bank":     "Citibank",
            "acct_name":"Pacific Coast Funding",
            "acct_num": "33445566",
            "routing":  "021272655",
            "address":  "789 Ocean Ave, Seattle, WA 98101",
        },
        {
            "payee":    "Summit Real Estate LLC",
            "contact":  "Anna Lee",
            "email":    "alee@summit.com",
            "phone":    "555-400-5004",
            "bank":     "US Bank",
            "acct_name":"Summit Real Estate LLC",
            "acct_num": "44556677",
            "routing":  "091000022",
            "address":  "321 Summit Way, Denver, CO 80201",
        },
    ]
    ben_keys  = {}
    instr_keys = {}
    for b in bene_data:
        bk = ins(cur, f"""
            INSERT INTO [{S}].[Beneficiary] (Payee_Name, Contact_Name, Contact_Email, Contact_Phone)
            OUTPUT INSERTED.Beneficiary_Key VALUES (?, ?, ?, ?)
        """, [b["payee"], b["contact"], b["email"], b["phone"]])
        ben_keys[b["payee"]] = bk

        ik = ins(cur, f"""
            INSERT INTO [{S}].[BeneficiaryBankInstruction] (
                Beneficiary_Key, Receiving_Bank_Name, Receiving_Account_Name,
                Receiving_Account_Number, Receiving_Routing_Number,
                Bank_Beneficiary_Address, Is_Current, Effective_From
            )
            OUTPUT INSERTED.BeneficiaryInstruction_Key
            VALUES (?, ?, ?, ?, ?, ?, 1, ?)
        """, [bk, b["bank"], b["acct_name"], b["acct_num"], b["routing"], b["address"], TODAY])
        instr_keys[b["payee"]] = ik
        print(f"  {b['payee']}: Beneficiary_Key={bk}, BeneficiaryInstruction_Key={ik}")

    # ── ETransaction ──────────────────────────────────────────────────────────
    print("Inserting ETransaction rows...")
    txn_rows = [
        {
            "req_id":     "TXN-2026-101",
            "payee":      "Sunset Properties LLC",
            "type":       "ACH",
            "svc_date":   "2026-08-28",
            "amount":     85000.00,
            "purpose":    "Monthly property management fee payment",
            "urgent":     0,
            "urg_reason": None,
            "status":     "Pending SAM Approval",
            "stage":      "Approver",
            "tier":       "Senior Accounting Manager / Assistant Controller",
            "req_vp":     0,
            "req_cfo":    0,
            "approver":   "Sarah Mitchell",
            "controller": "Robert Chen",
            "vp":         None,
            "cfo":        None,
            "owner":      "Sarah Mitchell",
            "releaser":   None,
        },
        {
            "req_id":     "TXN-2026-102",
            "payee":      "Harbor View Capital",
            "type":       "Wire",
            "svc_date":   "2026-08-29",
            "amount":     325000.00,
            "purpose":    "Acquisition deposit — Harbor View Phase 2",
            "urgent":     0,
            "urg_reason": None,
            "status":     "Pending Controller Approval",
            "stage":      "Controller",
            "tier":       "Controller",
            "req_vp":     0,
            "req_cfo":    0,
            "approver":   "Sarah Mitchell",
            "controller": "Robert Chen",
            "vp":         None,
            "cfo":        None,
            "owner":      "Robert Chen",
            "releaser":   "Robert Chen",
        },
        {
            "req_id":     "TXN-2026-103",
            "payee":      "Pacific Coast Funding",
            "type":       "Wire",
            "svc_date":   "2026-08-28",
            "amount":     750000.00,
            "purpose":    "Bridge loan repayment — Pacific Coast Phase 1",
            "urgent":     1,
            "urg_reason": "Loan maturity deadline 08/28",
            "status":     "Pending VP Approval",
            "stage":      "VP",
            "tier":       "Vice President",
            "req_vp":     1,
            "req_cfo":    0,
            "approver":   "Sarah Mitchell",
            "controller": "Robert Chen",
            "vp":         "Jennifer Walsh",
            "cfo":        None,
            "owner":      "Jennifer Walsh",
            "releaser":   "Jennifer Walsh",
        },
        {
            "req_id":     "TXN-2026-104",
            "payee":      "Summit Real Estate LLC",
            "type":       "Wire",
            "svc_date":   "2026-09-01",
            "amount":     1250000.00,
            "purpose":    "Land acquisition — Summit Portfolio Expansion",
            "urgent":     0,
            "urg_reason": None,
            "status":     "Pending CFO Approval",
            "stage":      "CFO",
            "tier":       "Vice President + CFO",
            "req_vp":     1,
            "req_cfo":    1,
            "approver":   "Sarah Mitchell",
            "controller": "Robert Chen",
            "vp":         "Jennifer Walsh",
            "cfo":        "David Thornton",
            "owner":      "David Thornton",
            "releaser":   "Jennifer Walsh",
        },
    ]
    txn_keys = {}
    for t in txn_rows:
        tk = ins(cur, f"""
            INSERT INTO [{S}].[ETransaction] (
                Request_ID, PreparedBy_User_Key,
                Entity_Key, Property_Department_Text,
                OriginatingBankAccount_Key, Beneficiary_Key, BeneficiaryInstruction_Key,
                SelectedApprover_User_Key, SelectedController_User_Key,
                VPApprover_User_Key, CFOApprover_User_Key,
                CurrentOwner_User_Key, BankReleaser_User_Key,
                ApprovalRule_Key,
                Request_Type, Treasury_Service_Date, Prepared_Date, Submitted_Date,
                Amount, Currency, Payment_Purpose,
                Urgent_Flag, Urgency_Reason,
                Current_Status, Current_Workflow_Stage,
                Approval_Tier_Snapshot, Requires_VP, Requires_CFO,
                Created_DateTime, Modified_DateTime
            )
            OUTPUT INSERTED.Transaction_Key
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, [
            t["req_id"],
            users["Patricia Batson"],
            be_key, "Treasury / Corporate Finance",
            ba_key,
            ben_keys[t["payee"]],
            instr_keys[t["payee"]],
            users[t["approver"]],
            users[t["controller"]],
            users[t["vp"]]  if t["vp"]  else None,
            users[t["cfo"]] if t["cfo"] else None,
            users[t["owner"]],
            users[t["releaser"]] if t["releaser"] else None,
            tier_rules.get(t["tier"]),
            t["type"], t["svc_date"], TODAY, NOW,
            t["amount"], "USD", t["purpose"],
            t["urgent"], t["urg_reason"],
            t["status"], t["stage"],
            t["tier"], t["req_vp"], t["req_cfo"],
            NOW, NOW,
        ])
        txn_keys[t["req_id"]] = tk
        print(f"  {t['req_id']} (${t['amount']:,.0f}): Transaction_Key={tk}")

    # ── TransactionVerification ───────────────────────────────────────────────
    print("Inserting TransactionVerification rows...")
    for req_id in txn_keys:
        vk = ins(cur, f"""
            INSERT INTO [{S}].[TransactionVerification] (
                Transaction_Key,
                Instructions_Previously_Used, Last_Used_Date,
                Verbal_Confirmed,
                Confirmed_With_KnownContact_Flag, Confirmed_With_Requester_Flag,
                Verbal_Contact_Name, Verbal_Confirm_DateTime,
                AVS_Score, External_Source_Flag, Internal_Doc_Not_Used_Flag,
                Verified_By_User_Key, Created_DateTime
            )
            OUTPUT INSERTED.Verification_Key
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, [
            txn_keys[req_id],
            1, "2026-07-15",
            1,
            1, 0,
            "Tom Davis", "2026-08-21 09:30:00",
            "96", 0, 0,
            users["Patricia Batson"], NOW,
        ])
        print(f"  {req_id}: Verification_Key={vk}")

    # ── WorkflowAssignment ────────────────────────────────────────────────────
    print("Inserting WorkflowAssignment rows...")
    # (role, txn_req_id, user_name, is_current, has_end_date)
    assignments = [
        # TXN-101: waiting at SAM — both active
        ("Approver",    "TXN-2026-101", "Sarah Mitchell",  1, False),
        ("Controller",  "TXN-2026-101", "Robert Chen",     1, False),
        # TXN-102: SAM done, Controller active
        ("Approver",    "TXN-2026-102", "Sarah Mitchell",  0, True),
        ("Controller",  "TXN-2026-102", "Robert Chen",     1, False),
        # TXN-103: SAM + Controller done, VP active
        ("Approver",    "TXN-2026-103", "Sarah Mitchell",  0, True),
        ("Controller",  "TXN-2026-103", "Robert Chen",     0, True),
        ("VP",          "TXN-2026-103", "Jennifer Walsh",  1, False),
        # TXN-104: SAM + Controller + VP done, CFO active
        ("Approver",    "TXN-2026-104", "Sarah Mitchell",  0, True),
        ("Controller",  "TXN-2026-104", "Robert Chen",     0, True),
        ("VP",          "TXN-2026-104", "Jennifer Walsh",  0, True),
        ("CFO",         "TXN-2026-104", "David Thornton",  1, False),
    ]
    for role, req_id, uname, is_cur, has_end in assignments:
        ak = ins(cur, f"""
            INSERT INTO [{S}].[WorkflowAssignment] (
                Transaction_Key, Workflow_Role, Assigned_User_Key,
                Assigned_By_User_Key, Assignment_Source,
                Assigned_DateTime, End_DateTime, Is_Current
            )
            OUTPUT INSERTED.Assignment_Key
            VALUES (?,?,?,?,?,?,?,?)
        """, [
            txn_keys[req_id], role, users[uname],
            users["Patricia Batson"], "Initial",
            NOW, NOW if has_end else None, is_cur,
        ])
        print(f"  {req_id} {role}: Assignment_Key={ak}")

    # ── WorkflowEvent ─────────────────────────────────────────────────────────
    print("Inserting WorkflowEvent rows...")
    # (req_id, actor_name_or_None, actor_role, event_type, decision, from_status, to_status, comment)
    events = [
        # TXN-101
        ("TXN-2026-101", "Patricia Batson", "Submitter", "Submitted", "Submitted",
         None, "Pending SAM Approval", None),
        # TXN-102
        ("TXN-2026-102", "Patricia Batson", "Submitter", "Submitted", "Submitted",
         None, "Pending SAM Approval", None),
        ("TXN-2026-102", "Sarah Mitchell",  "SAM",       "Approved",  "Approved",
         "Pending SAM Approval", "Pending Controller Approval", "Instructions verified and amount confirmed."),
        # TXN-103
        ("TXN-2026-103", "Patricia Batson", "Submitter", "Submitted", "Submitted",
         None, "Pending SAM Approval", None),
        ("TXN-2026-103", "Sarah Mitchell",  "SAM",       "Approved",  "Approved",
         "Pending SAM Approval", "Pending Controller Approval", "Urgent — reviewed and approved."),
        ("TXN-2026-103", "Robert Chen",     "Controller","Approved",  "Approved",
         "Pending Controller Approval", "Pending VP Approval", "Supporting documentation reviewed."),
        # TXN-104
        ("TXN-2026-104", "Patricia Batson", "Submitter", "Submitted", "Submitted",
         None, "Pending SAM Approval", None),
        ("TXN-2026-104", "Sarah Mitchell",  "SAM",       "Approved",  "Approved",
         "Pending SAM Approval", "Pending Controller Approval", None),
        ("TXN-2026-104", "Robert Chen",     "Controller","Approved",  "Approved",
         "Pending Controller Approval", "Pending VP Approval",
         "Reviewed supporting docs, acquisition price confirmed by independent appraisal."),
        ("TXN-2026-104", "Jennifer Walsh",  "VP",        "Approved",  "Approved",
         "Pending VP Approval", "Pending CFO Approval", "Executive review complete."),
    ]
    for req_id, actor, role, etype, decision, from_s, to_s, comment in events:
        ek = ins(cur, f"""
            INSERT INTO [{S}].[WorkflowEvent] (
                Transaction_Key, Actor_User_Key, Actor_Role,
                Event_Type, Decision, From_Status, To_Status,
                Event_DateTime, Comments_Reason
            )
            OUTPUT INSERTED.WorkflowEvent_Key
            VALUES (?,?,?,?,?,?,?,?,?)
        """, [
            txn_keys[req_id],
            users.get(actor), role,
            etype, decision, from_s, to_s,
            NOW, comment,
        ])
        print(f"  {req_id} [{role}] {etype}: WorkflowEvent_Key={ek}")

    # ── TransactionComment ────────────────────────────────────────────────────
    print("Inserting TransactionComment rows...")
    comments = [
        ("TXN-2026-103", "Patricia Batson", "General",
         "Urgent — bridge loan matures 08/28, late-payment penalty applies at EOD."),
        ("TXN-2026-104", "Robert Chen", "Approval Note",
         "Reviewed supporting docs; acquisition price confirmed by independent appraisal."),
    ]
    for req_id, author, ctype, text in comments:
        ck = ins(cur, f"""
            INSERT INTO [{S}].[TransactionComment] (
                Transaction_Key, Author_User_Key, Comment_Type,
                Comment_Text, Created_DateTime
            )
            OUTPUT INSERTED.Comment_Key
            VALUES (?,?,?,?,?)
        """, [txn_keys[req_id], users[author], ctype, text, NOW])
        print(f"  {req_id}: Comment_Key={ck}")

    conn.commit()
    print()
    print("Seed complete — all rows committed.")
    print()
    print("Tables populated:")
    print("  AppUser, BankAccount, Beneficiary, BeneficiaryBankInstruction,")
    print("  ETransaction, TransactionVerification,")
    print("  WorkflowAssignment, WorkflowEvent, TransactionComment")

except Exception as exc:
    conn.rollback()
    print(f"\nERROR — all changes rolled back.\n{exc}")
    raise

finally:
    conn.close()
