from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import threading
from typing import Any, Callable

import requests

from tiktok_oauth import load_tokens, refresh_access_token


logger = logging.getLogger(__name__)
CREATOR_INFO_URL = "https://open.tiktokapis.com/v2/post/publish/creator_info/query/"
CONTENT_INIT_URL = "https://open.tiktokapis.com/v2/post/publish/content/init/"
STATUS_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"
_CREATOR_CACHE_TTL_SECONDS = 60
_creator_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_history_lock = threading.Lock()

COMPLETED_STATUSES = {"PUBLISHED", "SUCCESS", "PUBLISH_COMPLETE"}
PROCESSING_STATUSES = {
    "PROCESSING_DOWNLOAD",
    "PROCESSING",
    "IN_PROGRESS",
    "INITIATED",
    "UPLOAD_COMPLETE",
}


def normalize_publish_status(status: str) -> str:
    """Map TikTok API status values to the statuses used by the dashboard."""
    value = str(status or "UNKNOWN").upper()
    if value in COMPLETED_STATUSES:
        return "PUBLISHED"
    if value in PROCESSING_STATUSES:
        return "PROCESSING"
    return "FAILED" if value not in {"UNKNOWN", ""} else "UNKNOWN"


ERROR_MESSAGES = {
    "scope_not_authorized": "TikTok не разрешил scope video.publish. Переподключите Sandbox-аккаунт.",
    "url_ownership_unverified": "URL картинки не находится под верифицированным TikTok URL prefix.",
    "privacy_level_option_mismatch": "Выбранный privacy level недоступен для этого аккаунта.",
    "unaudited_client_can_only_post_to_private_accounts": "Sandbox/unaudited-клиент может публиковать только приватные посты.",
    "access_token_invalid": "TikTok access token истёк или недействителен.",
    "rate_limit_exceeded": "TikTok временно ограничил частоту запросов. Повторите позже.",
    "spam_risk_too_many_posts": "TikTok отклонил публикацию из-за ограничения частоты постов.",
    "spam_risk_user_banned_from_posting": "TikTok временно запретил этому аккаунту публикации.",
    "reached_active_user_cap": "Достигнут лимит активных пользователей Sandbox-приложения.",
}


@dataclass
class TikTokAPIError(RuntimeError):
    code: str
    message: str
    http_status: int | None = None

    def __str__(self) -> str:
        friendly = ERROR_MESSAGES.get(self.code)
        if friendly:
            return friendly
        if self.code and self.code != "unknown":
            return f"TikTok API error ({self.code}): {self.message}"
        return f"TikTok API error: {self.message}"


@dataclass(frozen=True)
class TikTokSettings:
    client_key: str
    client_secret: str
    token_path: str
    history_path: str


def _safe_error(response: requests.Response) -> TikTokAPIError:
    code = "unknown"
    message = f"HTTP {response.status_code}"
    try:
        payload = response.json()
        error = payload.get("error") or {}
        if isinstance(error, dict):
            code = str(error.get("code") or code)
            message = str(error.get("message") or message)
    except (ValueError, TypeError):
        pass
    if response.status_code == 401 and code == "unknown":
        code = "access_token_invalid"
    if response.status_code == 429 and code == "unknown":
        code = "rate_limit_exceeded"
    return TikTokAPIError(code, message, response.status_code)


