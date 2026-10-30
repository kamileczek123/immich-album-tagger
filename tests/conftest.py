"""Shared fixtures."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fakes import FakeImmich

from immich_album_tagger.config import (
    AppConfig,
    AppSettings,
    ImmichConfig,
    LLMConfig,
    TaggingConfig,
)
from immich_album_tagger.history import History
from immich_album_tagger.immich import ImmichClient
from immich_album_tagger.service import TaggerService
from immich_album_tagger.suggest import Suggester

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def fake_immich() -> FakeImmich:
    return FakeImmich()


@pytest.fixture
def immich_config() -> ImmichConfig:
    return ImmichConfig(
        url="http://immich.test:2283", api_key="secret", page_size=2, tag_batch_size=2
    )


@pytest.fixture
def tagging_config() -> TaggingConfig:
    import yaml

    raw = yaml.safe_load((REPO / "config" / "tagging.yaml").read_text(encoding="utf-8"))
    return TaggingConfig.model_validate(raw)


def make_service(
    fake: FakeImmich,
    immich_config: ImmichConfig,
    tagging_config: TaggingConfig,
    tmp_path: Path,
    llm=None,
    app: AppSettings | None = None,
) -> TaggerService:
    config = AppConfig(
        immich=immich_config,
        llm=LLMConfig(),
        tagging=tagging_config,
        app=app or AppSettings(),
        config_dir=tmp_path / "config",
        data_dir=tmp_path,
        language_dir=tmp_path / "language",
        log_dir=tmp_path / "logs",
    )
    client = ImmichClient(immich_config, transport=httpx.MockTransport(fake.handler))
    history = History(tmp_path / "history.sqlite3")
    suggester = Suggester(tagging_config, llm)
    return TaggerService(config, client, suggester, history, llm, sleep=lambda _: None)
