import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const panelPath = resolve(process.argv[2] || "custom_components/glovo/frontend/glovo-ordering-panel.js");
const source = await readFile(panelPath, "utf8");
let shadowMode = "";
const registry = new Map();

globalThis.HTMLElement = class {
  attachShadow(options) {
    shadowMode = options.mode;
    this.shadowRoot = { querySelector: () => null };
    return this.shadowRoot;
  }
};
globalThis.customElements = {
  define(name, value) { registry.set(name, value); },
  get(name) { return registry.get(name); },
};

await import(`${pathToFileURL(panelPath).href}?harness=${Date.now()}`);
const Panel = registry.get("glovo-ordering-panel");
assert.ok(Panel, "component registered");
const panel = new Panel();
assert.equal(shadowMode, "open");

if (process.argv.includes("--emit-adoption-payload")) {
  const emitPanel = new Panel();
  emitPanel._generation = 7;
  emitPanel._model.context.addressHandle = "address-admin-a";
  emitPanel._model.context.store = { storeHandle: "store-admin-a", isOpen: true, orderingAvailable: true };
  emitPanel._model.draft.lines = [{ productHandle: "product-admin-a", quantity: 2, options: [] }];
  process.stdout.write(`${JSON.stringify(emitPanel._basketAdoptionRequest())}\n`);
  process.exit(0);
}

assert.deepEqual(
  Object.keys(panel._model),
  ["lifecycle", "capability", "context", "menu", "draft", "basket", "payments", "quote", "overlay", "library", "recovery"],
);
assert.equal(panel._model.basket.status, "unknown");

// Basket authority is a closed four-state frontend contract. The adoption
// adapter sends only current opaque handles, whitelists its response, and never
// retains provider-private identifiers.
const authorityStatuses = ["unknown", "absent_verified", "adopted", "conflict"];
for (const status of authorityStatuses) {
  assert.equal(panel._parseBasketAdoptionResponse({ status }).status, status);
}
assert.equal(panel._parseBasketAdoptionResponse({ status: "surprise" }).status, "unknown");
panel._generation = 41;
panel._model.context.addressHandle = "address-current";
panel._model.context.store = { storeHandle: "store-current", isOpen: true, orderingAvailable: true };
panel._model.draft.lines = [{
  productHandle: "product-current",
  label: "Current meal",
  quantity: 2,
  unitPriceMinor: 100,
  currency: "EUR",
  options: [{ groupHandle: "group-current", optionHandles: ["option-current"], optionLabels: ["Current option"], deltaMinor: 0 }],
}];
assert.deepEqual(panel._basketAdoptionRequest(), {
  generation: 41,
  addressHandle: "address-current",
  storeHandle: "store-current",
  products: [{
    productHandle: "product-current",
    quantity: 2,
    options: [{ groupHandle: "group-current", optionHandles: ["option-current"] }],
  }],
});
const privateResponse = panel._parseBasketAdoptionResponse({
  status: "adopted",
  revision: 7,
  itemCount: 2,
  currency: "EUR",
  providerTotal: 200,
  lines: [],
  providerId: "must-not-survive",
  checkoutSession: "must-not-survive",
});
assert.deepEqual(privateResponse, {
  status: "adopted",
  basket: { revision: 7, itemCount: 2, currency: "EUR", providerTotal: 200, linesAvailable: true },
});
assert.equal(JSON.stringify(privateResponse).includes("must-not-survive"), false);

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

// The fresh unknown state is fail-closed. A delegated desktop/mobile action uses
// the same handler, and one click makes exactly one adoption call with zero
// provider mutation commands.
for (const role of ["desktop", "mobile"]) {
  let delegatedCalls = 0;
  const delegatedPanel = new Panel();
  delegatedPanel._adoptBasket = () => { delegatedCalls += 1; };
  delegatedPanel._onClick({ target: { closest: () => ({ dataset: { action: "adopt-basket", role } }) } });
  assert.equal(delegatedCalls, 1, `${role} adoption control must use delegated action handling`);
}

