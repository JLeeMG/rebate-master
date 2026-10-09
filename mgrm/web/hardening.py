"""Protection applied to every request before any screen sees it.

- Records the caller's network address for the audit log.
- Refuses request bodies over MAX_REQUEST_BYTES before they are read, so a huge
  upload cannot exhaust the server, signed in or not.
- Adds the browser security headers: no framing by other sites (which stops an
  "Approve" button being clicked through a disguised page), no scripts or styles
  from anywhere but this platform, no content-type guessing, no caching of
  pages or evidence, and, when served over HTTPS, HTTPS only from then on.
"""

from mgrm.auth.users import request_address

MAX_REQUEST_BYTES = 25 * 1024 * 1024  # the largest evidence file is 20 MB

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'; object-src 'none'"
)
SECURITY_HEADERS = [
    (b"content-security-policy", CONTENT_SECURITY_POLICY.encode()),
    (b"x-frame-options", b"DENY"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"same-origin"),
    (b"cache-control", b"no-store"),
]
HTTPS_ONLY_HEADER = (b"strict-transport-security", b"max-age=31536000")
TOO_LARGE_PAGE = (
    b"<!doctype html><title>Too large</title><p>That upload is larger than 25 MB, so nothing was changed. "
    b"Evidence files can be up to 20 MB each.</p>"
)


class RequestTooLarge(Exception):
    pass


class Hardening:
    """Pure ASGI middleware, so the request body is limited as it streams in."""

    def __init__(self, app, https: bool) -> None:
        self.app = app
        self.headers = SECURITY_HEADERS + ([HTTPS_ONLY_HEADER] if https else [])

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        token = request_address.set(client[0] if client else None)
        try:
            declared = dict(scope.get("headers", [])).get(b"content-length")
            if declared is not None and (not declared.isdigit() or int(declared) > MAX_REQUEST_BYTES):
                await self._too_large(send)
                return
            await self._guarded(scope, receive, send)
        finally:
            request_address.reset(token)

    async def _guarded(self, scope, receive, send):
        received = 0
        started = False

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > MAX_REQUEST_BYTES:
                    raise RequestTooLarge
            return message

        async def send_with_headers(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                present = {name.lower() for name, _ in message.get("headers", [])}
                message["headers"] = list(message.get("headers", [])) + [h for h in self.headers if h[0] not in present]
            await send(message)

        try:
            await self.app(scope, limited_receive, send_with_headers)
        except RequestTooLarge:
            if not started:
                await self._too_large(send)

    async def _too_large(self, send):
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"text/html; charset=utf-8"), *self.headers]})
        await send({"type": "http.response.body", "body": TOO_LARGE_PAGE})
