"""PDF review overlay generator.

Generates agent-only review copies with:
- Page filtering (only pages needing verification)
- Color-coded highlights on specific fields
- Margin annotations explaining each highlight
- Summary cover page with verification checklist
"""

from __future__ import annotations

from pathlib import Path

import fitz  # PyMuPDF

from tc.models import HighlightAnnotation, HighlightColor

# RGB color values for highlights (translucent overlays)
COLOR_MAP: dict[HighlightColor, tuple[float, float, float]] = {
    HighlightColor.YELLOW: (1.0, 0.95, 0.0),
    HighlightColor.ORANGE: (1.0, 0.65, 0.0),
    HighlightColor.RED:    (1.0, 0.0, 0.0),
    HighlightColor.BLUE:   (0.0, 0.45, 1.0),
    HighlightColor.GREEN:  (0.0, 0.75, 0.0),
    HighlightColor.PURPLE: (0.55, 0.0, 0.85),
}

LABEL_MAP: dict[HighlightColor, str] = {
    HighlightColor.YELLOW: "VERIFY",
    HighlightColor.ORANGE: "ATTENTION",
    HighlightColor.RED:    "CRITICAL",
    HighlightColor.BLUE:   "REVIEWABLE",
    HighlightColor.GREEN:  "AI VERIFIED",
    HighlightColor.PURPLE: "JURISDICTION",
}


def generate_review_copy(
    source_pdf: str | Path,
    annotations: list[HighlightAnnotation],
    output_path: str | Path,
    gate_id: str,
    gate_name: str,
    legal_basis: str = "",
    address: str = "",
) -> Path:
    """Generate an annotated review copy from a source PDF.

    1. Creates a summary cover page
    2. Extracts only pages that have annotations
    3. Applies highlight overlays and margin notes
    4. Saves to output_path (agent's private folder)
    """
    source_pdf = Path(source_pdf)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    src_doc = fitz.open(str(source_pdf))
    out_doc = fitz.open()  # new empty document

    # --- Determine which pages to include ---
    pages_needed = sorted({a.page for a in annotations})

    # Count highlights by color
    color_counts: dict[HighlightColor, int] = {}
    for ann in annotations:
        color_counts[ann.color] = color_counts.get(ann.color, 0) + 1

    # --- Build summary cover page ---
    cover = out_doc.new_page(width=612, height=792)  # US Letter
    _draw_cover_page(cover, gate_id, gate_name, legal_basis, address,
                     source_pdf.name, len(pages_needed), src_doc.page_count,
                     annotations, color_counts)

    # --- Copy and annotate relevant pages ---
    for page_idx, src_page_num in enumerate(pages_needed):
        if src_page_num >= src_doc.page_count:
            continue

        # Copy page to output
        out_doc.insert_pdf(src_doc, from_page=src_page_num, to_page=src_page_num)
        out_page = out_doc[-1]

        # Add page header
        review_page_num = page_idx + 2  # +1 for cover, +1 for 1-based
        header_text = (
            f"Page {review_page_num} of {len(pages_needed) + 1} | "
            f"Source: {source_pdf.name} p.{src_page_num + 1} | Gate: {gate_id}"
        )
        header_rect = fitz.Rect(36, 10, 576, 28)
        out_page.insert_textbox(
            header_rect, header_text,
            fontsize=8, color=(0.4, 0.4, 0.4),
        )

        # Apply annotations for this page
        page_anns = [a for a in annotations if a.page == src_page_num]
        for ann in page_anns:
            _apply_highlight(out_page, ann)

    src_doc.close()
    out_doc.save(str(output_path))
    out_doc.close()

    return output_path


