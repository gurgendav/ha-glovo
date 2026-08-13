# Reviewed production final-checkout protocol evidence

**Home.8 verdict:** `productionFinalCheckoutSupported: true` only under the explicit guarded one-POST/manual-reconciliation policy below. Production constructs the strict request factory and adapter only when fresh literal preparation and paid-checkout gate pairs are all true and durable preparation authority is healthy. Defaults, migration, and reauthentication keep all gates false.

This is a dated first-party static-evidence record, not a claim that Glovo provides a public integration API or provider-enforced idempotency. No authenticated request and no basket, quote, status, checkout, order, completion, cancellation, or payment call was made while collecting or applying this evidence.

## Withdrawn home.5 canary (2026-08-13)

The audited preparation-only canary on home.5 stopped after exactly one basket-create mutation. It made zero retries and zero quote-template, final-checkout, checkout-status, payment, or order calls. The sanitized seam reported the provider status as unavailable. Offline review found that an unknown post-dispatch exception could be falsely persisted as terminal provider failure instead of durably requiring reconciliation, so home.5 was withdrawn. All four ordering gates were restored to false and production was rolled back to home.2. This public-safe record intentionally excludes credentials, account/store/basket identifiers, addresses, coordinates, payloads, provider response text, and private diagnostics.

## Withdrawn home.4 canary (2026-08-13)

The corrected optionless canary on home.4 sent exactly one basket-create mutation and received a deterministic provider failure. It made zero retries and zero quote-template, final-checkout, payment, checkout-status, order-status, or other order calls; zero active orders remained. No manual-check or integrity latch was created. All four ordering gates were restored to false and production was rolled back to home.2. This public-safe record intentionally excludes credentials, account/store/basket identifiers, addresses, coordinates, payloads, provider response text, and private diagnostics.

## Pinned common web fetcher evidence (2026-08-13)

The route HTML `001-rena-restaurant-complex.html` (SHA-256 `899301f3c06619ac2b37af09be4c83337878c2b8fd13c839f75dd0b3aab4ec11`) pins web app version `v1.2567.1`, API version `14`, and request TTL `7500`. The basket SDK chunk `009-86937-d352ebf5782090c3.js` (SHA-256 `5830b8a8e5a98a9de1233eb165bc771822970d2665cc088c4ccf2a63006af0cc`, module `52500`) sends basket POST/PUT/PATCH/DELETE through the global `Z.GO.request` fetcher and adds no basket-specific headers or query parameters.

Generic defaults are evidenced in `0006-91816-c3afd29330038219.js` (SHA-256 `33386b6e6c56cb5d2da4047e4fb42d7401407f6db1f3e2ccf2e423bd982b27ae`, module `49035`) and the common interceptor in `0009-26219-8c5c0fadda959f4a.js` (SHA-256 `07a22040d5fa6ee44c4ae9cce61ed2722fa2131b595c1146645e892d2fd74367`, module `30467`). Location overrides are published by `0022-66085-c7abf5e065d94713.js` (SHA-256 `ef964026292449d55ba2464708a122ef8ff4b06dd1e4bc03edd69f699d9a4058`, module `66085`). The shared request context includes Accept, app platform/type/version/development state, TTL, client info, API version, app context, separate process-private Perseus client/session IDs and timestamp/consent, dynamic session equal to the Perseus session, language, device, country/city, delivery coordinates/timestamp/accuracy, and Authorization on authenticated calls. Object-body mutations use JSON content type; bodyless DELETE does not force it. Request ID and delivery timestamp are fresh on every invocation.

## Frozen basket evidence (2026-08-13)

The withdrawn home.3 stop-before-submit canary reached basket creation but received a deterministic provider rejection. No checkout or payment was dispatched. Anonymous first-party static recovery pinned the replacement contract to:

- route `https://glovoapp.com/en/am/yerevan/stores/rena-restaurant-complex`, fetched `2026-08-13T11:03:05Z`, SHA-256 `899301f3c06619ac2b37af09be4c83337878c2b8fd13c839f75dd0b3aab4ec11`;
- basket chunk `https://glovoapp.com/_next/static/chunks/86937-d352ebf5782090c3.js`, fetched `2026-08-13T11:03:08Z`, SHA-256 `5830b8a8e5a98a9de1233eb165bc771822970d2665cc088c4ccf2a63006af0cc`;
- API/client module `52500` (module offset `3866`, `addProduct` offset `12483`, authenticated create offset `20830`);
- response validators module `80621` (module offset `36412`, basket schema around `38546–39513`).

The evidenced create root remains `{products, storeId, storeAddressId, storeCategoryId, handlingStrategy}`. Each product uses nested `ids`, structured `quantity`, and, when selected, nested customizations with provider group/option identities and structured quantities. Optionless quick-add products omit `customizations`; optional customization groups are omitted rather than fabricated. No basket-specific headers are evidenced beyond the generic authenticated web client.

The common basket response requires basket/customer/store identities, `products`, `basketPrice`, typed nullable `mbs`, nullable `isPrimeSubscriptionSimulated`, and boolean `usingDhBasket`; `productSuggestions` and `cityCode` are optional. The first-party validator declares basket products as `productSchema.partial()`. The adapter therefore requires the nested product IDs and quantity needed to prove exact selection, requires selected customizations to match exactly, and strictly validates and preserves every optional current product field when it appears. Sponsored suggestions retain the full rich product schema.

Replacement follows the first-party clone-current-basket/change-products PUT behavior. Quantity PATCH is exactly `{handlingStrategy, basketVersion, products:[{basketProductId, quantity}]}`. Whole-basket DELETE is bodyless. A post-dispatch exception is terminal provider rejection only when it carries `category=provider_rejection` and an exact allowlisted status in `{400,401,403,404,405,406,409,410,415,422,429}`. Missing or unapproved status, transport, 5xx, cancellation, malformed/mismatched successful responses, and every other unknown outcome remain ambiguous, durably latch reconciliation, and are never replayed. Exception class names alone are not evidence.

## Frozen final-checkout evidence (2026-08-13)

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

Any future release that composes the reviewed production adapter is limited to this policy:

1. two fresh, default-off administrator controls (preparation consent and separate paid-checkout consent), each explicitly acknowledged;
2. a fresh authoritative quote, exact store/items/options/full admin-only ephemeral address/masked saved card/price lines/total/currency/ETA/expiry display, and amount-bound acknowledgement;
3. durable prepared/dispatching authority before exactly one final POST;
4. no final POST retry, replay, fallback, speculative completion, cancellation, or payment mutation;
5. terminal evidence persisted, or all ambiguity latched for manual reconciliation;
6. at most one explicit known-ID status GET per administrator action, with no polling;
7. provider identifiers and the full address excluded from public/recovery projections and durable privacy-safe summaries.

This evidence does **not** establish provider idempotency or authorize a payment. Home.8 only composes the existing strict adapter behind default-off gates; it preserves the no-retry policy and blocks every later checkout until an ambiguous outcome is manually or provider-status reconciled.

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
