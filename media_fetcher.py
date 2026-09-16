from __future__ import annotations

from urllib.parse import urljoin
import time

import requests
from bs4 import BeautifulSoup
from config import load_config


HEADERS = {
    "User-Agent": "dot-news-bot/1.0 (+https://t.me/)",
}
MAX_HTML_BYTES = 2 * 1024 * 1024


def fetch_og_image(article_url: str, timeout: int = 10) -> str | None:
    retries = max(load_config().http_retry_count, 1)
    response = None
    for attempt in range(retries):
        try:
            response = requests.get(article_url, headers=HEADERS, timeout=timeout, stream=True)
            response.raise_for_status()
            break
        except requests.RequestException:
            if attempt + 1 < retries:
                time.sleep((2, 5, 10)[min(attempt, 2)])
    if response is None:
        return None

    try:
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            remaining = MAX_HTML_BYTES - total
            if remaining <= 0:
                break
            chunks.append(chunk[:remaining])
            total += len(chunks[-1])
        html = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
    finally:
        response.close()
    soup = BeautifulSoup(html, "html.parser")
    selectors = [
        ("property", "og:image"),
        ("name", "twitter:image"),
        ("property", "twitter:image"),
    ]
    for attr, value in selectors:
        tag = soup.find("meta", attrs={attr: value})
        if not tag:
            continue
        image_url = tag.get("content")
        if image_url:
            return urljoin(article_url, image_url.strip())
    return None

