"""Support handler profiles, Entra ID / dev resolution, and ephemeral tokens."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from dataclasses import asdict, dataclass
from typing import Literal

from fastapi import HTTPException, Request

_PROCESS_SECRET = secrets.token_hex(32)
_HANDLER_NAMESPACE = "shoppie-handler-v1"
DEFAULT_TOKEN_TTL_SECONDS = 1800


@dataclass(frozen=True)
class HandlerProfile:
    """One Shoppie support handler and their sign-off authority."""

    handler_id: str
    name: str
    email: str
    role: str
    can_sign_off_over_limit: bool

    def to_dict(self, auth_mode: Literal["entra", "dev"] = "dev") -> dict[str, object]:
        """Serialize the handler profile for API responses."""
        return {**asdict(self), "auth_mode": auth_mode}


HANDLERS: dict[str, HandlerProfile] = {
    "HND-01": HandlerProfile(
        handler_id="HND-01",
        name="Dana Whitfield",
        email="dana.whitfield@shoppie.example",
        role="Senior Support Lead",
        can_sign_off_over_limit=True,
    ),
    "HND-02": HandlerProfile(
        handler_id="HND-02",
        name="Marcus Oyelaran",
        email="marcus.oyelaran@shoppie.example",
        role="Support Handler",
        can_sign_off_over_limit=False,
    ),
}


def list_dev_handlers() -> list[dict[str, object]]:
    """Return the seeded handlers available for local development role switching."""
    return [profile.to_dict("dev") for profile in HANDLERS.values()]


def _signing_secret() -> bytes:
    secret = (
        os.getenv("SHOPPIE_HANDLER_SECRET")
        or os.getenv("UNDERCOVER_API_KEY")
        or _PROCESS_SECRET
    )
    return secret.encode("utf-8")


def create_handler_token(
    handler: HandlerProfile, ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS
) -> str:
    """Mint a short-lived HMAC-signed bearer token for one handler."""
    expires_at = int(time.time()) + ttl_seconds
    nonce = secrets.token_hex(8)
    body = f"{_HANDLER_NAMESPACE}:{handler.handler_id}:{expires_at}:{nonce}"
    body_b64 = base64.urlsafe_b64encode(body.encode("utf-8")).decode("ascii")
    sig = hmac.new(
        _signing_secret(), body_b64.encode("ascii"), hashlib.sha256
    ).hexdigest()
    return f"{body_b64}.{sig}"


def verify_handler_token(token: str) -> HandlerProfile:
    """Validate an ephemeral handler bearer token and return its handler profile."""
    body_b64, sep, supplied_sig = token.partition(".")
    if not sep or not body_b64 or not supplied_sig:
        raise HTTPException(status_code=401, detail="Invalid handler bearer token")
    expected_sig = hmac.new(
        _signing_secret(), body_b64.encode("ascii"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(supplied_sig, expected_sig):
        raise HTTPException(status_code=401, detail="Invalid handler bearer token")
    try:
        raw = base64.urlsafe_b64decode(body_b64.encode("ascii")).decode("utf-8")
        namespace, handler_id, exp_text, _nonce = raw.split(":", 3)
        expires_at = int(exp_text)
    except Exception as exc:
        raise HTTPException(
            status_code=401, detail="Malformed handler bearer token"
        ) from exc
    if namespace != _HANDLER_NAMESPACE or time.time() > expires_at:
        raise HTTPException(status_code=401, detail="Expired handler bearer token")
    profile = _profile_for_id(handler_id)
    if profile is None:
        raise HTTPException(status_code=401, detail="Unknown handler identity")
    return profile


def _profile_for_id(handler_id: str) -> HandlerProfile | None:
    if handler_id in HANDLERS:
        return HANDLERS[handler_id]
    if handler_id.startswith("entra:"):
        principal_name = handler_id.removeprefix("entra:")
        return _entra_profile(principal_name, principal_name)
    return None


def _entra_profile(principal_id: str, principal_name: str) -> HandlerProfile:
    lowered = f"{principal_id} {principal_name}".casefold()
    if "marcus" in lowered or "oyelaran" in lowered:
        return HANDLERS["HND-02"]
    if "dana" in lowered or "whitfield" in lowered:
        return HANDLERS["HND-01"]
    leads = {
        item.strip().casefold()
        for item in os.getenv("SHOPPIE_LEAD_HANDLERS", "").split(",")
        if item.strip()
    }
    is_lead = principal_id.casefold() in leads or principal_name.casefold() in leads
    display_name = principal_name or principal_id
    return HandlerProfile(
        handler_id=f"entra:{principal_name or principal_id}",
        name=display_name,
        email=principal_name if "@" in principal_name else "",
        role="Senior Support Lead" if is_lead else "Support Handler",
        can_sign_off_over_limit=is_lead,
    )


def resolve_handler(
    request: Request,
) -> tuple[HandlerProfile, Literal["entra", "dev"]]:
    """Identify the support handler from token, Easy Auth, or dev default."""
    if "X-API-Key" in request.headers:
        raise HTTPException(
            status_code=401,
            detail="Agent API key cannot stand in for a support handler",
        )
    auth_header = request.headers.get("Authorization", "")
    if auth_header:
        scheme, _, token = auth_header.partition(" ")
        if scheme.casefold() != "bearer" or not token.strip():
            raise HTTPException(
                status_code=401, detail="Invalid handler authorization header"
            )
        profile = verify_handler_token(token.strip())
        mode: Literal["entra", "dev"] = (
            "entra"
            if (
                request.headers.get("X-MS-CLIENT-PRINCIPAL-ID")
                or request.headers.get("X-MS-CLIENT-PRINCIPAL-NAME")
                or profile.handler_id.startswith("entra:")
            )
            else "dev"
        )
        return profile, mode

    principal_id = (request.headers.get("X-MS-CLIENT-PRINCIPAL-ID") or "").strip()
    principal_name = (request.headers.get("X-MS-CLIENT-PRINCIPAL-NAME") or "").strip()
    if principal_id or principal_name:
        return _entra_profile(principal_id, principal_name), "entra"

    requested_dev = (request.headers.get("X-Shoppie-Handler") or "").strip()
    if requested_dev:
        profile = HANDLERS.get(requested_dev)
        if profile is None:
            raise HTTPException(status_code=400, detail="Unknown dev handler profile")
        return profile, "dev"
    return HANDLERS["HND-01"], "dev"


def check_sign_off_permission(
    handler: HandlerProfile, payout: float | None, approval_limit: float
) -> tuple[bool, str | None]:
    """Check whether a handler can confirm a payout of the given INR amount."""
    if (
        payout is not None
        and float(payout) > approval_limit
        and not handler.can_sign_off_over_limit
    ):
        msg = (
            f"{handler.name} ({handler.role}) cannot sign off payouts "
            f"above ₹{approval_limit:,.2f}."
        )
        return False, msg
    return True, None
