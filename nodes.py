"""ComfyUI nodes for the Pixio API (https://beta.pixio.myapps.ai).

PixioGeneration is a universal supernode: pick any model from the Pixio
catalog and both its widgets and its media input sockets transform to match
that model's input schema (via the bundled web extension). Connected
IMAGE/VIDEO/AUDIO inputs are uploaded automatically and mapped onto the
model's file parameters in schema order.
"""

import io
import json
import os
import threading
import time
from urllib.parse import urlparse

import numpy as np
import torch
from PIL import Image

from .pixio_api import PixioClient, PixioError, extract_output_urls, resolve_api_key

try:
    import folder_paths
except ImportError:
    folder_paths = None

AUDIO_HINTS = ("audio", "voice", "music", "sound", "speech", "song", "vocal")
VIDEO_HINTS = ("video", "clip", "movie", "footage")


# --------------------------------------------------------------------------
# model catalog — powers the native model dropdown
#
# Priority: in-memory (live-fetched) > models_cache_local.json (written after
# every successful live fetch, gitignored) > models_cache.json (snapshot
# bundled with the repo). A background refresh runs at import time when an
# API key is resolvable from the env var or pixio_config.json.
# --------------------------------------------------------------------------

_NODE_DIR = os.path.dirname(__file__)
_BUNDLED_CATALOG = os.path.join(_NODE_DIR, "models_cache.json")
_LOCAL_CATALOG = os.path.join(_NODE_DIR, "models_cache_local.json")
_CATALOG_KEYS = ("id", "providerId", "name", "type", "credits", "company", "inputs")

_catalog_lock = threading.Lock()
_catalog = None


def _read_catalog_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            models = json.load(f).get("models") or []
        return models or None
    except (OSError, ValueError):
        return None


def get_catalog():
    global _catalog
    with _catalog_lock:
        if not _catalog:
            _catalog = _read_catalog_file(_LOCAL_CATALOG) or \
                _read_catalog_file(_BUNDLED_CATALOG) or []
        return _catalog


def set_catalog(models):
    global _catalog
    if not models:
        return
    slim = [{k: m.get(k) for k in _CATALOG_KEYS if m.get(k) is not None} for m in models]
    slim.sort(key=lambda m: m.get("id") or "")
    with _catalog_lock:
        _catalog = slim
    try:
        with open(_LOCAL_CATALOG, "w", encoding="utf-8") as f:
            json.dump({"models": slim}, f, ensure_ascii=False, separators=(",", ":"))
    except OSError:
        pass


def get_model_ids():
    ids = [m["id"] for m in get_catalog() if m.get("id")]
    return ids or ["pixio/flux-1/schnell"]


def _refresh_catalog_async():
    key = resolve_api_key("")
    if not key:
        return

    def run():
        try:
            set_catalog(PixioClient(key).list_models())
            print(f"[Pixio] model catalog refreshed ({len(get_catalog())} models)")
        except Exception as e:
            print(f"[Pixio] catalog refresh failed (using cached list): {e}")

    threading.Thread(target=run, daemon=True, name="pixio-catalog").start()


_refresh_catalog_async()


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _blank_image():
    return torch.zeros(1, 64, 64, 3, dtype=torch.float32)


def _silent_audio():
    return {"waveform": torch.zeros(1, 1, 1, dtype=torch.float32), "sample_rate": 44100}


def _output_dir():
    if folder_paths is not None:
        base = folder_paths.get_output_directory()
    else:
        base = os.path.join(os.path.dirname(__file__), "outputs")
    path = os.path.join(base, "pixio")
    os.makedirs(path, exist_ok=True)
    return path


