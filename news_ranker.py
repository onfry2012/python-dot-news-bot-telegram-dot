from __future__ import annotations

from datetime import datetime, timezone


def calculate_importance(
    *,
    source_count: int,
    category: str,
    first_seen_at: str,
    event_type: str = "regular",
    audience_value: int = 0,
    is_clickbait: bool = False,
    is_rumor: bool = False,
    is_unverified: bool = False,
    source_weight: float = 1.0,
) -> int:
    return int(importance_breakdown(
        source_count=source_count,
        category=category,
        first_seen_at=first_seen_at,
        event_type=event_type,
        audience_value=audience_value,
        is_clickbait=is_clickbait,
        is_rumor=is_rumor,
        is_unverified=is_unverified,
        source_weight=source_weight,
    )["total"])


def importance_breakdown(
    *,
    source_count: int,
    category: str,
    first_seen_at: str,
    event_type: str = "regular",
    audience_value: int = 0,
    is_clickbait: bool = False,
    is_rumor: bool = False,
    is_unverified: bool = False,
    source_weight: float = 1.0,
) -> dict[str, int]:
    source_points = {1: 5, 2: 15, 3: 25}.get(source_count, 35)
    weighted_source_points = round(source_points * max(0.75, min(source_weight, 1.2)))
    try:
        seen = datetime.fromisoformat(first_seen_at.replace("Z", "+00:00"))
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        hours = max((datetime.now(timezone.utc) - seen).total_seconds() / 3600, 0)
    except (ValueError, TypeError):
        hours = 48
    freshness = 15 if hours <= 1 else 12 if hours <= 3 else 8 if hours <= 6 else 4 if hours <= 12 else 0
    category_points = {
        "польша": 10,
        "украинцы в польше": 12,
        "украина": 12,
        "безопасность": 12,
        "экономика": 8,
        "политика": 8,
        "мир": 5,
        "технологии": 5,
    }.get(category.casefold(), 0)
    type_points = {"breaking": 25, "important": 18, "useful": 12, "regular": 5, "low_value": 0}.get(event_type, 5)
    penalties = (20 if is_rumor else 0) + (15 if is_clickbait else 0) + (20 if is_unverified else 0)
    audience_points = max(0, min(audience_value, 10))
    source_weight_points = weighted_source_points - source_points
    score = source_points + freshness + category_points + type_points + audience_points + source_weight_points - penalties
    total = max(0, min(100, int(score)))
    return {
        "sources": int(source_points),
        "freshness": int(freshness),
        "category": int(category_points),
        "event_type": int(type_points),
        "audience_value": int(audience_points),
        "source_weight": int(source_weight_points),
        "penalties": int(-penalties),
        "total": total,
    }


def decision(score: int, auto_publish: bool, auto_threshold: int, review_min: int, dry_run: bool = False) -> str:
    if score >= auto_threshold and auto_publish and not dry_run:
        return "AUTO_PUBLISH"
    if score >= review_min:
        return "REVIEW"
    return "LOW_PRIORITY"
