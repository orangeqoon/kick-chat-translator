"""Safe OAuth diagnosis using synthetic response bodies only."""
import importlib.util
import io
import json
from pathlib import Path
import urllib.error

import pytest


def helper_module():
    path = Path(__file__).resolve().parents[1] / "auth" / "kick_oauth_helper.py"
    spec = importlib.util.spec_from_file_location("oauth_diagnostics_helper", path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    return helper


@pytest.mark.parametrize("code", [
    "invalid_client", "invalid_grant", "invalid_request", "unauthorized_client",
    "unsupported_grant_type", "invalid_scope", "access_denied", "server_error",
    "temporarily_unavailable",
])
def test_known_protocol_code_retained_without_provider_description(code):
    helper = helper_module()
    body = json.dumps({"error": code, "error_description": "synthetic-sensitive-description", "access_token": "synthetic-sensitive-access"}).encode()
    error = urllib.error.HTTPError("https://id.kick.com/oauth/token", 401, "unused", {"Content-Type": "application/json"}, io.BytesIO(body))
    result = helper.classify_error(error)
    assert result["http_status"] == 401
    assert result["kind"] == "oauth_json"
    assert result["oauth_error"] == code
    assert "synthetic-sensitive" not in json.dumps(result)


@pytest.mark.parametrize("body", [
    b'{"error":"synthetic-sensitive-secret"}',
    b'{"error":{"secret":"synthetic-sensitive-secret"}}',
    b'["synthetic-sensitive-secret"]',
    b"[" * 1500 + b"0" + b"]" * 1500,
    b'{"error":"invalid_client","error_description":"synthetic-sensitive-truncated',
])
def test_unknown_or_malformed_response_never_retains_body(body):
    helper = helper_module()
    error = urllib.error.HTTPError("https://id.kick.com/oauth/token", 401, "unused", {"Content-Type": "application/json"}, io.BytesIO(body))
    result = helper.classify_error(error)
    assert result["oauth_error"] == "unrecognized"
    assert "synthetic-sensitive" not in json.dumps(result)
    assert "error_description" not in result