def _image_to_png_bytes(image, index=0):
    frame = image[min(index, image.shape[0] - 1)]
    arr = (frame.cpu().numpy() * 255.0).clip(0, 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    return buf.getvalue()


def _audio_to_wav_bytes(audio):
    import torchaudio
    waveform = audio["waveform"]
    if waveform.dim() == 3:
        waveform = waveform[0]
    buf = io.BytesIO()
    torchaudio.save(buf, waveform.cpu(), audio["sample_rate"], format="wav")
    return buf.getvalue()


def _bytes_to_image_tensor(data):
    img = Image.open(io.BytesIO(data)).convert("RGB")
    arr = np.asarray(img).astype(np.float32) / 255.0
    return torch.from_numpy(arr).unsqueeze(0)


def _bytes_to_audio(data, ext):
    buf = io.BytesIO(data)
    fmt = ext.lstrip(".").lower() or None
    try:
        import torchaudio
        try:
            waveform, sr = torchaudio.load(buf, format=fmt)
        except Exception:
            buf.seek(0)
            waveform, sr = torchaudio.load(buf)
        return {"waveform": waveform.unsqueeze(0), "sample_rate": sr}
    except Exception:
        pass
    try:
        import soundfile as sf
        buf.seek(0)
        arr, sr = sf.read(buf, dtype="float32", always_2d=True)
        return {"waveform": torch.from_numpy(arr.T).unsqueeze(0), "sample_rate": sr}
    except Exception as e:
        print(f"[Pixio] could not decode audio output ({e}); returning silence. "
              f"The file is still saved and the URL output is valid.")
        return None


def _video_from_file(path):
    """Wrap a saved video in ComfyUI's native VIDEO type (None if unsupported)."""
    try:
        from comfy_api.input_impl import VideoFromFile
    except ImportError:
        try:
            from comfy_api.input_impl.video_types import VideoFromFile
        except ImportError:
            print("[Pixio] this ComfyUI version has no native VIDEO type — "
                  "use the file_path/media_url outputs instead")
            return None
    return VideoFromFile(path)


def _first_video_frame(path):
    """Decode the first frame of a video as an IMAGE tensor thumbnail."""
    try:
        import av
        with av.open(path) as container:
            for frame in container.decode(video=0):
                arr = np.asarray(frame.to_image().convert("RGB")).astype(np.float32) / 255.0
                return torch.from_numpy(arr).unsqueeze(0)
    except Exception as e:
        print(f"[Pixio] could not extract video thumbnail: {e}")
    return None


def _file_kind(inp):
    text = f"{inp.get('name', '')} {inp.get('label', '')}".lower()
    if any(h in text for h in AUDIO_HINTS):
        return "audio"
    if any(h in text for h in VIDEO_HINTS):
        return "video"
    return "image"


def _ext_for(url, media_type):
    ext = os.path.splitext(urlparse(url).path)[1]
    if ext and len(ext) <= 6:
        return ext
    return {"image": ".png", "video": ".mp4", "audio": ".mp3"}.get(media_type, ".bin")


def _coerce(value, inp):
    kind = inp.get("type")
    if kind == "number":
        if isinstance(value, str):
            value = value.strip()
            if value == "":
                return None
            value = float(value)
        value = float(value)
        if value.is_integer():
            default = inp.get("defaultValue")
            keep_float = isinstance(default, float) and not float(default).is_integer()
            if not keep_float:
                return int(value)
        return value
    if kind == "boolean":
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    return value


def _video_to_mp4_bytes(video):
    """Extract raw bytes from a ComfyUI VIDEO input (or a plain file path)."""
    if isinstance(video, str) and os.path.isfile(video):
        with open(video, "rb") as f:
            return f.read()
    if hasattr(video, "save_to"):  # comfy_api VideoInput / VideoFromFile
        import tempfile
        path = os.path.join(tempfile.gettempdir(),
                            f"pixio_upload_{os.getpid()}_{id(video)}.mp4")
        try:
            video.save_to(path)
            with open(path, "rb") as f:
                return f.read()
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
    raise PixioError("Unsupported VIDEO input object — pass a URL for the video "
                     "parameter in model_params instead.")


def _prepare_params(client, model_def, prompt, raw_params, images, videos, audio, seed):
    inputs = model_def.get("inputs") or []
    schema = {i.get("name"): i for i in inputs}
    params = dict(raw_params)

    prompt = (prompt or "").strip()
    if prompt and ("prompt" in schema or not inputs):
        params["prompt"] = prompt

    if "seed" in schema and "seed" not in params and seed > 0:
        # Providers commonly validate seed as int32 — fold the 64-bit widget
        # value into that range so randomized seeds never fail validation.
        params["seed"] = seed % 2147483648

    # Map connected media / local paths onto the model's file inputs.
    image_queue = [img for img in images if img is not None]
    video_queue = [vid for vid in videos if vid is not None]
    audio_available = audio is not None
    for inp in inputs:
        if inp.get("type") != "file":
            continue
        name = inp.get("name")
        value = params.get(name)
        if isinstance(value, str) and value.strip():
            value = value.strip()
            if os.path.isfile(value):
                with open(value, "rb") as f:
                    params[name] = client.upload_bytes(
                        f.read(), os.path.basename(value), "application/octet-stream")
                print(f"[Pixio] uploaded local file for '{name}'")
            else:
                params[name] = value
            continue
        kind = _file_kind(inp)
        if kind == "audio" and audio_available:
            params[name] = client.upload_bytes(_audio_to_wav_bytes(audio), "input.wav", "audio/wav")
            audio_available = False
            print(f"[Pixio] uploaded connected audio for '{name}'")
        elif kind == "video" and video_queue:
            vid = video_queue.pop(0)
            params[name] = client.upload_bytes(_video_to_mp4_bytes(vid), "input.mp4", "video/mp4")
            print(f"[Pixio] uploaded connected video for '{name}'")
        elif kind == "image" and image_queue:
            img = image_queue.pop(0)
            params[name] = client.upload_bytes(_image_to_png_bytes(img), "input.png", "image/png")
            print(f"[Pixio] uploaded connected image for '{name}'")
        elif inp.get("required"):
            raise PixioError(
                f"Model input '{name}' ({kind}) is required. Connect an {kind} input to the "
                f"node or set a URL for it in the model widgets / model_params.")
        else:
            params.pop(name, None)

    cleaned = {}
    dropped = []
    for name, value in params.items():
        inp = schema.get(name)
        if inp is None:
            # When the model's schema is known, never send parameters it
            # doesn't accept — stale model_params from a previously selected
            # model would otherwise fail API validation.
            if inputs:
                dropped.append(name)
                continue
        else:
            if inp.get("type") == "select":
                allowed = [o.get("value") if isinstance(o, dict) else o
                           for o in inp.get("options") or []]
                if allowed and value not in allowed:
                    fallback = inp.get("defaultValue")
                    print(f"[Pixio] '{name}' = {value!r} not supported by this model; "
                          f"using {fallback!r} (allowed: {', '.join(map(str, allowed))})")
                    value = fallback
            value = _coerce(value, inp)
        if value is None or (isinstance(value, str) and value == ""):
            continue
        cleaned[name] = value
    if dropped:
        print(f"[Pixio] this model does not take: {', '.join(dropped)} (ignored)")
    return cleaned


# --------------------------------------------------------------------------
# nodes
# --------------------------------------------------------------------------

class PixioGeneration:
    """Universal Pixio generation node — any model, any modality."""

    CATEGORY = "Pixio"
    FUNCTION = "generate"
    # VIDEO is appended last so workflows saved before it existed keep their
    # slot indices for audio/media_url/file_path connections.
    RETURN_TYPES = ("IMAGE", "AUDIO", "STRING", "STRING", "VIDEO")
    RETURN_NAMES = ("image", "audio", "media_url", "file_path", "video")
    OUTPUT_NODE = True
    DESCRIPTION = ("Run any Pixio model. Click 'Load Pixio models' to turn the model field into "
                   "a searchable dropdown; the model's own parameters appear as widgets below.")

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "api_key": ("STRING", {
                    "default": os.environ.get("PIXIO_API_KEY", ""),
                    "tooltip": "Pixio API key (pxio_live_...). Leave empty to use the "
                               "PIXIO_API_KEY env var or pixio_config.json."}),
                "model": (get_model_ids(), {
                    "default": "pixio/flux-1/schnell",
                    "tooltip": "Pixio model to run. The list refreshes from your account "
                               "when an API key is available."}),
                "prompt": ("STRING", {"default": "", "multiline": True,
                                      "tooltip": "Used as the model's 'prompt' parameter."}),
                "model_params": ("STRING", {
                    "default": "{}", "multiline": True,
                    "tooltip": "JSON parameters for the selected model. Auto-filled by the "
                               "dynamic widgets — edit by hand only if you know the schema."}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xFFFFFFFFFFFFFFFF,
                                 "control_after_generate": True,
                                 "tooltip": "Sent to the model only if it has a 'seed' parameter; "
                                            "otherwise just forces a re-run when changed."}),
                "timeout_minutes": ("INT", {"default": 15, "min": 1, "max": 120}),
            },
            "optional": {
                # Socket pool: the web extension shows only the sockets the
                # selected model actually uses (relabeled to its real param
                # names). Python maps connected media onto the model's file
                # params in schema order.
                "image_1": ("IMAGE", {"tooltip": "Uploaded to Pixio and mapped to the model's "
                                                 "first image input."}),
                "image_2": ("IMAGE", {"tooltip": "Mapped to the model's second image input."}),
                "image_3": ("IMAGE", {"tooltip": "Mapped to the model's third image input."}),
                "image_4": ("IMAGE", {"tooltip": "Mapped to the model's fourth image input."}),
                "video_1": ("VIDEO", {"tooltip": "Uploaded and mapped to the model's first "
                                                 "video input."}),
                "video_2": ("VIDEO", {"tooltip": "Mapped to the model's second video input."}),
                "audio": ("AUDIO", {"tooltip": "Uploaded and mapped to the model's audio input."}),
                "pixio_key": ("STRING", {"forceInput": True,
                                         "tooltip": "Optional key from a PixioApiKey node; "
                                                    "overrides the api_key widget."}),
            },
        }

    @classmethod
    def VALIDATE_INPUTS(cls, **kwargs):
        # The live catalog can be newer than the bundled snapshot the dropdown
        # was built from — accept any model id and let the API be the judge.
        return True

    def generate(self, api_key, model, prompt, model_params, seed, timeout_minutes,
                 image_1=None, image_2=None, image_3=None, image_4=None,
                 video_1=None, video_2=None, audio=None, pixio_key=None):
        client = PixioClient((pixio_key or "").strip() or api_key)

        try:
            model_def = client.get_model(model)
        except PixioError as e:
            print(f"[Pixio] warning: could not fetch schema for '{model}': {e}")
            model_def = {"id": model, "inputs": [], "providerId": "pixio"}

        try:
            raw_params = json.loads(model_params) if model_params.strip() else {}
            if not isinstance(raw_params, dict):
                raise ValueError("must be a JSON object")
        except ValueError as e:
            raise PixioError(f"model_params is not valid JSON: {e}")

        params = _prepare_params(client, model_def, prompt, raw_params,
                                 [image_1, image_2, image_3, image_4],
                                 [video_1, video_2], audio, seed)
        return _execute_generation(client, model_def, model, params, timeout_minutes)


