class GlovoOrderingPanel extends HTMLElement {
  set hass(hass) {
    if (this._hass) return;
    this._hass = hass;
    this._generation = undefined;
    this._selections = [];
    this._render();
    this._load();
  }

  disconnectedCallback() {
    this._invalidateAuthority("Panel disconnected; confirmation was discarded.");
    this._clearExpiryTimer();
  }

  _render() {
    this.innerHTML = `
      <ha-card header="Glovo live ordering">
        <style>
          :host { display:block; max-width:680px; margin:0 auto; }
          .pad { padding:12px; } .stack { display:grid; gap:10px; }
          .row { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
          button, input, select { min-height:44px; box-sizing:border-box; }
          input, select { flex:1 1 150px; min-width:0; }
          button { padding:0 12px; } .section { border-top:1px solid var(--divider-color); padding-top:12px; }
          .danger { border-left:4px solid var(--error-color); padding-left:10px; font-weight:600; }
          .quote { background:var(--secondary-background-color); border-radius:8px; padding:10px; }
          .muted { color:var(--secondary-text-color); font-size:.9em; }
          @media (max-width:480px) { .row > * { width:100%; flex-basis:100%; } button { width:100%; } }
        </style>
        <div class="pad stack">
          <ha-alert id="alert" alert-type="warning" hidden></ha-alert>
          <div id="status" role="status">Loading safe ordering status…</div>
          <div id="gate" class="muted"></div>

          <section id="recovery" class="section" hidden>
            <div class="danger">MANUAL_CHECK_REQUIRED — do not submit this order again.</div>
            <p>Check Glovo manually. A timeout, browser navigation, or this panel cannot prove whether an order succeeded.</p>
            <div id="attempt" class="muted"></div>
            <label><input id="manual-ack" type="checkbox"> I checked Glovo manually and understand this only records a local conclusion; it never re-submits.</label>
            <div class="row" id="recovery-actions">
              <button data-resolution="found_succeeded">Found succeeded</button>
              <button data-resolution="found_failed_or_cancelled">Found failed/cancelled</button>
              <button data-resolution="still_unknown">Still unknown</button>
            </div>
          </section>

          <section id="journey" class="stack" hidden>
            <div class="section stack">
              <strong>1. Saved delivery address</strong>
              <div class="row"><select id="address"><option value="">Load saved addresses</option></select><button id="load-addresses">Refresh addresses</button></div>
            </div>
            <div class="section stack">
              <strong>2. Store by explicit slug or URL</strong>
              <div class="muted">Broad store discovery is unsupported. Enter an explicit store slug or URL only.</div>
              <div class="row"><input id="store-slug" autocomplete="off" placeholder="Store slug or URL"><button id="lookup-store">Look up store</button></div>
              <select id="store"><option value="">No store selected</option></select>
            </div>
            <div class="section stack">
              <strong>3. Menu and basket</strong>
              <div id="menu" class="muted">Look up a store to load its menu.</div>
              <div class="row"><button id="save-basket" disabled>Save basket</button><button id="refresh-basket" disabled>Refresh basket</button><button id="reconcile-basket" disabled>Reconcile basket</button><button id="clear-basket" disabled>Clear basket</button></div>
              <div id="basket" class="muted">Basket is empty.</div>
            </div>
            <div class="section stack">
              <strong>4. Provider-selected saved card</strong>
              <div class="muted">Only the provider-selected masked saved card can be used. Card numbers are never shown or entered here.</div>
              <div class="row"><select id="payment"><option value="">Save a basket first</option></select><button id="load-payments" disabled>Refresh card</button></div>
            </div>
            <div class="section stack">
              <strong>5. Authoritative quote</strong>
              <div class="row"><button id="create-quote" disabled>Get authoritative quote</button></div>
              <div id="quote" class="quote muted">No authoritative quote. Prices are never calculated locally.</div>
            </div>
            <div id="confirmation" class="section stack" hidden>
              <strong>6. Exact confirmation preparation</strong>
              <div id="confirmation-copy"></div>
              <label>Type the exact acknowledgement shown below:<input id="typed-ack" autocomplete="off"></label>
              <div class="row"><button id="prepare-confirmation">Prepare confirmation</button><button id="submit-checkout" disabled>Paid checkout unsupported</button></div>
              <div class="muted">This release prepares and displays an exact authoritative confirmation only. It never submits a paid order.</div>
            </div>
            <div id="checkout-recovery" class="section stack" hidden>
              <div class="danger">MANUAL_CHECK_REQUIRED — no retry or re-submit is available.</div>
              <p>Check Glovo manually, then use the manual resolution controls above.</p>
              <button id="check-status">Check safe checkout status</button>
            </div>
          </section>
        </div>
      </ha-card>`;
    this.querySelector("#load-addresses").addEventListener("click", () => this._loadAddresses());
    this.querySelector("#address").addEventListener("change", () => {
      this._invalidateAuthority();
      this._resetCatalog();
    });
    this.querySelector("#lookup-store").addEventListener("click", () => this._lookupStore());
    this.querySelector("#store").addEventListener("change", () => this._selectStore());
    this.querySelector("#save-basket").addEventListener("click", () => this._saveBasket());
    this.querySelector("#refresh-basket").addEventListener("click", () => this._refreshBasket());
    this.querySelector("#reconcile-basket").addEventListener("click", () => this._reconcileBasket());
    this.querySelector("#clear-basket").addEventListener("click", () => this._clearBasket());
    this.querySelector("#load-payments").addEventListener("click", () => this._loadPayments());
    this.querySelector("#payment").addEventListener("change", () => this._invalidateAuthority());
    this.querySelector("#create-quote").addEventListener("click", () => this._createQuote());
    this.querySelector("#prepare-confirmation").addEventListener("click", () => this._prepareConfirmation());
    this.querySelector("#submit-checkout").addEventListener("click", () => this._submitCheckout());
    this.querySelector("#check-status").addEventListener("click", () => this._checkCheckoutStatus());
    this.querySelectorAll("#recovery-actions button").forEach((button) => button.addEventListener("click", () => this._resolveManual(button.dataset.resolution)));
  }

