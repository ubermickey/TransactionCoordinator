# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Philosophy

- **Bias for action.** Make autonomous decisions — don't ask clarifying questions. Use context and codebase patterns to resolve ambiguity.
- **Elegance over cleverness.** Minimal syntax, novel solutions, zero dead code. Three clear lines beat a premature abstraction.
- **Ship, then iterate.** Simplest working approach first. Refine only when the user asks.
- **Think like a top California TC.** Deep knowledge of CAR forms, escrow timelines, disclosure requirements, contingency periods, brokerage compliance. Product decisions should reflect what a high-volume agent actually needs.

## Commands

```bash
# Run the web server (port 5001)
python3 -m flask --app tcli.web run --port 5001 --debug

# Run the CLI
python3 -m tcli --help
python3 -m tcli new "123 Main St" --type sale --brokerage douglas_elliman

# Verify Python compiles (no tests exist — use this as smoke check)
python3 -c "import tcli.web; import tcli.engine; import tcli.db"

# Verify JS syntax
node -e "const fs=require('fs'); new Function(fs.readFileSync('tcli/static/app.js','utf8')); console.log('OK')"

# Install dependencies
pip install -e .
```

No test suite, linter, or CI pipeline exists. Validate changes with the import/syntax checks above.

## Architecture

### Data Flow

```
Browser (SPA)  →  Flask API (web.py)  →  Engine (engine.py)  →  SQLite (db.py)
     ↑                                        ↓
  app.js ← JSON                         rules.py ← YAML configs
```

**Single-file SPA**: one HTML template (`index.html`), one JS file (`app.js` ~5500 lines), one CSS file (`app.css` ~3900 lines). No framework — vanilla JS with module-pattern IIFEs (Toast, Sidebar, ChatPanel, Shortcuts, PdfViewer, ReviewMode).

### Backend Modules (`tcli/`)

| Module | Purpose |
|--------|---------|
| `web.py` | Flask routes (~3400 lines). All API endpoints. The main file you'll modify for backend changes. |
| `db.py` | SQLite schema, migrations, connection context manager. DB at `~/.tc/tc.db`. |
| `engine.py` | Deadline calculation, phase advancement, gate management, Claude PDF extraction. |
| `rules.py` | YAML loader with `@cache`. Loads phases, gates, deadlines, forms, brokerages, jurisdictions. |
| `checklist.py` | Document checklist resolution — brokerage + property flags → required docs. |
| `contract_scanner.py` | PyMuPDF field detection. Scans PDFs for filled/empty fields, populates `contracts` + `contract_fields` tables. |
| `doc_analyzer.py` | Label-first PDF analysis. Detects signature lines, date fields, dollar entries via drawn underlines. |
| `doc_versions.py` | SHA-256 version tracking for CAR Contract Package PDFs. Detects changed/new/removed forms. |
| `overlay.py` | Agent-only PDF review copies with color-coded highlights (PyMuPDF). |
| `integrations.py` | DocuSign + SkySlope + Email adapters. Email sandboxed by default (`TC_SANDBOX=1`). |
| `notify.py` | SMTP email, Pushover, ntfy.sh notification dispatch. |
| `cli.py` | Typer CLI. Shares engine/db layer with web UI. |

### Config Files (YAML)

| Path | What it drives |
|------|----------------|
| `phases.yaml` | Transaction phase definitions (sale + lease) |
| `deadlines.yaml` | All deadline IDs, offsets, day types, reminder schedules |
| `agent_verification_gates.yaml` | 80+ compliance gates with legal citations, phase assignments |
| `brokerages/*.yaml` | Brokerage-specific checklists and DE gates |
| `jurisdictions/*.yaml` | State/city rules (CA, LA, Beverly Hills) — taxes, disclosures |
| `forms/*.yaml` | CAR form templates (rpa, tds, spq, etc.) for extraction prompts |
| `doc_manifests/` | Per-PDF YAML manifests with field locations, categories, bounding boxes |

### Database

SQLite at `~/.tc/tc.db`. Schema in `db.py` `SCHEMA` string. New columns via `_MIGRATIONS` list (ALTER TABLE, idempotent). Key tables:

**Core**: `txns`, `docs`, `gates`, `deadlines`, `audit`
**Signatures**: `sig_reviews`, `envelope_tracking`, `outbox`
**Contingencies**: `contingencies`, `contingency_items`
**Parties**: `parties`, `disclosures`
**PDF Analysis**: `field_annotations`, `contracts`, `contract_fields`
**Meta**: `bug_reports`, `review_notes`, `features`, `cloud_approvals`, `cloud_events`

### Frontend Patterns

