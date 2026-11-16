from __future__ import annotations

import io
import json
import urllib.request
from email.message import Message
from types import SimpleNamespace
from urllib.response import addinfourl

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from plugin.plugins.neko_live.adapters.twitch_auth_service import _request_json
from plugin.plugins.neko_live.modules.bili_live_ingest import BiliLiveIngestModule
from plugin.plugins.neko_live.modules.bili_live_ingest.danmaku_core import DanmakuListener
from plugin.plugins.neko_live.modules.douyin_live_ingest.webcast import fetch_webcast_info


@pytest.mark.parametrize("provider", ["bilibili", "douyin"])
@pytest.mark.parametrize("status", [200, 301, 302, 303, 307, 308])
def test_room_lookup_stops_at_redirect(monkeypatch, provider, status):
    requests = []
    responses = []
    payload = {
        "code": 0,
        "data": {"room_info": {"room_id": 123, "live_status": 1}},
        "roomInfo": {"roomId": "7390000000000000000", "title": "test room"},
    }

    class HttpTransport(urllib.request.HTTPSHandler):
        def https_open(self, request):
            requests.append(request)
            headers = Message()
            headers["Content-Type"] = "application/json"
            headers["Location"] = "https://redirect.invalid/room"
            response = addinfourl(
                io.BytesIO(json.dumps(payload).encode()),
                headers,
                request.full_url,
                status if len(requests) == 1 else 200,
            )
            response.msg = "test response"
            responses.append(response)
            return response

    build_opener = urllib.request.build_opener
    monkeypatch.setattr(urllib.request, "_opener", None)
    monkeypatch.setattr(
        urllib.request,
        "build_opener",
        lambda *handlers: build_opener(*handlers, HttpTransport(), urllib.request.ProxyHandler({})),
    )
    if provider == "bilibili":
        module = BiliLiveIngestModule()
        module.ctx = SimpleNamespace(bili_credential=SimpleNamespace(sessdata="test-session"))
        result, _ = module._do_room_lookup(123, buvid3="")
    else:
        result = fetch_webcast_info("room-123", cookie="sessionid=test-session")

    assert len(requests) == 1
    assert "test-session" in requests[0].get_header("Cookie")
    assert result.ok is (status == 200)
    assert "test-session" not in result.message
    assert "redirect.invalid" not in result.message
    assert all(response.closed for response in responses)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["bilibili", "twitch_validate", "twitch_token"])
@pytest.mark.parametrize("status", [200, 301, 302, 303, 307, 308])
async def test_authenticated_http_stops_at_redirect(provider, status):
    requests = []
    payload = {"code": 0, "data": {}, "ok": True}

    async def handle(request):
        requests.append({
            "method": request.method,
            "cookie": request.cookies.get("SESSDATA"),
            "authorization": request.headers.get("Authorization"),
            "body": dict(await request.post()),
        })
        return web.json_response(
            payload,
            status=status if len(requests) == 1 else 200,
            headers={"Location": str(server.make_url("/redirected"))},
        )

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handle)
    async with TestServer(app, host="127.0.0.1") as server:
        url = str(server.make_url("/request"))
        if provider == "bilibili":
            listener = DanmakuListener(123)
            if status == 200:
                assert await listener._request_json(url, cookies={"SESSDATA": "test-session"}) == payload
            else:
                with pytest.raises(ValueError, match="redirect refused"):
                    await listener._request_json(url, cookies={"SESSDATA": "test-session"})
        elif provider == "twitch_validate":
            received_status, received_payload = await _request_json(
                "GET", url, headers={"Authorization": "OAuth test-access-token"},
            )
        else:
            received_status, received_payload = await _request_json(
                "POST", url, data={"refresh_token": "test-refresh-token"},
            )

    assert len(requests) == 1
    if provider == "bilibili":
        assert requests[0]["cookie"] == "test-session"
    else:
        assert received_status == status
        assert received_payload == (payload if status == 200 else {})
        if provider == "twitch_validate":
            assert requests[0]["authorization"] == "OAuth test-access-token"
        else:
            assert requests[0]["method"] == "POST"
            assert requests[0]["body"] == {"refresh_token": "test-refresh-token"}
