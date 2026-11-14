"""Model config — resolve model credentials for Hermes bridge.

Standalone: reads from environment variables / config file. The original
implementation pulled credentials from a platform model-registry API; that
coupling is replaced by two env vars (or an optional providers.json file):

  CUA_MODEL_KEY      provider_id::model_id     (e.g. "dashscope::qwen-plus")
  CUA_MODEL_API_KEY  the API key
  CUA_MODEL_BASE_URL OpenAI-compatible base URL (default https://api.openai.com/v1)

A optional ``providers.json`` next to this file (or pointed to by
CUA_PROVIDERS_FILE) may hold several providers:

  {"dashscope": {"name": "DashScope", "apiUrl": "https://.../compatible-mode/v1",
                 "apiKey": "sk-..."},
   "deepseek":  {...}}

Keys:
  CUA_MODEL_KEY format: "<provider_id>::<model_id>"; if no "::" the whole
  value is treated as the model id and CUA_MODEL_* env vars apply directly.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger("cua.model_config")

_MODEL_CONFIG_CACHE: dict[str, tuple[float, dict]] = {}
_MODEL_CONFIG_TTL = 300  # seconds


def _parse_model_key(model_key: str) -> tuple[Optional[str], str]:
    """Split "providerId::modelId"; return (provider_id or None, model_id)."""
    if "::" in model_key:
        parts = model_key.split("::", 1)
        return parts[0].strip(), parts[1].strip()
    return None, model_key


def _load_providers_file() -> Dict[str, dict]:
    path = os.environ.get("CUA_PROVIDERS_FILE", str(Path(__file__).parent / "providers.json"))
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("providers.json parse failed: %s", exc)
        return {}


def _clean_base_url(url: str) -> str:
    url = (url or "").rstrip("/")
    if url.endswith("/chat/completions"):
        url = url[: -len("/chat/completions")]
    return url


async def resolve_model_config(model_key: str) -> dict:
    """Resolve (model, api_key, api_base) for a model key.

    Priority: providers.json > env vars (CUA_MODEL_*) > empty dict.
    Cached for _MODEL_CONFIG_TTL per provider.
    """
    if not model_key:
        return {}

    provider_id, model_id = _parse_model_key(model_key)
    if not provider_id:
        # No provider prefix: use direct env configuration
        return {
            "model": model_key,
            "api_key": os.environ.get("CUA_MODEL_API_KEY", ""),
            "api_base": _clean_base_url(os.environ.get("CUA_MODEL_BASE_URL", "https://api.openai.com/v1")),
            "provider_type": "CUSTOM",
        }

    now = time.time()
    cached = _MODEL_CONFIG_CACHE.get(provider_id)
    if cached and (now - cached[0]) < _MODEL_CONFIG_TTL:
        provider = cached[1]
    else:
        provider = _load_providers_file().get(provider_id) or {}
        if provider:
            _MODEL_CONFIG_CACHE[provider_id] = (now, provider)

    api_url = _clean_base_url(provider.get("apiUrl", ""))
    api_key = provider.get("apiKey", "")
    if not api_key:
        # fall back to a per-provider env var, e.g. CUA_PROVIDER_DASHSCOPE_API_KEY
        env_key = f"CUA_PROVIDER_{provider_id.upper().replace('-', '_')}_API_KEY"
        api_key = os.environ.get(env_key, "")

    result = {
        "model": model_id,
        "api_key": api_key,
        "api_base": api_url or "https://api.openai.com/v1",
        "provider_type": provider.get("type", "CUSTOM"),
    }
    if not result["api_key"]:
        logger.warning("No API key resolved for provider %s", provider_id)
    return result


def write_hermes_config(model_config: Optional[dict] = None) -> str:
    """Write ~/.hermes/config.yaml model block (+ CDP url), preserving other
    existing Hermes settings. Returns the Hermes home directory path.

    Reuses the production implementation, minus the platform MCP-server block
    (a standalone deployment configures MCP servers via hermes CLI instead).
    """
    import yaml  # local import: only needed when writing config

    home = os.path.expanduser("~")
    hermes_dir = os.path.join(home, ".hermes")
    os.makedirs(hermes_dir, exist_ok=True)
    config_path = os.path.join(hermes_dir, "config.yaml")

    existing: dict = {}
    if os.path.isfile(config_path):
        try:
            existing = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
            if not isinstance(existing, dict):
                existing = {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("Existing hermes config unreadable, will overwrite model block: %s", exc)

    if model_config and model_config.get("api_key"):
        existing["model"] = {
            "provider": "custom",
            "model": model_config.get("model", "gpt-4o-mini"),
            "base_url": _clean_base_url(model_config.get("api_base", "https://api.openai.com/v1")),
            "api_key": model_config.get("api_key", ""),
        }

    existing.setdefault("max_turns", 90)
    cdp_url = os.environ.get("BROWSER_CDP_URL", "http://127.0.0.1:9222")
    existing.setdefault("browser", {"cdp_url": cdp_url, "headless": False})

    Path(config_path).write_text(
        yaml.safe_dump(existing, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    logger.info("Hermes config written: %s", config_path)
    return hermes_dir
