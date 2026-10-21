"""
Corporate bulk-recipient listing: secure .xlsx reading and BankAccount verification.

The requester attaches a workbook listing the receiving accounts of ONE bulk payment. Every row must
resolve to exactly one authoritative, Open BankAccount (matched on account number AND routing number
inside SQL); a single unresolved row blocks the whole request. Nothing here logs, returns or renders
an account or routing number: results carry only row numbers, BankAccount keys and fixed messages.

Only .xlsx is supported (CSV is not an approved format). The reader uses the standard library
(zipfile + ElementTree) so no new dependency is needed, never evaluates formulas, and rejects
formula cells, error cells and numeric account cells (a numeric cell cannot prove leading zeros).

Required workbook layout (first sheet, first non-empty row = headers):
    "Account Number"   required, text
    "Routing Number"   required, text
Any other columns are ignored.
"""
import hashlib
import io
import os
import re
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

# Operational restrictions, NOT Treasury-approved business limits. Override with BULK_LISTING_MAX_ROWS /
# BULK_LISTING_MAX_FILE_MB; the hard ceilings protect the app (one SQL lookup per row; in-memory parsing).
DEFAULT_MAX_FILE_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_ROWS = 500
CEILING_MAX_FILE_BYTES = 10 * 1024 * 1024
CEILING_MAX_ROWS = 5000
MAX_FILE_BYTES = DEFAULT_MAX_FILE_BYTES    # defaults, kept for reference; code uses max_file_bytes()/max_rows()
MAX_ROWS = DEFAULT_MAX_ROWS
MAX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024  # zip-bomb guard across all parts
MAX_REPORTED_PROBLEMS = 25
ACCOUNT_HEADER = "account number"
ROUTING_HEADER = "routing number"


def _env_positive_int(name, default, ceiling):
    try:
        value = int((os.environ.get(name) or "").strip())
    except ValueError:
        return default
    return min(value, ceiling) if value > 0 else default


def max_rows():
    return _env_positive_int("BULK_LISTING_MAX_ROWS", DEFAULT_MAX_ROWS, CEILING_MAX_ROWS)


