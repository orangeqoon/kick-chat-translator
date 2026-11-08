import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kicktrans import config
from kicktrans.config import Config


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    paths = {
        "CONFIG_PATH": tmp_path / "state" / "services" / "kick-translator" / "config.json",
        "LEGACY_CONFIG_PATH": tmp_path / "legacy" / "config.json",
        "SHARED_CONFIG_PATH": tmp_path / "state" / "services" / "kick-config.json",
    }
    for name, value in paths.items():
        monkeypatch.setattr(config, name, value)
    return paths


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_shared_app_identity_never_copies_secrets(isolated_config):
    paths = isolated_config
    write_json(paths["SHARED_CONFIG_PATH"], {
        "client_id": "public-app", "redirect_uri": "http://localhost:3000/oauth2callback",
        "client_secret": "fake-sensitive-shared-value", "live_posting": True,
        "access_token": "fake-sensitive-token",
    })
    original = paths["SHARED_CONFIG_PATH"].read_bytes()
    cfg = Config.load()
    assert cfg.client_id == "public-app" and not cfg.live_posting
    assert not paths["CONFIG_PATH"].exists()
    cfg.save()
    saved = paths["CONFIG_PATH"].read_text(encoding="utf-8")
    assert "fake-sensitive" not in saved and "client_secret" not in saved
    assert paths["SHARED_CONFIG_PATH"].read_bytes() == original


def test_legacy_config_read_only_and_dry_run_until_explicit_save(isolated_config):
    paths = isolated_config
    write_json(paths["LEGACY_CONFIG_PATH"], {
        "client_id": "old-app", "redirect_uri": "http://localhost:8765/callback",
        "live_posting": True, "skip_users": ["botrix"],
    })
    original = paths["LEGACY_CONFIG_PATH"].read_bytes()
    cfg = Config.load()
    assert cfg.client_id == "old-app" and cfg.skip_users == ["botrix"]
    assert not cfg.live_posting
    assert not paths["CONFIG_PATH"].exists()
    cfg.save()
    assert paths["LEGACY_CONFIG_PATH"].read_bytes() == original
    assert paths["CONFIG_PATH"].exists()


def test_canonical_config_has_priority_and_preserves_explicit_posting(isolated_config):
    paths = isolated_config
    write_json(paths["CONFIG_PATH"], {"client_id": "canonical", "live_posting": True})
    write_json(paths["LEGACY_CONFIG_PATH"], {"client_id": "old"})
    write_json(paths["SHARED_CONFIG_PATH"], {"client_id": "shared", "client_secret": "fake"})
    cfg = Config.load()
    assert cfg.client_id == "canonical" and cfg.live_posting


@pytest.mark.parametrize("data", [
    {"client_secret": "sensitive-sentinel"},
    {"access_token": "sensitive-sentinel"},
    {"refresh_token": "sensitive-sentinel"},
    ["sensitive-sentinel"],
    {"live_posting": "sensitive-sentinel"},
    {"redirect_uri": "https://sensitive-sentinel.example/callback"},
    {"redirect_uri": "http://localhost:0/callback"},
])
def test_invalid_config_rejected_without_value_disclosure(tmp_path, data):
    path = tmp_path / "config.json"
    write_json(path, data)
    with pytest.raises(ValueError) as error:
        Config.load(path)
    assert "sensitive-sentinel" not in str(error.value)


