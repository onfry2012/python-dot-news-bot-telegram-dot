"""Heavy DOT News worker for a local PC.

The worker never publishes directly and never owns the central SQLite file.
It prepares RSS items locally and submits them to the authenticated Render API.
Run it with the same .env as the project after setting WORKER_HEARTBEAT_SECRET.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import requests

from ai_writer import AIWriter
from config import load_config
from media_fetcher import fetch_og_image
from news_fetcher import fetch_news, load_sources


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s local-worker: %(message)s")
logger = logging.getLogger("dot-news-local-worker")


def main() -> None:
    # The worker never serves the web panel; keep the local worker usable even
    # when the desktop .env intentionally has no panel password.
    os.environ.setdefault("WEB_PASSWORD", "local-worker-only")
    config = load_config()
    if not config.worker_heartbeat_secret:
        raise RuntimeError("WORKER_HEARTBEAT_SECRET is required")
    api = config.worker_api_url
    headers = {"X-Worker-Secret": config.worker_heartbeat_secret}
    writer = AIWriter(config.openai_api_key, config.openai_model, config.openai_retry_count)
    sources = load_sources(config.sources_path)
    state_path = Path(os.getenv("LOCAL_WORKER_STATE_PATH", ".local_worker_state.json"))
    source_offset = _read_offset(state_path)
    interval = max(int(os.getenv("LOCAL_WORKER_INTERVAL_MINUTES", "5")), 1)
    per_source = max(min(int(os.getenv("LOCAL_WORKER_LIMIT_PER_SOURCE", "5")), 20), 1)
    total_limit = max(min(int(os.getenv("LOCAL_WORKER_LIMIT_TOTAL", "20")), 100), 1)
    logger.info("local worker started; api=%s sources=%s interval=%s", api, len(sources), interval)

    while True:
        try:
            _post(api + "/api/worker/heartbeat", headers, {})
            selected = [sources[(source_offset + i) % len(sources)] for i in range(min(10, len(sources)))] if sources else []
            source_offset = (source_offset + len(selected)) % max(len(sources), 1)
            _write_offset(state_path, source_offset)
            processed = 0
            for source in selected:
                if processed >= total_limit:
                    break
                try:
                    items = fetch_news(source, limit=per_source)
                    for item in items:
                        if processed >= total_limit:
                            break
                        _post(api + "/api/worker/heartbeat", headers, {})
                        if _known(api, headers, item.link):
                            continue
                        analysis = writer.analyze(item)
                        rewritten = writer.rewrite(item)
                        image_url = item.media_url or fetch_og_image(item.link)
                        payload = {
                            "source_name": item.source_name,
                            "category": item.category,
                            "title": item.title,
                            "summary": item.summary,
                            "url": item.link,
                            "image_url": image_url,
                            "source_weight": item.source_weight,
                            "rewritten_post": rewritten,
                            "analysis": analysis,
                        }
                        result = _post(api + "/api/worker/submit", headers, payload)
                        processed += 1
                        logger.info("prepared url=%s result=%s", item.link[:100], result.get("result"))
                except Exception:
                    logger.exception("source processing failed: %s", source.name)
            logger.info("cycle complete: prepared=%s; next cycle in %s min", processed, interval)
        except Exception:
            logger.exception("worker cycle failed; retrying after heartbeat interval")
        time.sleep(interval * 60)


def _known(api: str, headers: dict[str, str], url: str) -> bool:
    response = requests.get(api + "/api/worker/check", params={"url": url}, headers=headers, timeout=20)
    response.raise_for_status()
    return bool(response.json().get("known"))


def _post(url: str, headers: dict[str, str], payload: dict) -> dict:
    response = requests.post(url, headers=headers, json=payload, timeout=60)
    response.raise_for_status()
    data = response.json()
    if data.get("error"):
        raise RuntimeError(str(data["error"]))
    return data


def _read_offset(path: Path) -> int:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return max(int(data.get("source_offset", 0)), 0)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 0


def _write_offset(path: Path, value: int) -> None:
    path.write_text(json.dumps({"source_offset": value}), encoding="utf-8")


if __name__ == "__main__":
    main()
