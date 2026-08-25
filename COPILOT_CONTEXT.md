# E-Transaction Approval Dashboard — Copilot Context Map
**Last updated: 2026-08-03**
> This file is a navigational reference for GitHub Copilot. Read it at the start of a new session to avoid re-exploring the project from scratch.

---

## Project Purpose
Flask prototype (demo/requirements-gathering only) for a treasury e-transaction approval workflow. No real banking data is stored. All data lives in-memory via Flask `session` or in `mock_data.py`. The prototype is used to demonstrate UX and gather stakeholder requirements before building against real systems (SharePoint, Azure AD, etc.).

**Run the app:** `python app.py` → http://127.0.0.1:5000  
**Virtual env:** `.venv\Scripts\Activate.ps1`  
**Dependencies:** `requirements.txt` (Flask + Werkzeug)

---

## File Structure

```
app.py                  ← Flask app: all routes, filters, session logic
mock_data.py            ← Static mock records + helper functions
requirements.txt        ← pip dependencies

templates/
  base.html             ← Shared layout: navbar, prototype banner, Bootstrap/FA imports
  role_select.html      ← Role picker landing page (Submitter / Approver / Treasury)
  dashboard.html        ← Approver dashboard (stat cards, filter bar, request table)
  intake.html           ← New request submission form (Sections A–E + JS validation)
  request_detail.html   ← Individual request view (Sections A–E + actions sidebar)
  confirmation.html     ← Post-submission confirmation screen

static/
  css/custom.css        ← All custom styles (badges, tier cards, layout helpers)
  js/app.js             ← (Minimal — most JS is inline in templates)

Meeting Agendas/        ← Stakeholder meeting notes (not used by app)
COPILOT_CONTEXT.md      ← This file
```

---

## app.py — Route & Function Map

| Symbol | Line approx. | Purpose |
|---|---|---|
| `STATUS_BADGE_MAP` | ~40 | Dict mapping status strings → Bootstrap badge classes |
| `status_badge_class` filter | ~55 | Jinja2 filter: `{{ status \| status_badge_class }}` |
| `currency_fmt` filter | ~59 | Jinja2 filter: `{{ amount \| currency }}` → `$1,234.56` |
| `yesno` filter | ~65 | Jinja2 filter: bool → "Yes" / "No" |
| `inject_globals` | ~69 | Context processor: injects `is_prototype`, `current_year`, `current_role` |
| `require_role` | ~75 | `before_request` guard: redirects to `role_select` if `session["role"]` not set (exempts `role_select`, `switch_role`, `static`) |
| `index()` | ~83 | `GET /` → redirects to `role_select` if no role, else dashboard |
| `role_select()` | ~89 | `GET+POST /role-select` → renders role picker; POST sets `session["role"]` and redirects to dashboard |
| `switch_role()` | ~101 | `GET /switch-role` → clears `session["role"]`, redirects to role_select |
| `intake()` | ~107 | `GET /intake` → renders intake form |
| `intake_submit()` | ~92 | `POST /intake/submit` → builds record dict, stores in `session["submitted_requests"]`, redirects to confirmation |
| `confirmation()` | ~210 | `GET /confirmation/<id>` → looks up record in session or mock data |
| `dashboard()` | ~218 | `GET /dashboard` → merges mock + session data, applies URL-param filters, computes stat cards |
| `request_detail()` | ~290 | `GET /dashboard/request/<id>` → deep-copies record, masks account/routing numbers, renders detail page |
| `request_action()` | ~315 | `POST /dashboard/request/<id>/action` → processes approve/reject/more_info/treasury_reviewed/mark_released/mark_completed; only mutates session records (mock records show flash warning) |
| `get_approval_tier()` | mock_data.py ~7 | Amount thresholds: >=$1M=CFO, >=$500K=VP, >=$250K=Controller, else SAM |
| `mask_account()` | mock_data.py ~14 | Shows only last 4 digits |
| `mask_routing()` | mock_data.py ~21 | Shows only last 4 digits |

