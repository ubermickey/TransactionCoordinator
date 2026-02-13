# App Intent

## Problem Statement

California residential real estate transactions involve 80+ compliance checkpoints, dozens of documents, strict contingency timelines, and coordination across 7+ parties. A single missed deadline or unverified gate can expose an agent to legal liability under DRE regulations.

Most agents track this with spreadsheets, email threads, and memory. Transaction coordinators (TCs) reduce errors but still rely on manual checklists that don't enforce sequencing or flag cascading risks.

This app replaces that manual overhead with an opinionated, phase-gated workflow that makes it structurally difficult to miss compliance steps.

## Target User

A high-volume California listing agent (or their TC) managing 5-15 concurrent transactions. They need:

- Confidence that no DRE-required step is skipped
- A single view of all deadlines, contingencies, and blocker gates across active deals
- Document tracking tied to CAR form requirements and brokerage-specific checklists
- Signature review with audit trail (not just "was it signed" but "did the agent verify it")
- Jurisdiction-aware compliance (state, county, city rules vary — LA vs Beverly Hills vs unincorporated)

The CLI serves agents who prefer terminal workflows or need scripted automation (daily digest cron, CI-style verification). The web UI is the primary interface for daily transaction management.

## Workflow Narrative

### 1. Create Transaction
Agent enters property address. System resolves jurisdictions (CA + city), initializes 80+ compliance gates, and creates default party placeholders. If a brokerage is specified, the document checklist auto-populates from brokerage-specific YAML configs.

### 2. Contract Execution
Agent uploads or extracts the RPA (Residential Purchase Agreement) via Claude PDF analysis. Key dates (acceptance, close of escrow) anchor all deadline calculations. Deadlines auto-populate with configurable day counts and reminder schedules.

### 3. Inspection & Contingencies
Investigation period opens. The inspection checklist auto-populates 11 base items plus conditional items based on property flags (pool, pre-1978, septic, HOA). Each contingency links to a compliance gate and deadline — removing a contingency auto-verifies the related gate.

### 4. Appraisal, Financing, Title/Escrow
Each phase has its own gates. Phase advancement is blocked until all HARD_GATE items in the current phase are verified. The agent reviews and signs off on each gate with legal citation context.

### 5. Pre-Closing & Closing
All contingencies resolved, final walk-through complete. Documents signed (tracked via signature review with DocuSign integration or manual entry). Funds disbursed, deed recorded.

### 6. Post-Closing
File archived, commissions tracked, post-closing items resolved. Full audit trail preserved.

## Efficacy Metrics

These are the signals that the app is working:

- **Blocker visibility**: Zero surprise missed deadlines. Every overdue item surfaces in the dashboard agenda and daily digest.
- **Gate verification rate**: Percentage of compliance gates verified before phase advancement (target: 100% for HARD_GATE, >90% for SOFT_GATE).
- **Cycle time awareness**: Days per phase tracked via audit log timestamps. Identifies bottlenecks (e.g., financing phase averaging 21 days vs 14-day target).
- **Document completion**: Required docs received/verified before closing (target: 100% received, >95% verified).
- **Overdue deadline count**: Active transactions with 0 overdue deadlines (target: >95% of portfolio).

## Non-Goals

- **Not a CRM.** No lead tracking, marketing automation, or client relationship management. Starts at "offer received."
- **Not an e-signature platform.** Integrates with DocuSign but doesn't replace it. Tracks signature status and provides review, not the signing ceremony itself.
- **Not multi-state.** Deeply California-specific (CAR forms, DRE regulations, county transfer taxes). The jurisdiction system could extend to other states, but it's not a priority.
- **Not multi-tenant SaaS.** Single-user SQLite database. No auth, no teams, no permissioning. This is a power tool for one agent or TC.
- **Not a document generator.** Reads and verifies documents, doesn't create them. PDF analysis extracts fields; it doesn't fill or generate new forms.
