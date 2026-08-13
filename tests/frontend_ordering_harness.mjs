import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";

const panelPath = resolve(process.argv[2] || "custom_components/glovo/frontend/glovo-ordering-panel.js");
const source = await readFile(panelPath, "utf8");
let shadowMode = "";
const registry = new Map();

globalThis.HTMLElement = class {
  attachShadow(options) {
    shadowMode = options.mode;
    return {};
  }
};
globalThis.customElements = {
  define(name, value) { registry.set(name, value); },
  get(name) { return registry.get(name); },
};

await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
const Panel = registry.get("glovo-ordering-panel");
assert.ok(Panel, "component registered");
const panel = new Panel();
assert.equal(shadowMode, "open");
assert.deepEqual(
  Object.keys(panel._model),
  ["lifecycle", "capability", "context", "menu", "draft", "basket", "payments", "quote", "overlay", "library", "recovery"],
);

assert.equal(panel._normalizeSearch("Crème BRÛLÉE"), "creme brulee");
panel._model.menu.products = [
  { productHandle: "p1", label: "Crème brûlée", optionGroups: [] },
  { productHandle: "p2", label: "Wrap", optionGroups: [{ groupHandle: "g", options: [] }] },
];
panel._model.menu.query = "creme";
panel._model.menu.filter = "all";
assert.deepEqual(panel._filteredProducts().map((item) => item.productHandle), ["p1"]);
panel._model.menu.query = "";
panel._model.menu.filter = "customizable";
assert.deepEqual(panel._filteredProducts().map((item) => item.productHandle), ["p2"]);
panel._model.draft.lines = [{ productHandle: "p1", quantity: 2, options: [] }];
panel._model.menu.filter = "basket";
assert.deepEqual(panel._filteredProducts().map((item) => item.productHandle), ["p1"]);

const product = {
  productHandle: "p2",
  label: "Wrap",
  optionGroups: [
    {
      groupHandle: "g1",
      label: "Sauce",
      min: 1,
      max: 1,
      options: [
        { optionHandle: "o1", label: "Mild", selected: true, priceMinor: 0, currency: "EUR" },
        { optionHandle: "o2", label: "Hot", selected: false, priceMinor: 20, currency: "EUR" },
      ],
    },
    {
      groupHandle: "g2",
      label: "Extras",
      min: 0,
      max: 2,
      options: [
        { optionHandle: "o3", label: "A", selected: false, priceMinor: 30, currency: "EUR" },
        { optionHandle: "o4", label: "B", selected: false, priceMinor: 40, currency: "EUR" },
        { optionHandle: "o5", label: "C", selected: false, priceMinor: 50, currency: "EUR" },
      ],
    },
  ],
};
let customization = { product, quantity: 1, selections: new Map([["g1", new Set()], ["g2", new Set()]]) };
let validation = panel._validateCustomization(customization);
assert.equal(validation.valid, false);
assert.equal(validation.errors.get("g1"), "Choose exactly 1.");
customization.selections.get("g1").add("o1");
customization.selections.get("g2").add("o3");
customization.selections.get("g2").add("o4");
customization.selections.get("g2").add("o5");
validation = panel._validateCustomization(customization);
assert.equal(validation.valid, false);
assert.equal(validation.errors.get("g2"), "Choose no more than 2.");
customization.selections.get("g2").delete("o5");
validation = panel._validateCustomization(customization);
assert.equal(validation.valid, true);

panel._model.draft.lines = [{
  productHandle: "p2",
  label: "Wrap",
  quantity: 2,
  unitPriceMinor: 100,
  currency: "EUR",
  options: [{ groupHandle: "g1", groupLabel: "Sauce", optionHandles: ["o1"], optionLabels: ["Mild"], deltaMinor: 0 }],
}];
assert.deepEqual(panel._serializeBasket(), [{
  productHandle: "p2",
  quantity: 2,
  options: [{ groupHandle: "g1", optionHandles: ["o1"] }],
}]);
assert.equal(panel._estimatedSubtotal().amountMinor, 200);
assert.equal(panel._formatMinor(560000, "AMD"), "AMD 5,600.00");
assert.equal(panel._formatMinor(1234, "JPY"), "¥1,234");
assert.equal(panel._formatMinor(1234, "EUR"), "€12.34");
assert.match(panel._formatMinor(1234, "KWD"), /1\.234/);
assert.equal(panel._formatMinor(1234, "ZZZ"), "Price unavailable");