const adoptionPanel = new Panel();
adoptionPanel._generation = 51;
adoptionPanel._model.context.addressHandle = "address-adoption";
adoptionPanel._model.context.store = { storeHandle: "store-adoption", isOpen: true, orderingAvailable: true };
adoptionPanel._model.draft.lines = [{ productHandle: "product-adoption", label: "Meal", quantity: 1, unitPriceMinor: 450, currency: "EUR", options: [] }];
adoptionPanel._model.quote = { challenge: "stale-quote" };
adoptionPanel._confirmation = { kind: "clear-basket" };
adoptionPanel._renderBasketSurfaces = () => {};
adoptionPanel._renderAll = () => {};
adoptionPanel._setLifecycle = () => {};
const adoptionRequests = [];
let releaseAdoption;
const adoptionGate = new Promise((resolveGate) => { releaseAdoption = resolveGate; });
adoptionPanel._request = async (operation, request) => {
  adoptionRequests.push({ operation, request });
  await adoptionGate;
  return {
    status: "adopted",
    revision: 8,
    itemCount: 1,
    currency: "EUR",
    providerTotal: 450,
    providerId: "private-provider-value",
  };
};
adoptionPanel._onClick({ target: { closest: () => ({ dataset: { action: "adopt-basket", role: "desktop" } }) } });
await Promise.resolve();
assert.equal(adoptionRequests.length, 1);
assert.equal(adoptionRequests[0].operation, "live/basket_adopt");
assert.deepEqual(adoptionRequests[0].request, {
  generation: 51,
  addressHandle: "address-adoption",
  storeHandle: "store-adoption",
  products: [{ productHandle: "product-adoption", quantity: 1, options: [] }],
});
assert.equal(adoptionPanel._model.quote, null, "adoption invalidates quote authority before I/O completes");
assert.equal(adoptionPanel._confirmation, null, "adoption invalidates pending confirmation before I/O completes");
releaseAdoption();
await adoptionPanel._singleFlights.get("basket-adopt");
assert.equal(adoptionPanel._model.basket.status, "adopted");
assert.equal(JSON.stringify(adoptionPanel._model).includes("private-provider-value"), false);
assert.equal(adoptionRequests.filter(({ operation }) => ["live/basket_set", "live/basket_clear", "live/create_quote", "live/execute_checkout"].includes(operation)).length, 0);

