// Pixio Integration — dynamic model picker + per-model widgets for PixioGeneration.
//
// Flow:
//   1. "Load Pixio models" fetches the catalog through /pixio/models (key stays server-side).
//   2. The `model` text widget is upgraded to a searchable combo of all model ids.
//   3. Selecting a model rebuilds the node's widgets from that model's input schema.
//   4. Every dynamic widget syncs its value into the hidden-in-plain-sight `model_params`
//      JSON widget, which is what the Python node actually reads — so workflows stay
//      portable and everything still works even if this extension fails to load.

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const NODE_NAME = "PixioGeneration";

function getWidget(node, name) {
    return node.widgets?.find((w) => w.name === name);
}

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
            const start = preset ?? (def === "" || def === undefined ? 0 : Number(def));
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

function applyModel(node, modelId, presets) {
    const def = (node.__pixioModels || []).find((m) => m.id === modelId);
    clearDynamicWidgets(node);
    if (def) {
        const credits = def.credits != null ? ` · ${def.credits} cr` : "";
        node.title = `Pixio — ${def.name || modelId}${credits}`;
        for (const input of def.inputs || []) {
            // The node already has a dedicated multiline prompt widget.
            if (input.name === "prompt" && input.type === "string") continue;
            addDynamicWidget(node, input, presets ? presets[input.name] : undefined);
        }
    }
    syncParams(node);
    node.setSize(node.computeSize());
    node.setDirtyCanvas(true, true);
}

async function refreshModels(node, silent) {
    if (node.__pixioLoading) return;
    node.__pixioLoading = true;
    const btn = getWidget(node, "__pixio_load");
    if (btn) btn.label = "⏳ Loading models…";
    node.setDirtyCanvas(true, true);
    try {
        const models = await fetchModels(getWidget(node, "api_key")?.value);
        node.__pixioModels = models;
        const mw = getWidget(node, "model");
        if (mw) {
            mw.type = "combo";
            mw.options = Object.assign({}, mw.options, {
                values: models.map((m) => m.id),
            });
        }
        if (btn) btn.label = `🔄 Reload models (${models.length})`;
        applyModel(node, mw?.value, readParams(node));
    } catch (e) {
        if (btn) btn.label = "🔄 Load Pixio models";
        console.warn("[Pixio] failed to load model catalog:", e);
        if (!silent) alert("Pixio: could not load models — " + e.message);
    } finally {
        node.__pixioLoading = false;
        node.setDirtyCanvas(true, true);
    }
}

function setupNode(node) {
    if (node.__pixioSetup) return;
    node.__pixioSetup = true;

    const btn = node.addWidget("button", "🔄 Load Pixio models", null, () =>
        refreshModels(node, false));
    btn.name = "__pixio_load";
    btn.serialize = false;
    if (btn.options) btn.options.serialize = false;

    // place the button right below the model widget
    const bi = node.widgets.indexOf(btn);
    node.widgets.splice(bi, 1);
    const mi = node.widgets.findIndex((w) => w.name === "model");
    node.widgets.splice(mi + 1, 0, btn);

    const mw = getWidget(node, "model");
    if (mw) {
        const original = mw.callback;
        mw.callback = (value, ...rest) => {
            original?.call(mw, value, ...rest);
            applyModel(node, value, null);
        };
    }

    // auto-load when a key is resolvable (widget, env var, or config file)
    setTimeout(() => refreshModels(node, true), 300);
}

app.registerExtension({
    name: "pixio.dynamic",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== NODE_NAME) return;

        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            onNodeCreated?.apply(this, arguments);
            setupNode(this);
        };

        const onConfigure = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function () {
            onConfigure?.apply(this, arguments);
            // widget values are restored now — rebuild dynamic widgets from model_params
            setTimeout(() => refreshModels(this, true), 100);
        };
    },
});