def _json_request(method: str, url: str, access_token: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        response = requests.request(
            method,
            url,
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
    except requests.RequestException as exc:
        raise TikTokAPIError("network_error", "Не удалось связаться с TikTok API") from exc
    if not response.ok:
        raise _safe_error(response)
    try:
        data = response.json()
    except ValueError as exc:
        raise TikTokAPIError("invalid_response", "TikTok API вернул некорректный JSON", response.status_code) from exc
    error = data.get("error") or {}
    if isinstance(error, dict) and error.get("code") not in (None, "", "ok"):
        raise TikTokAPIError(str(error.get("code")), str(error.get("message") or "TikTok API request failed"), response.status_code)
    return data


def _with_token_retry(settings: TikTokSettings, operation: Callable[[str], dict[str, Any]]) -> dict[str, Any]:
    tokens = load_tokens(settings.token_path)
    access_token = tokens.get("access_token")
    if not access_token:
        raise TikTokAPIError("not_connected", "TikTok-аккаунт ещё не подключён")
    try:
        return operation(str(access_token))
    except TikTokAPIError as exc:
        if exc.code != "access_token_invalid":
            raise
        refresh_access_token(settings.client_key, settings.client_secret, settings.token_path)
        refreshed = load_tokens(settings.token_path)
        new_access_token = refreshed.get("access_token")
        if not new_access_token:
            raise TikTokAPIError("access_token_invalid", "Не удалось обновить TikTok access token")
        return operation(str(new_access_token))


def get_creator_info(settings: TikTokSettings, force_refresh: bool = False) -> dict[str, Any]:
    cache_key = str(Path(settings.token_path).resolve())
    now = datetime.now(timezone.utc).timestamp()
    cached = _creator_cache.get(cache_key)
    if cached and not force_refresh and cached[0] > now:
        return cached[1]
    result = _with_token_retry(settings, lambda token: _json_request("POST", CREATOR_INFO_URL, token, {}))
    creator = result.get("data") or {}
    if not isinstance(creator, dict):
        raise TikTokAPIError("invalid_response", "TikTok creator info имеет неожиданный формат")
    _creator_cache[cache_key] = (now + _CREATOR_CACHE_TTL_SECONDS, creator)
    return creator


def available_privacy_levels(creator_info: dict[str, Any]) -> list[str]:
    values = creator_info.get("privacy_level_options") or []
    return [str(value) for value in values if value]


def publish_photo(
    settings: TikTokSettings,
    image_url: str,
    title: str,
    description: str,
    privacy_level: str,
) -> dict[str, Any]:
    image_url = image_url.strip()
    if not image_url:
        raise TikTokAPIError("invalid_image_url", "TIKTOK_TEST_IMAGE_URL не заполнен")
    if not image_url.lower().startswith("https://"):
        raise TikTokAPIError("invalid_image_url", "URL картинки должен начинаться с https://")
    creator_info = get_creator_info(settings, force_refresh=True)
    privacy_options = available_privacy_levels(creator_info)
    if not privacy_options:
        raise TikTokAPIError("privacy_level_option_mismatch", "TikTok не вернул доступные privacy options")
    if privacy_level not in privacy_options:
        raise TikTokAPIError("privacy_level_option_mismatch", "Выбранный privacy level недоступен для этого аккаунта")
    body = {
        "post_info": {
            "title": title[:90],
            "description": description[:4000],
            "privacy_level": privacy_level,
            "disable_comment": False,
            # TikTok chooses recommended music for direct photo posts.
            "auto_add_music": True,
        },
        "source_info": {
            "source": "PULL_FROM_URL",
            "photo_cover_index": 0,
            "photo_images": [image_url],
        },
        "post_mode": "DIRECT_POST",
        "media_type": "PHOTO",
    }
    logger.info("TikTok photo payload prepared: selected_privacy_level=%s", privacy_level)
    result = _with_token_retry(settings, lambda token: _json_request("POST", CONTENT_INIT_URL, token, body))
    data = result.get("data") or {}
    publish_id = data.get("publish_id")
    if not publish_id:
        raise TikTokAPIError("invalid_response", "TikTok не вернул publish_id")
    created_at = datetime.now(timezone.utc).isoformat()
    record_publish(settings.history_path, str(publish_id), created_at, "INITIATED", privacy_level)
    try:
        status_payload = get_post_status(settings, str(publish_id))
        raw_status, fail_reason = extract_status_details(status_payload)
        status = normalize_publish_status(raw_status)
    except TikTokAPIError as exc:
        status = "STATUS_CHECK_FAILED"
        fail_reason = exc.code
        status_payload = {"error_code": exc.code}
        logger.warning("TikTok status check failed after publish init: code=%s", exc.code)
    update_publish_status(settings.history_path, str(publish_id), status, fail_reason)
    return {
        "publish_id": str(publish_id),
        "created_at": created_at,
        "status": status,
        "fail_reason": fail_reason,
        "status_payload": status_payload,
    }


def get_post_status(settings: TikTokSettings, publish_id: str) -> dict[str, Any]:
    if not publish_id:
        raise TikTokAPIError("invalid_publish_id", "publish_id не указан")
    return _with_token_retry(settings, lambda token: _json_request("POST", STATUS_URL, token, {"publish_id": publish_id}))


def extract_status(payload: dict[str, Any]) -> str:
    status, _ = extract_status_details(payload)
    return status


def extract_status_details(payload: dict[str, Any]) -> tuple[str, str]:
    data = payload.get("data") or {}
    if isinstance(data, dict):
        status = str(data.get("status") or data.get("publish_status") or "UNKNOWN")
        fail_reason = str(data.get("fail_reason") or "")
        return status, fail_reason
    return "UNKNOWN", ""


def load_publish_history(path: str) -> list[dict[str, Any]]:
    file_path = Path(path)
    if not file_path.exists():
        return []
    try:
        value = json.loads(file_path.read_text(encoding="utf-8"))
        return value if isinstance(value, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def record_publish(path: str, publish_id: str, created_at: str, status: str, privacy_level: str, fail_reason: str = "") -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with _history_lock:
        history = load_publish_history(path)
        history.insert(0, {
            "publish_id": publish_id,
            "created_at": created_at,
            "status": status,
            "fail_reason": fail_reason,
            "privacy_level": privacy_level,
        })
        file_path.write_text(json.dumps(history[:50], ensure_ascii=False, indent=2), encoding="utf-8")


def update_publish_status(path: str, publish_id: str, status: str, fail_reason: str = "") -> None:
    with _history_lock:
        history = load_publish_history(path)
        for item in history:
            if item.get("publish_id") == publish_id:
                item["status"] = status
                item["fail_reason"] = fail_reason
                break
        Path(path).write_text(json.dumps(history[:50], ensure_ascii=False, indent=2), encoding="utf-8")