  _command(operation) { return `glovo/ordering/${operation}`; }
  _request(operation, request = {}) { return this._hass.callWS({ type: this._command(operation), ...request }); }
  _setStatus(value) { this.querySelector("#status").textContent = value; }
  _alert(value, type = "warning") { const item = this.querySelector("#alert"); item.alertType = type; item.textContent = value; item.hidden = !value; }
  _clearExpiryTimer() { if (this._expiryTimer) window.clearInterval(this._expiryTimer); this._expiryTimer = undefined; }

  _invalidateAuthority(reason = "Selection changed; quote confirmation was discarded.") {
    this._challenge = undefined;
    this._quote = undefined;
    this._checkoutSubmitting = false;
    this._clearExpiryTimer();
    const confirmation = this.querySelector("#confirmation");
    if (confirmation) confirmation.hidden = true;
    const submit = this.querySelector("#submit-checkout");
    if (submit) submit.disabled = true;
    if (reason && this._hass) this._setStatus(reason);
  }

  _resetCatalog() {
    this._storeHandle = undefined;
    this._menu = undefined;
    this._selections = [];
    const store = this.querySelector("#store");
    if (store) store.replaceChildren(new Option("Select explicit store", ""));
    const menu = this.querySelector("#menu");
    if (menu) menu.replaceChildren();
    const save = this.querySelector("#save-basket");
    if (save) save.disabled = true;
  }

  _withGeneration() { return { generation: this._generation }; }

  async _load() {
    try {
      const state = await this._request("state");
      if (Number.isInteger(state.generation) && state.generation > 0 && state.generation !== this._generation) {
        this._generation = state.generation;
        this._invalidateAuthority("Ordering generation changed; local confirmation was discarded.");
      }
      this._state = state;
      if (state.integrityFault) {
        this._alert("Ordering has a permanent integrity fault and remains blocked.");
        this._setStatus("Ordering is unavailable.");
        return;
      }
      if (state.manualCheckRequired) { await this._loadRecovery(); return; }
      const available = state.liveOrderingAvailable === true || state.orderingAvailable === true;
      const liveCheckout = state.liveCheckoutAvailable === true;
      this.querySelector("#gate").textContent = available
        ? `Preparation is enabled. Live paid checkout: ${liveCheckout ? "available" : "unavailable"}.`
        : "Live ordering preparation is unavailable. No live primitives are exposed while the gate is closed.";
      if (!available) { this._invalidateAuthority(""); this._setStatus("Ordering is unavailable."); return; }
      this._liveCheckoutAvailable = liveCheckout;
      this.querySelector("#journey").hidden = false;
      this._setStatus("Choose a masked saved address, then an explicit store.");
      await this._loadAddresses();
    } catch (_error) {
      this._setStatus("Ordering is unavailable.");
    }
  }

