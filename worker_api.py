"""Registration of articles prepared by the optional local worker."""
import logging
from datetime import datetime, timezone

from database import Database, Article
from event_matcher import find_matching_event
from news_ranker import calculate_importance, decision as ranking_decision, importance_breakdown


logger = logging.getLogger(__name__)


def register_prepared_article(db: Database, config, payload: dict) -> tuple[str, Article | None]:
    """Store a locally prepared item while keeping Render as the source of truth."""
    url = str(payload.get("url", "")).strip()
    title = str(payload.get("title", "")).strip()
    if not url or not title:
        raise ValueError("url and title are required")
    if db.has_article(url):
        return "duplicate", None

    source_name = str(payload.get("source_name", "Local worker"))[:200]
    category = str(payload.get("category", "news"))[:100]
    summary = str(payload.get("summary", ""))[:5000]
    rewritten = str(payload.get("rewritten_post", "")).strip()
    if not rewritten:
        raise ValueError("rewritten_post is required")
    image_url = str(payload.get("image_url") or "").strip() or None
    source_weight = float(payload.get("source_weight", 1.0) or 1.0)
    analysis = payload.get("analysis") if isinstance(payload.get("analysis"), dict) else {}
    match = find_matching_event(db, title, summary, config)
    article_id = db.create_draft(source_name, url, title, rewritten, image_url)
    event_id: int | None = None
    if match:
        event_id, _ = match
        event = db.get_event(event_id) or {}
        source_count = int(event.get("source_count", 1))
        first_seen = str(event.get("first_seen_at", ""))
    else:
        source_count = 1
        first_seen = datetime.now(timezone.utc).isoformat()
    score = calculate_importance(
        source_count=source_count,
        category=category,
        first_seen_at=first_seen,
        source_weight=source_weight,
        event_type=str(analysis.get("event_type", "regular")),
        audience_value=int(analysis.get("audience_value", 0) or 0),
        is_clickbait=bool(analysis.get("is_clickbait")),
        is_rumor=bool(analysis.get("is_rumor")),
        is_unverified=bool(analysis.get("is_unverified")),
    )
    breakdown = importance_breakdown(
        source_count=source_count,
        category=category,
        first_seen_at=first_seen,
        source_weight=source_weight,
        event_type=str(analysis.get("event_type", "regular")),
        audience_value=int(analysis.get("audience_value", 0) or 0),
        is_clickbait=bool(analysis.get("is_clickbait")),
        is_rumor=bool(analysis.get("is_rumor")),
        is_unverified=bool(analysis.get("is_unverified")),
    )
    if event_id:
        db.attach_to_event(article_id, event_id, score)
        db.set_event_score_breakdown(event_id, breakdown)
    else:
        event_id = db.create_event(title, summary, category, score, article_id, breakdown)
    auto_enabled = db.get_setting("auto_publish_enabled", "0") == "1"
    final_decision = ranking_decision(
        score, auto_enabled, config.importance_auto_publish,
        config.importance_review_min, config.ranking_dry_run,
    )
    db.set_article_ranking(article_id, score, final_decision)
    logger.info(
        "Worker article registered: article=%s score=%s decision=%s has_image=%s",
        article_id,
        score,
        final_decision,
        bool(image_url),
    )
    return "created", db.get_article(article_id)
