# AI Context Log — v001 — 2026-02-01 — Initial Build

## What Exists

### Git Branch: claude/real-estate-tc-role-KmkRT

4 commits pushed:
1. `9becd40` — YAML workflow specs (phases, gates, deadlines, jurisdictions)
2. `114387a` — Agent review overlay system (color-coded highlights)
3. `da7894a` — Python CLI app (472 lines, 6 modules, 12 commands)
4. `46c8647` — API_SETUP.txt guide

### Files on Disk

```
workflow/
├── pyproject.toml              # Python package config
├── .env.example                # API key template
├── .gitignore
├── API_SETUP.txt               # Setup instructions for all APIs
├── .venv/                      # Python virtual environment (installed, working)
├── tc/                         # Python package (472 lines total)
│   ├── __init__.py
│   ├── db.py                   # SQLite persistence (40 lines)
│   ├── rules.py                # YAML loader + taxes (39 lines)
│   ├── engine.py               # Deadlines + gates + Claude extraction (99 lines)
│   ├── notify.py               # SMTP email + Pushover/ntfy push (37 lines)
│   ├── overlay.py              # PDF review copies with highlights (69 lines)
│   └── cli.py                  # 12 Typer commands (187 lines)
├── phases.yaml                 # 9 transaction phases
├── agent_verification_gates.yaml  # 20 gates (17 HARD, 3 SOFT)
├── agent_review_overlay.yaml   # Review copy color/highlight spec
├── deadlines.yaml              # Deadline engine (REVIEWABLE + FIXED)
├── document_validation.yaml    # DocuSign + cross-doc checks
├── legal_update_process.yaml   # Rule monitoring process
├── aicontext_log/              # This log directory
│
│ (sibling directories at TransactionCoordinator/ level)
├── ../jurisdictions/california.yaml
├── ../jurisdictions/los_angeles.yaml
├── ../jurisdictions/beverly_hills.yaml
└── ../integration/tools.yaml
```

### SQLite DB Location
`~/.tc/tc.db` — has 2 test transactions (BH + LA)

## What Works (Tested)
- `pip install -e .` in .venv — SUCCESS
- `tc --help` — all 12 commands listed
- `tc new "9876 Wilshire Blvd, Beverly Hills, CA 90210"` — creates txn, detects BH jurisdiction
- `tc new "1234 Sunset Blvd, Los Angeles, CA 90028"` — creates txn, detects LA jurisdiction
- `tc list` — shows both transactions with Rich table

## What Needs Testing Still
- `tc status` — was interrupted
- `tc gates` — gate table display
- `tc deadlines` — deadline table
- `tc taxes` — transfer tax calculation (BH vs LA vs generic CA)
- `tc checklist` — jurisdiction compliance checklist
- `tc verify <gate>` — gate sign-off flow
- `tc review <gate> <pdf>` — PDF review copy generation
- `tc extract <pdf>` — needs ANTHROPIC_API_KEY

## Task List
- #2 [in_progress] Install and test CLI locally
- #3 [pending] Create .env with ANTHROPIC_API_KEY (blocked by #2)
- #4 [pending] Test extraction with real PDF (blocked by #3)
- #5 [pending] Test gate verification end-to-end (blocked by #4)
- #6 [pending] MVP COMPLETE checkpoint (blocked by #5)
- #7 [pending] Connect push notifications
- #8 [pending] Connect Gmail SMTP
- #9 [pending] Connect Google Drive API
- #10 [pending] Connect DocuSign API
- #11 [pending] Add cron digest command
- #12 [pending] Add CAR form template system

## Architecture Decisions
- SQLite instead of JSON files (single file, queryable)
- SMTP instead of Gmail API (no OAuth needed for email)
- Flat tc/ package (no src/ prefix)
- YAML rules drive everything — edit YAML, not code
- Virtual env at workflow/.venv
- 5 dependencies: typer, anthropic, pyyaml, pymupdf, httpx

## User Context
- Licensed real estate agent in LA/Beverly Hills (not just a TC)
- Uses SkySlope, DocuSign, Google Drive
- Wants email aliases and push notifications
- Has Google Workspace access
- Needs to verify everything they're legally liable for (20 gates)
- Jurisdiction: CA + LA County + BH (BH does NOT inherit City of LA rules)
