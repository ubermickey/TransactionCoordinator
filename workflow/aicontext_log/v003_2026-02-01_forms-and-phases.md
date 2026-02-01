# AI Context Log — v003 — 2026-02-01 — Forms and Phase Advancement

## Git: 7 commits on claude/real-estate-tc-role-KmkRT, all pushed

## What's New Since v002
- CAR form templates: RPA (19 fields), TDS (10 fields), CR-1 (5 fields)
- Phase advancement with HARD_GATE blocking
- form-diff tool for reviewing form updates
- Extract now accepts --form flag for template-guided extraction
- Package is tcli/, entry point is ./tc shell wrapper
- 16 commands, 627 lines of Python

## All 16 Commands

| Command | Status | Needs API? |
|---------|--------|------------|
| ./tc new | PASS | No |
| ./tc list | PASS | No |
| ./tc status | PASS | No |
| ./tc gates | PASS | No |
| ./tc deadlines | PASS | No |
| ./tc taxes | PASS | No |
| ./tc checklist | PASS | No |
| ./tc digest | PASS | No |
| ./tc advance | PASS | No |
| ./tc forms | PASS | No |
| ./tc form-diff | PASS | No |
| ./tc verify | PASS (interactive) | No |
| ./tc review | code OK | No (needs PDF) |
| ./tc extract | code OK | ANTHROPIC_API_KEY |
| ./tc push | code OK | PUSHOVER/NTFY |
| ./tc email | code OK | SMTP creds |

## Task Completion

| # | Task | Status |
|---|------|--------|
| 2 | Install and test CLI | DONE |
| 11 | Digest command | DONE |
| 12 | CAR form templates | DONE |
| 3 | .env + API key | Needs user |
| 4 | Test real extraction | Needs #3 |
| 5 | Test gate workflow | Needs #4 |
| 6 | MVP checkpoint | Needs #5 |
| 7-10 | API connections | Needs user creds |

## What Can Still Be Built Without APIs
- More form templates (SPQ, AD, SBSA, NHD)
- Tmux layout script
- Shell completion setup
- README with usage examples