// An exact adopted basket remains the immutable replace baseline while local
// quantity/customization/removal edits alter only the outgoing draft. One
// explicit update uses the adopted revision and never re-adopts or falls back to
// create; quote/payment continuation remains blocked until replacement succeeds.
const replacementPanel = new Panel();
replacementPanel._generation = 61;
replacementPanel._runtimeEpoch = "runtime-replacement";
replacementPanel._handlesIssuedAt = Date.now();
replacementPanel._model.context.addressHandle = "address-replacement";
replacementPanel._model.context.store = { storeHandle: "store-replacement", isOpen: true, orderingAvailable: true };
replacementPanel._model.menu.products = [{
  productHandle: "product-custom",
  label: "Custom meal",
  priceMinor: 500,
  currency: "EUR",
  optionGroups: [{
    groupHandle: "group-sauce",
    label: "Sauce",
    min: 1,
    max: 1,
    options: [
      { optionHandle: "option-mild", label: "Mild", priceMinor: 0, currency: "EUR" },
      { optionHandle: "option-hot", label: "Hot", priceMinor: 25, currency: "EUR" },
    ],
  }],
}];
replacementPanel._model.draft.lines = [
  { productHandle: "product-custom", label: "Custom meal", quantity: 1, unitPriceMinor: 500, currency: "EUR", options: [{ groupHandle: "group-sauce", optionHandles: ["option-mild"], optionLabels: ["Mild"], deltaMinor: 0 }] },
  { productHandle: "product-remove", label: "Remove me", quantity: 1, unitPriceMinor: 100, currency: "EUR", options: [] },
];
replacementPanel._renderBasketSurfaces = () => {};
replacementPanel._renderAll = () => {};
replacementPanel._setLifecycle = () => {};
const replacementRequests = [];
replacementPanel._request = async (operation, request) => {
  replacementRequests.push({ operation, request });
  if (operation === "live/basket_adopt") return { status: "adopted", revision: 1, itemCount: 2, currency: "EUR", providerTotal: 600, lines: [] };
  if (operation === "state") return { generation: 61, runtimeEpoch: "runtime-replacement", liveOrderingAvailable: true };
  if (operation === "live/basket_set") return { status: "adopted", revision: 2, itemCount: 2, currency: "EUR", providerTotal: 1050, lines: [] };
  if (operation === "live/payment_methods") return { paymentMethods: [] };
  throw new Error(`unexpected operation: ${operation}`);
};
await replacementPanel._adoptBasket();
assert.equal(replacementPanel._model.basket.status, "adopted");
assert.equal(replacementPanel._model.basket.revision, 1);
replacementRequests.length = 0;
replacementPanel._model.quote = { challenge: "must-be-discarded" };
replacementPanel._model.payments = { status: "ready", items: [{ key: "payment-before-edit" }], selected: "payment-before-edit" };
replacementPanel._confirmation = { kind: "clear-basket" };
replacementPanel._stepLine("product-custom", 1);
replacementPanel._model.overlay = {
  type: "customizer",
  editing: true,
  product: replacementPanel._model.menu.products[0],
  quantity: 2,
  selections: new Map([["group-sauce", new Set(["option-hot"])]]),
  errors: new Map(),
};
replacementPanel._commitCustomization();
replacementPanel._removeLine("product-remove");
assert.equal(replacementPanel._model.basket.status, "adopted");
assert.equal(replacementPanel._model.basket.revision, 1);
assert.equal(replacementPanel._model.quote, null);
assert.equal(replacementPanel._confirmation, null);
assert.deepEqual(replacementPanel._model.payments, { status: "idle", items: [], selected: "" });
await replacementPanel._loadPayments();
await replacementPanel._createQuote();
assert.deepEqual(replacementRequests, [], "dirty adopted drafts block quote/payment before replacement");
await replacementPanel._syncBasket();
assert.deepEqual(replacementRequests.map(({ operation }) => operation), ["state", "live/basket_set", "live/payment_methods"]);
const replaceRequest = replacementRequests.find(({ operation }) => operation === "live/basket_set").request;
assert.deepEqual(replaceRequest, {
  generation: 61,
  expectedRevision: 1,
  storeHandle: "store-replacement",
  addressHandle: "address-replacement",
  products: [{ productHandle: "product-custom", quantity: 2, options: [{ groupHandle: "group-sauce", optionHandles: ["option-hot"] }] }],
});
assert.equal(replacementRequests.filter(({ operation }) => operation === "live/basket_adopt").length, 0);
assert.equal(replacementRequests.filter(({ operation }) => operation === "live/create_quote").length, 0);
assert.equal(replacementPanel._model.basket.status, "adopted");
assert.equal(replacementPanel._model.basket.revision, 2);
assert.equal(replacementPanel._model.draft.dirty, false);
assert.equal(replacementPanel._adoptedBasketBaseline.revision, 2);
assert.deepEqual(replacementPanel._adoptedBasketBaseline.products, replaceRequest.products);

