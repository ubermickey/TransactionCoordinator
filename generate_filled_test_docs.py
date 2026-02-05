"""Generate randomly filled test documents for every CAR contract.

Creates multiple versions of each PDF with fields filled at different levels:
  1. Fully Executed    — 100% of fields filled (signatures, dates, text)
  2. Buyer Signed Only — buyer sigs filled, seller sigs empty, dates partial
  3. Partially Filled  — ~50% random fill
  4. Mostly Empty      — ~15% random fill (just started)
  5. Missing Dates     — signatures done, but dates left blank
  6. Missing Sigs      — dates and text filled, signatures blank

Each filled field gets realistic dummy data:
  - Signature areas   → cursive-style name or initials
  - Date areas        → random date in Jan-Feb 2026
  - Fillable blanks   → sample address / dollar amount / text
  - Time length items → circled with fill indicator
"""
import random
import re
import sys
from pathlib import Path

import fitz  # PyMuPDF
import yaml

ROOT = Path(__file__).parent
CAR_DIR = ROOT / "CAR Contract Packages"
MANIFEST_DIR = ROOT / "doc_manifests"
OUTPUT_DIR = ROOT / "Randomly Filled Test Docs"

# Dummy data pools
BUYER_NAMES = ["Sarah Chen", "Michael Torres", "Priya Patel", "James Wilson"]
SELLER_NAMES = ["Robert Kim", "Linda Vasquez", "David Okonkwo", "Karen Novak"]
INITIALS_BUYER = ["SC", "MT", "PP", "JW"]
INITIALS_SELLER = ["RK", "LV", "DO", "KN"]
DATES = [
    "01/15/2026", "01/22/2026", "01/28/2026", "02/01/2026",
    "02/05/2026", "02/10/2026", "02/14/2026", "02/18/2026",
]
ADDRESSES = [
    "1234 Sunset Blvd, Los Angeles, CA 90028",
    "567 Ocean Ave, Santa Monica, CA 90401",
    "890 Wilshire Blvd #1204, Beverly Hills, CA 90210",
]
AMOUNTS = ["$1,250,000", "$875,000", "$2,100,000", "$650,000", "$1,575,000"]
FILLER_TEXT = [
    "Per buyer request", "As discussed", "N/A", "See addendum",
    "Standard terms apply", "Buyer to confirm", "TBD",
]

# Fill scenario definitions
SCENARIOS = {
    "1-Fully-Executed": {
        "desc": "All fields filled",
        "signature_rate": 1.0,
        "date_rate": 1.0,
        "fillable_rate": 1.0,
        "buyer_only": False,
    },
    "2-Buyer-Signed-Only": {
        "desc": "Buyer sigs done, seller sigs empty, dates partial",
        "signature_rate": 0.5,  # only ~half (buyer side)
        "date_rate": 0.6,
        "fillable_rate": 0.7,
        "buyer_only": True,
    },
    "3-Partially-Filled": {
        "desc": "About 50% of fields randomly filled",
        "signature_rate": 0.5,
        "date_rate": 0.5,
        "fillable_rate": 0.5,
        "buyer_only": False,
    },
    "4-Mostly-Empty": {
        "desc": "Just started, ~15% filled",
        "signature_rate": 0.1,
        "date_rate": 0.15,
        "fillable_rate": 0.2,
        "buyer_only": False,
    },
    "5-Missing-Dates": {
        "desc": "Signatures done, dates blank",
        "signature_rate": 0.95,
        "date_rate": 0.0,
        "fillable_rate": 0.8,
        "buyer_only": False,
    },
    "6-Missing-Signatures": {
        "desc": "Dates and text filled, signatures blank",
        "signature_rate": 0.0,
        "date_rate": 0.95,
        "fillable_rate": 0.9,
        "buyer_only": False,
    },
}

# Ink color for fills (dark blue, like a pen)
INK = (0.05, 0.05, 0.55)
INK_SIG = (0.1, 0.1, 0.5)


def should_fill(rate):
    return random.random() < rate


def pick_sig_text(field_text, buyer_only, idx):
    """Pick a signature or initials string based on field context."""
    fl = field_text.lower()
    is_initial = "initial" in fl or len(field_text) <= 10
    is_seller = "seller" in fl or "owner" in fl or "landlord" in fl
    is_buyer = "buyer" in fl or "tenant" in fl or "lessee" in fl

    # If buyer_only mode, skip seller fields
    if buyer_only and is_seller:
        return None

    if is_initial:
        pool = INITIALS_SELLER if is_seller else INITIALS_BUYER
    else:
        pool = SELLER_NAMES if is_seller else BUYER_NAMES

    return pool[idx % len(pool)]


