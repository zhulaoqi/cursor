from __future__ import annotations

import argparse
import json
import os

from dotenv import load_dotenv

from aihub_image_test.client import (
    build_image_edit_payload,
    build_text_to_image_payload,
    generate_image,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Test AIHub Gemini image generation and image editing API.",
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("AIHUB_API_KEY"),
        help="AIHub API key. Defaults to AIHUB_API_KEY from environment or .env.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    text_parser = subparsers.add_parser("text", help="Run text-to-image request.")
    text_parser.add_argument(
        "--prompt",
        default="Latest Tesla Cybertruck driving through Las Vegas",
    )
    text_parser.add_argument("--aspect-ratio", default="16:9")
    text_parser.add_argument("--resolution", default="1K")
    text_parser.add_argument("--disable-google-search", action="store_true")
    text_parser.add_argument("--disable-web-search", action="store_true")

    edit_parser = subparsers.add_parser("edit", help="Run image editing request.")
    edit_parser.add_argument("--prompt", default="把背景替换为星空")
    edit_parser.add_argument("--image-url", required=True)
    edit_parser.add_argument("--mime-type", default="image/jpeg")
    edit_parser.add_argument("--aspect-ratio", default="1:1")
    edit_parser.add_argument("--resolution", default="2K")

    return parser.parse_args()


def main() -> None:
    load_dotenv()
    args = parse_args()

    if not args.api_key:
        raise SystemExit("Missing API key. Set AIHUB_API_KEY in .env or pass --api-key.")

    if args.command == "text":
        payload = build_text_to_image_payload(
            prompt=args.prompt,
            aspect_ratio=args.aspect_ratio,
            resolution=args.resolution,
            enable_google_search=not args.disable_google_search,
            enable_web_search=not args.disable_web_search,
        )
    else:
        payload = build_image_edit_payload(
            prompt=args.prompt,
            image_url=args.image_url,
            mime_type=args.mime_type,
            aspect_ratio=args.aspect_ratio,
            resolution=args.resolution,
        )

    try:
        result = generate_image(args.api_key, payload)
    except RuntimeError as exc:
        raise SystemExit(f"Request failed: {exc}") from None

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
