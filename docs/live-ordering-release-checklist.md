# Live-ordering release checklist

**No deployment is performed by this checklist.** At the time of writing,
production final checkout is blocked by `productionFinalCheckoutSupported: false`.

## Repository and compatibility gates

- [ ] Work from a clean, tracked checkout; run
  `python scripts/verify_clean_checkout.py` successfully.
- [ ] Run Ruff, Python compile/AST checks, JSON validation, available JavaScript
  syntax checks, `git diff --check`, privacy/capability scan, and mutation
  allowlist parity.
- [ ] Run the full offline test suite and the release/security tests from the
  archive checkout.
- [ ] Verify `manifest.json` does not claim an unevidenced Home Assistant
  minimum. The compatibility CI matrix exercises HA `2024.12` and the current
  published HA on the selected supported Python without secrets.
- [ ] Note the limitation honestly: the integration declares no third-party
  runtime requirement pin, and CI's `current` resolution is time-dependent.
  The offline stubs and import/compile checks are compatibility signals, not a
  replacement for a supported live Home Assistant deployment test.
- [ ] Verify `strings.json` and every tracked translation have equivalent key
  structure (translation parity); do not claim translated ordering wording not
  present in the artifact.

## Security, protocol, and operations gates

- [ ] Confirm `scripts/scan_ordering_privacy.py` reports no public DTO fixture,
  logger/message/diagnostic, or frontend leak, and no final-purchase
  service/entity/event/automation/intent/webhook/MQTT seam.
- [ ] Reconfirm the exact public asset SHA/date record in
  [protocol evidence](live-ordering-protocol-evidence.md). If any required
  method, request JSON, response/status, completion, redirect, retry, or token
  refresh behavior remains missing, keep final checkout disabled.
- [ ] Verify two default-off gates, saved-card-only selection, authoritative
  server quote, exact amount confirmation, one-attempt/no-retry behavior, and
  privacy/rollback rules against the operator runbook.
- [ ] Rollback test: disabling capability removes access while preserving the
  journal and every unresolved/ambiguous state for manual reconciliation.

## Controlled validation sequence (only after every gate is green)

- [ ] Run the **no-payment canary** with exact SHA, clean journal, and
  provider-app conflict check; stop before final confirmation and disable both
  gates afterward.
- [ ] Recheck the provider application and journal. Any mismatch, stale quote,
  unexpected remote mutation, raw sensitive data, or unresolved state aborts
  release.
- [ ] A **one real-payment attempt** is permitted only after all above gates and
  a complete production protocol verdict. Verify exact SHA, clean journal,
  app-conflict absence, saved card, server quote, exact amount confirmation,
  and named manual reconciler immediately before it.
- [ ] On timeout, cancellation, malformed/unknown response, redirect mismatch,
  or any uncertainty: do not retry; manually reconcile and preserve the
  journal. No deployment/release proceeds on ambiguity.
