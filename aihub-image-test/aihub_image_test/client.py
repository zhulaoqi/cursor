from __future__ import annotations

from typing import Any

import requests


AIHUB_IMAGE_GENERATIONS_URL = "https://aihub.yeahmobi.com/v1/aigc/image/generations"
DEFAULT_MODEL = "gemini-3.1-flash-image"


def build_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def build_text_to_image_payload(
    prompt: str,
    *,
    model: str = DEFAULT_MODEL,
    aspect_ratio: str = "16:9",
    resolution: str = "1K",
    enable_google_search: bool = True,
    enable_web_search: bool = True,
) -> dict[str, Any]:
    return {
        "model": model,
        "prompt": prompt,
        "aspectRatio": aspect_ratio,
        "resolution": resolution,
        "enableGoogleSearch": enable_google_search,
        "enableWebSearch": enable_web_search,
    }


def build_image_edit_payload(
    prompt: str,
    *,
    image_url: str,
    mime_type: str = "image/jpeg",
    model: str = DEFAULT_MODEL,
    aspect_ratio: str = "1:1",
    resolution: str = "2K",
) -> dict[str, Any]:
    return {
        "model": model,
        "prompt": prompt,
        "images": [
            {"file_uri": image_url, "mime_type": mime_type},
        ],
        "aspectRatio": aspect_ratio,
        "resolution": resolution,
    }


def generate_image(api_key: str, payload: dict[str, Any], *, timeout: int = 120) -> Any:
    response = requests.post(
        AIHUB_IMAGE_GENERATIONS_URL,
        headers=build_headers(api_key),
        json=payload,
        timeout=timeout,
        allow_redirects=False,
    )
    if 300 <= response.status_code < 400:
        location = response.headers.get("Location", "<missing Location header>")
        raise RuntimeError(
            "API request was redirected. "
            f"Original endpoint may be routed as a web page. Location: {location}"
        )

    response.raise_for_status()
    data = response.json()
    return data["data"][0]
