# Project: Transaction Coordinator

## User Preferences

- Make decisions autonomously. Do not ask clarifying questions — use your best judgment and proceed. The user prefers action over deliberation.
- When planning, answer your own open questions based on context and codebase patterns rather than prompting the user.
- Default to the simplest working approach. Ship it, then iterate.
- Think and respond like a top-producing California real estate agent. Apply deep knowledge of CAR forms, escrow timelines, disclosure requirements, contingency periods, and brokerage compliance. When making product decisions, choose what a high-volume agent or TC would actually need in practice.

## PDF & Contract Verification

When modifying PDF annotation, field detection, or bounding box logic:
1. **Always OCR-verify** — render affected pages to PNG and visually inspect the output before presenting to the user
2. Check bounding box accuracy: rectangles should tightly frame the entry space + label word, not span entire lines
3. Verify fill detection: pre-filled fields (DRE license, default day counts) must show GREEN, empty fields must show RED/YELLOW/ORANGE
4. Validate classification: `$` entries = entry_dollar, `Days` entries = entry_days, signatures = entry_signature, etc.
5. Test with both blank originals AND filled test docs to catch false positives/negatives
6. The `ul_bbox` (underline bounding box) is separate from the display `bbox` — use `ul_bbox` for fill detection on wide entries to avoid capturing static contract text

## Tech Stack

- Python 3.11+, Flask, SQLite (single file via tcli/db.py)
- Vanilla JS SPA (no framework), CSS custom properties for theming
- PyMuPDF for PDF analysis, YAML manifests for document field maps
- Notifications: SMTP email, Pushover, ntfy.sh (tcli/notify.py)
- Integrations: DocuSign + SkySlope adapters with sandbox mode (tcli/integrations.py)

## Architecture Notes

- All state in SQLite (~/.tc/tc.db). Schema defined in tcli/db.py SCHEMA string, new columns via _MIGRATIONS list.
- Web UI served by Flask (tcli/web.py) on port 5001. Single HTML template, one JS file, one CSS file.
- TC_SANDBOX=1 (default) mocks all external API calls and stores emails in outbox table.
- Jurisdiction rules in `jurisdictions/*.yaml` (California state + LA city/county). Gate definitions in `rules/*.yaml`.
- Brokerage checklists in `checklists/*.yaml`. Property flags (HOA, trust, solar, etc.) trigger conditional documents.
- CLI via Typer (`tcli/cli.py`). Web UI and CLI share the same engine/db layer.

## Session Continuity

When continuing across sessions, keep this outline current. Add new features as they ship.

### Features Built
1. **Transaction CRUD** — create, delete, phase advancement, property flags
2. **Document Checklist** — brokerage-specific, auto-populated, receive/verify/N/A workflow
3. **Compliance Gates** — 80+ CA gates with legal citations, phase-gated verification
4. **Deadline Tracking** — extracted from contracts, urgency coloring, timeline view
5. **Signature Review** — auto-detect from PDF manifests, SIG/INI classification, review/flag workflow
6. **Follow-up Pipeline** — sandboxed DocuSign/SkySlope/Email adapters, envelope tracking, reminders, simulate-sign
7. **Outbox** — all emails captured (sandbox or real), viewable per transaction
8. **Chat Panel** — Claude-powered assistant with full transaction context
9. **UX Layer** — dark mode, toast notifications, command palette (Cmd+K), keyboard shortcuts, responsive sidebar
10. **Contract Annotation** — PDF field detection via drawn underlines (PyMuPDF), color-coded rectangles (red=mandatory, yellow=optional, orange=days, green=filled), ul_bbox for precise fill detection, MAX_UL_WIDTH=200pt cap, context-based mandatory classification

### DB Tables
`txns`, `docs`, `gates`, `deadlines`, `audit`, `sig_reviews`, `envelope_tracking`, `outbox`

### Key Endpoints (web.py)
- Transactions: CRUD + advance phase + properties
- Documents: list, receive, verify, N/A
- Gates: list, verify
- Deadlines: list (with days_remaining)
- Signatures: list (lazy-populate), review, flag, add, delete
- Follow-ups: send, remind, simulate, outbox, envelopes
- Meta: brokerages, phases, sandbox-status, chat, doc-packages
