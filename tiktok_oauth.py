from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import urlencode

import requests


logger = logging.getLogger(__name__)
AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
SCOPES = "user.info.basic,video.publish"
STATE_TTL_SECONDS = 600
_pending: dict[str, tuple[str, float]] = {}
_lock = threading.Lock()


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def create_authorization_url(client_key: str, redirect_uri: str) -> str:
    if not client_key or not redirect_uri:
        raise ValueError("TikTok OAuth is not configured")
    state = secrets.token_urlsafe(32)
    verifier = _base64url(secrets.token_bytes(48))
    # TikTok's desktop/loopback PKCE flow requires the SHA-256 hex digest.
    challenge = hashlib.sha256(verifier.encode("ascii")).hexdigest()
    with _lock:
        now = time.time()
        for old_state, (_, expires_at) in list(_pending.items()):
            if expires_at <= now:
                _pending.pop(old_state, None)
        _pending[state] = (verifier, now + STATE_TTL_SECONDS)
    params = {
        "client_key": client_key,
        "response_type": "code",
        "scope": SCOPES,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def _take_verifier(state: str) -> str | None:
    with _lock:
        record = _pending.pop(state, None)
    if not record or record[1] < time.time():
        return None
    return record[0]


def exchange_code(client_key: str, client_secret: str, redirect_uri: str, code: str, state: str, token_path: str = ".tiktok_tokens.json") -> dict:
    verifier = _take_verifier(state)
    if not verifier:
        raise ValueError("Invalid or expired OAuth state")
    if not client_key or not client_secret:
        raise ValueError("TikTok Sandbox credentials are not configured")
    response = requests.post(
        TOKEN_URL,
        data={
            "client_key": client_key,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        },
        timeout=20,
    )
    if not response.ok:
        raise RuntimeError(f"TikTok token exchange failed with HTTP {response.status_code}")
    data = response.json()
    if not data.get("access_token"):
        logger.warning("TikTok token exchange returned no access token: error=%s", data.get("error", "unknown"))
        raise RuntimeError("TikTok token response did not contain access_token")
    save_tokens(data, token_path)
    return data


def refresh_access_token(client_key: str, client_secret: str, token_path: str) -> dict:
    tokens = load_tokens(token_path)
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise RuntimeError("No TikTok refresh token is stored")
    response = requests.post(
        TOKEN_URL,
        data={
            "client_key": client_key,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=20,
    )
    if not response.ok:
        raise RuntimeError(f"TikTok token refresh failed with HTTP {response.status_code}")
    data = response.json()
    if not data.get("access_token"):
        raise RuntimeError("TikTok refresh response did not contain access_token")
    save_tokens({**tokens, **data}, token_path)
    return data


def save_tokens(tokens: dict, token_path: str | None = None) -> None:
    path = Path(token_path or os.getenv("TIKTOK_TOKEN_PATH", ".tiktok_tokens.json"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tokens, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    logger.info("TikTok OAuth tokens saved locally")


def load_tokens(token_path: str | None = None) -> dict:
    path = Path(token_path or os.getenv("TIKTOK_TOKEN_PATH", ".tiktok_tokens.json"))
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}
