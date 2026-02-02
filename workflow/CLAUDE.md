# Project: Transaction Coordinator

## User Preferences

- Make decisions autonomously. Do not ask clarifying questions — use your best judgment and proceed. The user prefers action over deliberation.
- When planning, answer your own open questions based on context and codebase patterns rather than prompting the user.
- Default to the simplest working approach. Ship it, then iterate.

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