  async _loadAddresses() {
    try {
      const response = await this._request("live/addresses", this._withGeneration());
      const select = this.querySelector("#address"); select.replaceChildren(new Option("Select a saved address", ""));
      (response.addresses || []).forEach((item) => select.add(new Option(item.fullAddress || item.label, item.key || item.addressHandle)));
      this._invalidateAuthority();
    } catch (_error) { this._setStatus("Saved addresses are unavailable."); }
  }

  _storeSlug() {
    const raw = this.querySelector("#store-slug").value.trim();
    if (!raw) return "";
    try { const parsed = new URL(raw); return parsed.pathname.split("/").filter(Boolean).pop() || ""; } catch (_error) { return raw; }
  }

  async _lookupStore() {
    const storeSlug = this._storeSlug();
    const addressHandle = this.querySelector("#address").value;
    if (!addressHandle) { this._setStatus("Select a saved address before looking up a store."); return; }
    if (!storeSlug) { this._setStatus("Enter an explicit store slug or URL. Broad discovery is unsupported."); return; }
    try {
      const response = await this._request("live/stores", { ...this._withGeneration(), storeSlug, addressHandle });
      const select = this.querySelector("#store"); select.replaceChildren(new Option("Select explicit store", ""));
      (response.stores || []).forEach((item) => select.add(new Option(item.label, item.storeHandle)));
      this._setStatus("Select the explicit store to load its menu.");
      this._invalidateAuthority();
    } catch (_error) { this._setStatus("The explicit store is unavailable."); }
  }

  async _selectStore() {
    const storeHandle = this.querySelector("#store").value;
    this._invalidateAuthority(); this._selections = [];
    this.querySelector("#save-basket").disabled = true;
    if (!storeHandle) return;
    try {
      const addressHandle = this.querySelector("#address").value;
      const menu = await this._request("live/store_menu", { ...this._withGeneration(), storeHandle, addressHandle });
      this._storeHandle = storeHandle; this._menu = menu;
      const host = this.querySelector("#menu"); host.replaceChildren();
      (menu.products || []).forEach((product) => host.append(this._productControl(product)));
      this.querySelector("#save-basket").disabled = !(menu.products || []).length;
      this._setStatus("Choose quantities and approved options, then save the basket.");
    } catch (_error) { this._setStatus("Menu is unavailable."); }
  }

  _productControl(product) {
    const box = document.createElement("div"); box.className = "stack";
    const title = document.createElement("strong"); title.textContent = product.label; box.append(title);
    const quantity = document.createElement("input"); quantity.type = "number"; quantity.min = "0"; quantity.max = "99"; quantity.value = "0"; quantity.setAttribute("aria-label", `Quantity for ${product.label}`); box.append(quantity);
    const groupInputs = [];
    (product.optionGroups || []).forEach((group) => {
      const select = document.createElement("select"); select.multiple = group.multipleSelection === true; select.setAttribute("aria-label", group.label);
      (group.options || []).forEach((option) => { const row = new Option(option.label, option.optionHandle); row.selected = option.selected === true; select.add(row); });
      box.append(select); groupInputs.push({ groupHandle: group.groupHandle, input: select });
    });
    this._selections.push({ productHandle: product.productHandle, quantity, groupInputs });
    return box;
  }

  _basketProducts() {
    return this._selections.filter((item) => Number(item.quantity.value) > 0).map((item) => ({
      productHandle: item.productHandle, quantity: Number(item.quantity.value),
      options: item.groupInputs.map((group) => ({ groupHandle: group.groupHandle, optionHandles: Array.from(group.input.selectedOptions).map((option) => option.value) })),
    }));
  }

