"""Re-generate Testing Contracts with color-coded rounded rectangles.

Scans every PDF in 'Randomly Filled Test Docs', detects filled/empty,
draws rectangles around each entry space, saves to 'Testing Contracts'.

Also scans original blank CAR contracts for the base "Testing Contracts".

Colors:
  Green  (solid)   — filled field (has content)
  Red    (solid)   — unfilled mandatory (signature, date, license, dollar)
  Yellow (dashed)  — unfilled optional (phone, email, percentage, name)
  Orange (dashed)  — day-count / time-length entries
"""
import json
import re
import sys
from pathlib import Path

import fitz
import yaml

ROOT = Path(__file__).parent
CAR_DIR = ROOT / "CAR Contract Packages"
FILLED_DIR = ROOT / "Randomly Filled Test Docs"
MANIFEST_DIR = ROOT / "doc_manifests"
OUTPUT_DIR = ROOT / "Testing Contracts"

MANDATORY_CATS = {
    "entry_signature", "entry_license", "entry_dollar",
    # Legacy
    "signature_area", "signature", "date_area", "date",
}
OPTIONAL_CATS = {
    "entry_contact", "entry_percent", "entry_name", "entry_address",
    "entry_date",  # dates are optional unless context says otherwise
    # Legacy
    "fillable_blanks", "fillable", "address", "address_area",
}
TIME_CATS = {"entry_days"}
# General mandatory keywords (for entry_blank context matching)
MANDATORY_KW = re.compile(
    r"(purchase\s*price|price|deposit|earnest|escrow\s*date|close\s*of\s*escrow|"
    r"apn|parcel|legal\s*desc|loan\s*amount|down\s*payment|financing|"
    r"offer\s*expir|acceptance|broker.*license|dre|agency\s*confirm|"
    r"arbitration|mediation|lead.*paint|wire\s*fraud|"
    # Offer section — buyer name, property address, brokerage
    r"offer\s*from|\bfrom\b.*buyer|property.*acquired|situated\s*in|"
    r"\bcity\b|\bcounty\b|zip\s*code|assessor|"
    r"brokerage\s*firm|buyer.s\s*agent|seller.s\s*agent|"
    r"\bagent\b|\bbroker\b|\bfirm\b|\bby\b|"
    # Signers — at least one required
    r"\btenant\b|\bbuyer\b|\bseller\b|\blandlord\b|"
    r"\binitial|\bsignature)",
    re.IGNORECASE,
)
# Date-specific mandatory keywords — only these make a date entry mandatory
# Most dates (next to signatures, referencing contract text) are optional
MANDATORY_DATE_KW = re.compile(
    r"(close\s*of\s*escrow|escrow\s*date|offer\s*expir|acceptance\s*date|"
    r"contingency\s*removal|possession\s*date)",
    re.IGNORECASE,
)


def detect_filled(page, bbox, label_text="", category="", ul_bbox=None):
    """Detect if an entry space has been filled in.

    For wide entries (>150pt), checks ONLY the underline area to avoid
    picking up static contract text from the label side.
    For dollar fields, any digit means filled (even "$0").
    """
    x0, y0, x1, y1 = bbox.get("x0", 0), bbox.get("y0", 0), bbox.get("x1", 0), bbox.get("y1", 0)
    if x0 >= x1 or y0 >= y1:
        return False
    w = x1 - x0

    # For wide entries, use only the underline area for fill detection
    # to avoid capturing static contract text from the label side
    if w > 150 and ul_bbox:
        ux0 = ul_bbox.get("x0", x0)
        uy0 = ul_bbox.get("y0", y0)
        ux1 = ul_bbox.get("x1", x1)
        uy1 = ul_bbox.get("y1", y1)
        rect = fitz.Rect(ux0 - 1, uy0 - 1, ux1 + 1, uy1 + 1)
    else:
        rect = fitz.Rect(x0 - 1, y0 - 1, x1 + 1, y1 + 1)

    text = page.get_text("text", clip=rect).strip()
    if not text:
        return False
    # For dollar fields, any digit in the bbox means it's filled
    if category == "entry_dollar":
        digits = re.sub(r"[^0-9]", "", text)
        return len(digits) >= 1
    # Strip known label words — whatever remains is filled content
    remaining = text
    for word in label_text.split():
        if len(word) >= 2:
            remaining = remaining.replace(word, "", 1)
    # Also strip single-char labels like "$", "%"
    if len(label_text.strip()) == 1:
        remaining = remaining.replace(label_text.strip(), "", 1)
    remaining = re.sub(r"[\s()\[\]:;,./|#\-]+", "", remaining)
    return len(remaining) >= 2


