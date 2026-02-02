"""PDF document analyzer for CAR Contract Packages.

Scans all PDFs using PyMuPDF to extract:
- Form widgets (fillable fields) with page/bbox coordinates
- Signature areas
- Date fields with time-length options
- "123 Test St." references for removal
- Text blocks for OCR field mapping

Outputs per-document YAML manifests with version tracking.
"""
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import fitz  # PyMuPDF
import yaml

# ── Paths ────────────────────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent.parent
CAR_DIR = ROOT / "CAR Contract Packages"
MANIFEST_DIR = ROOT / "doc_manifests"

# ── Field classification patterns ────────────────────────────────────────────

SIG_PATTERNS = [
    re.compile(r"signature", re.I),
    re.compile(r"\bsign\b", re.I),
    re.compile(r"\binitial[s]?\b", re.I),
    re.compile(r"buyer.*initial", re.I),
    re.compile(r"seller.*initial", re.I),
    re.compile(r"agent.*signature", re.I),
    re.compile(r"broker.*signature", re.I),
]

DATE_PATTERNS = [
    re.compile(r"\bdate\b", re.I),
    re.compile(r"\btime\b", re.I),
    re.compile(r"\bdays?\b", re.I),
    re.compile(r"\bexpir", re.I),
    re.compile(r"\bdeadline\b", re.I),
    re.compile(r"AM\s*/\s*PM", re.I),
    re.compile(r"\d+\s*(?:calendar|business)\s*days?", re.I),
]

TIME_LENGTH_PATTERN = re.compile(
    r"(\d+)\s*(?:calendar|business|banking)?\s*days?", re.I
)

TEST_ADDRESS = re.compile(r"123\s*Test\s*St\.?", re.I)


def classify_field(name: str, label: str) -> str:
    """Classify a field as signature, date, address, or fillable."""
    combined = f"{name} {label}"
    for pat in SIG_PATTERNS:
        if pat.search(combined):
            return "signature"
    for pat in DATE_PATTERNS:
        if pat.search(combined):
            return "date"
    if re.search(r"address|property|street|city|zip|state", combined, re.I):
        return "address"
    return "fillable"


def bbox_to_dict(bbox) -> dict:
    """Convert a fitz Rect or tuple to a dict with rounded coords."""
    if hasattr(bbox, "x0"):
        return {"x0": round(bbox.x0, 1), "y0": round(bbox.y0, 1),
                "x1": round(bbox.x1, 1), "y1": round(bbox.y1, 1)}
    return {"x0": round(bbox[0], 1), "y0": round(bbox[1], 1),
            "x1": round(bbox[2], 1), "y1": round(bbox[3], 1)}


# ── Per-page analysis ────────────────────────────────────────────────────────

def analyze_page_widgets(page) -> list:
    """Extract form widgets from a page."""
    fields = []
    for widget in page.widgets():
        name = widget.field_name or ""
        label = widget.field_label or ""
        value = widget.field_value or ""
        ftype = classify_field(name, label)
        rect = widget.rect

        field = {
            "name": name,
            "label": label,
            "value": value,
            "type": ftype,
            "widget_type": widget.field_type_string,
            "bbox": bbox_to_dict(rect),
            "page_region": _page_region(rect, page.rect.height),
            "is_filled": bool(value and value.strip()),
        }

        if ftype == "date":
            m = TIME_LENGTH_PATTERN.search(f"{name} {label} {value}")
            if m:
                field["time_length_days"] = int(m.group(1))

        fields.append(field)
    return fields


def analyze_page_text(page) -> dict:
    """Extract text blocks, identify signature lines and test address refs."""
    blocks = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)["blocks"]
    sig_areas = []
    date_areas = []
    test_refs = []
    fillable_blanks = []

    for block in blocks:
        if block["type"] != 0:  # skip image blocks
            continue
        for line in block.get("lines", []):
            text = ""
            line_bbox = line["bbox"]
            for span in line.get("spans", []):
                text += span["text"]

            text_stripped = text.strip()
            if not text_stripped:
                continue

            # Test address references
            if TEST_ADDRESS.search(text_stripped):
                test_refs.append({
                    "text": text_stripped[:120],
                    "bbox": bbox_to_dict(line_bbox),
                    "page_region": _page_region(line_bbox, page.rect.height),
                })

            # Signature lines (underscores or "signature" text)
            if re.search(r"_{5,}|signature|initial", text_stripped, re.I):
                for pat in SIG_PATTERNS:
                    if pat.search(text_stripped):
                        sig_areas.append({
                            "text": text_stripped[:100],
                            "bbox": bbox_to_dict(line_bbox),
                            "page_region": _page_region(line_bbox, page.rect.height),
                        })
                        break

            # Date fields
            for pat in DATE_PATTERNS:
                if pat.search(text_stripped):
                    entry = {
                        "text": text_stripped[:100],
                        "bbox": bbox_to_dict(line_bbox),
                        "page_region": _page_region(line_bbox, page.rect.height),
                    }
                    m = TIME_LENGTH_PATTERN.search(text_stripped)
                    if m:
                        entry["time_length_days"] = int(m.group(1))
                    date_areas.append(entry)
                    break

            # Fillable blanks (lines with underscores suggesting blank fill)
            if re.search(r"_{3,}", text_stripped) and not re.search(
                r"signature|initial", text_stripped, re.I
            ):
                fillable_blanks.append({
                    "text": text_stripped[:120],
                    "bbox": bbox_to_dict(line_bbox),
                    "page_region": _page_region(line_bbox, page.rect.height),
                })

    return {
        "signature_areas": sig_areas,
        "date_areas": date_areas,
        "test_address_refs": test_refs,
        "fillable_blanks": fillable_blanks,
    }


