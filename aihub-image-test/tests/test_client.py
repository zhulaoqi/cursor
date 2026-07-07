from aihub_image_test.client import (
    AIHUB_IMAGE_GENERATIONS_URL,
    build_headers,
    build_image_edit_payload,
    build_text_to_image_payload,
    generate_image,
)

import pytest


def test_build_headers_uses_bearer_token():
    assert build_headers("sk-aihub-test") == {
        "Authorization": "Bearer sk-aihub-test",
    }


def test_build_text_to_image_payload_matches_demo_defaults():
    payload = build_text_to_image_payload(
        prompt="Latest Tesla Cybertruck driving through Las Vegas",
    )

    assert AIHUB_IMAGE_GENERATIONS_URL == (
        "https://aihub.yeahmobi.com/v1/aigc/image/generations"
    )
    assert payload == {
        "model": "gemini-3.1-flash-image",
        "prompt": "Latest Tesla Cybertruck driving through Las Vegas",
        "aspectRatio": "16:9",
        "resolution": "1K",
        "enableGoogleSearch": True,
        "enableWebSearch": True,
    }


def test_build_image_edit_payload_includes_source_image():
    payload = build_image_edit_payload(
        prompt="把背景替换为星空",
        image_url="https://example.com/photo.jpg",
        mime_type="image/jpeg",
    )

    assert payload == {
        "model": "gemini-3.1-flash-image",
        "prompt": "把背景替换为星空",
        "images": [
            {"file_uri": "https://example.com/photo.jpg", "mime_type": "image/jpeg"},
        ],
        "aspectRatio": "1:1",
        "resolution": "2K",
    }


def test_generate_image_reports_redirect_without_following_it(monkeypatch):
    captured_kwargs = {}

    class RedirectResponse:
        status_code = 307
        headers = {"Location": "https://aihub.yeahmobi.com/zh-CN/v1/aigc/image/generations"}
        text = "https://aihub.yeahmobi.com/zh-CN/v1/aigc/image/generations"

        def raise_for_status(self):
            raise AssertionError("redirect response should be handled before raise_for_status")

    def fake_post(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return RedirectResponse()

    monkeypatch.setattr("aihub_image_test.client.requests.post", fake_post)

    with pytest.raises(RuntimeError, match="API request was redirected"):
        generate_image("sk-aihub-test", {"prompt": "test"})

    assert captured_kwargs["allow_redirects"] is False
