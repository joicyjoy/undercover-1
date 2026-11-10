import json
from types import SimpleNamespace
from typing import Any

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import update

from agent import main as agent_main
from backend import main as backend_main
from backend.main import app
from backend.store import Store, support_requests


def test_health(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPPIE_DATABASE_PATH", str(tmp_path / "health.sqlite3"))
    with TestClient(app) as client:
        response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_seed_is_idempotent(tmp_path):
    store = Store(tmp_path / "seed.sqlite3")
    store.initialize()
    store.initialize()
    requests = store.list_requests()
    assert len(requests) == 9
    assert "REQ-7901" in {item["request_id"] for item in requests}


def test_seed_refreshes_request_and_order_records_in_an_existing_database(tmp_path):
    store = Store(tmp_path / "evidence.sqlite3")
    store.initialize()
    with store.engine.begin() as connection:
        connection.execute(
            update(support_requests)
            .where(support_requests.c.request_id == "REQ-7002")
            .values(
                request_json='{"customer_message": "stale", "evidence": []}',
                order_json='{"items": []}',
            )
        )
    store.initialize()

    request = store.get_request("REQ-7002")

    assert request is not None
    assert "cracked handle" in request["request"]["customer_message"]
    references = [item["reference"] for item in request["request"]["evidence"]]
    assert "synthetic://evidence/EV-7002-3" in references
    assert request["order"]["items"][0]["name"] == "Ceramic travel mug, 450 ml"
    assert request["order"]["shipment"]["tracking_number"] == "SHP-2026-10002"


def test_every_request_has_evidence_and_a_fulfillment_timeline(tmp_path):
    store = Store(tmp_path / "timeline.sqlite3")
    store.initialize()

    for summary in store.list_requests():
        request = store.get_request(summary["request_id"])
        assert request is not None
        assert request["request"]["evidence"]
        events = request["order"]["fulfillment_events"]
        assert events[-1]["status"] == request["order"]["fulfillment_status"]


def test_final_runtime_queue_has_no_reference_decision_route(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPPIE_DATABASE_PATH", str(tmp_path / "runtime.sqlite3"))
    with TestClient(app) as client:
        queue = client.get("/api/requests")
        references = client.get("/api/evals/support_decisions.json")

    assert len(queue.json()) == 9
    assert references.status_code == 404
    api_routes = [route for route in app.routes if isinstance(route, APIRoute)]
    assert all("eval" not in route.path.casefold() for route in api_routes)


def test_request_queue_and_safe_detail(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPPIE_DATABASE_PATH", str(tmp_path / "queue.sqlite3"))
    with TestClient(app) as client:
        queue = client.get("/api/requests")
        detail = client.get("/api/requests/REQ-7001")
        missing = client.get("/api/requests/REQ-0000")

    assert queue.status_code == 200
    assert len(queue.json()) == 9
    assert {item["status"] for item in queue.json()} == {"waiting"}
    assert detail.status_code == 200
    request = detail.json()["request"]
    assert request["request_id"] == "REQ-7001"
    assert "reason_code" not in request
    assert "required_action" not in request
    assert "intent_summary" not in request
    assert "phone" not in detail.json()["customer"]
    assert detail.json()["order"]["order_id"] == "ORD-2026-10001"
    assert detail.json()["steps"] == []
    assert "history" not in detail.json()
    assert "action_result" not in detail.json()
    assert missing.status_code == 404


def test_api_routes_have_operation_ids_and_no_query_inputs():
    routes = [route for route in app.routes if isinstance(route, APIRoute)]
    assert routes
    for route in routes:
        assert route.operation_id
        assert route.endpoint.__doc__
        assert "\n" not in route.endpoint.__doc__
        assert route.dependant.query_params == []
    post_paths = {route.path for route in routes if "POST" in (route.methods or set())}
    assert post_paths == {
        "/api/runs",
        "/api/runs/{run_id}/finish",
        "/api/runs/{run_id}/heartbeat",
        "/api/runs/{run_id}/todos/{request_id}",
        "/api/requests/{request_id}/steps",
        "/api/requests/{request_id}/refund",
        "/api/requests/{request_id}/start",
        "/api/requests/{request_id}/recommendation",
        "/api/requests/{request_id}/failure",
        "/api/requests/{request_id}/review",
        "/api/requests/{request_id}/send_back",
        "/api/requests/{request_id}/take_sent_back",
        "/api/requests/{request_id}/heartbeat",
        "/api/requests/{request_id}/complete",
        "/api/requests/{request_id}/hold",
    }


def function_url_event(path: str) -> dict[str, object]:
    """Build a Lambda Function URL (payload v2) GET event for a path."""
    return {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": path,
        "rawQueryString": "",
        "headers": {"host": "shoppie.lambda-url.ap-south-1.on.aws"},
        "requestContext": {
            "accountId": "123456789012",
            "apiId": "shoppie",
            "domainName": "shoppie.lambda-url.ap-south-1.on.aws",
            "domainPrefix": "shoppie",
            "http": {
                "method": "GET",
                "path": path,
                "protocol": "HTTP/1.1",
                "sourceIp": "203.0.113.1",
                "userAgent": "pytest",
            },
            "requestId": "test-request",
            "routeKey": "$default",
            "stage": "$default",
            "time": "09/Nov/2026:12:00:00 +0000",
            "timeEpoch": 1794225600000,
        },
        "isBase64Encoded": False,
    }


def test_lambda_handler_serves_api_and_dashboard_without_lifespan(
    tmp_path, monkeypatch
):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>Shoppie</title>")
    database = tmp_path / "lambda.sqlite3"
    Store(database).initialize()
    monkeypatch.setenv("SHOPPIE_DATABASE_PATH", str(database))
    monkeypatch.setattr(backend_main, "DASHBOARD_DIST", dist)
    if hasattr(app.state, "store"):
        monkeypatch.delattr(app.state, "store")
    lambda_context: Any = SimpleNamespace(
        function_name="shoppie-backend", aws_request_id="test-request"
    )

    health = backend_main.handler(function_url_event("/api/health"), lambda_context)
    root = backend_main.handler(function_url_event("/"), lambda_context)
    page = backend_main.handler(
        function_url_event("/requests/REQ-7001"), lambda_context
    )
    queue = backend_main.handler(function_url_event("/api/requests"), lambda_context)

    assert health["statusCode"] == 200
    assert json.loads(health["body"]) == {"status": "ok"}
    assert root["statusCode"] == 200
    assert "<title>Shoppie</title>" in root["body"]
    assert page["statusCode"] == 200
    assert "<title>Shoppie</title>" in page["body"]
    assert queue["statusCode"] == 200
    assert len(json.loads(queue["body"])) == 9


def test_serves_built_dashboard_and_resolves_backend_url(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>Shoppie</title>")
    (dist / "assets" / "app.js").write_text("console.log('shoppie');")
    monkeypatch.setenv("SHOPPIE_DATABASE_PATH", str(tmp_path / "dist.sqlite3"))
    monkeypatch.setattr(backend_main, "DASHBOARD_DIST", dist)

    with TestClient(app) as client:
        root = client.get("/")
        page = client.get("/requests/REQ-7001")
        asset = client.get("/assets/app.js")
        missing_api = client.get("/api/unknown")

    assert root.status_code == 200
    assert "<title>Shoppie</title>" in root.text
    assert page.status_code == 200
    assert "<title>Shoppie</title>" in page.text
    assert asset.status_code == 200
    assert "shoppie" in asset.text
    assert missing_api.status_code == 404
    assert missing_api.json() == {"detail": "Not Found"}

    monkeypatch.delenv("UNDERCOVER_API", raising=False)
    monkeypatch.delenv("PORT", raising=False)
    assert agent_main.backend_url() == "http://localhost:8000"
    monkeypatch.setenv("PORT", "10000")
    assert agent_main.backend_url() == "http://127.0.0.1:10000"
    monkeypatch.setenv("UNDERCOVER_API", "https://shoppie.onrender.com/")
    assert agent_main.backend_url() == "https://shoppie.onrender.com"


def test_agent_api_key_protects_agent_routes_and_never_stands_in_for_handler(
    tmp_path, monkeypatch
):
    key = "test-agent-secret-key-0123456789abcdef"
    monkeypatch.setenv("SHOPPIE_DATABASE_PATH", str(tmp_path / "auth.sqlite3"))
    monkeypatch.setenv("UNDERCOVER_API_KEY", key)

    with TestClient(app) as client:
        assert client.get("/api/policies").status_code == 401
        assert (
            client.get("/api/policies", headers={"X-API-Key": "wrong"}).status_code
            == 401
        )
        assert (
            client.get("/api/policies", headers={"X-API-Key": key}).status_code == 200
        )

        assert client.get("/api/requests").status_code == 200
        assert (
            client.get("/api/requests", headers={"X-API-Key": key}).status_code == 200
        )
        assert (
            client.get("/api/requests", headers={"X-API-Key": "wrong"}).status_code
            == 401
        )

        assert (
            client.post(
                "/api/requests/REQ-7001/review",
                json={"outcome": "approve", "notes": "", "reviewed_by": "Priya Nair"},
                headers={"X-API-Key": key},
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/api/runs",
                json={"cap": 1},
                headers={"X-API-Key": key},
            ).status_code
            == 401
        )

    monkeypatch.delenv("UNDERCOVER_API_KEY", raising=False)
    with TestClient(app) as no_key_client:
        assert no_key_client.get("/api/health").status_code == 200
        assert no_key_client.get("/api/requests").status_code == 200
        assert (
            no_key_client.get("/api/policies", headers={"X-API-Key": key}).status_code
            == 401
        )


def test_agent_openapi_exposes_only_agent_operations_without_servers_or_key_inputs(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SHOPPIE_DATABASE_PATH", str(tmp_path / "openapi.sqlite3"))
    with TestClient(app) as client:
        response = client.get("/api/agent/openapi.json")

    assert response.status_code == 200
    schema = response.json()
    assert "servers" not in schema
    operation_ids = {
        operation["operationId"]
        for path_item in schema["paths"].values()
        for operation in path_item.values()
        if isinstance(operation, dict) and "operationId" in operation
    }
    assert operation_ids == {
        "get_request_context",
        "calculate_refund",
        "add_request_step",
        "start_request",
        "save_recommendation",
        "complete_request",
    }
    parameters = [
        param["name"]
        for path_item in schema["paths"].values()
        for operation in path_item.values()
        if isinstance(operation, dict)
        for param in operation.get("parameters", [])
    ]
    assert "X-API-Key" not in parameters
    assert "x-api-key" not in {name.lower() for name in parameters}
