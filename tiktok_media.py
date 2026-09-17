from __future__ import annotations

import base64
from dataclasses import dataclass
from io import BytesIO
import logging
from pathlib import Path
import time
from urllib.parse import urljoin

import requests
from PIL import Image, ImageOps


logger = logging.getLogger(__name__)
MAX_IMAGE_EDGE = 1080
MAX_SOURCE_IMAGE_BYTES = 5 * 1024 * 1024
MAX_SOURCE_IMAGE_EDGE = 3000
Image.MAX_IMAGE_PIXELS = MAX_SOURCE_IMAGE_EDGE * MAX_SOURCE_IMAGE_EDGE


@dataclass
class TikTokMediaError(RuntimeError):
    code: str
    message: str

    def __str__(self) -> str:
        return self.message


def _read_source(source_image_url: str) -> bytes:
    if source_image_url.lower().startswith(("http://", "https://")):
        try:
            response = requests.get(
                source_image_url,
                timeout=30,
                headers={"User-Agent": "DOT-News/1.0"},
                stream=True,
            )
            response.raise_for_status()
            chunks: list[bytes] = []
            total = 0
            try:
                for chunk in response.iter_content(chunk_size=128 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_SOURCE_IMAGE_BYTES:
                        raise TikTokMediaError(
                            "image_download_failed",
                            "Фото новости слишком большое для обработки",
                        )
                    chunks.append(chunk)
            finally:
                response.close()
            return b"".join(chunks)
        except requests.RequestException as exc:
            raise TikTokMediaError("image_download_failed", "Не удалось скачать фото новости") from exc
    try:
        return Path(source_image_url).read_bytes()
    except OSError as exc:
        raise TikTokMediaError("image_download_failed", "Не удалось прочитать локальное фото новости") from exc


def _convert_to_jpeg(source: bytes, output_path: Path) -> None:
    try:
        with Image.open(BytesIO(source)) as original:
            if max(original.size) > MAX_SOURCE_IMAGE_EDGE:
                raise TikTokMediaError(
                    "image_conversion_failed",
                    "Фото новости имеет слишком большое разрешение для обработки",
                )
            image = ImageOps.exif_transpose(original).convert("RGB")
            image.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE), Image.Resampling.LANCZOS)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            image.save(output_path, format="JPEG", quality=90, optimize=True, exif=b"")
    except (OSError, ValueError) as exc:
        raise TikTokMediaError("image_conversion_failed", "Не удалось преобразовать фото в JPEG") from exc


def prepare_tiktok_image(
    source_image_url: str | None,
    article_id: int,
    fallback_image: str = "assets/tiktok_fallback.jpg",
    output_dir: str = ".tiktok_media",
    allow_fallback: bool = False,
) -> tuple[str, bool]:
    using_fallback = False
    if not source_image_url:
        if not allow_fallback:
            raise TikTokMediaError("no_image", "TikTok: пропущено — нет фото")
        source_image_url = fallback_image
        using_fallback = True
    output_path = Path(output_dir) / f"article-{article_id}.jpg"
    try:
        _convert_to_jpeg(_read_source(source_image_url), output_path)
    except TikTokMediaError as exc:
        if not allow_fallback:
            raise
        logger.warning("TikTok image preparation failed for article=%s code=%s; using fallback", article_id, exc.code)
        try:
            _convert_to_jpeg(_read_source(fallback_image), output_path)
        except TikTokMediaError as fallback_exc:
            raise TikTokMediaError("fallback_image_failed", "Не удалось подготовить fallback image") from fallback_exc
        return str(output_path), True
    return str(output_path), using_fallback


def storage_message(github_repo: str, github_token: str) -> str:
    if github_repo and github_token:
        return "Image uploaded to configured public media storage."
    return "Image prepared. Upload to verified media storage before TikTok publish."


def publish_image_to_public_storage(
    local_path: str,
    base_url: str,
    github_repo: str = "",
    github_branch: str = "main",
    github_media_path: str = "media",
    github_token: str = "",
    filename: str | None = None,
) -> str | None:
    if not github_repo or not github_token:
        return None
    path = Path(local_path)
    if not path.exists():
        raise TikTokMediaError("storage_file_missing", "Подготовленный JPEG не найден")
    if "/" not in github_repo or not base_url:
        raise TikTokMediaError("storage_not_configured", "Публичное хранилище TikTok настроено неполностью")
    filename = Path(filename or path.name).name
    if not filename.lower().endswith((".jpg", ".jpeg")):
        raise TikTokMediaError("storage_invalid_filename", "Имя TikTok-файла должно иметь расширение .jpg")
    repo_path = "/".join(part.strip("/") for part in (github_media_path, filename) if part.strip("/"))
    api_url = f"https://api.github.com/repos/{github_repo}/contents/{repo_path}"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {github_token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        existing = requests.get(api_url, headers=headers, params={"ref": github_branch}, timeout=30)
        try:
            if existing.ok:
                raise TikTokMediaError("storage_file_exists", f"Файл {filename} уже существует в GitHub storage")
            if existing.status_code != 404:
                raise TikTokMediaError("storage_upload_failed", "Не удалось проверить файл в GitHub storage")
        finally:
            existing.close()
        content = base64.b64encode(path.read_bytes()).decode("ascii")
        payload = {
            "message": f"Add DOT News TikTok image {filename}",
            "content": content,
            "branch": github_branch,
        }
        uploaded = requests.put(api_url, headers=headers, json=payload, timeout=30)
        try:
            if not uploaded.ok:
                raise TikTokMediaError("storage_upload_failed", "GitHub storage не принял изображение")
        finally:
            uploaded.close()
    except requests.RequestException as exc:
        raise TikTokMediaError("storage_upload_failed", "Не удалось загрузить изображение в public storage") from exc
    public_url = urljoin(base_url.rstrip("/") + "/", filename)
    last_status: int | None = None
    # GitHub Pages may deploy asynchronously after the API commit. Do not keep
    # the dashboard request open until the proxy returns 502; the UI exposes a
    # retry action for the remaining propagation time.
    for delay in (0, 2, 5, 8, 10):
        if delay:
            time.sleep(delay)
        try:
            check = requests.get(public_url, timeout=30, allow_redirects=True, stream=True)
            last_status = check.status_code
            check.close()
        except requests.RequestException:
            last_status = None
        if last_status == 200:
            logger.info("TikTok image uploaded and verified: filename=%s", filename)
            return public_url
    status_text = f"HTTP {last_status}" if last_status is not None else "сетевой сбой"
    raise TikTokMediaError(
        "storage_public_url_unavailable",
        f"Изображение загружено, но public URL пока не отвечает HTTP 200 ({status_text})",
    )
