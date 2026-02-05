"""Generate annotated PDFs with circles around every entry field.

Reads CAR Contract Packages + doc_manifests, draws color-coded circles
at every field_map entry, and saves to 'Testing Contracts/' folder.

Colors:
  Green  — signature areas
  Red    — date areas
  Yellow — fillable blanks
  Blue   — address fields
  Orange — time_length_review entries
"""
import sys
from pathlib import Path

import fitz  # PyMuPDF
import yaml

ROOT = Path(__file__).parent
CAR_DIR = ROOT / "CAR Contract Packages"
MANIFEST_DIR = ROOT / "doc_manifests"
OUTPUT_DIR = ROOT / "Testing Contracts"

# Color map: category -> (r, g, b) 0-1 range
COLORS = {
    "signature_area": (0.2, 0.78, 0.35),   # green
    "signature":      (0.2, 0.78, 0.35),
    "date_area":      (1.0, 0.23, 0.19),    # red
    "date":           (1.0, 0.23, 0.19),
    "fillable_blanks":(1.0, 0.8, 0.0),      # yellow
    "fillable":       (1.0, 0.8, 0.0),
    "address":        (0.0, 0.48, 1.0),     # blue
    "address_area":   (0.0, 0.48, 1.0),
    "time_length":    (1.0, 0.55, 0.0),     # orange
}
DEFAULT_COLOR = (0.55, 0.55, 0.58)  # gray


def get_color(category):
    return COLORS.get(category, DEFAULT_COLOR)


def annotate_pdf(pdf_path, manifest, output_path):
    """Open a PDF, draw circles at all field_map entries, save."""
    doc = fitz.open(str(pdf_path))
    field_map = manifest.get("field_map", [])
    time_lengths = manifest.get("time_length_review", [])

    drawn = 0

    # Draw field_map entries
    for entry in field_map:
        page_num = entry.get("page", 1) - 1  # 0-indexed in PyMuPDF
        if page_num < 0 or page_num >= len(doc):
            continue
        bbox = entry.get("bbox", {})
        if not bbox or "x0" not in bbox:
            continue

        page = doc[page_num]
        x0, y0, x1, y1 = bbox["x0"], bbox["y0"], bbox["x1"], bbox["y1"]
        cx = (x0 + x1) / 2
        cy = (y0 + y1) / 2
        w = abs(x1 - x0)
        h = abs(y1 - y0)
        r = max(w, h) / 2
        r = max(r, 6)  # minimum radius
        r = min(r, 25)  # max radius

        color = get_color(entry.get("category", ""))
        # Draw circle
        shape = page.new_shape()
        shape.draw_circle(fitz.Point(cx, cy), r)
        shape.finish(color=color, width=1.5, fill=None)
        shape.commit()
        drawn += 1

    # Draw time_length_review entries (orange circles)
    for entry in time_lengths:
        page_num = entry.get("page", 1) - 1
        if page_num < 0 or page_num >= len(doc):
            continue
        bbox = entry.get("bbox", {})
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

        color = get_color("time_length")
        shape = page.new_shape()
        shape.draw_circle(fitz.Point(cx, cy), r)
        shape.finish(color=color, width=1.5, fill=None)
        shape.commit()
        drawn += 1

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))
    doc.close()
    return drawn


def main():
    if not CAR_DIR.exists():
        print(f"ERROR: {CAR_DIR} not found")
        sys.exit(1)
    if not MANIFEST_DIR.exists():
        print(f"ERROR: {MANIFEST_DIR} not found")
        sys.exit(1)

    OUTPUT_DIR.mkdir(exist_ok=True)
    total_pdfs = 0
    total_fields = 0

    for folder in sorted(d for d in CAR_DIR.iterdir() if d.is_dir()):
        folder_name = folder.name
        out_folder = OUTPUT_DIR / folder_name
        print(f"\n--- {folder_name} ---")

        for pdf_file in sorted(folder.glob("*.pdf")):
            # Find matching manifest
            import re
            safe_name = re.sub(r"[^\w\-.]", "_", pdf_file.stem) + ".yaml"
            manifest_path = MANIFEST_DIR / folder_name / safe_name
            if not manifest_path.exists():
                print(f"  SKIP {pdf_file.name} (no manifest)")
                continue

            with open(manifest_path) as f:
                manifest = yaml.safe_load(f) or {}

            field_count = len(manifest.get("field_map", []))
            tl_count = len(manifest.get("time_length_review", []))
            if field_count == 0 and tl_count == 0:
                print(f"  SKIP {pdf_file.name} (no fields)")
                continue

            output_path = out_folder / pdf_file.name
            drawn = annotate_pdf(pdf_file, manifest, output_path)
            total_pdfs += 1
            total_fields += drawn
            print(f"  OK   {pdf_file.name} — {drawn} circles")

    print(f"\n{'='*50}")
    print(f"Done: {total_pdfs} PDFs, {total_fields} field circles drawn")
    print(f"Output: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