def _draw_cover_page(
    page,
    gate_id: str,
    gate_name: str,
    legal_basis: str,
    address: str,
    source_name: str,
    review_pages: int,
    total_pages: int,
    annotations: list[HighlightAnnotation],
    color_counts: dict[HighlightColor, int],
) -> None:
    """Draw the summary cover page with verification checklist."""
    y = 60

    # Title
    page.insert_textbox(
        fitz.Rect(36, y, 576, y + 30),
        "AGENT REVIEW COPY — CONFIDENTIAL",
        fontsize=16, color=(0.8, 0, 0), align=fitz.TEXT_ALIGN_CENTER,
    )
    y += 40

    # Address
    page.insert_textbox(
        fitz.Rect(36, y, 576, y + 24),
        address or "Property Address",
        fontsize=14, color=(0, 0, 0), align=fitz.TEXT_ALIGN_CENTER,
    )
    y += 35

    # Gate info
    info_lines = [
        f"Gate: {gate_id} — {gate_name}",
        f"Type: HARD_GATE — System halts until you verify",
        f"Legal Basis: {legal_basis}" if legal_basis else "",
        f"Source Document: {source_name}",
        f"Review Pages: {review_pages} (of {total_pages} in original)",
        "",
        "ITEMS TO VERIFY:",
    ]
    for line in info_lines:
        if not line:
            y += 8
            continue
        page.insert_textbox(
            fitz.Rect(36, y, 576, y + 16),
            line, fontsize=10, color=(0, 0, 0),
        )
        y += 18

    # Color breakdown
    for color, count in sorted(color_counts.items(), key=lambda x: x[0].value):
        rgb = COLOR_MAP[color]
        label = LABEL_MAP[color]
        # Draw color swatch
        swatch = fitz.Rect(50, y + 2, 62, y + 12)
        page.draw_rect(swatch, color=rgb, fill=rgb)
        page.insert_textbox(
            fitz.Rect(68, y, 576, y + 16),
            f"{label}: {count} item(s)",
            fontsize=10, color=(0, 0, 0),
        )
        y += 16

    y += 20

    # Checklist of items
    page.insert_textbox(
        fitz.Rect(36, y, 576, y + 16),
        "VERIFICATION CHECKLIST:",
        fontsize=11, color=(0, 0, 0),
    )
    y += 20

    for i, ann in enumerate(annotations, 1):
        if y > 720:  # don't overflow page
            page.insert_textbox(
                fitz.Rect(36, y, 576, y + 14),
                f"... and {len(annotations) - i + 1} more items (see review pages)",
                fontsize=9, color=(0.4, 0.4, 0.4),
            )
            break
        rgb = COLOR_MAP[ann.color]
        label = LABEL_MAP[ann.color]
        text = f"☐  [{label}] {ann.field_name}"
        if ann.annotation_text:
            text += f" — {ann.annotation_text}"
        page.insert_textbox(
            fitz.Rect(50, y, 576, y + 14),
            text, fontsize=9, color=rgb,
        )
        y += 14

    # Sign-off footer
    y = 740
    page.draw_line(fitz.Point(36, y), fitz.Point(300, y), color=(0, 0, 0))
    page.insert_textbox(
        fitz.Rect(36, y + 4, 576, y + 18),
        "Agent Signature / Verification Date",
        fontsize=8, color=(0.4, 0.4, 0.4),
    )


def _apply_highlight(page, ann: HighlightAnnotation) -> None:
    """Apply a single highlight annotation to a page."""
    rgb = COLOR_MAP[ann.color]

    # Draw translucent highlight rectangle
    rect = fitz.Rect(ann.x0, ann.y0, ann.x1, ann.y1)
    annot = page.add_highlight_annot(rect)
    annot.set_colors(stroke=rgb)
    annot.set_opacity(0.35)
    annot.update()

    # Add margin annotation (right side)
    margin_x = 430
    margin_width = 170
    note_rect = fitz.Rect(margin_x, ann.y0, margin_x + margin_width, ann.y0 + 40)

    label = LABEL_MAP[ann.color]
    note_text = f"[{label}] {ann.gate_id}"
    if ann.annotation_text:
        note_text += f"\n{ann.annotation_text}"
    if ann.legal_citation:
        note_text += f"\n{ann.legal_citation}"
    if ann.action_needed:
        note_text += f"\n→ {ann.action_needed}"

    page.insert_textbox(
        note_rect, note_text,
        fontsize=7, color=rgb,
    )
