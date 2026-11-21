"""A revocation while a reader is being authorized must take precedence."""
import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from multidict import MultiDict
from gateway import Gateway


class GrantLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_revoking_during_authorization_keeps_grant_closed(self):
        gateway = Gateway({"allowed_hosts": ["central.example.test"], "callback_token": "test-only"})
        stream = "ab" * 16
        grant = {"stream": stream, "host": "central.example.test", "cookie": "fixture",
                 "expires": time.monotonic() + 60, "clients": set()}
        gateway.grants["ticket"] = grant
        authorizing = asyncio.Event()
        finished = asyncio.Event()

        async def authorize(host, cookie, stream):
            authorizing.set()
            await finished.wait()
            return {"allowed": True}

        gateway.authorize = authorize
        request = SimpleNamespace(
            query=MultiDict({"secret": "test-only"}),
            json=AsyncMock(return_value={"action": "on_play", "app": "live", "stream": stream,
                                        "client_id": "reader", "param": "?ticket=ticket"}),
        )
        callback = asyncio.create_task(gateway.callback(request))
        await asyncio.wait_for(authorizing.wait(), 2)
        await gateway.revoke("ticket")
        finished.set()
        response = await asyncio.wait_for(callback, 2)
        self.assertEqual(response.status, 403)
        self.assertFalse(grant["clients"])
