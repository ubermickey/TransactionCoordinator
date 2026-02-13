"""Deterministic local tests for signature/initial entry detection."""
from pathlib import Path

import fitz

from tcli.doc_analyzer import detect_entry_spaces

ROOT = Path(__file__).resolve().parents[2]
RPA_FOLDER = "Residential Purchase Offer Agreement Package"
RPA_FILE = "California_Residential_Purchase_Agreement_-_1225_ts69656.pdf"
RPA_PATH = ROOT / "CAR Contract Packages" / RPA_FOLDER / RPA_FILE


def register(runner):
    doc = None
    entries_by_page = {}

    def setup():
        nonlocal doc, entries_by_page
        assert RPA_PATH.exists(), f"missing test PDF: {RPA_PATH}"
        doc = fitz.open(str(RPA_PATH))
        entries_by_page = {}
        for pno in range(len(doc)):
            out = detect_entry_spaces(doc[pno])
            entries_by_page[pno + 1] = out.get("entries", [])

    def teardown():
        if doc:
            doc.close()

    def test_initial_category_present():
        """Detector should emit a dedicated entry_initial category."""
        total_initial = sum(
            1 for rows in entries_by_page.values() for e in rows
            if e.get("category") == "entry_initial"
        )
        total_sig = sum(
            1 for rows in entries_by_page.values() for e in rows
            if e.get("category") == "entry_signature"
        )
        assert total_initial > 0, "expected entry_initial detections"
        assert total_sig > 0, "expected entry_signature detections"

    def test_footer_initials_detected():
        """Footer pages should contain buyer/seller initials slots."""
        pages = range(4, 17)  # RPA body footer pages
        total_initial = 0
        for p in pages:
            rows = [e for e in entries_by_page.get(p, []) if e.get("category") == "entry_initial"]
            total_initial += len(rows)
            assert len(rows) >= 4, f"page {p} expected >=4 initials slots, got {len(rows)}"
            names = " ".join((e.get("field") or "").lower() for e in rows)
            assert "buyer" in names, f"page {p} missing buyer initials labels"
            assert "seller" in names, f"page {p} missing seller initials labels"
        assert total_initial >= 52, f"footer initials recall too low ({total_initial})"

    def test_service_provider_not_signature():
        """Service-provider/options rows should not be tagged as signatures."""
        bad = []
        for e in entries_by_page.get(5, []):
            if e.get("category") != "entry_signature":
                continue
            ctx = (e.get("context") or "").lower()
            if "service provider" in ctx or "click here" in ctx or "portfolio escrow" in ctx:
                bad.append(e)
        assert not bad, f"found non-signature false positives on page 5: {len(bad)}"

    runner.test("detector:setup", setup)
    runner.test("detector:rpa_initial_category_present", test_initial_category_present)
    runner.test("detector:rpa_footer_initials_detected", test_footer_initials_detected)
    runner.test("detector:rpa_service_provider_not_signature", test_service_provider_not_signature)
    runner.test("detector:teardown", teardown)
