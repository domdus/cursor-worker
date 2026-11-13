from __future__ import annotations

import base64
import re
from typing import Any

MAX_IMAGES = 4
MAX_BYTES = 4 * 1024 * 1024
_MIME = {"image/jpeg", "image/png", "image/webp", "image/gif"}


def parse_image(data: str, mime: str = "") -> dict[str, str] | None:
    """Accept base64 or a data: URI. Returns {mime, data} or None."""
    raw = str(data or "").strip()
    if raw.startswith("data:"):
        head, _, raw = raw.partition(",")
        mime = mime or head[5:].split(";")[0]
    mime = (mime or "image/jpeg").split(";")[0].strip().lower()
    if mime == "image/jpg":
        mime = "image/jpeg"
    if mime not in _MIME:
        return None
    try:
        blob = base64.b64decode(re.sub(r"\s+", "", raw), validate=False)
    except (ValueError, TypeError):
        return None
    if not blob or len(blob) > MAX_BYTES:
        return None
    return {"mime": mime, "data": base64.b64encode(blob).decode("ascii")}


def parse_images(items: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in items or []:
        if len(out) >= MAX_IMAGES:
            break
        if isinstance(item, str):
            parsed = parse_image(item)
        elif isinstance(item, dict):
            parsed = parse_image(str(item.get("data") or ""), str(item.get("mime") or ""))
        else:
            parsed = None
        if parsed:
            out.append(parsed)
    return out


def cursor_payload(text: str, images: list[dict[str, str]] | None) -> Any:
    if not images:
        return text
    packed = [{"data": item["data"], "mime_type": item["mime"]} for item in images]
    try:
        from cursor_sdk import SDKImage, UserMessage
    except ImportError:
        return {"text": text, "images": packed}
    factory = getattr(SDKImage, "data_image", None)
    if factory is None:
        return {"text": text, "images": packed}
    try:
        return UserMessage(text=text, images=[factory(item["data"], mime_type=item["mime_type"]) for item in packed])
    except Exception:
        return {"text": text, "images": packed}