def _download_output(client, content_id, url):
    """Download an output, refreshing the signed URL from the API on failure.

    Right after a generation succeeds, some providers' records briefly carry an
    unsigned storage URL (403). Re-fetching the generation makes the API mint a
    fresh presigned outputUrl, so retry through that before giving up.
    """
    try:
        return client.download(url), url
    except PixioError as first_err:
        print(f"[Pixio] download failed, refreshing output URL: {first_err}")
        for delay in (2.0, 5.0):
            time.sleep(delay)
            try:
                fresh_urls = extract_output_urls(client.get_generation(content_id))
            except PixioError:
                continue
            for fresh in fresh_urls:
                try:
                    return client.download(fresh), fresh
                except PixioError:
                    continue
        raise first_err


def _execute_generation(client, model_def, model, params, timeout_minutes):
    """Start a generation, poll to completion, download and convert the outputs."""
    shown = {k: (v[:80] + "…" if isinstance(v, str) and len(v) > 80 else v)
             for k, v in params.items()}
    print(f"[Pixio] generating with {model}: {json.dumps(shown)}")
    content_id = client.generate(model, params, model_def.get("providerId") or "pixio")
    print(f"[Pixio] generation started: {content_id}")

    last_status = [None]

    def on_poll(status, _gen):
        if status != last_status[0]:
            print(f"[Pixio] {content_id}: {status}")
            last_status[0] = status

    gen = client.wait_for_generation(content_id, poll_interval=3.0,
                                     timeout=timeout_minutes * 60, on_poll=on_poll)

    urls = extract_output_urls(gen)
    if not urls:
        raise PixioError(f"Generation succeeded but no output URL was found: "
                         f"{json.dumps(gen)[:400]}")

    media_type = (gen.get("type") or "").lower()
    if not media_type:
        model_type = model_def.get("type") or ""
        media_type = model_type.split("-")[-1] if model_type else "file"

    out_dir = _output_dir()
    image_out = _blank_image()
    audio_out = _silent_audio()
    video_out = None
    ui = {}
    ui_images = []
    saved_paths = []
    primary_url = urls[0]

    if media_type == "image":
        tensors = []
        for i, url in enumerate(urls):
            data, url = _download_output(client, content_id, url)
            try:
                tensor = _bytes_to_image_tensor(data)
            except Exception:
                continue
            fname = f"pixio_{content_id[:8]}_{i}{_ext_for(url, 'image')}"
            fpath = os.path.join(out_dir, fname)
            with open(fpath, "wb") as f:
                f.write(data)
            saved_paths.append(fpath)
            ui_images.append({"filename": fname, "subfolder": "pixio", "type": "output"})
            tensors.append(tensor)
        if tensors:
            base_shape = tensors[0].shape[1:]
            image_out = torch.cat([t for t in tensors if t.shape[1:] == base_shape], dim=0)
    elif media_type == "audio":
        data, primary_url = _download_output(client, content_id, primary_url)
        ext = _ext_for(primary_url, "audio")
        fname = f"pixio_{content_id[:8]}{ext}"
        fpath = os.path.join(out_dir, fname)
        with open(fpath, "wb") as f:
            f.write(data)
        saved_paths.append(fpath)
        decoded = _bytes_to_audio(data, ext)
        if decoded:
            audio_out = decoded
        ui["audio"] = [{"filename": fname, "subfolder": "pixio", "type": "output"}]
    elif media_type == "video":
        data, primary_url = _download_output(client, content_id, primary_url)
        fname = f"pixio_{content_id[:8]}{_ext_for(primary_url, 'video')}"
        fpath = os.path.join(out_dir, fname)
        with open(fpath, "wb") as f:
            f.write(data)
        saved_paths.append(fpath)
        video_out = _video_from_file(fpath)
        thumb = _first_video_frame(fpath)
        if thumb is not None:
            image_out = thumb
        ui["images"] = [{"filename": fname, "subfolder": "pixio", "type": "output"}]
        ui["animated"] = (True,)
    else:
        # 3d models, svg, anything else — save the file and hand back the path/URL
        data, primary_url = _download_output(client, content_id, primary_url)
        fpath = os.path.join(out_dir, f"pixio_{content_id[:8]}{_ext_for(primary_url, media_type)}")
        with open(fpath, "wb") as f:
            f.write(data)
        saved_paths.append(fpath)

    file_path = saved_paths[0] if saved_paths else ""
    cost = gen.get("creditsCost")
    print(f"[Pixio] done ({media_type})"
          + (f" — cost {cost} credits" if cost is not None else "")
          + (f" — saved to {file_path}" if file_path else ""))

    result = {"result": (image_out, audio_out, primary_url, file_path, video_out)}
    if ui_images:
        ui["images"] = ui_images
    if ui:
        result["ui"] = ui
    return result