def _page_region(bbox, page_height: float) -> str:
    """Classify vertical position on page."""
    if hasattr(bbox, "y0"):
        y_mid = (bbox.y0 + bbox.y1) / 2
    else:
        y_mid = (bbox[1] + bbox[3]) / 2
    ratio = y_mid / page_height
    if ratio < 0.15:
        return "header"
    elif ratio < 0.4:
        return "upper"
    elif ratio < 0.6:
        return "middle"
    elif ratio < 0.85:
        return "lower"
    return "footer"


# ── Document-level analysis ──────────────────────────────────────────────────

def analyze_document(pdf_path: Path) -> dict:
    """Full analysis of a single PDF."""
    doc = fitz.open(str(pdf_path))
    result = {
        "file": pdf_path.name,
        "path": str(pdf_path),
        "page_count": len(doc),
        "pages": [],
        "summary": {
            "total_widgets": 0,
            "filled_widgets": 0,
            "empty_widgets": 0,
            "signature_fields": 0,
            "date_fields": 0,
            "address_fields": 0,
            "fillable_fields": 0,
            "signature_areas_text": 0,
            "date_areas_text": 0,
            "test_address_refs": 0,
            "fillable_blanks": 0,
            "time_length_options": [],
            "incomplete_fields": [],
        },
    }

    for pno in range(len(doc)):
        page = doc[pno]
        page_data = {"page": pno + 1, "width": round(page.rect.width, 1),
                      "height": round(page.rect.height, 1)}

        # Widgets
        widgets = analyze_page_widgets(page)
        page_data["widgets"] = widgets
        for w in widgets:
            result["summary"]["total_widgets"] += 1
            if w["is_filled"]:
                result["summary"]["filled_widgets"] += 1
            else:
                result["summary"]["empty_widgets"] += 1
                result["summary"]["incomplete_fields"].append({
                    "page": pno + 1,
                    "name": w["name"],
                    "type": w["type"],
                    "bbox": w["bbox"],
                })
            result["summary"][f"{w['type']}_fields"] += 1
            if w.get("time_length_days"):
                result["summary"]["time_length_options"].append({
                    "page": pno + 1,
                    "name": w["name"],
                    "days": w["time_length_days"],
                    "bbox": w["bbox"],
                })

        # Text analysis
        text_info = analyze_page_text(page)
        page_data["signature_areas"] = text_info["signature_areas"]
        page_data["date_areas"] = text_info["date_areas"]
        page_data["test_address_refs"] = text_info["test_address_refs"]
        page_data["fillable_blanks"] = text_info["fillable_blanks"]

        result["summary"]["signature_areas_text"] += len(text_info["signature_areas"])
        result["summary"]["date_areas_text"] += len(text_info["date_areas"])
        result["summary"]["test_address_refs"] += len(text_info["test_address_refs"])
        result["summary"]["fillable_blanks"] += len(text_info["fillable_blanks"])

        for da in text_info["date_areas"]:
            if da.get("time_length_days"):
                result["summary"]["time_length_options"].append({
                    "page": pno + 1,
                    "text": da["text"][:80],
                    "days": da["time_length_days"],
                    "bbox": da["bbox"],
                })

        result["pages"].append(page_data)

    doc.close()
    return result


# ── Manifest generation ──────────────────────────────────────────────────────

