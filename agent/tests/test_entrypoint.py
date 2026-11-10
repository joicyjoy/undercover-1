import ast
import io
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from starlette.testclient import TestClient

from agent import api as agent_api
from agent import entrypoint, main
from agent import instructions as agent_instructions
from agent import models as agent_models
from backend import instructions as backend_instructions
from backend import models as backend_models
from backend import refunds as backend_refunds

REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_KEY = "test-agent-secret-key-0123456789abcdef"


def _module_imports(root: Path) -> set[str]:
    names: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split(".")[0])
    return names


def test_agent_runtime_and_backend_never_cross_import():
    agent_imports = _module_imports(REPO_ROOT / "agent")
    runtime_imports = _module_imports(REPO_ROOT / "runtime")
    backend_imports = _module_imports(REPO_ROOT / "backend")

    assert "backend" not in agent_imports
    assert "backend" not in runtime_imports
    assert "agent" not in backend_imports
    assert "runtime" not in backend_imports


def test_agent_instructions_and_models_match_backend():
    assert agent_instructions.REQUEST_INSTRUCTIONS == (
        backend_instructions.REQUEST_INSTRUCTIONS
    )
    assert agent_instructions.QUEUE_INSTRUCTIONS == (
        backend_instructions.QUEUE_INSTRUCTIONS
    )
    assert set(agent_models.Recommendation.model_fields) == set(
        backend_models.Recommendation.model_fields
    )
    assert set(agent_models.Line.model_fields) == set(backend_refunds.Line.model_fields)


def test_importing_runtime_has_no_import_time_side_effects(tmp_path):
    script = (
        "import os, sys\n"
        "os.environ.pop('UNDERCOVER_API_KEY', None)\n"
        "os.environ.pop('DATABASE_URL', None)\n"
        "import runtime.main\n"
        "assert 'backend' not in sys.modules\n"
    )
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("UNDERCOVER_")
        and not k.startswith("AGENTCORE_CREDENTIAL_")
        and k != "DATABASE_URL"
    }
    env["PYTHONPATH"] = str(REPO_ROOT)
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


def test_agent_api_creates_http_client_lazily_on_call(monkeypatch):
    monkeypatch.setattr(agent_api, "backend", None)
    monkeypatch.setitem(agent_api._CLIENT_STATE, "client", None)
    monkeypatch.setitem(agent_api._CLIENT_STATE, "url", None)
    monkeypatch.setenv("UNDERCOVER_API", "http://127.0.0.1:8000")
    monkeypatch.delenv("PORT", raising=False)
    created = agent_api.get_backend()
    assert str(created.base_url).rstrip("/") == "http://127.0.0.1:8000"
    assert agent_api.get_backend() is created
    created.close()


def test_postgres_checkpointer_opens_pool_without_prepared_statements(monkeypatch):
    pool_calls: list[tuple[str, dict[str, Any]]] = []
    executed: list[str] = []
    setup_called: list[bool] = []

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def execute(self, sql: str) -> None:
            executed.append(sql)

    class FakePool:
        closed = False

        def __init__(self, conninfo: str, **kwargs: Any) -> None:
            pool_calls.append((conninfo, kwargs))

        def connection(self) -> FakeConn:
            return FakeConn()

    class FakeSaver:
        def __init__(self, pool: FakePool) -> None:
            self.conn = pool

        def setup(self) -> None:
            setup_called.append(True)

    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@pooler:6543/postgres")
    monkeypatch.delenv("SHOPPIE_DATABASE_PATH", raising=False)
    monkeypatch.setattr(main, "ConnectionPool", FakePool)
    monkeypatch.setattr(main, "PostgresSaver", FakeSaver)
    main._CHECKPOINTER_CACHE.clear()
    try:
        saver = main.get_checkpointer()
    finally:
        main._CHECKPOINTER_CACHE.clear()

    assert isinstance(saver, FakeSaver)
    assert setup_called == [True]
    assert executed == ["CREATE SCHEMA IF NOT EXISTS undercover"]
    conninfo, kwargs = pool_calls[0]
    assert conninfo == "postgresql://user:pass@pooler:6543/postgres"
    assert kwargs["open"] is True
    assert kwargs["kwargs"]["prepare_threshold"] is None
    assert kwargs["kwargs"]["options"] == "-c search_path=undercover"


