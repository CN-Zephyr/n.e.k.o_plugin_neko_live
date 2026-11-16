"""HTTP transport for credential-bearing requests to fixed provider endpoints."""

from __future__ import annotations

import urllib.error
import urllib.request


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def open_without_redirects(request: urllib.request.Request, *, timeout: float):
    """Keep cookies on the requested endpoint and close rejected responses."""
    opener = urllib.request.build_opener(_RejectRedirects())
    try:
        return opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        exc.close()
        raise