### `intake_submit()` — Record Fields Built
The full dict key list (relevant for template bindings and mock data parity):
`request_id`, `submitted_date`, `request_type`, `property_dept`, `property_code`, `prepared_by`, `prepared_date`, `approver`, `controller`, `treasury_service_date`, `instructions_previously_used` *(bool)*, `last_used_date`, `amount`, `currency`, `approval_tier`, `status`, `urgent`, `urgency_reason`, `payment_purpose`, `over_1m`, `assigned_approver`, `days_pending`, `orig_bank_name`, `orig_account_name`, `orig_account_number`, `orig_routing_number`, `orig_bank_contact`, `notes_orig`, `recv_payee_name`, `recv_bank_name`, `recv_account_name`, `recv_account_number`, `recv_routing_number`, `recv_bank_address`, `recv_contact_name`, `recv_contact_email`, `recv_contact_phone`, `notes_recv`, `verbal_confirmed`, `verbal_confirmed_with`, `verbal_contact_name`, `verbal_confirm_datetime`, `avs_score`, `external_source`, `internal_doc_not_used`, `attachments` *(dict)*, `timeline` *(list)*, `comments` *(list)*

### `request_action()` — Action Map
```python
"approve"           → "Pending Treasury Review"
"reject"            → "Rejected"
"more_info"         → "Needs More Information"
"treasury_reviewed" → "Pending Release"
"mark_released"     → "Released"
"mark_completed"    → "Completed"
```

---

## mock_data.py — MOCK_REQUESTS

7 pre-built records covering different scenarios:

| ID | Type | Amount | Status | Flags |
|---|---|---|---|---|
| TXN-2026-001 | ACH | $145K | Completed | Standard |
| TXN-2026-002 | Wire | ~$350K | Pending Controller Approval | — |
| TXN-2026-003 | Wire | ~$750K | Pending VP Approval | — |
| TXN-2026-004 | ACH | ~$1.2M | Pending CFO Approval | over_1m |
| TXN-2026-005 | Wire | ~$95K | Submitted | urgent, AVS not complete |
| TXN-2026-006 | Intra Bank Transfer | ~$200K | Pending Treasury Review | — |
| TXN-2026-007 | EFT | ~$310K | Pending Release | — |

Each record has the same field shape as the `intake_submit()` dict plus `docs_checklist` (dict of booleans: `treasury_template`, `amount_backup`, `auth_to_move`, `exec_approval`, `external_wire_ach`, `wf_avs_screenshot`, `final_pdf`, `controller_signed`, `confirmation_email`) and `avs_score_entered` / `wf_screenshot_attached` booleans not present on submitted records.

**Note:** Mock records cannot have their status permanently changed — `request_action()` only mutates `session["submitted_requests"]`. A flash message informs the user of this.

---

## Templates — Section Map

### intake.html — Form Sections
All sections use Bootstrap accordion (`accordion-item form-section-card`):

| ID | Label | Key Fields |
|---|---|---|
| `#sectionA` | A — Request Overview | `request_type`, `treasury_service_date`, `instructions_previously_used` checkbox + `last_used_date` (conditional), `urgent` radio + `urgency_reason`, `prepared_by`, `prepared_date` (auto-set), `property_dept`, `entity_id`, `approver`, `controller` |
| `#sectionB` | B — Verification Requirements | `verbal_confirmed`, `verbal_confirmed_with_known/requester`, `verbal_contact_name`, `verbal_confirm_datetime`, `avs_score`, `file_validation_evidence` (upload), `external_source`, `internal_doc_not_used` |
| `#sectionC` | C — Property / Originating Banking | `orig_bank_name`, `orig_account_name/number/routing`, `orig_bank_contact`, `notes_orig` |
| `#sectionD` | D — Receiving Bank Information | `recv_payee_name`, `recv_bank_name`, `recv_account_name/number/routing`, `recv_bank_address` (required for Wire), `recv_contact_*`, `notes_recv`, `file_wire_ach_instructions` (upload) |
| `#sectionE` | E — Transaction Details | `amount`, `currency`, `payment_purpose`, `file_payment_support` (upload) |

### intake.html — JavaScript Functions (inline `{% block scripts %}`)
| Function | Trigger | Purpose |
|---|---|---|
| `onInstructionsPreviouslyUsedChange()` | checkbox `#instructions_previously_used` change | Shows/hides `#last-used-date-row` and `#new-instructions-notice`; sets `last_used_date` required |
| `onAvsScoreChange()` | `#avs_score` input | Shows `#avs-low-score-warning` when score < 90 or blank |
| `onRequestTypeWireCheck()` | `#request_type` change | Sets `recv_bank_address` required and shows star/note when Wire |
| `validateAttachments(e)` | form submit | Pre-submit check for required file uploads + Wire address; shows `#attach-errors` banner |
| `expandAndScroll(sectionId)` | called by error links | Expands accordion section and scrolls to it |

