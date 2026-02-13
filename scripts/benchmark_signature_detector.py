#!/usr/bin/env python3
"""Benchmark signature/initial detection quality and write a markdown report."""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tcli.contract_scanner import scan_pdf
from tcli.doc_analyzer import (
    INITIAL_LABEL_RE,
    NON_SIG_CONTEXT_RE,
    SIG_CONTEXT_RE,
    detect_entry_spaces,
)

RPA_FOLDER = "Residential Purchase Offer Agreement Package"
RPA_FILE = "California_Residential_Purchase_Agreement_-_1225_ts69656.pdf"
RPA_PDF = ROOT / "CAR Contract Packages" / RPA_FOLDER / RPA_FILE
MISSING_SIGS_PDF = ROOT / "Randomly Filled Test Docs" / "6-Missing-Signatures" / RPA_FOLDER / RPA_FILE
REPORT_PATH = ROOT / "docs" / "strategy" / "signature-detector-benchmark-latest.md"

STRICT_INITIAL_RECALL = 0.98
STRICT_SIGNATURE_PRECISION = 0.93

GENERIC_SIG_LABEL_RE = re.compile(
    r"^(buyer|seller|tenant|landlord|by|housing\s+provider|"
    r"rental\s+property\s+owner|guarantor|seller/housing\s+provider)$",
    re.I,
)


@dataclass
class Metric:
    name: str
    value: float
    threshold: float

    @property
    def passed(self) -> bool:
        return self.value >= self.threshold

    @property
    def pct(self) -> str:
        return f"{self.value * 100:.2f}%"

    @property
    def threshold_pct(self) -> str:
        return f"{self.threshold * 100:.2f}%"


def _ulines(page) -> list[dict]:
    out = []
    width = page.rect.width
    for d in page.get_drawings():
        sw = d.get("width", 1)
        for item in d.get("items", []):
            if item[0] != "l":
                continue
            p1, p2 = item[1], item[2]
            dy = abs(p1.y - p2.y)
            dx = abs(p1.x - p2.x)
            if dy < 2 and dx > 15 and sw <= 0.5 and dx < width * 0.75:
                out.append({
                    "x0": min(p1.x, p2.x),
                    "x1": max(p1.x, p2.x),
                    "y": (p1.y + p2.y) / 2,
                    "w": dx,
                })
    return out


def _expected_initial_slots(doc) -> list[tuple[int, float, float]]:
    expected = []
    for pno in range(len(doc)):
        page = doc[pno]
        labels = []
        blocks = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE).get("blocks", [])
        for b in blocks:
            if b.get("type") != 0:
                continue
            for ln in b.get("lines", []):
                for sp in ln.get("spans", []):
                    st = (sp.get("text") or "").strip()
                    if INITIAL_LABEL_RE.search(st):
                        labels.append(sp["bbox"])
        if not labels:
            continue
        ulines = _ulines(page)
        for lb in labels:
            y_mid = (lb[1] + lb[3]) / 2
            slots = []
            for ul in ulines:
                if not (15 <= ul["w"] <= 70):
                    continue
                if abs(ul["y"] - y_mid) > 10:
                    continue
                gap = ul["x0"] - lb[2]
                if -2 <= gap <= 260:
                    slots.append((gap, ul))
            slots.sort(key=lambda it: (it[0], it[1]["x0"]))
            for _, ul in slots[:2]:
                expected.append((pno + 1, round(ul["x0"], 1), round(ul["y"], 1)))
    return expected


def _detected_initial_slots(doc) -> tuple[list[tuple[int, float, float]], list[dict], list[dict]]:
    detected_slots = []
    detected_initial_entries = []
    detected_signature_entries = []
    for pno in range(len(doc)):
        out = detect_entry_spaces(doc[pno])
        for e in out.get("entries", []):
            cat = (e.get("category") or "").lower()
            if cat == "entry_initial":
                detected_initial_entries.append(e)
                ul = e.get("ul_bbox") or {}
                if "x0" in ul:
                    y = (ul.get("y0", 0) + ul.get("y1", 0)) / 2
                    detected_slots.append((pno + 1, round(ul["x0"], 1), round(y, 1)))
            elif cat == "entry_signature":
                detected_signature_entries.append(e)
    return detected_slots, detected_initial_entries, detected_signature_entries


