"""Standalone launcher for the monitoring panel.

Use this when Telegram/OpenAI credentials are not configured yet.
"""

import os
from time import sleep

from database import Database
from web_server import start_web_server


def main() -> None:
    host = os.getenv("WEB_HOST", "127.0.0.1")
    port = int(os.getenv("WEB_PORT", "8080"))
    database_path = os.getenv("DATABASE_PATH", "dot_news_bot.sqlite3")
    sources_path = os.getenv("SOURCES_PATH", "sources.json")
    start_web_server(host, port, Database(database_path), sources_path)
    print(f"DOT NEWS web panel: http://{host}:{port}")
    try:
        while True:
            sleep(3600)
    except KeyboardInterrupt:
        print("Web panel stopped")


if __name__ == "__main__":
    main()
