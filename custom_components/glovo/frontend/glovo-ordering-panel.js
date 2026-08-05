class GlovoOrderingPanel extends HTMLElement {
  set hass(hass) {
    if (this._hass) return;
    this._hass = hass;
    this._load();
  }

  async _load() {
    this.innerHTML = `<ha-card header="Glovo Mock Ordering">
      <div style="padding: 16px">
        <ha-alert alert-type="warning">
          Experimental, fixture-only simulation. This panel cannot make a live purchase.
        </ha-alert>
        <p id="status">Loading mock fixture state…</p>
        <div id="catalog"></div>
      </div>
    </ha-card>`;
    try {
      const state = await this._hass.callWS({ type: "glovo/ordering/state" });
      const catalog = await this._hass.callWS({
        type: "glovo/ordering/catalog",
        generation: state.generation,
      });
      this.querySelector("#status").textContent =
        `Generation ${state.generation}; live ordering available: ${state.liveOrderingAvailable}.`;
      this.querySelector("#catalog").textContent =
        `Synthetic fixture stores: ${catalog.stores.map((store) => store.label).join(", ")}`;
    } catch (_error) {
      this.querySelector("#status").textContent = "Mock ordering is disabled or unavailable.";
    }
  }
}

customElements.define("glovo-ordering-panel", GlovoOrderingPanel);
