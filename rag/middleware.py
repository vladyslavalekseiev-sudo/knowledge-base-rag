"""Small HTTP protections for the public sample-data demo."""

import json
import math

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_REQUEST_BYTES = 8192


def validate_json(body: bytes) -> None:
    """Reject values that cannot safely pass through JSON error responses."""
    pending = [(json.loads(body.decode("utf-8")), 0)]
    while pending:
        value, depth = pending.pop()
        if depth > 64:
            raise ValueError("JSON nesting exceeds 64 levels.")
        if isinstance(value, str):
            value.encode("utf-8")  # Unpaired escaped surrogates are not valid Unicode text.
        elif isinstance(value, float) and not math.isfinite(value):
            raise ValueError("JSON numbers must be finite.")
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)
        elif isinstance(value, dict):
            pending.extend((item, depth + 1) for pair in value.items() for item in pair)


class HTTPProtections:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def secured_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend([
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-frame-options", b"DENY"),
                ])
                if scope["path"] == "/":
                    headers.append((b"content-security-policy", (
                        b"default-src 'self'; script-src 'self'; style-src 'self'; "
                        b"object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
                    )))
                message = {**message, "headers": headers}
            await send(message)

        if scope["method"] == "POST" and scope["path"] == "/api/ask":
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                if len(body) + len(chunk) > MAX_REQUEST_BYTES:
                    response = JSONResponse({"detail": "Request body exceeds 8192 bytes."}, status_code=413)
                    await response(scope, receive, secured_send)
                    return
                body.extend(chunk)
                if not message.get("more_body", False):
                    break

            content_type = dict(scope.get("headers", [])).get(b"content-type", b"").split(b";", 1)[0].strip().lower()
            if content_type and not (
                content_type == b"application/json"
                or content_type.startswith(b"application/") and content_type.endswith(b"+json")
            ):
                response = JSONResponse({"detail": "Use application/json for questions."}, status_code=415)
                await response(scope, receive, secured_send)
                return
            if body:
                try:
                    validate_json(bytes(body))
                except (ValueError, RecursionError):
                    response = JSONResponse({"detail": "Invalid JSON: use finite numbers, valid UTF-8 and at most 64 nesting levels."}, status_code=400)
                    await response(scope, receive, secured_send)
                    return

            async def replay() -> Message:
                return {"type": "http.request", "body": bytes(body), "more_body": False}

            await self.app(scope, replay, secured_send)
            return
        await self.app(scope, receive, secured_send)