def test_atomic_config_failure_preserves_previous_and_backup(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    Config(client_id="before").save(path)
    original = path.read_bytes()
    def fail_replace(*args):
        raise OSError("simulated replace failure")
    monkeypatch.setattr(config.os, "replace", fail_replace)
    with pytest.raises(OSError):
        Config(client_id="after").save(path)
    assert path.read_bytes() == original
    backups = list(tmp_path.glob("config.json.bak-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    assert not list(tmp_path.glob(".config-*"))


def test_repeated_saves_keep_each_previous_config(tmp_path):
    path = tmp_path / "config.json"
    for app in ("first", "second", "third"):
        Config(client_id=app).save(path)
    backups = [json.loads(p.read_text(encoding="utf-8"))["client_id"]
               for p in tmp_path.glob("config.json.bak-*")]
    assert sorted(backups) == ["first", "second"]
    assert Config.load(path).client_id == "third"


def test_path_import_honors_repository_overrides_without_creating_state(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    state = tmp_path / "separate-state"
    env = os.environ.copy()
    env.update(SCRIPTS_ROOT=str(root), SCRIPTS_STATE=str(state))
    result = subprocess.run(
        [sys.executable, "-c", "from kicktrans.paths import STATE_DIR; print(STATE_DIR)"],
        cwd=Path(__file__).resolve().parents[1], env=env,
        capture_output=True, text=True, timeout=10, check=True,
    )
    assert result.stdout.strip() == str(state.resolve())
    assert not state.exists()


def test_init_config_keeps_dry_run_and_does_not_overwrite(isolated_config, monkeypatch):
    from kicktrans import __main__ as cli
    paths = isolated_config
    monkeypatch.setattr(cli, "CONFIG_PATH", paths["CONFIG_PATH"])
    monkeypatch.setattr(sys, "argv", ["kicktrans", "init-config"])
    cli.main()
    assert Config.load().live_posting is False
    original = paths["CONFIG_PATH"].read_bytes()
    cli.main()
    assert paths["CONFIG_PATH"].read_bytes() == original


def test_migrate_auth_requires_explicit_source(monkeypatch):
    from kicktrans import __main__ as cli
    monkeypatch.setattr(sys, "argv", ["kicktrans", "migrate-auth"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2


def test_new_config_creation_refuses_concurrent_writer(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    operation = "rename" if os.name == "nt" else "link"
    original = getattr(config.os, operation)
    competing = b'{"client_id":"concurrent-owner"}'
    def race(source, destination):
        path.write_bytes(competing)
        return original(source, destination)
    monkeypatch.setattr(config.os, operation, race)
    with pytest.raises(FileExistsError):
        Config(client_id="initializer").save(path, overwrite=False)
    assert path.read_bytes() == competing
    assert not list(tmp_path.glob(".config-*"))


def test_console_demo_tolerates_delayed_first_event(monkeypatch):
    import queue
    import signal
    from kicktrans import __main__ as cli, engine

    class DelayedQueue:
        def __init__(self):
            self.calls = 0
        def get(self, timeout):
            self.calls += 1
            if self.calls == 1:
                raise queue.Empty
            return "停止しました"
        def empty(self):
            return True
    class Thread:
        def is_alive(self):
            return True
    class FakeEngine:
        def __init__(self, *args, **kwargs):
            self._thread = Thread()
        def start(self):
            pass
        def stop(self):
            pass
    events = DelayedQueue()
    monkeypatch.setattr(cli.queue, "Queue", lambda: events)
    monkeypatch.setattr(engine, "Engine", FakeEngine)
    monkeypatch.setattr(cli.Config, "load", lambda: Config())
    monkeypatch.setattr(signal, "signal", lambda *args: None)
    cli._run_engine(True, True)
    assert events.calls == 2

def test_module_entrypoint_sanitizes_provider_exceptions(monkeypatch):
    import getpass
    import runpy
    import warnings
    from kicktrans import storage

    class ProviderFailure(Exception):
        pass
    def fail(secret):
        raise ProviderFailure("sensitive-provider-sentinel")
    monkeypatch.setattr(storage, "save_client_secret", fail)
    monkeypatch.setattr(getpass, "getpass", lambda *args: "synthetic-input")
    monkeypatch.setattr(sys, "argv", ["kicktrans", "set-secret"])
    with warnings.catch_warnings(), pytest.raises(SystemExit) as error:
        warnings.simplefilter("ignore", RuntimeWarning)
        runpy.run_module("kicktrans", run_name="__main__")
    assert "ProviderFailure" in str(error.value)
    assert "sensitive-provider-sentinel" not in str(error.value)