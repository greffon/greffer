"""Request-ID middleware (greffer-observability epic, Feature #4).

Pure-ASGI (NOT Starlette's BaseHTTPMiddleware): BaseHTTPMiddleware runs its
``dispatch`` in a different task context than the endpoint, so a contextvar set
there would not be visible to the route handler's logs. A pure-ASGI middleware
runs the downstream app in the SAME context where ``request_id_var`` is set, so
every log line a request produces carries its request id.

It propagates an inbound ``X-Request-ID`` (so a manager-originated action
correlates with the greffer-side compose run it triggers) or generates one, and
echoes it back on the response header.
"""
from __future__ import annotations

import re
from uuid import uuid4

from app.log_context import request_id_var

_HEADER = b"x-request-id"
# Allowlist for an accepted inbound request id. This is the security boundary:
# it rules out CR/LF (so the echoed response header can't be split — the prod
# runtime is httptools, which does NOT validate header values) and all control
# bytes (so a crafted id can't forge a line in text-format logs), and the cap
# bounds header/log size. A value that fails ANY check is replaced by a fresh
# id rather than partially scrubbed, so a forged id can't masquerade as chosen.
_RID_RE = re.compile(r"[A-Za-z0-9._-]+")
_RID_MAX_LEN = 128


class RequestIDMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = None
        for key, value in scope.get("headers", []):
            if key.lower() == _HEADER:
                incoming = value.decode("latin-1")
                break
        request_id = (
            incoming
            if incoming and len(incoming) <= _RID_MAX_LEN
            and _RID_RE.fullmatch(incoming)
            else uuid4().hex
        )
        token = request_id_var.set(request_id)

        async def send_with_header(message):
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                # Drop any echoed-through duplicate, then set ours.
                headers[:] = [(k, v) for k, v in headers
                              if k.lower() != _HEADER]
                headers.append((_HEADER, request_id.encode("latin-1")))
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            request_id_var.reset(token)