const first = panel._beginLatestRead("menu");
const second = panel._beginLatestRead("menu");
assert.equal(panel._isLatestRead("menu", first, panel._generation), false);
assert.equal(panel._isLatestRead("menu", second, panel._generation), true);

let flightCalls = 0;
let releaseFlight;
const gate = new Promise((resolveGate) => { releaseFlight = resolveGate; });
const one = panel._runSingleFlight("basket-sync", async () => { flightCalls += 1; await gate; return "done"; });
const two = panel._runSingleFlight("basket-sync", async () => { flightCalls += 1; return "duplicate"; });
await Promise.resolve();
assert.equal(flightCalls, 1);
releaseFlight();
assert.deepEqual(await Promise.all([one, two]), ["done", "done"]);
assert.equal(panel._singleFlights.size, 0);

// The paid acknowledgement is bound to exact minor units and currency. Submit
// itself is single-flight, consumes the challenge before I/O, and is inert while
// capability is unresolved/false.
const checkoutPanel = new Panel();
checkoutPanel._generation = 19;
checkoutPanel._model.quote = {
  projection: {
    store: "Synthetic Store",
    items: [{ name: "Meal", quantity: 2, options: ["Large"] }],
    priceLines: [{ title: "Subtotal", value: "5,600 AMD" }],
    purchaseTotalCents: 560000,
    currencyCode: "AMD",
    address: "123 Synthetic Avenue",
    payment: "Saved card •••• 4242",
    eta: "20 min",
  },
  secondsRemaining: 30,
  challenge: null,
  ackText: "",
  typed: "",
};
checkoutPanel._renderAll = () => {};
checkoutPanel._setLifecycle = () => {};
checkoutPanel._invalidateAuthority = () => {};
checkoutPanel._refreshStateAfterMutationFailure = async () => {};
checkoutPanel.shadowRoot = { querySelectorAll: () => [] };
const checkoutRequests = [];
let releaseCheckout;
const checkoutGate = new Promise((resolveGate) => { releaseCheckout = resolveGate; });
checkoutPanel._request = async (operation, request) => {
  checkoutRequests.push({ operation, request });
  if (operation === "live/prepare_confirmation") {
    return { purchaseTotalCents: 560000, currencyCode: "AMD", challenge: "challenge-exact" };
  }
  await checkoutGate;
  return { status: "succeeded", manualCheckRequired: false };
};
await checkoutPanel._prepareConfirmation();
assert.equal(checkoutPanel._model.quote.ackText, "ACK 560000 AMD");
assert.equal(checkoutPanel._ackText, "ACK 560000 AMD");
checkoutPanel._model.quote.typed = "ACK 560000 AMD";
await checkoutPanel._submitCheckout();
assert.deepEqual(checkoutRequests.map((entry) => entry.operation), ["live/prepare_confirmation"]);
checkoutPanel._model.capability.liveCheckoutAvailable = true;
checkoutPanel._model.quote.challenge = "challenge-exact";
const submitOne = checkoutPanel._submitCheckout();
const submitTwo = checkoutPanel._submitCheckout();
await Promise.resolve();
assert.deepEqual(checkoutRequests.map((entry) => entry.operation), ["live/prepare_confirmation", "live/execute_checkout"]);
assert.equal(checkoutPanel._model.quote.challenge, null);
releaseCheckout();
await Promise.all([submitOne, submitTwo]);
assert.equal(checkoutRequests.filter((entry) => entry.operation === "live/execute_checkout").length, 1);
assert.deepEqual(checkoutRequests[1].request, { generation: 19, challenge: "challenge-exact", acknowledged: true });

