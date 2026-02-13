# Entry Detector + Cloud Governance Interfaces

## Cloud Approval + Tracking

This project uses transaction-scoped cloud governance for AI/cloud endpoints.

### Data Model

- `cloud_approvals`
  - `txn` (PK)
  - `granted_at`
  - `expires_at`
  - `granted_by`
  - `note`
  - `revoked_at`

- `cloud_events`
  - `txn`
  - `service`
  - `operation`
  - `endpoint`
  - `model`
  - `approved`
  - `outcome` (`blocked|success|error`)
  - `status_code`
  - `latency_ms`
  - `request_bytes`
  - `response_bytes`
  - `error`
  - `meta`
  - `created_at`

### API

- `POST /api/txns/<tid>/cloud-approval`
  - Body: `{ "minutes": 30, "note": "..." }`
  - Grants approval window for cloud operations.

- `GET /api/txns/<tid>/cloud-approval`
  - Returns current approval state with `active` and `remaining_seconds`.

- `DELETE /api/txns/<tid>/cloud-approval`
  - Revokes approval immediately.

- `GET /api/txns/<tid>/cloud-events?limit=100&service=&operation=&outcome=`
  - Returns cloud usage events in reverse chronological order.

### Error Contract

When blocked by approval policy, cloud endpoints return `403`:

```json
{
  "error": "cloud approval required",
  "code": "cloud_approval_required",
  "requires_approval": true,
  "txn": "abcd1234"
}
```

### Policy Defaults

- Cloud approval is required by default.
- Approval is transaction-scoped and defaults to 30 minutes.
- Cloud calls without `txn` context are blocked.

## Entry Detector Categories

The detector now emits a dedicated initials category:

- `entry_signature`: Full signature lines and signer-name signature blocks.
- `entry_initial`: Initials-only lines (buyer/seller/tenant/owner initials slots).

### Compatibility Rules

- Legacy signature filters (`signature_area`, `signature`, `entry_signature`) should include both
  signature and initials categories for review surfaces that historically grouped them.
- Direct filtering by `entry_initial` returns initials-only entries.

### Fill-Detection Contract

- `ul_bbox` is authoritative for wide signature/initial fields to avoid static label text.
- Fill detection must strip label tokens before deciding `is_filled`.