  async _saveBasket() {
    const products = this._basketProducts();
    const addressHandle = this.querySelector("#address").value;
    if (!products.length || !this._storeHandle || !addressHandle) { this._setStatus("Choose a saved address and at least one menu item."); return; }
    try {
      const basket = await this._request("live/basket_set", { ...this._withGeneration(), expectedRevision: this._basketRevision || 0, storeHandle: this._storeHandle, addressHandle, products });
      this._basketRevision = basket.revision; this._renderBasket(basket); this._invalidateAuthority();
      this.querySelector("#clear-basket").disabled = false; this.querySelector("#refresh-basket").disabled = false; this.querySelector("#reconcile-basket").disabled = false; this.querySelector("#load-payments").disabled = false;
      this._setStatus("Basket saved. Refresh the provider-selected saved card.");
    } catch (_error) { this._setStatus("Basket was not saved. Refresh and review selections; no order was submitted."); }
  }

  _renderBasket(basket) { this.querySelector("#basket").textContent = `Basket revision ${basket.revision}; ${basket.itemCount} item(s).`; }
  async _refreshBasket() {
    try { const basket = await this._request("live/basket", this._withGeneration()); this._basketRevision = basket.revision; this._renderBasket(basket); this._invalidateAuthority(); this._setStatus("Basket refreshed; any quote confirmation was discarded."); }
    catch (_error) { this._setStatus("Basket is unavailable. No order was submitted."); }
  }
  async _reconcileBasket() {
    try { const result = await this._request("live/basket_reconcile", this._withGeneration()); this._invalidateAuthority(); this._setStatus(result.status === "unsupported" ? "Basket reconciliation is unsupported; no retry was attempted." : "Basket reconciliation completed; review the refreshed basket."); await this._refreshBasket(); }
    catch (_error) { this._setStatus("Basket reconciliation is unavailable. No order was submitted."); }
  }
  async _clearBasket() {
    try { const result = await this._request("live/basket_clear", { ...this._withGeneration(), expectedRevision: this._basketRevision }); this._basketRevision = result.revision; this._renderBasket({ revision: result.revision, itemCount: 0 }); this._invalidateAuthority(); this.querySelector("#load-payments").disabled = true; this.querySelector("#create-quote").disabled = true; }
    catch (_error) { this._setStatus("Basket could not be cleared. No order was submitted."); }
  }

  async _loadPayments() {
    try { const response = await this._request("live/payment_methods", this._withGeneration()); const select = this.querySelector("#payment"); select.replaceChildren(new Option("Select provider-selected masked card", "")); (response.paymentMethods || []).forEach((item) => select.add(new Option(item.label, item.key || item.paymentHandle))); this.querySelector("#create-quote").disabled = false; this._invalidateAuthority(); this._setStatus("Select the provider-selected masked saved card, then get the authoritative quote."); }
    catch (_error) { this._setStatus("No eligible provider-selected saved card is available."); }
  }

  async _createQuote() {
    const addressHandle = this.querySelector("#address").value; const paymentHandle = this.querySelector("#payment").value;
    if (!addressHandle || !paymentHandle) { this._setStatus("Select both a masked address and the provider-selected card."); return; }
    this._invalidateAuthority();
    try { const quote = await this._request("live/create_quote", { ...this._withGeneration(), addressHandle, paymentHandle }); this._quote = quote; this._renderQuote(quote); this.querySelector("#confirmation").hidden = false; this._setStatus(this._liveCheckoutAvailable ? "Review the exact authoritative total before preparing confirmation." : "Paid checkout is unsupported; you can prepare and review the exact authoritative confirmation."); }
    catch (_error) { this._setStatus("Authoritative quote is unavailable. No local price was used."); }
  }

  _renderQuote(quote) {
    const host = this.querySelector("#quote"); host.replaceChildren();
    (quote.priceLines || []).forEach((line) => { const row = document.createElement("div"); row.textContent = `${line.title}: ${line.value || "—"}`; host.append(row); });
    const total = document.createElement("strong"); total.textContent = `Exact total: ${quote.purchaseTotalCents} ${quote.currencyCode}`; host.append(total);
    const detail = document.createElement("div"); detail.className = "muted"; detail.textContent = `${quote.address || "Masked saved address"}; ${quote.payment || "Provider-selected saved card"}; ETA: ${quote.eta || "not provided"}.`; host.append(detail);
    this._clearExpiryTimer(); this._expiryTimer = window.setInterval(() => { const seconds = Math.max(0, Math.ceil(Number(quote.expiresAt) - Date.now() / 1000)); detail.textContent = `${quote.address || "Masked saved address"}; ${quote.payment || "Provider-selected saved card"}; ETA: ${quote.eta || "not provided"}; quote expires in ${seconds}s.`; if (seconds <= 0) { this._invalidateAuthority("Quote expired; request a fresh authoritative quote."); } }, 1000);
  }

