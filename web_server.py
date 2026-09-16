from __future__ import annotations

from html import escape
import asyncio
from datetime import datetime, timezone
import logging
import json
import os
import secrets
import hmac
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, urlparse, unquote

from aiogram import Bot
from database import Article, Database
from tiktok_oauth import create_authorization_url, exchange_code
from tiktok_oauth import load_tokens
from tiktok_publisher import (
    TikTokAPIError,
    TikTokSettings,
    available_privacy_levels,
    extract_status,
    extract_status_details,
    get_creator_info,
    get_post_status,
    load_publish_history,
    normalize_publish_status,
    publish_photo,
    update_publish_status,
)
from tiktok_media import (
    TikTokMediaError,
    prepare_tiktok_image,
    publish_image_to_public_storage,
    storage_message,
)
from ai_writer import looks_untranslated_tiktok
logger = logging.getLogger(__name__)
_tiktok_previews: dict[int, dict] = {}
_tiktok_latest_preview_id: int | None = None
_web_sessions: dict[str, float] = {}
_web_sessions_lock = Lock()


def _session_token() -> str:
    return secrets.token_urlsafe(32)


def _cookie_session(handler: BaseHTTPRequestHandler) -> str | None:
    cookie_header = handler.headers.get("Cookie", "")
    for item in cookie_header.split(";"):
        key, _, value = item.strip().partition("=")
        if key == "dot_news_session" and value:
            return value
    return None


def _is_authenticated(handler: BaseHTTPRequestHandler) -> bool:
    token = _cookie_session(handler)
    if not token:
        return False
    now = time.time()
    with _web_sessions_lock:
        expires_at = _web_sessions.get(token, 0)
        if expires_at <= now:
            _web_sessions.pop(token, None)
            return False
        _web_sessions[token] = now + 86400
    return True


def _safe_next(value: str) -> str:
    if value.startswith("/") and not value.startswith("//"):
        return value
    return "/"


