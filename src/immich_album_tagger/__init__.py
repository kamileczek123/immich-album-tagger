"""Immich Album Tagger - suggest tags from album titles and apply them to every photo."""

from importlib.metadata import PackageNotFoundError, version

try:
    # Single source of truth: "version" in pyproject.toml (checked against the git tag in CI).
    __version__ = version("immich-album-tagger")
except PackageNotFoundError:  # running from a source tree without installing
    __version__ = "0.0.0+unknown"
