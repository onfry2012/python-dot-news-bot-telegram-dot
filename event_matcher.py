from __future__ import annotations

from difflib import SequenceMatcher
import re

from config import Config
from database import Database


def _tokens(value: str) -> set[str]:
    return {word for word in re.findall(r"[\wа-яА-ЯąćęłńóśźżĄĆĘŁŃÓŚŹŻ]{3,}", value.casefold())}


def similarity(first: str, second: str) -> float:
    first_tokens = _tokens(first)
    second_tokens = _tokens(second)
    overlap = len(first_tokens & second_tokens) / max(len(first_tokens | second_tokens), 1)
    sequence = SequenceMatcher(None, first.casefold(), second.casefold()).ratio()
    return round(max(overlap, sequence * 0.85), 3)


def find_matching_event(db: Database, title: str, summary: str, config: Config) -> tuple[int, float] | None:
    incoming = f"{title}. {summary}"
    best: tuple[int, float] | None = None
    for candidate in db.recent_event_candidates(config.event_match_window_hours, config.event_match_max_candidates):
        if not candidate.get("event_id"):
            continue
        reference = f"{candidate.get('canonical_title') or candidate.get('original_title')}. {candidate.get('summary') or candidate.get('rewritten_post') or ''}"
        score = similarity(incoming, reference)
        if score >= config.event_similarity_threshold and (best is None or score > best[1]):
            best = (int(candidate["event_id"]), score)
    return best