def test_entrypoint_auth_payload_validation_and_retry_readiness(monkeypatch):
    monkeypatch.delenv("UNDERCOVER_API_KEY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with TestClient(entrypoint.app, raise_server_exceptions=False) as client:
        missing_key = client.post("/invocations", json={"operation": "health"})
        assert missing_key.status_code == 500
        assert "UNDERCOVER_API_KEY" in missing_key.json()["error"]

        monkeypatch.setenv("UNDERCOVER_API_KEY", TEST_KEY)
        health = client.post("/invocations", json={"operation": "health"})
        assert health.status_code == 200
        assert health.json() == {"healthy": True}

        no_auth = client.post(
            "/invocations",
            json={"operation": "work_run", "run_id": "RUN-1", "cap": 1},
        )
        wrong_auth = client.post(
            "/invocations",
            json={"operation": "work_run", "run_id": "RUN-1", "cap": 1},
            headers={"Authorization": "Bearer wrong-key"},
        )
        assert no_auth.status_code == 401
        assert wrong_auth.status_code == 401

        auth = {"Authorization": f"Bearer {TEST_KEY}"}
        assert (
            client.post("/invocations", json=["bad"], headers=auth).status_code == 400
        )
        assert (
            client.post(
                "/invocations", json={"operation": "unknown"}, headers=auth
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/invocations",
                json={"operation": "work_run", "run_id": "RUN-1"},
                headers=auth,
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/invocations",
                json={"operation": "resume_request"},
                headers=auth,
            ).status_code
            == 400
        )


def test_entrypoint_on_runtime_fetches_secrets_once_and_reseeds_random(monkeypatch):
    monkeypatch.setenv("AGENTCORE_CREDENTIAL_UNDERCOVER_API_KEY_NAME", "key-provider")
    monkeypatch.setenv("AGENTCORE_CREDENTIAL_OPENROUTER_API_KEY_NAME", "or-provider")
    monkeypatch.delenv("UNDERCOVER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    reseeded: list[bool] = []
    fetched: list[str] = []
    fail_first = True

    monkeypatch.setattr(entrypoint.random, "seed", lambda: reseeded.append(True))

    def fake_requires_api_key(*, provider_name: str, into: str = "api_key"):
        def decorator(fn):
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                nonlocal fail_first
                fetched.append(provider_name)
                if fail_first and provider_name == "or-provider":
                    fail_first = False
                    raise RuntimeError("transient identity outage")
                kwargs[into] = (
                    TEST_KEY if provider_name == "key-provider" else "sk-or-test"
                )
                return fn(*args, **kwargs)

            return wrapper

        return decorator

    monkeypatch.setattr(entrypoint, "requires_api_key", fake_requires_api_key)

    with TestClient(entrypoint.app, raise_server_exceptions=False) as client:
        first = client.post("/invocations", json={"operation": "health"})
        assert first.status_code == 500

        second = client.post("/invocations", json={"operation": "health"})
        assert second.status_code == 200
        assert second.json() == {"healthy": True}
        assert len(reseeded) == 2

        fetched.clear()
        third = client.post("/invocations", json={"operation": "health"})
        assert third.status_code == 200
        assert fetched == []

        worked: list[tuple[str, int]] = []
        done = threading.Event()

        def record(run_id: str, cap: int) -> None:
            worked.append((run_id, cap))
            done.set()

        monkeypatch.setattr(entrypoint.queue, "work_run", record)
        no_bearer_on_runtime = client.post(
            "/invocations",
            json={"operation": "work_run", "run_id": "RUN-RT", "cap": 3},
        )
        assert no_bearer_on_runtime.status_code == 200
        assert no_bearer_on_runtime.json() == {"accepted": True}
        assert done.wait(timeout=5)
        assert worked == [("RUN-RT", 3)]
        assert os.environ["OPENROUTER_API_KEY"] == "sk-or-test"


def test_entrypoint_ping_tracks_running_and_failed_async_tasks(monkeypatch):
    monkeypatch.setenv("UNDERCOVER_API_KEY", TEST_KEY)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    auth = {"Authorization": f"Bearer {TEST_KEY}"}

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def slow_work(run_id: str, cap: int) -> None:
        started.set()
        assert release.wait(timeout=5)
        finished.set()
        if run_id == "RUN-FAIL":
            raise RuntimeError("boom")

    monkeypatch.setattr(entrypoint.queue, "work_run", slow_work)

    with TestClient(entrypoint.app) as client:
        assert client.get("/ping").json()["status"] == "Healthy"

        ok = client.post(
            "/invocations",
            json={"operation": "work_run", "run_id": "RUN-OK", "cap": 1},
            headers=auth,
        )
        assert ok.json() == {"accepted": True}
        assert started.wait(timeout=5)
        assert client.get("/ping").json()["status"] == "HealthyBusy"

        release.set()
        assert finished.wait(timeout=5)
        # Allow complete_async_task in finally block to run after finished.set()
        for _ in range(50):
            if client.get("/ping").json()["status"] == "Healthy":
                break
        assert client.get("/ping").json()["status"] == "Healthy"

        started.clear()
        release.clear()
        finished.clear()
        fail = client.post(
            "/invocations",
            json={"operation": "work_run", "run_id": "RUN-FAIL", "cap": 1},
            headers=auth,
        )
        assert fail.json() == {"accepted": True}
        assert started.wait(timeout=5)
        assert client.get("/ping").json()["status"] == "HealthyBusy"
        release.set()
        assert finished.wait(timeout=5)
        for _ in range(50):
            if client.get("/ping").json()["status"] == "Healthy":
                break
        assert client.get("/ping").json()["status"] == "Healthy"

    # Also smoke-test JSON stream reader compatibility
    assert json.loads(io.BytesIO(b'{"healthy": true}').read()) == {"healthy": True}
