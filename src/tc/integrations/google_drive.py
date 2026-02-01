"""Google Drive integration — folder creation, file management, private review folders."""

from __future__ import annotations

from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from tc.config import get_settings

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.compose",
]

# Subfolder names matching the workflow spec
TRANSACTION_SUBFOLDERS = [
    "01_Listing",
    "02_Contract",
    "03_Disclosures",
    "04_Inspections",
    "05_Appraisal",
    "06_Loan",
    "07_Title_Escrow",
    "08_Contingency_Removals",
    "09_Closing",
    "10_Compliance",
    "Notes",
]

REVIEW_SUBFOLDERS = ["Pending", "Completed", "Flagged"]


def get_credentials() -> Credentials:
    """Get or refresh Google OAuth credentials."""
    settings = get_settings()
    creds = None
    token_path = Path(settings.google_token_file)

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                settings.google_credentials_file, SCOPES
            )
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())

    return creds


def get_drive_service():
    """Get an authenticated Google Drive API service."""
    return build("drive", "v3", credentials=get_credentials())


def create_folder(service, name: str, parent_id: str | None = None) -> str:
    """Create a folder in Google Drive. Returns the folder ID."""
    metadata: dict = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
    }
    if parent_id:
        metadata["parents"] = [parent_id]
    folder = service.files().create(body=metadata, fields="id").execute()
    return folder["id"]


def create_transaction_folders(address: str, year: str) -> dict[str, str]:
    """Create the full transaction folder structure in Google Drive.

    Returns a dict mapping folder names to their Drive IDs.
    """
    service = get_drive_service()
    folder_ids: dict[str, str] = {}

    # Create or find Transactions/{year} parent
    txn_root = create_folder(service, f"Transactions")
    year_folder = create_folder(service, year, txn_root)
    address_folder = create_folder(service, address, year_folder)
    folder_ids["root"] = address_folder

    # Create subfolders
    for sub in TRANSACTION_SUBFOLDERS:
        fid = create_folder(service, sub, address_folder)
        folder_ids[sub] = fid

    return folder_ids


def create_private_review_folders(address: str, year: str) -> dict[str, str]:
    """Create the agent-private review folder structure.

    These folders are NEVER shared with anyone. Agent eyes only.
    """
    service = get_drive_service()
    folder_ids: dict[str, str] = {}

    private_root = create_folder(service, "Agent Private")
    reviews = create_folder(service, "Reviews", private_root)
    year_folder = create_folder(service, year, reviews)
    address_folder = create_folder(service, address, year_folder)
    folder_ids["root"] = address_folder

    for sub in REVIEW_SUBFOLDERS:
        fid = create_folder(service, sub, address_folder)
        folder_ids[sub] = fid

    return folder_ids


def upload_file(file_path: str | Path, folder_id: str,
                mime_type: str = "application/pdf") -> str:
    """Upload a file to a Google Drive folder. Returns the file ID."""
    service = get_drive_service()
    file_path = Path(file_path)
    metadata = {
        "name": file_path.name,
        "parents": [folder_id],
    }
    media = MediaFileUpload(str(file_path), mimetype=mime_type)
    result = service.files().create(
        body=metadata, media_body=media, fields="id"
    ).execute()
    return result["id"]


def upload_review_copy(file_path: str | Path, review_folder_id: str) -> str:
    """Upload a review copy to the agent's private Pending folder."""
    return upload_file(file_path, review_folder_id, "application/pdf")


def move_review_to_completed(file_id: str, pending_folder_id: str,
                             completed_folder_id: str) -> None:
    """Move a review copy from Pending to Completed after agent sign-off."""
    service = get_drive_service()
    service.files().update(
        fileId=file_id,
        addParents=completed_folder_id,
        removeParents=pending_folder_id,
        fields="id, parents",
    ).execute()


def get_file_link(file_id: str) -> str:
    """Get a direct link to a Google Drive file."""
    return f"https://drive.google.com/file/d/{file_id}/view"