def _setting_int(db: Database, name: str, default: int, minimum: int = 0, maximum: int = 1440) -> int:
    try:
        value = int(db.get_setting(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(max(value, minimum), maximum)


def _settings_panel(db: Database, scan_interval_minutes: int, publication_interval_minutes: int) -> str:
    return f'''<section class="settings-panel"><div class="section-head"><h2>Настройки публикаций</h2><span class="updated">Сохраняются локально в SQLite</span></div>
<form class="settings-form" method="post" action="/action"><input type="hidden" name="action" value="settings_save">
<div class="settings-field"><label for="scan-interval">Интервал сканирования новостей, минут</label><small>После завершения скана бот ждёт это время и начинает следующий цикл.</small><input id="scan-interval" type="number" name="scan_interval_minutes" min="1" max="1440" value="{scan_interval_minutes}" required></div>
<div class="settings-field"><label for="publication-interval">Пауза между автоматическими публикациями, минут</label><small>Если за один цикл выбрано несколько новостей, следующая публикация будет отложена на этот интервал. 0 отключает паузу.</small><input id="publication-interval" type="number" name="publication_interval_minutes" min="0" max="1440" value="{publication_interval_minutes}" required></div>
<div class="settings-actions"><button class="tool primary" type="submit">Сохранить настройки</button><a class="tool" href="/">Вернуться к обзору</a></div></form></section>'''


def _tiktok_article_controls(db: Database, article: Article) -> str:
    publication = db.get_tiktok_publication(article.id)
    tiktok_status = str(article.tiktok_status or "").upper()
    if not article.image_url:
        return (
            '<button class="tiktok-action" type="button" disabled>No image — TikTok skipped</button>'
            '<small class="tiktok-state bad">TikTok: пропущено — нет фото</small>'
        )
    if not publication:
        if tiktok_status == "SKIPPED_NO_IMAGE":
            reason = f" ({escape(str(article.tiktok_fail_reason))})" if article.tiktok_fail_reason else ""
            return (
                '<form method="post" action="/action">'
                '<input type="hidden" name="action" value="tiktok_prepare">'
                f'<input type="hidden" name="article_id" value="{article.id}">'
                '<button class="tiktok-action" type="submit">↻ Retry TikTok</button></form>'
                f'<small class="tiktok-state bad">TikTok: пропущено — нет фото{reason}</small>'
            )
        return (
            '<form method="post" action="/action">'
            '<input type="hidden" name="action" value="tiktok_prepare">'
            f'<input type="hidden" name="article_id" value="{article.id}">'
            '<button class="tiktok-action" type="submit">📱 Подготовить TikTok preview</button><small class="tiktok-hint">Подготовка preview может занять до минуты</small></form>'
            '<small class="tiktok-state">TikTok: Not published</small>'
        )
    status = str(publication.get("status") or "UNKNOWN").upper()
    fail_reason = str(publication.get("fail_reason") or "")
    status = normalize_publish_status(status)
    if status == "PUBLISHED":
        state = '<small class="tiktok-state good">✅ TikTok Published</small>'
        action = ""
    elif status == "PROCESSING":
        state = f'<small class="tiktok-state">TikTok: {escape(status)}</small>'
        action = (
            '<form method="post" action="/action">'
            '<input type="hidden" name="action" value="tiktok_article_status">'
            f'<input type="hidden" name="article_id" value="{article.id}">'
            '<button class="tiktok-action" type="submit">🔄 Refresh TikTok status</button></form>'
        )
    else:
        reason = f": {escape(fail_reason)}" if fail_reason else ""
        state = f'<small class="tiktok-state bad">❌ TikTok Failed{reason}</small>'
        action = (
            '<form method="post" action="/action">'
            '<input type="hidden" name="action" value="tiktok_prepare">'
            f'<input type="hidden" name="article_id" value="{article.id}">'
            '<button class="tiktok-action" type="submit">↻ Retry TikTok</button></form>'
        )
    return action + state


def _status_badge(status: str) -> str:
    labels = {"draft": "Черновик", "published": "Опубликовано", "skipped": "Пропущено"}
    return f'<span class="status {escape(status)}">{labels.get(status, status)}</span>'


def _automation_panel(
    db: Database,
    auto_scan: bool,
    auto_publish: bool,
    auto_tiktok_publish: bool,
    telegram_threshold: int,
    tiktok_threshold: int,
    ranking_dry_run: bool,
) -> str:
    runs = db.list_recent_automation_runs(20)
    if runs:
        rows = "".join(
            "<tr>"
            f"<td>{escape(str(row.get('created_at') or ''))}</td>"
            f"<td>#{int(row.get('article_id') or 0)} / event #{int(row.get('event_id') or 0) if row.get('event_id') else '-'}</td>"
            f"<td>{int(row.get('importance_score') or 0)}/100</td>"
            f"<td>{escape(str(row.get('category') or '-'))}</td>"
            f"<td>{int(row.get('source_count') or 1)}</td>"
            f"<td>{'yes' if row.get('image_url') else 'no'}</td>"
            f"<td>{escape(str(row.get('telegram_status') or 'UNKNOWN'))}</td>"
            f"<td>{escape(str(row.get('tiktok_status') or 'UNKNOWN'))}<small>{escape(str(row.get('reason') or ''))}</small></td>"
            "</tr>"
            for row in runs
        )
    else:
        rows = '<tr><td colspan="8" class="empty">Автоматических решений пока нет</td></tr>'
    dry_label = "включён: публикация заблокирована" if ranking_dry_run else "выключен: разрешён реальный режим"
    return f'''<section id="automation" class="automation-panel"><div class="section-head"><h2>Automation</h2><span class="updated">{escape(dry_label)}</span></div>
<div class="automation-controls"><form method="post" action="/action"><input type="hidden" name="action" value="auto_scan"><button class="tool {'on' if auto_scan else ''}" type="submit">{'⏸ Выключить автоскан' if auto_scan else '▶ Включить автоскан'}</button></form><form method="post" action="/action"><input type="hidden" name="action" value="auto_publish"><button class="tool {'on' if auto_publish else ''}" type="submit">{'⏸ Telegram autopost: ON' if auto_publish else '▶ Telegram autopost: OFF'}</button></form><form method="post" action="/action"><input type="hidden" name="action" value="auto_tiktok_publish"><button class="tool {'on' if auto_tiktok_publish else ''}" type="submit">{'⏸ TikTok autopost: ON' if auto_tiktok_publish else '▶ TikTok autopost: OFF'}</button></form></div>
<div class="automation-meta">Telegram: <strong>{telegram_threshold}+</strong> · TikTok: <strong>{tiktok_threshold}+</strong></div>
<div class="section-head automation-history-head"><h2>Последние 20 автоматических решений</h2></div><table><thead><tr><th>Время</th><th>Article / event</th><th>Score</th><th>Категория</th><th>Источники</th><th>Фото</th><th>Telegram</th><th>TikTok / причина</th></tr></thead><tbody>{rows}</tbody></table></section>'''


def _score_breakdown_html(db: Database, article: Article) -> str:
    if not article.event_id:
        return ""
    event = db.get_event(article.event_id) or {}
    raw = event.get("score_breakdown")
    breakdown = {}
    if raw:
        try:
            value = json.loads(str(raw))
            if isinstance(value, dict):
                breakdown = value
        except (TypeError, json.JSONDecodeError):
            breakdown = {}
    if not breakdown:
        return '<details class="score-breakdown"><summary>Score breakdown</summary><small>Детализация появится для новых расчётов рейтинга.</small></details>'
    labels = {
        "sources": "Sources",
        "freshness": "Freshness",
        "category": "Category",
        "event_type": "Event type",
        "audience_value": "Audience value",
        "source_weight": "Source weight",
        "penalties": "Penalties",
    }
    lines = "".join(
        f"<div>{escape(label)}: {('+' if int(breakdown.get(key, 0)) > 0 else '')}{int(breakdown.get(key, 0))}</div>"
        for key, label in labels.items()
    )
    return f'<details class="score-breakdown"><summary>Score breakdown</summary>{lines}<strong>Total: {int(breakdown.get("total", article.importance_score))}</strong></details>'


def _stats_24h_panel(db: Database) -> str:
    stats = db.dashboard_24h()
    distribution = stats["distribution"]
    distribution_rows = "".join(
        f"<tr><td>{escape(bucket)}</td><td>{int(distribution[bucket])}</td></tr>" for bucket in ("0-39", "40-49", "50-59", "60-69", "70-79", "80-89", "90-100")
    )
    tg = stats["telegram"]
    tt = stats["tiktok"]
    return f'''<section class="stats-panel"><div class="section-head"><h2>DOT News — последние 24 часа</h2><span class="updated">Новые записи SQLite</span></div>
<div class="stats-columns"><div><h3>Рейтинг событий</h3><div class="stats-list"><div>Найдено статей <strong>{stats['articles_found']}</strong></div><div>Создано событий <strong>{stats['events_created']}</strong></div><div>Средний score <strong>{stats['average_score']}</strong></div><div>Median score <strong>{stats['median_score']}</strong></div><div>Максимальный score <strong>{stats['max_score']}</strong></div><div>Количество 50+ <strong>{stats['count_50']}</strong></div><div>Количество 60+ <strong>{stats['count_60']}</strong></div><div>Количество 70+ <strong>{stats['count_70']}</strong></div><div>Количество 80+ <strong>{stats['count_80']}</strong></div></div></div><div><h3>Telegram</h3><div class="stats-list"><div>Автоматически <strong>{tg['auto_published']}</strong></div><div>Вручную <strong>{tg['manual_published']}</strong></div><div>Ошибок <strong>{tg['errors']}</strong></div></div><h3>TikTok</h3><div class="stats-list"><div>Автоматически <strong>{tt['auto_published']}</strong></div><div>Вручную <strong>{tt['manual_published']}</strong></div><div>SKIPPED_NO_IMAGE <strong>{tt['skipped_no_image']}</strong></div><div>FAILED <strong>{tt['failed']}</strong></div><div>PROCESSING <strong>{tt['processing']}</strong></div></div></div><div><h3>Score distribution</h3><table><thead><tr><th>Диапазон</th><th>Событий</th></tr></thead><tbody>{distribution_rows}</tbody></table><div class="percentiles">P50 <strong>{stats['p50']}</strong> · P75 <strong>{stats['p75']}</strong> · P90 <strong>{stats['p90']}</strong> · MAX <strong>{stats['max']}</strong></div></div></div></section>'''


def _publication_queue(
    db: Database,
    scan_interval_minutes: int,
    tiktok_auto_publish: bool = False,
    tiktok_auto_publish_score: int = 85,
) -> str:
    candidates = [
        article
        for article in db.list_by_status("draft", limit=1000)
        if article.decision in {"AUTO_PUBLISH", "UPDATE"}
    ]
    candidates.sort(key=lambda article: (article.importance_score, article.id), reverse=True)

    queue: list[Article] = []
    event_ids: set[int] = set()
    for article in candidates:
        event = db.get_event(article.event_id) if article.event_id else None
        if event and event.get("status") == "published" and article.decision != "UPDATE":
            continue
        if article.event_id and article.event_id in event_ids:
            continue
        queue.append(article)
        if article.event_id:
            event_ids.add(article.event_id)

    auto_per_scan = max(int(os.getenv("MAX_TELEGRAM_AUTO_PER_SCAN", "1")), 1)
    auto_per_hour = max(int(os.getenv("MAX_TELEGRAM_AUTO_PER_HOUR", "12")), 1)
    published_last_hour = db.count_recent_telegram_auto_published(1)
    available_slots = max(auto_per_hour - published_last_hour, 0)
    rows = []
    for position, article in enumerate(queue[:30], start=1):
        cycle = (position - 1) // auto_per_scan + 1
        if position > available_slots:
            eta = "после часового лимита"
        elif cycle == 1:
            eta = "ближайший цикл"
        else:
            eta = f"примерно через {(cycle - 1) * max(scan_interval_minutes, 1)} мин."
        event = db.get_event(article.event_id) if article.event_id else None
        if not tiktok_auto_publish:
            destination = "Telegram"
        elif not article.image_url:
            destination = "Telegram · TikTok: нет фото"
        elif article.importance_score < tiktok_auto_publish_score:
            destination = f"Telegram · TikTok: score < {tiktok_auto_publish_score}"
        else:
            destination = "Telegram + TikTok"
        rows.append(
            "<tr>"
            f"<td><strong>{position}</strong></td>"
            f"<td>#{article.id} / event #{article.event_id or '-'}</td>"
            f"<td><strong>{article.importance_score}/100</strong></td>"
            f"<td>{escape(str((event or {}).get('category') or '-'))}</td>"
            f"<td>{db.event_source_count(article.event_id)}</td>"
            f"<td>{'yes' if article.image_url else 'no'}</td>"
            f"<td>{escape(destination)}</td>"
            f"<td>{escape(article.decision)}</td>"
            f"<td>{escape(eta)}</td>"
            f"<td><a href=\"{escape(article.original_url, quote=True)}\" target=\"_blank\" rel=\"noreferrer\">{escape(article.original_title[:100])}</a></td>"
            f"<td><form method=\"post\" action=\"/action\" onsubmit=\"return confirm('Убрать эту новость из очереди?')\"><input type=\"hidden\" name=\"action\" value=\"queue_delete\"><input type=\"hidden\" name=\"article_id\" value=\"{article.id}\"><button class=\"delete\" type=\"submit\">Удалить</button></form></td>"
            "</tr>"
        )
    rows_html = "".join(rows) if rows else '<tr><td colspan="11" class="empty">Очередь публикации пуста</td></tr>'
    return f'''<section class="queue-panel"><div class="section-head"><h2>Очередь публикации</h2><span class="updated">{len(queue)} кандидатов · {published_last_hour}/{auto_per_hour} за последний час</span></div>
<div class="queue-meta">Автопубликация: до {auto_per_scan} новостей за цикл. Сначала идут самые сильные события; одинаковые события объединены в одну позицию.</div>
<table><thead><tr><th>#</th><th>Article / event</th><th>Score</th><th>Категория</th><th>Источники</th><th>Фото</th><th>Публикация</th><th>Решение</th><th>Ожидание</th><th>Материал</th><th>Действие</th></tr></thead><tbody>{rows_html}</tbody></table></section>'''


def _article_row(db: Database, article: Article, source_count: int = 1) -> str:
    post = (article.rewritten_post or article.original_title).replace("\n", " ")
    action = ""
    if article.status == "draft":
        event = db.get_event(article.event_id) if article.event_id else None
        duplicate_published = bool(event and event.get("status") == "published" and article.decision != "UPDATE")
        publish_action = (
            f'<form method="post" action="/publish" onsubmit="return confirm(\'Опубликовать эту новость в канал?\')">'
            f'<input type="hidden" name="article_id" value="{article.id}">'
            '<button class="publish" type="submit">Опубликовать</button></form>'
            if article.decision != "DUPLICATE_UPDATE_SKIP" and not duplicate_published
            else '<span class="muted-action">Дубликат уже опубликован</span>'
        )
        action = (
            publish_action
            + f'<form method="post" action="/action" onsubmit="return confirm(\'Пропустить эту новость?\')">'
            + f'<input type="hidden" name="action" value="skip"><input type="hidden" name="article_id" value="{article.id}">'
            + '<button class="skip" type="submit">Пропустить</button></form>'
        )
    return (
        "<tr>"
        f"<td>#{article.id}</td>"
        f"<td>{_status_badge(article.status)}</td>"
        f"<td>{escape(article.source_name)}<small>🔥 {article.importance_score}/100 · 📰 {source_count} источн. · event #{article.event_id or '-'} · {escape(article.decision)}</small>{_score_breakdown_html(db, article)}</td>"
        f"<td><a href=\"{escape(article.original_url)}\" target=\"_blank\" rel=\"noreferrer\">"
        f"{escape(article.original_title)}</a><small>{escape(post[:220])}</small></td>"
        f"<td>{escape(article.created_at)}</td>"
        f"<td>{action}{_tiktok_article_controls(db, article)}</td>"
        "</tr>"
    )


def _tiktok_panel(
    client_key: str,
    client_secret: str,
    token_path: str,
    history_path: str,
    image_url: str,
    title: str,
    description: str,
) -> str:
    connected = bool(load_tokens(token_path).get("access_token"))
    creator_error = ""
    privacy_options: list[str] = []
    if connected and client_key and client_secret:
        try:
            settings = TikTokSettings(client_key, client_secret, token_path, history_path)
            privacy_options = available_privacy_levels(get_creator_info(settings))
        except Exception as exc:
            creator_error = str(exc)
    status_label = "Connected" if connected else "Not connected"
    image_html = (
        f'<img class="tiktok-preview" src="{escape(image_url, quote=True)}" alt="TikTok test photo">'
        if image_url else '<div class="tiktok-no-image">TIKTOK_TEST_IMAGE_URL не заполнен</div>'
    )
    options_html = '<option value="">Выберите privacy level</option>' + "".join(
        f'<option value="{escape(option, quote=True)}">{escape(option)}</option>' for option in privacy_options
    )
    can_publish = connected and bool(image_url) and bool(privacy_options)
    disabled = "" if can_publish else " disabled"
    reason = creator_error or (
        "Заполните TIKTOK_TEST_IMAGE_URL" if not image_url else
        "Сначала подключите TikTok" if not connected else
        "TikTok не вернул доступные privacy options"
    )
    history = load_publish_history(history_path)
    latest = history[0] if history else None
    history_html = ""
    if latest:
        publish_id = escape(str(latest.get("publish_id", "")))
        created_at = escape(str(latest.get("created_at", "")))
        status = escape(str(latest.get("status", "UNKNOWN")))
        fail_reason = escape(str(latest.get("fail_reason", "")) or "—")
        history_html = (
            f'<div class="tiktok-result"><strong>Последний тест</strong>: '
            f'publish_id <code>{publish_id}</code> · {created_at}<br>'
            f'<strong>Status:</strong> {status}<br><strong>Fail reason:</strong> {fail_reason}'
            f'<form method="post" action="/action"><input type="hidden" name="action" value="tiktok_status">'
            f'<button class="tool" type="submit">Обновить статус TikTok</button></form></div>'
        )
    return f'''<section class="tiktok-panel"><div class="section-head"><h2>TikTok Sandbox</h2><span class="updated">TikTok: <strong>{status_label}</strong></span></div>
<div class="tiktok-content"><div>{image_html}</div><div class="tiktok-fields"><div><label>Image URL</label><div class="tiktok-value">{escape(image_url) or "не задан"}</div></div><div><label>Title</label><div class="tiktok-value">{escape(title)}</div></div><div><label>Description</label><div class="tiktok-value">{escape(description)}</div></div><div><label>Privacy level</label><form method="post" action="/action"><input type="hidden" name="action" value="tiktok_publish"><select id="tiktok-privacy" name="privacy_level" required{disabled}>{options_html}</select><div id="tiktok-selected-privacy" class="tiktok-selected">Selected privacy: none</div><button class="publish" type="submit"{disabled}>Publish test photo to TikTok</button></form>{f'<small class="tiktok-error">{escape(reason)}</small>' if not can_publish else ''}</div></div></div>{history_html}</section>'''


def _tiktok_preview_panel(db: Database, preview_id: int | None) -> str:
    if not preview_id:
        return ""
    preview = _tiktok_previews.get(preview_id)
    article = db.get_article(preview_id)
    if not preview or not article:
        return '<div class="notice">TikTok preview истёк. Подготовьте новость ещё раз.</div>'
    event = db.get_event(article.event_id) if article.event_id else None
    privacy_options = [str(value) for value in preview.get("privacy_options", []) if value]
    options_html = '<option value="">Выберите privacy level</option>' + "".join(
        f'<option value="{escape(option, quote=True)}">{escape(option)}</option>' for option in privacy_options
    )
    public_url = str(preview.get("public_url") or "")
    confirm_disabled = "" if public_url and privacy_options and not preview.get("review_required") else " disabled"
    storage_note = str(preview.get("storage_message") or "")
    if not public_url:
        storage_note = storage_note or "Image prepared. Upload to verified media storage before TikTok publish."
    fallback_note = " Используется fallback image." if preview.get("fallback_used") else ""
    local_name = Path(str(preview.get("local_path", ""))).name
    source_link = f'<a href="{escape(article.original_url, quote=True)}" target="_blank" rel="noreferrer">Открыть оригинал статьи</a>'
    public_url_html = (
        f'<div><label>Public image URL</label><div class="tiktok-value"><a href="{escape(public_url, quote=True)}" target="_blank" rel="noreferrer">{escape(public_url)}</a></div></div>'
        if public_url else ""
    )
    review_note = str(preview.get("review_note") or "")
    if review_note:
        storage_note = f"{storage_note} {review_note}".strip()
    return f'''<section class="tiktok-preview-panel"><div class="section-head"><h2>Preview: {escape(article.original_title)}</h2><span class="updated">TikTok manual review</span></div>
<div class="tiktok-content"><div><img class="tiktok-preview" src="/tiktok/media/{quote(local_name)}" alt="Prepared TikTok image"><small class="tiktok-preview-note">{escape(storage_note)}{fallback_note}</small></div><div class="tiktok-fields"><div><label>TikTok title</label><div class="tiktok-value">{escape(str(preview.get("title", "")))}</div></div><div><label>TikTok caption</label><div class="tiktok-value">{escape(str(preview.get("caption", "")))}</div></div><div><label>Hashtags</label><div class="tiktok-value">{escape(str(preview.get("hashtags", "")))}</div></div>{public_url_html}<div><label>Article / event</label><div class="tiktok-value">article #{article.id} · event #{article.event_id or '-'} · {int(article.importance_score)}/100</div></div><div><label>Source article</label><div class="tiktok-value">{source_link}</div></div><div><label>Privacy level</label><form method="post" action="/action"><input type="hidden" name="action" value="tiktok_confirm"><input type="hidden" name="article_id" value="{article.id}"><select name="privacy_level" required{confirm_disabled}>{options_html}</select><button class="publish" type="submit"{confirm_disabled}>⬆ Загрузить в TikTok</button><a class="tool cancel" href="/">❌ Отмена</a></form>{'<small class="tiktok-error">TikTok API не получит запрос, пока не будет исправлен язык TikTok-текста.</small>' if preview.get("review_required") else ''}{'<small class="tiktok-error">TikTok API не получит запрос, пока изображение не будет доступно по verified public URL.</small>' if not public_url and not preview.get("review_required") else ''}</div></div></div></section>'''


def render_dashboard(
    db: Database,
    sources_path: str,
    notice: str = "",
    category: str = "",
    search: str = "",
    tiktok_client_key: str = "",
    tiktok_client_secret: str = "",
    tiktok_token_path: str = ".tiktok_tokens.json",
    tiktok_history_path: str = ".tiktok_publish_history.json",
    tiktok_test_image_url: str = "",
    tiktok_test_title: str = "DOT News test photo",
    tiktok_test_description: str = "Test photo post from DOT News Sandbox",
    tiktok_preview_id: int | None = None,
    scan_interval_minutes: int = 5,
    publication_interval_minutes: int = 5,
    telegram_auto_publish_score: int = 80,
    tiktok_auto_publish_score: int = 85,
    ranking_dry_run: bool = True,
    tiktok_auto_publish_default: bool = False,
    tab: str = "dashboard",
    view: str = "overview",
) -> str:
    counts = db.counts_by_status()
    auto_scan = db.get_setting("auto_scan_enabled", "0") == "1"
    auto_publish = db.get_setting("auto_publish_enabled", "0") == "1"
    auto_tiktok_publish = db.get_setting(
        "auto_tiktok_publish_enabled",
        "1" if tiktok_auto_publish_default else "0",
    ) == "1"
    try:
        raw_sources = json.loads(Path(sources_path).read_text(encoding="utf-8"))
        categories = [(str(source.get("category", "news")), str(source.get("category", "news"))) for source in raw_sources]
        source_categories = {str(source.get("name", "")): str(source.get("category", "news")) for source in raw_sources}
        source_rows = "".join(
            f"<li><strong>{escape(str(source.get('name', 'Без названия')))}</strong>"
            f"<span>{escape(str(source.get('category', 'news')))}</span></li>"
            for source in raw_sources
        )
    except Exception as exc:
        logger.warning("Could not load sources for web panel: %s", exc)
        categories = []
        source_categories = {}
        source_rows = "<li>Не удалось загрузить sources.json</li>"

    articles = [
        article
        for article in db.list_recent(200)
        if (not category or source_categories.get(article.source_name, "") == category)
        and (not search or search.casefold() in article.original_title.casefold() or search.casefold() in article.source_name.casefold())
    ][:50]
    rows = "".join(_article_row(db, article, db.event_source_count(article.event_id)) for article in articles)
    if not rows:
        empty_text = f"По запросу «{escape(search)}» ничего не найдено" if search else "Новостей пока нет"
        rows = f'<tr><td colspan="6" class="empty">{empty_text}</td></tr>'
    notice_html = f'<div class="notice">{escape(notice)}</div>' if notice else ""
    category_links = ['<a class="topic active" href="/">Все</a>']
    for value, label in dict.fromkeys(categories):
        active = " active" if category == value else ""
        category_links.append(f'<a class="topic{active}" href="/?category={quote(value)}">{escape(label.title())}</a>')
    topics_html = "".join(category_links)
    search_value = escape(search, quote=True)
    tiktok_html = _tiktok_panel(
        tiktok_client_key,
        tiktok_client_secret,
        tiktok_token_path,
        tiktok_history_path,
        tiktok_test_image_url,
        tiktok_test_title,
        tiktok_test_description,
    )
    tiktok_preview_html = _tiktok_preview_panel(db, tiktok_preview_id)
    automation_html = _automation_panel(
        db,
        auto_scan,
        auto_publish,
        auto_tiktok_publish,
        telegram_auto_publish_score,
        tiktok_auto_publish_score,
        ranking_dry_run,
    )
    stats_html = _stats_24h_panel(db)
    runtime_scan_interval = _setting_int(db, "scan_interval_minutes", scan_interval_minutes, minimum=1)
    runtime_publication_interval = _setting_int(db, "publication_interval_minutes", publication_interval_minutes, minimum=0)
    queue_html = _publication_queue(
        db,
        runtime_scan_interval,
        auto_tiktok_publish,
        tiktok_auto_publish_score,
    )
    settings_html = _settings_panel(db, runtime_scan_interval, runtime_publication_interval)
    is_tiktok_tab = tab == "tiktok"
    is_preview_tab = tab == "preview"
    is_settings_tab = tab == "settings"
    is_queue_view = view == "queue"
    is_automation_view = view == "automation"
    is_overview = not is_tiktok_tab and not is_preview_tab and not is_settings_tab and not is_queue_view and not is_automation_view
    tab_nav = (
        '<nav class="tabs" aria-label="Разделы панели">'
        f'<a class="tab{" active" if is_overview else ""}" href="/">Обзор</a>'
        f'<a class="tab{" active" if is_tiktok_tab else ""}" href="/?tab=tiktok">TikTok Sandbox</a>'
        f'<a class="tab{" active" if is_preview_tab else ""}" href="/?tab=preview">TikTok Preview</a>'
        f'<a class="tab{" active" if is_settings_tab else ""}" href="/?tab=settings">Настройки</a>'
        '</nav>'
    )
    if is_preview_tab:
        dashboard_content = tiktok_preview_html or '<section class="tiktok-preview-panel"><div class="empty">Готового preview пока нет. Подготовьте его из карточки новости.</div></section>'
    elif is_tiktok_tab:
        dashboard_content = f"{tiktok_preview_html}{tiktok_html}"
    elif is_settings_tab:
        dashboard_content = settings_html
    elif is_queue_view:
        dashboard_content = queue_html
    elif is_automation_view:
        dashboard_content = f"{stats_html}{automation_html}"
    else:
        dashboard_content = f"{queue_html}{stats_html}{automation_html}"
    results_content = (
        f'<div class="dashboard-tools"><form class="search" method="post" action="/action"><input type="hidden" name="action" value="search"><input name="query" value="{search_value}" maxlength="120" placeholder="Например: Варшава, украинцы в Польше"><button class="tool primary" type="submit">Найти тему</button></form><div class="topics">{topics_html}</div><div class="grid"><div class="metric draft"><label>Черновики</label><b>{counts.get("draft", 0)}</b></div><div class="metric published"><label>Опубликовано</label><b>{counts.get("published", 0)}</b></div><div class="metric skipped"><label>Пропущено</label><b>{counts.get("skipped", 0)}</b></div></div><div class="layout"><section><div class="section-head"><h2>{escape("Результаты поиска: " + search) if search else "Последние новости"}</h2><span class="updated">{len(articles)} записей</span></div><table><thead><tr><th>ID</th><th>Статус</th><th>Источник</th><th>Материал</th><th>Создано</th><th>Действие</th></tr></thead><tbody>{rows}</tbody></table></section><aside class="sources"><h2>RSS-источники</h2><ul>{source_rows}</ul></aside></div></div>'
        if is_overview else ""
    )
    if is_tiktok_tab or is_preview_tab or is_settings_tab:
        toolbar_html = '<a class="tool" href="/">← Вернуться к обзору</a>'
    elif is_queue_view or is_automation_view:
        toolbar_html = '<a class="tool" href="/">← Обзор</a>'
    else:
        toolbar_html = (
            '<form method="post" action="/action"><input type="hidden" name="action" value="scan"><button class="tool primary" type="submit">🔎 Найти новости</button></form>'
            '<form method="post" action="/action"><input type="hidden" name="action" value="media"><button class="tool" type="submit">🖼 Проверить фото</button></form>'
            f'<form method="get" action="/"><input type="hidden" name="category" value="{escape(category, quote=True)}"><input type="hidden" name="search" value="{escape(search, quote=True)}"><input type="hidden" name="tab" value="{escape(tab, quote=True)}"><input type="hidden" name="view" value="{escape(view, quote=True)}"><button class="tool" type="submit">↻ Обновить список</button></form>'
            f'<form method="post" action="/action"><input type="hidden" name="action" value="auto_scan"><button class="tool automation-toggle {"on" if auto_scan else ""}" type="submit" aria-label="Переключить автоматический сбор">{"⏸ Выключить автоскан" if auto_scan else "▶ Включить автоскан"}</button></form>'
            f'<form method="post" action="/action"><input type="hidden" name="action" value="auto_publish"><button class="tool automation-toggle {"on" if auto_publish else ""}" type="submit" aria-label="Переключить автоматическую публикацию">{"⏸ Выключить автопостинг" if auto_publish else "▶ Включить автопостинг"}</button></form>'
        )
    if is_overview:
        toolbar_html += '<a class="tool" href="/?tab=tiktok">TikTok Sandbox</a>'
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>DOT NEWS · мониторинг</title>
<style>
:root {{ color-scheme: dark; --bg:#07131a; --sidebar:#061018; --panel:#0d1d26; --panel-2:#112832; --line:#1c3b46; --muted:#8fa8b2; --white:#edf7f8; --cyan:#62d6e5; --gold:#f0c75e; --yellow:#f0c75e; --red:#ff7d84; --green:#68dfa4; }}
.brand p {{ margin:6px 0 0; color:var(--muted); font-size:13px; }} .tabs {{ display:flex; gap:4px; border-bottom:1px solid var(--line); margin-bottom:20px; }} .tab {{ color:var(--muted); text-decoration:none; padding:10px 14px; border-bottom:2px solid transparent; }} .tab:hover,.tab.active {{ color:var(--white); border-color:var(--green); }} .dashboard-tools {{ display:block; }}
* {{ box-sizing:border-box; }} body {{ margin:0; background:var(--bg); color:var(--white); font:14px/1.45 Inter,Segoe UI,Arial,sans-serif; }}
main {{ max-width:1180px; margin:0 auto; padding:34px 22px 50px; }} header {{ display:flex; align-items:flex-end; justify-content:space-between; gap:20px; margin-bottom:28px; }}
h1 {{ margin:0; font-size:28px; letter-spacing:.02em; }} h1 span {{ color:#d5d9de; font-weight:400; }} .updated {{ color:var(--muted); }}
.grid {{ display:grid; grid-template-columns:repeat(3,1fr); gap:12px; margin-bottom:24px; }} .metric,.sources,section {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; }} .metric {{ padding:18px 20px; }}
.metric b {{ display:block; font-size:30px; margin-top:6px; }} .metric label {{ color:var(--muted); }} .draft b {{ color:var(--yellow); }} .published b {{ color:var(--green); }} .skipped b {{ color:var(--red); }}
.layout {{ display:grid; grid-template-columns:1fr 260px; gap:18px; align-items:start; }} section {{ overflow:hidden; }} .section-head {{ display:flex; justify-content:space-between; align-items:center; padding:17px 18px; border-bottom:1px solid var(--line); }} h2 {{ margin:0; font-size:16px; }}
table {{ width:100%; border-collapse:collapse; }} th,td {{ text-align:left; padding:13px 14px; border-bottom:1px solid var(--line); vertical-align:top; }} th {{ color:var(--muted); font-size:12px; font-weight:500; }} td a {{ color:var(--white); text-decoration:none; font-weight:600; }} td a:hover {{ text-decoration:underline; }} small {{ display:block; color:var(--muted); margin-top:5px; max-width:560px; }}
.status {{ display:inline-block; padding:3px 7px; border-radius:4px; font-size:12px; }} .status.draft {{ color:var(--yellow); background:#3a301a; }} .status.published {{ color:var(--green); background:#173323; }} .status.skipped {{ color:var(--red); background:#3a2022; }}
.publish {{ border:0; border-radius:4px; padding:7px 10px; color:#07140d; background:var(--green); cursor:pointer; font-weight:600; }} .publish:hover {{ filter:brightness(1.08); }} .delete {{ border:1px solid #744247; border-radius:6px; padding:7px 10px; color:var(--red); background:transparent; cursor:pointer; font-weight:600; }} .delete:hover {{ background:#3a2022; }} .notice {{ margin-bottom:16px; padding:12px 14px; background:#263b2d; border:1px solid #3f754f; border-radius:6px; }}
.skip {{ border:1px solid #744247; border-radius:4px; padding:7px 10px; color:var(--red); background:transparent; cursor:pointer; font-weight:600; margin-top:6px; }} .skip:hover {{ background:#3a2022; }}
.sources {{ padding:18px; }} .sources h2 {{ margin-bottom:12px; }} ul {{ list-style:none; padding:0; margin:0; }} li {{ display:flex; justify-content:space-between; gap:8px; padding:10px 0; border-bottom:1px solid var(--line); }} li:last-child {{ border-bottom:0; }} li span {{ color:var(--muted); }} .empty {{ color:var(--muted); text-align:center; padding:34px; }}
.toolbar {{ display:flex; flex-wrap:wrap; gap:8px; margin-bottom:18px; }} .toolbar form {{ margin:0; }} .tool {{ border:1px solid var(--line); border-radius:4px; padding:8px 11px; color:var(--white); background:var(--panel); cursor:pointer; }} .tool.primary {{ color:#07140d; background:var(--green); border-color:var(--green); }} .tool.on {{ color:#07140d; background:var(--yellow); border-color:var(--yellow); }}
.topics {{ display:flex; flex-wrap:wrap; gap:7px; margin-bottom:18px; }} .topic {{ color:var(--muted); text-decoration:none; border:1px solid var(--line); border-radius:4px; padding:6px 9px; }} .topic:hover,.topic.active {{ color:var(--white); border-color:#69727c; background:#252a30; }} .muted-action {{ display:inline-block; color:var(--muted); padding:8px 0; }}
.search {{ display:flex; gap:8px; margin-bottom:18px; }} .search input {{ flex:1; min-width:160px; border:1px solid var(--line); border-radius:4px; padding:9px 11px; color:var(--white); background:var(--panel); }}
.automation-panel {{ margin-bottom:18px; }} .automation-controls {{ display:flex; flex-wrap:wrap; gap:8px; padding:16px 18px 4px; }} .automation-controls form {{ margin:0; }} .automation-meta {{ padding:8px 18px 16px; color:var(--muted); }} .automation-history-head {{ margin-top:4px; }} .tiktok-panel {{ margin-bottom:18px; }} .tiktok-preview-panel {{ margin-bottom:18px; border:1px solid var(--yellow); }} .tiktok-content {{ display:grid; grid-template-columns:230px 1fr; gap:18px; padding:18px; }} .tiktok-preview {{ display:block; width:100%; aspect-ratio:1/1; object-fit:cover; border-radius:6px; background:#0d0f11; }} .tiktok-no-image {{ min-height:230px; display:grid; place-items:center; padding:18px; color:var(--muted); background:#0d0f11; border-radius:6px; text-align:center; }} .tiktok-fields {{ display:grid; gap:12px; }} .tiktok-fields label {{ display:block; color:var(--muted); font-size:12px; margin-bottom:4px; }} .tiktok-value {{ padding:8px 10px; border:1px solid var(--line); border-radius:4px; white-space:pre-wrap; overflow-wrap:anywhere; }} .tiktok-fields select {{ display:block; width:100%; max-width:360px; margin-bottom:8px; border:1px solid var(--line); border-radius:4px; padding:8px 10px; color:var(--white); background:var(--panel); }} .tiktok-selected {{ color:var(--muted); font-size:12px; margin-bottom:8px; }} .tiktok-fields button[disabled], .tiktok-action[disabled] {{ opacity:.45; cursor:not-allowed; }} .tiktok-error {{ color:var(--yellow); }} .tiktok-preview-note {{ display:block; color:var(--yellow); line-height:1.4; margin-top:8px; }} .tiktok-action {{ display:block; margin-top:7px; padding:5px 8px; border:1px solid var(--line); border-radius:4px; color:var(--white); background:transparent; cursor:pointer; }} .tiktok-action:hover {{ border-color:var(--yellow); }} .tiktok-state {{ display:block; color:var(--muted); margin-top:7px; }} .tiktok-state.good {{ color:var(--green); }} .tiktok-state.bad {{ color:var(--yellow); }} .cancel {{ display:inline-block; margin-left:8px; }} .tiktok-result {{ padding:12px 18px; border-top:1px solid var(--line); color:var(--muted); }} .tiktok-result form {{ display:inline-block; margin-left:10px; }} code {{ color:#cdd5df; }}
.queue-panel {{ margin-bottom:18px; }} .queue-meta {{ padding:12px 18px; color:var(--muted); border-bottom:1px solid var(--line); }} .queue-panel table {{ min-width:980px; }} .queue-panel td a {{ max-width:300px; display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
.tiktok-hint {{ display:block; color:var(--muted); margin-top:6px; }} .tiktok-action[aria-busy="true"] {{ opacity:.7; cursor:wait; }}
.settings-panel {{ max-width:820px; }} .settings-form {{ display:grid; gap:18px; padding:20px; }} .settings-field {{ display:grid; gap:5px; }} .settings-field label {{ color:var(--white); font-weight:600; }} .settings-field small {{ color:var(--muted); margin:0; max-width:none; }} .settings-field input {{ width:180px; border:1px solid var(--line); border-radius:6px; padding:10px 11px; color:var(--white); background:#091820; font:inherit; }} .settings-actions {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; }} .automation-toggle {{ min-width:190px; font-weight:600; }}
.app-shell {{ min-height:100vh; display:grid; grid-template-columns:228px minmax(0,1fr); }} .sidebar {{ position:sticky; top:0; height:100vh; display:flex; flex-direction:column; padding:26px 14px 18px; background:var(--sidebar); border-right:1px solid var(--line); }} .sidebar-brand {{ padding:0 12px 30px; color:var(--cyan); font-size:20px; font-weight:700; }} .sidebar-brand span {{ color:var(--white); font-weight:400; }} .side-nav {{ display:grid; gap:4px; }} .side-nav a {{ color:var(--muted); text-decoration:none; padding:11px 12px; border-radius:7px; }} .side-nav a:hover,.side-nav a.active {{ color:var(--white); background:#102831; }} .sidebar-foot {{ margin-top:auto; padding:12px; color:var(--muted); font-size:12px; }} main {{ width:min(1320px,100%); max-width:none; margin:0; padding:34px 38px 56px; }} h1 {{ font-size:29px; }} .grid {{ gap:12px; margin-bottom:20px; }} .metric,.sources,section {{ border-radius:9px; background:var(--panel); }} .metric b {{ font-size:29px; }} .layout {{ grid-template-columns:minmax(0,1fr) 270px; gap:18px; }} .section-head {{ padding:17px 19px; }} table {{ width:100%; }} th,td {{ padding:12px 14px; }} td a:hover {{ color:var(--cyan); }} .publish {{ border-radius:6px; padding:8px 11px; background:var(--cyan); }} .tool {{ border-radius:6px; text-decoration:none; }} .tool:hover {{ border-color:var(--cyan); }} .tool.primary {{ background:var(--cyan); border-color:var(--cyan); }} .topic:hover,.topic.active {{ border-color:var(--cyan); background:#102831; }} .stats-columns {{ display:grid; grid-template-columns:repeat(3,1fr); gap:24px; padding:20px; }} .stats-columns h3 {{ margin:0 0 10px; font-size:14px; color:var(--cyan); }} .stats-list {{ display:grid; gap:7px; color:var(--muted); }} .stats-list strong {{ color:var(--white); float:right; }} .queue-panel {{ box-shadow:0 10px 30px rgba(0,0,0,.12); }} .queue-meta,.automation-meta {{ padding:11px 18px 15px; }} .tiktok-value {{ background:#091820; border-radius:6px; }}
@media (max-width:900px) {{ .app-shell {{ grid-template-columns:1fr; }} .sidebar {{ position:static; height:auto; padding:16px; }} .sidebar-brand {{ padding-bottom:14px; }} .side-nav {{ display:flex; flex-wrap:wrap; }} .sidebar-foot {{ display:none; }} main {{ padding:24px 16px 40px; }} }} @media (max-width:760px) {{ header {{ display:block; }} .updated {{ margin-top:8px; }} .grid,.layout,.stats-columns,.tiktok-content {{ grid-template-columns:1fr; }} table {{ display:block; overflow-x:auto; white-space:nowrap; }} .layout {{ gap:12px; }} }}
</style></head><body><div class="app-shell"><aside class="sidebar"><div class="sidebar-brand">DOT <span>NEWS</span></div><nav class="side-nav"><a class="{'active' if is_overview else ''}" href="/">Обзор</a><a class="{'active' if is_queue_view else ''}" href="/?view=queue">Очередь публикации</a><a class="{'active' if is_automation_view else ''}" href="/?view=automation">Автоматизация</a><a class="{'active' if is_tiktok_tab else ''}" href="/?tab=tiktok">TikTok Sandbox</a><a class="{'active' if is_settings_tab else ''}" href="/?tab=settings">Настройки</a></nav><div class="sidebar-foot">Локально · секреты в .env</div></aside><main>
<header><div class="brand"><h1>🔴 DOT NEWS <span>monitor</span></h1><p>Редакционный центр публикаций</p></div><div class="updated">Автообновление каждые 60 секунд</div></header>{tab_nav}{notice_html}
<div class="toolbar">{toolbar_html}</div>
{dashboard_content}
{results_content}</main></div>
<script>const privacySelect=document.getElementById('tiktok-privacy');const selectedPrivacy=document.getElementById('tiktok-selected-privacy');if(privacySelect&&selectedPrivacy){{privacySelect.addEventListener('change',()=>{{selectedPrivacy.textContent='Selected privacy: '+(privacySelect.value||'none');}});}}const returnUrl=window.location.pathname+window.location.search;document.querySelectorAll('form[method="post"]').forEach(form=>{{if(!form.querySelector('input[name="return_to"]')){{const input=document.createElement('input');input.type='hidden';input.name='return_to';input.value=returnUrl;form.appendChild(input);}}if(form.querySelector('input[name="action"][value="tiktok_prepare"]')){{form.addEventListener('submit',()=>{{const button=form.querySelector('button[type="submit"]');if(button){{button.disabled=true;button.setAttribute('aria-busy','true');button.textContent='⏳ Подготовка TikTok preview...';}}}});}}}});setInterval(()=>location.reload(),60000);</script></body></html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    db: Database
    sources_path: str
    bot_token: str | None
    channel_id: str | None
    tiktok_client_key: str
    tiktok_client_secret: str
    tiktok_redirect_uri: str
    tiktok_token_path: str
    tiktok_history_path: str
    tiktok_test_image_url: str
    tiktok_test_title: str
    tiktok_test_description: str
    tiktok_media_base_url: str
    tiktok_fallback_image: str
    tiktok_media_dir: str
    github_media_repo: str
    github_media_branch: str
    github_media_path: str
    github_token: str
    publication_interval_minutes: int
    web_username: str
    web_password: str

    def _require_auth(self, next_path: str) -> bool:
        if _is_authenticated(self):
            return True
        location = "/login?" + urlencode({"next": _safe_next(next_path)})
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()
        return False

    def _login_page(self, status: int = 200, message: str = "", next_path: str = "/") -> None:
        notice = f'<p class="error">{escape(message)}</p>' if message else ""
        body = f'''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>DOT NEWS — вход</title>
<style>body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#071017;color:#eef5f7;font:16px system-ui,sans-serif}}main{{width:min(360px,calc(100% - 32px));padding:28px;border:1px solid #19333d;border-radius:10px;background:#0d1b22;box-sizing:border-box}}h1{{margin:0 0 8px;font-size:24px}}p{{color:#8da5ad;margin:0 0 20px}}label{{display:block;color:#8da5ad;font-size:13px;margin:14px 0 6px}}input{{width:100%;box-sizing:border-box;padding:11px;border:1px solid #29434c;border-radius:6px;background:#071017;color:#eef5f7;font:inherit}}button{{width:100%;margin-top:20px;padding:11px;border:0;border-radius:6px;background:#24c6d8;color:#061014;font-weight:700;cursor:pointer}}.error{{color:#ff9b9b;margin:0 0 12px}}</style>
<main><h1>🔴 DOT NEWS</h1><p>Вход в редакционную панель</p>{notice}<form method="post" action="/login"><input type="hidden" name="next" value="{escape(_safe_next(next_path), quote=True)}"><label for="username">Логин</label><input id="username" name="username" autocomplete="username" required><label for="password">Пароль</label><input id="password" name="password" type="password" autocomplete="current-password" required><button type="submit">Войти</button></form></main></html>'''.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _login(self, form: dict[str, list[str]]) -> None:
        username = form.get("username", [""])[0]
        password = form.get("password", [""])[0]
        next_path = _safe_next(form.get("next", ["/"])[0])
        valid_user = hmac.compare_digest(username, self.web_username)
        valid_password = hmac.compare_digest(password, self.web_password)
        if not (valid_user and valid_password):
            self._login_page(401, "Неверный логин или пароль", next_path)
            return
        token = _session_token()
        with _web_sessions_lock:
            _web_sessions[token] = time.time() + 86400
        self.send_response(303)
        self.send_header("Set-Cookie", f"dot_news_session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=86400")
        self.send_header("Location", next_path)
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/login":
            query = parse_qs(parsed.query)
            self._login_page(next_path=_safe_next(query.get("next", ["/"])[0]))
            return
        if parsed.path == "/logout":
            token = _cookie_session(self)
            with _web_sessions_lock:
                if token:
                    _web_sessions.pop(token, None)
            self.send_response(303)
            self.send_header("Set-Cookie", "dot_news_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")
            self.send_header("Location", "/login")
            self.end_headers()
            return
        if not self._require_auth(self.path):
            return
        if parsed.path.startswith("/tiktok/media/"):
            self._serve_tiktok_media(parsed.path)
            return
        if parsed.path == "/auth/tiktok":
            self._start_tiktok_auth()
            return
        if parsed.path == "/auth/tiktok/callback":
            self._finish_tiktok_auth(parse_qs(parsed.query))
            return
        if parsed.path != "/":
            self.send_error(404)
            return
        query = parse_qs(parsed.query)
        notice = query.get("notice", [""])[0]
        category = query.get("category", [""])[0]
        search = query.get("search", [""])[0]
        tab = query.get("tab", ["dashboard"])[0]
        view = query.get("view", ["overview"])[0]
        preview_value = query.get("tiktok_preview", [""])[0]
        try:
            preview_id = int(preview_value) if preview_value else None
        except ValueError:
            preview_id = None
        if not preview_id and query.get("tab", [""])[0] == "preview":
            preview_id = _tiktok_latest_preview_id
        if preview_id:
            tab = "preview"
        body = render_dashboard(
            self.db,
            self.sources_path,
            notice,
            category,
            search,
            self.tiktok_client_key,
            self.tiktok_client_secret,
            self.tiktok_token_path,
            self.tiktok_history_path,
            self.tiktok_test_image_url,
            self.tiktok_test_title,
            self.tiktok_test_description,
            preview_id,
            self.scan_interval_minutes,
            self.publication_interval_minutes,
            self.telegram_auto_publish_score,
            self.tiktok_auto_publish_score,
            self.ranking_dry_run,
            self.tiktok_auto_publish_default,
            tab,
            view,
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_tiktok_media(self, path: str) -> None:
        name = unquote(path.removeprefix("/tiktok/media/")).strip("/")
        if not name or Path(name).name != name:
            self.send_error(404)
            return
        root = Path(self.tiktok_media_dir).resolve()
        candidate = (root / name).resolve()
        if root not in candidate.parents or not candidate.is_file():
            self.send_error(404)
            return
        payload = candidate.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _start_tiktok_auth(self) -> None:
        if not self.tiktok_client_key or not self.tiktok_client_secret:
            self._html_page(503, "TikTok Sandbox credentials are not configured")
            return
        try:
            location = create_authorization_url(self.tiktok_client_key, self.tiktok_redirect_uri)
        except ValueError:
            self._html_page(503, "TikTok OAuth is not configured")
            return
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def _finish_tiktok_auth(self, query: dict[str, list[str]]) -> None:
        if query.get("error"):
            self._html_page(400, "TikTok authorization was cancelled")
            return
        code = query.get("code", [""])[0]
        state = query.get("state", [""])[0]
        if not code or not state:
            self._html_page(400, "TikTok authorization response is incomplete")
            return
        try:
            exchange_code(
                self.tiktok_client_key,
                self.tiktok_client_secret,
                self.tiktok_redirect_uri,
                code,
                state,
                self.tiktok_token_path,
            )
        except Exception:
            logger.exception("TikTok OAuth callback failed")
            self._html_page(502, "TikTok authorization failed. Check the server log.")
            return
        self._html_page(200, "TikTok account connected successfully")

    def _html_page(self, status: int, message: str) -> None:
        body = (
            "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\">"
            f"<title>TikTok OAuth</title><body><h1>{escape(message)}</h1></body></html>"
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        global _tiktok_latest_preview_id
        path = urlparse(self.path).path
        if path == "/login":
            length = int(self.headers.get("Content-Length", "0"))
            form = parse_qs(self.rfile.read(length).decode("utf-8"))
            self._login(form)
            return
        if not self._require_auth(self.path):
            return
        if path not in {"/publish", "/action"}:
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        form = parse_qs(self.rfile.read(length).decode("utf-8"))
        self._return_to = form.get("return_to", [""])[0]
        try:
            if path == "/action":
                action = form.get("action", [""])[0]
                if action == "settings_save":
                    scan_interval = _setting_int(
                        self.db,
                        "scan_interval_minutes",
                        self.scan_interval_minutes,
                        minimum=1,
                    )
                    publication_interval = _setting_int(
                        self.db,
                        "publication_interval_minutes",
                        5,
                        minimum=0,
                    )
                    try:
                        requested_scan = int(form.get("scan_interval_minutes", [str(scan_interval)])[0])
                        requested_publication = int(form.get("publication_interval_minutes", [str(publication_interval)])[0])
                    except (TypeError, ValueError) as exc:
                        raise ValueError("Интервалы должны быть целыми числами") from exc
                    if not 1 <= requested_scan <= 1440:
                        raise ValueError("Интервал сканирования должен быть от 1 до 1440 минут")
                    if not 0 <= requested_publication <= 1440:
                        raise ValueError("Пауза публикаций должна быть от 0 до 1440 минут")
                    self.db.set_setting("scan_interval_minutes", str(requested_scan))
                    self.db.set_setting("publication_interval_minutes", str(requested_publication))
                    self._redirect("Настройки сохранены", tab="settings")
                    return
                if action == "skip":
                    article_id = int(form.get("article_id", [""])[0])
                    article = self.db.get_article(article_id)
                    if not article or article.status != "draft":
                        raise ValueError("Черновик уже обработан или не найден")
                    self.db.set_status(article.id, "skipped")
                    self._redirect("Новость пропущена")
                    return
                if action == "queue_delete":
                    article_id = int(form.get("article_id", [""])[0])
                    article = self.db.get_article(article_id)
                    if not article or article.status != "draft":
                        raise ValueError("Новость уже обработана или не найдена")
                    self.db.set_status(article.id, "skipped")
                    self.db.record_publication_event(
                        "queue",
                        article.id,
                        article.event_id,
                        "manual",
                        "REMOVED",
                        "removed_from_publication_queue",
                    )
                    self._redirect("Новость удалена из очереди", view="queue")
                    return
                if action == "auto_scan":
                    current = self.db.get_setting("auto_scan_enabled", "0") == "1"
                    self.db.set_setting("auto_scan_enabled", "0" if current else "1")
                    self._redirect("Часовой сбор включён" if not current else "Часовой сбор выключен")
                    return
                if action == "auto_publish":
                    current = self.db.get_setting("auto_publish_enabled", "0") == "1"
                    self.db.set_setting("auto_publish_enabled", "0" if current else "1")
                    self._redirect("Автопостинг включён" if not current else "Автопостинг выключен")
                    return
                if action == "auto_tiktok_publish":
                    current = self.db.get_setting(
                        "auto_tiktok_publish_enabled",
                        "1" if self.tiktok_auto_publish_default else "0",
                    ) == "1"
                    self.db.set_setting("auto_tiktok_publish_enabled", "0" if current else "1")
                    self._redirect("TikTok автопостинг включён" if not current else "TikTok автопостинг выключен")
                    return
                if action == "tiktok_prepare":
                    article_id = int(form.get("article_id", [""])[0])
                    article = self.db.get_article(article_id)
                    if not article:
                        raise ValueError("Новость не найдена")
                    publication = self.db.get_tiktok_publication(article_id)
                    if publication and normalize_publish_status(str(publication.get("status", ""))) == "PUBLISHED":
                        self._redirect("Already published to TikTok")
                        return
                    if not article.image_url:
                        self.db.set_tiktok_status(article.id, "SKIPPED_NO_IMAGE", "no_image")
                        self._redirect("TikTok: пропущено — нет фото")
                        return
                    try:
                        local_path, fallback_used = prepare_tiktok_image(
                            article.image_url,
                            article.id,
                            self.tiktok_fallback_image,
                            self.tiktok_media_dir,
                            allow_fallback=False,
                        )
                    except TikTokMediaError as exc:
                        reason = exc.code if exc.code in {"image_download_failed", "image_conversion_failed"} else "image_conversion_failed"
                        self.db.set_tiktok_status(article.id, "SKIPPED_NO_IMAGE", reason)
                        self._redirect(f"TikTok: пропущено — нет фото ({reason})")
                        return
                    event = self.db.get_event(article.event_id) if article.event_id else None
                    source_count = int(event.get("source_count", 1)) if event else 1
                    category = str(event.get("category", article.source_name)) if event else article.source_name
                    from telegram_bot import writer
                    tiktok_draft = writer.create_tiktok_caption(
                        article.original_title,
                        str(event.get("summary", "")) if event else (article.rewritten_post or article.original_title),
                        category,
                        article.importance_score,
                        source_count,
                    )
                    review_required = looks_untranslated_tiktok(
                        article.original_title,
                        tiktok_draft["title"],
                        f'{tiktok_draft["caption"]} {tiktok_draft["hashtags"]}',
                    )
                    public_url = ""
                    storage_note = storage_message(self.github_media_repo, self.github_token)
                    try:
                        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                        filename = (
                            f"event-{article.event_id}-{timestamp}.jpg"
                            if article.event_id
                            else f"article-{article.id}-{timestamp}.jpg"
                        )
                        public_url = publish_image_to_public_storage(
                            local_path,
                            self.tiktok_media_base_url,
                            self.github_media_repo,
                            self.github_media_branch,
                            self.github_media_path,
                            self.github_token,
                            filename,
                        ) or ""
                    except TikTokMediaError as exc:
                        storage_note = str(exc)
                    privacy_options: list[str] = []
                    privacy_error = ""
                    try:
                        settings = TikTokSettings(
                            self.tiktok_client_key,
                            self.tiktok_client_secret,
                            self.tiktok_token_path,
                            self.tiktok_history_path,
                        )
                        privacy_options = available_privacy_levels(get_creator_info(settings, force_refresh=True))
                    except Exception as exc:
                        privacy_error = str(exc)
                    if privacy_error:
                        storage_note = f"{storage_note} TikTok privacy options unavailable: {privacy_error}"
                    _tiktok_previews[article.id] = {
                        "article_id": article.id,
                        "title": tiktok_draft["title"],
                        "caption": tiktok_draft["caption"],
                        "hashtags": tiktok_draft["hashtags"],
                        "description": tiktok_draft["description"],
                        "local_path": local_path,
                        "public_url": public_url,
                        "fallback_used": fallback_used,
                        "storage_message": storage_note,
                        "privacy_options": privacy_options,
                        "review_required": review_required,
                        "review_note": "Manual review required: possible untranslated source headline." if review_required else "",
                    }
                    _tiktok_latest_preview_id = article.id
                    self.db.set_tiktok_status(article.id, "REVIEW_TRANSLATION" if review_required else None, "untranslated_source" if review_required else None)
                    self._redirect("TikTok preview ready", tiktok_preview=article.id)
                    return
                if action == "tiktok_confirm":
                    article_id = int(form.get("article_id", [""])[0])
                    privacy_level = form.get("privacy_level", [""])[0].strip()
                    preview = _tiktok_previews.get(article_id)
                    article = self.db.get_article(article_id)
                    if not preview or not article:
                        raise ValueError("TikTok preview истёк. Подготовьте новость ещё раз")
                    publication = self.db.get_tiktok_publication(article_id)
                    if publication and normalize_publish_status(str(publication.get("status", ""))) == "PUBLISHED":
                        _tiktok_previews.pop(article_id, None)
                        self._redirect("Already published to TikTok")
                        return
                    public_url = str(preview.get("public_url") or "")
                    if not public_url:
                        raise ValueError("Image prepared. Upload to verified media storage before TikTok publish.")
                    if preview.get("review_required"):
                        raise ValueError("TikTok text requires manual language review before publishing")
                    tiktok_title = str(preview.get("title", "")).strip()
                    tiktok_caption = str(preview.get("caption", "")).strip()
                    tiktok_hashtags = str(preview.get("hashtags", "")).strip()
                    if looks_untranslated_tiktok(article.original_title, tiktok_title, f"{tiktok_caption} {tiktok_hashtags}"):
                        self.db.set_tiktok_status(article.id, "REVIEW_TRANSLATION", "untranslated_source")
                        raise ValueError("TikTok text requires manual language review before publishing")
                    logger.info("TikTok article publish confirmed: article_id=%s privacy_level=%s", article_id, privacy_level[:50])
                    settings = TikTokSettings(
                        self.tiktok_client_key,
                        self.tiktok_client_secret,
                        self.tiktok_token_path,
                        self.tiktok_history_path,
                    )
                    result = publish_photo(
                        settings,
                        public_url,
                        tiktok_title,
                        str(preview.get("description", "")),
                        privacy_level,
                    )
                    self.db.create_tiktok_publication(
                        article.id,
                        article.event_id,
                        result["publish_id"],
                        tiktok_title,
                        tiktok_caption,
                        public_url,
                        privacy_level,
                        result["status"],
                        str(result.get("fail_reason", "")),
                    )
                    manual_status = normalize_publish_status(str(result.get("status") or "UNKNOWN"))
                    self.db.record_publication_event(
                        "tiktok",
                        article.id,
                        article.event_id,
                        "manual",
                        manual_status,
                        str(result.get("fail_reason", "")),
                        str(result.get("publish_id") or ""),
                    )
                    self.db.set_tiktok_status(article.id, None, None)
                    _tiktok_previews.pop(article_id, None)
                    self._redirect(f"TikTok publication created. Status: {result['status']}")
                    return
                if action == "tiktok_article_status":
                    article_id = int(form.get("article_id", [""])[0])
                    publication = self.db.get_tiktok_publication(article_id)
                    if not publication:
                        raise ValueError("TikTok publication for this article was not found")
                    settings = TikTokSettings(
                        self.tiktok_client_key,
                        self.tiktok_client_secret,
                        self.tiktok_token_path,
                        self.tiktok_history_path,
                    )
                    status_payload = get_post_status(settings, str(publication["publish_id"]))
                    raw_status, fail_reason = extract_status_details(status_payload)
                    status = normalize_publish_status(raw_status)
                    self.db.update_tiktok_publication(int(publication["id"]), status, fail_reason)
                    self.db.update_publication_event_status(str(publication["publish_id"]), status, fail_reason)
                    self.db.set_tiktok_status(article_id, None, None)
                    self._redirect(f"TikTok status updated: {status}" + (f" ({fail_reason})" if fail_reason else ""))
                    return
                if action == "tiktok_publish":
                    privacy_level = form.get("privacy_level", [""])[0].strip()
                    logger.info("TikTok web photo publish: selected_privacy_level=%s", privacy_level[:50] or "<empty>")
                    settings = TikTokSettings(
                        self.tiktok_client_key,
                        self.tiktok_client_secret,
                        self.tiktok_token_path,
                        self.tiktok_history_path,
                    )
                    result = publish_photo(
                        settings,
                        self.tiktok_test_image_url,
                        self.tiktok_test_title,
                        self.tiktok_test_description,
                        privacy_level,
                    )
                    self._redirect(f"TikTok тест отправлен. Статус: {result['status']}")
                    return
                if action == "tiktok_status":
                    history = load_publish_history(self.tiktok_history_path)
                    if not history:
                        raise ValueError("В истории TikTok пока нет публикаций")
                    publish_id = str(history[0].get("publish_id", ""))
                    settings = TikTokSettings(
                        self.tiktok_client_key,
                        self.tiktok_client_secret,
                        self.tiktok_token_path,
                        self.tiktok_history_path,
                    )
                    status_payload = get_post_status(settings, publish_id)
                    status, fail_reason = extract_status_details(status_payload)
                    update_publish_status(self.tiktok_history_path, publish_id, status, fail_reason)
                    self._redirect(f"Статус TikTok обновлён: {status}")
                    return
                if not self.bot_token or not self.channel_id:
                    self.send_error(503, "Telegram publishing is not configured")
                    return
                from telegram_bot import refresh_media, scan_sources
                from telegram_bot import search_news

                bot = Bot(self.bot_token)
                try:
                    if action == "scan":
                        created = asyncio.run(scan_sources(bot))
                        self._redirect(f"Сканирование завершено. Новых черновиков: {created}")
                        return
                    if action == "media":
                        updated = asyncio.run(refresh_media(bot))
                        self._redirect(f"Проверка фото завершена. Обновлено: {updated}")
                        return
                    if action == "search":
                        query = form.get("query", [""])[0]
                        created = asyncio.run(search_news(bot, query))
                        self._redirect(f"Поиск завершён. Новых черновиков: {created}", search=query)
                        return
                    raise ValueError("Неизвестное действие")
                finally:
                    asyncio.run(bot.session.close())
            if not self.bot_token or not self.channel_id:
                self.send_error(503, "Telegram publishing is not configured")
                return
            article_id = int(form.get("article_id", [""])[0])
            article = self.db.get_article(article_id)
            if not article or article.status != "draft":
                raise ValueError("Черновик уже опубликован или не найден")
            event = self.db.get_event(article.event_id) if article.event_id else None
            if article.decision == "DUPLICATE_UPDATE_SKIP" or (event and event.get("status") == "published" and article.decision != "UPDATE"):
                raise ValueError("Это семантический дубль уже опубликованной новости")
            text = f"{article.rewritten_post}\n\nИсточник: {article.original_url}"
            try:
                asyncio.run(_publish(self.bot_token, self.channel_id, article, text))
            except Exception:
                self.db.record_publication_event("telegram", article.id, article.event_id, "manual", "FAILED", "telegram_publish_failed")
                raise
            self.db.set_status(article.id, "published", mode="manual")
            self.db.record_publication_event("telegram", article.id, article.event_id, "manual", "PUBLISHED")
            if article.event_id:
                self.db.update_event(article.event_id, status="published", last_published_summary=article.rewritten_post or "")
            self._redirect("Новость опубликована")
        except Exception as exc:
            logger.exception("Web publication failed")
            self._redirect(f"Ошибка публикации: {exc}")

    def _redirect(
        self,
        notice: str,
        search: str = "",
        tiktok_preview: int | None = None,
        tab: str = "",
        view: str = "",
    ) -> None:
        params = {"notice": notice}
        if search:
            params["search"] = search
        if tiktok_preview:
            params["tiktok_preview"] = str(tiktok_preview)
        if tab:
            params["tab"] = tab
        elif tiktok_preview:
            params["tab"] = "preview"
        elif "tiktok" in notice.casefold():
            params["tab"] = "tiktok"
        if view:
            params["view"] = view
        return_to = getattr(self, "_return_to", "")
        if return_to.startswith("/") and not return_to.startswith("//"):
            target = urlparse(return_to)
            preserved = dict(parse_qsl(target.query, keep_blank_values=True))
            preserved.update(params)
            location = target.path or "/"
            if preserved:
                location += "?" + urlencode(preserved)
        else:
            location = "/?" + urlencode(params)
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        rendered = format % args if args else format
        if "/auth/tiktok/callback" in rendered:
            rendered = rendered.split(" /auth/tiktok/callback", 1)[0] + " /auth/tiktok/callback"
        logger.info("web: %s", rendered)


async def _publish(bot_token: str, channel_id: str, article: Article, text: str) -> None:
    bot = Bot(bot_token)
    try:
        if article.image_url:
            await bot.send_photo(channel_id, photo=article.image_url, caption=text)
        else:
            await bot.send_message(channel_id, text)
    finally:
        await bot.session.close()


def start_web_server(
    host: str,
    port: int,
    db: Database,
    sources_path: str,
    bot_token: str | None = None,
    channel_id: str | None = None,
    tiktok_client_key: str = "",
    tiktok_client_secret: str = "",
    tiktok_redirect_uri: str = "http://127.0.0.1:8080/auth/tiktok/callback",
    tiktok_token_path: str = ".tiktok_tokens.json",
    tiktok_history_path: str = ".tiktok_publish_history.json",
    tiktok_test_image_url: str = "",
    tiktok_test_title: str = "DOT News test photo",
    tiktok_test_description: str = "Test photo post from DOT News Sandbox",
    tiktok_media_base_url: str = "https://onfry2012.github.io/dot-news-legal/media/",
    tiktok_fallback_image: str = "assets/tiktok_fallback.jpg",
    tiktok_media_dir: str = ".tiktok_media",
    github_media_repo: str = "",
    github_media_branch: str = "main",
    github_media_path: str = "media",
    github_token: str = "",
    scan_interval_minutes: int = 5,
    publication_interval_minutes: int = 5,
    telegram_auto_publish_score: int = 80,
    tiktok_auto_publish_score: int = 85,
    ranking_dry_run: bool = True,
    tiktok_auto_publish_default: bool = False,
    web_username: str = "admin",
    web_password: str = "",
) -> Thread:
    handler = type(
        "ConfiguredDashboardHandler",
        (DashboardHandler,),
        {
            "db": db,
            "sources_path": sources_path,
            "bot_token": bot_token,
            "channel_id": channel_id,
            "tiktok_client_key": tiktok_client_key,
            "tiktok_client_secret": tiktok_client_secret,
            "tiktok_redirect_uri": tiktok_redirect_uri,
            "tiktok_token_path": tiktok_token_path,
            "tiktok_history_path": tiktok_history_path,
            "tiktok_test_image_url": tiktok_test_image_url,
            "tiktok_test_title": tiktok_test_title,
            "tiktok_test_description": tiktok_test_description,
            "tiktok_media_base_url": tiktok_media_base_url,
            "tiktok_fallback_image": tiktok_fallback_image,
            "tiktok_media_dir": tiktok_media_dir,
            "github_media_repo": github_media_repo,
            "github_media_branch": github_media_branch,
            "github_media_path": github_media_path,
            "github_token": github_token,
            "scan_interval_minutes": scan_interval_minutes,
            "publication_interval_minutes": publication_interval_minutes,
            "telegram_auto_publish_score": telegram_auto_publish_score,
            "tiktok_auto_publish_score": tiktok_auto_publish_score,
            "ranking_dry_run": ranking_dry_run,
            "tiktok_auto_publish_default": tiktok_auto_publish_default,
            "web_username": web_username,
            "web_password": web_password,
        },
    )
    server = ThreadingHTTPServer((host, port), handler)
    thread = Thread(target=server.serve_forever, name="dot-news-web", daemon=True)
    thread.start()
    return thread

