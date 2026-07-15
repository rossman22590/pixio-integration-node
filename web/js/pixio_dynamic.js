// Pixio Integration — dynamic per-model widgets for all Pixio generation nodes.
//
// Flow:
//   1. Node dropdowns come from Python (bundled catalog snapshot), so every
//      node is usable even if this extension never loads.
//   2. On first Pixio node creation the catalog is fetched through
//      /pixio/models (the API key stays server-side; falls back to the bundled
//      snapshot when no key is set).
//   3. Selecting a model rebuilds the node's dynamic widgets from that model's
//      input schema. Params already covered by the node's native widgets
//      (prompt, seed, a domain node's aspect_ratio, ...) are skipped.
//   4. Every dynamic widget syncs its value into the `model_params` JSON
//      widget, which is what the Python node actually reads — so workflows
//      stay portable and everything still works without this extension.
//   5. Model changes are detected on the widget value itself, so rebuilds also
//      happen when a workflow is loaded or an embedding host app sets the
//      model programmatically (update_widget), not just on dropdown clicks.

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const UNIVERSAL_NODE = "PixioGeneration";
// Pixio nodes without a model dropdown — no dynamic behavior needed.
const NON_GENERATION_NODES = new Set([
    "PixioApiKey", "PixioCredits", "PixioUploadMedia",
]);
// Params that must never become dynamic widgets: they are handled by native
// widgets or injected by the Python node.
const ALWAYS_SKIP = new Set(["prompt", "seed"]);

function isPixioGenerationNode(name) {
    return typeof name === "string" && name.startsWith("Pixio") &&
        !NON_GENERATION_NODES.has(name);
}

// True when ComfyUI runs embedded in the Pixio workspace (iframe).
function isEmbedded() {
    try {
        return window.parent && window.parent !== window;
    } catch (e) {
        return false;
    }
}

function getWidget(node, name) {
    return node.widgets?.find((w) => w.name === name);
}

// ---------------------------------------------------------------------------
// catalog — fetched once, shared by every Pixio node on the canvas
// ---------------------------------------------------------------------------

let catalog = [];
let catalogPromise = null;

async function fetchModels(apiKey) {
    const resp = await api.fetchApi("/pixio/models", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ api_key: apiKey || "" }),
    });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(body.error || `HTTP ${resp.status}`);
    return body.models || [];
}

function loadCatalog(apiKey, force) {
    if (!catalogPromise || force) {
        catalogPromise = fetchModels(apiKey)
            .then((models) => {
                if (models.length) catalog = models;
                return catalog;
            })
            .catch((e) => {
                console.warn("[Pixio] failed to load model catalog:", e);
                if (force) throw e;
                return catalog;
            });
    }
    return catalogPromise;
}

// ---------------------------------------------------------------------------
// model_params <-> dynamic widgets
// ---------------------------------------------------------------------------

function readParams(node) {
    try {
        const parsed = JSON.parse(getWidget(node, "model_params")?.value || "{}");
        return typeof parsed === "object" && parsed !== null ? parsed : {};
    } catch (e) {
        return {};
    }
}

function syncParams(node) {
    const params = {};
    for (const w of node.widgets || []) {
        if (!w.pixioDynamic) continue;
        let v = w.value;
        if (w.pixioType === "number") v = Number(v);
        if (typeof v === "string") v = v.trim();
        if (v === "" || v === undefined || v === null || Number.isNaN(v)) continue;
        params[w.pixioName] = v;
    }
    const pw = getWidget(node, "model_params");
    if (pw) pw.value = JSON.stringify(params);
}

function clearDynamicWidgets(node) {
    if (!node.widgets) return;
    node.widgets = node.widgets.filter((w) => !w.pixioDynamic);
}

function addDynamicWidget(node, input, preset) {
    const label = input.label || input.name;
    const def = input.defaultValue;
    const onChange = () => syncParams(node);
    let w;
    switch (input.type) {
        case "boolean":
            w = node.addWidget("toggle", label, preset ?? !!def, onChange);
            break;
        case "number": {
            const start = preset ?? (def === "" || def === undefined || def === null
                ? 0 : Number(def));
            const isInt = Number.isInteger(start) && Number.isInteger(Number(def || 0));
            w = node.addWidget("number", label, start, onChange, {
                step: isInt ? 10 : 1,
                precision: isInt ? 0 : 3,
            });
            break;
        }
        case "select": {
            const values = (input.options || []).map((o) =>
                typeof o === "object" && o !== null ? o.value : o);
            w = node.addWidget("combo", label, preset ?? (def || values[0] || ""), onChange, {
                values,
            });
            break;
        }
        case "file":
            w = node.addWidget("text", label + " (url)", preset ?? "", onChange);
            break;
        default: // string, elements, loras, embeddings, finetune, ...
            w = node.addWidget("text", label, preset ?? (def ?? ""), onChange);
            break;
    }
    w.pixioDynamic = true;
    w.pixioName = input.name;
    w.pixioType = input.type;
    w.serialize = false;
    if (w.options) w.options.serialize = false;
    return w;
}

