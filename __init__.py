"""Pixio Integration for ComfyUI — use any of the 550+ Pixio models from one node."""

__version__ = "1.3.0"

from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS, get_model_ids

WEB_DIRECTORY = "./web"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]

print(f"[Pixio] Pixio Integration v{__version__} loaded — "
      f"{len(get_model_ids())} models in the dropdown catalog")


# ---------------------------------------------------------------------------
# Frontend route: the web extension fetches the model catalog through the
# ComfyUI server so the API key never leaves the machine via the browser.
# ---------------------------------------------------------------------------
try:
    import asyncio
    import hashlib
    import time

    from aiohttp import web
    from server import PromptServer

    from .nodes import get_catalog, set_catalog
    from .pixio_api import PixioClient, resolve_api_key

    _CATALOG_CACHE = {}
    _CATALOG_TTL = 300  # seconds

    @PromptServer.instance.routes.post("/pixio/models")
    async def _pixio_models(request):
        try:
            data = await request.json()
        except Exception:
            data = {}
        key = resolve_api_key(data.get("api_key", ""))
        if not key:
            # No key yet — serve the cached/bundled catalog so the UI still works.
            return web.json_response({"models": get_catalog(), "cached": True})

        cache_key = hashlib.sha256(key.encode()).hexdigest()
        cached = _CATALOG_CACHE.get(cache_key)
        if cached and time.time() - cached[0] < _CATALOG_TTL:
            return web.json_response({"models": cached[1]})

        loop = asyncio.get_event_loop()
        try:
            models = await loop.run_in_executor(None, lambda: PixioClient(key).list_models())
        except Exception as e:
            fallback = get_catalog()
            if fallback:
                print(f"[Pixio] live catalog fetch failed, serving cached list: {e}")
                return web.json_response({"models": fallback, "cached": True})
            return web.json_response({"error": str(e)}, status=502)

        _CATALOG_CACHE[cache_key] = (time.time(), models)
        set_catalog(models)  # keep the dropdown list and local cache fresh
        return web.json_response({"models": models})

except Exception as e:  # running outside ComfyUI (tests, linting)
    print(f"[Pixio] server route not registered: {e}")

