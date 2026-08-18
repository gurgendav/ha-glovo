# Guarded live paid-checkout operator runbook

> **Home.26 release policy:** this offline candidate authorizes no deployment, provider call, basket adoption or mutation, quote, checkout, payment, or live test. It makes no provider idempotency claim. Home.8 and Home.9 remain historical unsafe/no-go candidates, not deployable identities.

## Non-negotiable operating model

1. **Fresh default-off consent after migration.** Config-entry migration v19 closes preparation consent/acknowledgement and paid-checkout consent/acknowledgement, even if all four were true in published Home.25. Preserve unrelated options, then require fresh re-opt-in. Reauthentication also closes all four gates.
2. **One account authority.** Basket authority is account-scoped and cross-administrator serialized. A second administrator shares the same `UNKNOWN`, `ABSENT_VERIFIED`, `ADOPTED`, or `CONFLICT` state and cannot create a parallel writable basket.
3. **Read before any write.** After setup or reload, runtime state is `UNKNOWN`. Reacquire fresh account/address/store/menu/product context and perform explicit read-only adoption before any basket mutation.
4. **Bounded discovery.** Adoption issues one collection GET plus at most one conditional full per-store GET. It does not poll, retry, fall back, request a quote, or mutate provider state.
5. **Closed outcomes.** `ABSENT_VERIFIED` may authorize one separately requested create. Exact `ADOPTED` binds the provider basket to fresh runtime handles. `CONFLICT`, malformed/multiple/mismatched/oversized/inconsistent responses, failed reads, and `UNKNOWN` block mutation.
6. **Hash-only durability.** The private Home Assistant Store keeps only domain-separated account/store/intent/snapshot hashes and closed metadata. Provider IDs, products, addresses, handles, payloads, credentials, and raw persisted values are forbidden. Loaded evidence requires fresh provider re-adoption and never directly restores write authority.
7. **No ambiguous retry.** Basket and final-checkout mutations get one attempt. A deterministic rejected replace/delete never becomes create; any ambiguous outcome is durably blocked and is never retried.
8. **Paid quote and confirmation are separate.** Basket adoption does not authorize a quote. A fresh authoritative exact quote does not authorize checkout until a separate amount/currency-bound confirmation is shown and accepted.
9. **One final POST.** Dispatch `POST /v3/checkouts/order/1` once. Never refresh-and-replay, fall back, compensate, complete, cancel, redirect, capture, or issue another payment mutation.
10. **Privacy and rollback.** Public/recovery projections and logs expose no provider IDs. Full addresses remain admin-only and ephemeral. Rollback closes access but preserves every unresolved attempt and integrity block.

## Preflight for any separately authorized future operation

- [ ] Artifact is exactly `1.1.0+home.26` with canonical trust digest `c711950a2891a32d2f1a1c790364e39b7b937a11bc2642f3c4e865af421e2541` and clean release gates.
- [ ] `ORDERING_WEB_VERSION` is `v1.2580.2`; the generic fetcher pins the sanitized Chrome 151/macOS identity headers, and adds neither `Origin` nor endpoint-specific fallback headers.
- [ ] Migration completed at minor v19 and all four gates are false.
- [ ] Saved-payment bootstrap and priced revalidation both pin `clientSupports=GooglePay`, empty `clientReady=`, and `context=checkout`; callers cannot override the capability tuple.
- [ ] Journal, preparation authority, ordering safety state, and basket evidence Store are coherent with no unresolved/manual/integrity fault.
- [ ] Named administrator performs fresh re-opt-in; paid consent remains separate from preparation consent.
- [ ] Provider app is available for independent inspection and shows no conflicting order/payment.
- [ ] No private data capture, debug payload logging, screenshots, or raw response retention is enabled.

## Read-only adoption before basket mutation

1. Start from `UNKNOWN` after every integration/Core setup or reload, even if hash-only `PRESENT_OBSERVED` evidence exists.
2. Select a fresh saved-address handle, reacquire the exact store and menu, and reconcile the intended products/options with current catalog handles.
3. Invoke **Adopt current basket** once. Under the account-wide/cross-administrator lock, it performs one collection GET and at most one per-store full GET.
4. If the result is `ABSENT_VERIFIED`, stop and review before separately invoking a create. If `ADOPTED`, inspect the sanitized basket projection before any replace/delete/quote action.
5. On `CONFLICT`, `UNKNOWN`, transport error, malformed/oversized/inconsistent response, multiple matching summaries, or context mismatch, stop. Do not mutate, retry, or treat the basket as empty.
6. Any reload invalidates ephemeral handles and the adopted lease. Repeat fresh provider re-adoption before a later mutation.

## Basket mutation stop rules

- Preparation can change remote state and is never described as dry-run or provider-idempotent.
- Exactly one explicit create/replace/delete attempt is allowed under account-wide serialization.
- After any ambiguous mutation, stop immediately, preserve durable reconciliation authority, inspect the provider app, and never retry.
- A deterministic rejected replace/delete retains prior knowledge or closes to `UNKNOWN`; it never selects a create path.
- Another administrator must observe the same block and may not bypass it with a different browser/session.

## Separate exact quote and confirmation

1. After a freshly adopted exact basket, request a quote as a separate action. Adoption itself must issue no quote call.
2. Confirm quote freshness and exact store, items/quantities/options, admin-only ephemeral full destination, masked saved card, provider price lines, total in minor units and ISO currency, ETA, and expiry.
3. Prepare a separate confirmation bound to the exact quote, amount, currency, generation, runtime epoch, and basket authority.
4. Any basket/context change, reload, expiry, mismatch, or stale generation invalidates quote and confirmation. Return to read-only adoption; do not reuse prior authority.
5. A future named operator may submit only after separately authorizing the exact displayed purchase.

## Final checkout and recovery

- Final submission is one `POST /v3/checkouts/order/1` with the exact quoted server projection.
- `COMPLETED` is accepted only with exact basket, amount, and currency. Non-contradictory terminal `FAILED`/`CANCELLED` is failure. Everything else is manual.
- Pending/auth/`PROCESS_PAYMENT`, timeout, cancellation, transport loss, malformed/unknown/contradictory data, mismatch, or persistence uncertainty becomes `MANUAL_CHECK_REQUIRED`; do not place another order.
- If and only if a private checkout ID was learned, each deliberate administrator status action may issue at most one exact GET. There is no polling and no public ID projection.
- Preparation recovery records only directly observed local evidence and performs no provider GET or replay. `still_unknown` preserves the block.
- Disable both consent groups after any separately authorized validation. Rollback never clears unresolved or integrity state.

## Current release boundary

Do not deploy, run a canary, perform live read-only adoption, mutate a basket, request a paid quote, confirm checkout, submit payment, or call provider status as part of Home.26 release preparation. Those steps require a separate explicit authorization and are not claimed by this artifact.
