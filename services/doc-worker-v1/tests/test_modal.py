"""Client-side test for the hosted granite-docling VLM on Modal.

Looks up the deployed web-server URL via the Modal SDK, then sends an
OpenAI-compatible chat-completion request with an image and streams the
transcription back to stdout.

Usage:
    python test_modal.py                                  # default test image + prompt
    python test_modal.py --image path/to/local.png        # local file (base64-encoded)
    python test_modal.py --image https://example.com/x.png
    python test_modal.py --prompt "Convert this image to Docling."
    python test_modal.py --no-stream                      # wait for full response
"""

import argparse
import asyncio
import base64
import json
import mimetypes
import sys
from pathlib import Path

import aiohttp
import modal

APP_NAME = "vlm-endpoint-docworker-v1"
MODEL_NAME = "ibm-granite/granite-docling-258M"

DEFAULT_IMAGE_URL = (
    "https://huggingface.co/ibm-granite/granite-docling-258M/resolve/main/assets/new_arxiv.png"
)
DEFAULT_PROMPT = "Convert this image to Docling."


def _build_image_part(image: str) -> dict:
    """Build an OpenAI `image_url` content part from a URL or local path."""
    if image.startswith(("http://", "https://", "data:")):
        return {"type": "image_url", "image_url": {"url": image}}

    path = Path(image).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"image not found: {path}")

    mime, _ = mimetypes.guess_type(path.name)
    mime = mime or "image/png"
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


async def _resolve_url() -> str:
    cls = modal.Cls.from_name(APP_NAME, "VllmServer")
    return await cls().serve.get_web_url.aio()


async def _stream(url: str, messages: list) -> None:
    payload = {"model": MODEL_NAME, "messages": messages, "stream": True}
    headers = {"Accept": "text/event-stream"}

    async with aiohttp.ClientSession(base_url=url) as session:
        async with session.post(
            "/v1/chat/completions", json=payload, headers=headers
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"HTTP {resp.status}: {body}")

            async for raw in resp.content:
                line = raw.decode("utf-8", errors="ignore").strip()
                if not line.startswith("data:"):
                    continue
                data = line[len("data:") :].strip()
                if data == "[DONE]":
                    break
                try:
                    evt = json.loads(data)
                except json.JSONDecodeError:
                    continue
                chunk = (evt.get("choices") or [{}])[0].get("delta", {}).get("content")
                if chunk:
                    print(chunk, end="", flush=True)
            print()


async def _one_shot(url: str, messages: list) -> None:
    payload = {"model": MODEL_NAME, "messages": messages, "stream": False}
    async with aiohttp.ClientSession(base_url=url) as session:
        async with session.post("/v1/chat/completions", json=payload) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"HTTP {resp.status}: {body}")
            data = await resp.json()
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    print(content)


async def run(image: str, prompt: str, stream: bool) -> None:
    url = await _resolve_url()
    print(f"Endpoint: {url}", file=sys.stderr)

    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {
            "role": "user",
            "content": [
                _build_image_part(image),
                {"type": "text", "text": prompt},
            ],
        },
    ]

    if stream:
        await _stream(url, messages)
    else:
        await _one_shot(url, messages)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default=DEFAULT_IMAGE_URL,
                        help="image URL, data: URI, or local path")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--no-stream", dest="stream", action="store_false",
                        help="wait for the full response instead of streaming")
    args = parser.parse_args()

    asyncio.run(run(args.image, args.prompt, args.stream))


if __name__ == "__main__":
    main()
