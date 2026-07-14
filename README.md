# Pixio Integration for ComfyUI

Use the entire Pixio catalog (550+ models — Flux, Nano Banana, Kling, Veo, Runway,
ElevenLabs, Meshy, and more) from a single ComfyUI node. Pick a model from a
searchable dropdown and the node's widgets rebuild themselves to match that
model's parameters — a multifunctional swiss army knife for image, video, audio,
and 3D generation.

## Nodes

| Node | What it does |
| --- | --- |
| **Pixio Generation 🎛️ (any model)** | Run any Pixio model. Dynamic widgets per model, auto-upload of connected images/audio, polling, and download of results. |
| **Pixio API Key** | Holds your key so one node can feed many. |
| **Pixio Credits** | Check your remaining credit balance. |
| **Pixio Upload Media** | Upload an IMAGE/AUDIO/local file to Pixio and get a URL. |

## Installation

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/rossman22590/pixio-integration-node
pip install -r pixio-integration-node/requirements.txt
```

Restart ComfyUI.

## API key

Get a key (`pxio_live_...`) from [Pixio](https://beta.pixio.myapps.ai). Provide it one of three ways (checked in this order):

1. The `api_key` widget on the node (or a **Pixio API Key** node wired into `pixio_key`).
2. The `PIXIO_API_KEY` environment variable.
3. Copy `pixio_config.example.json` to `pixio_config.json` in this folder and put your key there.

> ⚠️ Your key is a credential. Don't commit `pixio_config.json` or share workflows with the key filled into the widget.

## Using the generation node

1. Add **Pixio → Pixio Generation 🎛️ (any model)**.
2. Enter your API key (or rely on the env var / config file).
3. Pick a model from the `model` dropdown (a catalog snapshot ships with the node, so the list works immediately; **🔄 Refresh Pixio models** pulls the current list your account can see). The node title shows the credit cost.
4. The model's parameters (aspect ratio, duration, voice, strength, …) appear as widgets. `select` params become dropdowns, `boolean` params become toggles, and `file` params become URL fields.
5. Write your prompt, queue, done.

### Inputs

- **prompt** — sent as the model's `prompt` parameter.
- **image_1 / image_2** — connected images are uploaded to Pixio automatically and mapped, in order, onto the model's image-file parameters (e.g. face swap's target + swap image). A URL typed into a file widget takes priority over the connected input.
- **audio** — same, for the model's audio parameter.
- **model_params** — the JSON the dynamic widgets write into; the Python node reads this. You can edit it by hand (or use the node entirely without the web extension this way).
- **seed** — passed to the model only if it has a `seed` parameter; otherwise it just forces a re-run when changed.

### Outputs

- **image** (IMAGE) — decoded image result(s), previewed on the node.
- **audio** (AUDIO) — decoded audio result.
- **media_url** (STRING) — the output URL (valid ~7 days).
- **file_path** (STRING) — every result is also saved to `ComfyUI/output/pixio/`. For video/3D outputs, use this path (e.g. with Video Helper Suite's *Load Video (Path)*).

For non-image models the `image` output is a 64×64 black placeholder (and vice versa for `audio`) so type validation stays happy — use the outputs that match the model's modality.

## Example: text → image → video chain

`Pixio Generation` (model `pixio/flux-1/schnell`, prompt) → `image` output into a second
`Pixio Generation` node's `image_1` (model set to any image-to-video model, e.g. Kling) →
take `file_path` / `media_url` from the second node.

## Notes

- Model catalog responses are cached for 5 minutes server-side; hit **Reload models** to refresh.
- Generations poll every 3 s until they succeed, fail, or hit `timeout_minutes`.
- Costs are charged by Pixio per generation (the credit price is shown in the node title and the console log after each run).

## Credits

- Built following the node pattern of [loadaudio](https://github.com/rossman22590/loadaudio)
  and the API reference from [pixio-skill](https://github.com/rossman22590/pixio-skill).
