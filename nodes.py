"""ComfyUI nodes for the Pixio API (https://beta.pixio.myapps.ai).

PixioGeneration is a universal generation node: pick any model from the Pixio
catalog and its inputs appear as widgets (via the bundled web extension).
Connected IMAGE/AUDIO inputs are uploaded automatically and mapped onto the
model's file parameters.
"""

import io
import json
import os
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


def _prepare_params(client, model_def, prompt, raw_params, images, audio, seed):
    inputs = model_def.get("inputs") or []
    schema = {i.get("name"): i for i in inputs}
    params = dict(raw_params)

    prompt = (prompt or "").strip()
    if prompt and ("prompt" in schema or not inputs):
        params["prompt"] = prompt

    if "seed" in schema and "seed" not in params and seed > 0:
        params["seed"] = seed

    # Map connected media / local paths onto the model's file inputs.
    image_queue = [img for img in images if img is not None]
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
    for name, value in params.items():
        inp = schema.get(name)
        if inp is not None:
            value = _coerce(value, inp)
        if value is None or (isinstance(value, str) and value == ""):
            continue
        cleaned[name] = value
    return cleaned


# --------------------------------------------------------------------------
# nodes
# --------------------------------------------------------------------------

class PixioGeneration:
    """Universal Pixio generation node — any model, any modality."""

    CATEGORY = "Pixio"
    FUNCTION = "generate"
    RETURN_TYPES = ("IMAGE", "AUDIO", "STRING", "STRING")
    RETURN_NAMES = ("image", "audio", "media_url", "file_path")
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
                "model": ("STRING", {
                    "default": "pixio/flux-1/schnell",
                    "tooltip": "Pixio model id. Click 'Load Pixio models' to get a dropdown."}),
                "prompt": ("STRING", {"default": "", "multiline": True,
                                      "tooltip": "Used as the model's 'prompt' parameter."}),
                "model_params": ("STRING", {
                    "default": "{}", "multiline": True,
                    "tooltip": "JSON parameters for the selected model. Auto-filled by the "
                               "dynamic widgets — edit by hand only if you know the schema."}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0x7FFFFFFF,
                                 "control_after_generate": True,
                                 "tooltip": "Sent to the model only if it has a 'seed' parameter; "
                                            "otherwise just forces a re-run when changed."}),
                "timeout_minutes": ("INT", {"default": 15, "min": 1, "max": 120}),
            },
            "optional": {
                "image_1": ("IMAGE", {"tooltip": "Uploaded to Pixio and mapped to the model's "
                                                 "first image input."}),
                "image_2": ("IMAGE", {"tooltip": "Mapped to the model's second image input."}),
                "audio": ("AUDIO", {"tooltip": "Uploaded and mapped to the model's audio input."}),
                "pixio_key": ("STRING", {"forceInput": True,
                                         "tooltip": "Optional key from a PixioApiKey node; "
                                                    "overrides the api_key widget."}),
            },
        }

    def generate(self, api_key, model, prompt, model_params, seed, timeout_minutes,
                 image_1=None, image_2=None, audio=None, pixio_key=None):
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
                                 [image_1, image_2], audio, seed)

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
        ui_images = []
        saved_paths = []
        primary_url = urls[0]

        if media_type == "image":
            tensors = []
            for i, url in enumerate(urls):
                data = client.download(url)
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
            data = client.download(primary_url)
            ext = _ext_for(primary_url, "audio")
            fpath = os.path.join(out_dir, f"pixio_{content_id[:8]}{ext}")
            with open(fpath, "wb") as f:
                f.write(data)
            saved_paths.append(fpath)
            decoded = _bytes_to_audio(data, ext)
            if decoded:
                audio_out = decoded
        else:
            # video, 3d models, svg, anything else — save the file and hand back the path/URL
            data = client.download(primary_url)
            fpath = os.path.join(out_dir, f"pixio_{content_id[:8]}{_ext_for(primary_url, media_type)}")
            with open(fpath, "wb") as f:
                f.write(data)
            saved_paths.append(fpath)

        file_path = saved_paths[0] if saved_paths else ""
        cost = gen.get("creditsCost")
        print(f"[Pixio] done ({media_type})"
              + (f" — cost {cost} credits" if cost is not None else "")
              + (f" — saved to {file_path}" if file_path else ""))

        result = {"result": (image_out, audio_out, primary_url, file_path)}
        if ui_images:
            result["ui"] = {"images": ui_images}
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


class PixioCredits:
    """Check remaining Pixio credits."""

    CATEGORY = "Pixio"
    FUNCTION = "check"
    RETURN_TYPES = ("STRING", "INT")
    RETURN_NAMES = ("summary", "total_credits")
    OUTPUT_NODE = True

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
        return (summary, total)


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
