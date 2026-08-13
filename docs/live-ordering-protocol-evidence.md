# Reviewed production final-checkout protocol evidence

**Verdict:** `productionFinalCheckoutSupported: true` under the explicit guarded, one-POST/manual-reconciliation policy in this document.

This is a dated first-party static-evidence record, not a claim that Glovo provides a public integration API or provider-enforced idempotency. No authenticated request and no basket, quote, status, checkout, order, completion, cancellation, or payment call was made while collecting or applying this evidence.

## Frozen evidence (2026-08-13)

| Area | Reviewed first-party static evidence | Adapter policy |
| --- | --- | --- |
| Final submission | Exactly `POST /v3/checkouts/order/1` for non-courier checkout. | One dispatch only. Never automatically retry, refresh-and-replay, or fall back. |
| Exact request projection | `{ "checkout": { "orderDetails": <exact quoted orderDetails including versionId, basketId and checkoutSessionId>, "components": <exact confirmed template components>, "analytics": { "templateReceived": <quoted value> } } }` | Preserve the server template projection exactly and bind it to the fresh authoritative quote. Do not locally reconstruct it. |
| Response states | `AUTH_REQUIRED`, `NO_AUTH_PENDING`, `COMPLETED`, `FAILED`, `CANCELLED`; actions include `AUTH`, `NO_AUTH`, `NO_AUTH_POLL`, `PROCESS_PAYMENT`, or null; post-auth action includes `NONE`, `COMPLETE_CHECKOUT`, or null. | Only schema-valid terminal `COMPLETED` with exact basket, amount, and currency is success. Schema-valid `FAILED`/`CANCELLED` without contradictory order/payment evidence is failure. Everything else is manual. |
| Status readback | Exactly `GET /v3/checkouts/order/{checkoutId}` with the same response schema. `checkoutId` is learned only from the final response. | One GET per explicit administrator action for the exact privately stored ID. No polling. No ID means zero GETs and manual provider-app inspection. |
| Identifier semantics | `checkoutSessionId`, returned `checkoutId`, and completed `order.id` are distinct. | Never expose provider IDs publicly. Recovery projections expose only `hasCheckoutId`. |
| Saved-card support | Saved-card selection is evidenced for the headless path. Interactive auth, redirect, native/web wallet, 3DS, split-payment, and token continuation paths exist. | Saved card only. Any auth/payment action or continuation requirement remains manual and blocks further checkout. |
| Completion | First-party web code contains `PUT /v3/checkouts/order/{checkoutId}/complete`, with payload varying by payment result. | This adapter does not issue completion. Never call it speculatively. |
| Cancellation/payment mutation | Web assets contain payment cancellation and redirect/payment mutations. | This adapter does not issue checkout completion, cancellation, payment cancellation, capture, redirect, or other payment mutation. |
| Retry behavior | The browser's generic fetcher can retry and can refresh on 401. | That behavior is deliberately not copied. Final POST has no automatic retry or reactive refresh/replay. Status is an explicit single GET only. |
| Idempotency | No final-checkout-linked `Idempotency-Key` was established. `Glovo-Request-Id` is generic correlation only. | Do not claim provider idempotency. Lost final response before learning `checkoutId` is unreconcilable in the adapter and remains manual. |
| Lookup by session | No lookup by `checkoutSessionId`, request ID, idempotency key, or client attempt was found. | Never resubmit to discover the outcome. Require manual account/payment inspection. |

## Terminal classification

A response is provider-confirmed success only when it is schema-valid `COMPLETED`, contains an order, and its basket ID, integer amount in minor units, and ISO currency exactly match the durable attempt authority. A response is provider-confirmed failure only when it is schema-valid `FAILED` or `CANCELLED` with no contradictory order or positive payment evidence.

Pending, authentication, `PROCESS_PAYMENT`, malformed data, transport failure, cancellation, unknown states, identifier mismatch, basket/amount/currency mismatch, and persistence uncertainty all remain `MANUAL_CHECK_REQUIRED`. A learned checkout ID may be retained privately for explicit status inspection. If paired journal/safety persistence cannot be made coherent, the integration fails closed with an integrity fault.

## Safety consequence

Production adapter support is limited to this policy:

1. two fresh, default-off administrator controls (preparation consent and separate paid-checkout consent), each explicitly acknowledged;
2. a fresh authoritative quote, exact store/items/options/full admin-only ephemeral address/masked saved card/price lines/total/currency/ETA/expiry display, and amount-bound acknowledgement;
3. durable prepared/dispatching authority before exactly one final POST;
4. no final POST retry, replay, fallback, speculative completion, cancellation, or payment mutation;
5. terminal evidence persisted, or all ambiguity latched for manual reconciliation;
6. at most one explicit known-ID status GET per administrator action, with no polling;
7. provider identifiers and the full address excluded from public/recovery projections and durable privacy-safe summaries.

This verdict does **not** establish provider idempotency. It says the adapter can be enabled only because it never retries an ambiguous paid POST and blocks every later checkout until the outcome is manually or provider-status reconciled.

## Principal static sources

First-party web assets fetched anonymously on 2026-08-13:

- checkout API/schema chunk `9332-3c136e6442e27d4b.js`, SHA-256 `e5ca6c219d41098b2945e2a4860be115ed41db99633624bb45684f313ffaeac3`;
- checkout orchestration page `page-a0c178e019539f90.js`, SHA-256 `17da38f448aab8f5ffcebeb06a3c5d2b97e88f104a7b19b9f693d99b44f8ae6b`;
- payment API chunk `32770-f90fab42b4d88724.js`, SHA-256 `d096962c73c42205d33d606c457844dc035a240732204408fe1d7a1182d3f786`;
- generic authenticated/retrying fetcher `26219-8c5c0fadda959f4a.js`, SHA-256 `07a22040d5fa6ee44c4ae9cce61ed2722fa2131b595c1146645e892d2fd74367`;
- generic request-header client `91816-c3afd29330038219.js`, SHA-256 `33386b6e6c56cb5d2da4047e4fb42d7401407f6db1f3e2ccf2e423bd982b27ae`.

Current Android artifact `com.glovo` version `2026.33.0` independently contained the checkout/payment endpoint families but did not prove final-checkout idempotency. XAPK SHA-256: `68cbcebbfee9b58488e510a24577ae99867a6a5d76ac1060a106a9c13384bf12`; base APK SHA-256: `dc846eb440d6cfb418d42ade5724252fdccc7c22dbbeffc1dd9186077bb49cc5`.

## Remaining limitations

- Glovo's customer API is private and may change without notice.
- Provider-enforced idempotency and duplicate-suppression scope/retention are unproven.
- There is no evidenced lookup when the POST response is lost before `checkoutId` is learned.
- Interactive authentication, redirects, wallets, 3DS, split payment, completion, and cancellation remain unsupported.
- The complete HTTP error classification and market/account-specific capability matrix remain unknown; ambiguity is therefore manual, never inferred as success or safe-to-retry.
