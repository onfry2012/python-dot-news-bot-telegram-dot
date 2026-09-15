from __future__ import annotations

import json
import re
import time

from openai import OpenAI, OpenAIError

from news_fetcher import NewsItem


_POLISH_DIACRITICS = re.compile(r"[ąćęłńóśźżĄĆĘŁŃÓŚŹŻ]")
_POLISH_WORDS = {"jest", "dla", "nie", "został", "została", "który", "która", "oraz", "wciąż", "przed"}


def public_category_label(category: str) -> str:
    value = category.casefold()
    if "украин" in value and "поль" not in value:
        return "Новости Украины"
    if "украин" in value and "поль" in value:
        return "Польша и украинцы"
    if value == "мир":
        return "Новости мира"
    if value in {"технологии", "наука"}:
        return "Технологии и наука"
    if value in {"польша", "политика", "безопасность", "экономика"}:
        return "Новости Польши"
    return "Новости"


def looks_untranslated_tiktok(source_title: str, title: str, caption: str) -> bool:
    """Catch an obvious copy of a Polish headline before any publish request."""
    source = " ".join(source_title.casefold().split())
    output_title = " ".join(title.casefold().split())
    output = f"{output_title} {caption.casefold()}"
    cyrillic_count = sum(1 for char in output if "\u0400" <= char <= "\u04ff")
    if cyrillic_count == 0 or re.search(r"[іїєґІЇЄҐ]", output):
        return True
    title_latin_count = len(re.findall(r"[a-z]", output_title))
    title_cyrillic_count = sum(1 for char in output_title if "\u0400" <= char <= "\u04ff")
    if title_latin_count > 4 and title_cyrillic_count == 0:
        return True
    if source and (output_title == source or source in output_title):
        return True
    if _POLISH_DIACRITICS.search(source_title):
        if len(_POLISH_DIACRITICS.findall(title)) >= 2:
            return True
        polish_word_count = sum(1 for word in re.findall(r"[\wąćęłńóśźż]+", output) if word in _POLISH_WORDS)
        if polish_word_count >= 2 and sum(1 for char in output if "\u0400" <= char <= "\u04ff") < 8:
            return True
    return False


