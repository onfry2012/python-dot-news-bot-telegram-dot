import asyncio
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import socket

from telegram_bot import run_bot
from config import load_config
from database import Database
from web_server import start_web_server


def _lan_ipv4() -> str | None:
    try:
        addresses = socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        return None
    candidates = sorted({address for address in addresses if address and not address.startswith(("127.", "169.254."))})
    return candidates[0] if candidates else None


def main() -> None:
    config = load_config()
    Path(config.log_file).parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    file_handler = RotatingFileHandler(config.log_file, maxBytes=config.log_max_bytes, backupCount=config.log_backup_count, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[console, file_handler])
    if config.web_enabled:
        start_web_server(
            config.web_host,
            config.web_port,
            Database(config.database_path),
            config.sources_path,
            config.bot_token,
            config.channel_id,
            config.tiktok_client_key,
            config.tiktok_client_secret,
            config.tiktok_redirect_uri,
            config.tiktok_token_path,
            config.tiktok_publish_history_path,
            config.tiktok_test_image_url,
            config.tiktok_test_title,
            config.tiktok_test_description,
            config.tiktok_media_base_url,
            config.tiktok_fallback_image,
            config.tiktok_media_dir,
            config.github_media_repo,
            config.github_media_branch,
            config.github_media_path,
            config.github_token,
            config.scan_interval_minutes,
            config.publication_interval_minutes,
            config.importance_auto_publish,
            config.tiktok_auto_publish_score,
            config.ranking_dry_run,
            config.tiktok_auto_publish_enabled,
            config.web_username,
            config.web_password,
            config.worker_heartbeat_secret,
            config.worker_offline_after_minutes,
        )
        logger = logging.getLogger(__name__)
        logger.info("Web panel bound on %s:%s", config.web_host, config.web_port)
        logger.info("Web panel Local: http://127.0.0.1:%s/", config.web_port)
        lan_ip = _lan_ipv4()
        if lan_ip:
            logger.info("Web panel LAN: http://%s:%s/", lan_ip, config.web_port)
        else:
            logger.warning("LAN IPv4 was not detected. Run ipconfig to find the local IPv4 address.")
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