// A selection change while adoption is in flight rejects the stale result even
// if the generation is unchanged.
const staleAdoptionPanel = new Panel();
staleAdoptionPanel._generation = 52;
staleAdoptionPanel._model.context.addressHandle = "address-stale";
staleAdoptionPanel._model.context.store = { storeHandle: "store-stale", isOpen: true, orderingAvailable: true };
staleAdoptionPanel._model.draft.lines = [{ productHandle: "product-before", label: "Before", quantity: 1, unitPriceMinor: 100, currency: "EUR", options: [] }];
staleAdoptionPanel._renderBasketSurfaces = () => {};
staleAdoptionPanel._renderAll = () => {};
staleAdoptionPanel._setLifecycle = () => {};
let releaseStaleAdoption;
const staleAdoptionGate = new Promise((resolveGate) => { releaseStaleAdoption = resolveGate; });
staleAdoptionPanel._request = async () => { await staleAdoptionGate; return { status: "adopted", revision: 9, itemCount: 1 }; };
const staleAdoption = staleAdoptionPanel._adoptBasket();
await Promise.resolve();
staleAdoptionPanel._model.draft.lines[0].productHandle = "product-after";
staleAdoptionPanel._invalidateBasketAdoption();
releaseStaleAdoption();
await staleAdoption;
assert.equal(staleAdoptionPanel._model.basket.status, "unknown");
assert.equal(staleAdoptionPanel._model.basket.revision, 0);

// Adopted draft edits retain the exact provider baseline while invalidating all
// draft-derived continuation. Verified absence remains selection-bound.
const boundaryPanel = new Panel();
boundaryPanel._generation = 55;
boundaryPanel._renderAll = () => {};
boundaryPanel._setLifecycle = () => {};
boundaryPanel._model.context.addressHandle = "address-boundary";
boundaryPanel._model.context.store = { storeHandle: "store-boundary", isOpen: true, orderingAvailable: true };
boundaryPanel._model.basket.status = "adopted";
boundaryPanel._model.basket.revision = 1;
boundaryPanel._recordAdoptedBasketBaseline({ generation: 55, addressHandle: "address-boundary", storeHandle: "store-boundary", products: [] }, 1);
boundaryPanel._model.payments = { status: "ready", items: [{ key: "payment-draft" }], selected: "payment-draft" };
boundaryPanel._model.quote = { challenge: "draft-quote" };
boundaryPanel._confirmation = { kind: "clear-basket" };
boundaryPanel._draftChanged("Synthetic draft change.");
assert.equal(boundaryPanel._model.basket.status, "adopted");
assert.equal(boundaryPanel._model.basket.revision, 1);
assert.deepEqual(boundaryPanel._model.payments, { status: "idle", items: [], selected: "" });
assert.equal(boundaryPanel._model.quote, null);
assert.equal(boundaryPanel._confirmation, null);
boundaryPanel._model.basket.status = "absent_verified";
boundaryPanel._model.basket.revision = 0;
boundaryPanel._draftChanged("Synthetic absence-bound draft change.");
assert.equal(boundaryPanel._model.basket.status, "unknown");
assert.equal(boundaryPanel._model.basket.revision, 0);

// Address and store boundaries still discard adoption completely.
boundaryPanel._model.basket.status = "adopted";
boundaryPanel._model.quote = { challenge: "address-quote" };
boundaryPanel._confirmation = { kind: "clear-basket" };
boundaryPanel._onChange({ target: { id: "address-select", value: "address-new", selectedOptions: [{ textContent: "New address" }], dataset: {} } });
assert.equal(boundaryPanel._model.basket.status, "unknown");
assert.equal(boundaryPanel._model.quote, null);
assert.equal(boundaryPanel._confirmation, null);
boundaryPanel._model.context.addressHandle = "address-new";
boundaryPanel._model.context.store = { storeHandle: "store-old", isOpen: true, orderingAvailable: true };
boundaryPanel._model.basket.status = "adopted";
boundaryPanel._model.quote = { challenge: "store-quote" };
boundaryPanel._confirmation = { kind: "clear-basket" };
boundaryPanel._request = async () => ({ storeHandle: "store-new", isOpen: true, orderingAvailable: true, products: [] });
await boundaryPanel._loadStoreMenu({ storeHandle: "store-new", isOpen: true, orderingAvailable: true });
assert.equal(boundaryPanel._model.basket.status, "unknown");
assert.equal(boundaryPanel._model.quote, null);
assert.equal(boundaryPanel._confirmation, null);