def generate_manifest(analysis: dict, folder_name: str) -> dict:
    """Generate a YAML-ready manifest with version tracking."""
    s = analysis["summary"]
    manifest = {
        "version": "1.0.0",
        "last_analyzed": datetime.now().isoformat(timespec="seconds"),
        "file": analysis["file"],
        "folder": folder_name,
        "page_count": analysis["page_count"],
        "status": "complete" if s["empty_widgets"] == 0 and s["test_address_refs"] == 0
                  else "needs_attention",
        "issues": [],
        "summary": {
            "total_form_widgets": s["total_widgets"],
            "filled": s["filled_widgets"],
            "empty": s["empty_widgets"],
            "signature_widgets": s["signature_fields"],
            "date_widgets": s["date_fields"],
            "address_widgets": s["address_fields"],
            "other_fillable_widgets": s["fillable_fields"],
            "signature_areas_from_text": s["signature_areas_text"],
            "date_areas_from_text": s["date_areas_text"],
            "fillable_blanks_from_text": s["fillable_blanks"],
            "test_address_references": s["test_address_refs"],
        },
        "time_length_review": s["time_length_options"],
        "field_map": [],
    }

    if s["test_address_refs"] > 0:
        manifest["issues"].append({
            "type": "test_data",
            "detail": f'{s["test_address_refs"]} references to "123 Test St." found — must be removed',
        })

    if s["empty_widgets"] > 0:
        manifest["issues"].append({
            "type": "incomplete_fields",
            "detail": f'{s["empty_widgets"]} form widgets are unfilled',
            "fields": s["incomplete_fields"],
        })

    # Build field map for zoom-in navigation
    for page_data in analysis["pages"]:
        pno = page_data["page"]
        for w in page_data["widgets"]:
            manifest["field_map"].append({
                "page": pno,
                "field": w["name"] or w["label"] or "(unnamed)",
                "category": w["type"],
                "bbox": w["bbox"],
                "region": w["page_region"],
                "filled": w["is_filled"],
                "value": w["value"][:50] if w["value"] else None,
            })
        for sa in page_data["signature_areas"]:
            manifest["field_map"].append({
                "page": pno,
                "field": sa["text"][:60],
                "category": "signature_area",
                "bbox": sa["bbox"],
                "region": sa["page_region"],
            })
        for da in page_data["date_areas"]:
            manifest["field_map"].append({
                "page": pno,
                "field": da["text"][:60],
                "category": "date_area",
                "bbox": da["bbox"],
                "region": da["page_region"],
                "time_length_days": da.get("time_length_days"),
            })

    return manifest


# ── Cross-reference with brokerage YAML ──────────────────────────────────────

def cross_reference_brokerage() -> dict:
    """Compare PDFs in CAR packages against douglas_elliman.yaml requirements."""
    brokerage_path = ROOT / "brokerages" / "douglas_elliman.yaml"
    if not brokerage_path.exists():
        return {"error": "douglas_elliman.yaml not found"}

    with open(brokerage_path) as f:
        brokerage = yaml.safe_load(f)

    # Collect all required doc codes/names from brokerage
    required = {}
    for section_key in ["sale_listing", "sale_buyer", "lease_listing", "lease_buyer"]:
        section = brokerage.get(section_key, [])
        for doc in section:
            code = doc.get("code", "")
            name = doc.get("name", "")
            required[code] = {
                "name": name,
                "section": section_key,
                "required": doc.get("required", "always"),
                "phase": doc.get("phase", ""),
            }

    # Collect actual PDF filenames
    actual_pdfs = {}
    for folder in CAR_DIR.iterdir():
        if not folder.is_dir():
            continue
        for pdf in folder.glob("*.pdf"):
            # Normalize: strip timestamp suffix and extension
            clean = pdf.stem
            # Remove trailing _ts##### pattern
            clean = re.sub(r"_ts\d+$", "", clean)
            # Replace underscores with spaces
            clean_readable = clean.replace("_", " ")
            actual_pdfs[clean_readable.lower()] = {
                "file": pdf.name,
                "folder": folder.name,
                "path": str(pdf),
            }

    # Match
    matched = []
    unmatched_required = []
    for code, info in required.items():
        name_lower = info["name"].lower()
        found = False
        for pdf_key, pdf_info in actual_pdfs.items():
            # fuzzy: check if significant words overlap
            name_words = set(re.findall(r"\w{4,}", name_lower))
            pdf_words = set(re.findall(r"\w{4,}", pdf_key))
            overlap = name_words & pdf_words
            if len(overlap) >= 2 or name_lower in pdf_key or pdf_key in name_lower:
                matched.append({
                    "code": code,
                    "brokerage_name": info["name"],
                    "pdf_file": pdf_info["file"],
                    "folder": pdf_info["folder"],
                    "section": info["section"],
                })
                found = True
                break
        if not found:
            unmatched_required.append({
                "code": code,
                "name": info["name"],
                "section": info["section"],
                "required": info["required"],
                "phase": info["phase"],
            })

    return {
        "total_required": len(required),
        "matched": len(matched),
        "unmatched": len(unmatched_required),
        "matches": matched,
        "missing_from_packages": unmatched_required,
    }


