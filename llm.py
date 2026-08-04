"""Thin Ollama client shared by trend_summary.py and swim_advisor.py.

Which model answers which prompt is configuration, not code - set WG_TREND_MODEL
and WG_ADVISOR_MODEL in .env to swap in gemma3, qwen, or anything else you have
pulled. Host is configurable too, so Ollama can live on another box on the LAN
(handy if the dashboard runs on a Linux server but the GPU/Mac is elsewhere).
"""
import json
import os
import time

import requests

DEFAULT_HOST = "http://localhost:11434"
TREND_MODEL_DEFAULT = "llama3.2:3b"
ADVISOR_MODEL_DEFAULT = "qwen2.5:32b"


def host() -> str:
    return os.environ.get("OLLAMA_HOST", DEFAULT_HOST).rstrip("/")


def trend_model() -> str:
    return os.environ.get("WG_TREND_MODEL", TREND_MODEL_DEFAULT)


def advisor_model() -> str:
    return os.environ.get("WG_ADVISOR_MODEL", ADVISOR_MODEL_DEFAULT)


def timeout_s(default: int) -> int:
    try:
        return int(os.environ.get("WG_LLM_TIMEOUT", default))
    except ValueError:
        return default


class LLMResult:
    """Generated text plus the metadata worth surfacing on the dashboard."""

    def __init__(self, text: str, model: str, elapsed_s: float, eval_count: int | None = None):
        self.text = text
        self.model = model
        self.elapsed_s = elapsed_s
        self.eval_count = eval_count

    @property
    def tokens_per_s(self) -> float | None:
        if not self.eval_count or self.elapsed_s <= 0:
            return None
        return self.eval_count / self.elapsed_s


def generate(prompt: str, model: str, fmt=None, timeout: int = 60, options: dict | None = None) -> LLMResult | None:
    """One-shot completion. Returns None on any transport/HTTP error.

    `fmt` may be "json" or a JSON Schema dict. Schemas are enforced by Ollama's
    constrained decoding (Ollama >= 0.5), which is what makes small models
    reliable at the swim advisor's output contract instead of merely asked
    nicely. If the server is too old to understand a schema, we retry with plain
    "json" rather than failing the run.
    """
    payload = {"model": model, "prompt": prompt, "stream": False}
    if fmt is not None:
        payload["format"] = fmt
    if options:
        payload["options"] = options

    started = time.monotonic()
    try:
        resp = requests.post(f"{host()}/api/generate", json=payload, timeout=timeout)
        if resp.status_code == 400 and isinstance(fmt, dict):
            payload["format"] = "json"
            resp = requests.post(f"{host()}/api/generate", json=payload, timeout=timeout)
        resp.raise_for_status()
        body = resp.json()
    except (requests.RequestException, json.JSONDecodeError):
        return None

    return LLMResult(
        text=(body.get("response") or "").strip(),
        model=body.get("model", model),
        elapsed_s=time.monotonic() - started,
        eval_count=body.get("eval_count"),
    )


def installed_models() -> list[str]:
    """Tags currently pulled, for bench_models.py and setup checks."""
    try:
        resp = requests.get(f"{host()}/api/tags", timeout=10)
        resp.raise_for_status()
        return sorted(m["name"] for m in resp.json().get("models", []))
    except (requests.RequestException, json.JSONDecodeError, KeyError):
        return []