def max_file_bytes():
    mb = _env_positive_int("BULK_LISTING_MAX_FILE_MB", DEFAULT_MAX_FILE_BYTES // (1024 * 1024),
                           CEILING_MAX_FILE_BYTES // (1024 * 1024))
    return mb * 1024 * 1024
_VALUE_RE = re.compile(r"^[A-Za-z0-9]{3,34}$")

# Password-protected, rights-managed (sensitivity-label) and legacy .xls workbooks are OLE2 containers, not zip packages.
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ENCRYPTED_PACKAGE_MARKER = "EncryptedPackage".encode("utf-16-le")

_NS_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_NS_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_NS_PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"


class ListingError(Exception):
    """A problem with the listing file itself; the message is safe to show (no banking values)."""


@dataclass
class ListingRow:
    line: int          # worksheet row number, as the requester sees it
    account: str
    routing: str


@dataclass
class BulkValidation:
    ok: bool = False
    parse_ok: bool = False
    errors: list = field(default_factory=list)
    recipients: list = field(default_factory=list)   # [{"line": int, "bank_account_key": int}]
    sha256: str = ""
    row_count: int = 0

    @property
    def matched_count(self):
        return len(self.recipients)


def sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def check_file_basics(filename, size):
    """Cheap pre-upload checks on the file's name and size; raises ListingError."""
    if not (filename or "").lower().endswith(".xlsx"):
        raise ListingError("The recipient listing must be an Excel workbook (.xlsx). CSV and other formats are not accepted.")
    limit = max_file_bytes()
    if size > limit:
        raise ListingError(f"The recipient listing is larger than {limit // (1024 * 1024)} MB.")
    if size == 0:
        raise ListingError("The recipient listing file is empty.")


def _read_part(zf, name, budget):
    info = zf.getinfo(name)
    if info.file_size > budget[0]:
        raise ListingError("The workbook is too large when unpacked.")
    data = zf.read(name)
    budget[0] -= len(data)
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise ListingError("The workbook contains unsupported XML content.")
    return data


def _col_index(ref):
    letters = re.match(r"[A-Z]+", ref or "")
    if not letters:
        raise ListingError("The workbook contains an unreadable cell reference.")
    n = 0
    for ch in letters.group(0):
        n = n * 26 + (ord(ch) - 64)
    return n


def _shared_strings(zf, budget):
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    root = ET.fromstring(_read_part(zf, "xl/sharedStrings.xml", budget))
    out = []
    for si in root.iter(_NS_MAIN + "si"):
        out.append("".join(t.text or "" for t in si.iter(_NS_MAIN + "t")))
    return out


def _first_sheet_path(zf, budget):
    wb = ET.fromstring(_read_part(zf, "xl/workbook.xml", budget))
    sheets = wb.find(_NS_MAIN + "sheets")
    first = sheets[0] if sheets is not None and len(sheets) else None
    if first is None:
        raise ListingError("The workbook has no worksheets.")
    rid = first.get(_NS_REL + "id")
    rels = ET.fromstring(_read_part(zf, "xl/_rels/workbook.xml.rels", budget))
    for rel in rels.iter(_NS_PKG_REL + "Relationship"):
        if rel.get("Id") == rid:
            target = rel.get("Target") or ""
            return target.lstrip("/") if target.startswith("/") else "xl/" + target
    raise ListingError("The workbook's first worksheet could not be located.")


def _read_rows(content: bytes):
    """Yield (row_number, {col_index: (kind, value)}) for the first worksheet. kind: s(text) n(number) e(error) f(formula) b(bool)."""
    if content.startswith(_OLE2_MAGIC):
        if _ENCRYPTED_PACKAGE_MARKER in content:
            raise ListingError("The workbook is encrypted (password-protected or protected by a sensitivity label) and cannot be read. "
                               "Save an unprotected copy as a standard .xlsx file and upload that copy.")
        raise ListingError("The file is an older Excel (.xls) format saved with an .xlsx name. "
                           "Open it in Excel and use Save As > Excel Workbook (.xlsx).")
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        raise ListingError("The file is not a readable Excel (.xlsx) workbook.")
    if sum(i.file_size for i in zf.infolist()) > MAX_UNCOMPRESSED_BYTES:
        raise ListingError("The workbook is too large when unpacked.")
    budget = [MAX_UNCOMPRESSED_BYTES]
    try:
        strings = _shared_strings(zf, budget)
        sheet = ET.fromstring(_read_part(zf, _first_sheet_path(zf, budget), budget))
    except (KeyError, ET.ParseError):
        raise ListingError("The workbook could not be read. Save it again as a standard .xlsx file.")
    rows = []
    for row in sheet.iter(_NS_MAIN + "row"):
        number = int(row.get("r") or (rows[-1][0] + 1 if rows else 1))
        cells = {}
        for c in row.iter(_NS_MAIN + "c"):
            col = _col_index(c.get("r"))
            if c.find(_NS_MAIN + "f") is not None:
                cells[col] = ("f", "")
                continue
            t = c.get("t") or "n"
            v = c.find(_NS_MAIN + "v")
            if t == "inlineStr":
                text = "".join(x.text or "" for x in c.iter(_NS_MAIN + "t"))
                cells[col] = ("s", text)
            elif v is None or v.text is None:
                continue
            elif t == "s":
                try:
                    cells[col] = ("s", strings[int(v.text)])
                except (ValueError, IndexError):
                    raise ListingError("The workbook contains an unreadable text value.")
            elif t == "str":
                cells[col] = ("s", v.text)
            elif t == "e":
                cells[col] = ("e", "")
            elif t == "b":
                cells[col] = ("b", "")
            else:
                cells[col] = ("n", v.text)
        rows.append((number, cells))
        if len(rows) > max_rows() + 50:
            raise ListingError(f"The listing has more than {max_rows()} recipient rows.")
    return rows


def _norm_header(value):
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def parse_listing(content: bytes, filename: str = "listing.xlsx"):
    """Return (rows, problems, total_rows) — rows: list[ListingRow]; problems: safe messages. Raises ListingError if unreadable."""
    check_file_basics(filename, len(content))
    raw_rows = _read_rows(content)

    header_row = next(((n, cells) for n, cells in raw_rows if any(v[1] != "" or v[0] != "s" for v in cells.values())), None)
    if header_row is None:
        raise ListingError("The workbook is empty.")
    header_no, header_cells = header_row
    columns = {}
    for idx, (kind, value) in header_cells.items():
        if kind == "s" and _norm_header(value) in (ACCOUNT_HEADER, ROUTING_HEADER):
            name = _norm_header(value)
            if name in columns:
                raise ListingError(f"The header \"{value.strip()}\" appears more than once.")
            columns[name] = idx
    missing = [h.title() for h in (ACCOUNT_HEADER, ROUTING_HEADER) if h not in columns]
    if missing:
        raise ListingError("The first row must contain the headers \"Account Number\" and \"Routing Number\" "
                           f"(missing: {', '.join(missing)}).")

    problems, rows, total_rows = [], [], 0
    for number, cells in raw_rows:
        if number <= header_no:
            continue
        if not any(v[0] != "s" or v[1].strip() for v in cells.values()):
            continue  # entirely blank row
        total_rows += 1
        parsed = {}
        bad = False
        for label, key in (("Account Number", ACCOUNT_HEADER), ("Routing Number", ROUTING_HEADER)):
            kind, value = cells.get(columns[key], ("s", ""))
            if kind == "f":
                problems.append(f"Row {number}: {label} contains a formula; paste the value as text.")
                bad = True
            elif kind == "e":
                problems.append(f"Row {number}: {label} contains a spreadsheet error value.")
                bad = True
            elif kind in ("n", "b"):
                problems.append(f"Row {number}: {label} is not text; format the column as Text so leading zeros are kept.")
                bad = True
            else:
                value = value.strip()
                if not value:
                    problems.append(f"Row {number}: {label} is blank.")
                    bad = True
                elif not _VALUE_RE.match(value):
                    problems.append(f"Row {number}: {label} must be 3-34 letters or digits with no spaces or symbols.")
                    bad = True
                else:
                    parsed[key] = value
        if not bad:
            rows.append(ListingRow(number, parsed[ACCOUNT_HEADER], parsed[ROUTING_HEADER]))
    if len(rows) + len(problems) > max_rows():
        raise ListingError(f"The listing has more than {max_rows()} recipient rows.")
    if not rows and not problems:
        raise ListingError("The listing has no recipient rows.")

    seen = {}
    for row in rows:
        key = (row.account.upper(), row.routing.upper())
        if key in seen:
            problems.append(f"Row {row.line}: duplicate of row {seen[key]}.")
        else:
            seen[key] = row.line
    return rows, problems, total_rows


def _summarize(problems):
    shown = problems[:MAX_REPORTED_PROBLEMS]
    if len(problems) > len(shown):
        shown.append(f"...and {len(problems) - len(shown)} more problem(s).")
    return shown


def validate_bulk_listing(content: bytes, filename: str, *, originating_bank_account_key, matcher) -> BulkValidation:
    """
    Parse the workbook and verify every row against BankAccount via `matcher`, which takes
    [(account, routing), ...] and returns, per pair, a list of {"bank_account_key", "status"}.
    The whole listing must verify: ok is True only if there are no problems at all.
    """
    result = BulkValidation(sha256=sha256_hex(content))
    try:
        rows, problems, total_rows = parse_listing(content, filename)
    except ListingError as exc:
        result.errors = [str(exc)]
        return result
    result.parse_ok = True
    result.row_count = total_rows

    matches = []
    if rows:
        try:
            matches = matcher([(r.account, r.routing) for r in rows])
        except Exception:
            result.errors = problems + ["Recipient accounts could not be verified right now. Please try again."]
            result.errors = _summarize(result.errors)
            return result

    recipients, used_keys = [], {}
    for row, found in zip(rows, matches):
        if not found:
            problems.append(f"Row {row.line}: no matching BankAccount record.")
        elif len(found) > 1:
            problems.append(f"Row {row.line}: matches more than one BankAccount record (ambiguous).")
        else:
            key, status = found[0]["bank_account_key"], found[0].get("status")
            if status != "Open":
                problems.append(f"Row {row.line}: the matched BankAccount is not Open.")
            elif originating_bank_account_key not in (None, "") and int(key) == int(originating_bank_account_key):
                problems.append(f"Row {row.line}: the account is the originating account and cannot also be a recipient.")
            elif key in used_keys:
                problems.append(f"Row {row.line}: resolves to the same BankAccount as row {used_keys[key]}.")
            else:
                used_keys[key] = row.line
                recipients.append({"line": row.line, "bank_account_key": key})

    result.errors = _summarize(problems)
    result.ok = not problems and bool(recipients) and len(recipients) == len(rows)
    result.recipients = recipients if result.ok else []
    return result
