"""Agent-only PDF review copies with color-coded highlights."""
import fitz
from pathlib import Path
from . import rules

COLORS = {
    "YELLOW": (1, 0.95, 0.6),
    "ORANGE": (1, 0.8, 0.4),
    "RED":    (1, 0.6, 0.6),
    "BLUE":   (0.6, 0.8, 1),
    "GREEN":  (0.7, 1, 0.7),
    "PURPLE": (0.85, 0.7, 1),
}


def _cover(doc, gate: dict):
    pg = doc.new_page(width=612, height=792)
    y = 72
    pg.insert_text((72, y), f"AGENT REVIEW — {gate['name']}", fontsize=16, fontname="helv", color=(0.2, 0.2, 0.6))
    y += 28
    pg.insert_text((72, y), f"Gate: {gate['id']}  |  Type: {gate['type']}", fontsize=11, fontname="helv")
    y += 18
    pg.insert_text((72, y), f"Legal: {gate['legal_basis']['statute']}", fontsize=10, fontname="helv", color=(0.5, 0, 0))
    y += 25
    pg.insert_text((72, y), "VERIFY:", fontsize=11, fontname="helv")
    for item in gate.get("what_agent_verifies", []):
        y += 15
        pg.insert_text((90, y), f"\u2610 {item}", fontsize=9, fontname="helv")
    y += 30
    for label, rgb in COLORS.items():
        pg.draw_rect(fitz.Rect(90, y - 8, 106, y + 4), color=rgb, fill=rgb)
        pg.insert_text((112, y), label, fontsize=8, fontname="helv")
        y += 14
    y += 25
    pg.insert_text((72, y), "Sign-off: ________________  Date: __________", fontsize=10, fontname="helv")


def review_copy(src_pdf: str, gate_id: str, out_dir: Path, highlights: list[dict] | None = None) -> Path:
    """Generate review PDF.

    highlights: [{"page": 0, "rect": [x0,y0,x1,y1], "color": "RED", "note": "..."}]
    If None, copies all pages with a yellow review banner.
    """
    src = fitz.open(src_pdf)
    out = fitz.open()
    g = rules.gate(gate_id)
    _cover(out, g)

    if highlights:
        pages = sorted({h["page"] for h in highlights})
        for i in pages:
            pg = out.new_page(width=src[i].rect.width, height=src[i].rect.height)
            pg.show_pdf_page(pg.rect, src, i)
            for h in highlights:
                if h["page"] != i:
                    continue
                r = fitz.Rect(h["rect"])
                rgb = COLORS.get(h.get("color", "YELLOW"), COLORS["YELLOW"])
                a = pg.add_highlight_annot(r)
                a.set_colors(stroke=rgb)
                a.set_opacity(0.35)
                a.update()
                if h.get("note"):
                    pg.insert_text((r.x1 + 4, r.y0 + 10), h["note"], fontsize=7, fontname="helv", color=(0.3, 0.3, 0.3))
    else:
        for i in range(len(src)):
            pg = out.new_page(width=src[i].rect.width, height=src[i].rect.height)
            pg.show_pdf_page(pg.rect, src, i)
            pg.draw_rect(fitz.Rect(0, 0, pg.rect.width, 22), color=COLORS["YELLOW"], fill=COLORS["YELLOW"])
            pg.insert_text((8, 15), f"AGENT REVIEW — {gate_id} — Page {i+1}/{len(src)}", fontsize=8, fontname="helv", color=(0.3, 0.3, 0.3))

    out_path = out_dir / f"{gate_id}_review.pdf"
    out_dir.mkdir(parents=True, exist_ok=True)
    out.save(str(out_path))
    src.close()
    out.close()
    return out_path
