class GlovoOrderingPanel extends HTMLElement {
  set hass(hass) {
    if (this._hass) return;
    this._hass = hass;
    this._load();
  }

  async _load() {
    this.innerHTML = `<ha-card header="Glovo Ordering">
      <div style="padding: 16px">
        <ha-alert id="alert" alert-type="warning"></ha-alert>
        <p id="status">Loading ordering state…</p>
        <div id="recovery" hidden>
          <div id="attempt"></div>
          <label style="display:block;margin:12px 0">
            <input id="ack" type="checkbox">
            I reviewed the provider account and understand this local conclusion never redispatches the attempt.
          </label>
          <div style="display:flex;gap:8px;flex-wrap:wrap">
            <button data-resolution="found_succeeded">Found succeeded</button>
            <button data-resolution="found_failed_or_cancelled">Found failed or cancelled</button>
            <button data-resolution="still_unknown">Still unknown</button>
          </div>
        </div>
        <div id="catalog"></div>
      </div>
    </ha-card>`;
    try {
      const state = await this._hass.callWS({ type: "glovo/ordering/state" });
      if (state.integrityFault) {
        this.querySelector("#alert").textContent =
          "Ordering has a permanent integrity fault. It cannot be cleared from this panel or API.";
        this.querySelector("#status").textContent = "Ordering remains blocked.";
        return;
      }
      if (state.manualCheckRequired) {
        await this._loadRecovery();
        return;
      }
      this.querySelector("#alert").textContent =
        "Experimental fixture-only simulation. This panel cannot make a live purchase.";
      const catalog = await this._hass.callWS({
        type: "glovo/ordering/catalog",
        generation: state.generation,
      });
      this.querySelector("#status").textContent =
        `Generation ${state.generation}; live ordering available: ${state.liveOrderingAvailable}.`;
      this.querySelector("#catalog").textContent =
        `Synthetic fixture stores: ${catalog.stores.map((store) => store.label).join(", ")}`;
    } catch (_error) {
      this.querySelector("#status").textContent = "Ordering is disabled or unavailable.";
    }
  }

  async _loadRecovery() {
    const response = await this._hass.callWS({ type: "glovo/ordering/manual_checks" });
    const attempt = response.attempts[0];
    this.querySelector("#alert").textContent =
      "A checkout outcome is ambiguous. Do not place this order again. New ordering is blocked until an administrator reviews the provider account.";
    if (!attempt) {
      this.querySelector("#status").textContent =
        "A previous conclusion is waiting for its durable latch to clear.";
      return;
    }
    this._attempt = attempt;
    this.querySelector("#status").textContent = "Manual account check required.";
    this.querySelector("#attempt").textContent =
      `${attempt.storeDisplayName}: ${attempt.itemSummary}; ${attempt.amountMinor} ${attempt.currency}; ` +
      `${attempt.maskedPaymentLabel}; ${attempt.maskedAddressAlias}; reason: ${attempt.ambiguityReason}.`;
    const recovery = this.querySelector("#recovery");
    recovery.hidden = false;
    recovery.querySelectorAll("button[data-resolution]").forEach((button) => {
      button.addEventListener("click", () => this._resolve(button.dataset.resolution));
    });
  }

  async _resolve(resolution) {
    const acknowledged = this.querySelector("#ack").checked === true;
    if (!acknowledged) {
      this.querySelector("#status").textContent = "Explicit acknowledgement is required.";
      return;
    }
    const attempt = this._attempt;
    try {
      const prepared = await this._hass.callWS({
        type: "glovo/ordering/prepare_manual_resolution",
        attemptRef: attempt.attemptRef,
        expectedRecordRevision: attempt.recordRevision,
        expectedState: attempt.state,
        resolution,
      });
      const result = await this._hass.callWS({
        type: "glovo/ordering/resolve_manual_check",
        attemptRef: attempt.attemptRef,
        expectedRecordRevision: attempt.recordRevision,
        expectedState: attempt.state,
        resolution,
        challenge: prepared.challenge,
        acknowledged: true,
      });
      this.querySelector("#status").textContent = result.manualCheckRequired
        ? "Review recorded; ordering remains blocked."
        : "Manual check resolved. Old checkout authority was invalidated.";
      await this._load();
    } catch (_error) {
      this.querySelector("#status").textContent =
        "Resolution was rejected or could not be persisted; ordering remains blocked.";
    }
  }
}

customElements.define("glovo-ordering-panel", GlovoOrderingPanel);
