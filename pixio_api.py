"""Pixio API client used by the ComfyUI nodes.

Docs: https://beta.pixio.myapps.ai/api/v1/guide
"""

import io
import json
import os
import time

import requests

DEFAULT_BASE_URL = "https://beta.pixio.myapps.ai"

TERMINAL_STATUSES = {"succeeded", "failed", "canceled", "cancelled"}


class PixioError(Exception):
    """Raised when the Pixio API returns an error response."""


def resolve_api_key(api_key: str = "") -> str:
    key = (api_key or "").strip()
    if not key:
        key = os.environ.get("PIXIO_API_KEY", "").strip()
    if not key:
        config_path = os.path.join(os.path.dirname(__file__), "pixio_config.json")
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    key = (json.load(f).get("api_key") or "").strip()
            except (OSError, ValueError):
                pass
    return key


class PixioClient:
    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE_URL, timeout: int = 120):
        self.api_key = resolve_api_key(api_key)
        if not self.api_key:
            raise PixioError(
                "No Pixio API key provided. Set it on the node, in the PIXIO_API_KEY "
                "environment variable, or in pixio_config.json next to this node."
            )
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {self.api_key}"

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self.base_url}{path}"
        kwargs.setdefault("timeout", self.timeout)
        resp = self.session.request(method, url, **kwargs)
        if resp.status_code >= 400:
            detail = ""
            try:
                body = resp.json()
                detail = body.get("error") or body.get("message") or json.dumps(body)
            except ValueError:
                detail = resp.text[:500]
            raise PixioError(f"Pixio API {method} {path} failed ({resp.status_code}): {detail}")
        try:
            return resp.json()
        except ValueError:
            raise PixioError(f"Pixio API {method} {path} returned non-JSON response")

    # ----- catalog -----

    def list_models(self) -> list:
        return self._request("GET", "/api/v1/models").get("models", [])

    def get_model(self, model_id: str) -> dict:
        """Return the model record with its input schema under 'inputs'."""
        try:
            data = self._request("GET", f"/api/v1/models/{requests.utils.quote(model_id, safe='')}")
            model = data.get("model") or data
            if model.get("id"):
                # The detail endpoint returns the schema as a sibling 'params' array.
                if not model.get("inputs"):
                    model["inputs"] = data.get("params") or []
                return model
        except PixioError:
            pass
        for model in self.list_models():
            if model.get("id") == model_id:
                return model
        raise PixioError(f"Model '{model_id}' not found in the Pixio catalog")

    # ----- account -----

    def credits(self) -> dict:
        return self._request("GET", "/api/v1/credits")

    # ----- uploads -----

    def upload_bytes(self, data: bytes, filename: str, content_type: str) -> str:
        """Upload a file and return a URL usable in generation params."""
        result = self._request(
            "POST",
            "/api/v1/uploads",
            files={"file": (filename, io.BytesIO(data), content_type)},
        )
        uploads = result.get("uploads") or []
        if uploads and uploads[0].get("url"):
            return uploads[0]["url"]
        if result.get("url"):
            return result["url"]
        raise PixioError(f"Upload succeeded but no URL in response: {json.dumps(result)[:300]}")

    # ----- generation -----

    def generate(self, model_id: str, params: dict, provider_id: str = "pixio") -> str:
        body = {"providerId": provider_id, "modelId": model_id, "params": params}
        result = self._request("POST", "/api/v1/generate", json=body)
        content_id = result.get("contentId") or result.get("id")
        if not content_id:
            raise PixioError(f"Generate response missing contentId: {json.dumps(result)[:300]}")
        return content_id

    def get_generation(self, content_id: str) -> dict:
        return self._request("GET", f"/api/v1/generations/{content_id}")

    def wait_for_generation(self, content_id: str, poll_interval: float = 3.0,
                            timeout: float = 900.0, on_poll=None) -> dict:
        start = time.time()
        while True:
            gen = self.get_generation(content_id)
            status = (gen.get("status") or "").lower()
            if on_poll:
                on_poll(status, gen)
            if status in TERMINAL_STATUSES:
                if status != "succeeded":
                    error = gen.get("error") or gen.get("message") or "no error detail"
                    raise PixioError(f"Generation {content_id} {status}: {error}")
                return gen
            if time.time() - start > timeout:
                raise PixioError(
                    f"Generation {content_id} timed out after {int(timeout)}s (status: {status})"
                )
            time.sleep(poll_interval)

    def download(self, url: str) -> bytes:
        resp = self.session.get(url, timeout=self.timeout) if url.startswith(self.base_url) \
            else requests.get(url, timeout=self.timeout)
        if resp.status_code >= 400:
            raise PixioError(f"Failed to download output ({resp.status_code}): {url}")
        return resp.content


def extract_output_urls(generation: dict) -> list:
    """Collect every output URL from a completed generation, primary first."""
    urls = []

    def add(url):
        if isinstance(url, str) and url.startswith("http") and url not in urls:
            urls.append(url)

    add(generation.get("outputUrl"))
    outputs = generation.get("outputs")
    if isinstance(outputs, dict):
        for value in outputs.values():
            if isinstance(value, str):
                add(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, str):
                        add(item)
                    elif isinstance(item, dict):
                        add(item.get("url"))
    elif isinstance(outputs, list):
        for item in outputs:
            if isinstance(item, str):
                add(item)
            elif isinstance(item, dict):
                add(item.get("url"))
    for key in ("output", "url", "resultUrl", "videoUrl", "imageUrl", "audioUrl", "modelUrl"):
        add(generation.get(key))
    return urls
