"""AI document extraction using Claude API.

Reads PDFs and extracts contract terms, dates, parties, and financial
figures with confidence scores.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import anthropic

from tc.config import get_settings

EXTRACTION_PROMPT = """\
You are a California real estate transaction coordinator AI. Extract all \
relevant terms from this document.

Return a JSON object with the following fields (use null for any field not found):

{
  "document_type": "RPA | TDS | SPQ | NHD | CR1 | Amendment | Appraisal | \
Title_Report | Inspection | Closing_Disclosure | Other",
  "confidence": 0.0 to 1.0,

  "parties": {
    "buyers": [{"name": "", "entity_type": "individual|trust|llc|corp"}],
    "sellers": [{"name": "", "entity_type": ""}],
    "buyer_agent": {"name": "", "license": "", "brokerage": ""},
    "listing_agent": {"name": "", "license": "", "brokerage": ""},
    "escrow_officer": {"name": "", "company": ""},
    "title_officer": {"name": "", "company": ""},
    "lender": {"name": "", "company": ""}
  },

  "property": {
    "address": "",
    "city": "",
    "state": "CA",
    "zip": "",
    "apn": "",
    "type": "SFR|condo|multi_unit|PUD",
    "year_built": null,
    "sqft": null,
    "bed": null,
    "bath": null,
    "has_hoa": false,
    "hoa_name": ""
  },

  "financial": {
    "purchase_price": null,
    "deposit_amount": null,
    "loan_amount": null,
    "loan_type": "",
    "interest_rate": null,
    "seller_credits": null,
    "close_of_escrow": "YYYY-MM-DD or null"
  },

  "dates": {
    "acceptance_date": "YYYY-MM-DD or null",
    "close_of_escrow": "YYYY-MM-DD or null"
  },

  "contingencies": {
    "investigation_days": null,
    "appraisal_days": null,
    "loan_days": null,
    "deposit_delivery_days": null,
    "investigation_waived": false,
    "appraisal_waived": false,
    "loan_waived": false
  },

  "flags": [
    {
      "field": "field name",
      "issue": "description of anomaly or concern",
      "severity": "red|orange|yellow",
      "confidence": 0.0 to 1.0
    }
  ],

  "raw_extracted_text_summary": "Brief summary of the document content"
}

Be precise. For confidence scores: 1.0 = certain, 0.95+ = high confidence, \
0.7-0.95 = medium (flag for review), below 0.7 = low (require manual input).

Flag anything unusual: missing fields, contradictory terms, non-standard \
language, waived contingencies, contingency periods shorter than 17 days, \
prices over $5M (Measure ULA), seller with out-of-state address (HARPTA).
"""


def extract_from_pdf(pdf_path: str | Path) -> dict[str, Any]:
    """Extract contract terms from a PDF using Claude's vision capability."""
    settings = get_settings()
    if not settings.has_anthropic():
        raise RuntimeError("ANTHROPIC_API_KEY not configured. Set it in .env")

    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"Document not found: {pdf_path}")

    pdf_bytes = pdf_path.read_bytes()
    pdf_b64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4096,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": pdf_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": EXTRACTION_PROMPT,
                    },
                ],
            }
        ],
    )

    response_text = message.content[0].text

    # Parse JSON from the response (handle markdown code blocks)
    text = response_text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        text = text.rsplit("```", 1)[0]

    return json.loads(text)


def apply_extraction_to_transaction(txn: "Transaction", extraction: dict) -> list[str]:
    """Apply extracted data to a transaction. Returns list of changes made."""
    from datetime import date as date_type
    changes: list[str] = []

    fin = extraction.get("financial", {})
    dates = extraction.get("dates", {})
    cont = extraction.get("contingencies", {})
    prop = extraction.get("property", {})

    if fin.get("purchase_price") and not txn.purchase_price:
        txn.purchase_price = fin["purchase_price"]
        changes.append(f"Purchase price: ${txn.purchase_price:,.2f}")

    if fin.get("deposit_amount") and not txn.deposit_amount:
        txn.deposit_amount = fin["deposit_amount"]
        changes.append(f"Deposit: ${txn.deposit_amount:,.2f}")

    if fin.get("loan_amount") and not txn.loan_amount:
        txn.loan_amount = fin["loan_amount"]
        changes.append(f"Loan amount: ${txn.loan_amount:,.2f}")

    if dates.get("acceptance_date") and not txn.acceptance_date:
        txn.acceptance_date = date_type.fromisoformat(dates["acceptance_date"])
        changes.append(f"Acceptance date: {txn.acceptance_date}")

    if dates.get("close_of_escrow") and not txn.close_of_escrow:
        txn.close_of_escrow = date_type.fromisoformat(dates["close_of_escrow"])
        changes.append(f"Close of escrow: {txn.close_of_escrow}")

    if cont.get("investigation_days") is not None:
        txn.investigation_days = cont["investigation_days"]
        changes.append(f"Investigation contingency: {txn.investigation_days} days")

    if cont.get("appraisal_days") is not None:
        txn.appraisal_days = cont["appraisal_days"]
        changes.append(f"Appraisal contingency: {txn.appraisal_days} days")

    if cont.get("loan_days") is not None:
        txn.loan_days = cont["loan_days"]
        changes.append(f"Loan contingency: {txn.loan_days} days")

    if cont.get("deposit_delivery_days") is not None:
        txn.deposit_delivery_days = cont["deposit_delivery_days"]
        changes.append(f"Deposit delivery: {txn.deposit_delivery_days} business days")

    if prop.get("city") and not txn.city:
        txn.city = prop["city"]
        changes.append(f"City: {txn.city}")
        # Auto-detect jurisdictions
        city_lower = txn.city.lower()
        if "beverly hills" in city_lower:
            txn.jurisdictions = ["california", "los_angeles_county", "beverly_hills"]
        elif "los angeles" in city_lower:
            txn.jurisdictions = ["california", "los_angeles"]
        else:
            txn.jurisdictions = ["california"]
        changes.append(f"Jurisdictions: {', '.join(txn.jurisdictions)}")

    if prop.get("has_hoa"):
        txn.has_hoa = True
        changes.append("HOA: Yes")

    if prop.get("year_built"):
        txn.year_built = prop["year_built"]
        changes.append(f"Year built: {txn.year_built}")

    return changes
