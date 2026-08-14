# Guarded live paid-checkout release checklist

**No deployment or provider call is performed by this checklist.** Home.9 composes the reviewed strict final adapter/request factory only behind all four literal default-off gates and healthy durable authority. Composition is not payment authorization; provider idempotency is not claimed.

## Repository and release identity

- [ ] Work from a clean tracked candidate; run the explicit staged archive suite and `python scripts/verify_clean_checkout.py` after commit.
- [ ] Manifest is `1.1.0+home.9`; trust ID matches the documented canonical descriptor.
- [ ] Inspect staged names, stat, and full diff; all intended tests are tracked and no private/credential material is present.
- [ ] Run focused paid/status/cancellation tests twice and the full suite twice after the last edit.
- [ ] Run Ruff, compileall, Node syntax, frontend harness, privacy scanner, every release/security verifier, JSON parsing/key parity, and `git diff --check`.

## Configuration and capability

- [ ] Fresh options expose separate preparation and paid controls with separate acknowledgements; paid consent implies preparation consent.
- [ ] Migration minor version 5 and reauthentication reset all four controls to literal false.
- [ ] Capability is true only with every gate, adapter/factory, durable authority, and facade healthy; it becomes false on unload, options change, reauth, unresolved state, or integrity fault.
- [ ] Preparation-only/default gates construct no final adapter and omit `live/execute_checkout`; all gates plus healthy composition expose it only to authenticated administrators.
- [ ] No public service/entity/event/automation/intent/webhook/MQTT purchase seam exists; only the admin WebSocket workspace is available.

## Exact checkout and frontend contract

- [ ] Saved-card-only payment; authoritative fresh template/quote projection preserved exactly.
- [ ] Confirmation shows store, items/quantities/options, admin-only ephemeral full address, masked card, price lines, exact total/currency, ETA, and expiry.
- [ ] Currency formatting covers configured 0/2/3 exponents (including JPY, AMD/EUR, KWD); unsupported currencies fail display closed.
- [ ] Acknowledgement is bound to exact minor units and currency; submit is challenge-bound, single-flight, and disabled while capability is unresolved.

## Dispatch and reconciliation safety

- [ ] Unresolved preparation authority exposes only the admin state and local-attestation recovery controls; normal mutation and final-submit controls remain absent.
- [ ] Preparation attestation is challenge/revision/state/generation/administrator bound, short-lived, one-shot, privacy-safe, makes no provider request, and never retries the original write.
- [ ] Terminal operator resolution persists authority history, refreshes the exact paired binding, then atomically clears it while advancing generation; simultaneous manual recovery remains visible and independently blocked.
- [ ] A preparatory post-dispatch exception becomes terminal provider failure only with `category=provider_rejection` and exact status in `{400,401,403,404,405,406,409,410,415,422,429}`; all other exceptions durably require reconciliation.
- [ ] Class names, missing/unallowlisted status, 5xx, transport/cancellation, and malformed/mismatched success cannot bypass the preparation reconciliation latch; outcome-persistence failure remains an integrity fault.
- [ ] Exactly one `POST /v3/checkouts/order/1`; no automatic retry, token refresh/replay, fallback, or duplicate dispatch.
- [ ] No `/complete`, checkout/payment cancellation, redirect, capture, wallet, or payment mutation is implemented.
- [ ] Durable prepared/reserved and dispatching authority precedes network invocation; all literal gates, generation, freshness, and fingerprints are rechecked.
- [ ] Terminal `COMPLETED` requires exact basket, amount, and currency; `FAILED`/`CANCELLED` requires non-contradictory terminal evidence.
- [ ] Pending/auth/`PROCESS_PAYMENT`, malformed, transport/cancellation, mismatch, and persistence uncertainty remain manual.
- [ ] No learned checkout ID means zero status GETs. A learned ID means exactly one GET per explicit admin action and no polling.
- [ ] Provider terminal reconciliation persists journal and safety state coherently; paired persistence faults retain manual state or enter integrity fault.
- [ ] Public/recovery projections expose only `hasCheckoutId`, never provider IDs; full address remains ephemeral and absent from journals/logs/recovery.

## Protocol and operational review

- [ ] Reconfirm the dated static source hashes in [protocol evidence](live-ordering-protocol-evidence.md).
- [ ] Explicitly acknowledge that provider idempotency and lookup by `checkoutSessionId` remain unproven.
- [ ] Verify the [operator runbook](live-ordering-operator-runbook.md) no-payment canary, one-payment authorization, stop criteria, status action, and rollback-preserves-uncertainty procedure.
- [ ] Any changed source asset, contract, route, schema, action, or continuation requirement blocks release until reviewed.

## Future controlled validation (not authorized by this home.9 build task)

- [ ] Do not deploy, run a live canary, mutate a basket, request a quote, submit checkout, or attempt payment as part of this offline composition task.
- [ ] A named operator must separately authorize one exact displayed purchase in a future supervised session.
- [ ] Any future ambiguity must stop without retry, completion/cancel/payment mutation, while preserving durable state for provider-app reconciliation.
