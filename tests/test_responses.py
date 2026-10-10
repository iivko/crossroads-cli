import hashlib
import json

import httpx
import pytest
from typer.testing import CliRunner

from market_cli.client import Client, CLIError, decode_response
from market_cli.main import app


@pytest.fixture
def api(monkeypatch, tmp_path):
    """Exercise the real CLI HTTP client against an in-process HTTP transport."""
    monkeypatch.setenv("MARKET_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("MARKET_TOKEN", "agent-token")
    monkeypatch.delenv("MARKET_API_URL", raising=False)
    monkeypatch.delenv("MARKET_WORKSPACE", raising=False)
    original_client = httpx.Client
    calls = []

    def install(handler):
        def capture(request):
            calls.append(request)
            return handler(request)

        def transport_client(**kwargs):
            return original_client(transport=httpx.MockTransport(capture), **kwargs)

        monkeypatch.setattr(httpx, "Client", transport_client)
        return calls

    return install


def reply(label, payload, status=200):
    return httpx.Response(status, json={label: payload, "status_code": status})


@pytest.mark.parametrize(
    ("method", "path", "label", "payload"),
    [
        ("GET", "/products/p1", "product", {"id": "p1", "status_code": 17, "product": {"id": "inner"}}),
        ("GET", "/products", "products", {"items": [{"id": "p1"}], "limit": 50, "offset": 0}),
        ("GET", "/rules", "rules", {"effective": {"max_total": 700000}}),
        ("PUT", "/rules", "rules", {"version": 2}),
        ("GET", "/requests/r1", "request_detail", {"request": {"id": "r1"}, "negotiations": []}),
        ("POST", "/requests/r1/launch", "request", {"id": "r1", "state": "negotiating"}),
        ("GET", "/requests/r1/compare", "comparison", {"request_id": "r1", "negotiations": []}),
        ("POST", "/negotiations/n1/claim", "claim", {"claim_token": "claim", "expected_version": 2}),
        ("POST", "/negotiations/n1/actions", "action", {"offer_id": "o1", "state": "negotiating"}),
        ("POST", "/negotiations/n1/retry", "negotiation", {"id": "n1", "run": 2}),
        ("GET", "/approvals/n1", "negotiation", {"id": "n1", "approval_url": "http://localhost:3100"}),
        ("GET", "/orders/o1", "order", {"id": "o1", "approvals": []}),
        ("GET", "/agent/instructions", "instructions", {"role": "seller", "product_schema": {}}),
        ("GET", "/agent/inbox", "inbox", {"items": [{"room_id": "n1"}]}),
        ("POST", "/credentials", "credential", {"id": "c1", "token": "secret"}),
        ("DELETE", "/credentials/c1", "credential", {"revoked": True}),
        ("POST", "/products/p1/images", "image", {"id": "i1", "status": "pending"}),
        ("DELETE", "/products/p1/images/i1", "image", {"removed": True}),
        ("GET", "/activity", "activity", {"items": [], "limit": 50, "offset": 0}),
    ],
)
def test_json_envelope_unwrapped_once_without_losing_payload_fields(method, path, label, payload):
    assert decode_response(reply(label, payload), method, path) == payload


@pytest.mark.parametrize(
    ("path", "label"),
    [
        ("/workspaces", "workspaces"),
        ("/credentials", "credentials"),
        ("/categories", "categories"),
        ("/products/p1/history", "product_history"),
        ("/products/p1/images", "images"),
        ("/rules/history", "rule_history"),
        ("/requests/r1/matches", "matches"),
    ],
)
def test_legacy_list_outputs_remain_arrays(path, label):
    items = [{"id": "one"}, {"id": "two"}]
    assert decode_response(reply(label, {"items": items}), "GET", path) == items


@pytest.mark.parametrize(
    "body",
    [
        {"id": "old-unwrapped-product"},
        [],
        None,
        {"product": {}, "status_code": "200"},
        {"product": {}, "status_code": True},
        {"product": {}, "status_code": 201},
        {"product": {}},
        {"product": {}, "status_code": 200, "extra": {}},
        {"product": {}, "other": {}, "status_code": 200},
        {"wrong_label": {}, "status_code": 200},
        {"product": [], "status_code": 200},
        {"error": {"code": "error", "message": "Oops", "details": None}, "status_code": 200},
    ],
)
def test_invalid_envelopes_raise_a_safe_protocol_error(body):
    with pytest.raises(CLIError) as exc:
        decode_response(httpx.Response(200, json=body), "GET", "/products/p1")
    assert exc.value.code == "invalid_response"
    assert exc.value.exit_code == 1
    assert exc.value.details == {"http_status": 200}
    assert "old-unwrapped-product" not in exc.value.message


@pytest.mark.parametrize("payload", [{}, {"items": {}}, {"items": ["bad"]}, {"items": [], "lost": 1}])
def test_malformed_collection_is_rejected(payload):
    with pytest.raises(CLIError) as exc:
        decode_response(reply("images", payload), "GET", "/products/p1/images")
    assert exc.value.code == "invalid_response"


@pytest.mark.parametrize(
    ("status", "exit_code"), [(401, 3), (403, 4), (404, 1), (409, 5), (422, 2), (500, 1)]
)
def test_api_errors_retain_codes_details_and_exit_status(status, exit_code):
    error = {"code": "example_error", "message": "Operation denied", "details": {"fields": ["stock"]}}
    with pytest.raises(CLIError) as exc:
        decode_response(reply("error", error, status), "POST", "/products")
    assert (exc.value.code, exc.value.message, exc.value.details, exc.value.exit_code) == (
        error["code"],
        error["message"],
        error["details"],
        exit_code,
    )


@pytest.mark.parametrize(
    "error",
    [None, [], "error", {}, {"code": "missing_fields"}, {"code": [], "message": "Oops", "details": None}],
)
def test_malformed_error_payload_is_rejected(error):
    with pytest.raises(CLIError) as exc:
        decode_response(reply("error", error, 403), "GET", "/products")
    assert exc.value.code == "invalid_response"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(502, text="<html>private proxy content</html>"),
        httpx.Response(200, content=b"invalid json", headers={"content-type": "application/json"}),
        httpx.Response(204),
        reply("product", {}, 302),
    ],
)
def test_non_json_empty_or_redirect_response_is_rejected(response):
    with pytest.raises(CLIError) as exc:
        decode_response(response, "GET", "/products/p1")
    assert exc.value.code == "invalid_response"
    assert "private proxy content" not in exc.value.message


