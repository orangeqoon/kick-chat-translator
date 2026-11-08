"""Offline cross-component checks using synthetic data and native Windows DPAPI."""
import asyncio
import http.client
import importlib.util
import io
import json
import sys
import time
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

from kicktrans import kick_api, storage
from kicktrans.config import Config


@pytest.fixture
def native_store(tmp_path, monkeypatch):
    if sys.platform != "win32":
        pytest.skip("Native Windows DPAPI integration")
    canonical = tmp_path / "state" / "tokens" / "kick-translator"
    legacy = tmp_path / "state" / "services" / "kick-translator-auth"
    for name, path in {
        "CANONICAL_DIR": canonical,
        "STORE_PATH": canonical / "credentials.dpapi",
        "LEGACY_HELPER_DIR": legacy,
        "LEGACY_TOKENS_PATH": legacy / "kick-tokens.dpapi",
        "LEGACY_CLIENT_SECRET_PATH": legacy / "client-secret.dpapi",
    }.items():
        monkeypatch.setattr(storage, name, path)
    monkeypatch.delenv("KICK_CLIENT_SECRET", raising=False)
    return canonical, legacy


def load_helper():
    path = Path(__file__).resolve().parents[1] / "auth" / "kick_oauth_helper.py"
    spec = importlib.util.spec_from_file_location("offline_helper_integration", path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    return helper


@pytest.mark.parametrize("rotated", [True, False])
def test_native_encrypted_store_refreshes_through_api_client(native_store, monkeypatch, rotated):
    canonical, _ = native_store
    secret = "synthetic-client-secret-for-offline-test"
    storage.save_client_secret(secret)
    kick_api.save_tokens(kick_api.Tokens("synthetic-old-access", "synthetic-old-refresh", 1))
    initial = storage.STORE_PATH.read_bytes()
    assert b"synthetic-old-access" not in initial and secret.encode() not in initial
    requests = []
    async def fake_request(session, form):
        assert form["grant_type"] == "refresh_token"
        assert form["client_secret"] == secret
        assert form["refresh_token"] == "synthetic-old-refresh"
        requests.append(form["client_id"])
        response = {"access_token": "synthetic-new-access", "expires_in": 3600}
        if rotated:
            response["refresh_token"] = "synthetic-new-refresh"
        return response
    monkeypatch.setattr(kick_api, "_token_request", fake_request)
    client = kick_api.KickClient("synthetic-public-app", kick_api.load_tokens(), object())
    assert asyncio.run(client._ensure_token()) == "synthetic-new-access"
    reloaded = kick_api.load_tokens()
    assert reloaded.access_token == "synthetic-new-access"
    assert reloaded.refresh_token == ("synthetic-new-refresh" if rotated else "synthetic-old-refresh")
    assert reloaded.expires_at > time.time()
    assert requests == ["synthetic-public-app"]
    backups = list(canonical.glob("credentials.dpapi.bak-*"))
    assert any(path.read_bytes() == initial for path in backups)
    assert storage.get_client_secret() == secret


def test_cli_migrates_helper_data_into_same_native_store(native_store, monkeypatch, capsys):
    from kicktrans import __main__ as cli
    _, legacy = native_store
    legacy.mkdir(parents=True)
    response = {
        "access_token": "synthetic-helper-access", "refresh_token": "synthetic-helper-refresh",
        "expires_in": 3600, "obtained_at": int(time.time()),
        "scope": "user:read channel:read chat:write events:subscribe",
    }
    old_token = storage.dpapi_protect(json.dumps(response).encode())
    old_secret = storage.dpapi_protect(b"synthetic-helper-secret")
    storage.LEGACY_TOKENS_PATH.write_bytes(old_token)
    storage.LEGACY_CLIENT_SECRET_PATH.write_bytes(old_secret)
    monkeypatch.setattr(sys, "argv", ["kicktrans", "migrate-auth", "--source", "helper"])
    cli.main()
    assert kick_api.load_tokens().access_token == "synthetic-helper-access"
    assert storage.get_client_secret() == "synthetic-helper-secret"
    assert storage.LEGACY_TOKENS_PATH.read_bytes() == old_token
    assert storage.LEGACY_CLIENT_SECRET_PATH.read_bytes() == old_secret
    output = capsys.readouterr()
    assert "synthetic-helper" not in output.out + output.err
    assert b"synthetic-helper" not in storage.STORE_PATH.read_bytes()


@pytest.mark.parametrize("scenario", ["success", "denied", "exchange_failure"])
def test_helper_commits_pair_only_after_success(native_store, monkeypatch, unused_tcp_port, scenario):
    helper = load_helper()
    storage.save_credentials(
        {"access_token": "synthetic-current-access", "refresh_token": "synthetic-current-refresh", "expires_at": 1900000000},
        "synthetic-current-secret",
    )
    before = storage.STORE_PATH.read_bytes()
    cfg = Config(client_id="synthetic-public-app", redirect_uri=f"http://127.0.0.1:{unused_tcp_port}/callback")
    monkeypatch.setattr(helper.Config, "load", lambda: cfg)
    monkeypatch.setattr(__import__("getpass"), "getpass", lambda *args: "synthetic-next-secret")
    monkeypatch.setattr(helper, "TIMEOUT", 2)
    statuses = []
    monkeypatch.setattr(helper, "status", lambda **kw: statuses.append(kw))

    def authorize_locally(url):
        parsed = urllib.parse.urlparse(url)
        assert parsed.netloc == "id.kick.com"
        params = urllib.parse.parse_qs(parsed.query)
        callback = {"state": params["state"][0]}
        callback["error" if scenario == "denied" else "code"] = "access_denied" if scenario == "denied" else "synthetic-code"
        connection = http.client.HTTPConnection("127.0.0.1", unused_tcp_port, timeout=3)
        try:
            connection.request("GET", "/callback?" + urllib.parse.urlencode(callback))
            response = connection.getresponse()
            response.read()
            assert response.status == (400 if scenario == "denied" else 200)
        finally:
            connection.close()
        return True

    def token_exchange(request, timeout):
        assert request.full_url == "https://id.kick.com/oauth/token"
        form = urllib.parse.parse_qs(request.data.decode())
        assert form["client_secret"] == ["synthetic-next-secret"]
        if scenario == "exchange_failure":
            raise urllib.error.HTTPError(request.full_url, 403, "denied", {"Content-Type": "application/json"},
                                         io.BytesIO(b'{"error_description":"synthetic-sensitive-provider-body"}'))
        return io.BytesIO(json.dumps({"access_token": "synthetic-next-access", "refresh_token": "synthetic-next-refresh", "expires_in": 3600}).encode())

    monkeypatch.setattr(helper.webbrowser, "open", authorize_locally)
    monkeypatch.setattr(helper.urllib.request, "urlopen", token_exchange)
    result = helper.main(["--secret", "prompt"])
    if scenario == "success":
        assert result == 0
        assert kick_api.load_tokens().access_token == "synthetic-next-access"
        assert storage.get_client_secret() == "synthetic-next-secret"
    else:
        assert result == (5 if scenario == "denied" else 6)
        assert storage.STORE_PATH.read_bytes() == before
        assert kick_api.load_tokens().access_token == "synthetic-current-access"
        assert storage.get_client_secret() == "synthetic-current-secret"
    public_status = json.dumps(statuses)
    assert "synthetic-next-secret" not in public_status
    assert "synthetic-sensitive-provider-body" not in public_status


def test_helper_existing_secret_is_staged_without_changing_store(native_store, monkeypatch, tmp_path):
    helper = load_helper()
    storage.save_client_secret("synthetic-current-secret")
    before = storage.STORE_PATH.read_bytes()
    shared = tmp_path / "shared-config.json"
    shared.write_text(json.dumps({"client_id": "synthetic-app", "client_secret": "synthetic-new-shared-secret"}), encoding="utf-8")
    monkeypatch.setattr(helper, "CONFIG", shared)
    assert helper.get_secret(Config(client_id="synthetic-app"), "existing") == "synthetic-new-shared-secret"
    assert storage.STORE_PATH.read_bytes() == before
    with pytest.raises(ValueError):
        helper.get_secret(Config(client_id="different-app"), "existing")
    assert storage.STORE_PATH.read_bytes() == before


def test_concurrent_migrations_cannot_mix_auth_identities(native_store, tmp_path):
    import os
    import subprocess

    canonical, _ = native_store
    gate = tmp_path / "migration-gate"
    env = os.environ.copy()
    env["SCRIPTS_STATE"] = str(canonical.parents[1])
    code = r"""
import json,sys,time,types
from pathlib import Path
from kicktrans import storage
ready,gate,identity=Path(sys.argv[1]),Path(sys.argv[2]),sys.argv[3]
tokens={'access_token':identity+'-access','refresh_token':identity+'-refresh','expires_at':1900000000}
sys.modules['keyring']=types.SimpleNamespace(get_password=lambda service,key:json.dumps(tokens) if key=='tokens' else identity+'-secret')
ready.write_text('ready')
deadline=time.monotonic()+5
while not gate.exists():
    if time.monotonic()>deadline: raise RuntimeError('gate timeout')
    time.sleep(.01)
try:
    storage.migrate_legacy('keyring')
    print('created')
except FileExistsError:
    print('exists')
"""
    processes = []
    ready_paths = []
    try:
        for identity in ("synthetic-one", "synthetic-two"):
            ready = tmp_path / (identity + ".ready")
            ready_paths.append(ready)
            processes.append(subprocess.Popen(
                [sys.executable, "-c", code, str(ready), str(gate), identity],
                cwd=Path(__file__).resolve().parents[1], env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            ))
        deadline = time.monotonic() + 5
        while not all(path.exists() for path in ready_paths):
            assert time.monotonic() < deadline, "migration workers did not become ready"
            time.sleep(0.01)
        gate.touch()
        outputs = []
        for process in processes:
            out, err = process.communicate(timeout=10)
            assert process.returncode == 0, err
            outputs.append(out.strip())
        assert sorted(outputs) == ["created", "exists"]
        tokens = storage.load_token_data()
        identity = tokens["access_token"].removesuffix("-access")
        assert tokens["refresh_token"] == identity + "-refresh"
        assert storage.get_client_secret() == identity + "-secret"
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)

