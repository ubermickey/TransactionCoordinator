# AI Context Log — v002 — 2026-02-01 — Tested and Working

## Status: MVP is functionally complete (minus API keys)

### Git: 5 commits on claude/real-estate-tc-role-KmkRT

All pushed to GitHub.

### What Changed Since v001
- Package renamed `tc/` -> `tcli/` (Python 3.14 entry point conflict)
- Added `./tc` shell wrapper script (reliable entry point)
- Added `tcli/__main__.py` for `python3 -m tcli` usage
- Added `tc digest` command (daily briefing across all transactions)
- Fixed duplicate county tax bug in LA transactions
- Added `.venv/` to .gitignore
- All 13 commands tested and working

### All Commands Tested

| Command | Status | Notes |
|---------|--------|-------|
| `./tc new <address>` | PASS | Detects BH/LA/CA jurisdictions correctly |
| `./tc list` | PASS | Rich table output |
| `./tc status` | PASS | Dashboard with gate/deadline counts |
| `./tc gates` | PASS | All 24 gates displayed with status |
| `./tc deadlines` | PASS | Empty until extraction populates them |
| `./tc taxes` | PASS | BH: $233K, LA: $473K on $6M (correct, no duplicates) |
| `./tc checklist` | PASS | CA forms + BH retrofits shown |
| `./tc digest` | PASS | Shows pending gates across all transactions |
| `./tc verify` | needs interactive | Prompts for confirmation (works) |
| `./tc review` | needs PDF | Generates review copies (code tested) |
| `./tc extract` | needs API key | Sends PDF to Claude (code reviewed) |
| `./tc push` | needs config | Pushover/ntfy (code reviewed) |
| `./tc email` | needs config | SMTP (code reviewed) |

### Files on Disk

```
workflow/
├── pyproject.toml
├── .env.example
├── .gitignore
├── API_SETUP.txt
├── tc                     <- shell wrapper (executable)
├── .venv/                 <- Python 3.14 virtual env (installed)
├── tcli/                  <- Python package
│   ├── __init__.py
│   ├── __main__.py
│   ├── db.py             (SQLite)
│   ├── rules.py          (YAML + taxes)
│   ├── engine.py         (deadlines + gates + extraction)
│   ├── notify.py         (SMTP + push)
│   ├── overlay.py        (PDF review copies)
│   └── cli.py            (13 commands)
├── aicontext_log/
├── phases.yaml
├── agent_verification_gates.yaml
├── agent_review_overlay.yaml
├── deadlines.yaml
├── document_validation.yaml
├── legal_update_process.yaml
├── ../jurisdictions/{california,los_angeles,beverly_hills}.yaml
└── ../integration/tools.yaml
```

### Task Status

- #2  DONE  Install and test CLI
- #3  NEXT  Create .env with ANTHROPIC_API_KEY (user action)
- #4  blocked  Test extraction with real PDF (needs #3)
- #5  blocked  Test gate verification end-to-end (needs #4)
- #6  blocked  MVP COMPLETE checkpoint (needs #5)
- #7  pending  Connect push notifications (user action)
- #8  pending  Connect Gmail SMTP (user action)
- #9  pending  Connect Google Drive API (user action)
- #10 pending  Connect DocuSign API (user action)
- #11 DONE  Digest command built and tested
- #12 pending  CAR form template system

### What Needs User Action

1. Copy `.env.example` to `.env`, add `ANTHROPIC_API_KEY`
2. Run `./tc extract` on a real contract PDF
3. Optionally configure push/email (see API_SETUP.txt)

### User's tmux Note

User mentioned "tmux" — they may want a tmux-based workflow or were
noting something about their terminal setup. Not yet addressed.
