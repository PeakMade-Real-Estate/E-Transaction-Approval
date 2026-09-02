"""
SharePoint connection module — E Transaction Library via Microsoft Graph (app-only auth).

Uses the existing Entra ID app registration credentials (AZURE_CLIENT_ID, AZURE_TENANT_ID,
AZURE_CLIENT_SECRET) already configured in .env for client-credentials (app-only) Graph access.
Requires the app registration to be granted an application permission such as Sites.Selected
(scoped to this library) or Sites.ReadWrite.All, with admin consent.

Library metadata columns (created in SharePoint, per project decision):
    TransactionRequestID   Single line of text  — join key back to Fabric ETransaction.Request_ID
    TransactionKey         Number               — join key back to Fabric ETransaction.Transaction_Key
    DocumentSection        Choice               — B - Verification / D - Receiving Banking / E - Transaction / Additional
    DocumentType           Choice               — Validation Evidence / AVS Screenshot / Wire/ACH Instructions /
                                                    Payment Support / Approval Evidence / Release Confirmation / Other
    UploadedByRole         Choice               — Submitter / SAM / Controller / VP / CFO / Treasury / System
    DocumentStatus         Choice               — Active / Superseded / Rejected
    IsRequiredDocument     Yes/No
    SourceSystem           Choice               — E-Transaction App / SharePoint / Workflow / Manual Upload
    OriginalFileName       Single line of text
    AttachmentCorrelationID Single line of text — unique ID generated per uploaded file
    Description            Multiple lines of text (built-in)

Folder convention: one folder per request, named after the request ID, e.g. "TXN-2026-0104/".
Microsoft Graph auto-creates missing parent folders on upload by path, so no explicit
folder-creation call is required before uploading.

[STORAGE] TODO: Consider Sites.Selected + item-level permission scoping once the app registration's
                Graph permissions are finalized for production.
"""

import os
import uuid

import msal
import requests

GRAPH_BASE  = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]

# ─────────────────────────────────────────────────────────────
#  Library metadata field internal names
# ─────────────────────────────────────────────────────────────

FIELD_REQUEST_ID      = "TransactionRequestID"
FIELD_TRANSACTION_KEY = "TransactionKey"
FIELD_SECTION         = "DocumentSection"
FIELD_DOC_TYPE        = "DocumentType"
FIELD_UPLOADED_ROLE   = "UploadedByRole"
FIELD_STATUS          = "DocumentStatus"
FIELD_IS_REQUIRED     = "IsRequiredDocument"
FIELD_SOURCE_SYSTEM   = "SourceSystem"
FIELD_ORIGINAL_NAME   = "OriginalFileName"
FIELD_CORRELATION_ID  = "AttachmentCorrelationID"
FIELD_DESCRIPTION     = "Description"

# ─────────────────────────────────────────────────────────────
#  Choice column values
# ─────────────────────────────────────────────────────────────

SECTION_VERIFICATION      = "B - Verification"
SECTION_RECEIVING_BANKING = "D - Receiving Banking"
SECTION_TRANSACTION       = "E - Transaction"
SECTION_ADDITIONAL        = "Additional"

DOC_TYPE_VALIDATION_EVIDENCE   = "Validation Evidence"
DOC_TYPE_AVS_SCREENSHOT        = "AVS Screenshot"
DOC_TYPE_WIRE_ACH_INSTRUCTIONS = "Wire/ACH Instructions"
DOC_TYPE_PAYMENT_SUPPORT       = "Payment Support"
DOC_TYPE_APPROVAL_EVIDENCE     = "Approval Evidence"
DOC_TYPE_RELEASE_CONFIRMATION  = "Release Confirmation"
DOC_TYPE_OTHER                 = "Other"

# Maps a DocumentType back to the request_detail.html `attachments` dict key it already renders.
DOC_TYPE_TO_ATTACHMENT_KEY = {
    DOC_TYPE_VALIDATION_EVIDENCE:   "validation_evidence",
    DOC_TYPE_WIRE_ACH_INSTRUCTIONS: "wire_ach_instructions",
    DOC_TYPE_PAYMENT_SUPPORT:       "payment_support",
}