@pytest.mark.parametrize("change", ["login_before_refresh", "login_during_refresh", "secret_during_refresh"])
def test_refresh_cannot_overwrite_a_newer_auth_snapshot(native_store, monkeypatch, change):
    old = {"access_token": "synthetic-account-a", "refresh_token": "synthetic-refresh-a", "expires_at": 1}
    newer = {"access_token": "synthetic-account-b", "refresh_token": "synthetic-refresh-b", "expires_at": 1900000000}
    storage.save_credentials(old, "synthetic-secret-a")
    client = kick_api.KickClient("synthetic-app", kick_api.load_tokens(), object())
    expected = []
    requests = []
    if change == "login_before_refresh":
        storage.save_credentials(newer, "synthetic-secret-b")
        expected.append(storage.STORE_PATH.read_bytes())

    async def delayed_response(session, form):
        requests.append(form["refresh_token"])
        if change == "secret_during_refresh":
            storage.save_client_secret("synthetic-new-secret")
        else:
            storage.save_credentials(newer, "synthetic-secret-b")
        expected.append(storage.STORE_PATH.read_bytes())
        return {"access_token": "synthetic-stale-response-a", "refresh_token": "synthetic-stale-refresh-a", "expires_in": 3600}

    monkeypatch.setattr(kick_api, "_token_request", delayed_response)
    with pytest.raises(RuntimeError):
        asyncio.run(client._ensure_token())
    assert storage.STORE_PATH.read_bytes() == expected[0]
    assert client.tokens.access_token == "synthetic-account-a"
    if change == "login_before_refresh":
        assert requests == []
    else:
        assert requests == ["synthetic-refresh-a"]
    if change == "secret_during_refresh":
        assert storage.get_client_secret() == "synthetic-new-secret"
        assert kick_api.load_tokens().access_token == "synthetic-account-a"
    else:
        assert storage.get_client_secret() == "synthetic-secret-b"
        assert kick_api.load_tokens().access_token == "synthetic-account-b"