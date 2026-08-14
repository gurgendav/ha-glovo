# Guarded live paid-checkout release checklist

**No deployment or provider call is performed by this checklist.** Home.10 is an offline release candidate at `1.1.0+home.10`; it does not claim a deployment, live canary, quote, basket adoption, checkout, payment, or provider idempotency.

## Repository and release identity

- [ ] Work from a clean tracked candidate and run `python scripts/verify_clean_checkout.py` after commit.
- [ ] Manifest is exactly `1.1.0+home.10`.
- [ ] Canonical descriptor is exactly `ha-glovo|upstream=0142e44c091f3ff594fe627499070d515598ac5a|version=1.1.0+home.10|profile=coordinator-source-provenance-v1`.
- [ ] Independently hash the descriptor and obtain SHA-256 `452d7aaf4c2f618154d1ffc8e52661553b4b0365d2eb46fd5fad19b7b6390268`; the runtime trust ID embeds the same version and digest.
- [ ] `ORDERING_WEB_VERSION` remains `v1.2569.0`, the latest reproducibly preserved authenticated first-party evidence. Public landing and marketplace bundles did not expose a newer authenticated basket constant, so no newer version is claimed.
- [ ] Inspect staged names, stat, and full diff; only intended release, migration, documentation, scanner, and test paths are present.
- [ ] Run focused migration/foundation/release/privacy tests, full pytest, Ruff, compileall, Node syntax and frontend harness, privacy scanner, clean-checkout verifier, and `git diff --check`.

## Migration and fresh consent

- [ ] Config-entry minor version is 6 in both integration and config flow.
- [ ] Migration from every prior minor—including installed historical Home.8/Home.9 no-go candidates with all four gates true—forces `allow_ordering`, ordering acknowledgement, `allow_live_checkout`, and checkout acknowledgement to literal false while preserving unrelated options.
- [ ] A current v6 entry with four valid booleans may preserve only the normalized dependency-consistent gate state; missing or malformed gates fail closed.
- [ ] Reauthentication and new setup keep all four gates false.
- [ ] After migration, require fresh re-opt-in through the options flow. Never infer consent from historical values.

## Durable account-scoped basket authority

- [ ] Basket authority is account-scoped, not administrator-scoped, and whole operations are cross-administrator serialized.
- [ ] The runtime exposes the closed knowledge states `UNKNOWN`, `ABSENT_VERIFIED`, `ADOPTED`, and `CONFLICT`; `UNKNOWN` is never projected as an empty writable basket.
- [ ] Discovery is read-only and bounded: exactly one collection GET and at most one conditional full per-store GET, using only the reviewed routes, with no query, polling, fallback, retry, or mutation.
- [ ] The private Home Assistant Store uses its dedicated v1 key and persists hash-only account/store/intent/snapshot evidence plus generation, state, and observation time. It persists no provider IDs, products, addresses, handles, payloads, credentials, or raw values.
- [ ] Loaded durable evidence is not write authority. Every setup/reload starts runtime knowledge at `UNKNOWN` and requires fresh provider re-adoption using fresh account, address, store, menu, and product context.
- [ ] Require an explicit read-only adoption action before any basket mutation. Only fresh `ABSENT_VERIFIED` may permit one explicit create; exact `ADOPTED` may permit continuation; mismatch, multiple, malformed, oversized, inconsistent, failed reads, or `CONFLICT` block.
- [ ] Deterministic rejected replace/delete never degrades into create. Any ambiguous mutation is durably blocked and never retried.
- [ ] A second administrator cannot create independent basket authority or bypass an in-flight/account recovery block.

## Quote, confirmation, and paid checkout

- [ ] Basket adoption/mutation, paid quote creation, exact confirmation, and final checkout remain separate administrator actions.
- [ ] Read-only adoption never requests a quote and never mutates a basket.
- [ ] Paid submission requires a fresh authoritative exact quote and separate amount/currency-bound confirmation showing exact store, items/quantities/options, admin-only ephemeral full address, masked saved card, provider price lines, total/currency, ETA, and expiry.
- [ ] Preparation-only/default gates construct no final adapter and omit `live/execute_checkout`; all four gates plus healthy durable composition are still required.
- [ ] Exactly one final `POST /v3/checkouts/order/1`; no automatic retry, token refresh/replay, fallback, completion, cancellation, redirect, capture, wallet, or other payment mutation.
- [ ] Pending/auth/`PROCESS_PAYMENT`, malformed, transport/cancellation, mismatch, and persistence uncertainty remain manual. A known private checkout ID allows at most one explicit status GET per administrator action and no polling.
- [ ] Public/recovery schemas and logs expose no provider IDs or raw persisted values. Full addresses and low-entropy private fingerprints are not public or logged.

## Protocol and operational review

- [ ] Reconfirm the dated static source hashes and exact collection/per-store routes in [protocol evidence](live-ordering-protocol-evidence.md).
- [ ] Explicitly acknowledge no provider idempotency or lookup by `checkoutSessionId` is established.
- [ ] Follow the [operator runbook](live-ordering-operator-runbook.md): fresh re-opt-in, read-only adoption before mutation, separate exact quote confirmation, stop criteria, and rollback that preserves uncertainty.
- [ ] Any changed route, schema, source asset, action, continuation requirement, privacy projection, or persistence image blocks release until reviewed.

## Future controlled validation (not authorized by this Home.10 build task)

- [ ] Do not deploy, run a live canary, adopt a live basket, mutate a basket, request a quote, submit checkout, or attempt payment as part of this offline release task.
- [ ] A named operator must separately authorize each future consequential step and one exact displayed purchase.
- [ ] Any future ambiguity stops without retry while preserving durable state for provider-app reconciliation.
