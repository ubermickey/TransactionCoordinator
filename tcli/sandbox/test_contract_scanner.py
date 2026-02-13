"""Deterministic local tests for contract scanner fill detection."""
import re
from pathlib import Path

import fitz
import yaml

from tcli.contract_scanner import _detect_filled, scan_pdf

ROOT = Path(__file__).resolve().parents[2]
FOLDER = "Residential Purchase Offer Agreement Package"
FILE = "California_Residential_Purchase_Agreement_-_1225_ts69656.pdf"
BLANK_PDF = ROOT / "CAR Contract Packages" / FOLDER / FILE
MISSING_SIGS_PDF = ROOT / "Randomly Filled Test Docs" / "6-Missing-Signatures" / FOLDER / FILE
MANIFEST = ROOT / "doc_manifests" / FOLDER / (re.sub(r"[^\w\-.]", "_", Path(FILE).stem) + ".yaml")


def register(runner):
    manifest = {}

    def setup():
        nonlocal manifest
        assert BLANK_PDF.exists(), f"missing blank PDF: {BLANK_PDF}"
        assert MISSING_SIGS_PDF.exists(), f"missing scenario PDF: {MISSING_SIGS_PDF}"
        assert MANIFEST.exists(), f"missing manifest: {MANIFEST}"
        with open(MANIFEST) as f:
            manifest = yaml.safe_load(f) or {}

    def test_blank_rpa_not_all_filled():
        """Blank RPA should not scan as fully filled."""
        data = scan_pdf(BLANK_PDF, FOLDER, scenario="blank")
        assert data, "scan_pdf returned no data for blank RPA"
        assert data["filled_fields"] < data["total_fields"], (
            f"blank contract incorrectly marked fully filled: {data['filled_fields']}/{data['total_fields']}"
        )
        assert data["unfilled_mandatory"] > 0, "expected unfilled mandatory fields on blank RPA"

    def test_missing_signatures_has_unfilled_required():
        """Missing-signatures scenario should expose unfilled required signature/initial fields."""
        data = scan_pdf(MISSING_SIGS_PDF, FOLDER, scenario="6-Missing-Signatures")
        assert data, "scan_pdf returned no data for missing-signatures scenario"
        assert data["unfilled_mandatory"] > 0, "expected unfilled mandatory fields"
        unfilled_sig = [
            f for f in data["fields"]
            if f["category"] in ("entry_signature", "entry_initial") and not f["is_filled"]
        ]
        assert unfilled_sig, "expected unfilled signature/initial fields in missing-signatures scenario"

    def test_wide_signature_ul_bbox_empty_on_blank():
        """Wide signature/initial fields with ul_bbox should remain empty on blank docs."""
        field_map = manifest.get("field_map", [])
        candidates = []
        for e in field_map:
            cat = (e.get("category") or "").lower()
            if cat not in ("entry_signature", "entry_initial", "signature", "signature_area"):
                continue
            bbox = e.get("bbox") or {}
            ul_bbox = e.get("ul_bbox") or {}
            if "x0" not in bbox or "x0" not in ul_bbox:
                continue
            if (bbox["x1"] - bbox["x0"]) < 120:
                continue
            candidates.append(e)
        assert candidates, "expected wide signature fields with ul_bbox in manifest"

        doc = fitz.open(str(BLANK_PDF))
        try:
            for e in candidates[:20]:
                page_num = int(e.get("page", 1)) - 1
                if page_num < 0 or page_num >= len(doc):
                    continue
                filled = _detect_filled(
                    doc[page_num],
                    e.get("bbox") or {},
                    field_name=e.get("field") or "",
                    category=e.get("category") or "",
                    ul_bbox=e.get("ul_bbox"),
                )
                assert not filled, f"blank wide field falsely marked filled: p{e.get('page')} {e.get('field')}"
        finally:
            doc.close()

    runner.test("scan:setup", setup)
    runner.test("scan:blank_rpa_not_all_filled", test_blank_rpa_not_all_filled)
    runner.test("scan:missing_signatures_has_unfilled_required", test_missing_signatures_has_unfilled_required)
    runner.test("scan:wide_signature_ul_bbox_empty_on_blank", test_wide_signature_ul_bbox_empty_on_blank)