class PixioApiKey:
    """Holds a Pixio API key so it can be wired to multiple nodes."""

    CATEGORY = "Pixio"
    FUNCTION = "get_key"
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("pixio_key",)

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"api_key": ("STRING", {
            "default": "",
            "tooltip": "Leave empty to use the PIXIO_API_KEY env var or pixio_config.json."})}}

    def get_key(self, api_key):
        key = resolve_api_key(api_key)
        if not key:
            raise PixioError("No Pixio API key found (widget, PIXIO_API_KEY env var, "
                             "or pixio_config.json).")
        return (key,)


def _render_text_card(lines, width=512):
    """Render text lines to an IMAGE tensor so plain ComfyUI can display them."""
    from PIL import ImageDraw, ImageFont
    try:
        font_big = ImageFont.load_default(size=34)
        font_small = ImageFont.load_default(size=22)
    except TypeError:  # Pillow < 10.1 has no size argument
        font_big = font_small = ImageFont.load_default()
    pad, gap = 24, 12
    heights = [(font_big if i == 0 else font_small).getbbox(line)[3] + gap
               for i, line in enumerate(lines)]
    img = Image.new("RGB", (width, pad * 2 + sum(heights)), (24, 26, 32))
    draw = ImageDraw.Draw(img)
    y = pad
    for i, line in enumerate(lines):
        font = font_big if i == 0 else font_small
        draw.text((pad, y), line, fill=(240, 240, 245) if i == 0 else (170, 200, 255),
                  font=font)
        y += heights[i]
    arr = np.asarray(img).astype(np.float32) / 255.0
    return torch.from_numpy(arr).unsqueeze(0)


