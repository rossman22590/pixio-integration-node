# Pixio Generation 🎛️ — ComfyUI custom node

**One node. 550+ AI models.** Run the entire Pixio catalog — Flux, Nano Banana, Seedream, Kling, Seedance, Veo, Grok Imagine, ElevenLabs, Meshy, and more — from a single node that **transforms itself** to match whatever model you pick.

Select a model from the dropdown and the node rebuilds live:

- its **widgets** become that model's exact parameters (aspect ratio, duration, CFG, voice, …)
- its **input sockets** become what the model actually takes — an image-to-video model shows an IMAGE socket, a lipsync model shows IMAGE + AUDIO, text-to-image shows none
- its **title** shows the model name and the credit cost per run

Generate images, video, audio, and 3D without ever leaving your ComfyUI graph.

---

## Where to find it

After installing, either:

- **Node menu** → `Pixio` → **Pixio Generation 🎛️ (any model)**, or
- **double-click the canvas** and search `pixio` (or a model name like `kling`, `flux`, `seedance`)

---

## Quick start

1. **Install the node.** Add this repo to your machine's custom nodes and rebuild:
   `https://github.com/rossman22590/pixio-integration-node`
   (Local ComfyUI: clone into `ComfyUI/custom_nodes/`, `pip install -r requirements.txt`, restart.)
2. **Add your API key.** Get a key (`pxio_live_…`) from your Pixio account → **API Keys**. Set it once as the `PIXIO_API_KEY` environment variable on the machine — every Pixio node then works with the key field left empty. (You can also paste it into the node's `api_key` widget, but see the security note below.)
3. **Pick a model and run.** Choose from the searchable `model` dropdown, watch the node transform, write a prompt, hit **Run**. The result previews right on the node and is saved to `output/pixio/`.

> 🔐 **Security note:** a key typed into the `api_key` widget gets saved inside the workflow file. Anyone you share that workflow with can spend your credits. For anything you might share or deploy, use the `PIXIO_API_KEY` env var and keep the widget empty.

---

## How the node transforms

Everything is driven by each model's real input schema:

| You pick… | The node shows… |
| --- | --- |
| Flux Schnell (text-to-image) | `image_size`, `steps`, `guidance`, `output format` widgets — no media sockets |
| Kling (image-to-video) | `duration`, `aspect ratio`, `CFG` widgets + an **IMAGE** socket labeled with the model's real input name |
| OmniHuman (lipsync) | **IMAGE + AUDIO** sockets |
| Seedance 2 Direct (text-to-video) | `input mode`, `duration`, `resolution`, `ratio`, `generate audio` + reference image/video/audio sockets |

The node rebuilds when you change the dropdown, when a workflow loads, and when a host app sets the model programmatically. Behind the scenes every widget writes into the `model_params` JSON field — the single source of truth the backend reads — so saved workflows and API runs behave identically even on frontends that never load the extension.

**On the Pixio platform**, every Pixio node also gets a **🎛 Configure in Pixio panel** button that opens the workspace's model browser: search all models grouped by type, see credit costs, fill the model's parameters in a form, and apply it to the node in one click.

---

## Inputs

| Input | Type | What it does |
| --- | --- | --- |
| `prompt` | text | Sent as the model's prompt. |
| `image_1 … image_4` | IMAGE | Uploaded to Pixio automatically and mapped, in order, onto the model's image inputs (e.g. face swap's target + swap image). |
| `video_1 / video_2` | VIDEO | Same, for video inputs — chain another Pixio node's `video` output straight in for video-to-video. |
| `audio` | AUDIO | Same, for the model's audio input. |
| `model_params` | JSON | Auto-filled by the widgets. Hand-edit only for exotic per-model extras; values here always win. |
| `seed` | INT | Sent to models that accept a seed (safely clamped to their range); otherwise just forces a re-run. |
| `pixio_key` | STRING | Optional link from a **Pixio API Key** node so one key feeds many nodes. |

With the extension loaded, only the sockets the selected model uses are shown, labeled with the model's own parameter names. Without it, the full socket pool is visible and unused sockets are simply ignored. Parameters the selected model doesn't accept are automatically dropped before sending — switching models never produces "unknown parameter" API errors.

## Outputs

| Output | Type | Use it for |
| --- | --- | --- |
| `image` | IMAGE | Decoded result(s), previewed on the node → **Save Image**, upscalers, or another Pixio node. For video results this carries the first frame. |
| `video` | VIDEO | Native ComfyUI video → core **Save Video**. Video results also play inline on the node. |
| `audio` | AUDIO | → **Save Audio** / **Preview Audio**. An inline player appears on the node too. |
| `media_url` | STRING | The hosted output URL (valid ~7 days). |
| `file_path` | STRING | Every result is **also saved automatically** to `output/pixio/` — handy for 3D files or path-based loaders. |

Outputs that don't match the model's modality are safe placeholders, so type validation never complains.

---

## Recipes

**Text → Image:** Pixio Generation (any text-to-image model) → `image` → Save Image.

**Image → Video:** connect any IMAGE into the node's image socket, pick an image-to-video model (Kling, Seedance…), `video` → Save Video.

**Chain generations:** Pixio (Flux, text-to-image) `image` → second Pixio node's image socket (Kling, image-to-video) → `video` → Save Video. Two nodes, prompt to movie.

**Lipsync:** connect a face IMAGE and a voice AUDIO (e.g. from a text-to-audio Pixio node), pick a lipsync model, run.

**Save an existing result without paying again:** set `control after generate` to **fixed** and Run — the cached generation is reused instantly and only the Save node executes. On **randomize**, every Run is a brand-new (billed) generation.

---

## The other Pixio nodes

| Node | What it does |
| --- | --- |
| **Pixio API Key** | Holds your key once; wire its output into any number of nodes' `pixio_key`. |
| **Pixio Credits** | Live balance check — connect its `image` output to a Preview Image node to see your credits on the canvas every run. |
| **Pixio Upload Media** | Turn any IMAGE / AUDIO / local file into a hosted Pixio URL for use in `model_params`. |

---

## Credits & costs

- The node title and the model browser show each model's **credit cost per run** before you spend anything.
- The console logs the actual billed cost after every run: `[Pixio] done — cost N credits`.
- Model catalog responses are cached ~5 minutes; **🔄 Refresh Pixio models** pulls the live list your account can see.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Widgets/sockets don't change when switching models | Hard-refresh the browser tab (the extension loads with the page). On a managed machine, make sure it was rebuilt on the latest commit — the startup log must show `[Pixio] Pixio Integration vX.Y.Z loaded`. |
| `No Pixio API key found` | Set `PIXIO_API_KEY` on the machine, paste a key into the widget, or wire a Pixio API Key node. |
| Console says `this model does not take: … (ignored)` | Informational — leftover parameters from a previously selected model were safely dropped. |
| A download briefly fails after generation | The node automatically re-fetches a freshly signed URL and retries; if it still fails after retries, check the `media_url` output in your browser. |
| Timeout on long video jobs | Raise `timeout_minutes` (some video models take several minutes). |

## Requirements

- ComfyUI (a recent version with the native VIDEO type; older versions still work via `file_path`/`media_url`)
- A Pixio account and API key — models, pricing, and docs at [beta.pixio.myapps.ai](https://beta.pixio.myapps.ai)
- Python deps in `requirements.txt` (installed automatically on managed machines)

## Repository

`https://github.com/rossman22590/pixio-integration-node`
