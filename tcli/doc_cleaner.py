"""Remove '123 Test St.' references from CAR Contract Package PDFs.

Uses PyMuPDF redaction to find and blank out all instances of the test address.
Creates backups before modifying and logs all changes.
"""
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

import fitz  # PyMuPDF

ROOT = Path(__file__).resolve().parent.parent
CAR_DIR = ROOT / "CAR Contract Packages"
BACKUP_DIR = ROOT / "CAR Contract Packages_backup"

# Match variations: "123 Test St", "123 Test St.", "123 Test Street"
TEST_PATTERNS = [
    "123 Test St.",
    "123 Test St",
    "123 Test Street",
]


def clean_document(pdf_path: Path) -> dict:
    """Redact test address from a single PDF. Returns change summary."""
    doc = fitz.open(str(pdf_path))
    total_redactions = 0
    page_details = []

    for pno in range(len(doc)):
        page = doc[pno]
        page_redactions = 0

        for pattern in TEST_PATTERNS:
            instances = page.search_for(pattern)
            for rect in instances:
                page.add_redact_annot(rect, fill=(1, 1, 1))  # white fill
                page_redactions += 1

        if page_redactions > 0:
            page.apply_redactions()
            total_redactions += page_redactions
            page_details.append({"page": pno + 1, "redactions": page_redactions})

    result = {
        "file": pdf_path.name,
        "total_redactions": total_redactions,
        "pages": page_details,
    }

    if total_redactions > 0:
        # Save to temp file then replace (PyMuPDF can't overwrite in place without incremental)
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".pdf", dir=str(pdf_path.parent))
        doc.save(tmp_path, deflate=True, garbage=3)
        doc.close()
        Path(tmp_path).replace(pdf_path)
    else:
        doc.close()
    return result


def run_cleanup():
    """Back up and clean all affected PDFs."""
    # Create backup
    if not BACKUP_DIR.exists():
        print(f"Creating backup at: {BACKUP_DIR}")
        shutil.copytree(str(CAR_DIR), str(BACKUP_DIR))
        print("Backup complete.")
    else:
        print(f"Backup already exists at: {BACKUP_DIR}")

    total_files = 0
    total_redactions = 0
    changed_files = []

    folders = sorted(d for d in CAR_DIR.iterdir() if d.is_dir())
    for folder in folders:
        pdfs = sorted(folder.glob("*.pdf"))
        for pdf in pdfs:
            result = clean_document(pdf)
            total_files += 1
            if result["total_redactions"] > 0:
                total_redactions += result["total_redactions"]
                changed_files.append(result)
                print(f"  Cleaned: {pdf.name} — {result['total_redactions']} redactions on {len(result['pages'])} pages")

    print(f"\nDone. {total_files} files scanned, {len(changed_files)} modified, {total_redactions} total redactions.")
    print(f"Backup at: {BACKUP_DIR}")
    return {"files_scanned": total_files, "files_modified": len(changed_files),
            "total_redactions": total_redactions, "details": changed_files}


if __name__ == "__main__":
    run_cleanup()
