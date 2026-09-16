from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
from pathlib import Path

import feedparser
import requests

from config import load_config


MAX_RSS_BYTES = 1 * 1024 * 1024
RSS_TIMEOUT_SECONDS = 20


@dataclass(frozen=True)
class Source:
    name: str
    url: str
    category: str
    weight: float = 1.0


@dataclass(frozen=True)
class NewsItem:
    source_name: str
    category: str
    title: str
    summary: str
    link: str
    media_url: str | None = None
    source_weight: float = 1.0


def load_sources(path: str) -> list[Source]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [Source(name=item["name"], url=item["url"], category=item.get("category", "news"), weight=float(item.get("weight", 1.0))) for item in data]


def fetch_news(source: Source, limit: int = 5) -> list[NewsItem]:
    response = None
    try:
        response = requests.get(
            source.url,
            headers={"User-Agent": "DOT-News/1.0"},
            timeout=RSS_TIMEOUT_SECONDS,
            stream=True,
        )
        response.raise_for_status()
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_RSS_BYTES:
                raise RuntimeError(f"RSS source too large: {source.name}")
            chunks.append(chunk)
        feed = feedparser.parse(BytesIO(b"".join(chunks)).read())
    except requests.RequestException as exc:
        raise RuntimeError(f"Could not fetch RSS source: {source.name}") from exc
    finally:
        if response is not None:
            response.close()

    if getattr(feed, "bozo", False) and not feed.entries:
        raise RuntimeError(f"Could not parse RSS source: {source.name}")

    items: list[NewsItem] = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=load_config().max_news_age_hours)
    for entry in feed.entries[:limit]:
        link = getattr(entry, "link", "")
        title = getattr(entry, "title", "").strip()
        if not link or not title:
            continue
        published = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
        if published:
            published_at = datetime(*published[:6], tzinfo=timezone.utc)
            if published_at < cutoff:
                continue
        summary = getattr(entry, "summary", "") or getattr(entry, "description", "")
        # A malformed feed can include a full article or embedded page in the
        # description. Keep prompts and the in-memory scan batch bounded.
        summary = str(summary)[:8000]
        media_url = _entry_media_url(entry)
        items.append(
            NewsItem(
                source_name=source.name,
                category=source.category,
                title=title,
                summary=summary,
                link=link,
                media_url=media_url,
                source_weight=source.weight,
            )
        )
    return items


def _entry_media_url(entry: object) -> str | None:
    for key in ("media_content", "media_thumbnail", "enclosures"):
        values = getattr(entry, key, None) or []
        for value in values:
            url = value.get("url") if hasattr(value, "get") else None
            mime = value.get("type", "") if hasattr(value, "get") else ""
            if url and (mime.startswith("image/") or key != "enclosures"):
                return str(url)
    return None

