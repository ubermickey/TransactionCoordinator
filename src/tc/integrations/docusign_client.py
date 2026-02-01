"""DocuSign integration — envelope management and signature validation."""

from __future__ import annotations

from tc.config import get_settings
from tc.engine.validation import DocumentValidationReport, validate_envelope_from_api


def _get_api_client():
    """Get an authenticated DocuSign API client."""
    from docusign_esign import ApiClient

    settings = get_settings()
    client = ApiClient()
    client.host = settings.docusign_base_url
    # In production, use JWT or authorization code grant for auth.
    # This is a placeholder for the OAuth token flow.
    return client


def get_envelope_status(envelope_id: str) -> dict:
    """Get full envelope data including recipients and tabs."""
    from docusign_esign import EnvelopesApi

    settings = get_settings()
    client = _get_api_client()
    envelopes_api = EnvelopesApi(client)

    # Get envelope info
    envelope = envelopes_api.get_envelope(
        account_id=settings.docusign_account_id,
        envelope_id=envelope_id,
    )

    # Get recipients
    recipients = envelopes_api.list_recipients(
        account_id=settings.docusign_account_id,
        envelope_id=envelope_id,
    )

    # Get tabs for each signer
    signers_data = []
    for signer in (recipients.signers or []):
        tabs = envelopes_api.list_tabs(
            account_id=settings.docusign_account_id,
            envelope_id=envelope_id,
            recipient_id=signer.recipient_id,
        )
        signer_dict = {
            "name": signer.name,
            "email": signer.email,
            "status": signer.status,
            "recipientId": signer.recipient_id,
            "tabs": {
                "signHereTabs": [t.to_dict() for t in (tabs.sign_here_tabs or [])],
                "initialHereTabs": [t.to_dict() for t in (tabs.initial_here_tabs or [])],
                "dateSignedTabs": [t.to_dict() for t in (tabs.date_signed_tabs or [])],
                "textTabs": [t.to_dict() for t in (tabs.text_tabs or [])],
                "checkboxTabs": [t.to_dict() for t in (tabs.checkbox_tabs or [])],
            },
        }
        signers_data.append(signer_dict)

    return {
        "envelopeId": envelope.envelope_id,
        "status": envelope.status,
        "emailSubject": envelope.email_subject,
        "recipients": {"signers": signers_data},
    }


def validate_envelope(envelope_id: str) -> DocumentValidationReport:
    """Fetch envelope data from DocuSign and run full signature validation."""
    envelope_data = get_envelope_status(envelope_id)
    return validate_envelope_from_api(envelope_data)


def download_envelope_documents(envelope_id: str, output_dir: str) -> list[str]:
    """Download all documents from a completed envelope. Returns file paths."""
    from pathlib import Path

    from docusign_esign import EnvelopesApi

    settings = get_settings()
    client = _get_api_client()
    envelopes_api = EnvelopesApi(client)

    # List documents
    docs_list = envelopes_api.list_documents(
        account_id=settings.docusign_account_id,
        envelope_id=envelope_id,
    )

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    downloaded: list[str] = []

    for doc in (docs_list.envelope_documents or []):
        if doc.document_id == "certificate":
            continue
        content = envelopes_api.get_document(
            account_id=settings.docusign_account_id,
            envelope_id=envelope_id,
            document_id=doc.document_id,
        )
        file_path = output_path / f"{doc.name}.pdf"
        with open(file_path, "wb") as f:
            f.write(content)
        downloaded.append(str(file_path))

    return downloaded
