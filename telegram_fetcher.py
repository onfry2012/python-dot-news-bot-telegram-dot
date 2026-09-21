"""Optional collector for public Telegram channels via a separate user account.

The collector is deliberately disabled until TELEGRAM_SOURCES_ENABLED=true and
the account has been authorized. It reads text/captions only; media downloads
remain in the existing article-media pipeline.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
from typing import Any

from config import Config
from database import Database
from news_fetcher import NewsItem

logger = logging.getLogger(__name__)


def load_telegram_sources(path: str) -> list[dict[str, Any]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("telegram_sources.json must contain a list")
    return [item for item in data if isinstance(item, dict) and item.get("enabled", True) and item.get("username")]


def _session(config: Config) -> str:
    # StringSession is convenient for Render; a file session is useful locally.
    return config.telegram_user_session_string or config.telegram_user_session_path


async def fetch_telegram_news(config: Config, db: Database) -> list[NewsItem]:
    """Fetch only unseen, recent messages from configured public channels."""
    if not config.telegram_sources_enabled:
        return []
    if not config.telegram_user_api_id or not config.telegram_user_api_hash:
        logger.warning("Telegram channel collector disabled: API credentials are not configured")
        return []

    try:
        from telethon import TelegramClient
    except ImportError:
        logger.error("Telegram channel collector unavailable: install Telethon")
        return []

    try:
        sources = load_telegram_sources(config.telegram_sources_path)
    except Exception:
        logger.exception("Could not load Telegram channel sources")
        return []
    if not sources:
        return []

    client = TelegramClient(_session(config), config.telegram_user_api_id, config.telegram_user_api_hash)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            logger.warning("Telegram channel collector is not authorized; run authorize_telegram_sources.py once")
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max(config.max_news_age_hours, 1))
        result: list[NewsItem] = []
        for source in sources:
            username = str(source["username"]).strip()
            entity_name = username.removeprefix("@")
            source_key = f"telegram_source_last_id:{entity_name.casefold()}"
            last_id = int(db.get_setting(source_key, "0") or 0)
            max_seen_id = last_id
            try:
                messages = await client.get_messages(entity_name, limit=(config.telegram_initial_messages_per_source if not last_id else config.telegram_fetch_limit_per_source))
                for message in reversed(messages or []):
                    message_id = int(getattr(message, "id", 0) or 0)
                    max_seen_id = max(max_seen_id, message_id)
                    message_date = getattr(message, "date", None)
                    if message_id <= last_id or not message_date or message_date < cutoff:
                        continue
                    text = (getattr(message, "message", "") or "").strip()
                    if len(text) < 40:
                        continue
                    link = f"https://t.me/{entity_name}/{message_id}"
                    result.append(NewsItem(
                        source_name=str(source.get("name") or f"Telegram @{entity_name}"),
                        category=str(source.get("category") or "news"),
                        title=text.splitlines()[0][:300],
                        summary=text[:8000],
                        link=link,
                        media_url=None,
                        source_weight=float(source.get("weight", 1.0)),
                    ))
                if max_seen_id > last_id:
                    db.set_setting(source_key, str(max_seen_id))
            except Exception as exc:
                logger.warning("Could not read Telegram source %s: %s", entity_name, exc)
        logger.info("Telegram channel collector: sources=%s new_items=%s", len(sources), len(result))
        return result
    finally:
        await client.disconnect()