def fill_field(page, bbox, category, field_text, scenario, person_idx):
    """Attempt to fill a single field with dummy data. Returns True if filled."""
    x0, y0, x1, y1 = bbox["x0"], bbox["y0"], bbox["x1"], bbox["y1"]
    w = abs(x1 - x0)
    h = abs(y1 - y0)

    # Skip tiny or huge bboxes (likely misdetections)
    if w < 5 or h < 3 or w > 540:
        return False

    cat = (category or "").lower()

    if "signature" in cat:
        if not should_fill(scenario["signature_rate"]):
            return False
        text = pick_sig_text(field_text, scenario["buyer_only"], person_idx)
        if text is None:
            return False
        fontsize = min(h * 0.85, 11)
        fontsize = max(fontsize, 6)
        try:
            page.insert_text(
                fitz.Point(x0 + 2, y1 - 1),
                text, fontsize=fontsize, fontname="helv",
                color=INK_SIG,
            )
        except Exception:
            return False
        return True

    elif "date" in cat:
        if not should_fill(scenario["date_rate"]):
            return False
        date_str = random.choice(DATES)
        fontsize = min(h * 0.8, 9)
        fontsize = max(fontsize, 5.5)
        try:
            page.insert_text(
                fitz.Point(x0 + 1, y1 - 1),
                date_str, fontsize=fontsize, fontname="helv",
                color=INK,
            )
        except Exception:
            return False
        return True

    elif "fillable" in cat or "address" in cat:
        if not should_fill(scenario["fillable_rate"]):
            return False
        if "address" in cat:
            text = random.choice(ADDRESSES)
        elif w > 100:
            text = random.choice(FILLER_TEXT)
        elif w > 50:
            text = random.choice(AMOUNTS)
        else:
            text = str(random.randint(1, 30))
        fontsize = min(h * 0.8, 9)
        fontsize = max(fontsize, 5.5)
        # Truncate if too long for the box
        max_chars = int(w / (fontsize * 0.45))
        text = text[:max_chars]
        try:
            page.insert_text(
                fitz.Point(x0 + 1, y1 - 1),
                text, fontsize=fontsize, fontname="helv",
                color=INK,
            )
        except Exception:
            return False
        return True

    return False


def generate_filled_pdf(pdf_path, manifest, output_path, scenario):
    """Open a PDF, fill fields per scenario, save."""
    doc = fitz.open(str(pdf_path))
    field_map = manifest.get("field_map", [])
    person_idx = random.randint(0, 3)
    filled = 0
    total = 0

    for entry in field_map:
        page_num = entry.get("page", 1) - 1
        if page_num < 0 or page_num >= len(doc):
            continue
        bbox = entry.get("bbox", {})
        if not bbox or "x0" not in bbox:
            continue

        total += 1
        page = doc[page_num]
        category = entry.get("category", "")
        field_text = entry.get("field", "")

        if fill_field(page, bbox, category, field_text, scenario, person_idx):
            filled += 1

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))
    doc.close()
    return filled, total


def main():
    if not CAR_DIR.exists():
        print(f"ERROR: {CAR_DIR} not found")
        sys.exit(1)

    OUTPUT_DIR.mkdir(exist_ok=True)
    grand_total_files = 0
    grand_total_fills = 0

    random.seed(42)  # reproducible

    for scenario_name, scenario in SCENARIOS.items():
        scenario_dir = OUTPUT_DIR / scenario_name
        print(f"\n{'='*60}")
        print(f"  Scenario: {scenario_name}")
        print(f"  {scenario['desc']}")
        print(f"{'='*60}")

        for folder in sorted(d for d in CAR_DIR.iterdir() if d.is_dir()):
            folder_name = folder.name
            out_folder = scenario_dir / folder_name
            print(f"\n  --- {folder_name} ---")

            for pdf_file in sorted(folder.glob("*.pdf")):
                safe_name = re.sub(r"[^\w\-.]", "_", pdf_file.stem) + ".yaml"
                manifest_path = MANIFEST_DIR / folder_name / safe_name
                if not manifest_path.exists():
                    continue

                with open(manifest_path) as f:
                    manifest = yaml.safe_load(f) or {}

                if not manifest.get("field_map"):
                    continue

                output_path = out_folder / pdf_file.name
                filled, total = generate_filled_pdf(
                    pdf_file, manifest, output_path, scenario,
                )
                grand_total_files += 1
                grand_total_fills += filled
                pct = f"{filled}/{total}" if total else "0/0"
                print(f"    OK  {pdf_file.name} — {pct} fields filled")

    print(f"\n{'='*60}")
    print(f"  Done: {grand_total_files} PDFs generated")
    print(f"  Total fields filled: {grand_total_fills}")
    print(f"  Output: {OUTPUT_DIR}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