- **Tab system**: 7 tabs — Dashboard, Documents, Signatures, Contingencies, Parties, Compliance, Activity
- `switchTab()` dispatches to render functions. `tabKeys` and `PAGES` array must stay in sync with HTML tab bar.
- Sidebar tools map `data-tool` attributes to action handlers — update both HTML and JS when adding/removing.
- API helpers: `api()`, `get()`, `post()`, `del()` with toast error handling. `getCached()` for dashboard (8s TTL).
- Module-level caches: `_docsData`, `_gatesData`, `_contData`, `_discData` — refreshed on tab render.
- Collapsible sections use `toggleCollapsible(btn)` with `.collapsible-toggle.open` class.
- `PdfViewer.open(folder, filename, txnId)` loads PDFs via `/api/doc-packages/{folder}/{filename}/pdf`.
- `openDocPdf(code)` tries uploaded file → package map → upload dialog fallback.

### Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `TC_DATA_DIR` | `~/.tc` | SQLite database directory |
| `TC_SANDBOX` / `TC_EMAIL_SANDBOX` | `1` | Email sandbox mode (stores to outbox table, no SMTP) |
| `ANTHROPIC_API_KEY` | — | Required for chat panel and PDF extraction |
| `DOCUSIGN_*` | — | DocuSign JWT auth (integration_key, secret_key, account_id, etc.) |

## Feature Registry

The app has a built-in feature tracker (`features` table, `/api/features` endpoints). Every feature records its name, category, affected files, dependencies, and status.

## Cloud Governance

- Cloud calls are transaction-scoped and require active approval (`/api/txns/<tid>/cloud-approval`).
- Approval defaults to 30 minutes unless overridden.
- All cloud call attempts/outcomes are recorded in `cloud_events` and exposed via `/api/txns/<tid>/cloud-events`.

**When you add or modify a feature**: update the feature registry via `POST /api/features` or by adding to `init_features()` in `web.py:3177`. Track:
- `name`: feature name
- `category`: core, docs, compliance, dates, signatures, integrations, comms, ai, pdf, ui, feedback
- `files`: list of files touched
- `depends_on`: list of feature names this depends on
- `status`: active, deprecated, planned

This prevents loose ends — before modifying a feature, check what depends on it.

## PDF & Contract Verification

When modifying PDF annotation, field detection, or bounding box logic:
1. **Always OCR-verify** — render affected pages to PNG and visually inspect before presenting
2. Bounding boxes should tightly frame entry space + label word, not span entire lines
3. Fill detection: pre-filled fields (DRE license, default day counts) = GREEN, empty = RED/YELLOW/ORANGE
4. Classification: `$` entries = `entry_dollar`, `Days` = `entry_days`, signatures = `entry_signature`, initials = `entry_initial`
5. Test with both blank originals AND filled test docs
6. `ul_bbox` (underline bbox) is separate from display `bbox` — use `ul_bbox` for fill detection on wide signature/initial fields to avoid capturing static contract text

## Critical Sync Points

These components must stay in sync when modified:

1. **Tabs**: HTML tab bar (`index.html`) ↔ `tabKeys` (`app.js`) ↔ `PAGES` array ↔ `switchTab()` dispatch ↔ keyboard shortcuts overlay ↔ sidebar tool actions
2. **DB schema**: `SCHEMA` string ↔ `_MIGRATIONS` list (new columns on existing tables go in migrations, new tables go in schema)
3. **Phase/Gate IDs**: `phases.yaml` ↔ `agent_verification_gates.yaml` ↔ `engine.py` phase order ↔ `checklist.py` phase assignments
4. **Contingency → Gate mapping**: `engine.py` `CONT_MAP` dict maps deadline IDs to contingency types and gate IDs
5. **Property flags**: `txns.props` JSON ↔ `checklist.py` conditional docs ↔ `engine.py` conditional inspection items ↔ `app.js` property flag checkboxes
6. **Document codes**: `checklist.py` codes ↔ `docs` table ↔ `_CODE_TO_PDF` / `_findPdf()` package map in `app.js`

## Conventions

- All dates stored as ISO strings (`YYYY-MM-DD` or `datetime('now','localtime')`)
- API routes: `/api/txns/<tid>/...`. Always return JSON.
- Audit trail: call `db.log(c, txn_id, action, detail)` for state-changing operations
- Status enums are string-based: docs (`required`/`received`/`verified`/`na`), gates (`pending`/`verified`), contingency items (`pending`/`scheduled`/`complete`/`waived`)
- `contingency_items` auto-populate 11 base inspection items + conditional items from property flags (pool, pre-1978, septic)