class AIWriter:
    def __init__(self, api_key: str, model: str, retry_count: int = 3) -> None:
        self.client = OpenAI(api_key=api_key)
        self.model = model
        self.retry_count = max(retry_count, 1)

    def _call(self, messages: list[dict], max_tokens: int):
        last_error: Exception | None = None
        for attempt in range(self.retry_count):
            try:
                return self.client.chat.completions.create(
                    model=self.model, messages=messages, temperature=0.5, max_tokens=max_tokens
                )
            except OpenAIError as exc:
                last_error = exc
                if attempt + 1 < self.retry_count:
                    time.sleep((2, 5, 10)[min(attempt, 2)])
        raise RuntimeError(f"OpenAI API error: {last_error}") from last_error

    def analyze(self, item: NewsItem) -> dict:
        prompt = f"""Проанализируй новость для DOT NEWS и верни только JSON без markdown.
Поля: event_type (breaking|important|useful|regular|low_value), audience_value (0-10),
is_clickbait (boolean), is_rumor (boolean), is_unverified (boolean), materially_new_facts (boolean),
reason (короткая строка).
Заголовок: {item.title}
Описание: {item.summary}
Категория: {item.category}"""
        try:
            response = self._call(
                [{"role": "system", "content": "Ты строгий классификатор новостей. Отвечай только валидным JSON."}, {"role": "user", "content": prompt}],
                180,
            )
            data = json.loads((response.choices[0].message.content or "{}").strip())
            if data.get("event_type") not in {"breaking", "important", "useful", "regular", "low_value"}:
                raise ValueError("invalid event_type")
            return data
        except (RuntimeError, ValueError, json.JSONDecodeError, TypeError):
            return {
                "event_type": "regular", "audience_value": 0, "is_clickbait": False,
                "is_rumor": False, "is_unverified": True, "materially_new_facts": False,
                "reason": "AI-анализ недоступен или вернул некорректный JSON", "analysis_failed": True,
            }

    def rewrite(self, item: NewsItem) -> str:
        prompt = f"""
Сделай короткий Telegram-пост на русском языке в стиле DOT NEWS.
Это новостной канал о Польше, политике, украинцах в Польше, безопасности,
экономике, важных мировых событиях, а также технологиях и науке.

Формат:
🔴 DOT NEWS

{{Заголовок}}

Коротко:
{{2-3 предложения простым языком; последнее предложение при необходимости кратко объясняет значение события}}

📰 {public_category_label(item.category)}
#{item.category} #DOTNEWS

Правила:
- максимум 900 символов;
- выделяй только действительно значимые новости, важные для людей или широко обсуждаемые;
- не преувеличивай значимость обычных заявлений, локальных происшествий и рекламы;
- отделяй подтверждённые факты от заявлений и оценок политиков;
- не делай вывод о популярности новости только по одному источнику;
- без кликбейта;
- не копируй статью целиком;
- не выдавай слухи за факт;
- если исходный текст на английском, переведи смысл, а не дословно;
- не используй жестокие или шокирующие подробности.
- не добавляй строку «Источник:» и не называй внутренние RSS-источники; используй только редакционную метку категории из шаблона.

Оригинальный заголовок: {item.title}
Описание: {item.summary}
Ссылка: {item.link}
""".strip()
        response = self._call(
            [{"role": "system", "content": "Ты редактор Telegram-канала DOT NEWS. Пиши кратко, спокойно и проверяемо."}, {"role": "user", "content": prompt}],
            450,
        )

        text = response.choices[0].message.content or ""
        text = text.strip()
        # Keep internal RSS names out of the public post and use a stable category label.
        text = re.sub(r"(?im)^\s*(?:источник|source)\s*:\s*.*(?:\n|$)", "", text)
        text = re.sub(r"(?is)\n*почему это важно\s*:\s*.*?(?=\n(?:📰|#)|$)", "", text).strip()
        category_label = f"📰 {public_category_label(item.category)}"
        if category_label not in text:
            hashtag_match = re.search(r"(?m)^\s*#", text)
            if hashtag_match:
                text = f"{text[:hashtag_match.start()].rstrip()}\n\n{category_label}\n{text[hashtag_match.start():].lstrip()}"
            else:
                text = f"{text}\n\n{category_label}"
        if len(text) > 900:
            text = text[:897].rstrip() + "..."
        return text

    def create_tiktok_caption(
        self,
        title: str,
        summary: str,
        category: str,
        importance_score: int,
        source_count: int,
    ) -> dict[str, str]:
        prompt = f"""Подготовь отдельный TikTok PHOTO-пост для DOT NEWS на основе исходных данных новости.
Верни только валидный JSON без markdown:
{{"title":"русский заголовок","caption":"1–2 коротких русских предложения","hashtags":["#русский_хештег","#DOTNEWS"]}}

Правила:
- переведи смысл на русский, а не дословно;
- создай новый русский заголовок, не копируй исходный заголовок;
- весь title, caption и каждый hashtag должны быть на русском языке или быть универсальным хештегом;
- смысл должен следовать только из исходных данных;
- без кликбейта, выдуманных фактов и непроверенных утверждений;
- не копируй Telegram-пост дословно;
- не используй URL, длинные ссылки и призывы подписаться;
- title до 90 символов, итоговый caption с хештегами до 420 символов;
- если исходник на польском, английском, украинском или другом языке, всё равно выдай весь результат на русском.

Заголовок исходной новости: {title}
Краткое содержание: {summary}
Категория: {category}
Важность: {importance_score}/100
Количество источников: {source_count}
""".strip()
        response = self._call(
            [
                {
                    "role": "system",
                    "content": (
                        "Ты редактор коротких новостных TikTok-постов DOT NEWS. "
                        "Return the entire TikTok post in Russian. Do not leave the original-language headline untranslated."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            320,
        )
        raw = (response.choices[0].message.content or "").strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE)
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError("OpenAI returned invalid TikTok JSON") from exc
        if not isinstance(data, dict):
            raise RuntimeError("OpenAI returned invalid TikTok draft")
        russian_title = str(data.get("title") or "").strip()
        russian_caption = str(data.get("caption") or "").strip()
        raw_hashtags = data.get("hashtags") or []
        if isinstance(raw_hashtags, str):
            hashtags = raw_hashtags.split()
        elif isinstance(raw_hashtags, list):
            hashtags = [str(value).strip() for value in raw_hashtags if str(value).strip()]
        else:
            hashtags = []
        hashtags = [value if value.startswith("#") else f"#{value}" for value in hashtags[:5]]
        hashtag_line = " ".join(hashtags)
        description = "\n".join(value for value in (russian_caption, hashtag_line) if value)
        if not russian_title or not russian_caption or not hashtags:
            raise RuntimeError("OpenAI returned an incomplete TikTok draft")
        if "http://" in description or "https://" in description:
            raise RuntimeError("TikTok draft unexpectedly contains a URL")
        if len(russian_title) > 90:
            russian_title = russian_title[:87].rstrip() + "..."
        if len(description) > 420:
            available_caption = max(20, 420 - len(hashtag_line) - 1)
            russian_caption = russian_caption[: max(available_caption - 3, 1)].rstrip() + "..."
            description = "\n".join(value for value in (russian_caption, hashtag_line) if value)
        return {
            "title": russian_title,
            "caption": russian_caption,
            "hashtags": hashtag_line,
            "description": description,
        }