  async _prepareConfirmation() {
    if (!this._quote) return;
    try { const prepared = await this._request("live/prepare_confirmation", this._withGeneration()); this._challenge = prepared.challenge; const exact = `${prepared.purchaseTotalCents} ${prepared.currencyCode}`; this._ackText = `ACK ${exact}`; this.querySelector("#confirmation-copy").textContent = `Type exactly: ${this._ackText}. This prepares the displayed exact total and currency for canary validation only; paid checkout is unsupported.`; this.querySelector("#typed-ack").value = ""; this.querySelector("#submit-checkout").disabled = true; this._setStatus("Exact confirmation prepared for review. Paid checkout remains unsupported."); }
    catch (_error) { this._invalidateAuthority("Confirmation is invalid or expired. Get a fresh quote."); }
  }

  async _submitCheckout() {
    const button = this.querySelector("#submit-checkout");
    if (this._checkoutSubmitting || !this._challenge || this.querySelector("#typed-ack").value !== this._ackText) { this._setStatus("Type the exact displayed acknowledgement before submitting."); return; }
    const challenge = this._challenge;
    this._checkoutSubmitting = true; button.disabled = true; this._challenge = undefined;
    try { const result = await this._request("live/execute_checkout", { ...this._withGeneration(), challenge, acknowledged: true }); this._afterCheckout(result); }
    catch (_error) { this._afterCheckout({ status: "MANUAL_CHECK_REQUIRED" }); }
  }

  _afterCheckout(result) { this._invalidateAuthority(""); this.querySelector("#submit-checkout").disabled = true; const manual = String(result.status || "").toUpperCase().includes("MANUAL") || result.manualCheckRequired === true; if (manual) { this.querySelector("#checkout-recovery").hidden = false; this._setStatus("MANUAL_CHECK_REQUIRED — check Glovo manually. This panel will not retry."); } else { this._setStatus("Checkout submission result recorded. Do not submit again; check Glovo manually if uncertain."); } }
  async _checkCheckoutStatus() { try { const status = await this._request("live/checkout_status", this._withGeneration()); this._setStatus(status.status === "succeeded" ? "Provider confirmed a result." : "Manual check remains required. Do not retry."); } catch (_error) { this._setStatus("Manual check remains required. Do not retry."); } }

  async _loadRecovery() {
    try { const response = await this._hass.callWS({ type: "glovo/ordering/manual_checks" }); const attempt = (response.attempts || [])[0]; this._alert("MANUAL_CHECK_REQUIRED — do not place this order again. Check Glovo manually before recording a conclusion."); this.querySelector("#recovery").hidden = false; if (!attempt) { this._setStatus("Manual review remains required."); return; } this._attempt = attempt; this.querySelector("#attempt").textContent = `${attempt.storeDisplayName}; ${attempt.itemSummary}; ${attempt.amountMinor} ${attempt.currency}; ${attempt.maskedPaymentLabel}; ${attempt.maskedAddressAlias}.`; this._setStatus("Manual account check required."); } catch (_error) { this._setStatus("Manual recovery is unavailable; ordering remains blocked."); }
  }

  async _resolveManual(resolution) {
    if (this.querySelector("#manual-ack").checked !== true || !this._attempt) { this._setStatus("Explicit manual-review acknowledgement is required."); return; }
    try { const prepared = await this._hass.callWS({ type: "glovo/ordering/prepare_manual_resolution", attemptRef: this._attempt.attemptRef, expectedRecordRevision: this._attempt.recordRevision, expectedState: this._attempt.state, resolution }); await this._hass.callWS({ type: "glovo/ordering/resolve_manual_check", attemptRef: this._attempt.attemptRef, expectedRecordRevision: this._attempt.recordRevision, expectedState: this._attempt.state, resolution, challenge: prepared.challenge, acknowledged: true }); this._invalidateAuthority(); await this._load(); } catch (_error) { this._setStatus("Resolution was rejected or not persisted; ordering remains blocked."); }
  }
}

const componentName = new URL(import.meta.url).searchParams.get("component") || "glovo-ordering-panel";
if (!customElements.get(componentName)) customElements.define(componentName, GlovoOrderingPanel);