### request_detail.html — Detail Sections (cards, not accordion)
| Section Header | Content |
|---|---|
| A — Request Overview | All overview fields including `instructions_previously_used` / `last_used_date` |
| B — Verification Requirements | Checklist items + verbal confirmation detail card + AVS Score / AVS Validation Evidence (WF screenshot via `docs_checklist.wf_avs_screenshot`) / Validation Evidence (uploaded file via `attachments.validation_evidence`) |
| C — Originating Banking Information | Masked account/routing with reveal toggle buttons (`toggleMask()`) |
| D — Receiving Bank Information | Same masking pattern + `attachments.wire_ach_instructions` |
| E — Transaction Details | Amount, tier badge, payment purpose, `attachments.payment_support` |
| Right sidebar | Approval Actions form (POST to `/dashboard/request/<id>/action`), Timeline, Comments |

### request_detail.html — Key Template Variables
- `record` — full record dict (deep copy with masked fields added as `*_masked` keys)
- `attachments` — shortcut set via `{% set attachments = record.get('attachments', {}) %}`
- `docs` — shortcut set via `{% set docs = record.get('docs_checklist', {}) %}`

### dashboard.html — Key Features
- Stat cards (Total, Pending Controller, Pending Treasury, Pending Release, Completed, Urgent, >$1M) — each links to a pre-filtered dashboard URL
- Filter bar: status, request type, property text search, approver text search, urgent checkbox, >$1M checkbox, amount min/max
- Request table with status badges, urgent badge (uses `urgent-pulse` CSS animation on dashboard; **no animation on detail page**)
- Each row links to `/dashboard/request/<id>`

---

## custom.css — Key Classes

| Class | Purpose |
|---|---|
| `.prototype-banner` | Yellow hazard-stripe top banner |
| `.bg-navy` | Navbar background `#1e3a5f` |
| `.bg-orange/purple/teal` | Extended Bootstrap color utils |
| `.badge-orange/purple/teal` | Colored badge backgrounds |
| `.urgent-pulse` | Blinking animation (used on **dashboard only** — removed from detail page) |
| `.tier-card-sam/controller/vp/cfo` | Left border colors for approval tier cards |
| `.tier-badge-sam/controller/vp/cfo` | Background colors for tier badges |
| `.cfo-warning` | Red-bordered warning box for >$1M transactions |
| `.stat-card` | Dashboard summary stat card |
| `.stat-number / .stat-label / .stat-icon` | Stat card internals |
| `.detail-section-header` | Navy left-border section header in detail cards |
| `.section-label` | Small muted uppercase label above field values |
| `.section-icon` | Circular letter icon in accordion headers |
| `.masked-value / .unmasked-value` | Show/hide pair for account number masking |
| `.checklist-complete / .checklist-missing` | Green check / red X icon colors |
| `.high-value-banner` | Red warning banner for >$1M transactions |
| `.form-section-card` | Intake accordion item styling |

---

## Integration TODOs (Marked in Code)
These comment tags mark every future integration point throughout the codebase:

| Tag | System |
|---|---|
| `[AUTH]` | Azure AD / MSAL authentication and user roles |
| `[STORAGE]` | SharePoint list or SQL table (replace session) |
| `[UPLOAD]` | SharePoint Document Library / Azure Blob Storage |
| `[ESIGN]` | DocuSign / Adobe Sign e-signature |
| `[EMAIL]` | Microsoft Graph / SendGrid notifications |
| `[WORKFLOW]` | Approval routing engine (Power Automate / custom) |
| `[AUDIT]` | Immutable audit log |
| `[BANKING]` | Wells Fargo AVS API |
| `[RELEASE]` | Treasury release confirmation workflow |
| `[RBAC]` | Permission-based access to sensitive banking details |

---

## Session State
The app stores submitted (non-mock) records in Flask `session["submitted_requests"]` as a list of dicts. This is ephemeral — cleared when the server restarts. Mock records in `MOCK_REQUESTS` are always present. Both lists are merged with `MOCK_REQUESTS + submitted` in `dashboard()` and `request_detail()`.

