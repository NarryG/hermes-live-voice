# Vendored from TheSmokeDev/hermes-talk (MIT License)
# https://github.com/TheSmokeDev/hermes-talk — Copyright (c) TheSmokeDev contributors
# Bundled with this plugin under the terms of the MIT License; see NOTICE.md.
# Upstream may include newer features; this copy is pinned for self-contained installs.

"""OpenAI Platform auth resolution for the Realtime voice lane.

Resolution order, fail-closed at every step:

1. ``TALK_OPENAI_API_KEY`` — a Talk-scoped configured key. Present but
   blank fails closed: no silent fall-through past an operator's explicit
   configuration.
2. ``OPENAI_API_KEY`` — the shared environment key.
3. The user's Hermes sign-in for ``openai-codex`` (ChatGPT subscription).
   This lane carries NO token: Hermes core makes the signed request
   (``hermes_cli.plugin_provider_requests.credentialed_provider_request``)
   with its own locked refresh, so the plugin never reads, refreshes or
   rewrites a Codex token. Codex refresh tokens are single-use; a second
   refresher outside Hermes's lock would sign the user out.

API keys win by default to preserve the original contract. An explicit
``TALK_PREFER_CODEX_OAUTH=true`` reverses that choice and refuses key fallback.
An API-key token is a bearer credential — never log it.

``auth_diagnostic`` only inspects ``~/.codex/auth.json`` read-only (the Codex
CLI's own login, used by the ``codex app-server`` lane). Nothing here writes it.
"""

from __future__ import annotations

import base64
import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


REALTIME_AUTH_REQUIRED_MESSAGE = (
    "Realtime voice needs a credential: set TALK_OPENAI_API_KEY or "
    "OPENAI_API_KEY, or sign in with `codex login` to use your ChatGPT "
    "subscription"
)

_CODEX_FALLBACK_EXPIRY_S = 60 * 60
_REFRESH_MARGIN_S = 60

SOURCE_CONFIGURED = "configured"
SOURCE_ENV = "env"
SOURCE_CODEX_OAUTH = "codex-oauth"
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


class TalkAuthError(RuntimeError):
    """Raised when no OpenAI Platform credential can be resolved."""


@dataclass(frozen=True, slots=True)
class TalkAuth:
    """A resolved OpenAI Platform bearer credential."""

    token: str
    source: str  # one of SOURCE_CONFIGURED, SOURCE_ENV, SOURCE_CODEX_OAUTH
    detail: str
    expires_at: datetime | None = None


def _read_env(env: Mapping[str, str] | None, key: str) -> str | None:
    source = os.environ if env is None else env
    raw = source.get(key)
    if raw is None:
        return None
    trimmed = raw.strip()
    return trimmed or None


def prefer_codex_oauth(env: Mapping[str, str] | None = None) -> bool:
    """Whether voice is explicitly pinned to the Codex OAuth lane.

    Absence preserves the historical key-first order. Once the operator sets
    the knob, however, a blank or misspelled value is configuration ambiguity
    at a billing boundary and therefore fails closed.
    """

    source = os.environ if env is None else env
    if "TALK_PREFER_CODEX_OAUTH" not in source:
        return False
    raw = str(source.get("TALK_PREFER_CODEX_OAUTH") or "").strip().lower()
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    raise TalkAuthError(
        "TALK_PREFER_CODEX_OAUTH must be true or false; refusing to choose a "
        "possibly metered auth lane"
    )


def _preference_receipt(env: Mapping[str, str]) -> tuple[str, bool | None]:
    """A secret-free preference state for read-only diagnostics."""

    if "TALK_PREFER_CODEX_OAUTH" not in env:
        return "absent", False
    raw = str(env.get("TALK_PREFER_CODEX_OAUTH") or "").strip().lower()
    if raw in _TRUE_VALUES:
        return "enabled", True
    if raw in _FALSE_VALUES:
        return "disabled", False
    return "invalid", None


def _codex_auth_path(codex_home: Path | None = None) -> Path:
    if codex_home is not None:
        return codex_home / "auth.json"
    configured = os.environ.get("CODEX_HOME", "").strip()
    base = Path(configured) if configured else Path.home() / ".codex"
    return base / "auth.json"


def _decode_jwt_payload(token: str) -> tuple[dict | None, bool]:
    """Return a decoded object payload and whether JWT-shaped input was malformed."""

    parts = token.split(".")
    if len(parts) < 2:
        return None, False
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + padding))
    except Exception:  # noqa: BLE001 - any malformed JWT means "no expiry claim"
        return None, True
    if not isinstance(data, dict):
        return None, True
    return data, False