class PixioCredits:
    """Check remaining Pixio credits."""

    CATEGORY = "Pixio"
    FUNCTION = "check"
    RETURN_TYPES = ("STRING", "INT", "IMAGE")
    RETURN_NAMES = ("summary", "total_credits", "image")
    OUTPUT_NODE = True
    DESCRIPTION = ("Fetches your Pixio credit balance. Connect the image output to a "
                   "Preview Image node to see it on the canvas.")

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {"api_key": ("STRING", {"default": os.environ.get("PIXIO_API_KEY", "")})},
            "optional": {"pixio_key": ("STRING", {"forceInput": True})},
        }

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")  # always re-check

    def check(self, api_key, pixio_key=None):
        client = PixioClient((pixio_key or "").strip() or api_key)
        credits = client.credits()
        recurring = credits.get("recurring") or {}
        total = int(credits.get("total", 0))
        summary = (f"Pixio credits — total: {total} "
                   f"(recurring {recurring.get('current', '?')}/{recurring.get('quota', '?')}, "
                   f"permanent {credits.get('permanent', '?')})")
        print(f"[Pixio] {summary}")
        card = _render_text_card([
            f"Pixio credits: {total:,}",
            f"recurring: {recurring.get('current', '?')} / {recurring.get('quota', '?')}",
            f"permanent: {credits.get('permanent', '?')}",
        ])
        return (summary, total, card)


