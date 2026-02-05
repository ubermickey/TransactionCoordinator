"""PDF document analyzer for CAR Contract Packages.

Scans all PDFs using PyMuPDF to extract precise data-entry spaces
using a LABEL-FIRST algorithm:
1. Scan text for known label patterns (signature, date, $, etc.)
2. Look for a drawn underline near each label in the expected direction
3. Create an entry ONLY when both label + line exist
4. Lines without matching labels are structural (table borders, etc.) → skip

This eliminates false positives from table column borders, footer lines,
and other structural elements that the old line-first approach picked up.

Outputs per-document YAML manifests with version tracking.
"""
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import fitz  # PyMuPDF
import yaml

# ── Paths ────────────────────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent.parent
CAR_DIR = ROOT / "CAR Contract Packages"
MANIFEST_DIR = ROOT / "doc_manifests"

# ── Patterns ─────────────────────────────────────────────────────────────────

TIME_LENGTH_PATTERN = re.compile(
    r"(\d+)\s*(?:calendar|business|banking)?\s*days?", re.I
)

TEST_ADDRESS = re.compile(r"123\s*Test\s*St\.?", re.I)

# Footer attribution text — lines near these are structural
FOOTER_TEXT = re.compile(r"Produced with|zipForm|Lone Wolf", re.I)


def classify_field(name: str, label: str) -> str:
    """Classify a widget field as signature, date, address, or fillable."""
    combined = f"{name} {label}"
    if re.search(r"signature|initial", combined, re.I):
        return "entry_signature"
    if re.search(r"\bdate\b", combined, re.I):
        return "entry_date"
    if re.search(r"address|property|street|city|zip|state", combined, re.I):
        return "entry_address"
    if re.search(r"license|dre|calbre", combined, re.I):
        return "entry_license"
    return "entry_blank"


def bbox_to_dict(bbox) -> dict:
    """Convert a fitz Rect or tuple to a dict with rounded coords."""
    if hasattr(bbox, "x0"):
        return {"x0": round(bbox.x0, 1), "y0": round(bbox.y0, 1),
                "x1": round(bbox.x1, 1), "y1": round(bbox.y1, 1)}
    return {"x0": round(bbox[0], 1), "y0": round(bbox[1], 1),
            "x1": round(bbox[2], 1), "y1": round(bbox[3], 1)}


# ── Label Pattern Definitions ────────────────────────────────────────────────
# Each pattern: (category, direction_to_find_line, compiled_regex, min_line_width)
# direction: "above" = line is ABOVE the label text (signatures)
#            "right" = line is to the RIGHT of label
#            "left"  = line is to the LEFT of label
#            "either"= check both left and right

