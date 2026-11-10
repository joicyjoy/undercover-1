"""The Shoppie backend's API for the Shoppie Agent: one function for each
operation it uses."""

import contextlib
import os
from typing import Any

import httpx

DEFAULT_BACKEND_URL = "http://127.0.0.1:8000"
API_KEY_HEADER = "X-API-Key"
MISSING_KEY = "UNDERCOVER_API_KEY is missing. Add it to .env"

backend: httpx.Client | None = None
_CLIENT_STATE: dict[str, Any] = {"client": None, "url": None}


class Refused(Exception):
    """The backend refused a request, with its status code and reason."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"{status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail

    @property
    def answer(self) -> str:
        """The refusal as a model is answered with it."""
        return f"Refused with status {self.status_code}: {self.detail}"


def api_url() -> str:
    """Return the base URL of the Shoppie backend."""
    if url := os.getenv("UNDERCOVER_API"):
        return url.rstrip("/")
    if port := os.getenv("PORT"):
        return f"http://127.0.0.1:{port}"
    return DEFAULT_BACKEND_URL


def get_backend() -> httpx.Client:
    """Return the shared HTTP client for the backend, created lazily on first call."""
    if backend is not None:
        return backend
    url = api_url()
    client = _CLIENT_STATE["client"]
    if client is None or _CLIENT_STATE["url"] != url or client.is_closed:
        client = httpx.Client(
            base_url=url,
            timeout=30,
            transport=httpx.HTTPTransport(retries=3),
        )
        _CLIENT_STATE["client"] = client
        _CLIENT_STATE["url"] = url
    return client


def api_key() -> str:
    """The agent's API key, read as each request is sent."""
    key = os.getenv("UNDERCOVER_API_KEY")
    if not key:
        raise RuntimeError(MISSING_KEY)
    return key


def call(method: str, path: str, **request: Any) -> Any:
    """Send one request to the backend with the agent's key and return its JSON, or
    raise Refused."""
    if not path.startswith("/api"):
        path = f"/api/{path.lstrip('/')}"
    headers = request.pop("headers", {})
    headers[API_KEY_HEADER] = api_key()
    response = get_backend().request(method, path, headers=headers, **request)
    if response.is_error:
        detail = response.text
        with contextlib.suppress(Exception):
            detail = response.json().get("detail", detail)
        raise Refused(response.status_code, str(detail))
    return response.json()


def get_request_context(request_id: str) -> dict[str, Any]:
    return call("GET", f"/requests/{request_id}/context")


def calculate_refund(
    request_id: str, reason: str, lines: list[dict[str, Any]]
) -> dict[str, Any]:
    body = {"reason": reason, "lines": lines}
    return call("POST", f"/requests/{request_id}/refund", json=body)


def save_recommendation(
    request_id: str,
    recommendation: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    body = {**(recommendation or {}), **extra}
    return call("POST", f"/requests/{request_id}/recommendation", json=body)


def complete_request(
    request_id: str,
    outcome: str,
    reviewed_by: str,
    notes: str = "",
    reviewed_at: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    body = {
        "outcome": outcome,
        "reviewed_by": reviewed_by,
        "notes": notes,
        "reviewed_at": reviewed_at,
        **extra,
    }
    return call("POST", f"/requests/{request_id}/complete", json=body)


def hold_payment(request_id: str, reason: str) -> dict[str, Any]:
    return call("POST", f"/requests/{request_id}/hold", json={"reason": reason})


def list_policy_sections() -> list[dict[str, Any]]:
    return call("GET", "/policies")


def read_policy_section(request_id: str, policy: str, section: str) -> dict[str, Any]:
    return call("GET", f"/requests/{request_id}/policies/{policy}/sections/{section}")


def start_request(request_id: str) -> dict[str, Any]:
    return call("POST", f"/requests/{request_id}/start")


def add_request_step(
    request_id: str,
    stage: str,
    message: str | None = None,
    sources: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"stage": stage}
    if message is not None:
        body["message"] = message
    if sources is not None:
        body["sources"] = sources
    return call("POST", f"/requests/{request_id}/steps", json=body)


record_step = add_request_step


def record_failure(request_id: str) -> dict[str, Any]:
    return call("POST", f"/requests/{request_id}/failure")


def get_request(request_id: str) -> dict[str, Any]:
    return call("GET", f"/requests/{request_id}")