// Unknown/conflict block every sync/quote/payment/pay path when methods are
// invoked directly, not only through disabled controls.
for (const status of ["unknown", "conflict"]) {
  const blockedPanel = new Panel();
  blockedPanel._generation = 53;
  blockedPanel._model.basket.status = status;
  blockedPanel._model.context.addressHandle = "address-blocked";
  blockedPanel._model.context.store = { storeHandle: "store-blocked", isOpen: true, orderingAvailable: true };
  blockedPanel._model.draft.lines = [{ productHandle: "product-blocked", quantity: 1, options: [] }];
  blockedPanel._model.payments.selected = "payment-blocked";
  blockedPanel._model.capability.liveCheckoutAvailable = true;
  blockedPanel._model.quote = { typed: "ACK", challenge: "challenge", ackText: "ACK" };
  blockedPanel._ackText = "ACK";
  blockedPanel._setLifecycle = () => {};
  let blockedRequests = 0;
  blockedPanel._request = async () => { blockedRequests += 1; return {}; };
  await blockedPanel._syncBasket();
  await blockedPanel._loadPayments();
  await blockedPanel._createQuote();
  await blockedPanel._prepareConfirmation();
  await blockedPanel._submitCheckout();
  assert.equal(blockedRequests, 0, `${status} must block all mutation/quote/payment calls`);
}

// An adopted exact selection can quote directly without creating a basket.
const adoptedQuotePanel = new Panel();
adoptedQuotePanel._generation = 54;
adoptedQuotePanel._model.basket.status = "adopted";
adoptedQuotePanel._model.context.addressHandle = "address-quote";
adoptedQuotePanel._model.payments.selected = "payment-quote";
adoptedQuotePanel._setLifecycle = () => {};
adoptedQuotePanel._renderAll = () => {};
adoptedQuotePanel._startQuoteExpiry = () => {};
const adoptedQuoteRequests = [];
adoptedQuotePanel._request = async (operation, request) => {
  adoptedQuoteRequests.push({ operation, request });
  return { purchaseTotalCents: 777, currencyCode: "EUR", items: [] };
};
await adoptedQuotePanel._createQuote();
assert.deepEqual(adoptedQuoteRequests.map(({ operation }) => operation), ["live/create_quote"]);
assert.equal(adoptedQuoteRequests.some(({ operation }) => operation === "live/basket_set"), false);
assert.ok(adoptedQuotePanel._model.quote);

// All direct basket-status writes remain inside the closed public state set.
const directBasketStatuses = [...source.matchAll(/this\._model\.basket(?:\.status\s*=|\s*=\s*\{\s*status:)\s*"([^"]+)"/g)].map((match) => match[1]);
assert.ok(directBasketStatuses.length > 0);
assert.deepEqual([...new Set(directBasketStatuses.filter((status) => !authorityStatuses.includes(status)))], []);
const shellIds = [...source.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]);
assert.equal(new Set(shellIds).size, shellIds.length, "static shell IDs must be unique");
const adoptionButtonSource = source.slice(source.indexOf("_basketAdoptionButton(role)"), source.indexOf("_renderBasketInto", source.indexOf("_basketAdoptionButton(role)")));
assert.equal(adoptionButtonSource.includes(".id ="), false, "responsive adoption controls use delegated roles, not duplicate IDs");

