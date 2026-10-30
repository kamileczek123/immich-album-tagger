import re
from pathlib import Path

from immich_album_tagger import __version__
from immich_album_tagger.auth import Sessions, hash_secret, load_credentials, verify_secret
from immich_album_tagger.i18n import Translator, bundled_dir, parse_lan, sync_language_files
from immich_album_tagger.oplog import ERROR, SUCCESS, OperationLog

REPO = Path(__file__).resolve().parents[1]


def test_language_files_match_app_version_and_keys():
    files = {p.stem: parse_lan(p.read_text("utf-8")) for p in bundled_dir().glob("*.lan")}
    assert {"english", "polish"} <= set(files)
    english = set(files["english"][0])
    for name, (messages, meta) in files.items():
        assert meta["version"] == __version__, f"{name}.lan @version must be bumped"
        base = {re.sub(r"\.(one|few|many|other)$", "", k) for k in messages}
        assert {re.sub(r"\.(one|few|many|other)$", "", k) for k in english} <= base, name


def test_parse_and_translate_with_fallback():
    messages, meta = parse_lan("# c\n@code = pl\na = Ala ma {n} kotów\nbad line\n")
    assert meta == {"code": "pl"} and messages == {"a": "Ala ma {n} kotów"}
    t = Translator(messages, {"b": "fallback {x}"}, "polish")
    assert t("a", n=3) == "Ala ma 3 kotów"
    assert t("b", x=1) == "fallback 1"
    assert t("missing.key") == "missing.key"
    assert t("a") == "Ala ma {n} kotów"  # missing parameter does not crash


def test_sync_copies_and_upgrades_with_backup(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "polish.lan").write_text("@version = 0.4.0\na = new\n", "utf-8")
    target = tmp_path / "language"
    assert sync_language_files(target, "0.4.0", source) == ["polish.lan"]
    (target / "polish.lan").write_text("@version = 0.4.0\na = edited\n", "utf-8")
    assert sync_language_files(target, "0.4.0", source) == []  # same version: user edits kept
    assert "edited" in (target / "polish.lan").read_text()
    (target / "polish.lan").write_text("@version = 0.3.0\na = old\n", "utf-8")
    assert sync_language_files(target, "0.4.0", source) == ["polish.lan"]
    assert (target / "polish.lan.bak-0.3.0").read_text().endswith("old\n")


def test_unknown_language_falls_back_to_english(tmp_path):
    t = Translator.load(tmp_path, "klingon")
    assert t.language == "english" and t("logs.status.error") == "ERROR"


def test_hashing_and_credentials(tmp_path):
    stored = hash_secret("sekret")
    assert verify_secret("sekret", stored) and not verify_secret("Sekret", stored)
    path = tmp_path / "credentials.yaml"
    path.write_text("encrypted: false\nusername: alex\npassword: sekret\n")
    creds = load_credentials(path)
    assert creds.encrypted and creds.check("alex", "sekret") and not creds.check("alex", "x")
    assert "sekret" not in path.read_text()
    again = load_credentials(path)  # already hashed: read as is
    assert again.check("alex", "sekret")
    assert load_credentials(tmp_path / "missing.yaml") is None


def test_read_only_credentials_still_work(tmp_path):
    path = tmp_path / "credentials.yaml"
    path.write_text("username: alex\npassword: sekret\n")
    path.chmod(0o444)
    tmp_path.chmod(0o555)
    try:
        creds = load_credentials(path)
    finally:
        tmp_path.chmod(0o755)
        path.chmod(0o644)
    if creds.encrypted:  # running as root: permissions are not enforced
        return
    assert creds.check("alex", "sekret")


def test_sessions(tmp_path):
    sessions = Sessions(tmp_path / "session.key", hours=1)
    token = sessions.issue()
    assert sessions.valid(token) and not sessions.valid(token + "x") and not sessions.valid(None)
    assert Sessions(tmp_path / "session.key", hours=1).valid(token)  # key persists


def test_operation_log_persists_and_clears(tmp_path):
    log = OperationLog(tmp_path / "logs")
    log.add(SUCCESS, "apply", "a1", "Album", {"tags": ["x"]})
    log.add(ERROR, "share", "a1", "Album", {"user": "u"}, "boom")
    reopened = OperationLog(tmp_path / "logs")  # e.g. after a restart
    entries = reopened.entries()
    assert [e["status"] for e in entries] == [ERROR, SUCCESS]
    assert entries[0]["message"] == "boom"
    reopened.add(SUCCESS, "unshare")
    assert len(OperationLog(tmp_path / "logs").entries()) == 3
    reopened.clear()
    assert reopened.entries() == []


def test_credentials_from_environment_and_old_hash_format(tmp_path, monkeypatch):
    import base64
    import hashlib

    import pytest

    from immich_album_tagger.auth import AuthError

    user_hash, pass_hash = hash_secret("alex"), hash_secret("sekret")
    assert "$" not in user_hash and user_hash.startswith("scrypt:")
    monkeypatch.setenv("UI_USER_HASH", user_hash)
    monkeypatch.setenv("UI_PASS_HASH", pass_hash)
    path = tmp_path / "credentials.yaml"
    content = "encrypted: true\nusername: ${UI_USER_HASH}\npassword: ${UI_PASS_HASH}\n"
    path.write_text(content)
    creds = load_credentials(path)
    assert creds.check("alex", "sekret") and not creds.check("alex", "x")
    assert path.read_text() == content  # already hashed: file left as deployed

    salt = b"0123456789abcdef"
    digest = hashlib.scrypt(b"sekret", salt=salt, dklen=32, n=2**14, r=8, p=1)
    old = f"scrypt${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"
    assert verify_secret("sekret", old) and not verify_secret("other", old)

    monkeypatch.delenv("UI_PASS_HASH")
    with pytest.raises(AuthError, match="environment variables"):
        load_credentials(path)
