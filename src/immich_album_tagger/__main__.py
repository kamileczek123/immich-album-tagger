"""Command line entry point: ``immich-album-tagger <command>``.

Commands:
  serve        run the web UI (default)
  check        test the connection to Immich and the LLM
  suggest-all  write suggestions for many albums to a JSON file for review
  apply        apply the reviewed JSON file
  share        share many albums with one user (or remove access)
  remove       take back the tagging of albums (tags and description lines)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path

from . import __version__
from .config import ConfigError, load_config
from .immich import ImmichError
from .llm import LLMError
from .service import TaggerService, Targets

PICK_CHOICES = ["first", "album-name", "rules", "keywords", "llm", "none"]


def _eprint(*args: object) -> None:
    print(*args, file=sys.stderr, flush=True)


def _cli_config(config_dir: Path):
    """Config for the commands that work with the API key from immich.yaml."""
    config = load_config(config_dir)
    if not config.immich.api_key:
        raise ConfigError("immich.api_key is empty (set IMMICH_API_KEY); the CLI needs it")
    return config


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .app import create_app

    os.environ["CONFIG_DIR"] = str(args.config_dir)
    app = create_app(load_config(args.config_dir))  # fails fast with a readable error
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level.lower())
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    config = _cli_config(args.config_dir)
    service = TaggerService.from_config(config)
    ok = True
    try:
        info = service.immich.check()
        print(f"Immich  OK   {config.immich.url}  version {info['version']}")
        print(f"        OK   API key belongs to {info['user']} ({info['user_id']})")
        albums = service.immich.list_albums()
        print(f"        OK   {len(albums)} albums visible to this API key")
        print(f"        OK   {len(service.immich.list_tags())} existing tags")
    except ImmichError as exc:
        ok = False
        print(f"Immich  FAIL {exc}")
    if service.llm is None:
        print("LLM     off  (llm.enabled is false)")
    else:
        # Every endpoint of the pool on its own: a failover would hide a broken one.
        for endpoint in service.llm.endpoints:
            client, label = endpoint.client, f"{endpoint.model} @ {endpoint.client.config.base_url}"
            try:
                models = client.list_models()
                listed = endpoint.model in models
                marker = "OK  " if listed or not models else "WARN"
                note = "" if listed else " (model not in the /models list)"
                print(f"LLM     {marker} {label}{note}, {endpoint.limit} at a time")
                reply = client.complete('Reply with the JSON object {"ok": true}.', "ping")
                print(f"        OK   model replied: {reply.strip()[:80]!r}")
            except LLMError as exc:
                ok = False
                print(f"LLM     FAIL {label}: {exc}")
    service.close()
    return 0 if ok else 1


def _pick(variants: list[dict], how: str) -> list[str]:
    if how == "none" or not variants:
        return []
    if how == "first":
        return list(variants[0]["tags"])
    for variant in variants:
        if variant["source"] == how:
            return list(variant["tags"])
    return []


def cmd_suggest_all(args: argparse.Namespace) -> int:
    config = _cli_config(args.config_dir)
    service = TaggerService.from_config(config)
    pattern = re.compile(args.match, re.IGNORECASE) if args.match else None
    albums = service.list_albums()
    if args.only_untagged:
        albums = [a for a in albums if not a["applied"]]
    if pattern:
        albums = [a for a in albums if pattern.search(a["name"])]
    if args.limit:
        albums = albums[: args.limit]

    existing = service.existing_tags() if service.llm else []
    entries = []
    for index, album_dict in enumerate(albums, start=1):
        album = service.immich.get_album(album_dict["id"])
        result = service.suggester.suggest(album, existing)
        variants = [v.to_dict() for v in result.variants]
        entries.append(
            {
                "album_id": album.id,
                "album_name": album.name,
                "asset_count": album.asset_count,
                "selected": _pick(variants, args.pick),
                # base forms by default; put the original word here too to keep both
                "selected_keywords": [k["base"] for k in result.keywords],
                "keywords": result.keywords,
                "related": result.related,
                "variants": variants,
                "warnings": result.warnings,
            }
        )
        _eprint(f"[{index}/{len(albums)}] {album.name}: {len(variants)} variants")

    text = json.dumps(entries, ensure_ascii=False, indent=2)
    if args.out == "-":
        print(text)
    else:
        Path(args.out).write_text(text, encoding="utf-8")
    _eprint(
        f"Wrote {len(entries)} albums to {args.out}. Edit 'selected', then run "
        f"'immich-album-tagger apply --file {args.out}'."
    )
    service.close()
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    config = _cli_config(args.config_dir)
    service = TaggerService.from_config(config)
    raw = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
    entries = json.loads(raw)
    defaults = config.tagging.apply
    targets = Targets(
        tags=defaults.tags if args.tags is None else args.tags,
        photo_description=(
            defaults.photo_description if args.photo_description is None else args.photo_description
        ),
        album_description=(
            defaults.album_description if args.album_description is None else args.album_description
        ),
    )
    _eprint(f"Targets: {', '.join(k for k, v in vars(targets).items() if v) or 'none'}")
    todo = [e for e in entries if e.get("selected") or e.get("selected_keywords")]
    _eprint(
        f"{len(todo)} of {len(entries)} albums have selected tags"
        + (" (dry run)" if args.dry_run else "")
    )
    failures = 0
    for index, entry in enumerate(todo, start=1):
        prefix = f"[{index}/{len(todo)}] {entry.get('album_name', entry['album_id'])}"
        try:
            result = service.apply(
                entry["album_id"],
                entry.get("selected") or [],
                entry.get("selected_keywords") or [],
                targets,
                dry_run=args.dry_run,
            )
        except (ImmichError, ValueError) as exc:
            failures += 1
            _eprint(f"{prefix}: FAILED {exc}")
            continue
        state = "would tag" if args.dry_run else "tagged"
        what = ", ".join(result.tags) or "-"
        _eprint(f"{prefix}: {state} {result.own_count}/{result.asset_count} photos; tags: {what}")
        if targets.album_description:
            _eprint(f"    album keywords: {' '.join(result.keywords)}")
        for warning in result.warnings:
            _eprint(f"    warning: {warning}")
    service.close()
    return 1 if failures else 0


def cmd_share(args: argparse.Namespace) -> int:
    config = _cli_config(args.config_dir)
    service = TaggerService.from_config(config)
    me = service.immich.me()["id"]
    pattern = re.compile(args.match, re.IGNORECASE) if args.match else None
    albums = [
        a
        for a in service.immich.list_albums()
        if (not a.owner or a.owner.id == me)
        and (not pattern or pattern.search(a.name))
        and (not args.album_id or a.id in args.album_id)
    ]
    action = "Remove access of" if args.remove else f"Share as {args.role} with"
    _eprint(f"{action} {args.user}: {len(albums)} albums")
    for album in albums:
        _eprint(f"  {album.name}")
    if args.dry_run or not albums:
        service.close()
        return 0
    out = service.share_many([a.id for a in albums], args.user, args.role, remove=args.remove)
    for entry in out["results"]:
        detail = f" ({entry['detail']})" if entry.get("detail") else ""
        _eprint(f"  {entry['status']:9} {entry.get('album_name', entry['album_id'])}{detail}")
    _eprint(", ".join(f"{k}: {v}" for k, v in out["counts"].items()))
    service.close()
    return 1 if out["counts"].get("error") else 0


def cmd_remove(args: argparse.Namespace) -> int:
    """Take back the tagging of albums the tool tagged (tags and description lines)."""
    if not (args.match or args.album_id or args.all):
        _eprint("Choose albums with --match, --album-id or --all.")
        return 2
    config = _cli_config(args.config_dir)
    service = TaggerService.from_config(config)
    tagged = service.history.latest_by_album(service.immich.me()["id"])
    pattern = re.compile(args.match, re.IGNORECASE) if args.match else None
    albums = [
        a
        for a in service.immich.list_albums()
        if a.id in tagged
        and (not pattern or pattern.search(a.name))
        and (not args.album_id or a.id in args.album_id)
    ]
    _eprint(f"Remove tagging of {len(albums)} albums")
    for album in albums:
        _eprint(f"  {album.name}: {', '.join(tagged[album.id]['tags']) or '-'}")
    if args.dry_run or not albums:
        service.close()
        return 0
    failed = 0
    for album in albums:
        try:
            result = service.remove(album.id)
        except (ValueError, ImmichError) as exc:
            failed += 1
            _eprint(f"  FAIL {album.name}: {exc}")
            continue
        status = "OK  " if result.complete is not False else "WARN"
        _eprint(f"  {status} {album.name}: {result.untagged} photos untagged")
        for warning in result.warnings:
            _eprint(f"       {warning}")
    service.close()
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="immich-album-tagger",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=Path(os.environ.get("CONFIG_DIR", "config")),
        help="directory with immich.yaml, llm.yaml, tagging.yaml",
    )
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the web UI")
    serve.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    serve.set_defaults(func=cmd_serve)

    check = sub.add_parser("check", help="test connections to Immich and the LLM")
    check.set_defaults(func=cmd_check)

    suggest = sub.add_parser("suggest-all", help="write suggestions for many albums to JSON")
    suggest.add_argument("--out", required=True, help="output JSON file, or - for stdout")
    suggest.add_argument(
        "--only-untagged", action="store_true", help="skip albums already tagged by this tool"
    )
    suggest.add_argument("--match", help="only albums whose title matches this regex")
    suggest.add_argument("--limit", type=int, help="process at most N albums")
    suggest.add_argument(
        "--pick",
        choices=PICK_CHOICES,
        default="first",
        help="which variant to pre-fill into 'selected' (default: first)",
    )
    suggest.set_defaults(func=cmd_suggest_all)

    apply = sub.add_parser("apply", help="apply the 'selected' tags from a suggestions file")
    apply.add_argument("--file", required=True, help="suggestions JSON file, or - for stdin")
    apply.add_argument("--dry-run", action="store_true", help="show what would happen")
    for flag, help_text in (
        ("tags", "attach tags to photos"),
        ("photo-description", "write the tag line into photo descriptions"),
        ("album-description", "write the keyword line into the album description"),
    ):
        apply.add_argument(
            f"--{flag}",
            action=argparse.BooleanOptionalAction,
            default=None,
            help=f"{help_text} (default from tagging.yaml 'apply')",
        )
    apply.set_defaults(func=cmd_apply)
    share = sub.add_parser("share", help="share many of your albums with one user")
    share.add_argument("--user", required=True, help="e-mail or id of an Immich user")
    share.add_argument("--role", choices=["viewer", "editor"], default="viewer")
    share.add_argument("--match", help="only albums whose title matches this regex")
    share.add_argument("--album-id", action="append", help="only this album (repeatable)")
    share.add_argument("--remove", action="store_true", help="remove access instead")
    share.add_argument("--dry-run", action="store_true", help="list the albums, change nothing")
    share.set_defaults(func=cmd_share)

    remove = sub.add_parser(
        "remove", help="take back the tagging of albums (tags and the app's description lines)"
    )
    remove.add_argument("--match", help="only tagged albums whose title matches this regex")
    remove.add_argument("--album-id", action="append", help="only this album (repeatable)")
    remove.add_argument("--all", action="store_true", help="every album the tool tagged")
    remove.add_argument("--dry-run", action="store_true", help="list the albums, change nothing")
    remove.set_defaults(func=cmd_remove)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        args = parser.parse_args([*(argv or sys.argv[1:]), "serve"])
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per request is too noisy
    logging.basicConfig(
        level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        return args.func(args)
    except ConfigError as exc:
        _eprint(f"Configuration error: {exc}")
        return 2
    except (ImmichError, LLMError) as exc:
        _eprint(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