---

## Chat History — Changes Made (August 2026)

### August 3, 2026
**4. Role selection landing page (testing stub for future Azure AD auth)**
- Files: `app.py`, `templates/base.html`, `templates/role_select.html` (new)
- **New `before_request` guard** (`require_role`): any request without `session["role"]` is redirected to `/role-select`. Exempts `role_select`, `switch_role`, and `static` endpoints.
- **New `GET+POST /role-select` route** (`role_select()`): renders 3-card picker page; POST validates role is one of `submitter | approver | treasury`, stores in `session["role"]`, redirects to dashboard.
- **New `GET /switch-role` route** (`switch_role()`): clears `session["role"]`, redirects back to role picker.
- **`inject_globals` updated**: now injects `current_role = session.get("role")` into all templates.
- **`base.html` navbar updated**: "Signed in as / Demo User" replaced with role-aware display (icon changes per role: `file-pen` / `stamp` / `vault`). A "Switch Role" link appears beside it (hidden on the role_select page itself).
- Role-card design: 3 Bootstrap cards with hover lift effect, color-coded footers (blue=Submitter, green=Approver, purple=Treasury). Page includes a prototype disclaimer referencing `[AUTH]` / `[RBAC]` tags.
- **No access is actually restricted yet** — all three roles see all data. Real RBAC will be enforced via Azure AD. `[AUTH]` / `[RBAC]` tags mark future gating points.

---

## Chat History — Changes Made (July 2026)

### July 29, 2026
**1. Removed urgent badge blink on request detail page**
- File: `templates/request_detail.html` line ~18
- Removed `.urgent-pulse` class from the URGENT badge in the page header. The badge keeps `bg-danger` (solid red). The `.urgent-pulse` animation remains on `dashboard.html` row badges.

**2. Added AVS Validation Evidence to Section B of request detail**
- File: `templates/request_detail.html` — Section B card, AVS/evidence row
- The previously 2-column layout (AVS Score | Validation Evidence) is now 3-column:
  - **AVS Score** (`col-sm-3`) — numeric score from `record.avs_score`
  - **AVS Validation Evidence** (`col-sm-4`) — WF screenshot presence from `record.docs_checklist.wf_avs_screenshot` (boolean); shows green "Attached" or warning "Not attached"
  - **Validation Evidence** (`col-sm-5`) — uploaded alternative verification file from `record.attachments.validation_evidence`; shows filename or warning "Not attached"
- Both evidence fields use `bg-warning text-dark` (not `bg-danger`) because either one is sufficient for the approver.

### July 31, 2026
**3. "Previously Used Instructions" checkbox on intake form**
- Files: `templates/intake.html`, `app.py`
- **Before:** Section A had a plain always-visible `last_used_date` date input with hint text "Leave blank if never used before" and a hidden `#new-instructions-notice` warning div.
- **After:** Replaced with a checkbox (`#instructions_previously_used`, `name="instructions_previously_used"`, `value="yes"`):
  - **Unchecked (default):** `#last-used-date-row` is hidden, `#new-instructions-notice` ("First-time or unverified instructions — verbal confirmation required") is **visible**
  - **Checked:** date picker for "Last Date Used" appears and is marked required; notice hides; clearing the checkbox also clears the date value
- New JS function `onInstructionsPreviouslyUsedChange()` wired in `DOMContentLoaded`
- `app.py` `intake_submit()` now captures `instructions_previously_used` (bool) and `last_used_date` (string) as separate fields in the record dict

---

## Known Prototype Limitations / Decisions
- **No file storage:** File uploads record only the filename string. Actual file bytes are discarded. `[UPLOAD]` tag marks all such spots.
- **No persistence:** All submitted records vanish on server restart. Mock data is hardcoded.
- **No auth:** Any user can see all records and take any action. `[AUTH]` / `[RBAC]` tags mark gating points.
- **Demo secret key:** Hardcoded in `app.py` — must be replaced with `os.environ.get('SECRET_KEY')` before any real deployment.
- **Approval tier on mock records:** The `approval_tier` string is stored as a plain string, not recomputed from amount. New submissions recompute it via `get_approval_tier(amount)`.
- **Status changes on mock records are not persisted** — `request_action()` only mutates `session["submitted_requests"]`. The app flashes an info message when an action targets a mock record.