def _decode_jwt_expiry_s(token: str) -> int | None:
    """Read the ``exp`` claim from a JWT without verifying the signature."""

    data, _malformed = _decode_jwt_payload(token)
    if data is None:
        return None
    exp = data.get("exp")
    return exp if isinstance(exp, (int, float)) else None


def _auth_json_uses_chatgpt_tokens(data: dict) -> bool:
    """Mirror OpenClaw ``codexAuthJsonUsesChatGptTokens``."""

    auth_mode = data.get("auth_mode")
    if isinstance(auth_mode, str) and auth_mode.strip():
        return auth_mode.strip().lower() in {"chatgpt", "chatgptauthtokens"}
    return not isinstance(data.get("OPENAI_API_KEY"), str)


@dataclass(slots=True)
class _CodexOauthCredential:
    access: str
    refresh: str
    expires_s: int
    account_id: str | None
    id_token: str | None


def _parse_codex_oauth_credential(
    data: dict, fallback_expiry_s: int
) -> _CodexOauthCredential | None:
    if not _auth_json_uses_chatgpt_tokens(data):
        return None
    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        return None
    access = tokens.get("access_token")
    refresh = tokens.get("refresh_token")
    if not isinstance(access, str) or not access:
        return None
    if not isinstance(refresh, str) or not refresh:
        return None
    _payload, malformed_jwt = _decode_jwt_payload(access)
    if malformed_jwt:
        return None
    expires_s = _decode_jwt_expiry_s(access)
    if expires_s is None:
        expires_s = fallback_expiry_s
    account_id = tokens.get("account_id")
    id_token = tokens.get("id_token")
    return _CodexOauthCredential(
        access=access,
        refresh=refresh,
        expires_s=expires_s,
        account_id=account_id if isinstance(account_id, str) else None,
        id_token=id_token if isinstance(id_token, str) else None,
    )