// Routine Home Assistant state updates must not replace focused form controls.
// Simulate typing every character of a slug while HA publishes a fresh hass object
// between keystrokes; the model must retain the full draft without a workspace render.
panel._rendered = true;
panel._hass = { callWS() {} };
panel._updateEnvironment = () => {};
let routineRenders = 0;
panel._renderAll = () => { routineRenders += 1; };
let typedSlug = "";
for (const character of "kfc-yrv") {
  typedSlug += character;
  panel._onInput({ target: { id: "store-input", value: typedSlug, dataset: {} } });
  panel.hass = { callWS() {}, states: { [`sensor.tick_${typedSlug.length}`]: {} } };
}
assert.equal(panel._model.context.storeInput, "kfc-yrv");
assert.equal(routineRenders, 0, "routine hass updates must preserve focused input/caret");

// Explicit lookup owns the current store/menu association. It clears stale handles
// before the request and immediately loads the sole fresh exact result.
panel._generation = 7;
panel._model.context.addressHandle = "choice-current";
panel._model.context.storeInput = "kfc-yrv";
panel._model.context.stores = [{ storeHandle: "live-stale", label: "Stale" }];
panel._model.context.store = panel._model.context.stores[0];
panel._model.menu = { status: "ready", products: [{ productHandle: "stale-product" }], query: "", filter: "all", error: "" };
panel._setLifecycle = () => {};
panel._invalidateAuthority = () => {};
panel._renderAll = () => {};
const freshStore = { storeHandle: "live-fresh", label: "KFC" };
panel._request = async (operation) => {
  assert.equal(operation, "live/stores");
  return { stores: [freshStore] };
};
let loadedStore = null;
panel._loadStoreMenu = async (store) => { loadedStore = store; };
await panel._lookupStore();
assert.deepEqual(panel._model.context.stores, [freshStore]);
assert.equal(loadedStore, freshStore);

// A failed lookup must not leave an old selectable handle behind, and the explicit
// retry action must acquire fresh store/menu authority rather than reuse that handle.
panel._model.context.stores = [{ storeHandle: "live-stale", label: "Stale" }];
panel._model.context.store = panel._model.context.stores[0];
panel._request = async () => { throw new Error("synthetic provider read failure"); };
await panel._lookupStore();
assert.deepEqual(panel._model.context.stores, []);
assert.equal(panel._model.context.store, null);
let retryLookups = 0;
panel._lookupStore = () => { retryLookups += 1; };
panel._onClick({ target: { closest: () => ({ dataset: { action: "retry-menu" } }) } });
assert.equal(retryLookups, 1);

// Closed-store menus are read-only. UI helpers must refuse every path that can
// create a draft or synchronize provider state, even when invoked directly.
panel._model.context.store = {
  storeHandle: "closed-store",
  label: "Closed Kitchen",
  isOpen: false,
  orderingAvailable: false,
};
panel._model.context.addressHandle = "choice-current";
panel._model.menu.products = [product, { productHandle: "p1", label: "Soup", optionGroups: [] }];
panel._model.draft.lines = [];
panel._model.overlay = null;
panel._renderCustomizer = () => {};
panel._openDialog = () => {};
panel._openCustomizer("p2", null);
assert.equal(panel._model.overlay, null);
panel._addSimple("p1");
assert.deepEqual(panel._model.draft.lines, []);
panel._model.draft.lines = [{ productHandle: "p1", quantity: 1, options: [] }];
let closedMutationRequests = 0;
panel._request = async () => { closedMutationRequests += 1; return {}; };
panel._renderBasketSurfaces = () => {};
await panel._syncBasket();
assert.equal(closedMutationRequests, 0);

// Sync performs a GET-only runtime/age preflight. A runtime rebuild or handles older
// than the safety margin discards the draft and must never reach basket_set.
const authorityPanel = new Panel();
authorityPanel._generation = 9;
authorityPanel._runtimeEpoch = "runtime-a";
authorityPanel._handlesIssuedAt = Date.now();
authorityPanel._setLifecycle = () => {};
authorityPanel._renderBasketSurfaces = () => {};
authorityPanel._renderAll = () => {};
authorityPanel._invalidateAuthority = () => {};
authorityPanel._model.context.addressHandle = "address-current";
authorityPanel._model.context.store = { storeHandle: "store-current", isOpen: true, orderingAvailable: true };
authorityPanel._model.draft.lines = [{ productHandle: "product-current", quantity: 1, options: [] }];
let authorityRequests = [];
authorityPanel._request = async (operation) => {
  authorityRequests.push(operation);
  return { generation: 9, runtimeEpoch: "runtime-b", liveOrderingAvailable: true };
};
authorityPanel._applyState = async (state) => {
  authorityPanel._runtimeEpoch = state.runtimeEpoch;
  authorityPanel._resetEphemeralGeneration("");
};
await authorityPanel._syncBasket();
assert.deepEqual(authorityRequests, ["state"]);
assert.deepEqual(authorityPanel._model.draft.lines, []);