_SIMPLE_UPLOAD_LIMIT = 4 * 1024 * 1024   # Graph simple PUT-by-path cap
_UPLOAD_CHUNK_SIZE    = 5 * 1024 * 1024
_ILLEGAL_FILENAME_CHARS = '"*:<>?/\\|'

_msal_app       = None
_site_id_cache  = None
_drive_id_cache = None


def _config():
    """Return (hostname, site_path, library_id) from .env, or raise if incomplete."""
    hostname   = os.environ.get("SHAREPOINT_SITE_HOSTNAME", "")
    site_path  = os.environ.get("SHAREPOINT_SITE_PATH", "")
    library_id = os.environ.get("SHAREPOINT_LIBRARY_ID", "")
    if not hostname or not site_path or not library_id:
        raise RuntimeError(
            "SHAREPOINT_SITE_HOSTNAME, SHAREPOINT_SITE_PATH, and SHAREPOINT_LIBRARY_ID "
            "must be set in .env before connecting to SharePoint."
        )
    return hostname, site_path, library_id


def _get_access_token() -> str:
    """Acquire an app-only Microsoft Graph token using the existing Entra ID app registration."""
    global _msal_app

    client_id     = os.environ.get("AZURE_CLIENT_ID", "")
    client_secret = os.environ.get("AZURE_CLIENT_SECRET", "")
    tenant_id     = os.environ.get("AZURE_TENANT_ID", "")
    if not client_id or not client_secret or not tenant_id:
        raise RuntimeError(
            "AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, and AZURE_TENANT_ID must be set in .env "
            "before connecting to SharePoint."
        )

    if _msal_app is None:
        _msal_app = msal.ConfidentialClientApplication(
            client_id=client_id,
            client_credential=client_secret,
            authority=f"https://login.microsoftonline.com/{tenant_id}",
        )

    result = _msal_app.acquire_token_for_client(scopes=GRAPH_SCOPE)
    if "access_token" not in result:
        raise RuntimeError(
            f"Unable to acquire a Microsoft Graph token: "
            f"{result.get('error')}: {result.get('error_description')}"
        )
    return result["access_token"]


def _graph_request(method: str, url: str, **kwargs) -> requests.Response:
    """Issue an authenticated Microsoft Graph request and raise on non-2xx responses."""
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {_get_access_token()}"
    timeout = kwargs.pop("timeout", 30)
    resp = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
    resp.raise_for_status()
    return resp


def get_site_id() -> str:
    """Return (and cache) the Graph site ID for the configured SharePoint site."""
    global _site_id_cache
    if _site_id_cache:
        return _site_id_cache
    hostname, site_path, _ = _config()
    resp = _graph_request("GET", f"{GRAPH_BASE}/sites/{hostname}:{site_path}")
    _site_id_cache = resp.json()["id"]
    return _site_id_cache


def get_drive_id() -> str:
    """Return (and cache) the Graph drive ID backing the configured library (list) ID."""
    global _drive_id_cache
    if _drive_id_cache:
        return _drive_id_cache
    _, _, library_id = _config()
    site_id = get_site_id()
    resp = _graph_request("GET", f"{GRAPH_BASE}/sites/{site_id}/lists/{library_id}/drive")
    _drive_id_cache = resp.json()["id"]
    return _drive_id_cache


def _sanitize_filename(filename: str) -> str:
    name = (filename or "").strip().strip(".")
    for ch in _ILLEGAL_FILENAME_CHARS:
        name = name.replace(ch, "-")
    return name or "file"


