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

panel._model.overlay = { type: "customizer" };
panel._model.quote = { challenge: "memory-only" };
panel._model.draft.lines = [{ productHandle: "ephemeral", quantity: 1, options: [] }];
panel._resetEphemeralGeneration("");
assert.equal(panel._model.overlay, null);
assert.equal(panel._model.quote, null);
assert.deepEqual(panel._model.draft.lines, []);

console.log("frontend interaction harness: PASS");