def _build_label_patterns():
    """Build the label pattern table used for label-first detection."""
    patterns = []

    def add(category, direction, regex_str, min_w=15, max_w=None):
        patterns.append({
            "category": category,
            "direction": direction,
            "regex": re.compile(regex_str, re.I),
            "min_w": min_w,
            "max_w": max_w,
        })

    # ── Signatures: label BELOW a long underline (line is ABOVE the label) ──
    sig_labels = (
        r"^("
        r"signature|"
        r"\(signature\)\s*by|"
        r"buyer\s*$|seller\s*$|tenant\s*$|landlord\s*$|"
        r"housing\s+provider\s*$|rental\s+property\s+owner\s*$|"
        r"by\s*$|"
        r"printed\s+name\s+of|"
        r"associate[- ]licensee|"
        r"buyer/?tenant|seller/?housing\s+provider|"
        r"buyer/?seller/?landlord/?tenant|"
        r"tenant\s*\(signature\)|housing\s+provider\s*\(signature\)|"
        r"printed\s+name\s+of\s+legally\s+authorized\s+signer|"
        r"printed\s+name\s+of\s+buyer|printed\s+name\s+of\s+seller|"
        r"printed\s+name\s+of\s+owner|printed\s+name\s+of\s+tenant|"
        r"printed\s+name\s+of\s+housing\s+provider|"
        r"printed\s+name\s+of\s+rpo|"
        r"guarantor|guarantor\s*\(print\s*name\)"
        r")$"
    )
    add("entry_signature", "above", sig_labels, min_w=100)

    # ── Signatures: label LEFT of a long underline (standalone sign lines) ──
    # "Buyer _____________ Date ___" layout on signature pages
    add("entry_signature", "right",
        r"^(buyer|seller|tenant|landlord|by|"
        r"housing\s+provider|rental\s+property\s+owner|guarantor)$",
        min_w=100)

    # ── Initials: label LEFT of short underline ──
    add("entry_signature", "right",
        r"^(buyer'?s?\s+initials?|seller'?s?\s+initials?|"
        r"owner'?s?\s+initials?|tenant'?s?\s+initials?|"
        r"housing\s+providers?\s+initials?|"
        r"buyer'?s?/?tenant'?s?\s+initials?)$",
        min_w=15, max_w=60)

    # ── Date: label left or right of underline ──
    # Match "Date" or "Date Prepared" but NOT "dated" (past tense in sentences)
    add("entry_date", "either",
        r"^date(\s+prepared)?$", min_w=15)

    # ── Dollar amounts ──
    add("entry_dollar", "right",
        r"(?:^|\s)\$\s*$|"
        r"dollars?\s*\$|dollars?\s*\(\s*\$|"
        r"cost\s+not\s+to\s+exceed\s*\$|"
        r"agrees?\s+to\s+pay\s*\$|"
        r"rental\s+fee.*\$|"
        r"treatment\s+is:?\s*\$|"
        r"an\s+additional\s*\$|"
        r"^\$$",
        min_w=15)

    # ── Days: "N (or" LEFT pattern, "days" RIGHT pattern ──
    add("entry_days", "right",
        r"\d+\s*\(or$|\d+\s*\(or\s*$|within\s+\d+\s*\(or",
        min_w=15)
    add("entry_days", "left",
        r"^(days?|calendar\s+days?|business\s+days?|\)\s*days?)$",
        min_w=15)

    # ── License / DRE ──
    add("entry_license", "right",
        r"dre\s+lic\.?\s*#?|license\s+number|calbre|caldre|"
        r"lic\.?\s*#|dre\s*#",
        min_w=15)

    # ── Contact info ──
    add("entry_contact", "right",
        r"^(e-?mail|phone\s*#?|tel(ephone)?\s*#?|fax|be\s+contacted\s+at)$",
        min_w=15)

    # ── Address ──
    add("entry_address", "right",
        r"^(property\s+address|address|city|state|zip(\s+code)?|"
        r"\(city\)|\(state\)|\(zip\s*code?\)|county(\s+of)?|"
        r"situated\s+in|street\s+address|\(unit/?apartment\))$",
        min_w=15)

    # ── Percent ──
    add("entry_percent", "right", r"^%$|percent\s+of", min_w=15)

    # ── Brokerage / Agent ──
    add("entry_brokerage", "right",
        r"buyer'?s?\s+brokerage\s+firm|seller'?s?\s+brokerage\s+firm|"
        r"housing\s+provider'?s?\s+brokerage\s+firm|"
        r"tenant'?s?\s+brokerage\s+firm|"
        r"real\s+estate\s+broker\s*\(|"
        r"^(agent|seller'?s?\s+agent|buyer'?s?\s+agent)$",
        min_w=15)

    # ── Generic fill-in blanks ──
    add("entry_blank", "right",
        r"^(explanation|explanation/?clarification|"
        r"other\s*(terms|items|instructions|documents|addenda)?|"
        r"additional\s+(terms|inspection)|"
        r"assessor'?s?\s+parcel\s+no|title,?\s+if\s+applicable|"
        r"escrow\s+(holder|#)|addendum\s*#|unit\s*#|bath\s*#|"
        r"premises|year\s+built|roof.*type|name|age|"
        r"this\s+is\s+an\s+offer\s+from|to\s+be\s+acquired\s+is|"
        r"described\s+as|title)$",
        min_w=15)

    return patterns


LABEL_PATTERNS = _build_label_patterns()


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

        if "date" in ftype:
            m = TIME_LENGTH_PATTERN.search(f"{name} {label} {value}")
            if m:
                field["time_length_days"] = int(m.group(1))

        fields.append(field)
    return fields


