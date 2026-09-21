from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from html import escape
import gc
import json
import logging
from pathlib import Path
import time
from urllib.parse import quote_plus

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ai_writer import AIWriter, looks_untranslated_tiktok
from config import load_config
from database import Article, Database
from media_fetcher import fetch_og_image
from news_fetcher import NewsItem, fetch_news, load_sources
from telegram_fetcher import fetch_telegram_news
from event_matcher import find_matching_event
from news_ranker import calculate_importance, decision as ranking_decision, importance_breakdown
from tiktok_media import TikTokMediaError, prepare_tiktok_image, publish_image_to_public_storage
from tiktok_publisher import TikTokAPIError, TikTokSettings, available_privacy_levels, get_creator_info, normalize_publish_status, publish_photo

try:
    import resource
except ImportError:  # Windows development environment
    resource = None


logger = logging.getLogger(__name__)
router = Router()

config = load_config()
db = Database(config.database_path)
writer = AIWriter(config.openai_api_key, config.openai_model, config.openai_retry_count)


def _rss_mb() -> float:
    """Return current process RSS in MB without adding a runtime dependency."""
    try:
        # Render/Linux exposes the current resident set size here. Unlike
        # ru_maxrss, this value is not a high-water mark.
        for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
            if line.startswith("VmRSS:"):
                return round(int(line.split()[1]) / 1024, 1)
    except (FileNotFoundError, OSError, ValueError, IndexError):
        pass
    if resource is None:
        return -1.0
    try:
        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    except (AttributeError, OSError):
        return -1.0


def admin_only(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id == config.admin_id)


def admin_callback(callback: CallbackQuery) -> bool:
    return bool(callback.from_user and callback.from_user.id == config.admin_id)


def draft_keyboard(article_id: int, has_image: bool = True, can_publish: bool = True) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if can_publish:
        builder.row(InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"publish:{article_id}"))
    else:
        builder.row(InlineKeyboardButton(text="⛔ Уже опубликовано", callback_data=f"duplicate:{article_id}"))
    builder.row(
        InlineKeyboardButton(text="🔁 Переписать", callback_data=f"rewrite:{article_id}"),
        InlineKeyboardButton(text="🖼 Без фото", callback_data=f"noimage:{article_id}"),
    )
    builder.row(InlineKeyboardButton(text="❌ Пропустить", callback_data=f"skip:{article_id}"))
    return builder.as_markup()


def control_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔎 Найти новости", callback_data="panel:scan"))
    builder.row(
        InlineKeyboardButton(text="📝 Черновики", callback_data="panel:drafts"),
        InlineKeyboardButton(text="📊 Статус", callback_data="panel:status"),
    )
    builder.row(
        InlineKeyboardButton(text="📰 Источники", callback_data="panel:sources"),
        InlineKeyboardButton(text="🖼 Проверить фото", callback_data="panel:media"),
    )
    scan_label = "⏸ Автоскан: ВКЛ" if auto_scan_enabled() else "▶ Автоскан: ВЫКЛ"
    publish_label = "⏸ Автопостинг: ВКЛ" if auto_publish_enabled() else "▶ Автопостинг: ВЫКЛ"
    tiktok_label = "⏸ TikTok автопост: ВКЛ" if tiktok_auto_publish_enabled() else "▶ TikTok автопост: ВЫКЛ"
    builder.row(InlineKeyboardButton(text=scan_label, callback_data="panel:auto_scan"))
    builder.row(InlineKeyboardButton(text=publish_label, callback_data="panel:auto_publish"))
    builder.row(InlineKeyboardButton(text=tiktok_label, callback_data="panel:auto_tiktok_publish"))
    return builder.as_markup()


def menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔎 Найти новости"), KeyboardButton(text="📝 Черновики")],
            [KeyboardButton(text="📊 Статус"), KeyboardButton(text="📰 Источники")],
            [KeyboardButton(text="🖼 Проверить фото"), KeyboardButton(text="⚙ Панель")],
            [KeyboardButton(text="⏱ Автоскан"), KeyboardButton(text="🤖 Автопостинг")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def auto_scan_enabled() -> bool:
    return db.get_setting("auto_scan_enabled", "1" if config.auto_scan_enabled else "0") == "1"


def auto_publish_enabled() -> bool:
    return db.get_setting("auto_publish_enabled", "0") == "1"


def tiktok_auto_publish_enabled() -> bool:
    return db.get_setting(
        "auto_tiktok_publish_enabled",
        "1" if config.tiktok_auto_publish_enabled else "0",
    ) == "1"


def _runtime_minutes(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(int(db.get_setting(name, str(default))), minimum)
    except (TypeError, ValueError):
        return max(default, minimum)


def get_scan_interval_minutes() -> int:
    return _runtime_minutes("scan_interval_minutes", config.scan_interval_minutes)


def get_publication_interval_minutes() -> int:
    return _runtime_minutes(
        "publication_interval_minutes",
        config.publication_interval_minutes,
        minimum=0,
    )


def automation_status() -> str:
    scan = "включён" if auto_scan_enabled() else "выключен"
    publish = "включён" if auto_publish_enabled() else "выключен"
    tiktok_publish = "включён" if tiktok_auto_publish_enabled() else "выключен"
    return (
        f"Автосбор по расписанию: {scan}\n"
        f"Автопубликация в канал: {publish}\n"
        f"Автопостинг TikTok: {tiktok_publish} (порог {config.tiktok_auto_publish_score}+)\n"
        f"Интервал сканирования: {get_scan_interval_minutes()} мин.\n"
        f"Пауза между публикациями: {get_publication_interval_minutes()} мин.\n"
        f"Telegram: до {config.max_telegram_auto_per_scan} за цикл, до {config.max_telegram_auto_per_hour} в час"
    )


async def send_admin_error(bot: Bot, text: str) -> None:
    logger.exception(text)
    await bot.send_message(config.admin_id, f"Ошибка: {text}")


async def send_draft(bot: Bot, article: Article) -> None:
    ranking = f"🔥 Важность: {article.importance_score}/100\n📰 Источников: {len(db.event_articles(article.event_id)) if article.event_id else 1}\n\n"
    caption = (
        f"{ranking}{escape(article.rewritten_post or '')}\n\n"
        f'<a href="{escape(article.original_url, quote=True)}">Оригинал статьи</a>'
    )
    event = db.get_event(article.event_id) if article.event_id else None
    duplicate_published = bool(event and event.get("status") == "published" and article.decision != "UPDATE")
    keyboard = draft_keyboard(
        article.id,
        has_image=bool(article.image_url),
        can_publish=article.decision != "DUPLICATE_UPDATE_SKIP" and not duplicate_published,
    )
    if article.image_url:
        try:
            await bot.send_photo(
                config.admin_id,
                photo=article.image_url,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
            return
        except Exception as exc:
            logger.warning("Could not send image for article %s: %s", article.id, exc)
    await bot.send_message(config.admin_id, caption, parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def create_draft_from_item(bot: Bot, item: NewsItem, ranking_auto_publish: bool | None = None) -> Article | None:
    if db.has_article(item.link):
        return None

    match = find_matching_event(db, item.title, item.summary, config)
    auto_publish_for_ranking = auto_publish_enabled() if ranking_auto_publish is None else ranking_auto_publish
    analysis = writer.analyze(item)
    if analysis.get("analysis_failed"):
        await bot.send_message(config.admin_id, f"AI-анализ недоступен для новости, оставляю её на ручную проверку:\n{item.title}")
    try:
        post = writer.rewrite(item)
    except RuntimeError as exc:
        await bot.send_message(config.admin_id, f"OpenAI недоступен для новости:\n{item.title}\n{item.link}\n\n{exc}")
        return None

    image_url = item.media_url or fetch_og_image(item.link)
    article_id = db.create_draft(item.source_name, item.link, item.title, post, image_url)
    if match:
        event_id, match_score = match
        event = db.get_event(event_id)
        db.attach_to_event(article_id, event_id, 0)
        event = db.get_event(event_id) or {}
        score = calculate_importance(
            source_count=int(event.get("source_count", 1)), category=item.category,
            first_seen_at=str(event.get("first_seen_at", "")), source_weight=item.source_weight,
            event_type=str(analysis.get("event_type", "regular")), audience_value=int(analysis.get("audience_value", 0) or 0),
            is_clickbait=bool(analysis.get("is_clickbait")), is_rumor=bool(analysis.get("is_rumor")), is_unverified=bool(analysis.get("is_unverified")),
        )
        breakdown = importance_breakdown(
            source_count=int(event.get("source_count", 1)), category=item.category,
            first_seen_at=str(event.get("first_seen_at", "")), source_weight=item.source_weight,
            event_type=str(analysis.get("event_type", "regular")), audience_value=int(analysis.get("audience_value", 0) or 0),
            is_clickbait=bool(analysis.get("is_clickbait")), is_rumor=bool(analysis.get("is_rumor")), is_unverified=bool(analysis.get("is_unverified")),
        )
        is_update = bool(analysis.get("materially_new_facts")) and event.get("status") == "published"
        if is_update:
            post = "🔴 ОБНОВЛЕНИЕ\n\n" + post
            db.update_draft(article_id, post, image_url)
            db.update_event(event_id, status="update_review", event_version=int(event.get("event_version", 1)) + 1)
        elif event.get("status") == "published":
            analysis["skip_duplicate"] = True
        db.attach_to_event(article_id, event_id, score)
        db.set_event_score_breakdown(event_id, breakdown)
        final_decision = "UPDATE" if is_update else ("DUPLICATE_UPDATE_SKIP" if analysis.get("skip_duplicate") else ("REVIEW" if analysis.get("analysis_failed") else ranking_decision(score, auto_publish_for_ranking, config.importance_auto_publish, config.importance_review_min, config.ranking_dry_run)))
        db.set_article_ranking(article_id, score, final_decision)
        logger.info("Semantic duplicate: article=%s event=%s similarity=%s score=%s", article_id, event_id, match_score, score)
    else:
        from datetime import datetime, timezone
        score = calculate_importance(
            source_count=1, category=item.category, first_seen_at=datetime.now(timezone.utc).isoformat(), source_weight=item.source_weight,
            event_type=str(analysis.get("event_type", "regular")), audience_value=int(analysis.get("audience_value", 0) or 0),
            is_clickbait=bool(analysis.get("is_clickbait")), is_rumor=bool(analysis.get("is_rumor")), is_unverified=bool(analysis.get("is_unverified")),
        )
        breakdown = importance_breakdown(
            source_count=1, category=item.category, first_seen_at=datetime.now(timezone.utc).isoformat(), source_weight=item.source_weight,
            event_type=str(analysis.get("event_type", "regular")), audience_value=int(analysis.get("audience_value", 0) or 0),
            is_clickbait=bool(analysis.get("is_clickbait")), is_rumor=bool(analysis.get("is_rumor")), is_unverified=bool(analysis.get("is_unverified")),
        )
        event_id = db.create_event(item.title, item.summary, item.category, score, article_id, breakdown)
        final_decision = "REVIEW" if analysis.get("analysis_failed") else ranking_decision(score, auto_publish_for_ranking, config.importance_auto_publish, config.importance_review_min, config.ranking_dry_run)
        db.set_article_ranking(article_id, score, final_decision)
        logger.info("Created event=%s article=%s score=%s", event_id, article_id, score)
    return db.get_article(article_id)


def recalculate_pending_drafts() -> tuple[int, int]:
    """Apply the current threshold to drafts without touching published articles."""
    recalculated = 0
    eligible = 0
    for article in db.list_by_status("draft", limit=500):
        event = db.get_event(article.event_id) if article.event_id else None
        if event and event.get("status") == "published" and article.decision != "UPDATE":
            final_decision = "DUPLICATE_UPDATE_SKIP"
        else:
            final_decision = ranking_decision(
                article.importance_score,
                auto_publish_enabled(),
                config.importance_auto_publish,
                config.importance_review_min,
                config.ranking_dry_run,
            )
        if final_decision != article.decision:
            recalculated += 1
        db.set_article_ranking(article.id, article.importance_score, final_decision)
        if final_decision in {"AUTO_PUBLISH", "UPDATE"}:
            eligible += 1
    logger.info("Recalculated pending drafts: changed=%s eligible=%s", recalculated, eligible)
    return recalculated, eligible


async def scan_sources(bot: Bot, send_drafts: bool = True, auto_publish: bool = False, fetch_new: bool = True) -> int:
    logger.info("Scan memory: stage=start rss_mb=%s", _rss_mb())
    created = 0
    if auto_publish:
        recalculate_pending_drafts()
    pending_articles = [article for article in db.list_by_status("draft", limit=500) if article.decision in {"AUTO_PUBLISH", "UPDATE"}] if auto_publish else []
    pending_ids = {article.id for article in pending_articles}
    new_articles: list[Article] = list(pending_articles)
    sources = load_sources(config.sources_path) if fetch_new else []
    source_count = len(sources)
    source_offset = int(db.get_setting("scan_source_offset", "0")) % max(source_count, 1)
    cycle_sources = [
        sources[(source_offset + index) % source_count]
        for index in range(min(config.scan_sources_per_cycle, source_count))
    ] if source_count else []
    db.set_setting("scan_source_offset", str((source_offset + len(cycle_sources)) % max(source_count, 1)))
    items_to_process: list[NewsItem] = []
    overflow_items: list[NewsItem] = []
    for source in cycle_sources:
        try:
            items = fetch_news(source, limit=config.scan_limit_per_source)
        except Exception as exc:
            await bot.send_message(config.admin_id, f"Не удалось прочитать RSS {source.name}: {exc}")
            continue
        # Take one lead item from every source first so one feed cannot
        # consume the whole cycle cap.
        if items:
            items_to_process.append(items[0])
            overflow_items.extend(items[1:])
        del items
        if len(items_to_process) >= config.scan_limit_total:
            break

    if len(items_to_process) < config.scan_limit_total:
        remaining = config.scan_limit_total - len(items_to_process)
        items_to_process.extend(overflow_items[:remaining])
    overflow_items.clear()

    # Telegram is an optional secondary source. Keep its contribution inside
    # the same hard scan cap so it cannot increase memory or OpenAI load.
    if len(items_to_process) < config.scan_limit_total:
        try:
            telegram_items = await fetch_telegram_news(config, db)
            items_to_process.extend(telegram_items[: config.scan_limit_total - len(items_to_process)])
            del telegram_items
        except Exception:
            logger.exception("Telegram channel collector failed")

    logger.info("Scan memory: stage=rss_loaded rss_mb=%s sources=%s", _rss_mb(), len(cycle_sources))
    gc.collect()

    for item in items_to_process:
        article = await create_draft_from_item(bot, item, ranking_auto_publish=auto_publish)
        if not article:
            continue
        new_articles.append(article)
        created += 1

    logger.info("Scan memory: stage=ai_and_ranking_done rss_mb=%s created=%s", _rss_mb(), created)
    items_to_process.clear()
    gc.collect()

    if not auto_publish:
        if send_drafts:
            for article in new_articles:
                if article.decision in {"REVIEW", "UPDATE"}:
                    await send_draft(bot, article)
        return created

    candidates = sorted(
        [article for article in new_articles if article.decision in {"AUTO_PUBLISH", "UPDATE"}],
        key=lambda article: (article.importance_score, article.id),
        reverse=True,
    )
    # Never publish two RSS versions of the same semantic event in one cycle.
    unique_candidates: list[Article] = []
    selected_event_ids: set[int] = set()
    for article in candidates:
        if article.event_id and article.event_id in selected_event_ids:
            continue
        unique_candidates.append(article)
        if article.event_id:
            selected_event_ids.add(article.event_id)
    hourly_remaining = max(
        config.max_telegram_auto_per_hour - db.count_recent_telegram_auto_published(1),
        0,
    )
    selected = unique_candidates[: min(config.max_telegram_auto_per_scan, hourly_remaining)]
    selected_ids = {article.id for article in selected}
    logger.info("Auto publish candidates selected: count=%s ids=%s", len(selected), [article.id for article in selected])
    tiktok_auto_count = 0
    for position, article in enumerate(selected):
        try:
            if position and get_publication_interval_minutes() > 0:
                await asyncio.sleep(get_publication_interval_minutes() * 60)
            await publish_article(bot, article)
            logger.info("Telegram auto publish succeeded: article=%s", article.id)
            db.set_status(article.id, "published", mode="auto")
            db.record_publication_event("telegram", article.id, article.event_id, "auto", "PUBLISHED")
            if article.event_id:
                db.update_event(article.event_id, status="published", last_published_summary=article.rewritten_post or "")
            if tiktok_auto_count < config.max_tiktok_auto_per_scan:
                tiktok_auto_count += 1
                tiktok_status, tiktok_reason = await asyncio.to_thread(publish_article_to_tiktok, article)
            else:
                tiktok_status, tiktok_reason = "NOT_ELIGIBLE", "tiktok_scan_limit"
            db.record_automation_run(
                article.id,
                article.event_id,
                article.importance_score,
                "PUBLISHED",
                tiktok_status,
                tiktok_reason,
            )
        except Exception as exc:
            db.record_publication_event("telegram", article.id, article.event_id, "auto", "FAILED", "telegram_publish_failed")
            db.record_automation_run(article.id, article.event_id, article.importance_score, "FAILED", "NOT_ELIGIBLE", "telegram_publish_failed")
            await bot.send_message(config.admin_id, f"Ошибка автопубликации #{article.id}: {exc}")

    for article in new_articles:
        if article.id in selected_ids:
            continue
        if article.id in pending_ids:
            continue
        if article.decision in {"AUTO_PUBLISH", "UPDATE"}:
            reason = "telegram_scan_limit"
            db.record_automation_run(article.id, article.event_id, article.importance_score, "DEFERRED", "NOT_ELIGIBLE", reason)
            if send_drafts:
                await send_draft(bot, article)
        elif article.decision == "REVIEW":
            db.record_automation_run(article.id, article.event_id, article.importance_score, "REVIEW", "NOT_ELIGIBLE", "below_telegram_threshold")
            if send_drafts:
                await send_draft(bot, article)
        else:
            db.record_automation_run(article.id, article.event_id, article.importance_score, "LOW_PRIORITY", "NOT_ELIGIBLE", "below_review_threshold")
    new_articles.clear()
    gc.collect()
    return created


def publish_article_to_tiktok(article: Article) -> tuple[str, str]:
    """Run the guarded, non-blocking-to-Telegram TikTok auto-publish pipeline."""
    if not tiktok_auto_publish_enabled():
        logger.info("TikTok auto publish decision: article=%s status=NOT_ELIGIBLE reason=disabled", article.id)
        return "NOT_ELIGIBLE", "disabled"
    if config.tiktok_auto_min_interval_minutes and db.has_recent_tiktok_auto_attempt(config.tiktok_auto_min_interval_minutes):
        logger.info(
            "TikTok auto publish decision: article=%s status=NOT_ELIGIBLE reason=cooldown",
            article.id,
        )
        return "NOT_ELIGIBLE", "tiktok_cooldown"
    if db.has_successful_tiktok_publication(article.id, article.event_id):
        logger.info("TikTok auto publish decision: article=%s status=NOT_ELIGIBLE reason=already_published", article.id)
        return "NOT_ELIGIBLE", "already_published"
    if not article.image_url:
        db.set_tiktok_status(article.id, "SKIPPED_NO_IMAGE", "no_image")
        db.record_publication_event("tiktok", article.id, article.event_id, "auto", "SKIPPED_NO_IMAGE", "no_image")
        logger.info("TikTok auto publish decision: article=%s status=SKIPPED_NO_IMAGE reason=no_image", article.id)
        return "SKIPPED_NO_IMAGE", "no_image"
    if article.importance_score < config.tiktok_auto_publish_score:
        logger.info("TikTok auto publish decision: article=%s status=NOT_ELIGIBLE reason=score_below_threshold", article.id)
        return "NOT_ELIGIBLE", "score_below_threshold"

    db.set_tiktok_status(article.id, "PROCESSING", None)
    logger.info("TikTok auto publish decision: article=%s status=PROCESSING score=%s", article.id, article.importance_score)
    try:
        local_path, _ = prepare_tiktok_image(
            article.image_url,
            article.id,
            config.tiktok_fallback_image,
            config.tiktok_media_dir,
            allow_fallback=False,
        )
        event = db.get_event(article.event_id) if article.event_id else None
        source_count = int(event.get("source_count", 1)) if event else 1
        category = str(event.get("category", article.source_name)) if event else article.source_name
        tiktok_draft = writer.create_tiktok_caption(
            article.original_title,
            str(event.get("summary", "")) if event else (article.rewritten_post or article.original_title),
            category,
            article.importance_score,
            source_count,
        )
        if looks_untranslated_tiktok(
            article.original_title,
            tiktok_draft["title"],
            f'{tiktok_draft["caption"]} {tiktok_draft["hashtags"]}',
        ):
            db.set_tiktok_status(article.id, "REVIEW_TRANSLATION", "untranslated_source")
            db.record_publication_event("tiktok", article.id, article.event_id, "auto", "REVIEW", "untranslated_source")
            logger.warning("TikTok auto publish skipped: article=%s reason=untranslated_source", article.id)
            return "REVIEW", "untranslated_source"

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        filename = f"event-{article.event_id}-{timestamp}.jpg" if article.event_id else f"article-{article.id}-{timestamp}.jpg"
        public_url = publish_image_to_public_storage(
            local_path,
            config.tiktok_media_base_url,
            config.github_media_repo,
            config.github_media_branch,
            config.github_media_path,
            config.github_token,
            filename,
        )
        if not public_url:
            raise TikTokMediaError("storage_not_configured", "TikTok public media storage is not configured")

        settings = TikTokSettings(
            config.tiktok_client_key,
            config.tiktok_client_secret,
            config.tiktok_token_path,
            config.tiktok_publish_history_path,
        )
        privacy_options = available_privacy_levels(get_creator_info(settings, force_refresh=True))
        if not privacy_options:
            raise TikTokAPIError("privacy_level_option_mismatch", "TikTok не вернул доступные privacy options")
        privacy_level = "SELF_ONLY" if "SELF_ONLY" in privacy_options else privacy_options[0]
        logger.info("TikTok auto publish privacy selected: article=%s privacy_level=%s", article.id, privacy_level)

        last_error: TikTokAPIError | None = None
        result: dict | None = None
        retryable = {"network_error", "rate_limit_exceeded", "access_token_invalid", "invalid_response"}
        for attempt in range(3):
            try:
                result = publish_photo(
                    settings,
                    public_url,
                    tiktok_draft["title"],
                    tiktok_draft["description"],
                    privacy_level,
                )
                break
            except TikTokAPIError as exc:
                last_error = exc
                logger.warning("TikTok auto publish failed: article=%s attempt=%s code=%s", article.id, attempt + 1, exc.code)
                if exc.code not in retryable or attempt == 2:
                    break
                time.sleep((2, 5, 10)[attempt])
        if result is None:
            error = last_error or TikTokAPIError("unknown", "TikTok auto publish failed")
            db.set_tiktok_status(article.id, "FAILED", error.code)
            db.record_publication_event("tiktok", article.id, article.event_id, "auto", "FAILED", error.code)
            logger.error("TikTok auto publish final status: article=%s status=FAILED reason=%s", article.id, error.code)
            return "FAILED", error.code

        raw_status = str(result.get("status") or "UNKNOWN").upper()
        status = normalize_publish_status(raw_status)
        fail_reason = str(result.get("fail_reason") or "")
        db.create_tiktok_publication(
            article.id,
            article.event_id,
            str(result["publish_id"]),
            tiktok_draft["title"],
            tiktok_draft["caption"],
            public_url,
            privacy_level,
            raw_status,
            fail_reason,
            mode="auto",
        )
        db.set_tiktok_status(article.id, status, fail_reason or None)
        db.record_publication_event("tiktok", article.id, article.event_id, "auto", status, fail_reason, str(result.get("publish_id") or ""))
        logger.info("TikTok auto publish final status: article=%s publish_id=%s status=%s", article.id, result["publish_id"], status)
        return status, fail_reason
    except TikTokMediaError as exc:
        status = "SKIPPED_NO_IMAGE" if exc.code in {"no_image", "image_download_failed", "image_conversion_failed"} else "FAILED"
        db.set_tiktok_status(article.id, status, exc.code)
        db.record_publication_event("tiktok", article.id, article.event_id, "auto", status, exc.code)
        logger.error("TikTok auto publish final status: article=%s status=%s reason=%s", article.id, status, exc.code)
        return status, exc.code
    except TikTokAPIError as exc:
        db.set_tiktok_status(article.id, "FAILED", exc.code)
        db.record_publication_event("tiktok", article.id, article.event_id, "auto", "FAILED", exc.code)
        logger.error("TikTok auto publish final status: article=%s status=FAILED reason=%s", article.id, exc.code)
        return "FAILED", exc.code
    except Exception:
        db.set_tiktok_status(article.id, "FAILED", "unexpected_error")
        db.record_publication_event("tiktok", article.id, article.event_id, "auto", "FAILED", "unexpected_error")
        logger.exception("TikTok auto publish final status: article=%s status=FAILED reason=unexpected_error", article.id)
        return "FAILED", "unexpected_error"


async def search_news(bot: Bot, query: str) -> int:
    query = " ".join(query.split())[:120]
    if not query:
        return 0
    source = load_sources(config.sources_path)[0]
    source = source.__class__(
        name=f"Поиск: {query}",
        url=f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=pl&gl=PL&ceid=PL:pl",
        category="поиск",
    )
    created = 0
    for item in fetch_news(source, limit=config.scan_limit_total):
        if created >= config.scan_limit_total:
            break
        article = await create_draft_from_item(bot, item)
        if article:
            await send_draft(bot, article)
            created += 1
    return created


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    if not admin_only(message):
        return
    await message.answer("DOT NEWS Bot готов.", reply_markup=menu_keyboard())
    await message.answer("Панель управления DOT NEWS", reply_markup=control_keyboard())


@router.message(Command("panel"))
async def cmd_panel(message: Message) -> None:
    if not admin_only(message):
        return
    await message.answer("Панель управления DOT NEWS", reply_markup=control_keyboard())


@router.message(F.text == "🔎 Найти новости")
async def menu_search(message: Message) -> None:
    if admin_only(message):
        await message.answer("Введите команду с темой поиска, например:\n/search Варшава")


@router.message(F.text == "📝 Черновики")
async def menu_drafts(message: Message, bot: Bot) -> None:
    if not admin_only(message):
        return
    drafts = db.list_by_status("draft", limit=10)
    if not drafts:
        await message.answer("Черновиков нет.", reply_markup=menu_keyboard())
        return
    for article in drafts:
        await send_draft(bot, article)


@router.message(F.text == "📊 Статус")
async def menu_status(message: Message) -> None:
    if admin_only(message):
        counts = db.counts_by_status()
        await message.answer(
            "Статус базы:\n"
            f"draft: {counts.get('draft', 0)}\n"
            f"published: {counts.get('published', 0)}\n"
            f"skipped: {counts.get('skipped', 0)}",
            reply_markup=menu_keyboard(),
        )


@router.message(F.text == "📰 Источники")
async def menu_sources(message: Message) -> None:
    if admin_only(message):
        sources = load_sources(config.sources_path)
        await message.answer(
            "\n".join(f"- {source.name} ({source.category})" for source in sources),
            reply_markup=menu_keyboard(),
        )


@router.message(F.text == "🖼 Проверить фото")
async def menu_media(message: Message, bot: Bot) -> None:
    if admin_only(message):
        updated = await refresh_media(bot)
        await message.answer(f"Проверка фото завершена. Обновлено черновиков: {updated}", reply_markup=menu_keyboard())


@router.message(F.text == "⚙ Панель")
async def menu_panel(message: Message) -> None:
    if admin_only(message):
        await message.answer("Панель управления DOT NEWS", reply_markup=control_keyboard())


@router.message(F.text == "⏱ Автоскан")
async def menu_auto_scan(message: Message) -> None:
    if not admin_only(message):
        return
    db.set_setting("auto_scan_enabled", "0" if auto_scan_enabled() else "1")
    await message.answer(automation_status(), reply_markup=control_keyboard())


@router.message(F.text == "🤖 Автопостинг")
async def menu_auto_publish(message: Message) -> None:
    if not admin_only(message):
        return
    db.set_setting("auto_publish_enabled", "0" if auto_publish_enabled() else "1")
    await message.answer(automation_status(), reply_markup=control_keyboard())


@router.message(Command("scan"))
async def cmd_scan(message: Message, bot: Bot) -> None:
    if not admin_only(message):
        return
    await message.answer("Сканирую RSS-источники...")
    created = await scan_sources(bot)
    await message.answer(f"Готово. Новых черновиков: {created}")


@router.message(Command("drafts"))
async def cmd_drafts(message: Message, bot: Bot) -> None:
    if not admin_only(message):
        return
    drafts = db.list_by_status("draft", limit=10)
    if not drafts:
        await message.answer("Черновиков нет.")
        return
    for article in drafts:
        await send_draft(bot, article)


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    if not admin_only(message):
        return
    counts = db.counts_by_status()
    await message.answer(
        "Статус базы:\n"
        f"draft: {counts.get('draft', 0)}\n"
        f"published: {counts.get('published', 0)}\n"
        f"skipped: {counts.get('skipped', 0)}"
    )


@router.message(Command("sources"))
async def cmd_sources(message: Message) -> None:
    if not admin_only(message):
        return
    sources = load_sources(config.sources_path)
    text = "\n".join(f"- {source.name} ({source.category}): {source.url}" for source in sources)
    await message.answer(text or "Источники не настроены.")


@router.message(Command("refreshmedia"))
async def cmd_refreshmedia(message: Message, bot: Bot) -> None:
    if not admin_only(message):
        return
    updated = await refresh_media(bot)
    await message.answer(f"Проверка фото завершена. Обновлено черновиков: {updated}")


async def refresh_media(bot: Bot) -> int:
    drafts = db.list_by_status("draft", limit=100)
    updated = 0
    for article in drafts:
        if article.image_url:
            continue
        image_url = fetch_og_image(article.original_url)
        if not image_url:
            continue
        db.update_draft(article.id, article.rewritten_post or "", image_url)
        refreshed = db.get_article(article.id)
        if refreshed:
            await send_draft(bot, refreshed)
            updated += 1
    return updated


@router.callback_query(F.data.startswith("panel:"))
async def cb_panel(callback: CallbackQuery, bot: Bot) -> None:
    if not admin_callback(callback):
        await callback.answer()
        return
    action = callback.data.split(":", 1)[1]
    if action == "scan":
        await callback.answer("Сканирование запущено")
        created = await scan_sources(bot)
        if callback.message:
            await callback.message.answer(f"Готово. Новых черновиков: {created}", reply_markup=control_keyboard())
        return
    if action == "drafts":
        await callback.answer()
        drafts = db.list_by_status("draft", limit=10)
        if not drafts and callback.message:
            await callback.message.answer("Черновиков нет.", reply_markup=control_keyboard())
        for article in drafts:
            await send_draft(bot, article)
        return
    if action == "status":
        await callback.answer()
        counts = db.counts_by_status()
        if callback.message:
            await callback.message.answer(
                "Статус базы:\n"
                f"draft: {counts.get('draft', 0)}\n"
                f"published: {counts.get('published', 0)}\n"
                f"skipped: {counts.get('skipped', 0)}",
                reply_markup=control_keyboard(),
            )
        return
    if action == "sources":
        await callback.answer()
        sources = load_sources(config.sources_path)
        text = "\n".join(f"- {source.name} ({source.category})" for source in sources)
        if callback.message:
            await callback.message.answer(text or "Источники не настроены.", reply_markup=control_keyboard())
        return
    if action == "media":
        await callback.answer("Проверяю фото")
        updated = await refresh_media(bot)
        if callback.message:
            await callback.message.answer(
                f"Проверка фото завершена. Обновлено черновиков: {updated}",
                reply_markup=control_keyboard(),
            )
        return
    if action == "auto_scan":
        db.set_setting("auto_scan_enabled", "0" if auto_scan_enabled() else "1")
        await callback.answer("Автосбор обновлён")
        if callback.message:
            await callback.message.answer(automation_status(), reply_markup=control_keyboard())
        return
    if action == "auto_publish":
        db.set_setting("auto_publish_enabled", "0" if auto_publish_enabled() else "1")
        await callback.answer("Автопостинг обновлён")
        if callback.message:
            await callback.message.answer(automation_status(), reply_markup=control_keyboard())
        return
    if action == "auto_tiktok_publish":
        db.set_setting("auto_tiktok_publish_enabled", "0" if tiktok_auto_publish_enabled() else "1")
        await callback.answer("TikTok автопостинг обновлён")
        if callback.message:
            await callback.message.answer(automation_status(), reply_markup=control_keyboard())
        return
    await callback.answer("Неизвестное действие", show_alert=True)


@router.message(Command("addsource"))
async def cmd_addsource(message: Message, command: CommandObject) -> None:
    if not admin_only(message):
        return
    if not command.args:
        await message.answer("Формат: /addsource Название | RSS-ссылка | категория")
        return
    parts = [part.strip() for part in command.args.split("|", 2)]
    if len(parts) != 3 or not parts[0] or not parts[1].startswith(("http://", "https://")):
        await message.answer("Формат: /addsource Название | RSS-ссылка | категория")
        return
    path = Path(config.sources_path)
    try:
        sources = json.loads(path.read_text(encoding="utf-8"))
        sources.append({"name": parts[0], "type": "rss", "url": parts[1], "category": parts[2] or "news"})
        path.write_text(json.dumps(sources, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:
        logger.exception("Could not add source")
        await message.answer(f"Не удалось добавить источник: {exc}")
        return
    await message.answer(f"Источник добавлен: {parts[0]}")


@router.message(Command("search"))
async def cmd_search(message: Message, command: CommandObject, bot: Bot) -> None:
    if not admin_only(message):
        return
    if not command.args:
        await message.answer("Формат: /search Варшава")
        return
    await message.answer(f"Ищу новости по теме: {command.args.strip()[:120]}")
    created = await search_news(bot, command.args)
    await message.answer(f"Поиск завершён. Новых черновиков: {created}", reply_markup=control_keyboard())


@router.callback_query(F.data.startswith("publish:"))
async def cb_publish(callback: CallbackQuery, bot: Bot) -> None:
    if not admin_callback(callback):
        await callback.answer()
        return
    article_id = int(callback.data.split(":", 1)[1])
    article = db.get_article(article_id)
    if not article or article.status != "draft":
        await callback.answer("Черновик не найден", show_alert=True)
        return
    event = db.get_event(article.event_id) if article.event_id else None
    if article.decision == "DUPLICATE_UPDATE_SKIP" or (event and event.get("status") == "published" and article.decision != "UPDATE"):
        await callback.answer("Это семантический дубль уже опубликованной новости", show_alert=True)
        return

    try:
        await publish_article(bot, article)
    except Exception as exc:
        db.record_publication_event("telegram", article.id, article.event_id, "manual", "FAILED", "telegram_publish_failed")
        await callback.answer("Не удалось опубликовать", show_alert=True)
        await bot.send_message(config.admin_id, f"Ошибка публикации #{article.id}: {exc}")
        return

    db.set_status(article.id, "published", mode="manual")
    db.record_publication_event("telegram", article.id, article.event_id, "manual", "PUBLISHED")
    if article.event_id:
        db.update_event(article.event_id, status="published", last_published_summary=article.rewritten_post or "")
    await callback.answer("Опубликовано")
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data.startswith("duplicate:"))
async def cb_duplicate(callback: CallbackQuery) -> None:
    if not admin_callback(callback):
        await callback.answer()
        return
    await callback.answer("Дубликат уже опубликован", show_alert=True)


@router.callback_query(F.data.startswith("rewrite:"))
async def cb_rewrite(callback: CallbackQuery, bot: Bot) -> None:
    if not admin_callback(callback):
        await callback.answer()
        return
    article_id = int(callback.data.split(":", 1)[1])
    article = db.get_article(article_id)
    if not article:
        await callback.answer("Черновик не найден", show_alert=True)
        return
    item = NewsItem(article.source_name, "news", article.original_title, "", article.original_url)
    try:
        post = writer.rewrite(item)
    except RuntimeError as exc:
        await bot.send_message(config.admin_id, f"OpenAI недоступен при переписывании #{article.id}: {exc}")
        await callback.answer("OpenAI недоступен", show_alert=True)
        return
    db.update_draft(article.id, post, article.image_url)
    updated = db.get_article(article.id)
    if updated:
        await send_draft(bot, updated)
    await callback.answer("Новый вариант отправлен")


@router.callback_query(F.data.startswith("noimage:"))
async def cb_noimage(callback: CallbackQuery) -> None:
    if not admin_callback(callback):
        await callback.answer()
        return
    article_id = int(callback.data.split(":", 1)[1])
    article = db.get_article(article_id)
    if not article:
        await callback.answer("Черновик не найден", show_alert=True)
        return
    db.update_draft(article.id, article.rewritten_post or "", None)
    await callback.answer("Фото убрано")


@router.callback_query(F.data.startswith("skip:"))
async def cb_skip(callback: CallbackQuery) -> None:
    if not admin_callback(callback):
        await callback.answer()
        return
    article_id = int(callback.data.split(":", 1)[1])
    article = db.get_article(article_id)
    if not article:
        await callback.answer("Черновик не найден", show_alert=True)
        return
    db.set_status(article.id, "skipped")
    await callback.answer("Пропущено")
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)


async def run_bot() -> None:
    bot = Bot(config.bot_token)
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Открыть меню"),
            BotCommand(command="panel", description="Панель управления"),
            BotCommand(command="scan", description="Найти новости"),
            BotCommand(command="search", description="Поиск по теме"),
            BotCommand(command="drafts", description="Показать черновики"),
            BotCommand(command="status", description="Статус базы"),
            BotCommand(command="sources", description="Список источников"),
        ]
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    scheduled_task = asyncio.create_task(scheduled_scan(bot))
    try:
        await dispatcher.start_polling(bot)
    finally:
        if scheduled_task:
            scheduled_task.cancel()
            await scheduled_task


async def scheduled_scan(bot: Bot) -> None:
    first_cycle = True
    while True:
        if not first_cycle:
            await asyncio.sleep(get_scan_interval_minutes() * 60)
        first_cycle = False
        if not auto_scan_enabled():
            continue
        try:
            publish = auto_publish_enabled()
            # When the local worker is healthy it performs RSS/OpenAI work.
            # Render only drains the central publication queue. If the worker
            # disappears, the normal bounded emergency scan resumes.
            local_worker_online = config.worker_enabled and db.worker_is_online(config.worker_offline_after_minutes)
            created = await scan_sources(
                bot,
                send_drafts=not publish,
                auto_publish=publish,
                fetch_new=not local_worker_online,
            )
            mode = "опубликовано" if publish else "черновиков"
            if not local_worker_online or created:
                await bot.send_message(config.admin_id, f"Автоматическая подборка готова: {created} {mode}.\n\n{automation_status()}")
            logger.info("Scheduled scan created %s items, auto publish=%s worker_online=%s", created, publish, local_worker_online)
        except Exception:
            logger.exception("Scheduled scan failed")


async def publish_article(bot: Bot, article: Article) -> None:
    text = (
        f"{escape(article.rewritten_post or '')}\n\n"
        f'<a href="{escape(article.original_url, quote=True)}">Источник</a>'
    )
    last_error: Exception | None = None
    for attempt in range(max(config.http_retry_count, 1)):
        try:
            if article.image_url:
                try:
                    await asyncio.wait_for(
                        bot.send_photo(config.channel_id, photo=article.image_url, caption=text, parse_mode=ParseMode.HTML),
                        timeout=30,
                    )
                except Exception as image_exc:
                    # A broken or slow source image must not block the whole
                    # scheduler. Preserve the news as a text post instead.
                    logger.warning("Telegram image failed for article=%s: %s; sending text fallback", article.id, image_exc)
                    await asyncio.wait_for(
                        bot.send_message(config.channel_id, text, parse_mode=ParseMode.HTML),
                        timeout=30,
                    )
            else:
                await asyncio.wait_for(
                    bot.send_message(config.channel_id, text, parse_mode=ParseMode.HTML),
                    timeout=30,
                )
            return
        except Exception as exc:
            last_error = exc
            if attempt + 1 < max(config.http_retry_count, 1):
                await asyncio.sleep((2, 5, 10)[min(attempt, 2)])
    raise RuntimeError(f"Telegram publish failed after retries: {last_error}") from last_error
