# Guarded live paid-checkout operator runbook

> **Release policy:** production final checkout is supported only through the reviewed one-POST, no-retry adapter and manual-reconciliation policy in [protocol evidence](live-ordering-protocol-evidence.md). This runbook does not authorize unattended ordering, deployment, or a live test.

## Non-negotiable operating model

1. **Two fresh default-off controls.** Preparation consent and its acknowledgement permit remote basket/template preparation. Separate paid-checkout consent and acknowledgement permit one paid submission. Paid consent implies both preparation controls; migration and reauthentication reset all four values.
2. **Preparation can change remote state.** Basket and template operations are consequential and are not described as dry-run or idempotent.
3. **Exact payment authority.** Use only the provider-selected saved card. Immediately before submit, show the exact store, items/quantities/options, admin-only ephemeral full address, masked card, provider price lines, total with ISO currency, ETA, and expiry. Require the exact amount-bound acknowledgement.
4. **One final POST.** Dispatch `POST /v3/checkouts/order/1` once. Never retry, refresh-and-replay, fall back, or compensate after dispatch.
5. **No payment continuations.** Never issue checkout completion, checkout/payment cancellation, redirect, capture, wallet, 3DS, split-payment, or other payment mutation.
6. **Manual ambiguity.** Pending/auth/`PROCESS_PAYMENT`, timeout, cancellation, transport loss, malformed/unknown/contradictory response, exact-identity mismatch, or persistence uncertainty becomes `MANUAL_CHECK_REQUIRED`. Do not place another order.
7. **Status is explicit and read-only.** If and only if the private journal learned a checkout ID, one administrator action may issue exactly one `GET /v3/checkouts/order/{checkoutId}`. There is no polling. Without an ID, use the provider app.
8. **Privacy.** Public and recovery projections expose only `hasCheckoutId`, never provider IDs. The full address is admin-only and ephemeral; never put it, credentials, card data, IDs, request bodies, or raw responses in logs, journals, screenshots, issues, or chat.
9. **Rollback preserves uncertainty.** Disable access, but never clear/retry/overwrite an unresolved attempt. An integrity fault is fail-closed and not normally clearable.

## Preflight

- [ ] Artifact is release `1.1.0+home.5` with the reviewed trust identity and all release gates green from a clean tracked archive.
- [ ] Journal and safety state are coherent, with no unresolved/manual/integrity record.
- [ ] Both preparation controls and both separate paid controls were freshly enabled by the named administrator.
- [ ] Provider app shows no conflicting order or payment.
- [ ] Selected payment is a saved card and no interactive continuation is anticipated.
- [ ] Exact quote facts and expiry are visible; operator can manually inspect the provider app immediately.
- [ ] No private data capture is enabled.

## No-payment canary

1. Verify release/trust identity and every preflight item.
2. Exercise only approved address/store/menu/basket/template reads or preparation actions.
3. Stop before **Submit paid order**. Verify no final attempt exists.
4. Inspect provider app for unexpected basket/order/payment state.
5. Disable both consent groups. Preserve any unexpected durable state.

Abort on changed identity, stale quote, unexpected mutation, private-data exposure, journal residue, or provider-app conflict.

## Deliberately authorized one-payment validation

1. Repeat preflight and obtain explicit human authorization for the displayed store, items/options, full destination, saved card, exact amount, and currency.
2. Confirm the amount-bound acknowledgement exactly and invoke the paid control once.
3. If terminal `COMPLETED` exactly matches durable basket/amount/currency, preserve provider-confirmed success evidence. If terminal `FAILED`/`CANCELLED` is schema-valid and non-contradictory, preserve provider-confirmed failure evidence.
4. For every other result, stop immediately. Do not retry or issue completion/cancellation/payment mutations. Inspect the provider app.
5. If an ID was learned, the operator may use **Check provider status** once per deliberate action. Pending/malformed/transport/mismatch remains manual. With no learned ID, this action performs no GET.
6. Disable both consent groups after the validation. Rollback never clears unresolved state.

## Recovery

- `MANUAL_CHECK_REQUIRED` blocks further ordering across restart, browser, and administrator.
- Use privacy-safe recovery facts and `hasCheckoutId`; never copy provider IDs into public channels.
- Provider-confirmed terminal status can durably clear the manual latch only after coherent journal and safety persistence.
- If either persistence layer fails, retain manual state or enter permanent integrity fault. Do not infer an outcome.
- Manual administrator resolution requires direct provider-app/account/payment inspection and the existing challenge-bound acknowledgement. `still_unknown` preserves the block.
