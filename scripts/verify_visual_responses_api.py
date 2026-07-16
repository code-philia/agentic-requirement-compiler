from __future__ import annotations

import argparse
import base64
import mimetypes
import os
from pathlib import Path

from openai import OpenAI
from openai.types.responses import Response


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify Responses API return shape for image input.")
    parser.add_argument("image", help="Path to a local image file.")
    parser.add_argument("--base-url", default=os.getenv("VISUAL_BASE_URL") or os.getenv("OPENAI_BASE_URL") or os.getenv("OPENAI_API_BASE") or "")
    parser.add_argument("--api-key", default=os.getenv("VISUAL_API_KEY") or os.getenv("OPENAI_API_KEY") or "")
    parser.add_argument("--model", default=_normalize_model(os.getenv("VISUAL_MODEL") or os.getenv("MODEL") or "gpt-5.4"))
    args = parser.parse_args()

    if not args.base_url:
        raise SystemExit("Missing --base-url or VISUAL_BASE_URL/OPENAI_BASE_URL/OPENAI_API_BASE.")
    if not args.api_key:
        raise SystemExit("Missing --api-key or VISUAL_API_KEY/OPENAI_API_KEY.")

    image_path = Path(args.image).expanduser().resolve()
    if not image_path.exists():
        raise SystemExit(f"Image not found: {image_path}")

    mime_type, _ = mimetypes.guess_type(str(image_path))
    if not mime_type:
        mime_type = "image/png"
    data_url = f"data:{mime_type};base64,{base64.b64encode(image_path.read_bytes()).decode('utf-8')}"

    print(f"base_url={args.base_url}")
    print(f"model={args.model}")
    print(f"image={image_path}")
    print(f"mime_type={mime_type}")

    client = OpenAI(api_key=args.api_key, base_url=args.base_url)
    
    response = client.chat.completions.create(
        model=args.model,
        messages=[
            {
                "role": "system",
                "content": "You are a helpful assistant that describes images in one short sentence.",
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this image in one short sentence."},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ],
    )
    print(response.choices[0].message.content)
    
    # response = client.responses.create(
    #     model=args.model,
    #     instructions="Describe this image in one short sentence.",
    #     input=[
    #         {
    #             "role": "user",
    #             "content": "hello",
    #         }
    #     ],
    #     stream=False,
    # )

    # print("\n=== raw type ===")
    # print(type(response))

    # print("\n=== is official Response ===")
    # print(isinstance(response, Response))

    # print("\n=== repr prefix ===")
    # print(repr(response)[:3000])

    # print("\n=== fields ===")
    # for field in ("id", "object", "status", "error", "model", "output", "output_text"):
    #     value = getattr(response, field, None)
    #     print(f"{field}: {_short(value)}")

    # print("\n=== extracted text ===")
    # text = extract_response_text(response)
    # print(text if text else "<EMPTY>")


def extract_response_text(response: Response) -> str:
    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    parts: list[str] = []
    for item in getattr(response, "output", []) or []:
        content = getattr(item, "content", []) or []
        if not isinstance(content, list):
            continue
        for content_item in content:
            if getattr(content_item, "type", None) == "output_text":
                text = getattr(content_item, "text", None)
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
    return "\n".join(parts)


def _normalize_model(model: str) -> str:
    model = str(model or "").strip()
    if model.startswith("openai:"):
        return model.split(":", 1)[1].strip()
    return model


def _short(value: object, *, limit: int = 1200) -> str:
    text = repr(value)
    return text if len(text) <= limit else f"{text[:limit]}...<truncated>"


if __name__ == "__main__":
    main()