def _match_slots(expected: list[tuple[int, float, float]],
                 detected: list[tuple[int, float, float]]) -> int:
    used = set()
    matched = 0
    for ep, ex, ey in expected:
        best_idx = None
        best_score = None
        for i, (dp, dx, dy) in enumerate(detected):
            if i in used or dp != ep:
                continue
            if abs(dx - ex) > 2.0 or abs(dy - ey) > 2.0:
                continue
            score = abs(dx - ex) + abs(dy - ey)
            if best_score is None or score < best_score:
                best_score = score
                best_idx = i
        if best_idx is not None:
            used.add(best_idx)
            matched += 1
    return matched


def _signature_precision_proxy(sig_entries: list[dict]) -> float:
    if not sig_entries:
        return 0.0
    suspicious = 0
    for e in sig_entries:
        field = (e.get("field") or "").strip().lower()
        ctx = (e.get("context") or "").lower()
        if GENERIC_SIG_LABEL_RE.match(field):
            if NON_SIG_CONTEXT_RE.search(ctx) or not SIG_CONTEXT_RE.search(ctx):
                suspicious += 1
    return max(0.0, (len(sig_entries) - suspicious) / len(sig_entries))


def _render_report(metrics: list[Metric], details: dict) -> str:
    status = "PASS" if all(m.passed for m in metrics) else "FAIL"
    lines = [
        "# Signature Detector Benchmark",
        "",
        f"- Run at: `{datetime.now().isoformat(timespec='seconds')}`",
        f"- Overall: **{status}**",
        "",
        "## Metrics",
        "",
        "| Metric | Value | Threshold | Result |",
        "|---|---:|---:|---|",
    ]
    for m in metrics:
        lines.append(
            f"| {m.name} | {m.pct} | {m.threshold_pct} | {'PASS' if m.passed else 'FAIL'} |"
        )

    lines.extend([
        "",
        "## Details",
        "",
        f"- Expected initials slots: `{details['expected_initial_slots']}`",
        f"- Detected initials slots: `{details['detected_initial_slots']}`",
        f"- Matched initials slots: `{details['matched_initial_slots']}`",
        f"- Detected signature entries: `{details['detected_signature_entries']}`",
        f"- Blank RPA filled/total: `{details['blank_filled']}/{details['blank_total']}`",
        f"- Blank RPA unfilled mandatory: `{details['blank_unfilled_mandatory']}`",
        f"- Missing-sigs unfilled mandatory: `{details['missing_sigs_unfilled_mandatory']}`",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    if not RPA_PDF.exists():
        raise SystemExit(f"Missing benchmark PDF: {RPA_PDF}")
    if not MISSING_SIGS_PDF.exists():
        raise SystemExit(f"Missing benchmark scenario PDF: {MISSING_SIGS_PDF}")

    doc = fitz.open(str(RPA_PDF))
    try:
        expected = _expected_initial_slots(doc)
        detected, initial_entries, sig_entries = _detected_initial_slots(doc)
    finally:
        doc.close()

    matched = _match_slots(expected, detected)
    initials_recall = matched / len(expected) if expected else 0.0
    sig_precision = _signature_precision_proxy(sig_entries)

    blank_scan = scan_pdf(RPA_PDF, RPA_FOLDER, scenario="blank")
    missing_scan = scan_pdf(MISSING_SIGS_PDF, RPA_FOLDER, scenario="6-Missing-Signatures")
    if not blank_scan or not missing_scan:
        raise SystemExit("scan_pdf failed for benchmark inputs")

    metrics = [
        Metric("Initials Recall", initials_recall, STRICT_INITIAL_RECALL),
        Metric("Signature Precision (proxy)", sig_precision, STRICT_SIGNATURE_PRECISION),
    ]
    # Scanner sanity checks are hard requirements.
    scanner_ok = (
        blank_scan["filled_fields"] < blank_scan["total_fields"]
        and blank_scan["unfilled_mandatory"] > 0
        and missing_scan["unfilled_mandatory"] > 0
    )

    details = {
        "expected_initial_slots": len(expected),
        "detected_initial_slots": len(detected),
        "matched_initial_slots": matched,
        "detected_signature_entries": len(sig_entries),
        "blank_filled": blank_scan["filled_fields"],
        "blank_total": blank_scan["total_fields"],
        "blank_unfilled_mandatory": blank_scan["unfilled_mandatory"],
        "missing_sigs_unfilled_mandatory": missing_scan["unfilled_mandatory"],
    }
    report = _render_report(metrics, details)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(report + "\n", encoding="utf-8")
    print(report)

    ok = all(m.passed for m in metrics) and scanner_ok
    if not scanner_ok:
        print("\nScanner sanity checks: FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
