"""Scan PDFs, detect filled/empty fields, populate contracts DB, render crops."""
import io
import json
import re
from pathlib import Path

import fitz  # PyMuPDF
import yaml

from . import db
from .doc_versions import CAR_DIR, MANIFEST_DIR

ROOT = Path(__file__).resolve().parent.parent

# All folders that may contain scannable contract PDFs
SCAN_DIRS = {
    "car": CAR_DIR,
    "testing": ROOT / "Testing Contracts",
    "filled": ROOT / "Randomly Filled Test Docs",
}

# Mandatory categories — unfilled = red
MANDATORY_CATEGORIES = {
    "entry_signature", "entry_date", "entry_license", "entry_dollar",
    # Legacy categories (for older manifests)
    "signature_area", "signature", "date_area", "date",
}

# Optional categories — unfilled = yellow
OPTIONAL_CATEGORIES = {
    "entry_contact", "entry_percent", "entry_name", "entry_address",
    # Legacy categories
    "fillable_blanks", "fillable", "address", "address_area",
}

# Time-related categories — unfilled = orange
TIME_CATEGORIES = {"entry_days"}

# Field name keywords that elevate entry_blank to mandatory
MANDATORY_KEYWORDS = re.compile(
    r"(purchase\s*price|price|deposit|earnest|escrow\s*date|close\s*of\s*escrow|"
    r"apn|parcel|legal\s*desc|loan\s*amount|down\s*payment|financing|"
    r"offer\s*expir|acceptance|broker.*license|dre|agency\s*confirm|"
    r"arbitration|mediation|lead.*paint|wire\s*fraud)",
    re.IGNORECASE,
)


def _is_mandatory(category: str, field_name: str = "") -> bool:
    cat = (category or "").lower()
    if cat in MANDATORY_CATEGORIES:
        return True
    if cat in TIME_CATEGORIES:
        return False  # time entries get orange, not red
    # entry_blank with mandatory keywords → mandatory
    if cat == "entry_blank" and field_name and MANDATORY_KEYWORDS.search(field_name):
        return True
    if cat in OPTIONAL_CATEGORIES:
        if field_name and MANDATORY_KEYWORDS.search(field_name):
            return True
        return False
    return False


def _detect_filled(page, bbox: dict) -> bool:
    """Check if a field bbox has content (text inserted) on the page."""
    x0, y0, x1, y1 = bbox.get("x0", 0), bbox.get("y0", 0), bbox.get("x1", 0), bbox.get("y1", 0)
    if x0 >= x1 or y0 >= y1:
        return False
    # Expand rect slightly for detection
    rect = fitz.Rect(x0 - 1, y0 - 1, x1 + 1, y1 + 1)
    # Check for drawn content (annotations, drawings)
    text = page.get_text("text", clip=rect).strip()
    if text:
        # Filter out the form label text — if text matches the field name it's just the label
        return True
    # Check for drawing commands (ink from our fill script)
    drawings = page.get_drawings()
    for d in drawings:
        for item in d.get("items", []):
            if len(item) >= 2:
                # Check if any drawing point falls within our rect
                try:
                    pt = item[1]
                    if isinstance(pt, fitz.Point) and rect.contains(pt):
                        return True
                except (TypeError, IndexError):
                    pass
    return False


def _load_manifest(folder_name: str, pdf_stem: str) -> dict:
    safe_name = re.sub(r"[^\w\-.]", "_", pdf_stem) + ".yaml"
    manifest_path = MANIFEST_DIR / folder_name / safe_name
    if manifest_path.exists():
        with open(manifest_path) as f:
            return yaml.safe_load(f) or {}
    return {}


def scan_pdf(pdf_path: Path, folder_name: str, scenario: str = "") -> dict | None:
    """Scan a single PDF, detect field fill status, return contract data."""
    manifest = _load_manifest(folder_name, pdf_path.stem)
    field_map = manifest.get("field_map", [])
    if not field_map:
        return None

    doc = fitz.open(str(pdf_path))
    page_count = doc.page_count

    fields_out = []
    filled_count = 0
    unfilled_mandatory = 0
    unfilled_optional = 0

    for idx, entry in enumerate(field_map):
        page_num = entry.get("page", 1) - 1
        bbox = entry.get("bbox", {})
        category = entry.get("category", "")
        field_name = entry.get("field", "")
        mandatory = _is_mandatory(category, field_name)

        is_filled = False
        if 0 <= page_num < len(doc) and bbox and "x0" in bbox:
            is_filled = _detect_filled(doc[page_num], bbox)

        if is_filled:
            filled_count += 1
        elif mandatory:
            unfilled_mandatory += 1
        else:
            unfilled_optional += 1

        fields_out.append({
            "field_idx": idx,
            "page": entry.get("page", 1),
            "category": category,
            "field_name": field_name,
            "bbox": json.dumps(bbox),
            "mandatory": 1 if mandatory else 0,
            "is_filled": 1 if is_filled else 0,
        })

    doc.close()

    return {
        "folder": folder_name,
        "filename": pdf_path.name,
        "scenario": scenario,
        "source_path": str(pdf_path),
        "page_count": page_count,
        "total_fields": len(fields_out),
        "filled_fields": filled_count,
        "unfilled_mandatory": unfilled_mandatory,
        "unfilled_optional": unfilled_optional,
        "fields": fields_out,
    }