def annotate_pdf(pdf_path, manifest, output_path, detect=True):
    doc = fitz.open(str(pdf_path))
    field_map = manifest.get("field_map", [])
    drawn = 0
    stats = {"filled": 0, "unfilled_mandatory": 0, "unfilled_optional": 0, "unfilled_time": 0}
    PAD = 2  # padding around entry bbox
    # Track first occurrence of each field per page — second occurrence
    # is optional (e.g., only one signer/initialer is required)
    seen_first = {}  # (page, field) → first y0

    for entry in field_map:
        page_num = entry.get("page", 1) - 1
        if page_num < 0 or page_num >= len(doc):
            continue
        bbox = entry.get("bbox", {})
        if not bbox or "x0" not in bbox:
            continue

        page = doc[page_num]
        x0, y0, x1, y1 = bbox["x0"], bbox["y0"], bbox["x1"], bbox["y1"]
        w = abs(x1 - x0)
        h = abs(y1 - y0)
        if w < 3 or h < 2:
            continue

        cat = (entry.get("category", "") or "").lower()
        field_text = entry.get("field", "")
        context_text = entry.get("context", field_text)

        # Determine if mandatory/optional/time
        is_time = cat in TIME_CATS
        mandatory = cat in MANDATORY_CATS or (
            cat == "entry_blank" and MANDATORY_KW.search(context_text)
        ) or (
            cat == "entry_date" and MANDATORY_DATE_KW.search(context_text)
        )

        # First occurrence of a field per page = mandatory;
        # repeats are optional (usually only one signer needed)
        if mandatory:
            track = (page_num, field_text)
            first_y = seen_first.get(track)
            if first_y is not None and abs(y0 - first_y) > 5:
                mandatory = False  # second row → optional
            else:
                seen_first[track] = y0

        if detect:
            ul_bbox = entry.get("ul_bbox")
            is_filled = detect_filled(page, bbox, field_text, category=cat,
                                      ul_bbox=ul_bbox)
        else:
            is_filled = False

        if is_filled:
            color = (0.2, 0.78, 0.35)  # green
            stroke_width = 1.5
            dashes = None
            stats["filled"] += 1
        elif is_time:
            color = (1.0, 0.55, 0.0)  # orange
            stroke_width = 1.5
            dashes = "[3 2]"
            stats["unfilled_time"] += 1
        elif mandatory:
            color = (1.0, 0.23, 0.19)  # red
            stroke_width = 2.0
            dashes = None
            stats["unfilled_mandatory"] += 1
        else:
            color = (1.0, 0.8, 0.0)  # yellow
            stroke_width = 1.5
            dashes = "[4 3]"
            stats["unfilled_optional"] += 1

        # Draw rectangle with padding + light fill so shape is clearly rectangular
        rect = fitz.Rect(x0 - PAD, y0 - PAD, x1 + PAD, y1 + PAD)
        shape = page.new_shape()
        shape.draw_rect(rect)
        shape.finish(color=color, width=stroke_width, fill=color,
                     fill_opacity=0.08, dashes=dashes,
                     lineJoin=0, lineCap=0)
        shape.commit()
        drawn += 1

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))
    doc.close()
    return drawn, stats


def process_folder(src_dir, out_base, scenario_label, detect):
    total_pdfs = 0
    total_drawn = 0
    grand_stats = {"filled": 0, "unfilled_mandatory": 0, "unfilled_optional": 0, "unfilled_time": 0}

    for folder in sorted(d for d in src_dir.iterdir() if d.is_dir()):
        folder_name = folder.name
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

            output_path = out_base / folder_name / pdf_file.name
            drawn, stats = annotate_pdf(pdf_file, manifest, output_path, detect=detect)
            total_pdfs += 1
            total_drawn += drawn
            for k in grand_stats:
                grand_stats[k] += stats.get(k, 0)
            g = stats["filled"]
            r = stats["unfilled_mandatory"]
            y = stats["unfilled_optional"]
            o = stats["unfilled_time"]
            print(f"    OK  {pdf_file.name} — {drawn} rects ({g}G {r}R {y}Y {o}O)")

    return total_pdfs, total_drawn, grand_stats


def main():
    import shutil
    import subprocess
    # Clean output
    if OUTPUT_DIR.exists():
        subprocess.run(["find", str(OUTPUT_DIR), "-name", ".DS_Store", "-delete"],
                       capture_output=True)
        shutil.rmtree(str(OUTPUT_DIR), ignore_errors=True)
    OUTPUT_DIR.mkdir(exist_ok=True)

    grand_pdfs = 0
    grand_drawn = 0

    # 1. Original blank CAR contracts (detect pre-filled fields like DRE lic #)
    print(f"\n{'='*60}")
    print(f"  Blank Originals (detect pre-filled)")
    print(f"{'='*60}")
    out = OUTPUT_DIR / "0-Blank-Originals"
    n, d, s = process_folder(CAR_DIR, out, "blank", detect=True)
    grand_pdfs += n
    grand_drawn += d
    print(f"\n  Subtotal: {n} PDFs, {d} rects, {s['unfilled_mandatory']}R {s['unfilled_optional']}Y {s['unfilled_time']}O")

    # 2. Each randomly filled scenario
    if FILLED_DIR.exists():
        for scenario_dir in sorted(d for d in FILLED_DIR.iterdir() if d.is_dir()):
            print(f"\n{'='*60}")
            print(f"  {scenario_dir.name}")
            print(f"{'='*60}")
            out = OUTPUT_DIR / scenario_dir.name
            n, d, s = process_folder(scenario_dir, out, scenario_dir.name, detect=True)
            grand_pdfs += n
            grand_drawn += d
            print(f"\n  Subtotal: {n} PDFs, {d} rects, {s['filled']}G {s['unfilled_mandatory']}R {s['unfilled_optional']}Y {s['unfilled_time']}O")

    print(f"\n{'='*60}")
    print(f"  TOTAL: {grand_pdfs} PDFs, {grand_drawn} rectangles")
    print(f"  Output: {OUTPUT_DIR}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
