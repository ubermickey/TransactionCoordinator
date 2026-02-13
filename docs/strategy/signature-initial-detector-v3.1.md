# Signature + Initial Detector v3.1

## Goal

Ship a production-grade detector for contract review that:

- Detects full signatures and initials as separate categories.
- Maximizes initials recall on CAR forms with footer initials slots.
- Reduces false positives from generic labels (`Buyer`, `Seller`, `By`) in non-sign contexts.
- Preserves compatibility with legacy signature review flows.

## Design

## Categories

- `entry_signature`
- `entry_initial`

## Core Rules

1. Label-first matching remains the primary method.
2. Footer structural filtering keeps initials footer slots and removes attribution lines only.
3. Generic signature labels require legal-sign context:
   - allowed only when nearby context includes signature/date markers.
   - blocked when context matches non-sign service-provider/option-table phrases.
4. Initials labels can claim multiple short lines (multi-slot detection).

## Scanner Rules

1. Wide fields use `ul_bbox` for fill detection.
2. Fill detection strips label tokens before classifying content as filled.
3. `entry_initial` is mandatory in review scoring and verify queue logic.

## Acceptance Gates (Strict+)

- Initials recall `>= 98%`
- Signature precision `>= 93%` (proxy benchmark)
- Zero critical regressions in:
  - Signatures tab
  - Verify Queue
  - Annotated PDF highlights
- Two consecutive full-green runs before final sign-off.

## Verification Artifacts

- Benchmark script: `/Users/mikeudem/Projects/TransactionCoordinator/scripts/benchmark_signature_detector.py`
- Benchmark report: `/Users/mikeudem/Projects/TransactionCoordinator/docs/strategy/signature-detector-benchmark-latest.md`
- Sandbox tests:
  - `tcli/sandbox/test_signature_detector.py`
  - `tcli/sandbox/test_contract_scanner.py`
