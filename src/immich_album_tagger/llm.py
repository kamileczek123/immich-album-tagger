"""Minimal client for any OpenAI-compatible ``/chat/completions`` endpoint.

Works with llama.cpp ``llama-server``, vLLM, Ollama (``/v1``), LiteLLM/Bifrost gateways
and OpenAI itself. Only the parts needed for tag suggestions are implemented.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import LLMConfig

log = logging.getLogger(__name__)

# True in the prefetch workers: their requests wait while someone is waiting interactively.
background: ContextVar[bool] = ContextVar("llm_background", default=False)

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"```(?:json)?", re.IGNORECASE)


class LLMError(RuntimeError):
    """The LLM call failed or returned something unusable."""


class LLMClient:
    def __init__(self, config: LLMConfig, transport: httpx.BaseTransport | None = None):
        self.config = config
        headers = {"Content-Type": "application/json", **config.extra_headers}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        self._http = httpx.Client(
            base_url=config.base_url,
            headers=headers,
            timeout=config.timeout_seconds,
            verify=config.verify_tls,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def complete(self, system: str, user: str) -> str:
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            **self.config.extra_body,
        }
        if self.config.json_mode:
            body["response_format"] = {"type": "json_object"}
        try:
            response = self._http.post("/chat/completions", json=body)
        except httpx.HTTPError as exc:
            raise LLMError(f"cannot reach LLM at {self.config.base_url} ({exc})") from exc
        if response.status_code >= 400:
            raise LLMError(f"LLM returned HTTP {response.status_code}: {response.text[:300]}")
        try:
            return response.json()["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LLMError("LLM response has no choices[0].message.content") from exc

    def list_models(self) -> list[str]:
        try:
            response = self._http.get("/models")
            response.raise_for_status()
            return [item["id"] for item in response.json().get("data", [])]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise LLMError(f"cannot list models at {self.config.base_url} ({exc})") from exc


@dataclass(eq=False)
class _Endpoint:
    client: LLMClient
    limit: int
    busy: int = 0
    failed_at: float | None = None
    calls: int = field(default=0)

    @property
    def model(self) -> str:
        return self.client.config.model


class LLMPool:
    """The main LLM endpoint plus ``llm.endpoints``, used side by side.

    Each endpoint takes at most ``max_concurrent`` requests at once; a request goes to the
    next endpoint (round robin) with a free slot and waits when all are busy. Interactive
    requests go first: background requests (prefetch, see :data:`background`) wait while an
    interactive one is waiting. A request that fails is tried on the other endpoints; a
    failing endpoint is skipped for ``cooldown`` seconds while another one works.
    Same interface as :class:`LLMClient` (``complete``, ``list_models``, ``close``).
    """

    def __init__(
        self,
        config: LLMConfig,
        transport: httpx.BaseTransport | None = None,
        cooldown: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.config = config
        self.cooldown = cooldown
        self._clock = clock
        self.endpoints = [
            _Endpoint(LLMClient(cfg, transport), cfg.max_concurrent)
            for cfg in config.all_endpoints()
        ]
        self._cond = threading.Condition()
        self._next = 0
        self._interactive_waiting = 0

    @property
    def capacity(self) -> int:
        """Requests that can run at once over all endpoints."""
        return sum(e.limit for e in self.endpoints)

    @property
    def models(self) -> list[str]:
        return [e.model for e in self.endpoints]

    def complete(self, system: str, user: str) -> str:
        tried: set[int] = set()
        error: LLMError | None = None
        while len(tried) < len(self.endpoints):
            index = self._acquire(tried)
            endpoint = self.endpoints[index]
            try:
                reply = endpoint.client.complete(system, user)
            except LLMError as exc:
                error = exc
                tried.add(index)
                with self._cond:
                    endpoint.failed_at = self._clock()
                if len(self.endpoints) > 1:
                    log.warning("LLM %s failed, trying another endpoint: %s", endpoint.model, exc)
                continue
            finally:
                self._release(index)
            with self._cond:
                endpoint.failed_at = None
                endpoint.calls += 1
            return reply
        assert error is not None
        raise error

    def _acquire(self, tried: set[int]) -> int:
        is_background = background.get()
        with self._cond:
            if not is_background:
                self._interactive_waiting += 1
            try:
                while True:
                    if not (is_background and self._interactive_waiting):
                        index = self._pick(tried)
                        if index is not None:
                            self.endpoints[index].busy += 1
                            return index
                    self._cond.wait()
            finally:
                if not is_background:
                    self._interactive_waiting -= 1

    def _pick(self, tried: set[int]) -> int | None:
        """Caller holds the lock. Next endpoint with a free slot; recently failed ones only
        when no other is left to try."""
        count = len(self.endpoints)
        order = [(self._next + i) % count for i in range(count)]
        candidates = [i for i in order if i not in tried]
        now = self._clock()
        healthy = [
            i
            for i in candidates
            if self.endpoints[i].failed_at is None
            or now - self.endpoints[i].failed_at >= self.cooldown
        ]
        for i in healthy or candidates:
            if self.endpoints[i].busy < self.endpoints[i].limit:
                self._next = (i + 1) % count
                return i
        return None

    def _release(self, index: int) -> None:
        with self._cond:
            self.endpoints[index].busy -= 1
            self._cond.notify_all()

    def list_models(self) -> list[str]:
        """Models listed by the main endpoint."""
        return self.endpoints[0].client.list_models()

    def status(self) -> list[dict[str, Any]]:
        with self._cond:
            now = self._clock()
            return [
                {
                    "model": e.model,
                    "base_url": e.client.config.base_url,
                    "max_concurrent": e.limit,
                    "busy": e.busy,
                    "calls": e.calls,
                    "failing": e.failed_at is not None and now - e.failed_at < self.cooldown,
                }
                for e in self.endpoints
            ]

    def close(self) -> None:
        for endpoint in self.endpoints:
            endpoint.client.close()


def extract_json(text: str) -> Any:
    """Parse JSON from a model reply that may contain think blocks, fences or prose."""
    cleaned = _FENCE.sub("", _THINK_BLOCK.sub("", text)).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = cleaned.find(opener), cleaned.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError(f"LLM reply is not valid JSON: {cleaned[:200]!r}")


def parse_variants(text: str) -> list[tuple[str, list[str]]]:
    """Turn a model reply into ``[(label, [tags...]), ...]``.

    Accepts ``{"variants": [{"label": .., "tags": [..]}]}``, a bare list of such objects,
    or a bare list of tag lists.
    """
    data = extract_json(text)
    items = data.get("variants", []) if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise LLMError("LLM reply has no 'variants' list")
    variants: list[tuple[str, list[str]]] = []
    for index, item in enumerate(items, start=1):
        if isinstance(item, dict):
            label = str(item.get("label") or f"Suggestion {index}")
            raw_tags = item.get("tags") or []
        elif isinstance(item, list):
            label, raw_tags = f"Suggestion {index}", item
        else:
            continue
        tags = [str(tag) for tag in raw_tags if isinstance(tag, (str, int, float))]
        if tags:
            variants.append((label, tags))
    if not variants:
        raise LLMError("LLM returned no usable variants")
    return variants


def parse_keywords(text: str, words: list[str]) -> tuple[list[dict[str, str]], list[str]]:
    """Turn a keyword-normalisation reply into ``([{word, base}], related)``.

    Words the model skipped keep their lower-cased original as base form, so every input
    word is always present exactly once and in order.
    """
    data = extract_json(text)
    if not isinstance(data, dict):
        raise LLMError("keyword reply is not a JSON object")
    bases: dict[str, str] = {}
    for item in data.get("keywords") or []:
        if isinstance(item, dict) and item.get("word") and item.get("base"):
            bases.setdefault(str(item["word"]).casefold(), str(item["base"]).strip().lower())
    keywords = [{"word": w, "base": bases.get(w.casefold(), w.lower()) or w.lower()} for w in words]
    related = [str(r).strip().lower() for r in data.get("related") or [] if str(r).strip()]
    return keywords, related