def detect_entry_spaces(page) -> dict:
    """Label-first entry space detection.

    Algorithm:
    1. Collect all text spans and drawn underlines
    2. Filter out structural lines (footer, table columns, full-width separators)
    3. For each text span, check against known label patterns
    4. When a label matches, look for a nearby unclaimed underline in the
       expected direction (above/right/left/either)
    5. Create entry only when BOTH label + line exist
    6. Fallback: unclaimed non-structural lines with adjacent text → entry_blank

    Returns dict with 'entries' and 'test_address_refs'.
    """
    entries = []
    test_refs = []
    page_height = page.rect.height
    page_width = page.rect.width
    seen_keys = set()  # deduplicate by (y, x0)

    ADJ_GAP = 20   # max horizontal gap (pt) between label and line
    VERT_TOL = 8    # vertical tolerance for same-line alignment
    MAX_UL_WIDTH = 200  # cap display width for long underlines

    # ── Step 1: Collect all text spans with positions ──
    all_spans = []
    blocks = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)["blocks"]
    for block in blocks:
        if block["type"] != 0:
            continue
        for line in block.get("lines", []):
            full_text = "".join(s["text"] for s in line.get("spans", [])).strip()
            if not full_text:
                continue
            lb = line["bbox"]

            # Test address references
            if TEST_ADDRESS.search(full_text):
                test_refs.append({
                    "text": full_text[:120],
                    "bbox": bbox_to_dict(lb),
                    "page_region": _page_region(lb, page_height),
                })

            for span in line.get("spans", []):
                st = span["text"].strip()
                if st:
                    all_spans.append({
                        "text": st,
                        "bbox": span["bbox"],
                        "line_text": full_text,
                        "line_bbox": lb,
                    })

            # Text underscore blanks (___) — add directly as entries
            if re.search(r"_{3,}", full_text):
                y_key = (round(lb[1]), round(lb[0]))
                if y_key not in seen_keys:
                    seen_keys.add(y_key)
                    capped_lb = lb
                    if lb[2] - lb[0] > 250:
                        capped_lb = (lb[0], lb[1], lb[0] + 250, lb[3])
                    entries.append({
                        "category": "entry_blank",
                        "bbox": bbox_to_dict(capped_lb),
                        "field": full_text[:80],
                        "context": full_text[:120],
                        "region": _page_region(capped_lb, page_height),
                    })

    # ── Step 1b: Collect drawn horizontal underlines ──
    underlines = []
    drawings = page.get_drawings()
    for d in drawings:
        sw = d.get("width", 1)
        for item in d.get("items", []):
            if item[0] == "l":  # line command
                p1, p2 = item[1], item[2]
                dy = abs(p1.y - p2.y)
                dx = abs(p1.x - p2.x)
                # Horizontal, thin stroke, not full-page width
                if dy < 2 and dx > 15 and sw <= 0.5 and dx < page_width * 0.75:
                    underlines.append({
                        "x0": min(p1.x, p2.x),
                        "y": (p1.y + p2.y) / 2,
                        "x1": max(p1.x, p2.x),
                        "width": dx,
                    })

    # ── Step 2: Filter structural lines ──
    structural = set()  # indices of structural underlines

    # 2a. Footer attribution lines: y > 740 and footer text nearby
    footer_text_on_page = any(
        FOOTER_TEXT.search(sp["text"]) for sp in all_spans
    )
    for i, ul in enumerate(underlines):
        if ul["y"] > 740 and footer_text_on_page:
            structural.add(i)

    # 2b. Table column borders: lines sharing same x0 (within 2pt)
    x0_groups = defaultdict(list)
    for i, ul in enumerate(underlines):
        x0_key = round(ul["x0"])
        x0_groups[x0_key].append(i)
    # Merge nearby x0 keys (within 3pt)
    merged_groups = {}
    sorted_keys = sorted(x0_groups.keys())
    for k in sorted_keys:
        placed = False
        for mk in merged_groups:
            if abs(k - mk) <= 3:
                merged_groups[mk].extend(x0_groups[k])
                placed = True
                break
        if not placed:
            merged_groups[k] = list(x0_groups[k])
    for mk, indices in merged_groups.items():
        if len(indices) >= 4:
            # Table columns: 4+ lines at same x0, BUT only if they are
            # short (<100pt) — long lines at the same x0 are entry spaces
            # (e.g., signature lines all starting at same indent)
            widths = [underlines[i]["width"] for i in indices]
            avg_w = sum(widths) / len(widths)
            if avg_w < 100:
                for i in indices:
                    structural.add(i)

    # 2c. Full-width separators: width > 60% page width
    for i, ul in enumerate(underlines):
        if ul["width"] > page_width * 0.60:
            structural.add(i)

    # Build non-structural underline list with claimed tracking
    claimed = set()  # indices of underlines claimed by a label match

    # ── Step 1c: Split spans into words with approximate positions ──
    all_words = []
    for sp in all_spans:
        sb = sp["bbox"]
        text = sp["text"].strip()
        if not text:
            continue
        parts = text.split()
        if len(parts) <= 1:
            all_words.append({
                "text": text, "bbox": sb,
                "line_text": sp["line_text"],
            })
        else:
            span_w = sb[2] - sb[0]
            total_chars = sum(len(p) for p in parts) + len(parts) - 1
            if total_chars == 0:
                continue
            char_w = span_w / total_chars
            x = sb[0]
            for part in parts:
                pw = len(part) * char_w
                all_words.append({
                    "text": part,
                    "bbox": (x, sb[1], x + pw, sb[3]),
                    "line_text": sp["line_text"],
                })
                x += pw + char_w

    # ── Helper: find nearby unclaimed non-structural underline ──
    def _find_line(span_bbox, direction, min_w, max_w=None):
        """Find the closest unclaimed non-structural underline near a label.

        direction: "right" (line RIGHT of label), "left" (line LEFT of label),
                   "above" (line ABOVE label), "either" (check right then left)
        Returns (underline_index, underline_dict) or (None, None).
        """
        sb = span_bbox
        s_y_mid = (sb[1] + sb[3]) / 2
        best_idx = None
        best_dist = float("inf")

        for i, ul in enumerate(underlines):
            if i in structural or i in claimed:
                continue
            if ul["width"] < min_w:
                continue
            if max_w and ul["width"] > max_w:
                continue

            if direction == "right":
                # Line starts near where label ends, same vertical band
                if abs(s_y_mid - ul["y"]) < VERT_TOL:
                    gap = ul["x0"] - sb[2]
                    if -2 <= gap <= ADJ_GAP:
                        if gap < best_dist:
                            best_dist = gap
                            best_idx = i

            elif direction == "left":
                # Line ends near where label starts, same vertical band
                if abs(s_y_mid - ul["y"]) < VERT_TOL:
                    gap = sb[0] - ul["x1"]
                    if -2 <= gap <= ADJ_GAP:
                        if gap < best_dist:
                            best_dist = gap
                            best_idx = i

            elif direction == "above":
                # Line is ABOVE the label text (label sits below the line)
                # The line's y should be above span's top (y0) by 0-15pt
                vert_gap = sb[1] - ul["y"]
                if 0 <= vert_gap <= 15:
                    # Horizontal overlap: label text overlaps the line's span
                    if sb[2] > ul["x0"] and sb[0] < ul["x1"]:
                        if vert_gap < best_dist:
                            best_dist = vert_gap
                            best_idx = i

            elif direction == "either":
                # Try right first, then left
                if abs(s_y_mid - ul["y"]) < VERT_TOL:
                    gap_r = ul["x0"] - sb[2]
                    gap_l = sb[0] - ul["x1"]
                    if -2 <= gap_r <= ADJ_GAP:
                        if gap_r < best_dist:
                            best_dist = gap_r
                            best_idx = i
                    elif -2 <= gap_l <= ADJ_GAP:
                        if gap_l < best_dist:
                            best_dist = gap_l
                            best_idx = i

        if best_idx is not None:
            return best_idx, underlines[best_idx]
        return None, None

    # ── Helper: build bbox from label + underline ──
    def _build_bbox(label_bbox, ul, direction):
        """Build display bbox covering label + underline, with width caps."""
        lb = label_bbox
        ul_w = ul["x1"] - ul["x0"]

        if direction == "above":
            # Label is below line
            x0 = ul["x0"]
            x1 = ul["x1"]
            y0 = ul["y"] - 6
            y1 = max(ul["y"] + 2, lb[3])
            if ul_w > MAX_UL_WIDTH:
                mid = (lb[0] + lb[2]) / 2
                x0 = max(x0, mid - MAX_UL_WIDTH / 2)
                x1 = min(x1, mid + MAX_UL_WIDTH / 2)
        elif direction in ("right", "either"):
            # Label + line — encompass both regardless of relative position
            x0 = min(lb[0], ul["x0"])
            x1 = max(lb[2], ul["x1"])
            y0 = min(ul["y"] - 6, lb[1])
            y1 = max(ul["y"] + 2, lb[3])
            if ul_w > MAX_UL_WIDTH:
                # Cap underline display width, but keep label visible
                if lb[0] < ul["x0"]:
                    # Label left, line right — cap line's right end
                    x1 = min(ul["x1"], lb[2] + MAX_UL_WIDTH)
                else:
                    # Label right, line left — cap line's left end
                    x0 = max(ul["x0"], lb[0] - MAX_UL_WIDTH)
        elif direction == "left":
            # Line left, label right
            x0 = ul["x0"]
            x1 = max(lb[2], ul["x1"])
            y0 = min(ul["y"] - 6, lb[1])
            y1 = max(ul["y"] + 2, lb[3])
            if ul_w > MAX_UL_WIDTH:
                x0 = max(ul["x0"], lb[0] - MAX_UL_WIDTH)
        else:
            x0 = ul["x0"]
            x1 = ul["x1"]
            y0 = ul["y"] - 6
            y1 = ul["y"] + 2
            if ul_w > MAX_UL_WIDTH:
                x1 = x0 + MAX_UL_WIDTH

        # Absolute safety cap
        if x1 - x0 > MAX_UL_WIDTH + 50:
            x1 = x0 + MAX_UL_WIDTH + 50

        return (x0, y0, x1, y1)

    # ── Helper: get aligned text context for an underline ──
    def _get_context(ul):
        """Get all text aligned with an underline for context string."""
        ctx_words = []
        for w in all_words:
            wb = w["bbox"]
            w_y_mid = (wb[1] + wb[3]) / 2
            if abs(w_y_mid - ul["y"]) < VERT_TOL:
                ctx_words.append(w["text"])
        return " ".join(ctx_words)

    # ── Step 3: Label-first matching ──
    # For each span, try all label patterns. When matched, find a nearby line.
    # We match against FULL span text and also multi-word sequences.

    for sp in all_spans:
        sp_text = sp["text"].strip()
        if not sp_text:
            continue
        sp_lower = sp_text.lower()
        # Also try the full line text for multi-word patterns
        line_lower = sp.get("line_text", "").lower()

        for pat in LABEL_PATTERNS:
            # Check span text first (most labels are single spans)
            matched_text = None
            if pat["regex"].search(sp_lower):
                matched_text = sp_text
            elif pat["regex"].search(line_lower):
                # Multi-word label that spans the full line text
                matched_text = sp_text

            if not matched_text:
                continue

            # Found a label match — look for an underline
            ul_idx, ul = _find_line(
                sp["bbox"], pat["direction"], pat["min_w"],
                pat.get("max_w"),
            )
            if ul is None:
                continue

            # Deduplicate
            key = (round(ul["y"]), round(ul["x0"]))
            if key in seen_keys:
                continue
            seen_keys.add(key)

            # Claim this line
            claimed.add(ul_idx)

            # Build bbox
            bbox = _build_bbox(sp["bbox"], ul, pat["direction"])

            # Context
            line_ctx = _get_context(ul)
            if pat["direction"] == "above":
                line_ctx += " " + sp.get("line_text", "")

            # Extra: time_length_days for entry_days
            extra = {}
            if pat["category"] == "entry_days":
                m = TIME_LENGTH_PATTERN.search(line_ctx)
                if m:
                    extra["time_length_days"] = int(m.group(1))

            entries.append({
                "category": pat["category"],
                "bbox": bbox_to_dict(bbox),
                "ul_bbox": bbox_to_dict((ul["x0"], ul["y"] - 4,
                                         ul["x1"], ul["y"] + 2)),
                "field": sp_text[:80],
                "context": line_ctx[:120],
                "region": _page_region(bbox, page_height),
                **extra,
            })
            break  # first matching pattern wins for this span

    # ── Step 4: Fallback for unclaimed non-structural lines ──
    # Lines that weren't matched by any label pattern but have adjacent text.
    # Only accept words that look like plausible labels — skip noise words
    # (articles, conjunctions, punctuation, single digits).
    NOISE_WORDS = {
        "a", "an", "the", "and", "or", "of", "to", "in", "on", "at",
        "by", "is", "as", "if", "for", "not", "but", "has", "had",
        "are", "was", "be", "no", "so", "it", "its", "do", "may",
        "can", "all", "any", "per", "via", "from", "with", "that",
        "this", "than", "then", "also", "both", "each", "such",
        "will", "shall", "been", "being", "upon", "into", "only",
        "more", "most", "less", "very", "does", "did", "have",
        "must", "would", "could", "should", "which", "their",
        "them", "they", "when", "were", "what", "who", "whom",
        "your", "our", "his", "her", "my",
    }
    NOISE_PAT = re.compile(
        r"^[.,:;/\\()\[\]{}<>|&*+=#@!?\-~`\"\']+$|"  # pure punctuation
        r"^\d{1,2}$"  # 1-2 digit numbers
    )

    for i, ul in enumerate(underlines):
        if i in structural or i in claimed:
            continue

        key = (round(ul["y"]), round(ul["x0"]))
        if key in seen_keys:
            continue

        # Find adjacent text (left or right, or below for long lines)
        aligned = []
        for w in all_words:
            wb = w["bbox"]
            w_y_mid = (wb[1] + wb[3]) / 2
            if abs(w_y_mid - ul["y"]) < VERT_TOL:
                aligned.append(w)

        # Skip footer lines
        combined = " ".join(w["text"] for w in aligned)
        if FOOTER_TEXT.search(combined):
            continue

        # Find closest LABEL-LIKE word to LEFT (skip noise)
        closest_left = None
        best_gap = float("inf")
        for w in aligned:
            wb = w["bbox"]
            wt = w["text"].strip().lower()
            if wt in NOISE_WORDS or NOISE_PAT.match(wt):
                continue
            if wb[2] <= ul["x0"] + 2 and wb[2] > ul["x0"] - ADJ_GAP:
                gap = ul["x0"] - wb[2]
                if gap < best_gap:
                    best_gap = gap
                    closest_left = w

        # Find closest LABEL-LIKE word to RIGHT (skip noise)
        closest_right = None
        best_gap = float("inf")
        for w in aligned:
            wb = w["bbox"]
            wt = w["text"].strip().lower()
            if wt in NOISE_WORDS or NOISE_PAT.match(wt):
                continue
            if wb[0] >= ul["x1"] - 2 and wb[0] < ul["x1"] + ADJ_GAP:
                gap = wb[0] - ul["x1"]
                if gap < best_gap:
                    best_gap = gap
                    closest_right = w

        # Check below for long lines
        closest_below = None
        if ul["width"] >= 100:
            best_gap = float("inf")
            for w in all_words:
                wb = w["bbox"]
                wt = w["text"].strip().lower()
                if wt in NOISE_WORDS or NOISE_PAT.match(wt):
                    continue
                if wb[1] > ul["y"] and wb[1] < ul["y"] + 15:
                    if wb[2] > ul["x0"] and wb[0] < ul["x1"]:
                        gap = wb[1] - ul["y"]
                        if gap < best_gap:
                            best_gap = gap
                            closest_below = w

        label_word = None
        direction_used = "right"
        if closest_left:
            label_word = closest_left
            direction_used = "right"  # label is left, line is right
        elif closest_right:
            label_word = closest_right
            direction_used = "left"  # label is right, line is left
        elif closest_below:
            label_word = closest_below
            direction_used = "above"
        else:
            # No label-like adjacent text — orphan structural line → skip
            continue

        seen_keys.add(key)
        claimed.add(i)

        bbox = _build_bbox(label_word["bbox"], ul, direction_used)
        line_ctx = _get_context(ul)

        # Classify the fallback entry using the label word
        lw_text = re.sub(r"[.,:;]+$", "", label_word["text"]).lower()
        classify_text = (label_word["text"] + " " + label_word.get("line_text", "")).lower()

        # Check for days pattern nearby
        has_days = False
        for w in aligned:
            wb = w["bbox"]
            if wb[0] >= ul["x1"] - 5 and wb[0] < ul["x1"] + 60:
                if re.match(r"^days?$", w["text"], re.I):
                    has_days = True
                    break

        if has_days:
            category = "entry_days"
        elif re.search(r"(\d+|_+|\))\s*(calendar\s+|business\s+)?days?\b", classify_text):
            category = "entry_days"
        elif lw_text in ("signature", "sign"):
            category = "entry_signature"
        elif lw_text in ("initial", "initials"):
            category = "entry_signature"
        elif lw_text in ("dre", "lic", "license", "calbre", "caldre"):
            category = "entry_license"
        elif lw_text == "date":
            category = "entry_date"
        elif lw_text in ("phone", "fax", "email", "e-mail"):
            category = "entry_contact"
        elif lw_text == "$":
            category = "entry_dollar"
        elif lw_text == "%":
            category = "entry_percent"
        elif re.search(r"signature|sign here", classify_text):
            category = "entry_signature"
        elif re.search(r"\bdate\b", classify_text):
            category = "entry_date"
        elif re.search(r"\$", classify_text):
            category = "entry_dollar"
        elif re.search(r"phone|fax|email", classify_text):
            category = "entry_contact"
        elif re.search(r"print\s*name|firm\s*name|broker.*name|agent.*name", classify_text):
            category = "entry_brokerage"
        else:
            category = "entry_blank"

        extra = {}
        if category == "entry_days":
            m = TIME_LENGTH_PATTERN.search(line_ctx)
            if m:
                extra["time_length_days"] = int(m.group(1))

        entries.append({
            "category": category,
            "bbox": bbox_to_dict(bbox),
            "ul_bbox": bbox_to_dict((ul["x0"], ul["y"] - 4,
                                     ul["x1"], ul["y"] + 2)),
            "field": label_word["text"][:80],
            "context": line_ctx[:120],
            "region": _page_region(bbox, page_height),
            **extra,
        })

    return {
        "entries": entries,
        "test_address_refs": test_refs,
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
            "total_entry_spaces": 0,
            "entry_categories": {},
            "test_address_refs": 0,
            "time_length_options": [],
            "incomplete_fields": [],
        },
    }

    for pno in range(len(doc)):
        page = doc[pno]
        page_data = {"page": pno + 1, "width": round(page.rect.width, 1),
                      "height": round(page.rect.height, 1)}

        # Widgets (PDF form fields)
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

        # Entry space detection (span-level)
        text_info = detect_entry_spaces(page)
        page_data["entries"] = text_info["entries"]
        page_data["test_address_refs"] = text_info["test_address_refs"]

        result["summary"]["total_entry_spaces"] += len(text_info["entries"])
        result["summary"]["test_address_refs"] += len(text_info["test_address_refs"])
        for e in text_info["entries"]:
            cat = e["category"]
            result["summary"]["entry_categories"][cat] = \
                result["summary"]["entry_categories"].get(cat, 0) + 1
            if e.get("time_length_days"):
                result["summary"]["time_length_options"].append({
                    "page": pno + 1,
                    "text": e["field"][:80],
                    "days": e["time_length_days"],
                    "bbox": e["bbox"],
                })

        result["pages"].append(page_data)

    doc.close()
    return result


