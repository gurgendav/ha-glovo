# Guarded live paid-checkout release checklist

**No deployment or provider call is performed by this checklist.** Home.26 is an offline release candidate at `1.1.0+home.26`; it does not claim a deployment, live canary, quote, checkout, payment, or provider idempotency.

## Repository and release identity

- [ ] Work from a clean tracked candidate and run `python scripts/verify_clean_checkout.py` after commit.
- [ ] Manifest is exactly `1.1.0+home.26`.
- [ ] Canonical descriptor is exactly `ha-glovo|upstream=0142e44c091f3ff594fe627499070d515598ac5a|version=1.1.0+home.26|profile=coordinator-source-provenance-v1`.
- [ ] Independently hash the descriptor and obtain SHA-256 `c711950a2891a32d2f1a1c790364e39b7b937a11bc2642f3c4e865af421e2541`; the runtime trust ID embeds the same version and digest.
- [ ] `ORDERING_WEB_VERSION` is `v1.2580.2`; the shared builder pins `Glovo-App-Version`, matching `Glovo-Client-Info`, `Referer`, Chrome 151/macOS `User-Agent`, `sec-ch-ua-platform`, `sec-ch-ua`, and `sec-ch-ua-mobile`, with no unevidenced `Origin`.
- [ ] Inspect staged names, stat, and full diff; only intended release, migration, documentation, scanner, and test paths are present.
- [ ] Run focused migration/foundation/release/privacy tests, full pytest, Ruff, compileall, Node syntax and frontend harness, privacy scanner, clean-checkout verifier, and `git diff --check`.

## Migration and fresh consent

- [ ] Config-entry minor version is 19 in both integration and config flow.
- [ ] Migration from every prior minor—including installed historical Home.8/Home.9 no-go candidates with all four gates true—forces `allow_ordering`, ordering acknowledgement, `allow_live_checkout`, and checkout acknowledgement to literal false while preserving unrelated options.
- [ ] Every prior entry, including a fully opted-in published Home.25 minor v18 entry with four valid true booleans, is reset to four literal false gates; missing or malformed gates also fail closed.
- [ ] Current Home.26 minor v19 entries preserve only dependency-consistent literal booleans; malformed or incomplete gates fail closed.
- [ ] Reauthentication and new setup keep all four gates false.
- [ ] After migration, require fresh re-opt-in through the options flow. Never infer consent from historical values.

## Durable account-scoped basket authority

- [ ] Basket authority is account-scoped, not administrator-scoped, and whole operations are cross-administrator serialized.
- [ ] The runtime exposes the closed knowledge states `UNKNOWN`, `ABSENT_VERIFIED`, `ADOPTED`, and `CONFLICT`; `UNKNOWN` is never projected as an empty writable basket.
- [ ] Discovery is read-only and bounded: exactly one collection GET and at most one conditional full per-store GET, using only the reviewed routes, with no query, polling, fallback, retry, or mutation.
- [ ] Rich-basket `storeInfo.logo` accepts only `null` or the existing bounded string, is discarded from authority evidence, and rejects objects, arrays, booleans, numbers, and oversized strings at exact path `root.storeInfo.logo`.
- [ ] The private Home Assistant Store uses its dedicated v1 key and persists hash-only account/store/intent/snapshot evidence plus generation, state, and observation time. It persists no provider IDs, products, addresses, handles, payloads, credentials, or raw values.
- [ ] Loaded durable evidence is not write authority. Every setup/reload starts runtime knowledge at `UNKNOWN` and requires fresh provider re-adoption using fresh account, address, store, menu, and product context.
- [ ] Require an explicit read-only adoption action before any basket mutation. Only fresh `ABSENT_VERIFIED` may permit one explicit create; exact `ADOPTED` may permit continuation; mismatch, multiple, malformed, oversized, inconsistent, failed reads, or `CONFLICT` block.
- [ ] Deterministic rejected replace/delete never degrades into create. Any ambiguous mutation is durably blocked and never retried.
- [ ] A second administrator cannot create independent basket authority or bypass an in-flight/account recovery block.

## Quote, confirmation, and paid checkout

- [ ] Payment bootstrap and priced revalidation, basket collection/detail reads, basket mutations, quote-template creation, and final submission use the same location-aware authenticated common web headers; payment and basket reads never fall back to the basic transport.
- [ ] Process-private device/client/session identities are generated locally and distinct, dynamic session equals session, session timestamp is process-scoped, and request ID/delivery timestamp are fresh per request. Location comes only from validated authoritative context and none of these fields are caller supplied.
- [ ] Initial and post-template `GET /v4/payment_methods` queries both use the fixed browser tuple `clientSupports=GooglePay`, empty `clientReady=`, and `context=checkout`; only the priced request adds the reviewed amount/currency/session fields, and neither capability value is public caller input.
- [ ] The sanitized three-method/one-action regression reports raw/card/selected/selectable counts of `3/1/1/1`, selected-selectable count `1`, unsupported-type count `2`, and gives authority only to the selected usable card.
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

## Future controlled validation (not authorized by this Home.26 build task)

- [ ] Do not deploy, run a live canary, adopt a live basket, mutate a basket, request a quote, submit checkout, or attempt payment as part of this offline release task.
- [ ] A named operator must separately authorize each future consequential step and one exact displayed purchase.
- [ ] Any future ambiguity stops without retry while preserving durable state for provider-app reconciliation.
