"""Document version tracking for CAR Contract Packages.

Tracks PDF file hashes to detect when forms are updated (new CAR revisions).
Supports:
- Detecting changed/new/removed PDFs since last scan
- Re-analyzing only changed files
- Version history log
- Field-location queries for quick zoom-in
"""
import hashlib
import json
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CAR_DIR = ROOT / "CAR Contract Packages"
MANIFEST_DIR = ROOT / "doc_manifests"
VERSION_DB = MANIFEST_DIR / "_versions.yaml"

SIGNATURE_CATEGORIES = {
    "signature_area",
    "signature",
    "entry_signature",
    "entry_initial",
}


def file_hash(path: Path) -> str:
    """SHA-256 hash of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def scan_pdfs() -> dict:
    """Scan all PDFs and return {relative_path: hash}."""
    current = {}
    for folder in sorted(d for d in CAR_DIR.iterdir() if d.is_dir()):
        for pdf in sorted(folder.glob("*.pdf")):
            rel = f"{folder.name}/{pdf.name}"
            current[rel] = file_hash(pdf)
    return current


def load_version_db() -> dict:
    """Load the version database."""
    if VERSION_DB.exists():
        with open(VERSION_DB) as f:
            return yaml.safe_load(f) or {}
    return {"files": {}, "history": []}


def save_version_db(db: dict):
    """Persist the version database."""
    MANIFEST_DIR.mkdir(exist_ok=True)
    with open(VERSION_DB, "w") as f:
        yaml.dump(db, f, default_flow_style=False, sort_keys=False, width=120)


def check_changes() -> dict:
    """Compare current PDFs against stored hashes. Return change report."""
    db = load_version_db()
    stored = db.get("files", {})
    current = scan_pdfs()

    added = [k for k in current if k not in stored]
    removed = [k for k in stored if k not in current]
    changed = [k for k in current if k in stored and current[k] != stored[k]]
    unchanged = [k for k in current if k in stored and current[k] == stored[k]]

    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "unchanged": unchanged,
        "total_current": len(current),
        "current_hashes": current,
    }


def update_versions(changes: dict, reanalyzed: list[str] = None):
    """Update version DB after a scan/re-analysis."""
    db = load_version_db()
    now = datetime.now().isoformat(timespec="seconds")

    # Update hashes
    db["files"] = changes["current_hashes"]

    # Log change event
    entry = {
        "timestamp": now,
        "added": len(changes["added"]),
        "removed": len(changes["removed"]),
        "changed": len(changes["changed"]),
        "unchanged": len(changes["unchanged"]),
    }
    if changes["added"]:
        entry["added_files"] = changes["added"]
    if changes["removed"]:
        entry["removed_files"] = changes["removed"]
    if changes["changed"]:
        entry["changed_files"] = changes["changed"]
    if reanalyzed:
        entry["reanalyzed"] = reanalyzed

    db.setdefault("history", []).append(entry)
    save_version_db(db)
    return entry


def needs_reanalysis() -> list[str]:
    """Return list of PDF relative paths needing re-analysis."""
    changes = check_changes()
    return changes["added"] + changes["changed"]


# ── Field lookup for zoom-in ─────────────────────────────────────────────────

def load_manifest(folder: str, filename: str) -> dict:
    """Load a document manifest."""
    folder_name = Path(folder).name
    if folder_name != folder or folder_name in {"", ".", ".."}:
        return {}

    stem = Path(filename).stem
    # Normalize to match manifest naming
    import re
    safe_name = re.sub(r"[^\w\-.]", "_", stem) + ".yaml"
    base = MANIFEST_DIR.resolve()
    folder_path = (MANIFEST_DIR / folder_name).resolve()
    try:
        folder_path.relative_to(base)
    except ValueError:
        return {}

    manifest_path = (folder_path / safe_name).resolve()
    try:
        manifest_path.relative_to(folder_path)
    except ValueError:
        return {}

    if manifest_path.exists():
        with open(manifest_path) as f:
            return yaml.safe_load(f) or {}
    return {}


def field_locations(folder: str, filename: str, category: str = None, page: int = None) -> list:
    """Query field locations for a document. Filter by category and/or page.

    Returns list of fields with page, bbox, category, region for zoom-in.
    """
    m = load_manifest(folder, filename)
    fields = m.get("field_map", [])

    if category:
        cat = (category or "").lower()
        if cat in ("signature_area", "signature", "entry_signature"):
            fields = [f for f in fields if (f.get("category") or "").lower() in SIGNATURE_CATEGORIES]
        elif cat == "entry_initial":
            fields = [f for f in fields if (f.get("category") or "").lower() == "entry_initial"]
        else:
            fields = [f for f in fields if (f.get("category") or "").lower() == cat]
    if page:
        fields = [f for f in fields if f.get("page") == page]

    return fields


def signature_locations(folder: str, filename: str) -> list:
    """Get all signature locations for quick zoom-in."""
    m = load_manifest(folder, filename)
    return [
        f for f in m.get("field_map", [])
        if (f.get("category") or "").lower() in SIGNATURE_CATEGORIES
    ]


def date_locations(folder: str, filename: str) -> list:
    """Get all date/time-length fields for review."""
    m = load_manifest(folder, filename)
    fields = [
        f for f in m.get("field_map", [])
        if (f.get("category") or "").lower() in ("date", "date_area", "entry_date")
    ]
    time_lengths = m.get("time_length_review", [])
    return {"date_fields": fields, "time_length_options": time_lengths}


# ── Incremental re-analysis ──────────────────────────────────────────────────

def reanalyze_changed():
    """Re-analyze only PDFs that have changed since last scan."""
    from . import doc_analyzer

    changes = check_changes()
    to_analyze = changes["added"] + changes["changed"]

    if not to_analyze:
        print("All documents up to date. No re-analysis needed.")
        update_versions(changes)
        return {"reanalyzed": 0}

    print(f"Re-analyzing {len(to_analyze)} changed/new documents...")
    reanalyzed = []

    for rel_path in to_analyze:
        pdf_path = CAR_DIR / rel_path
        folder_name = pdf_path.parent.name
        print(f"  {rel_path}")

        try:
            analysis = doc_analyzer.analyze_document(pdf_path)
            manifest = doc_analyzer.generate_manifest(analysis, folder_name)

            # Bump version for changed files
            if rel_path in changes["changed"]:
                old_manifest = load_manifest(folder_name, pdf_path.name)
                old_ver = old_manifest.get("version", "1.0.0")
                parts = old_ver.split(".")
                parts[-1] = str(int(parts[-1]) + 1)
                manifest["version"] = ".".join(parts)
                manifest["previous_version"] = old_ver

            import re
            safe_name = re.sub(r"[^\w\-.]", "_", pdf_path.stem) + ".yaml"
            manifest_path = MANIFEST_DIR / folder_name
            manifest_path.mkdir(exist_ok=True, parents=True)
            with open(manifest_path / safe_name, "w") as f:
                yaml.dump(manifest, f, default_flow_style=False, sort_keys=False, width=120)

            reanalyzed.append(rel_path)
        except Exception as e:
            print(f"    ERROR: {e}")

    entry = update_versions(changes, reanalyzed)
    print(f"\nDone. {len(reanalyzed)} documents re-analyzed.")
    if changes["removed"]:
        print(f"Removed (no longer present): {changes['removed']}")

    return {"reanalyzed": len(reanalyzed), "entry": entry}


# ── CLI entry point ──────────────────────────────────────────────────────────

def main():
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    if cmd == "status":
        changes = check_changes()
        print(f"Total PDFs: {changes['total_current']}")
        print(f"New: {len(changes['added'])}")
        print(f"Changed: {len(changes['changed'])}")
        print(f"Removed: {len(changes['removed'])}")
        print(f"Unchanged: {len(changes['unchanged'])}")
        if changes["added"]:
            print("\nNew files:")
            for f in changes["added"]:
                print(f"  + {f}")
        if changes["changed"]:
            print("\nChanged files:")
            for f in changes["changed"]:
                print(f"  ~ {f}")

    elif cmd == "update":
        reanalyze_changed()

    elif cmd == "history":
        db = load_version_db()
        for entry in db.get("history", [])[-10:]:
            print(f"  {entry['timestamp']}: +{entry['added']} ~{entry['changed']} -{entry['removed']}")

    elif cmd == "fields":
        if len(sys.argv) < 4:
            print("Usage: doc_versions fields <folder> <filename> [category] [page]")
            sys.exit(1)
        folder = sys.argv[2]
        filename = sys.argv[3]
        cat = sys.argv[4] if len(sys.argv) > 4 else None
        pg = int(sys.argv[5]) if len(sys.argv) > 5 else None
        fields = field_locations(folder, filename, cat, pg)
        for f in fields:
            print(f"  p{f['page']} [{f['category']}] {f['region']}: {f.get('field','')} @ {f['bbox']}")

    else:
        print("Commands: status, update, history, fields")


if __name__ == "__main__":
    main()