authorityPanel._generation = 9;
authorityPanel._runtimeEpoch = "runtime-b";
authorityPanel._handlesIssuedAt = Date.now() - 241000;
authorityPanel._model.context.addressHandle = "address-expired";
authorityPanel._model.context.store = { storeHandle: "store-expired", isOpen: true, orderingAvailable: true };
authorityPanel._model.draft.lines = [{ productHandle: "product-expired", quantity: 1, options: [] }];
authorityRequests = [];
authorityPanel._request = async (operation) => {
  authorityRequests.push(operation);
  return { generation: 9, runtimeEpoch: "runtime-b", liveOrderingAvailable: true };
};
authorityPanel._applyState = async () => {};
await authorityPanel._syncBasket();
assert.deepEqual(authorityRequests, ["state"]);
assert.deepEqual(authorityPanel._model.draft.lines, []);

const freshPanel = new Panel();
freshPanel._generation = 9;
freshPanel._runtimeEpoch = "runtime-fresh";
freshPanel._handlesIssuedAt = Date.now();
freshPanel._setLifecycle = () => {};
freshPanel._renderBasketSurfaces = () => {};
freshPanel._renderAll = () => {};
freshPanel._invalidateAuthority = () => {};
freshPanel._loadPayments = async () => {};
freshPanel._model.context.addressHandle = "address-fresh";
freshPanel._model.context.store = { storeHandle: "store-fresh", isOpen: true, orderingAvailable: true };
freshPanel._model.draft.lines = [{ productHandle: "product-fresh", quantity: 1, options: [] }];
const freshRequests = [];
freshPanel._request = async (operation) => {
  freshRequests.push(operation);
  if (operation === "state") return { generation: 9, runtimeEpoch: "runtime-fresh", liveOrderingAvailable: true };
  assert.equal(operation, "live/basket_set");
  return { revision: 1, itemCount: 1, currency: "EUR", providerTotal: 100, lines: [] };
};
await freshPanel._syncBasket();
assert.deepEqual(freshRequests, ["state", "live/basket_set"]);
assert.equal(freshPanel._model.basket.status, "synced");

// Saving a multi-line local draft creates an address-independent package and does
// not mutate the provider basket.
const packagePanel = new Panel();
packagePanel._generation = 11;
packagePanel._setLifecycle = () => {};
packagePanel._closeDialog = () => {};
packagePanel._renderAll = () => {};
packagePanel._loadLibrary = async () => {};
packagePanel._model.library.storeRevision = 4;
packagePanel._model.library.packageEditor = { packageRef: "", expectedRevision: 0, name: "Team lunch", aliases: ["usual"] };
packagePanel._model.context.store = { storeHandle: "store-live", isOpen: true, orderingAvailable: true };
packagePanel._model.draft.lines = [
  { productHandle: "product-a", label: "Meal A", quantity: 2, options: [] },
  { productHandle: "product-b", label: "Meal B", quantity: 1, options: [{ groupHandle: "group-b", optionHandles: ["option-b"], optionLabels: ["Large"] }] },
];
const packageRequests = [];
packagePanel._request = async (operation, request) => { packageRequests.push({ operation, request }); return { storeRevision: 5 }; };
await packagePanel._savePackage();
assert.equal(packageRequests.length, 1);
assert.equal(packageRequests[0].operation, "library/package_save");
assert.deepEqual(packageRequests[0].request.products, [
  { productHandle: "product-a", quantity: 2, options: [] },
  { productHandle: "product-b", quantity: 1, options: [{ groupHandle: "group-b", optionHandles: ["option-b"] }] },
]);
assert.equal("addressRef" in packageRequests[0].request, false);
assert.equal("addressRevision" in packageRequests[0].request, false);
assert.equal("addressHandle" in packageRequests[0].request, false);