// The paid acknowledgement is bound to exact minor units and currency. Submit
// itself is single-flight, consumes the challenge before I/O, and is inert while
// capability is unresolved/false.
const checkoutPanel = new Panel();
checkoutPanel._generation = 19;
checkoutPanel._model.basket.status = "adopted";
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
authorityPanel._model.basket.status = "absent_verified";
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
authorityPanel._model.basket.status = "absent_verified";
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
freshPanel._model.basket.status = "absent_verified";
const freshRequests = [];
freshPanel._request = async (operation) => {
  freshRequests.push(operation);
  if (operation === "state") return { generation: 9, runtimeEpoch: "runtime-fresh", liveOrderingAvailable: true };
  assert.equal(operation, "live/basket_set");
  return { revision: 1, itemCount: 1, currency: "EUR", providerTotal: 100, lines: [] };
};
await freshPanel._syncBasket();
assert.deepEqual(freshRequests, ["state", "live/basket_set"]);
assert.equal(freshPanel._model.basket.status, "adopted");

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

// Preparation recovery is a separate admin-only, no-provider-I/O challenge flow.
const recoveryPanel = new Panel();
recoveryPanel._renderAll = () => {};
recoveryPanel._invalidateAuthority = () => {};
recoveryPanel.shadowRoot = {
  querySelector(selector) {
    if (selector === "#preparation-ack") return { checked: true };
    return null;
  },
};
const recoveryRequests = [];
recoveryPanel._hass = {
  async callWS(request) {
    recoveryRequests.push(request);
    if (request.type === "glovo/ordering/preparation_check") {
      return { generation: 21, recordRevision: 3, state: "RECONCILIATION_REQUIRED", purposeCategory: "basket" };
    }
    if (request.type === "glovo/ordering/prepare_preparation_resolution") {
      return { challenge: "operator-challenge" };
    }
    if (request.type === "glovo/ordering/resolve_preparation_check") {
      return { resolved: true, preparationRecoveryRequired: false, reloadRequired: true };
    }
    if (request.type === "glovo/ordering/state") {
      return { generation: 22, runtimeEpoch: "reloaded", liveOrderingAvailable: false };
    }
    throw new Error(`unexpected operation: ${request.type}`);
  },
};
await recoveryPanel._loadRecovery({ preparationRecoveryRequired: true });
assert.equal(recoveryPanel._model.recovery.kind, "preparation");
await recoveryPanel._resolvePreparation("found_failed_or_cancelled");
assert.deepEqual(recoveryRequests.map((request) => request.type), [
  "glovo/ordering/preparation_check",
  "glovo/ordering/prepare_preparation_resolution",
  "glovo/ordering/resolve_preparation_check",
  "glovo/ordering/state",
]);
assert.deepEqual(recoveryRequests[1], {
  type: "glovo/ordering/prepare_preparation_resolution",
  expectedGeneration: 21,
  expectedRecordRevision: 3,
  expectedState: "RECONCILIATION_REQUIRED",
  resolution: "found_failed_or_cancelled",
});
assert.deepEqual(recoveryRequests[2], {
  ...recoveryRequests[1],
  type: "glovo/ordering/resolve_preparation_check",
  challenge: "operator-challenge",
  acknowledged: true,
});
assert.equal(recoveryRequests.some((request) => request.type.startsWith("glovo/ordering/live/")), false);

panel._rendered = false;
panel._invalidateAuthority = Panel.prototype._invalidateAuthority.bind(panel);
panel._model.overlay = { type: "customizer" };
panel._model.quote = { challenge: "memory-only" };
panel._confirmation = { kind: "clear-basket" };
panel._model.basket.status = "adopted";
panel._model.draft.lines = [{ productHandle: "ephemeral", quantity: 1, options: [] }];
panel._resetEphemeralGeneration("");
assert.equal(panel._model.overlay, null);
assert.equal(panel._model.quote, null);
assert.equal(panel._confirmation, null);
assert.equal(panel._model.basket.status, "unknown");
assert.deepEqual(panel._model.draft.lines, []);

console.log("frontend interaction harness: PASS");