def test_error_envelope_status_must_match_http():
    with pytest.raises(CLIError) as exc:
        decode_response(
            httpx.Response(
                403,
                json={"error": {"code": "denied", "message": "Denied", "details": None}, "status_code": 401},
            ),
            "GET",
            "/products",
        )
    assert exc.value.code == "invalid_response"


def test_login_consumes_token_and_identity_envelopes_without_printing_secret(api, tmp_path):
    identity = {"user": {"email": "buyer@demo.local"}, "workspace": {"id": "ws-buyer", "role": "buyer"}}

    def handler(request):
        if request.url.path.endswith("/auth/token"):
            assert "authorization" not in request.headers
            assert json.loads(request.content)["password"] == "password"
            return reply("token", {"token": "new-secret"})
        assert request.url.path.endswith("/auth/me")
        assert request.headers["authorization"] == "Bearer new-secret"
        return reply("identity", identity)

    api(handler)
    result = CliRunner().invoke(
        app,
        ["--profile", "buyer", "auth", "login", "--email", "buyer@demo.local", "--password-stdin"],
        input="password\n",
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == identity
    assert "new-secret" not in result.stdout
    assert json.loads((tmp_path / "config.json").read_text())["profiles"]["buyer"]["token"] == "new-secret"


def test_cli_collection_output_stays_flat(api):
    api(lambda request: reply("workspaces", {"items": [{"id": "ws-buyer", "role": "buyer"}]}))
    result = CliRunner().invoke(app, ["workspace", "list", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == [{"id": "ws-buyer", "role": "buyer"}]


def test_action_preserves_payload_idempotency_key_and_flat_result(api, tmp_path):
    data = {"type": "decline", "claim_token": "claim-secret", "expected_version": 3}
    path = tmp_path / "action.json"
    path.write_text(json.dumps(data))

    def handler(request):
        assert request.method == "POST"
        assert request.url.path == "/api/v1/negotiations/n1/actions"
        assert request.headers["idempotency-key"] == "one-action"
        assert json.loads(request.content) == data
        return reply("action", {"state": "declined", "offer_id": None})

    api(handler)
    result = CliRunner().invoke(
        app, ["negotiations", "act", "n1", "--file", str(path), "--idempotency-key", "one-action"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"state": "declined", "offer_id": None}


def test_image_download_uses_enveloped_metadata_and_unwrapped_bytes(api, tmp_path):
    content = b"actual-byte-fixture"
    checksum = hashlib.sha256(content).hexdigest()
    image = {"id": "i1", "status": "ready", "sha256": checksum, "mime": "image/jpeg", "caption": "Front"}

    def handler(request):
        if request.url.path.endswith("/products/p1/images"):
            return reply("images", {"items": [image]})
        assert request.url.path.endswith("/images/i1/content")
        return httpx.Response(200, content=content, headers={"content-type": "image/jpeg"})

    api(handler)
    destination = tmp_path / "photos"
    result = CliRunner().invoke(
        app, ["products", "images", "read", "p1", "--all", "--output-dir", str(destination)]
    )
    assert result.exit_code == 0, result.output
    manifest = json.loads(result.stdout)
    assert (destination / "i1.jpg").read_bytes() == content
    assert manifest["images"][0]["sha256"] == checksum


def test_image_binary_error_still_uses_json_error_envelope(api):
    api(lambda request: reply("error", {"code": "image_private", "message": "Denied", "details": None}, 403))
    with pytest.raises(CLIError) as exc:
        Client("http://127.0.0.1:8100", "token").call("GET", "/images/i1/content", binary=True)
    assert (exc.value.code, exc.value.exit_code) == ("image_private", 4)


def test_image_binary_success_cannot_write_a_json_body():
    with pytest.raises(CLIError) as exc:
        decode_response(reply("image", {"id": "i1"}), "GET", "/images/i1/content", binary=True)
    assert exc.value.code == "invalid_response"


def test_malformed_http_response_prints_a_structured_cli_error(api):
    api(lambda request: httpx.Response(200, json={"product": {"id": "p1"}, "status_code": 201}))
    result = CliRunner().invoke(app, ["products", "show", "p1"])
    assert result.exit_code == 1
    assert not result.stdout
    assert json.loads(result.stderr) == {
        "error": {
            "code": "invalid_response",
            "message": "API returned an invalid response. Check that the CLI and API versions are compatible.",
            "details": {"http_status": 200},
        }
    }