class PixioUploadMedia:
    """Upload an image, audio clip, or local file to Pixio and get back a URL."""

    CATEGORY = "Pixio"
    FUNCTION = "upload"
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("url",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {"api_key": ("STRING", {"default": os.environ.get("PIXIO_API_KEY", "")})},
            "optional": {
                "image": ("IMAGE",),
                "audio": ("AUDIO",),
                "file_path": ("STRING", {"default": "", "tooltip": "Local file path to upload."}),
                "pixio_key": ("STRING", {"forceInput": True}),
            },
        }

    def upload(self, api_key, image=None, audio=None, file_path="", pixio_key=None):
        client = PixioClient((pixio_key or "").strip() or api_key)
        if image is not None:
            url = client.upload_bytes(_image_to_png_bytes(image), "upload.png", "image/png")
        elif audio is not None:
            url = client.upload_bytes(_audio_to_wav_bytes(audio), "upload.wav", "audio/wav")
        elif file_path.strip() and os.path.isfile(file_path.strip()):
            path = file_path.strip()
            with open(path, "rb") as f:
                url = client.upload_bytes(f.read(), os.path.basename(path),
                                          "application/octet-stream")
        else:
            raise PixioError("Nothing to upload — connect an image or audio, or set file_path.")
        print(f"[Pixio] uploaded: {url[:100]}…")
        return (url,)


NODE_CLASS_MAPPINGS = {
    "PixioGeneration": PixioGeneration,
    "PixioApiKey": PixioApiKey,
    "PixioCredits": PixioCredits,
    "PixioUploadMedia": PixioUploadMedia,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "PixioGeneration": "Pixio Generation 🎛️ (any model)",
    "PixioApiKey": "Pixio API Key",
    "PixioCredits": "Pixio Credits",
    "PixioUploadMedia": "Pixio Upload Media",
}