# ── Manifest generation ──────────────────────────────────────────────────────

def generate_manifest(analysis: dict, folder_name: str) -> dict:
    """Generate a YAML-ready manifest with version tracking."""
    s = analysis["summary"]
    manifest = {
        "version": "3.0.0",
        "last_analyzed": datetime.now().isoformat(timespec="seconds"),
        "file": analysis["file"],
        "folder": folder_name,
        "page_count": analysis["page_count"],
        "status": "complete" if s["empty_widgets"] == 0 and s["test_address_refs"] == 0
                  else "needs_attention",
        "issues": [],
        "summary": {
            "total_form_widgets": s["total_widgets"],
            "filled_widgets": s["filled_widgets"],
            "empty_widgets": s["empty_widgets"],
            "total_entry_spaces": s["total_entry_spaces"],
            "entry_categories": s["entry_categories"],
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

    # Build field map — widgets first, then text-detected entry spaces
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
        for e in page_data["entries"]:
            entry = {
                "page": pno,
                "field": e["field"],
                "category": e["category"],
                "bbox": e["bbox"],
                "region": e["region"],
            }
            if e.get("time_length_days"):
                entry["time_length_days"] = e["time_length_days"]
            if e.get("context"):
                entry["context"] = e["context"]
            if e.get("ul_bbox"):
                entry["ul_bbox"] = e["ul_bbox"]
            manifest["field_map"].append(entry)

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
                cats = s.get("entry_categories", {})
                cat_str = ", ".join(f"{k}={v}" for k, v in sorted(cats.items()))
                print(f"    Entry spaces: {s['total_entry_spaces']} ({cat_str})")
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