def _read_codex_auth_json(auth_path: Path) -> dict | None:
    try:
        data = json.loads(auth_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - unreadable auth.json means "no OAuth lane"
        return None
    return data if isinstance(data, dict) else None


def _fallback_expiry_s(auth_path: Path) -> int:
    """Expiry estimate when the access token carries no JWT ``exp``.

    Codex CLI access tokens live about an hour; anchor on the auth.json
    mtime, which every token refresh (CLI or ours) bumps.
    """

    try:
        base = auth_path.stat().st_mtime
    except OSError:
        base = time.time()
    return int(base) + _CODEX_FALLBACK_EXPIRY_S


def _resolve_codex_oauth() -> TalkAuth:
    """The ChatGPT-subscription lane. Carries no token: Hermes signs the request.

    Whether the user is signed in is answered by Hermes at request time
    (``ProviderNotSignedIn``), never by reading or refreshing a token here.
    """

    return TalkAuth(
        token="",
        source=SOURCE_CODEX_OAUTH,
        detail="Hermes sign-in for openai-codex (ChatGPT subscription; Hermes signs the requests)",
    )


def resolve_auth(
    *,
    env: Mapping[str, str] | None = None,
    codex_home: Path | None = None,
) -> TalkAuth:
    """Resolve the Realtime auth lane in fail-closed order.

    Order: ``TALK_OPENAI_API_KEY`` -> ``OPENAI_API_KEY`` -> the Hermes
    ``openai-codex`` sign-in (no token: Hermes signs the request).
    A Talk-scoped key that is present but blank fails closed instead of
    silently falling through to another source.
    """

    del codex_home  # kept for signature compatibility; the token lane is Hermes's
    source = os.environ if env is None else env
    if prefer_codex_oauth(source):
        return _resolve_codex_oauth()

    configured = source.get("TALK_OPENAI_API_KEY")
    if configured is not None:
        trimmed = configured.strip()
        if not trimmed:
            raise TalkAuthError(
                f"{REALTIME_AUTH_REQUIRED_MESSAGE}. TALK_OPENAI_API_KEY is set "
                "but empty — fix or remove it to allow other auth sources."
            )
        return TalkAuth(
            token=trimmed,
            source=SOURCE_CONFIGURED,
            detail="TALK_OPENAI_API_KEY (Talk-scoped key)",
        )

    shared = source.get("OPENAI_API_KEY")
    if shared is not None:
        env_key = shared.strip()
        if not env_key:
            raise TalkAuthError(
                f"{REALTIME_AUTH_REQUIRED_MESSAGE}. OPENAI_API_KEY is set but "
                "empty — fix or remove it to allow Codex OAuth."
            )
        return TalkAuth(
            token=env_key,
            source=SOURCE_ENV,
            detail="OPENAI_API_KEY environment variable",
        )

    return _resolve_codex_oauth()


def auth_diagnostic(
    *,
    env: Mapping[str, str] | None = None,
    codex_home: Path | None = None,
    now_s: float | None = None,
) -> dict:
    """Inspect auth selection without refreshing, writing, or exposing secrets.

    Read-only view of the Codex CLI login (``~/.codex/auth.json``) that the
    ``codex app-server`` lane uses. Nothing in this plugin refreshes or writes
    that file: the Codex CLI and Hermes own their own refresh.
    """

    source = os.environ if env is None else env
    now = time.time() if now_s is None else now_s
    preference, preferred = _preference_receipt(source)

    scoped_raw = source.get("TALK_OPENAI_API_KEY")
    shared_raw = source.get("OPENAI_API_KEY")
    scoped_state = (
        "absent" if scoped_raw is None else ("present" if scoped_raw.strip() else "blank")
    )
    shared_state = (
        "absent" if shared_raw is None else ("present" if shared_raw.strip() else "blank")
    )
    metered_key_present = "present" in {scoped_state, shared_state}

    auth_path = _codex_auth_path(codex_home)
    oauth_state = "missing"
    data = _read_codex_auth_json(auth_path)
    if auth_path.exists() and data is None:
        oauth_state = "invalid"
    elif data is not None:
        credential = _parse_codex_oauth_credential(data, _fallback_expiry_s(auth_path))
        if credential is None:
            oauth_state = "invalid"
        elif credential.expires_s <= now + _REFRESH_MARGIN_S:
            oauth_state = "expired"
        else:
            oauth_state = "valid"

    winning_lane: str | None = None
    blocked_by: str | None = None
    if preferred is None:
        blocked_by = "invalid-preference"
    elif preferred:
        if oauth_state in {"valid", "expired"}:
            winning_lane = SOURCE_CODEX_OAUTH
        else:
            blocked_by = "codex-oauth-unusable"
    elif scoped_state == "blank":
        blocked_by = "blank-talk-key"
    elif scoped_state == "present":
        winning_lane = SOURCE_CONFIGURED
    elif shared_state == "blank":
        blocked_by = "blank-openai-key"
    elif shared_state == "present":
        winning_lane = SOURCE_ENV
    elif oauth_state in {"valid", "expired"}:
        winning_lane = SOURCE_CODEX_OAUTH
    else:
        blocked_by = "no-usable-auth"

    metered_wins = (
        winning_lane in {SOURCE_CONFIGURED, SOURCE_ENV}
        and oauth_state in {"valid", "expired"}
    )
    return {
        "configured": winning_lane is not None,
        "winning_lane": winning_lane,
        "preference": preference,
        "codex_oauth": oauth_state,
        "metered_key_present": metered_key_present,
        "metered_key_wins_over_codex": metered_wins,
        "metered_keys_ignored": bool(preferred and metered_key_present),
        "refresh_required": winning_lane == SOURCE_CODEX_OAUTH and oauth_state == "expired",
        "blocked_by": blocked_by,
    }


def auth_status(
    *,
    env: Mapping[str, str] | None = None,
    codex_home: Path | None = None,
) -> dict:
    """Report which auth source would be used, without exposing any token."""

    receipt = auth_diagnostic(env=env, codex_home=codex_home)
    lane = receipt["winning_lane"]
    if lane == SOURCE_CONFIGURED:
        detail = "TALK_OPENAI_API_KEY (Talk-scoped key)"
    elif lane == SOURCE_ENV:
        detail = "OPENAI_API_KEY environment variable"
    elif lane == SOURCE_CODEX_OAUTH:
        detail = (
            "Codex CLI login (ChatGPT subscription), token "
            + ("expired (the Codex CLI renews it on use)" if receipt["refresh_required"] else "valid")
        )
    elif receipt["blocked_by"] == "invalid-preference":
        detail = "TALK_PREFER_CODEX_OAUTH is invalid; set it to true or false"
    elif receipt["blocked_by"] == "blank-talk-key":
        detail = "TALK_OPENAI_API_KEY is set but empty; fix or unset it"
    elif receipt["blocked_by"] == "blank-openai-key":
        detail = "OPENAI_API_KEY is set but empty; fix or unset it"
    elif receipt["blocked_by"] == "codex-oauth-unusable":
        detail = "Codex OAuth is required but unusable; sign in to openai-codex in Hermes"
    else:
        detail = REALTIME_AUTH_REQUIRED_MESSAGE
    return {"configured": lane is not None, "source": lane, "detail": detail}


__all__ = [
    "REALTIME_AUTH_REQUIRED_MESSAGE",
    "SOURCE_CODEX_OAUTH",
    "SOURCE_CONFIGURED",
    "SOURCE_ENV",
    "TalkAuth",
    "TalkAuthError",
    "auth_diagnostic",
    "auth_status",
    "prefer_codex_oauth",
    "resolve_auth",
]
