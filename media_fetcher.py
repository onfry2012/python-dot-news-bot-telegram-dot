from __future__ import annotations

from urllib.parse import urljoin
import time

import requests
from bs4 import BeautifulSoup
from config import load_config


HEADERS = {
    "User-Agent": "dot-news-bot/1.0 (+https://t.me/)",
}


def fetch_og_image(article_url: str, timeout: int = 10) -> str | None:
    retries = max(load_config().http_retry_count, 1)
    response = None
    for attempt in range(retries):
        try:
            response = requests.get(article_url, headers=HEADERS, timeout=timeout)
            response.raise_for_status()
            break
        except requests.RequestException:
            if attempt + 1 < retries:
                time.sleep((2, 5, 10)[min(attempt, 2)])
    if response is None:
        return None

    soup = BeautifulSoup(response.text, "html.parser")
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
