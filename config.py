from dataclasses import dataclass
import os
from pathlib import Path
import shutil

from dotenv import load_dotenv


load_dotenv(override=True)


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_id: int
    channel_id: str
    openai_api_key: str
    openai_model: str
    database_path: str
    sources_path: str
    scan_sources_per_cycle: int
    scan_limit_per_source: int
    scan_limit_total: int
    max_news_age_hours: int
    auto_scan_enabled: bool
    scan_interval_minutes: int
    publication_interval_minutes: int
    web_enabled: bool
    web_host: str
    web_port: int
    web_username: str
    web_password: str
    event_similarity_threshold: float
    event_match_window_hours: int
    event_match_max_candidates: int
    importance_auto_publish: int
    importance_review_min: int
    openai_retry_count: int
    http_retry_count: int
    log_file: str
    log_max_bytes: int
    log_backup_count: int
    ranking_dry_run: bool
    tiktok_client_key: str
    tiktok_client_secret: str
    tiktok_redirect_uri: str
    tiktok_token_path: str
    tiktok_test_image_url: str
    tiktok_test_title: str
    tiktok_test_description: str
    tiktok_publish_history_path: str
    tiktok_media_base_url: str
    tiktok_fallback_image: str
    tiktok_media_dir: str
    github_media_repo: str
    github_media_branch: str
    github_media_path: str
    github_token: str
    tiktok_auto_publish_enabled: bool
    tiktok_auto_publish_score: int
    max_telegram_auto_per_scan: int
    max_telegram_auto_per_hour: int
    max_tiktok_auto_per_scan: int


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Environment variable {name} is required")
    return value


def _persistent_path(value: str, data_dir: str) -> str:
    """Put relative state paths on Render's persistent disk when configured."""
    if not data_dir:
        return value
    path = os.path.expanduser(value)
    if os.path.isabs(path):
        return path
    return os.path.join(data_dir, path)


def _migrate_state_file(value: str, data_dir: str) -> str:
    target = Path(_persistent_path(value, data_dir)).expanduser()
    if not data_dir or target.exists() or Path(value).is_absolute():
        return str(target if data_dir else value)
    legacy = Path(value).expanduser()
    if legacy.exists() and legacy.resolve() != target.resolve():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(legacy, target)
    return str(target)


