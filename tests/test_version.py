import re
from pathlib import Path

import immich_album_tagger

REPO = Path(__file__).resolve().parents[1]


def test_version_comes_from_pyproject():
    # The CI build refuses tags that differ from this version, so they must stay in sync.
    declared = re.search(r'^version = "(.+)"', (REPO / "pyproject.toml").read_text(), re.M)
    assert declared and immich_album_tagger.__version__ == declared.group(1)


def test_cli_name():
    text = (REPO / "pyproject.toml").read_text()
    assert 'immich-album-tagger = "immich_album_tagger.__main__:main"' in text