def scan_folder(base_dir: Path, scenario: str = "") -> list[dict]:
    """Scan all PDFs in a folder tree, return contract data list."""
    results = []
    if not base_dir.exists():
        return results

    for folder in sorted(d for d in base_dir.iterdir() if d.is_dir()):
        for pdf_file in sorted(folder.glob("*.pdf")):
            data = scan_pdf(pdf_file, folder.name, scenario)
            if data:
                results.append(data)
    return results


def populate_db(contracts: list[dict]):
    """Insert/update scanned contracts into the database."""
    with db.conn() as c:
        for ct in contracts:
            c.execute(
                "INSERT INTO contracts(folder, filename, scenario, source_path,"
                " page_count, total_fields, filled_fields, unfilled_mandatory,"
                " unfilled_optional, status)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(folder, filename, scenario)"
                " DO UPDATE SET source_path=excluded.source_path,"
                " page_count=excluded.page_count, total_fields=excluded.total_fields,"
                " filled_fields=excluded.filled_fields,"
                " unfilled_mandatory=excluded.unfilled_mandatory,"
                " unfilled_optional=excluded.unfilled_optional,"
                " scanned_at=datetime('now','localtime')",
                (ct["folder"], ct["filename"], ct["scenario"], ct["source_path"],
                 ct["page_count"], ct["total_fields"], ct["filled_fields"],
                 ct["unfilled_mandatory"], ct["unfilled_optional"], "unverified"),
            )
            cid = c.execute(
                "SELECT id FROM contracts WHERE folder=? AND filename=? AND scenario=?",
                (ct["folder"], ct["filename"], ct["scenario"]),
            ).fetchone()["id"]

            for f in ct["fields"]:
                c.execute(
                    "INSERT INTO contract_fields(contract_id, field_idx, page,"
                    " category, field_name, bbox, mandatory, is_filled, status)"
                    " VALUES(?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(contract_id, field_idx)"
                    " DO UPDATE SET is_filled=excluded.is_filled,"
                    " mandatory=excluded.mandatory, category=excluded.category",
                    (cid, f["field_idx"], f["page"], f["category"],
                     f["field_name"], f["bbox"], f["mandatory"], f["is_filled"],
                     "unverified"),
                )


def render_field_crop(source_path: str, page_num: int, bbox: dict,
                      padding: int = 20, zoom: float = 2.0) -> bytes:
    """Render a cropped region of a PDF page as PNG bytes."""
    doc = fitz.open(source_path)
    if page_num < 1 or page_num > doc.page_count:
        doc.close()
        return b""
    page = doc[page_num - 1]

    x0 = bbox.get("x0", 0) - padding
    y0 = bbox.get("y0", 0) - padding
    x1 = bbox.get("x1", 0) + padding
    y1 = bbox.get("y1", 0) + padding

    # Clamp to page bounds
    pr = page.rect
    x0 = max(x0, pr.x0)
    y0 = max(y0, pr.y0)
    x1 = min(x1, pr.x1)
    y1 = min(y1, pr.y1)

    clip = fitz.Rect(x0, y0, x1, y1)
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, clip=clip)

    # Draw a circle/ring around the field within the crop
    # The field center relative to the clip
    fbbox_x0 = bbox.get("x0", 0) - x0
    fbbox_y0 = bbox.get("y0", 0) - y0
    fbbox_x1 = bbox.get("x1", 0) - x0
    fbbox_y1 = bbox.get("y1", 0) - y0

    png_bytes = pix.tobytes("png")
    doc.close()
    return png_bytes


def generate_annotated_pdf(source_path: str, contract_fields: list, output_path: Path):
    """Create an annotated copy of a PDF with color-coded circles on every field."""
    doc = fitz.open(source_path)

    for f in contract_fields:
        page_num = f["page"] - 1
        if page_num < 0 or page_num >= len(doc):
            continue
        bbox = json.loads(f["bbox"]) if isinstance(f["bbox"], str) else f["bbox"]
        if not bbox or "x0" not in bbox:
            continue

        page = doc[page_num]
        x0, y0, x1, y1 = bbox["x0"], bbox["y0"], bbox["x1"], bbox["y1"]
        cx = (x0 + x1) / 2
        cy = (y0 + y1) / 2
        w = abs(x1 - x0)
        h = abs(y1 - y0)
        r = max(w, h) / 2
        r = max(r, 6)
        r = min(r, 25)

        is_filled = f["is_filled"]
        mandatory = f["mandatory"]
        status = f.get("status", "unverified")

        if status == "ignored":
            color = (0.55, 0.55, 0.58)  # gray
            width = 1.0
        elif is_filled:
            color = (0.2, 0.78, 0.35)  # green
            width = 2.0
        elif mandatory:
            color = (1.0, 0.23, 0.19)  # red
            width = 2.5
        else:
            color = (1.0, 0.8, 0.0)  # yellow
            width = 2.0

        shape = page.new_shape()
        shape.draw_circle(fitz.Point(cx, cy), r)
        shape.finish(color=color, width=width, fill=None)
        shape.commit()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))
    doc.close()