def load_config() -> Config:
    data_dir = os.getenv("DATA_DIR", "").strip()
    return Config(
        bot_token=_required("TELEGRAM_BOT_TOKEN"),
        admin_id=int(_required("TELEGRAM_ADMIN_ID")),
        channel_id=_required("TELEGRAM_CHANNEL_ID"),
        openai_api_key=_required("OPENAI_API_KEY"),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        database_path=_migrate_state_file(os.getenv("DATABASE_PATH", "dot_news_bot.sqlite3"), data_dir),
        sources_path=os.getenv("SOURCES_PATH", "sources.json"),
        scan_sources_per_cycle=max(int(os.getenv("SCAN_SOURCES_PER_CYCLE", "6")), 1),
        scan_limit_per_source=int(os.getenv("SCAN_LIMIT_PER_SOURCE", "10")),
        scan_limit_total=int(os.getenv("SCAN_LIMIT_TOTAL", "50")),
        max_news_age_hours=int(os.getenv("MAX_NEWS_AGE_HOURS", "72")),
        auto_scan_enabled=os.getenv("AUTO_SCAN_ENABLED", "false").lower() in {"1", "true", "yes", "on"},
        scan_interval_minutes=int(os.getenv("SCAN_INTERVAL_MINUTES", "5")),
        publication_interval_minutes=max(int(os.getenv("PUBLICATION_INTERVAL_MINUTES", "5")), 0),
        web_enabled=os.getenv("WEB_ENABLED", "true").lower() in {"1", "true", "yes", "on"},
        web_host=os.getenv("WEB_HOST", "127.0.0.1"),
        web_port=int(os.getenv("WEB_PORT", "8080")),
        web_username=os.getenv("WEB_USERNAME", "admin"),
        web_password=_required("WEB_PASSWORD"),
        event_similarity_threshold=float(os.getenv("EVENT_SIMILARITY_THRESHOLD", "0.76")),
        event_match_window_hours=int(os.getenv("EVENT_MATCH_WINDOW_HOURS", "72")),
        event_match_max_candidates=int(os.getenv("EVENT_MATCH_MAX_CANDIDATES", "300")),
        importance_auto_publish=int(os.getenv("IMPORTANCE_AUTO_PUBLISH", "80")),
        importance_review_min=int(os.getenv("IMPORTANCE_REVIEW_MIN", "50")),
        openai_retry_count=int(os.getenv("OPENAI_RETRY_COUNT", "3")),
        http_retry_count=int(os.getenv("HTTP_RETRY_COUNT", "3")),
        log_file=_persistent_path(os.getenv("LOG_FILE", "logs/dot_news.log"), data_dir),
        log_max_bytes=int(os.getenv("LOG_MAX_BYTES", "5242880")),
        log_backup_count=int(os.getenv("LOG_BACKUP_COUNT", "3")),
        ranking_dry_run=os.getenv("RANKING_DRY_RUN", "true").lower() in {"1", "true", "yes", "on"},
        tiktok_client_key=os.getenv("TIKTOK_CLIENT_KEY", ""),
        tiktok_client_secret=os.getenv("TIKTOK_CLIENT_SECRET", ""),
        tiktok_redirect_uri=os.getenv("TIKTOK_REDIRECT_URI", "http://127.0.0.1:8080/auth/tiktok/callback"),
        tiktok_token_path=_migrate_state_file(os.getenv("TIKTOK_TOKEN_PATH", ".tiktok_tokens.json"), data_dir),
        tiktok_test_image_url=os.getenv("TIKTOK_TEST_IMAGE_URL", ""),
        tiktok_test_title=os.getenv("TIKTOK_TEST_TITLE", "DOT News test photo"),
        tiktok_test_description=os.getenv("TIKTOK_TEST_DESCRIPTION", "Test photo post from DOT News Sandbox"),
        tiktok_publish_history_path=_migrate_state_file(os.getenv("TIKTOK_PUBLISH_HISTORY_PATH", ".tiktok_publish_history.json"), data_dir),
        tiktok_media_base_url=os.getenv("TIKTOK_MEDIA_BASE_URL", "https://onfry2012.github.io/dot-news-legal/media/"),
        tiktok_fallback_image=os.getenv("TIKTOK_FALLBACK_IMAGE", "assets/tiktok_fallback.jpg"),
        tiktok_media_dir=_persistent_path(os.getenv("TIKTOK_MEDIA_DIR", ".tiktok_media"), data_dir),
        github_media_repo=os.getenv("GITHUB_MEDIA_REPO", ""),
        github_media_branch=os.getenv("GITHUB_MEDIA_BRANCH", "main"),
        github_media_path=os.getenv("GITHUB_MEDIA_PATH", "media"),
        github_token=os.getenv("GITHUB_TOKEN", ""),
        tiktok_auto_publish_enabled=os.getenv("TIKTOK_AUTO_PUBLISH_ENABLED", "false").lower() in {"1", "true", "yes", "on"},
        tiktok_auto_publish_score=int(os.getenv("TIKTOK_AUTO_PUBLISH_SCORE", "85")),
        max_telegram_auto_per_scan=max(int(os.getenv("MAX_TELEGRAM_AUTO_PER_SCAN", "1")), 1),
        max_telegram_auto_per_hour=max(int(os.getenv("MAX_TELEGRAM_AUTO_PER_HOUR", "12")), 1),
        max_tiktok_auto_per_scan=max(int(os.getenv("MAX_TIKTOK_AUTO_PER_SCAN", "1")), 1),
    )

