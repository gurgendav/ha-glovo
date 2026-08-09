# Live-ordering operator runbook (production gate remains closed)

> **Current state:** this integration cannot make a production purchase.
> `productionFinalCheckoutSupported: false` is recorded in
> [protocol evidence](live-ordering-protocol-evidence.md). This runbook defines
> the controls that would apply only after that blocker is resolved; it is not
> authority to operate a live checkout today.

## Non-negotiable operating model

1. **Two default-off gates.** Gate 1 is the administrator's explicit options
   switch plus acknowledgement for the experimental ordering surface. Gate 2 is
   a separately reviewed production-final-checkout capability, disabled unless
   the exact-SHA protocol evidence is complete and approved. Gate 1 alone is
   never payment authority.
2. **Preparation can change remote state.** Browsing/menu selection, basket
   creation or replacement, and quote-template preparation may mutate a remote
   basket or template. Treat them as externally visible; do not describe them
   as dry-run or idempotent.
3. **Payment and amount authority.** Only a selected saved card may be used;
   never collect, display, or log card details. The server quote is
   authoritative. Show and require confirmation of the exact quoted amount and
   currency immediately before a final action; reject stale, changed, missing,
   or locally computed totals.
4. **One attempt only.** A final submit, if ever enabled, is one dispatch with
   no retry, fallback, refresh-and-replay, compensating mutation, or automatic
   redirect continuation. Timeout, cancellation, malformed response, unknown
   status, or lost identifier is **ambiguous**: stop and reconcile manually in
   the provider application before any further action.
5. **Privacy.** Do not put tokens, cookies, payment data, full addresses,
   provider checkout/order references, request bodies, or raw responses in
   issue reports, logs, screenshots, fixtures, or chat. Use only masked saved
   selections and a local opaque journal handle.
6. **Rollback preserves uncertainty.** Disable the gate and remove the panel
   if required, but never clear, overwrite, retry, or mark resolved an
   unresolved/ambiguous journal record. Reconcile manually first.

## Required preflight (both canaries)

- [ ] The release checklist is green from a clean tracked checkout.
- [ ] The exact source and asset SHA reviewed is the one recorded in the
  release record; any change invalidates the result.
- [ ] Journal is clean: no unresolved, submitted, pending, or ambiguous record.
- [ ] Check the provider application for an existing basket/order/payment
  conflict before starting.
- [ ] A named operator has authority to stop; monitoring and manual provider
  access are available.
- [ ] No secrets, cookies, addresses, card data, raw provider IDs, or raw
  responses will be captured.

## No-payment canary (only after Gate 2 exists)

1. Verify the exact artifact SHA and all preflight items.
2. Enable Gate 1 and the separately approved Gate 2 only for the designated
   operator; record the timestamp and local opaque run handle.
3. Exercise only the approved non-payment path. Observe that remote basket or
   template changes, if any, match the intended preparation action.
4. Stop before any final confirmation/submit control. Verify that no final
   journal attempt exists and check the provider application for conflicts.
5. Disable both gates. Preserve the clean journal evidence and any ambiguous
   state rather than deleting it.

**Abort immediately** on a changed SHA, incomplete evidence, unexpected remote
basket/template state, unmasked sensitive data, journal residue, app conflict,
or any appearance of a final-payment control before the planned stop point.

## One-payment E2E (blocked until the protocol evidence verdict changes)

This procedure is not currently executable. Once independently approved:

1. Repeat every preflight check, including clean journal, provider-app conflict
   check, exact SHA match, selected saved card, server-authoritative quote, and
   displayed exact amount/currency.
2. Have the operator compare the final quote with the provider application and
   give one explicit amount-bound confirmation.
3. Send **one** final attempt only. Do not retry under any circumstance.
4. If the response is anything other than the fully evidenced deterministic
   success classification, stop. Treat it as ambiguous/rejected exactly as
   specified by the frozen contract and reconcile manually in the provider app.
5. After a deterministic success, perform only the separately evidenced
   read-only status/check operation. Do not complete/capture/redirect/replay
   unless that exact operation is proven mandatory by the evidence.
6. Disable both gates after the attempt. Rollback preserves the journal; it
   never clears unresolved state.

**Stop/abort criteria:** quote changes or expires; a different payment/address
is selected; any endpoint/schema/status/completion/redirect behavior differs
from frozen evidence; a token refresh or retry would be needed; connection is
lost; a provider-app conflict appears; privacy is at risk; or the operator
cannot manually reconcile immediately.