def _upload_bytes(request_id: str, filename: str, content: bytes) -> dict:
    """Upload file bytes into the request's library folder, creating the folder if needed."""
    drive_id  = get_drive_id()
    item_path = f"{request_id}/{_sanitize_filename(filename)}"

    if len(content) <= _SIMPLE_UPLOAD_LIMIT:
        resp = _graph_request(
            "PUT",
            f"{GRAPH_BASE}/drives/{drive_id}/root:/{item_path}:/content",
            headers={"Content-Type": "application/octet-stream"},
            data=content,
        )
        return resp.json()

    # Large files use a resumable upload session; Graph creates missing parent folders here too.
    session_resp = _graph_request(
        "POST",
        f"{GRAPH_BASE}/drives/{drive_id}/root:/{item_path}:/createUploadSession",
        headers={"Content-Type": "application/json"},
        json={"item": {"@microsoft.graph.conflictBehavior": "replace"}},
    )
    upload_url = session_resp.json()["uploadUrl"]
    total      = len(content)
    last_json  = None
    for start in range(0, total, _UPLOAD_CHUNK_SIZE):
        end   = min(start + _UPLOAD_CHUNK_SIZE, total)
        chunk = content[start:end]
        chunk_resp = requests.put(
            upload_url,
            headers={
                "Content-Length": str(len(chunk)),
                "Content-Range":  f"bytes {start}-{end - 1}/{total}",
            },
            data=chunk,
            timeout=120,
        )
        chunk_resp.raise_for_status()
        last_json = chunk_resp.json()
    return last_json


def _set_item_fields(drive_id: str, item_id: str, fields: dict) -> None:
    _graph_request(
        "PATCH",
        f"{GRAPH_BASE}/drives/{drive_id}/items/{item_id}/listItem/fields",
        headers={"Content-Type": "application/json"},
        json=fields,
    )


def upload_attachment(request_id, file_storage, *, section, doc_type, uploaded_by_role,
                       transaction_key=None, description="", is_required=False,
                       source_system="E-Transaction App"):
    """
    Upload a Werkzeug FileStorage into the request's library folder and tag it with the
    E Transaction Library metadata columns. Returns None if no file was provided.
    """
    if file_storage is None or not file_storage.filename:
        return None

    content = file_storage.read()
    if not content:
        return None

    item = _upload_bytes(request_id, file_storage.filename, content)

    correlation_id = str(uuid.uuid4())
    fields = {
        FIELD_REQUEST_ID:     request_id,
        FIELD_SECTION:        section,
        FIELD_DOC_TYPE:       doc_type,
        FIELD_UPLOADED_ROLE:  uploaded_by_role,
        FIELD_STATUS:         "Active",
        FIELD_IS_REQUIRED:    bool(is_required),
        FIELD_SOURCE_SYSTEM:  source_system,
        FIELD_ORIGINAL_NAME:  file_storage.filename,
        FIELD_CORRELATION_ID: correlation_id,
        FIELD_DESCRIPTION:    description,
    }
    if transaction_key:
        fields[FIELD_TRANSACTION_KEY] = int(transaction_key)

    _set_item_fields(get_drive_id(), item["id"], fields)

    return {
        "filename":       item.get("name", file_storage.filename),
        "web_url":        item.get("webUrl", ""),
        "section":        section,
        "doc_type":       doc_type,
        "correlation_id": correlation_id,
    }


def list_attachments(request_id):
    """Return metadata for every file in the request's library folder, or [] if none exist."""
    drive_id = get_drive_id()
    try:
        resp = _graph_request(
            "GET",
            f"{GRAPH_BASE}/drives/{drive_id}/root:/{request_id}:/children",
            params={"$expand": "listItem($expand=fields)"},
        )
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return []
        raise

    results = []
    for item in resp.json().get("value", []):
        if "folder" in item:
            continue
        fields = item.get("listItem", {}).get("fields", {})
        results.append({
            "filename":         item.get("name", ""),
            "web_url":          item.get("webUrl", ""),
            "section":          fields.get(FIELD_SECTION, ""),
            "doc_type":         fields.get(FIELD_DOC_TYPE, ""),
            "uploaded_by_role": fields.get(FIELD_UPLOADED_ROLE, ""),
            "document_status":  fields.get(FIELD_STATUS, ""),
            "is_required":      bool(fields.get(FIELD_IS_REQUIRED, False)),
            "description":      fields.get(FIELD_DESCRIPTION, ""),
            "correlation_id":   fields.get(FIELD_CORRELATION_ID, ""),
            "transaction_key":  fields.get(FIELD_TRANSACTION_KEY),
            "uploaded_date":    item.get("createdDateTime", ""),
        })
    return results
