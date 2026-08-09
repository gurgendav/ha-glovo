# Public static final-checkout protocol evidence

**Verdict:** `productionFinalCheckoutSupported: false`

This record is deliberately fail-closed. It covers a public, unauthenticated
static-asset inspection only: no cookies, browser storage, account, API call,
HAR, provider response, or live checkout was used. Full assets were processed
in memory only and are not retained in this repository.

## Frozen source observations

Retrieved **2026-08-09 UTC** with an anonymous GET and no cookies:

| Public source | SHA-256 | Sanitized observation |
| --- | --- | --- |
| `https://glovoapp.com/en` | `d0f5d9dde227726979d9de5603a106220421e87f1a191345fc228364bc8347da` | Public Next.js page; it references current static chunks. |
| `https://glovoapp.com/_next/static/chunks/app/layout-9570fd060b722e48.js` | `5f409203cee34c370a4922585b7b728e88fc73f87033f56fe2f5bbd143bbd777` | Tiny route semantic: `postCheckoutUrl` forms `/[language]/order-tracking/<orderId>`; another route is `/[language]/payments-redirect`. This is navigation metadata, not an API contract. |
| `https://glovoapp.com/_next/static/chunks/13604-6befd679450a8834.js` | `2be296cc849f54c477c894c989079dbe07d5f577182f89f2625655c8d0a1ac8b` | Tiny semantic: static analytics vocabulary includes `checkout-submit` and a `checkoutId` attribute name. It does not bind either to a request or response schema. |
| `https://glovoapp.com/_next/static/chunks/17625-e3830da54cd0d345.js` | `9a9ce4f6b7eed7d045464d1d60ef0195c405d36cc86f6aea940700090ddbe978` | Static feature/validation metadata contains generic authentication-token terminology. It contains no final-checkout operation binding. |

The page and chunk names/hashes are snapshot identifiers, not stability
promises. They must be re-fetched and independently reviewed before any future
production claim.

## Required contract, and what was **not** evidenced

No exact public static evidence tied together all of the following:

| Required before enabling production final checkout | Evidence result |
| --- | --- |
| exact final method and API path | **missing** |
| exact request JSON keys, nesting, types, and required/optional rules | **missing** |
| exact successful response fields and terminal meaning | **missing** |
| deterministic rejection response/status classification | **missing** |
| ambiguous/timeout/redirect handling and safe reconciliation rule | **missing** |
| read-only status endpoint and response contract | **missing** |
| mandatory completion/capture/redirect-return operation, if any | **missing** |
| whether retry, token refresh, or redirect continuation is mandatory or safe | **missing** |

The public bundle exposes only UI-route and analytics-level checkout words. It
does **not** evidence a final POST, its request JSON, an API success/rejection
schema, a status endpoint, or completion semantics. The repository's
`ordering_live_checkout.py` remains a deliberately unwired fixture-only model;
its paths and payload are not provider evidence and must never be promoted from
this document.

## Enforced consequence

Production final checkout is blocked. Do not add a live transport, submit,
retry, refresh/replay, status polling, completion call, or redirect continuation
based on these observations. A future proposal must first freeze a complete,
independently reviewable public contract without retaining personal data or
credentials; otherwise it remains unsupported.