// ---------------------------------------------------------------------------
// per-model rebuild
// ---------------------------------------------------------------------------

function applyModel(node) {
    const modelId = getWidget(node, "model")?.value;
    const def = catalog.find((m) => m.id === modelId);
    // Unknown model or catalog not loaded yet — leave the node exactly as it
    // is (model_params included) so nothing the user typed gets destroyed.
    if (!def) return;

    const presets = readParams(node);
    clearDynamicWidgets(node);

    const credits = def.credits != null ? ` · ${def.credits} cr` : "";
    node.title = `Pixio — ${def.name || modelId}${credits}`;

    // Anything the node already has a native widget for stays native.
    const nativeNames = new Set((node.widgets || []).map((w) => w.name));
    for (const input of def.inputs || []) {
        if (ALWAYS_SKIP.has(input.name) || nativeNames.has(input.name)) continue;
        addDynamicWidget(node, input, presets[input.name]);
    }

    syncParams(node);
    node.setSize(node.computeSize());
    node.setDirtyCanvas(true, true);
}

function scheduleApply(node, delay = 50) {
    clearTimeout(node.__pixioApplyTimer);
    node.__pixioApplyTimer = setTimeout(() => applyModel(node), delay);
}

// Rebuild on ANY change to the model widget's value — dropdown clicks,
// workflow loads, and programmatic updates from an embedding host app.
function watchModelWidget(node) {
    const mw = getWidget(node, "model");
    if (!mw || mw.__pixioWatched) return;
    mw.__pixioWatched = true;

    const desc = Object.getOwnPropertyDescriptor(mw, "value");
    let plain = mw.value;
    try {
        Object.defineProperty(mw, "value", {
            configurable: true,
            enumerable: true,
            get() {
                return desc?.get ? desc.get.call(mw) : plain;
            },
            set(v) {
                const prev = desc?.get ? desc.get.call(mw) : plain;
                if (desc?.set) desc.set.call(mw, v);
                else plain = v;
                if (v !== prev) scheduleApply(node);
            },
        });
    } catch (e) {
        // property not configurable on this frontend — fall back to callback only
    }

    const original = mw.callback;
    mw.callback = (value, ...rest) => {
        original?.call(mw, value, ...rest);
        scheduleApply(node);
    };
}

// ---------------------------------------------------------------------------
// node setup
// ---------------------------------------------------------------------------

function addPanelButton(node) {
    // Inside the Pixio workspace iframe: button that opens the Pixio Models
    // panel in the host app, targeting this node (same postMessage bridge the
    // assets browser uses).
    const btn = node.addWidget("button", "🎛 Configure in Pixio panel", null, () => {
        window.parent.postMessage(JSON.stringify({
            type: "pixio",
            data: { node: node.id },
        }), "*");
    });
    btn.name = "__pixio_panel";
    btn.serialize = false;
    if (btn.options) btn.options.serialize = false;
}

function addRefreshButton(node) {
    const btn = node.addWidget("button", "🔄 Refresh Pixio models", null, async () => {
        btn.label = "⏳ Loading models…";
        node.setDirtyCanvas(true, true);
        try {
            const models = await loadCatalog(getWidget(node, "api_key")?.value, true);
            const mw = getWidget(node, "model");
            if (mw?.options) {
                // widget is already a combo (defined in Python) — swap in the live list
                mw.options.values = models.map((m) => m.id);
            }
            btn.label = `🔄 Reload models (${models.length})`;
            applyModel(node);
        } catch (e) {
            btn.label = "🔄 Refresh Pixio models";
            alert("Pixio: could not load models — " + e.message);
        } finally {
            node.setDirtyCanvas(true, true);
        }
    });
    btn.name = "__pixio_load";
    btn.serialize = false;
    if (btn.options) btn.options.serialize = false;
}

function setupPixioNode(node, isUniversal) {
    if (node.__pixioSetup) return;
    node.__pixioSetup = true;

    // buttons are appended after the static widgets so their indices in
    // widgets_values stay identical whether or not this extension is loaded
    // (workflow portability); dynamic widgets never serialize at all
    if (isUniversal) addRefreshButton(node);
    if (isEmbedded()) addPanelButton(node);
    watchModelWidget(node);

    loadCatalog(getWidget(node, "api_key")?.value)
        .then(() => scheduleApply(node));
}

app.registerExtension({
    name: "pixio.dynamic",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (!isPixioGenerationNode(nodeData.name)) return;
        const isUniversal = nodeData.name === UNIVERSAL_NODE;

        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            onNodeCreated?.apply(this, arguments);
            setupPixioNode(this, isUniversal);
        };

        const onConfigure = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function () {
            onConfigure?.apply(this, arguments);
            // widget values (model, model_params) are restored now — rebuild
            // the dynamic widgets from them once the catalog is ready
            loadCatalog(getWidget(this, "api_key")?.value)
                .then(() => scheduleApply(this, 100));
        };
    },
});