# ── Main ─────────────────────────────────────────────────────────────────────

def run_full_analysis():
    """Analyze all PDFs and generate manifests."""
    MANIFEST_DIR.mkdir(exist_ok=True)

    all_results = []
    total_test_refs = 0
    total_time_lengths = []

    folders = sorted(d for d in CAR_DIR.iterdir() if d.is_dir())
    for folder in folders:
        pdfs = sorted(folder.glob("*.pdf"))
        print(f"\n{'='*70}")
        print(f"FOLDER: {folder.name} ({len(pdfs)} PDFs)")
        print(f"{'='*70}")

        for pdf in pdfs:
            print(f"\n  Analyzing: {pdf.name}")
            try:
                analysis = analyze_document(pdf)
                manifest = generate_manifest(analysis, folder.name)

                s = analysis["summary"]
                print(f"    Pages: {analysis['page_count']}")
                print(f"    Widgets: {s['total_widgets']} (filled={s['filled_widgets']}, empty={s['empty_widgets']})")
                print(f"    Sig fields: {s['signature_fields']} | Date fields: {s['date_fields']}")
                print(f"    Sig areas (text): {s['signature_areas_text']} | Date areas (text): {s['date_areas_text']}")
                print(f"    Fillable blanks: {s['fillable_blanks']}")
                print(f"    Test address refs: {s['test_address_refs']}")
                if s["time_length_options"]:
                    print(f"    Time-length options: {len(s['time_length_options'])}")
                    for tl in s["time_length_options"]:
                        print(f"      p{tl['page']}: {tl.get('name', tl.get('text',''))} = {tl['days']} days")
                if manifest["issues"]:
                    print(f"    ISSUES: {len(manifest['issues'])}")
                    for issue in manifest["issues"]:
                        print(f"      [{issue['type']}] {issue['detail']}")

                total_test_refs += s["test_address_refs"]
                total_time_lengths.extend(s["time_length_options"])

                # Save manifest
                safe_name = re.sub(r"[^\w\-.]", "_", pdf.stem) + ".yaml"
                manifest_path = MANIFEST_DIR / folder.name
                manifest_path.mkdir(exist_ok=True, parents=True)
                with open(manifest_path / safe_name, "w") as f:
                    yaml.dump(manifest, f, default_flow_style=False, sort_keys=False, width=120)

                all_results.append(manifest)

            except Exception as e:
                print(f"    ERROR: {e}")
                all_results.append({
                    "file": pdf.name,
                    "folder": folder.name,
                    "error": str(e),
                })

    # Cross-reference
    print(f"\n{'='*70}")
    print("CROSS-REFERENCE: Brokerage requirements vs. CAR packages")
    print(f"{'='*70}")
    xref = cross_reference_brokerage()
    if "error" not in xref:
        print(f"  Required docs: {xref['total_required']}")
        print(f"  Matched: {xref['matched']}")
        print(f"  Missing from packages: {xref['unmatched']}")
        if xref["missing_from_packages"]:
            print("\n  Missing documents:")
            for m in xref["missing_from_packages"]:
                print(f"    [{m['code']}] {m['name']} ({m['section']}, {m['required']})")

    # Summary report
    print(f"\n{'='*70}")
    print("OVERALL SUMMARY")
    print(f"{'='*70}")
    print(f"  Total PDFs analyzed: {len([r for r in all_results if 'error' not in r])}")
    print(f"  Errors: {len([r for r in all_results if 'error' in r])}")
    print(f"  Total '123 Test St.' references: {total_test_refs}")
    print(f"  Total time-length options for review: {len(total_time_lengths)}")

    needs_attention = [r for r in all_results if r.get("status") == "needs_attention"]
    print(f"  Documents needing attention: {len(needs_attention)}")
    for r in needs_attention:
        print(f"    - {r['file']}: {[i['type'] for i in r.get('issues', [])]}")

    # Save summary
    summary = {
        "run_date": datetime.now().isoformat(timespec="seconds"),
        "total_analyzed": len([r for r in all_results if "error" not in r]),
        "total_test_refs": total_test_refs,
        "total_time_lengths": len(total_time_lengths),
        "time_length_details": total_time_lengths,
        "cross_reference": xref,
        "needs_attention": [{
            "file": r["file"],
            "folder": r.get("folder", ""),
            "issues": r.get("issues", []),
        } for r in needs_attention],
    }
    with open(MANIFEST_DIR / "_summary.yaml", "w") as f:
        yaml.dump(summary, f, default_flow_style=False, sort_keys=False, width=120)

    print(f"\nManifests saved to: {MANIFEST_DIR}")
    return summary


if __name__ == "__main__":
    run_full_analysis()
