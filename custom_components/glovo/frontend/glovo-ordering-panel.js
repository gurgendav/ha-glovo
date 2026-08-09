class GlovoOrderingPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._initializeModel();
    this._readEpochs = new Map();
    this._singleFlights = new Map();
    this._generation = undefined;
    this._rendered = false;
  }

  _initializeModel() {
    this._model = {
      lifecycle: { status: "booting", message: "Loading safe ordering status…", kind: "status" },
      capability: { liveOrderingAvailable: false, liveCheckoutAvailable: false, integrityFault: false },
      context: { addresses: [], addressHandle: "", addressLabel: "", storeInput: "", stores: [], store: null },
      menu: { status: "idle", products: [], query: "", filter: "all", error: "" },
      draft: { lines: [], dirty: false },
      basket: { status: "empty", revision: 0, itemCount: 0, currency: "", providerTotal: null, linesAvailable: true },
      payments: { status: "idle", items: [], selected: "" },
      quote: null,
      overlay: null,
      library: { status: "idle", storeRevision: 0, addresses: [], packages: [], aliasEditor: null, packageEditor: null, error: "" },
      recovery: null,
    };
  }

  set hass(hass) {
    const firstAssignment = !this._hass;
    this._hass = hass;
    if (!this._rendered) this._renderShell();
    this._updateEnvironment();
    if (firstAssignment) this._bootstrap();
  }

  disconnectedCallback() {
    this._invalidateAuthority("Panel disconnected; confirmation was discarded.");
    this._clearExpiryTimer();
    this._closeAllDialogs(false);
  }

  _renderShell() {
    this.shadowRoot.innerHTML = `
      <style>
        :host {
          display: block; color: var(--primary-text-color); background: var(--primary-background-color);
          font: inherit; min-block-size: 100%; --workspace-gap: 16px;
        }
        * { box-sizing: border-box; }
        button, input, select, textarea { font: inherit; color: inherit; }
        button, input, select, textarea, .touch-target { min-block-size: 44px; }
        button { cursor: pointer; border: 1px solid var(--divider-color); border-radius: 10px; padding: 8px 14px; background: var(--card-background-color); }
        button:hover:not(:disabled) { border-color: var(--primary-color); }
        button:focus-visible, input:focus-visible, select:focus-visible, textarea:focus-visible { outline: 3px solid var(--primary-color); outline-offset: 2px; }
        button:disabled { cursor: not-allowed; color: var(--disabled-text-color); opacity: .72; }
        .primary { color: var(--text-primary-color); background: var(--primary-color); border-color: var(--primary-color); font-weight: 650; }
        .danger { color: var(--error-color); border-color: var(--error-color); }
        .quiet { color: var(--secondary-text-color); }
        .warning { color: var(--warning-color); }
        .success { color: var(--success-color); }
        .workspace { inline-size: min(1440px, 100%); margin-inline: auto; padding: 20px; }
        .topbar { display: flex; gap: 14px; align-items: flex-start; justify-content: space-between; margin-block-end: 16px; }
        .topbar h1 { margin: 0; font-size: clamp(1.45rem, 3vw, 2rem); }
        .badges { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; }
        .badge { border: 1px solid var(--divider-color); border-radius: 999px; padding: 5px 10px; font-size: .82rem; background: var(--secondary-background-color); }
        .statusbar { margin-block-end: 14px; padding: 10px 12px; border-inline-start: 4px solid var(--primary-color); border-radius: 8px; background: var(--secondary-background-color); }
        .statusbar[data-kind="error"] { border-color: var(--error-color); }
        .statusbar[data-kind="warning"] { border-color: var(--warning-color); }
        .context-card, .panel-card { border-radius: var(--ha-card-border-radius, 12px); box-shadow: var(--ha-card-box-shadow); background: var(--card-background-color); border: 1px solid var(--divider-color); }
        .context-card { padding: 16px; margin-block-end: 14px; }
        .context-grid { display: grid; grid-template-columns: minmax(220px, 1fr) minmax(260px, 1.35fr); gap: 14px; }
        .field { display: grid; gap: 6px; min-inline-size: 0; }
        .field > span, .field-label { font-size: .84rem; color: var(--secondary-text-color); font-weight: 600; }
        .field-row { display: flex; gap: 8px; min-inline-size: 0; }
        .field-row input, .field-row select { min-inline-size: 0; inline-size: 100%; border: 1px solid var(--divider-color); border-radius: 9px; padding-inline: 11px; background: var(--card-background-color); }
        .selected-address { overflow-wrap: anywhere; line-height: 1.35; }
        .store-summary { grid-column: 1 / -1; display: flex; flex-wrap: wrap; gap: 8px 16px; padding-block-start: 4px; }
        .closed-store-warning { grid-column: 1 / -1; display: grid; gap: 5px; padding: 14px 16px; border: 2px solid var(--warning-color); border-radius: 10px; background: color-mix(in srgb, var(--warning-color) 13%, var(--card-background-color)); }
        .closed-store-warning strong { font-size: 1rem; color: var(--warning-color); }
        .view-nav { display: flex; gap: 6px; margin-block: 12px; }
        .view-nav button[aria-current="page"] { color: var(--primary-color); border-color: var(--primary-color); background: var(--secondary-background-color); font-weight: 700; }
        .layout { display: grid; grid-template-columns: minmax(0, 1fr) 360px; gap: var(--workspace-gap); align-items: start; }
        .main-column { min-inline-size: 0; }
        .basket-column { position: sticky; inset-block-start: 16px; max-block-size: calc(100vh - 32px); overflow: auto; }
        .panel-card { padding: 16px; }
        .toolbar { position: sticky; inset-block-start: 0; z-index: 2; display: grid; gap: 10px; padding: 10px 0; background: var(--primary-background-color); }
        .search-row { display: flex; gap: 8px; }
        .search-row input { inline-size: 100%; border: 1px solid var(--divider-color); border-radius: 12px; padding-inline: 14px; background: var(--card-background-color); }
        .filter-row { display: flex; gap: 8px; overflow-x: auto; scrollbar-width: thin; padding-block-end: 2px; }
        .filter-row button { white-space: nowrap; border-radius: 999px; }
        .filter-row button[aria-pressed="true"] { border-color: var(--primary-color); color: var(--primary-color); font-weight: 700; }
        .result-count { margin: 0; font-size: .88rem; color: var(--secondary-text-color); }
        .product-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 12px; }
        .product-card { content-visibility: auto; contain-intrinsic-size: 190px; display: grid; gap: 12px; align-content: start; min-block-size: 178px; padding: 15px; border: 1px solid var(--divider-color); border-radius: var(--ha-card-border-radius, 12px); background: var(--card-background-color); }
        .product-card h3, .basket-card h3, .library-card h3 { margin: 0; font-size: 1.02rem; overflow-wrap: anywhere; }
        .product-title { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; }
        .price { font-variant-numeric: tabular-nums; white-space: nowrap; font-weight: 700; }
        .card-actions, .stepper { display: flex; gap: 8px; align-items: center; }
        .card-actions { align-self: end; justify-content: space-between; }
        .stepper button { inline-size: 44px; padding: 0; font-size: 1.2rem; }
        .stepper output { min-inline-size: 2ch; text-align: center; font-variant-numeric: tabular-nums; }
        .empty-state { display: grid; justify-items: start; gap: 8px; padding: 28px; border: 1px dashed var(--divider-color); border-radius: 12px; background: var(--card-background-color); }
        .empty-state h2, .empty-state p { margin: 0; }
        .skeleton-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 12px; }
        .skeleton { min-block-size: 170px; border-radius: 12px; background: linear-gradient(90deg, var(--secondary-background-color), var(--divider-color), var(--secondary-background-color)); background-size: 220% 100%; animation: shimmer 1.3s linear infinite; }
        @keyframes shimmer { to { background-position: -220% 0; } }
        .basket-head { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
        .basket-lines { display: grid; gap: 10px; margin-block: 14px; }
        .basket-line { display: grid; gap: 6px; padding-block-end: 10px; border-block-end: 1px solid var(--divider-color); }
        .basket-line:last-child { border-block-end: 0; }
        .line-head { display: flex; justify-content: space-between; gap: 8px; }
        .line-options { font-size: .83rem; color: var(--secondary-text-color); overflow-wrap: anywhere; }
        .basket-actions { display: grid; gap: 8px; margin-block-start: 12px; }
        .basket-actions .primary { inline-size: 100%; }
        .totals { display: grid; gap: 6px; padding-block: 10px; border-block: 1px solid var(--divider-color); }
        .total-row { display: flex; justify-content: space-between; gap: 12px; }
        .authority-note { font-size: .82rem; color: var(--secondary-text-color); }
        .quote-box { display: grid; gap: 8px; margin-block-start: 12px; padding: 12px; border-radius: 10px; background: var(--secondary-background-color); }
        .quote-box h3 { margin: 0; }
        .quote-line { display: flex; justify-content: space-between; gap: 12px; }
        .mobile-basket-bar { display: none; position: fixed; z-index: 8; inset-inline: 0; inset-block-end: 0; padding: 9px 12px calc(9px + env(safe-area-inset-bottom)); border-block-start: 1px solid var(--divider-color); background: var(--card-background-color); box-shadow: var(--ha-card-box-shadow); }
        .mobile-basket-bar .bar-inner { display: flex; align-items: center; justify-content: space-between; gap: 12px; inline-size: 100%; }
        dialog { inline-size: min(680px, calc(100vw - 32px)); max-block-size: calc(100vh - 32px); margin: auto; padding: 0; border: 1px solid var(--divider-color); border-radius: var(--ha-card-border-radius, 14px); color: var(--primary-text-color); background: var(--card-background-color); box-shadow: var(--ha-card-box-shadow); }
        dialog::backdrop { background: color-mix(in srgb, var(--primary-text-color) 48%, transparent); }
        .dialog-frame { display: grid; grid-template-rows: auto minmax(0, 1fr) auto; max-block-size: calc(100vh - 34px); }
        .dialog-head, .dialog-foot { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 12px 16px; background: var(--card-background-color); }
        .dialog-head { border-block-end: 1px solid var(--divider-color); }
        .dialog-head h2 { margin: 0; font-size: 1.15rem; }
        .dialog-foot { border-block-start: 1px solid var(--divider-color); }
        .dialog-body { overflow: auto; padding: 16px; }
        .customizer-form { display: grid; gap: 16px; }
        fieldset { display: grid; gap: 8px; margin: 0; padding: 12px; border: 1px solid var(--divider-color); border-radius: 10px; }
        fieldset[data-invalid="true"] { border-color: var(--error-color); }
        legend { padding-inline: 5px; font-weight: 700; }
        .choice { display: flex; align-items: center; gap: 10px; min-block-size: 44px; }
        .choice input { inline-size: 20px; block-size: 20px; accent-color: var(--primary-color); }
        .choice-label { flex: 1; }
        .group-rule, .field-error { font-size: .82rem; }
        .field-error { color: var(--error-color); font-weight: 650; }
        .custom-quantity { display: flex; align-items: center; gap: 10px; }
        .custom-quantity input { inline-size: 72px; text-align: center; border: 1px solid var(--divider-color); border-radius: 8px; }
        .library-layout { display: grid; gap: 18px; }
        .library-section { display: grid; gap: 10px; }
        .section-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
        .section-head h2 { margin: 0; font-size: 1.15rem; }
        .library-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 10px; }
        .library-card { display: grid; gap: 10px; padding: 14px; border: 1px solid var(--divider-color); border-radius: 11px; background: var(--card-background-color); }
        .library-actions { display: flex; flex-wrap: wrap; gap: 6px; }
        .form-grid { display: grid; gap: 12px; }
        .form-grid input, .form-grid select, .form-grid textarea { inline-size: 100%; border: 1px solid var(--divider-color); border-radius: 8px; padding: 9px 11px; background: var(--card-background-color); }
        .blocking { inline-size: min(760px, calc(100% - 32px)); margin: 32px auto; padding: 22px; }
        .blocking h1 { margin-block-start: 0; }
        .recovery-actions { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
        [hidden] { display: none !important; }
        @media (max-width: 1023px) {
          .layout { display: block; }
          .basket-column { display: none; }
          .mobile-basket-bar { display: block; }
          .workspace { padding-block-end: calc(84px + env(safe-area-inset-bottom)); }
        }
        @media (max-width: 719px) {
          .workspace { padding: 12px 12px calc(82px + env(safe-area-inset-bottom)); }
          .topbar { display: grid; }
          .badges { justify-content: flex-start; }
          .context-grid { grid-template-columns: 1fr; }
          .store-summary { grid-column: auto; }
          .product-grid { grid-template-columns: 1fr; }
          .toolbar { inset-block-start: 0; }
          dialog { inline-size: 100vw; max-inline-size: none; max-block-size: 96vh; margin: auto 0 0; border-radius: 18px 18px 0 0; border-block-end: 0; }
          .dialog-frame { max-block-size: 96vh; min-block-size: min(88vh, 760px); }
          #package-dialog { max-block-size: 100vh; border-radius: 0; }
          #package-dialog .dialog-frame { min-block-size: 100vh; max-block-size: 100vh; }
          .recovery-actions { grid-template-columns: 1fr; }
        }
        @media (prefers-reduced-motion: reduce) {
          *, *::before, *::after { scroll-behavior: auto !important; animation-duration: .001ms !important; animation-iteration-count: 1 !important; transition-duration: .001ms !important; }
        }
        @media (forced-colors: active) {
          button, input, select, textarea, .context-card, .panel-card, .product-card, fieldset, dialog { border: 1px solid CanvasText; }
          .primary { forced-color-adjust: none; background: Highlight; color: HighlightText; }
          .badge { border: 1px solid CanvasText; }
        }
      </style>
      <main class="workspace" id="workspace" aria-busy="true">
        <header class="topbar">
          <div><h1>Glovo Ordering</h1><div class="quiet">Build a reviewed provider basket. Paid ordering is separately capability-gated.</div></div>
          <div class="badges" id="capability-badges"></div>
        </header>
        <div id="status" class="statusbar" role="status" aria-live="polite"></div>
        <section id="blocking" class="blocking panel-card" role="alert" hidden></section>
        <div id="app" hidden>
          <section class="context-card" aria-labelledby="context-title"><h2 id="context-title">Delivery and store</h2><div id="context-content"></div></section>
          <nav class="view-nav" aria-label="Ordering workspace"><button data-action="nav-view" data-view="menu" aria-current="page">Menu</button><button data-action="nav-view" data-view="packages">Packages</button></nav>
          <div class="layout">
            <section class="main-column" id="main-content" aria-live="polite"></section>
            <aside class="basket-column panel-card" aria-label="Draft basket"><div id="basket-panel"></div></aside>
          </div>
        </div>
      </main>
      <div class="mobile-basket-bar" id="mobile-basket-bar" hidden><div class="bar-inner" id="mobile-basket-content"></div></div>
      <dialog id="customizer-dialog" aria-labelledby="customizer-title"><div class="dialog-frame"><header class="dialog-head"><h2 id="customizer-title">Customize item</h2><button data-action="close-dialog" data-dialog="customizer-dialog" aria-label="Close customizer">×</button></header><div class="dialog-body" id="customizer-content"></div><footer class="dialog-foot" id="customizer-footer"></footer></div></dialog>
      <dialog id="basket-dialog" aria-labelledby="basket-dialog-title"><div class="dialog-frame"><header class="dialog-head"><h2 id="basket-dialog-title">Basket</h2><button data-action="close-dialog" data-dialog="basket-dialog" aria-label="Close basket">×</button></header><div class="dialog-body" id="basket-dialog-content"></div></div></dialog>
      <dialog id="package-dialog" aria-labelledby="package-dialog-title"><div class="dialog-frame"><header class="dialog-head"><h2 id="package-dialog-title">Package editor</h2><button data-action="close-dialog" data-dialog="package-dialog" aria-label="Close package editor">×</button></header><div class="dialog-body" id="package-editor-content"></div><footer class="dialog-foot" id="package-editor-footer"></footer></div></dialog>
      <dialog id="confirm-dialog" aria-labelledby="confirm-title"><div class="dialog-frame"><header class="dialog-head"><h2 id="confirm-title">Confirm action</h2></header><div class="dialog-body"><p id="confirm-copy"></p></div><footer class="dialog-foot"><button data-action="close-dialog" data-dialog="confirm-dialog">Cancel</button><button class="danger" id="confirm-button" data-action="confirm-action">Confirm</button></footer></div></dialog>
      <template id="recovery-actions-template"><div class="recovery-actions"><button data-action="resolve-manual" data-resolution="found_succeeded">Found succeeded</button><button data-action="resolve-manual" data-resolution="found_failed_or_cancelled">Found failed/cancelled</button><button data-action="resolve-manual" data-resolution="still_unknown">Still unknown</button></div></template>`;
    this.shadowRoot.addEventListener("click", (event) => this._onClick(event));
    this.shadowRoot.addEventListener("input", (event) => this._onInput(event));
    this.shadowRoot.addEventListener("change", (event) => this._onChange(event));
    this.shadowRoot.addEventListener("keydown", (event) => this._onKeydown(event));
    this.shadowRoot.querySelectorAll("dialog").forEach((dialog) => {
      dialog.addEventListener("cancel", (event) => { event.preventDefault(); this._closeDialog(dialog.id); });
      dialog.addEventListener("close", () => this._afterDialogClosed(dialog.id));
    });
    this._rendered = true;
  }

  _updateEnvironment() {
    const language = this._hass?.locale?.language || this._hass?.language || globalThis.navigator?.language || "en";
    this._locale = language;
    const rtlLanguages = new Set(["ar", "fa", "he", "ur"]);
    const explicitDirection = this._hass?.locale?.textDirection;
    this._direction = explicitDirection || (rtlLanguages.has(String(language).split("-")[0]) ? "rtl" : "ltr");
    this.setAttribute("dir", this._direction);
  }

  _command(operation) { return `glovo/ordering/${operation}`; }
  _request(operation, request = {}) { return this._hass.callWS({ type: this._command(operation), ...request }); }
  _withGeneration() { return { generation: this._generation }; }
  _storeAllowsOrdering() { const store = this._model.context.store; return store?.isOpen === true && store?.orderingAvailable === true; }
  _el(tag, className, text) { const element = document.createElement(tag); if (className) element.className = className; if (text !== undefined) element.textContent = text; return element; }
  _button(text, action, className = "") { const button = this._el("button", className, text); button.type = "button"; button.dataset.action = action; return button; }

  _setLifecycle(status, message, kind = "status") {
    this._model.lifecycle = { status, message, kind };
    this._renderStatus();
  }

  _renderStatus() {
    const status = this.shadowRoot.querySelector("#status");
    if (!status) return;
    status.textContent = this._model.lifecycle.message || "";
    status.dataset.kind = this._model.lifecycle.kind;
    this.shadowRoot.querySelector("#workspace").setAttribute("aria-busy", String(this._model.lifecycle.status === "booting"));
  }

  _beginLatestRead(key) {
    const token = (this._readEpochs.get(key) || 0) + 1;
    this._readEpochs.set(key, token);
    return token;
  }

  _isLatestRead(key, token, capturedGeneration) {
    return this._readEpochs.get(key) === token && capturedGeneration === this._generation;
  }

  async _runSingleFlight(key, operation) {
    if (this._singleFlights.has(key)) return this._singleFlights.get(key);
    const flight = Promise.resolve().then(operation).finally(() => this._singleFlights.delete(key));
    this._singleFlights.set(key, flight);
    return flight;
  }

  async _bootstrap() {
    this._setLifecycle("booting", "Loading safe ordering status…");
    try {
      const state = await this._request("state");
      await this._applyState(state, true);
    } catch (_error) {
      this._model.lifecycle = { status: "error", message: "Ordering status is unavailable. Nothing was submitted.", kind: "error" };
      this._renderAll();
    }
  }

  async _applyState(state, initial = false) {
    const nextGeneration = Number.isInteger(state?.generation) && state.generation > 0 ? state.generation : undefined;
    if (nextGeneration && nextGeneration !== this._generation) {
      this._generation = nextGeneration;
      this._resetEphemeralGeneration("Ordering generation changed; local handles and quote authority were discarded.");
    }
    this._model.capability = {
      liveOrderingAvailable: state?.liveOrderingAvailable === true || state?.orderingAvailable === true,
      liveCheckoutAvailable: state?.liveCheckoutAvailable === true,
      integrityFault: state?.integrityFault === true,
      enabled: state?.enabled === true,
      mockOnly: state?.mockOnly === true,
    };
    if (state?.integrityFault === true) {
      this._model.lifecycle = { status: "blocked", message: "Ordering has a permanent integrity fault and remains blocked.", kind: "error" };
      this._model.recovery = { kind: "integrity" };
      this._renderAll();
      return;
    }
    if (this._stateRequiresRecovery(state)) {
      await this._loadRecovery();
      return;
    }
    if (!this._model.capability.liveOrderingAvailable) {
      this._model.lifecycle = { status: "disabled", message: "Live ordering preparation is unavailable. No live primitives were loaded.", kind: "warning" };
      this._renderAll();
      return;
    }
    this._model.recovery = null;
    this._model.lifecycle = { status: "ready", message: initial ? "Choose a saved address, then look up an explicit store." : "Ordering authority refreshed safely.", kind: "status" };
    this._renderAll();
    await Promise.all([this._loadAddresses(), this._refreshBasket(), this._loadLibrary()]);
  }

  _stateRequiresRecovery(state) {
    if (state.manualCheckRequired) return true;
    return state.orderingBlocked === true;
  }

  _resetEphemeralGeneration(reason) {
    this._readEpochs.clear();
    this._closeAllDialogs(false);
    this._invalidateAuthority("");
    this._model.overlay = null;
    this._confirmation = null;
    this._model.context = { addresses: [], addressHandle: "", addressLabel: "", storeInput: "", stores: [], store: null };
    this._model.menu = { status: "idle", products: [], query: "", filter: "all", error: "" };
    this._model.draft = { lines: [], dirty: false };
    this._model.basket = { status: "empty", revision: 0, itemCount: 0, currency: "", providerTotal: null, linesAvailable: true };
    this._model.payments = { status: "idle", items: [], selected: "" };
    this._model.library = { status: "idle", storeRevision: 0, addresses: [], packages: [], aliasEditor: null, packageEditor: null, error: "" };
    if (reason) this._model.lifecycle = { status: "ready", message: reason, kind: "warning" };
  }

  _invalidateAuthority(reason = "Selection changed; quote confirmation was discarded.") {
    this._clearExpiryTimer();
    this._model.quote = null;
    this._ackText = "";
    if (reason && this._rendered) this._setLifecycle("ready", reason, "warning");
  }

  _clearExpiryTimer() {
    if (this._expiryTimer) globalThis.clearInterval(this._expiryTimer);
    this._expiryTimer = undefined;
  }

  _renderAll() {
    if (!this._rendered) return;
    this._renderStatus();
    this._renderCapability();
    const blocking = this._model.lifecycle.status === "blocked" || this._model.lifecycle.status === "disabled" || this._model.lifecycle.status === "error";
    this.shadowRoot.querySelector("#blocking").hidden = !blocking;
    this.shadowRoot.querySelector("#app").hidden = blocking || this._model.lifecycle.status === "booting";
    this._renderBlocking();
    if (!blocking) {
      this._renderContext();
      this._renderCurrentView();
      this._renderBasketSurfaces();
    }
  }

  _renderCapability() {
    const host = this.shadowRoot.querySelector("#capability-badges");
    host.replaceChildren();
    const prep = this._el("span", "badge", this._model.capability.liveOrderingAvailable ? "Preparation enabled" : "Preparation unavailable");
    prep.classList.add(this._model.capability.liveOrderingAvailable ? "success" : "warning");
    host.append(prep);
    if (!this._model.capability.liveCheckoutAvailable) host.append(this._el("span", "badge quiet", "Paid checkout unavailable"));
  }

  _renderBlocking() {
    const host = this.shadowRoot.querySelector("#blocking");
    host.replaceChildren();
    if (host.hidden) return;
    if (this._model.recovery?.kind === "manual") {
      host.append(this._el("h1", "", "MANUAL_CHECK_REQUIRED"));
      host.append(this._el("p", "", "Check Glovo manually. Do not place this order again. This panel will not retry or re-submit an order whose outcome is unknown."));
      if (this._model.recovery.attempt) {
        const attempt = this._model.recovery.attempt;
        const summary = this._el("p", "quiet");
        summary.textContent = [attempt.storeDisplayName, attempt.itemSummary, this._formatMinor(attempt.amountMinor, attempt.currency), attempt.maskedPaymentLabel, attempt.maskedAddressAlias].filter(Boolean).join(" · ");
        host.append(summary);
        const label = this._el("label", "choice");
        const checkbox = this._el("input"); checkbox.type = "checkbox"; checkbox.id = "manual-ack";
        label.append(checkbox, this._el("span", "choice-label", "I checked Glovo manually and understand this only records a local conclusion; it never re-submits."));
        host.append(label);
        host.append(this.shadowRoot.querySelector("#recovery-actions-template").content.cloneNode(true));
      } else {
        host.append(this._el("p", "quiet", "Manual recovery details are unavailable. Ordering remains blocked."));
      }
      const status = this._button("Check safe checkout status", "check-checkout-status");
      host.append(status);
      return;
    }
    const title = this._model.recovery?.kind === "integrity" ? "Ordering blocked" : "Ordering unavailable";
    host.append(this._el("h1", "", title), this._el("p", "", this._model.lifecycle.message));
    const retry = this._button("Check status again", "retry-state", "primary");
    host.append(retry);
  }

  _renderContext() {
    const host = this.shadowRoot.querySelector("#context-content");
    host.replaceChildren();
    const grid = this._el("div", "context-grid");
    const addressField = this._el("label", "field");
    addressField.append(this._el("span", "", "Saved delivery address"));
    const addressRow = this._el("div", "field-row");
    const contextLocked = this._model.basket.itemCount > 0;
    const address = this._el("select"); address.id = "address-select"; address.setAttribute("aria-label", "Saved delivery address"); address.disabled = contextLocked;
    address.add(new Option(this._model.context.addresses.length ? "Select a saved address" : "No saved addresses", ""));
    this._model.context.addresses.forEach((item) => address.add(new Option(item.fullAddress || item.label || "Saved address", item.key || item.addressHandle)));
    address.value = this._model.context.addressHandle;
    const refresh = this._button("Refresh", "refresh-addresses"); refresh.setAttribute("aria-label", "Refresh saved addresses");
    addressRow.append(address, refresh); addressField.append(addressRow);
    if (this._model.context.addressHandle && this._model.context.addressLabel) {
      const selectedAddress = this._el("div", "selected-address quiet", this._model.context.addressLabel);
      selectedAddress.setAttribute("aria-label", "Selected full delivery address");
      addressField.append(selectedAddress);
    }
    const storeField = this._el("label", "field");
    storeField.append(this._el("span", "", "Explicit store slug or URL"));
    const storeRow = this._el("div", "field-row");
    const storeInput = this._el("input"); storeInput.id = "store-input"; storeInput.autocomplete = "off"; storeInput.placeholder = "Store slug or URL"; storeInput.value = this._model.context.storeInput; storeInput.disabled = contextLocked;
    const lookup = this._button("Look up", "lookup-store", "primary");
    lookup.disabled = !this._model.context.addressHandle || contextLocked;
    storeRow.append(storeInput, lookup); storeField.append(storeRow);
    grid.append(addressField, storeField);
    if (this._model.context.stores.length) {
      const storeChoice = this._el("label", "field"); storeChoice.append(this._el("span", "", "Matching store"));
      const select = this._el("select"); select.id = "store-select"; select.disabled = contextLocked; select.add(new Option("Select explicit store", ""));
      this._model.context.stores.forEach((item) => select.add(new Option(item.label || "Store", item.storeHandle)));
      select.value = this._model.context.store?.storeHandle || ""; storeChoice.append(select); grid.append(storeChoice);
    }
    if (this._model.context.store) {
      const summary = this._el("div", "store-summary");
      summary.append(this._el("strong", "", this._model.context.store.label || "Selected store"));
      if (this._model.context.store.category) summary.append(this._el("span", "quiet", this._model.context.store.category));
      for (const key of ["deliveryFee", "serviceFee", "minimumOrder"]) if (this._model.context.store[key]) summary.append(this._el("span", "quiet", String(this._model.context.store[key])));
      grid.append(summary);
      if (!this._storeAllowsOrdering()) {
        const warning = this._el("div", "closed-store-warning"); warning.setAttribute("role", "alert");
        warning.append(this._el("strong", "", "Store closed — browsing only"), this._el("span", "", "This menu is visible for reference, but this store is not accepting orders."));
        grid.append(warning);
      }
    }
    if (contextLocked) grid.append(this._el("div", "store-summary warning", "Address and store switching are locked while a provider basket exists. Keep it, or clear the provider basket with explicit confirmation before switching."));
    host.append(grid);
  }

  _renderCurrentView() {
    const view = this._model.library.activeView === "packages" ? "packages" : "menu";
    this.shadowRoot.querySelectorAll("[data-action='nav-view']").forEach((button) => button.setAttribute("aria-current", button.dataset.view === view ? "page" : "false"));
    if (view === "packages") this._renderPackages(); else this._renderMenu();
  }

  _normalizeSearch(value) { return String(value || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLocaleLowerCase(); }

  _filteredProducts() {
    const query = this._normalizeSearch(this._model.menu.query);
    return this._model.menu.products.filter((product) => {
      const searchable = [product.label, ...(product.optionGroups || []).flatMap((group) => [group.label, ...(group.options || []).map((option) => option.label)])].map((value) => this._normalizeSearch(value)).join(" ");
      if (query && !searchable.includes(query)) return false;
      if (this._model.menu.filter === "customizable" && !(product.optionGroups || []).length) return false;
      if (this._model.menu.filter === "basket" && !this._model.draft.lines.some((line) => line.productHandle === product.productHandle)) return false;
      return true;
    });
  }

  _renderMenu() {
    const host = this.shadowRoot.querySelector("#main-content");
    host.replaceChildren();
    const toolbar = this._el("div", "toolbar");
    const searchRow = this._el("div", "search-row");
    const search = this._el("input"); search.type = "search"; search.id = "menu-search"; search.placeholder = "Search products and options"; search.value = this._model.menu.query; search.setAttribute("aria-label", "Search menu");
    const clear = this._button("Clear", "clear-search"); clear.disabled = !this._model.menu.query;
    searchRow.append(search, clear);
    const filters = this._el("div", "filter-row"); filters.setAttribute("aria-label", "Menu filters");
    [["all", "All"], ["customizable", "Customizable"], ["basket", "In basket"]].forEach(([value, label]) => { const button = this._button(label, "set-filter"); button.dataset.filter = value; button.setAttribute("aria-pressed", String(this._model.menu.filter === value)); filters.append(button); });
    const filtered = this._filteredProducts();
    const count = this._el("p", "result-count", `${filtered.length} of ${this._model.menu.products.length} products`);
    toolbar.append(searchRow, filters, count); host.append(toolbar);
    if (this._model.menu.status === "loading") {
      const skeletons = this._el("div", "skeleton-grid"); skeletons.setAttribute("aria-label", "Loading menu");
      for (let index = 0; index < 6; index += 1) skeletons.append(this._el("div", "skeleton"));
      host.append(skeletons); return;
    }
    if (this._model.menu.status === "error") {
      host.append(this._emptyState("Menu unavailable", this._model.menu.error || "The menu could not be loaded safely.", "Try menu again", "retry-menu")); return;
    }
    if (!this._model.context.store) {
      host.append(this._emptyState("Choose an explicit store", "Select a saved delivery address and look up a store slug or URL. Broad discovery is unsupported.")); return;
    }
    if (!this._model.menu.products.length) {
      host.append(this._emptyState("This menu is empty", "The selected store returned no orderable products.")); return;
    }
    if (!filtered.length) {
      host.append(this._emptyState("No products match", "Reset search and filters to see the full menu.", "Reset results", "reset-results")); return;
    }
    const grid = this._el("div", "product-grid");
    filtered.forEach((product) => grid.append(this._renderProductCard(product)));
    host.append(grid);
  }

  _emptyState(title, copy, actionLabel, action) {
    const box = this._el("div", "empty-state"); box.append(this._el("h2", "", title), this._el("p", "quiet", copy));
    if (actionLabel && action) box.append(this._button(actionLabel, action, "primary"));
    return box;
  }

  _renderProductCard(product) {
    const card = this._el("article", "product-card");
    const top = this._el("div", "product-title");
    top.append(this._el("h3", "", product.label || "Menu item"), this._el("span", "price", this._formatMinor(product.priceMinor, product.currency)));
    card.append(top);
    const customizable = (product.optionGroups || []).length > 0;
    if (customizable) card.append(this._el("span", "badge", "Customizable"));
    const line = this._model.draft.lines.find((item) => item.productHandle === product.productHandle);
    if (line) {
      const selected = line.options.flatMap((group) => group.optionLabels || []).join(", ");
      card.append(this._el("div", "quiet", selected || "No optional extras"));
    } else if (!this._storeAllowsOrdering()) card.append(this._el("div", "quiet", "Visible for reference while this store is closed."));
    else card.append(this._el("div", "quiet", customizable ? "Choose required and optional selections." : "Add directly to your local draft."));
    const actions = this._el("div", "card-actions");
    if (!this._storeAllowsOrdering()) {
      const browseOnly = this._button("Browsing only", ""); browseOnly.disabled = true; actions.append(browseOnly);
    } else if (line) {
      actions.append(this._lineStepper(line));
      const edit = this._button(customizable ? "Edit" : "Remove", customizable ? "customize-product" : "remove-line"); edit.dataset.productHandle = product.productHandle; actions.append(edit);
    } else {
      const add = this._button(customizable ? "Customize" : "Add", customizable ? "customize-product" : "add-simple", "primary"); add.dataset.productHandle = product.productHandle; actions.append(add);
    }
    card.append(actions); return card;
  }

  _lineStepper(line) {
    const stepper = this._el("div", "stepper"); stepper.setAttribute("aria-label", `Quantity for ${line.label || "item"}`);
    const minus = this._button("−", "step-line"); minus.dataset.productHandle = line.productHandle; minus.dataset.delta = "-1"; minus.setAttribute("aria-label", "Decrease quantity");
    const output = this._el("output", "", String(line.quantity));
    const plus = this._button("+", "step-line"); plus.dataset.productHandle = line.productHandle; plus.dataset.delta = "1"; plus.setAttribute("aria-label", "Increase quantity"); plus.disabled = line.quantity >= 50 || this._draftQuantity() >= 100;
    stepper.append(minus, output, plus); return stepper;
  }

  _formatMinor(amountMinor, currency) {
    if (!Number.isInteger(Number(amountMinor)) || !currency) return "Price unavailable";
    try { return new Intl.NumberFormat(this._locale || "en", { style: "currency", currency: String(currency) }).format(Number(amountMinor) / 100); }
    catch (_error) { return `${Number(amountMinor)} ${String(currency)}`; }
  }

  async _loadAddresses() {
    if (!this._generation) return;
    const token = this._beginLatestRead("addresses"); const capturedGeneration = this._generation;
    try {
      const response = await this._request("live/addresses", this._withGeneration());
      if (!this._isLatestRead("addresses", token, capturedGeneration)) return;
      this._model.context.addresses = Array.isArray(response?.addresses) ? response.addresses : [];
      if (!this._model.context.addresses.some((item) => (item.key || item.addressHandle) === this._model.context.addressHandle)) {
        this._model.context.addressHandle = ""; this._model.context.addressLabel = ""; this._resetCatalog();
      }
      this._renderContext();
    } catch (_error) {
      if (this._isLatestRead("addresses", token, capturedGeneration)) this._setLifecycle("ready", "Saved addresses are unavailable. Use Refresh to recover manually.", "error");
    }
  }

  _storeSlug() {
    const raw = this._model.context.storeInput.trim();
    if (!raw) return "";
    try { const parsed = new URL(raw); return parsed.pathname.split("/").filter(Boolean).pop() || ""; }
    catch (_error) { return raw; }
  }

  async _lookupStore() {
    const storeSlug = this._storeSlug(); const addressHandle = this._model.context.addressHandle;
    if (!addressHandle) { this._setLifecycle("ready", "Select a saved address before looking up a store.", "warning"); return; }
    if (!storeSlug) { this._setLifecycle("ready", "Enter an explicit store slug or URL. Broad discovery is unsupported.", "warning"); return; }
    const token = this._beginLatestRead("stores"); const capturedGeneration = this._generation;
    this._invalidateAuthority("");
    this._model.context.stores = [];
    this._model.context.store = null;
    this._model.menu = { status: "idle", products: [], query: "", filter: "all", error: "" };
    this._model.draft = { lines: [], dirty: false };
    if (!this._model.basket.itemCount) this._model.basket.status = "empty";
    this._setLifecycle("ready", "Looking up the explicit store…");
    this._renderAll();
    try {
      const response = await this._request("live/stores", { ...this._withGeneration(), storeSlug, addressHandle });
      if (!this._isLatestRead("stores", token, capturedGeneration)) return;
      this._model.context.stores = Array.isArray(response?.stores) ? response.stores : [];
      if (this._model.context.stores.length === 1) {
        this._setLifecycle("ready", "Exact store found; loading its current menu…");
        await this._loadStoreMenu(this._model.context.stores[0]);
        return;
      }
      this._setLifecycle("ready", this._model.context.stores.length ? "Select the exact store to load its menu." : "No matching store was returned.", this._model.context.stores.length ? "status" : "warning");
      this._renderAll();
    } catch (_error) {
      if (this._isLatestRead("stores", token, capturedGeneration)) {
        this._model.menu.status = "error";
        this._model.menu.error = "The explicit store lookup failed. Nothing was submitted.";
        this._setLifecycle("ready", "The explicit store is unavailable. No provider mutation was attempted.", "error");
        this._renderAll();
      }
    }
  }

  _resetCatalog() {
    this._invalidateAuthority("");
    this._model.context.stores = []; this._model.context.store = null;
    this._model.menu = { status: "idle", products: [], query: "", filter: "all", error: "" };
    this._model.payments = { status: "idle", items: [], selected: "" };
  }

  async _loadStoreMenu(store) {
    if (!store || !this._model.context.addressHandle) return;
    this._model.context.store = store; this._model.menu.status = "loading"; this._model.menu.error = ""; this._invalidateAuthority(""); this._renderAll();
    const token = this._beginLatestRead("menu"); const capturedGeneration = this._generation;
    try {
      const response = await this._request("live/store_menu", { ...this._withGeneration(), storeHandle: store.storeHandle, addressHandle: this._model.context.addressHandle });
      if (!this._isLatestRead("menu", token, capturedGeneration)) return;
      if (response?.storeHandle !== store.storeHandle || response?.isOpen !== store.isOpen || response?.orderingAvailable !== store.orderingAvailable) throw new Error("stale menu association");
      this._model.context.store = { ...store, isOpen: response.isOpen, orderingAvailable: response.orderingAvailable };
      this._model.menu.status = "ready"; this._model.menu.products = Array.isArray(response?.products) ? response.products : [];
      this._setLifecycle("ready", this._storeAllowsOrdering() ? "Menu loaded. Add items to the local draft; nothing syncs until you choose Sync basket." : "Closed-store menu loaded for browsing only. Basket and package preparation are unavailable.", this._storeAllowsOrdering() ? "status" : "warning");
      this._renderAll();
    } catch (_error) {
      if (!this._isLatestRead("menu", token, capturedGeneration)) return;
      this._model.menu.status = "error"; this._model.menu.error = "The selected menu is unavailable. Nothing was submitted.";
      this._setLifecycle("ready", "Menu loading failed safely. Use Try menu again; no mutation will be retried.", "error"); this._renderAll();
    }
  }

  _openCustomizer(productHandle, trigger) {
    if (!this._storeAllowsOrdering()) { this._setLifecycle("ready", "This closed-store menu is browse-only. No draft or provider basket was changed.", "warning"); return; }
    const product = this._model.menu.products.find((item) => item.productHandle === productHandle);
    if (!product) return;
    const existing = this._model.draft.lines.find((line) => line.productHandle === productHandle);
    const selections = new Map();
    (product.optionGroups || []).forEach((group) => {
      const selectedExisting = existing?.options.find((item) => item.groupHandle === group.groupHandle)?.optionHandles;
      const defaults = selectedExisting || (group.options || []).filter((option) => option.selected === true).map((option) => option.optionHandle);
      selections.set(group.groupHandle, new Set(defaults));
    });
    this._model.overlay = { type: "customizer", product, quantity: existing?.quantity || 1, selections, errors: new Map(), editing: Boolean(existing) };
    this._renderCustomizer(); this._openDialog("customizer-dialog", trigger);
  }

  _groupRule(group) {
    const minimum = Number(group.min) || 0; const maximum = Number(group.max) || 0;
    if (minimum === maximum && minimum > 0) return `Choose exactly ${minimum}.`;
    if (minimum > 0) return `Choose ${minimum}–${maximum}. Required.`;
    if (maximum === 1) return "Choose up to 1.";
    return `Choose up to ${maximum}.`;
  }

  _renderCustomizer() {
    const overlay = this._model.overlay;
    if (!overlay || overlay.type !== "customizer") return;
    const product = overlay.product;
    this.shadowRoot.querySelector("#customizer-title").textContent = product.label || "Customize item";
    const host = this.shadowRoot.querySelector("#customizer-content"); host.replaceChildren();
    const form = this._el("form", "customizer-form"); form.noValidate = true;
    form.append(this._el("div", "price", this._formatMinor(product.priceMinor, product.currency)));
    (product.optionGroups || []).forEach((group, groupIndex) => {
      const fieldset = this._el("fieldset"); fieldset.dataset.groupHandle = group.groupHandle;
      const error = overlay.errors.get(group.groupHandle); fieldset.dataset.invalid = String(Boolean(error));
      const legend = this._el("legend", "", group.label || `Option group ${groupIndex + 1}`); fieldset.append(legend, this._el("div", "group-rule", this._groupRule(group)));
      const selected = overlay.selections.get(group.groupHandle) || new Set();
      const type = group.max === 1 ? "radio" : "checkbox";
      if (type === "radio" && Number(group.min) === 0) {
        const label = this._el("label", "choice"); const input = this._el("input"); input.type = "radio"; input.name = `option-group-${groupIndex}`; input.dataset.groupHandle = group.groupHandle; input.dataset.optionHandle = ""; input.checked = selected.size === 0;
        label.append(input, this._el("span", "choice-label", "No selection")); fieldset.append(label);
      }
      (group.options || []).forEach((option) => {
        const label = this._el("label", "choice"); const input = this._el("input");
        input.type = type; input.name = `option-group-${groupIndex}`; input.dataset.groupHandle = group.groupHandle; input.dataset.optionHandle = option.optionHandle; input.checked = selected.has(option.optionHandle);
        if (type === "checkbox" && selected.size >= Number(group.max) && !input.checked) input.disabled = true;
        const copy = this._el("span", "choice-label", option.label || "Option");
        const delta = this._el("span", "price", Number(option.priceMinor) === 0 ? "Included" : `+${this._formatMinor(option.priceMinor, option.currency || product.currency)}`);
        label.append(input, copy, delta); fieldset.append(label);
      });
      if (error) fieldset.append(this._el("div", "field-error", error));
      form.append(fieldset);
    });
    const quantity = this._el("div", "custom-quantity"); quantity.append(this._el("strong", "", "Quantity"));
    const minus = this._button("−", "step-custom"); minus.dataset.delta = "-1"; minus.setAttribute("aria-label", "Decrease quantity");
    const input = this._el("input"); input.id = "custom-quantity"; input.type = "number"; input.min = "1"; input.max = "50"; input.step = "1"; input.inputMode = "numeric"; input.value = String(overlay.quantity); input.setAttribute("aria-label", "Item quantity");
    const plus = this._button("+", "step-custom"); plus.dataset.delta = "1"; plus.setAttribute("aria-label", "Increase quantity");
    quantity.append(minus, input, plus); form.append(quantity);
    if (overlay.errors.get("quantity")) form.append(this._el("div", "field-error", overlay.errors.get("quantity")));
    host.append(form);
    const footer = this.shadowRoot.querySelector("#customizer-footer"); footer.replaceChildren();
    footer.append(this._button("Cancel", "close-dialog")); footer.lastElementChild.dataset.dialog = "customizer-dialog";
    footer.append(this._button(overlay.editing ? "Update item" : "Add to basket", "commit-customization", "primary"));
  }

  _validateCustomization(customization) {
    const errors = new Map();
    const quantity = Number(customization.quantity);
    if (!Number.isInteger(quantity) || quantity < 1 || quantity > 50) errors.set("quantity", "Quantity must be a whole number from 1 to 50.");
    (customization.product.optionGroups || []).forEach((group) => {
      const count = (customization.selections.get(group.groupHandle) || new Set()).size;
      const minimum = Number(group.min) || 0; const maximum = Number(group.max) || 0;
      if (count < minimum) errors.set(group.groupHandle, minimum === maximum ? `Choose exactly ${minimum}.` : `Choose at least ${minimum}.`);
      else if (count > maximum) errors.set(group.groupHandle, `Choose no more than ${maximum}.`);
    });
    const existingQuantity = this._model.draft.lines.filter((line) => line.productHandle !== customization.product.productHandle).reduce((sum, line) => sum + line.quantity, 0);
    if (Number.isInteger(quantity) && existingQuantity + quantity > 100) errors.set("quantity", "The draft may contain at most 100 items in total.");
    return { valid: errors.size === 0, errors };
  }

  _commitCustomization() {
    if (!this._storeAllowsOrdering()) { this._model.overlay = null; this._setLifecycle("ready", "This closed-store menu is browse-only. No draft or provider basket was changed.", "warning"); return; }
    const overlay = this._model.overlay;
    if (!overlay || overlay.type !== "customizer") return;
    const validation = this._validateCustomization(overlay); overlay.errors = validation.errors;
    if (!validation.valid) { this._renderCustomizer(); this._focusFirstError(); return; }
    const product = overlay.product;
    const options = (product.optionGroups || []).map((group) => {
      const handles = Array.from(overlay.selections.get(group.groupHandle) || []);
      const selectedOptions = (group.options || []).filter((option) => handles.includes(option.optionHandle));
      return { groupHandle: group.groupHandle, groupLabel: group.label || "Options", optionHandles: handles, optionLabels: selectedOptions.map((option) => option.label || "Option"), deltaMinor: selectedOptions.reduce((sum, option) => sum + (Number(option.priceMinor) || 0), 0), currency: selectedOptions[0]?.currency || product.currency };
    });
    const line = { productHandle: product.productHandle, label: product.label || "Menu item", quantity: Number(overlay.quantity), unitPriceMinor: Number(product.priceMinor) || 0, currency: product.currency || "", options };
    const index = this._model.draft.lines.findIndex((item) => item.productHandle === line.productHandle);
    if (index >= 0) this._model.draft.lines.splice(index, 1, line); else this._model.draft.lines.push(line);
    this._draftChanged("Local draft updated. Choose Sync basket when ready.");
    this._closeDialog("customizer-dialog");
  }

  _focusFirstError() {
    const invalid = this.shadowRoot.querySelector("#customizer-content fieldset[data-invalid='true']");
    if (invalid) { invalid.tabIndex = -1; invalid.focus(); return; }
    this.shadowRoot.querySelector("#custom-quantity")?.focus();
  }

  _addSimple(productHandle) {
    if (!this._storeAllowsOrdering()) { this._setLifecycle("ready", "This closed-store menu is browse-only. No draft or provider basket was changed.", "warning"); return; }
    const product = this._model.menu.products.find((item) => item.productHandle === productHandle); if (!product) return;
    const existing = this._model.draft.lines.find((line) => line.productHandle === productHandle);
    if (existing) { this._stepLine(productHandle, 1); return; }
    if (this._draftQuantity() >= 100) { this._setLifecycle("ready", "The local draft is limited to 100 items.", "warning"); return; }
    this._model.draft.lines.push({ productHandle, label: product.label || "Menu item", quantity: 1, unitPriceMinor: Number(product.priceMinor) || 0, currency: product.currency || "", options: [] });
    this._draftChanged("Item added to the local draft. Nothing has been synced yet.");
  }

  _stepLine(productHandle, delta) {
    if (!this._storeAllowsOrdering()) { this._setLifecycle("ready", "This closed-store menu is browse-only. No draft or provider basket was changed.", "warning"); return; }
    const line = this._model.draft.lines.find((item) => item.productHandle === productHandle); if (!line) return;
    const next = line.quantity + Number(delta);
    if (next <= 0) { this._removeLine(productHandle); return; }
    if (next > 50 || (delta > 0 && this._draftQuantity() >= 100)) { this._setLifecycle("ready", "Quantity must remain within 1–50 per item and 100 total.", "warning"); return; }
    line.quantity = next; this._draftChanged("Draft quantity changed. Sync basket to update the provider.");
  }

  _removeLine(productHandle) {
    this._model.draft.lines = this._model.draft.lines.filter((line) => line.productHandle !== productHandle);
    this._draftChanged("Item removed from the local draft. Provider basket was not changed.");
  }

  _draftChanged(message) {
    this._model.draft.dirty = true;
    this._model.basket.status = this._model.draft.lines.length ? "draft" : (this._model.basket.itemCount ? "provider-review" : "empty");
    this._invalidateAuthority(""); this._setLifecycle("ready", message); this._renderAll();
  }

  _draftQuantity() { return this._model.draft.lines.reduce((sum, line) => sum + Number(line.quantity || 0), 0); }

  _serializeBasket() {
    return this._model.draft.lines.map((line) => ({ productHandle: line.productHandle, quantity: line.quantity, options: line.options.map((group) => ({ groupHandle: group.groupHandle, optionHandles: [...group.optionHandles] })) }));
  }

  _estimatedSubtotal() {
    if (!this._model.draft.lines.length) return { amountMinor: 0, currency: "" };
    const currency = this._model.draft.lines[0].currency;
    if (!currency || this._model.draft.lines.some((line) => line.currency !== currency)) return { amountMinor: null, currency: "" };
    return { amountMinor: this._model.draft.lines.reduce((sum, line) => sum + (line.unitPriceMinor + line.options.reduce((optionSum, group) => optionSum + (group.deltaMinor || 0), 0)) * line.quantity, 0), currency };
  }

  _renderBasketSurfaces() {
    this._renderBasketInto(this.shadowRoot.querySelector("#basket-panel"));
    this._renderBasketInto(this.shadowRoot.querySelector("#basket-dialog-content"));
    const mobile = this.shadowRoot.querySelector("#mobile-basket-bar"); mobile.hidden = this.shadowRoot.querySelector("#app").hidden;
    const content = this.shadowRoot.querySelector("#mobile-basket-content"); content.replaceChildren();
    const summary = this._el("div"); summary.append(this._el("strong", "", `${this._draftQuantity()} item${this._draftQuantity() === 1 ? "" : "s"}`), this._el("div", "quiet", this._basketStatusLabel()));
    content.append(summary, this._button("View basket", "open-basket", "primary"));
  }

  _basketStatusLabel() {
    const labels = { empty: "Empty", draft: "Draft · not synced", syncing: "Syncing…", synced: `Provider-synced · revision ${this._model.basket.revision}`, "provider-review": "Provider state needs review", "remote-only": "Remote lines unavailable" };
    return labels[this._model.basket.status] || "Draft";
  }

  _renderBasketInto(host) {
    host.replaceChildren();
    const head = this._el("div", "basket-head"); head.append(this._el("h2", "", "Basket"), this._el("span", "badge", this._basketStatusLabel())); host.append(head);
    if (!this._model.draft.lines.length) {
      const copy = this._model.basket.itemCount > 0 ? `The provider reports ${this._model.basket.itemCount} item(s), but editable line details are unavailable after reload.` : "Your local draft is empty.";
      host.append(this._el("p", "quiet", copy));
    } else {
      const lines = this._el("div", "basket-lines");
      this._model.draft.lines.forEach((line) => {
        const row = this._el("article", "basket-line"); const top = this._el("div", "line-head"); top.append(this._el("h3", "", line.label), this._el("strong", "price", this._formatMinor((line.unitPriceMinor + line.options.reduce((sum, group) => sum + (group.deltaMinor || 0), 0)) * line.quantity, line.currency))); row.append(top);
        const optionCopy = line.options.flatMap((group) => group.optionLabels || []).join(", "); if (optionCopy) row.append(this._el("div", "line-options", optionCopy));
        const actions = this._el("div", "card-actions"); actions.append(this._lineStepper(line));
        const product = this._model.menu.products.find((item) => item.productHandle === line.productHandle);
        if ((product?.optionGroups || []).length) { const edit = this._button("Edit", "customize-product"); edit.dataset.productHandle = line.productHandle; actions.append(edit); }
        const remove = this._button("Remove", "remove-line", "danger"); remove.dataset.productHandle = line.productHandle; actions.append(remove); row.append(actions); lines.append(row);
      }); host.append(lines);
    }
    const estimated = this._estimatedSubtotal(); const totals = this._el("div", "totals");
    const estimateRow = this._el("div", "total-row"); estimateRow.append(this._el("span", "", "Estimated catalog subtotal"), this._el("strong", "", estimated.amountMinor === null ? "Unavailable" : this._formatMinor(estimated.amountMinor, estimated.currency))); totals.append(estimateRow);
    totals.append(this._el("div", "authority-note", "This estimate is non-authoritative and excludes provider fees or changes. Final total is available only from an authoritative quote."));
    if (this._model.basket.providerTotal !== null) { const provider = this._el("div", "total-row"); provider.append(this._el("span", "", "Authoritative provider total"), this._el("strong", "", this._formatMinor(this._model.basket.providerTotal, this._model.basket.currency))); totals.append(provider); }
    host.append(totals);
    const actions = this._el("div", "basket-actions");
    const sync = this._button(this._model.basket.status === "syncing" ? "Syncing…" : "Sync basket", "sync-basket", "primary"); sync.disabled = !this._model.draft.lines.length || this._model.basket.status === "syncing" || !this._model.context.store || !this._model.context.addressHandle || !this._storeAllowsOrdering(); actions.append(sync);
    if (this._model.basket.revision > 0 || this._model.basket.itemCount > 0) actions.append(this._button("Clear provider basket", "ask-clear-basket", "danger"));
    actions.append(this._button("Refresh provider status", "refresh-basket"));
    if (this._model.basket.status === "synced" && this._model.basket.itemCount > 0) {
      if (this._model.payments.status !== "ready") actions.append(this._button("Load provider-selected card", "load-payments"));
      else {
        const payment = this._el("select"); payment.dataset.role = "payment-select"; payment.setAttribute("aria-label", "Provider-selected masked card");
        payment.add(new Option("Select provider-selected masked card", ""));
        this._model.payments.items.forEach((item) => payment.add(new Option(item.label || "Masked saved card", item.key || item.paymentHandle)));
        payment.value = this._model.payments.selected; actions.append(payment);
        const quote = this._button("Get authoritative quote", "create-quote", "primary"); quote.disabled = !this._model.payments.selected; actions.append(quote);
      }
    }
    host.append(actions); this._renderQuoteInto(host);
  }

  _applyBasketProjection(basket, fromSync = false) {
    this._model.basket.revision = Number.isInteger(basket?.revision) ? basket.revision : this._model.basket.revision;
    this._model.basket.itemCount = Number.isInteger(basket?.itemCount) ? basket.itemCount : 0;
    this._model.basket.currency = basket?.currency || "";
    this._model.basket.providerTotal = Number.isInteger(basket?.providerTotal) ? basket.providerTotal : null;
    this._model.basket.linesAvailable = Array.isArray(basket?.lines);
    if (fromSync) { this._model.basket.status = this._model.basket.itemCount ? "synced" : "empty"; this._model.draft.dirty = false; }
    else if (!this._model.basket.itemCount) this._model.basket.status = this._model.draft.lines.length ? "draft" : "empty";
    else if (this._model.draft.lines.length && !this._model.draft.dirty) this._model.basket.status = "synced";
    else this._model.basket.status = "remote-only";
  }

  async _syncBasket() {
    if (!this._storeAllowsOrdering()) { this._setLifecycle("ready", "The store is closed. Basket synchronization is unavailable and no provider request was sent.", "warning"); return; }
    return this._runSingleFlight("basket-sync", async () => {
      const capturedGeneration = this._generation;
      const products = this._serializeBasket(); const storeHandle = this._model.context.store?.storeHandle; const addressHandle = this._model.context.addressHandle;
      if (!products.length || !storeHandle || !addressHandle) { this._setLifecycle("ready", "Choose an address, store, and at least one item before syncing.", "warning"); return; }
      this._invalidateAuthority(""); this._model.basket.status = "syncing"; this._renderBasketSurfaces();
      try {
        const basket = await this._request("live/basket_set", { ...this._withGeneration(), expectedRevision: this._model.basket.revision || 0, storeHandle, addressHandle, products });
        if (capturedGeneration !== this._generation) return;
        this._applyBasketProjection(basket, true); this._setLifecycle("ready", "Provider basket synced. Provider status and total are authoritative; the catalog estimate is not."); this._renderAll();
        await this._loadPayments();
      } catch (_error) {
        if (capturedGeneration !== this._generation) return;
        this._model.basket.status = "provider-review";
        this._setLifecycle("ready", "Basket sync outcome is unknown or unconfirmed. The local draft was preserved and this panel will not retry automatically.", "error");
        this._renderAll(); await this._refreshStateAfterMutationFailure();
      }
    });
  }

  async _refreshBasket() {
    if (!this._generation || !this._model.capability.liveOrderingAvailable) return;
    const token = this._beginLatestRead("basket"); const capturedGeneration = this._generation;
    try {
      const basket = await this._request("live/basket", this._withGeneration());
      if (!this._isLatestRead("basket", token, capturedGeneration)) return;
      this._applyBasketProjection(basket); this._invalidateAuthority(""); this._renderBasketSurfaces();
    } catch (_error) {
      if (this._isLatestRead("basket", token, capturedGeneration)) { this._model.basket.status = "provider-review"; this._setLifecycle("ready", "Provider basket status is unavailable. No mutation was attempted.", "error"); this._renderBasketSurfaces(); }
    }
  }

  _askConfirmation(title, copy, kind, trigger) {
    this._confirmation = { kind };
    this.shadowRoot.querySelector("#confirm-title").textContent = title;
    this.shadowRoot.querySelector("#confirm-copy").textContent = copy;
    this.shadowRoot.querySelector("#confirm-button").textContent = kind === "clear-basket" ? "Clear basket" : "Delete";
    this._openDialog("confirm-dialog", trigger);
  }

  async _confirmAction() {
    const kind = this._confirmation?.kind; const payload = this._confirmation?.payload;
    this._closeDialog("confirm-dialog"); this._confirmation = null;
    if (kind === "clear-basket") await this._clearBasket();
    else if (kind === "delete-address") await this._deleteAddressAlias(payload);
    else if (kind === "delete-package") await this._deletePackage(payload);
  }

  async _clearBasket() {
    return this._runSingleFlight("basket-clear", async () => {
      const capturedGeneration = this._generation;
      try {
        const result = await this._request("live/basket_clear", { ...this._withGeneration(), expectedRevision: this._model.basket.revision });
        if (capturedGeneration !== this._generation) return;
        this._model.draft = { lines: [], dirty: false }; this._model.basket = { status: "empty", revision: result.revision, itemCount: 0, currency: "", providerTotal: null, linesAvailable: true };
        this._model.payments = { status: "idle", items: [], selected: "" }; this._invalidateAuthority(""); this._setLifecycle("ready", "Provider basket cleared after explicit confirmation."); this._renderAll();
      } catch (_error) {
        if (capturedGeneration !== this._generation) return;
        this._model.basket.status = "provider-review"; this._setLifecycle("ready", "Basket clear outcome is unknown. This panel will not retry; verify provider state manually.", "error"); this._renderAll(); await this._refreshStateAfterMutationFailure();
      }
    });
  }

  async _refreshStateAfterMutationFailure() {
    try { const state = await this._request("state"); await this._applyState(state); }
    catch (_error) { this._setLifecycle("ready", "Safe state refresh failed. No retry was attempted; recover manually before another mutation.", "error"); }
  }

  async _loadPayments() {
    if (!this._generation) return;
    const token = this._beginLatestRead("payments"); const capturedGeneration = this._generation; this._model.payments.status = "loading";
    try {
      const response = await this._request("live/payment_methods", this._withGeneration());
      if (!this._isLatestRead("payments", token, capturedGeneration)) return;
      this._model.payments.items = Array.isArray(response?.paymentMethods) ? response.paymentMethods : []; this._model.payments.selected = this._model.payments.items[0]?.key || this._model.payments.items[0]?.paymentHandle || ""; this._model.payments.status = "ready";
      this._invalidateAuthority(""); this._setLifecycle("ready", this._model.payments.items.length ? "Provider-selected masked card loaded. You can request an authoritative quote." : "No eligible provider-selected card is available.", this._model.payments.items.length ? "status" : "warning"); this._renderAll();
    } catch (_error) { if (this._isLatestRead("payments", token, capturedGeneration)) { this._model.payments.status = "error"; this._setLifecycle("ready", "Provider-selected card is unavailable. No payment details were exposed.", "error"); this._renderAll(); } }
  }

  async _createQuote() {
    return this._runSingleFlight("quote", async () => {
      const capturedGeneration = this._generation;
      const addressHandle = this._model.context.addressHandle; const paymentHandle = this._model.payments.selected;
      if (!addressHandle || !paymentHandle) { this._setLifecycle("ready", "Load the provider-selected masked card before requesting a quote.", "warning"); return; }
      this._invalidateAuthority(""); this._setLifecycle("ready", "Requesting an authoritative quote…");
      try {
        const quote = await this._request("live/create_quote", { ...this._withGeneration(), addressHandle, paymentHandle });
        if (capturedGeneration !== this._generation) return;
        this._model.quote = { projection: quote, challenge: null, ackText: "", typed: "", status: "ready" }; this._startQuoteExpiry(quote);
        this._setLifecycle("ready", "Review the authoritative provider total. Paid checkout is not shown when capability is unavailable."); this._renderAll();
      } catch (_error) { if (capturedGeneration !== this._generation) return; this._setLifecycle("ready", "Authoritative quote is unavailable. No local estimate was treated as a final total, and no retry was attempted.", "error"); await this._refreshStateAfterMutationFailure(); }
    });
  }

  _startQuoteExpiry(quote) {
    this._clearExpiryTimer();
    this._expiryTimer = globalThis.setInterval(() => {
      const seconds = Math.max(0, Math.ceil(Number(quote.expiresAt) - Date.now() / 1000));
      if (this._model.quote) this._model.quote.secondsRemaining = seconds;
      if (seconds <= 0) { this._invalidateAuthority("Quote expired; request a fresh authoritative quote."); this._renderAll(); }
      else this._renderBasketSurfaces();
    }, 1000);
  }

  _renderQuoteInto(host) {
    const model = this._model.quote; if (!model) return;
    const quote = model.projection; const box = this._el("section", "quote-box"); box.append(this._el("h3", "", "Authoritative quote"));
    (quote.priceLines || []).forEach((line) => { const row = this._el("div", "quote-line"); row.append(this._el("span", "", line.title || "Price"), this._el("span", "", line.value || "—")); box.append(row); });
    const total = this._el("div", "quote-line"); total.append(this._el("strong", "", "Exact total"), this._el("strong", "", this._formatMinor(quote.purchaseTotalCents, quote.currencyCode))); box.append(total);
    const details = [quote.address || "Masked saved address", quote.payment || "Provider-selected saved card", quote.eta ? `ETA ${quote.eta}` : "ETA unavailable", Number.isInteger(model.secondsRemaining) ? `expires in ${model.secondsRemaining}s` : ""].filter(Boolean).join(" · "); box.append(this._el("div", "authority-note", details));
    const advanced = this._el("details"); const summary = this._el("summary", "touch-target", "Advanced canary validation"); advanced.append(summary);
    const prepare = this._button("Prepare exact confirmation", "prepare-confirmation"); advanced.append(prepare);
    if (model.ackText) {
      advanced.append(this._el("p", "quiet", `Type exactly: ${model.ackText}`));
      const input = this._el("input"); input.dataset.role = "typed-ack"; input.autocomplete = "off"; input.value = model.typed; input.setAttribute("aria-label", "Exact acknowledgement"); advanced.append(input);
      if (this._model.capability.liveCheckoutAvailable === true) {
        const button = this._button("Submit paid order", "submit-checkout", "danger"); button.disabled = model.typed !== model.ackText || this._singleFlights.has("checkout"); advanced.append(button);
      } else advanced.append(this._el("p", "authority-note", "Confirmation can be reviewed, but this release exposes no paid action."));
    }
    box.append(advanced); host.append(box);
  }

  async _prepareConfirmation() {
    if (!this._model.quote) return;
    return this._runSingleFlight("quote-confirmation", async () => {
      const capturedGeneration = this._generation;
      try {
        const prepared = await this._request("live/prepare_confirmation", this._withGeneration());
        if (capturedGeneration !== this._generation) return;
        const exact = `${prepared.purchaseTotalCents} ${prepared.currencyCode}`;
        this._ackText = `ACK ${exact}`;
        this._model.quote.challenge = prepared.challenge; this._model.quote.ackText = this._ackText; this._model.quote.typed = "";
        this._setLifecycle("ready", "Exact confirmation prepared for review. Authority remains challenge-bound and expires with the quote."); this._renderAll();
      } catch (_error) { if (capturedGeneration !== this._generation) return; this._invalidateAuthority("Confirmation is invalid or expired. Get a fresh authoritative quote; no retry was attempted."); this._renderAll(); }
    });
  }

  async _submitCheckout() {
    if (this._model.capability.liveCheckoutAvailable !== true || !this._model.quote) return;
    const typed = this._model.quote.typed;
    if (typed !== this._ackText || !this._model.quote.challenge) { this._setLifecycle("ready", "Type the exact displayed acknowledgement before submitting.", "warning"); return; }
    return this._runSingleFlight("checkout", async () => {
      const capturedGeneration = this._generation;
      const challenge = this._model.quote.challenge; this._model.quote.challenge = null;
      this.shadowRoot.querySelectorAll("[data-action='submit-checkout']").forEach((button) => { button.disabled = true; });
      try {
        const result = await this._request("live/execute_checkout", { ...this._withGeneration(), challenge, acknowledged: true });
        if (capturedGeneration !== this._generation) return;
        this._invalidateAuthority("");
        if (String(result?.status || "").toUpperCase().includes("MANUAL") || result?.manualCheckRequired === true) await this._refreshStateAfterMutationFailure();
        else this._setLifecycle("ready", "Checkout result recorded. Do not submit again; check Glovo manually if uncertain.");
      } catch (_error) { if (capturedGeneration !== this._generation) return; this._invalidateAuthority(""); this._setLifecycle("blocked", "MANUAL_CHECK_REQUIRED — the checkout outcome is unknown. Do not retry or re-submit.", "error"); await this._refreshStateAfterMutationFailure(); }
      this._renderAll();
    });
  }

  async _checkCheckoutStatus() {
    try { const status = await this._request("live/checkout_status", this._withGeneration()); this._setLifecycle("blocked", status?.status === "succeeded" ? "Provider confirmed a result. Resolve the manual record only after checking Glovo." : "Manual check remains required. Do not retry.", "warning"); }
    catch (_error) { this._setLifecycle("blocked", "Manual check remains required. No retry was attempted.", "error"); }
  }

  async _loadLibrary() {
    if (!this._generation || !this._model.capability.liveOrderingAvailable) return;
    const token = this._beginLatestRead("library"); const capturedGeneration = this._generation; this._model.library.status = "loading";
    try {
      const response = await this._request("library/list", this._withGeneration());
      if (!this._isLatestRead("library", token, capturedGeneration)) return;
      this._model.library.status = "ready"; this._model.library.storeRevision = Number(response?.storeRevision) || 0; this._model.library.addresses = Array.isArray(response?.addresses) ? response.addresses : []; this._model.library.packages = Array.isArray(response?.packages) ? response.packages : []; this._model.library.error = "";
      if (this._model.library.activeView === "packages") this._renderPackages();
    } catch (_error) { if (this._isLatestRead("library", token, capturedGeneration)) { this._model.library.status = "error"; this._model.library.error = "Package library is unavailable. Ordering and local drafts remain separate."; if (this._model.library.activeView === "packages") this._renderPackages(); } }
  }

  _renderPackages() {
    const host = this.shadowRoot.querySelector("#main-content"); host.replaceChildren();
    if (this._model.library.status === "loading") { const box = this._emptyState("Loading package library", "Reading versioned aliases and packages…"); box.setAttribute("aria-busy", "true"); host.append(box); return; }
    if (this._model.library.status === "error") { host.append(this._emptyState("Package library unavailable", this._model.library.error, "Try library again", "reload-library")); return; }
    const layout = this._el("div", "library-layout");
    const addressSection = this._el("section", "library-section"); const addressHead = this._el("div", "section-head"); addressHead.append(this._el("h2", "", "Address aliases"), this._button("Create address alias", "new-address-alias", "primary")); addressSection.append(addressHead);
    if (!this._model.library.addresses.length) addressSection.append(this._el("p", "quiet", "No address aliases yet. Bind an alias to the currently selected saved address."));
    else {
      const grid = this._el("div", "library-grid"); this._model.library.addresses.forEach((address) => {
        const card = this._el("article", "library-card"); card.append(this._el("h3", "", address.name || "Address alias"), this._el("div", "quiet", `Version ${address.revision}`));
        const actions = this._el("div", "library-actions"); const edit = this._button("Update alias", "edit-address-alias"); edit.dataset.addressRef = address.addressRef; const remove = this._button("Delete alias", "ask-delete-address", "danger"); remove.dataset.addressRef = address.addressRef; actions.append(edit, remove); card.append(actions); grid.append(card);
      }); addressSection.append(grid);
    }
    if (this._model.library.aliasEditor) addressSection.append(this._renderAddressEditor());
    const packageSection = this._el("section", "library-section"); const packageHead = this._el("div", "section-head"); packageHead.append(this._el("h2", "", "Package library"), this._button("Create package", "new-package", "primary")); packageSection.append(packageHead);
    if (!this._model.library.packages.length) packageSection.append(this._el("p", "quiet", "No packages yet. Build a menu draft, then save it as a versioned package with multiple aliases."));
    else {
      const grid = this._el("div", "library-grid"); this._model.library.packages.forEach((item) => grid.append(this._renderPackageCard(item))); packageSection.append(grid);
    }
    layout.append(addressSection, packageSection); host.append(layout);
  }

  _renderAddressEditor() {
    const editor = this._model.library.aliasEditor; const form = this._el("div", "panel-card form-grid");
    form.append(this._el("h3", "", editor.addressRef ? "Update address alias" : "Create address alias"));
    const nameLabel = this._el("label", "field"); nameLabel.append(this._el("span", "", "Alias name")); const input = this._el("input"); input.dataset.libraryField = "alias-name"; input.value = editor.name; nameLabel.append(input); form.append(nameLabel);
    const addressLabel = this._el("label", "field"); addressLabel.append(this._el("span", "", "Bind to current saved address (versioned)")); const select = this._el("select"); select.dataset.libraryField = "alias-handle"; select.add(new Option("Select saved address", "")); this._model.context.addresses.forEach((item) => select.add(new Option(item.fullAddress || item.label || "Saved address", item.key || item.addressHandle))); select.value = editor.addressHandle || this._model.context.addressHandle; addressLabel.append(select); form.append(addressLabel);
    const actions = this._el("div", "library-actions"); actions.append(this._button("Cancel", "cancel-address-alias"), this._button(editor.addressRef ? "Update alias" : "Create address alias", "save-address-alias", "primary")); form.append(actions); return form;
  }

  _renderPackageCard(item) {
    const card = this._el("article", "library-card"); card.append(this._el("h3", "", item.name || "Package"));
    const aliases = Array.isArray(item.aliases) && item.aliases.length ? item.aliases.join(", ") : "No alternate aliases";
    card.append(this._el("div", "quiet", aliases), this._el("div", "", `${item.itemCount || 0} item(s) · ${item.storeLabel || "Store unavailable"}`), this._el("div", "quiet", `Address: ${item.addressName || "Unbound"} · version ${item.revision}`));
    const override = this._el("select"); override.setAttribute("aria-label", `Address override for ${item.name || "package"}`); override.dataset.packageAddress = item.packageRef; override.add(new Option("Pinned versioned address", "")); this._model.library.addresses.forEach((address) => override.add(new Option(address.name, address.addressRef))); card.append(override);
    const actions = this._el("div", "library-actions");
    [["Load into draft", "load-package"], ["Edit", "edit-package"], ["Duplicate", "duplicate-package"], ["Delete package", "ask-delete-package"]].forEach(([copy, action]) => { const button = this._button(copy, action, action.includes("delete") ? "danger" : ""); button.dataset.packageRef = item.packageRef; actions.append(button); });
    card.append(actions); return card;
  }

  _openAddressEditor(addressRef = "") {
    const address = this._model.library.addresses.find((item) => item.addressRef === addressRef);
    this._model.library.aliasEditor = { addressRef: address?.addressRef || "", expectedRevision: address?.revision || 0, name: address?.name || "", addressHandle: this._model.context.addressHandle };
    this._renderPackages();
  }

  async _saveAddressAlias() {
    const editor = this._model.library.aliasEditor; if (!editor) return;
    const name = editor.name.trim(); const addressHandle = editor.addressHandle || this._model.context.addressHandle;
    if (!name || !addressHandle) { this._setLifecycle("ready", "Alias name and an exact current saved address are required.", "warning"); return; }
    return this._runSingleFlight("library-write", async () => {
      const capturedGeneration = this._generation;
      try {
        await this._request("library/address_save", { ...this._withGeneration(), expectedStoreRevision: this._model.library.storeRevision, addressRef: editor.addressRef, expectedRevision: editor.expectedRevision, name, addressHandle });
        if (capturedGeneration !== this._generation) return;
        this._model.library.aliasEditor = null; this._setLifecycle("ready", "Address alias saved with an exact versioned binding."); await this._loadLibrary(); this._renderAll();
      } catch (_error) { if (capturedGeneration !== this._generation) return; this._setLifecycle("ready", "Address alias was not saved. Refresh the library before retrying manually.", "error"); }
    });
  }

  async _deleteAddressAlias(addressRef) {
    const address = this._model.library.addresses.find((item) => item.addressRef === addressRef); if (!address) return;
    return this._runSingleFlight("library-write", async () => {
      const capturedGeneration = this._generation;
      try { await this._request("library/address_delete", { ...this._withGeneration(), expectedStoreRevision: this._model.library.storeRevision, addressRef, expectedRevision: address.revision }); if (capturedGeneration !== this._generation) return; this._setLifecycle("ready", "Address alias deleted."); await this._loadLibrary(); this._renderAll(); }
      catch (_error) { if (capturedGeneration !== this._generation) return; this._setLifecycle("ready", "Address alias could not be deleted. It may still be referenced or have a newer revision.", "error"); }
    });
  }

  _openPackageEditor(packageRef = "", duplicate = false, trigger) {
    const item = this._model.library.packages.find((entry) => entry.packageRef === packageRef);
    const address = this._model.library.addresses.find((entry) => entry.addressRef === item?.addressRef);
    this._model.library.packageEditor = { packageRef: duplicate ? "" : (item?.packageRef || ""), expectedRevision: duplicate ? 0 : (item?.revision || 0), name: item ? `${item.name}${duplicate ? " copy" : ""}` : "", aliases: item?.aliases ? [...item.aliases] : [], addressRef: item?.addressRef || "", addressRevision: address?.revision || 0 };
    this._renderPackageEditor(); this._openDialog("package-dialog", trigger);
  }

  _renderPackageEditor() {
    const editor = this._model.library.packageEditor; if (!editor) return;
    const host = this.shadowRoot.querySelector("#package-editor-content"); host.replaceChildren(); const form = this._el("div", "form-grid");
    const name = this._el("label", "field"); name.append(this._el("span", "", "Package name")); const nameInput = this._el("input"); nameInput.dataset.libraryField = "package-name"; nameInput.value = editor.name; name.append(nameInput); form.append(name);
    const aliases = this._el("label", "field"); aliases.append(this._el("span", "", "Aliases (comma separated, multiple allowed)")); const aliasInput = this._el("input"); aliasInput.dataset.libraryField = "package-aliases"; aliasInput.value = editor.aliases.join(", "); aliases.append(aliasInput); form.append(aliases);
    const address = this._el("label", "field"); address.append(this._el("span", "", "Versioned address alias")); const select = this._el("select"); select.dataset.libraryField = "package-address"; select.add(new Option("Select address alias", "")); this._model.library.addresses.forEach((item) => select.add(new Option(`${item.name} · v${item.revision}`, item.addressRef))); select.value = editor.addressRef; address.append(select); form.append(address);
    form.append(this._el("p", "quiet", `${this._model.draft.lines.length} current draft line(s) will be saved. Loading or preparing a package never syncs the provider basket.`));
    if (!this._model.context.store) form.append(this._el("p", "field-error", "Select an explicit store and load its menu before saving a package."));
    else if (!this._storeAllowsOrdering()) form.append(this._el("p", "field-error", "Closed-store menus are browse-only and cannot be saved as prepared packages."));
    host.append(form);
    const footer = this.shadowRoot.querySelector("#package-editor-footer"); const save = this._button(editor.packageRef ? "Update package" : "Create package", "save-package", "primary"); save.disabled = !this._storeAllowsOrdering(); footer.replaceChildren(this._button("Cancel", "close-dialog"), save); footer.firstElementChild.dataset.dialog = "package-dialog";
  }

  async _savePackage() {
    const editor = this._model.library.packageEditor; const address = this._model.library.addresses.find((item) => item.addressRef === editor?.addressRef);
    if (!this._storeAllowsOrdering()) { this._setLifecycle("ready", "Closed-store menus are browse-only. No package or provider state was changed.", "warning"); return; }
    if (!editor || !editor.name.trim() || !address || !this._model.context.store || !this._model.draft.lines.length) { this._setLifecycle("ready", "Package name, versioned address alias, loaded store, and draft items are required.", "warning"); return; }
    const aliases = editor.aliases.map((item) => item.trim()).filter(Boolean);
    return this._runSingleFlight("library-write", async () => {
      const capturedGeneration = this._generation;
      try {
        await this._request("library/package_save", { ...this._withGeneration(), expectedStoreRevision: this._model.library.storeRevision, packageRef: editor.packageRef, expectedRevision: editor.expectedRevision, name: editor.name.trim(), aliases, addressRef: address.addressRef, addressRevision: address.revision, storeHandle: this._model.context.store.storeHandle, products: this._serializeBasket() });
        if (capturedGeneration !== this._generation) return;
        this._model.library.packageEditor = null; this._closeDialog("package-dialog"); this._setLifecycle("ready", "Package saved. This changed only the local versioned library; provider basket was not mutated."); await this._loadLibrary(); this._renderAll();
      } catch (_error) { if (capturedGeneration !== this._generation) return; this._setLifecycle("ready", "Package was not saved. Refresh the library before retrying manually.", "error"); }
    });
  }

  async _deletePackage(packageRef) {
    const item = this._model.library.packages.find((entry) => entry.packageRef === packageRef); if (!item) return;
    return this._runSingleFlight("library-write", async () => {
      const capturedGeneration = this._generation;
      try { await this._request("library/package_delete", { ...this._withGeneration(), expectedStoreRevision: this._model.library.storeRevision, packageRef, expectedRevision: item.revision }); if (capturedGeneration !== this._generation) return; this._setLifecycle("ready", "Package deleted."); await this._loadLibrary(); this._renderAll(); }
      catch (_error) { if (capturedGeneration !== this._generation) return; this._setLifecycle("ready", "Package could not be deleted because its revision changed or persistence is unavailable.", "error"); }
    });
  }

  async _preparePackage(packageRef, addressKey = "", editorRequest = null) {
    return this._runSingleFlight("package-prepare", async () => {
      const capturedGeneration = this._generation;
      this._setLifecycle("ready", "Preparing the package with fresh GET-only reconciliation…");
      try {
        const response = await this._request("library/package_prepare", { ...this._withGeneration(), packageKey: packageRef, addressKey });
        if (capturedGeneration !== this._generation) return;
        if (response?.status !== "ready" || response?.selectionComplete !== true) { this._setLifecycle("ready", `Package needs review: ${this._safeStaleReason(response?.reason)}. No basket write occurred.`, "warning"); return; }
        const menu = response.menu || { products: [] };
        this._model.context.addressHandle = response.address?.key || "";
        this._model.context.addressLabel = response.address?.fullAddress || response.address?.label || response.address?.name || "";
        this._model.context.addresses = response.address?.key ? [{ ...response.address }] : [];
        this._model.context.store = response.store || null; this._model.context.stores = response.store ? [response.store] : []; this._model.menu = { status: "ready", products: Array.isArray(menu.products) ? menu.products : [], query: "", filter: "all", error: "" };
        this._model.draft.lines = (response.selection?.products || []).map((selection) => this._lineFromSelection(selection)).filter(Boolean); this._model.draft.dirty = true; this._model.basket.status = this._model.draft.lines.length ? "draft" : "empty"; this._invalidateAuthority("");
        this._model.library.activeView = editorRequest ? "packages" : "menu";
        this._setLifecycle("ready", editorRequest ? "Package reconciled for editing. Saving updates only the versioned library." : "Package loaded into the local draft after exact reconciliation. Choose Sync basket separately."); this._renderAll();
        if (editorRequest) {
          const action = editorRequest.duplicate ? "duplicate-package" : "edit-package";
          const selector = `button[data-action='${action}'][data-package-ref='${CSS.escape(packageRef)}']`;
          this._openPackageEditor(packageRef, editorRequest.duplicate, this.shadowRoot.querySelector(selector));
        }
      } catch (_error) { if (capturedGeneration !== this._generation) return; this._setLifecycle("ready", "Package preparation failed safely. No provider mutation or automatic retry occurred.", "error"); }
    });
  }

  _lineFromSelection(selection) {
    const product = this._model.menu.products.find((item) => item.productHandle === selection.productHandle); if (!product) return null;
    const selectedGroups = Array.isArray(selection.options) ? selection.options : [];
    const options = (product.optionGroups || []).map((group) => {
      const handles = selectedGroups.find((item) => item.groupHandle === group.groupHandle)?.optionHandles || [];
      const selected = (group.options || []).filter((option) => handles.includes(option.optionHandle));
      return { groupHandle: group.groupHandle, groupLabel: group.label || "Options", optionHandles: [...handles], optionLabels: selected.map((option) => option.label || "Option"), deltaMinor: selected.reduce((sum, option) => sum + (Number(option.priceMinor) || 0), 0), currency: selected[0]?.currency || product.currency };
    });
    return { productHandle: product.productHandle, label: product.label || "Menu item", quantity: Number(selection.quantity) || 1, unitPriceMinor: Number(product.priceMinor) || 0, currency: product.currency || "", options };
  }

  _safeStaleReason(reason) {
    const safe = { account_changed: "provider account changed", address_missing_or_changed: "address missing or changed", store_missing_or_changed: "store missing or changed", store_closed: "store is currently closed", product_missing_or_changed: "product missing or changed", option_missing_or_changed: "option missing or changed", selection_constraints_changed: "selection constraints changed" };
    return safe[reason] || "saved package is stale";
  }

  async _loadRecovery() {
    this._model.lifecycle = { status: "blocked", message: "MANUAL_CHECK_REQUIRED — check Glovo manually. This panel will not retry.", kind: "error" };
    try {
      const response = await this._hass.callWS({ type: "glovo/ordering/manual_checks" }); const attempt = (response?.attempts || [])[0];
      this._model.recovery = { kind: "manual", attempt: attempt || null }; this._renderAll();
    } catch (_error) { this._model.recovery = { kind: "manual", attempt: null }; this._renderAll(); }
  }

  async _resolveManual(resolution) {
    const attempt = this._model.recovery?.attempt; const acknowledgement = this.shadowRoot.querySelector("#manual-ack");
    if (!attempt || acknowledgement?.checked !== true) { this._setLifecycle("blocked", "Explicit manual-review acknowledgement is required.", "warning"); return; }
    return this._runSingleFlight("manual-resolution", async () => {
      try {
        const prepared = await this._hass.callWS({ type: "glovo/ordering/prepare_manual_resolution", attemptRef: attempt.attemptRef, expectedRecordRevision: attempt.recordRevision, expectedState: attempt.state, resolution });
        await this._hass.callWS({ type: "glovo/ordering/resolve_manual_check", attemptRef: attempt.attemptRef, expectedRecordRevision: attempt.recordRevision, expectedState: attempt.state, resolution, challenge: prepared.challenge, acknowledged: true });
        this._invalidateAuthority(""); await this._bootstrap();
      } catch (_error) { this._setLifecycle("blocked", "Resolution was rejected or not persisted; ordering remains blocked. It was not retried.", "error"); }
    });
  }

  _openDialog(id, trigger) {
    const dialog = this.shadowRoot.querySelector(`#${id}`); if (!dialog) return;
    this._returnFocus = trigger instanceof Element ? trigger : this.shadowRoot.activeElement;
    if (!dialog.open) dialog.showModal();
  }

  _closeDialog(id, restore = true) {
    const dialog = this.shadowRoot.querySelector(`#${id}`); if (dialog?.open) dialog.close();
    if (restore && this._returnFocus?.isConnected) this._returnFocus.focus();
  }

  _afterDialogClosed(id) {
    if (id === "customizer-dialog") this._model.overlay = null;
    if (id === "package-dialog" && this._model.library.packageEditor) this._model.library.packageEditor = null;
  }

  _closeAllDialogs(restore = false) { if (!this._rendered) return; this.shadowRoot.querySelectorAll("dialog[open]").forEach((dialog) => this._closeDialog(dialog.id, restore)); }

  _onClick(event) {
    const button = event.target.closest("button[data-action]"); if (!button) return;
    const action = button.dataset.action;
    if (action === "nav-view") { this._model.library.activeView = button.dataset.view; if (button.dataset.view === "packages" && this._model.library.status === "idle") this._loadLibrary(); this._renderAll(); }
    else if (action === "refresh-addresses") this._loadAddresses();
    else if (action === "lookup-store") this._lookupStore();
    else if (action === "retry-menu") this._lookupStore();
    else if (action === "clear-search") { this._model.menu.query = ""; this._renderMenu(); this.shadowRoot.querySelector("#menu-search")?.focus(); }
    else if (action === "reset-results") { this._model.menu.query = ""; this._model.menu.filter = "all"; this._renderMenu(); }
    else if (action === "set-filter") { this._model.menu.filter = button.dataset.filter; this._renderMenu(); }
    else if (action === "add-simple") this._addSimple(button.dataset.productHandle);
    else if (action === "customize-product") this._openCustomizer(button.dataset.productHandle, button);
    else if (action === "step-line") this._stepLine(button.dataset.productHandle, Number(button.dataset.delta));
    else if (action === "remove-line") this._removeLine(button.dataset.productHandle);
    else if (action === "step-custom") { if (this._model.overlay?.type === "customizer") { this._model.overlay.quantity = Math.max(1, Math.min(50, Number(this._model.overlay.quantity) + Number(button.dataset.delta))); this._renderCustomizer(); } }
    else if (action === "commit-customization") this._commitCustomization();
    else if (action === "sync-basket") this._syncBasket();
    else if (action === "refresh-basket") this._refreshBasket();
    else if (action === "open-basket") { this._renderBasketInto(this.shadowRoot.querySelector("#basket-dialog-content")); this._openDialog("basket-dialog", button); }
    else if (action === "ask-clear-basket") this._askConfirmation("Clear provider basket?", "This removes the provider basket and the local draft. It requires the current provider revision and cannot be undone.", "clear-basket", button);
    else if (action === "confirm-action") this._confirmAction();
    else if (action === "close-dialog") this._closeDialog(button.dataset.dialog || "customizer-dialog");
    else if (action === "load-payments") this._loadPayments();
    else if (action === "create-quote") this._createQuote();
    else if (action === "prepare-confirmation") this._prepareConfirmation();
    else if (action === "submit-checkout") this._submitCheckout();
    else if (action === "retry-state") this._bootstrap();
    else if (action === "check-checkout-status") this._checkCheckoutStatus();
    else if (action === "resolve-manual") this._resolveManual(button.dataset.resolution);
    else if (action === "reload-library") this._loadLibrary();
    else if (action === "new-address-alias") this._openAddressEditor();
    else if (action === "edit-address-alias") this._openAddressEditor(button.dataset.addressRef);
    else if (action === "cancel-address-alias") { this._model.library.aliasEditor = null; this._renderPackages(); }
    else if (action === "save-address-alias") this._saveAddressAlias();
    else if (action === "ask-delete-address") { this._confirmation = { kind: "delete-address", payload: button.dataset.addressRef }; this._askConfirmation("Delete address alias?", "Referenced aliases cannot be deleted. No provider address will be changed.", "delete-address", button); this._confirmation.payload = button.dataset.addressRef; }
    else if (action === "new-package") this._openPackageEditor("", false, button);
    else if (action === "edit-package") this._preparePackage(button.dataset.packageRef, "", { duplicate: false });
    else if (action === "duplicate-package") this._preparePackage(button.dataset.packageRef, "", { duplicate: true });
    else if (action === "save-package") this._savePackage();
    else if (action === "ask-delete-package") { this._askConfirmation("Delete package?", "This removes only the local versioned package. It never changes the provider basket.", "delete-package", button); this._confirmation.payload = button.dataset.packageRef; }
    else if (action === "load-package") { const select = this.shadowRoot.querySelector(`select[data-package-address='${CSS.escape(button.dataset.packageRef)}']`); this._preparePackage(button.dataset.packageRef, select?.value || ""); }
  }

  _onInput(event) {
    const target = event.target;
    if (target.id === "menu-search") { this._model.menu.query = target.value; this._renderMenu(); this.shadowRoot.querySelector("#menu-search")?.focus(); }
    else if (target.id === "store-input") this._model.context.storeInput = target.value;
    else if (target.id === "custom-quantity" && this._model.overlay?.type === "customizer") this._model.overlay.quantity = Number(target.value);
    else if (target.dataset.role === "typed-ack" && this._model.quote) { this._model.quote.typed = target.value; this.shadowRoot.querySelectorAll("[data-action='submit-checkout']").forEach((button) => { button.disabled = target.value !== this._model.quote.ackText || this._singleFlights.has("checkout"); }); }
    else if (target.dataset.libraryField === "alias-name" && this._model.library.aliasEditor) this._model.library.aliasEditor.name = target.value;
    else if (target.dataset.libraryField === "package-name" && this._model.library.packageEditor) this._model.library.packageEditor.name = target.value;
    else if (target.dataset.libraryField === "package-aliases" && this._model.library.packageEditor) this._model.library.packageEditor.aliases = target.value.split(",");
  }

  _onChange(event) {
    const target = event.target;
    if (target.id === "address-select") {
      this._invalidateAuthority(""); this._model.context.addressHandle = target.value; this._model.context.addressLabel = target.selectedOptions[0]?.textContent || ""; this._resetCatalog(); this._setLifecycle("ready", "Address changed; store, menu, payments, and quote authority were discarded.", "warning"); this._renderAll();
    } else if (target.id === "store-select") {
      const store = this._model.context.stores.find((item) => item.storeHandle === target.value); this._loadStoreMenu(store);
    } else if (target.dataset.groupHandle && this._model.overlay?.type === "customizer") {
      const selected = this._model.overlay.selections.get(target.dataset.groupHandle) || new Set();
      if (target.type === "radio") { selected.clear(); if (target.dataset.optionHandle) selected.add(target.dataset.optionHandle); }
      else if (target.checked) selected.add(target.dataset.optionHandle); else selected.delete(target.dataset.optionHandle);
      this._model.overlay.selections.set(target.dataset.groupHandle, selected); this._model.overlay.errors.delete(target.dataset.groupHandle); this._renderCustomizer();
    } else if (target.dataset.role === "payment-select") { this._model.payments.selected = target.value; this._invalidateAuthority("Payment selection changed; quote authority was discarded."); this._renderAll(); }
    else if (target.dataset.libraryField === "alias-handle" && this._model.library.aliasEditor) this._model.library.aliasEditor.addressHandle = target.value;
    else if (target.dataset.libraryField === "package-address" && this._model.library.packageEditor) { const address = this._model.library.addresses.find((item) => item.addressRef === target.value); this._model.library.packageEditor.addressRef = target.value; this._model.library.packageEditor.addressRevision = address?.revision || 0; }
  }

  _onKeydown(event) {
    if (event.key !== "Escape") return;
    if (event.target.id === "menu-search" && this._model.menu.query) { event.preventDefault(); this._model.menu.query = ""; this._renderMenu(); this.shadowRoot.querySelector("#menu-search")?.focus(); }
  }
}

const componentName = new URL(import.meta.url).searchParams.get("component") || "glovo-ordering-panel";
if (!customElements.get(componentName)) customElements.define(componentName, GlovoOrderingPanel);
