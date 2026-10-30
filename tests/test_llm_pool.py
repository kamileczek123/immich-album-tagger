"""Several LLM endpoints side by side, parallel requests, prefetch workers."""

import json
import threading
import time

import httpx
import pytest

from immich_album_tagger.config import LLMConfig, PrefetchConfig
from immich_album_tagger.immich import Album
from immich_album_tagger.llm import LLMError, LLMPool, background
from immich_album_tagger.prefetch import Prefetcher
from immich_album_tagger.suggest import SuggestionResult


def config(**kwargs) -> LLMConfig:
    base = {"enabled": True, "base_url": "http://gw/v1", "model": "main", "api_key": "k"}
    return LLMConfig(**{**base, **kwargs})


class Server:
    """OpenAI-compatible fake that records which model got each request."""

    def __init__(self, delay=0.0, failing=()):
        self.delay = delay
        self.failing = set(failing)
        self.lock = threading.Lock()
        self.seen: list[tuple[str, str]] = []  # (host, model)
        self.active: dict[str, int] = {}
        self.peak: dict[str, int] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        with self.lock:
            self.seen.append((request.url.host, model))
            self.active[model] = self.active.get(model, 0) + 1
            self.peak[model] = max(self.peak.get(model, 0), self.active[model])
        try:
            time.sleep(self.delay)
            if model in self.failing:
                return httpx.Response(500, text="boom")
            reply = {"choices": [{"message": {"content": f"from {model}"}}]}
            return httpx.Response(200, json=reply)
        finally:
            with self.lock:
                self.active[model] -= 1


def pool(server, cfg, **kwargs) -> LLMPool:
    return LLMPool(cfg, transport=httpx.MockTransport(server.handler), **kwargs)


def test_extra_endpoints_inherit_the_main_settings():
    cfg = config(
        max_concurrent=3,
        extra_body={"x": 1},
        endpoints=[
            {"model": "second"},
            {"model": "gpu", "base_url": "http://gpu/v1/", "api_key": "g", "max_concurrent": 1},
        ],
    )
    main, second, gpu = cfg.all_endpoints()
    assert (main.model, main.max_concurrent) == ("main", 3)
    assert (second.base_url, second.api_key, second.max_concurrent) == ("http://gw/v1", "k", 3)
    assert second.extra_body == {"x": 1} and second.endpoints == []
    assert (gpu.base_url, gpu.api_key, gpu.max_concurrent) == ("http://gpu/v1", "g", 1)


def test_round_robin_over_endpoints():
    server = Server()
    llm = pool(server, config(endpoints=[{"model": "second"}]))
    assert llm.models == ["main", "second"] and llm.capacity == 4
    replies = [llm.complete("s", "u") for _ in range(4)]
    assert replies == ["from main", "from second", "from main", "from second"]


def test_concurrency_stays_within_each_endpoint_limit():
    server = Server(delay=0.05)
    llm = pool(server, config(max_concurrent=2, endpoints=[{"model": "b", "max_concurrent": 1}]))
    threads = [threading.Thread(target=llm.complete, args=("s", "u")) for _ in range(9)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert server.peak == {"main": 2, "b": 1}  # parallel, never above the limits
    assert len(server.seen) == 9


def test_failover_and_cooldown():
    server = Server(failing={"main"})
    clock = [0.0]
    llm = pool(server, config(endpoints=[{"model": "b"}]), clock=lambda: clock[0])
    assert llm.complete("s", "u") == "from b"  # main failed, b answered
    assert [m for _, m in server.seen] == ["main", "b"]
    server.seen.clear()
    llm.complete("s", "u")
    llm.complete("s", "u")
    assert [m for _, m in server.seen] == ["b", "b"]  # main skipped while cooling down
    assert llm.status()[0]["failing"] is True
    server.failing.clear()
    clock[0] += 61
    server.seen.clear()
    llm.complete("s", "u")
    llm.complete("s", "u")
    assert {m for _, m in server.seen} == {"main", "b"}  # back in rotation


def test_all_endpoints_failing_raises():
    server = Server(failing={"main", "b"})
    with pytest.raises(LLMError, match="HTTP 500"):
        pool(server, config(endpoints=[{"model": "b"}])).complete("s", "u")


def test_interactive_requests_go_before_background_ones():
    gate = threading.Event()
    order: list[str] = []

    def handler(request):
        user = json.loads(request.content)["messages"][1]["content"]
        if user == "first":
            gate.wait(2)
        order.append(user)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    llm = LLMPool(config(max_concurrent=1), transport=httpx.MockTransport(handler))

    def run(text, is_background):
        background.set(is_background)
        llm.complete("s", text)

    first = threading.Thread(target=run, args=("first", True))
    first.start()
    time.sleep(0.05)  # "first" holds the only slot
    waiting_bg = threading.Thread(target=run, args=("background", True))
    waiting_bg.start()
    time.sleep(0.05)
    waiting_ui = threading.Thread(target=run, args=("interactive", False))
    waiting_ui.start()
    time.sleep(0.05)
    gate.set()
    for thread in (first, waiting_bg, waiting_ui):
        thread.join(2)
    assert order == ["first", "interactive", "background"]


def test_album_requests_run_side_by_side(tagging_config):
    from immich_album_tagger.suggest import Suggester

    both = threading.Barrier(2, timeout=2)  # breaks if the two requests come one by one

    class LLM:
        def complete(self, system, user):
            both.wait()
            if "variants" in system:
                return '{"variants": [{"label": "A", "tags": ["x"]}]}'
            return '{"keywords": [{"word": "Zielonej", "base": "zielona"}], "related": []}'

    album = Album(id="a", name="Sylwester na Zielonej", asset_count=1)
    result = Suggester(tagging_config, LLM()).suggest(album)
    assert not result.warnings
    assert any(v.source == "llm" for v in result.variants)
    assert "zielona" in {k["base"] for k in result.keywords}


def test_prefetch_workers_prepare_albums_at_the_same_time():
    both = threading.Barrier(2, timeout=2)
    done: list[str] = []

    def compute(album_id):
        both.wait()  # only passes when two albums are being prepared at once
        done.append(album_id)
        return Album(id=album_id, name=album_id, asset_count=1), SuggestionResult()

    prefetcher = Prefetcher(PrefetchConfig(), lambda n: ["a", "b", "c"][:n], compute, workers=2)
    threads = [threading.Thread(target=prefetcher.run_once) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(3)
    assert sorted(done) == ["a", "b"]  # two different albums, never the same twice
    assert prefetcher.status()["workers"] == 2