// Package preparation requires an explicit fresh current address. Missing selection
// sends nothing; a valid selection sends only packageKey + addressHandle authority.
const preparePanel = new Panel();
preparePanel._generation = 12;
preparePanel._setLifecycle = () => {};
preparePanel._renderAll = () => {};
preparePanel._invalidateAuthority = () => {};
preparePanel._model.basket = { status: "empty", revision: 0, itemCount: 0, currency: "", providerTotal: null, linesAvailable: true };
const prepareRequests = [];
preparePanel._request = async (operation, request) => {
  prepareRequests.push({ operation, request });
  return {
    status: "ready",
    selectionComplete: true,
    address: { key: "address-fresh", fullAddress: "123 Synthetic Avenue" },
    store: { storeHandle: "store-fresh", label: "Synthetic Store", isOpen: true, orderingAvailable: true },
    menu: { products: [{ productHandle: "product-fresh", label: "Fresh meal", priceMinor: 900, currency: "EUR", optionGroups: [] }] },
    selection: { products: [{ productHandle: "product-fresh", quantity: 2, options: [] }] },
  };
};
await preparePanel._preparePackage("pkg-synthetic", "");
assert.deepEqual(prepareRequests, []);
await preparePanel._preparePackage("pkg-synthetic", "address-fresh");
assert.deepEqual(prepareRequests.map((entry) => entry.operation), ["library/package_prepare"]);
assert.deepEqual(prepareRequests[0].request, { generation: 12, packageKey: "pkg-synthetic", addressHandle: "address-fresh" });
assert.equal(preparePanel._model.context.addressLabel, "123 Synthetic Avenue");
assert.equal(preparePanel._model.library.activeView, "menu");
assert.deepEqual(preparePanel._model.draft.lines.map((line) => [line.label, line.quantity]), [["Fresh meal", 2]]);
assert.equal(prepareRequests.some((entry) => entry.operation === "live/basket_set"), false);

// Package-card address options intentionally show the admin-only full address.
assert.equal(preparePanel._addressDisplay({ fullAddress: "123 Synthetic Avenue", label: "Masked" }), "123 Synthetic Avenue");

// Bootstrap may render packages before the parallel address read completes. Once
// addresses arrive, the active package view must rerender so its selectors gain
// the current full-address options instead of remaining permanently empty.
const addressRacePanel = new Panel();
addressRacePanel._generation = 13;
addressRacePanel._model.library.activeView = "packages";
const addressRaceEvents = [];
addressRacePanel._renderContext = () => { addressRaceEvents.push(["context", addressRacePanel._model.context.addresses.length]); };
addressRacePanel._renderPackages = () => { addressRaceEvents.push(["packages", addressRacePanel._model.context.addresses.length]); };
addressRacePanel._resetCatalog = () => {};
addressRacePanel._request = async (operation) => {
  assert.equal(operation, "live/addresses");
  return { addresses: [{ key: "address-race", fullAddress: "456 Synthetic Boulevard" }] };
};
await addressRacePanel._loadAddresses();
assert.deepEqual(addressRaceEvents, [["context", 1], ["packages", 1]]);
assert.equal(addressRacePanel._addressDisplay(addressRacePanel._model.context.addresses[0]), "456 Synthetic Boulevard");

panel._rendered = false;
panel._invalidateAuthority = Panel.prototype._invalidateAuthority.bind(panel);
panel._model.overlay = { type: "customizer" };
panel._model.quote = { challenge: "memory-only" };
panel._model.draft.lines = [{ productHandle: "ephemeral", quantity: 1, options: [] }];
panel._resetEphemeralGeneration("");
assert.equal(panel._model.overlay, null);
assert.equal(panel._model.quote, null);
assert.deepEqual(panel._model.draft.lines, []);

console.log("frontend interaction harness: PASS");
