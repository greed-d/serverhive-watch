import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import live_watch  # noqa: E402


def response(status, page=""):
    return status, page.encode("utf-8"), {}


class SiteCheckTests(unittest.TestCase):
    def check(self, previous, status, page=""):
        with patch.object(
            live_watch.base,
            "http_get",
            side_effect=lambda *args, **kwargs: response(status, page),
        ):
            return asyncio.run(live_watch.check_site_once(previous))

    def test_502_is_baseline(self):
        state, alerts = self.check(None, 502, "bad gateway")
        self.assertFalse(state["reachable"])
        self.assertEqual(alerts, [])

    def test_first_live_response_alerts(self):
        state, alerts = self.check(None, 200, "<title>Giveaway</title><p>Live</p>")
        self.assertTrue(state["reachable"])
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["priority"], "urgent")
        self.assertIn("giveaway", alerts[0]["title"].lower())

    def test_down_to_up_transition_alerts(self):
        previous, _ = self.check(None, 502, "bad gateway")
        state, alerts = self.check(previous, 200, "<title>Giveaway</title><p>Live</p>")
        self.assertTrue(state["reachable"])
        self.assertEqual(len(alerts), 1)

    def test_content_change_respects_guard(self):
        previous, _ = self.check(None, 200, "<p>one</p>")
        previous["last_change_alert"] = __import__("time").time()
        _, alerts = self.check(previous, 200, "<p>two</p>")
        self.assertEqual(alerts, [])

        previous["last_change_alert"] = 0
        _, alerts = self.check(previous, 200, "<p>three</p>")
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["priority"], "high")


class FakeSender:
    def __init__(self, user_id, username, title=None, first_name="Test"):
        self.id = user_id
        self.username = username
        self.title = title
        self.first_name = first_name
        self.last_name = None


class FakeMessage:
    def __init__(self, message_id, text, date=None):
        self.id = message_id
        self.message = text
        self.date = date or live_watch.base.utcnow()
        self.action = None
        self.entities = []
        self.buttons = []
        self.web_preview = None


class FakeEvent:
    def __init__(self, chat_id, message, sender, sender_id):
        self.chat_id = chat_id
        self.message = message
        self.sender = sender
        self.sender_id = sender_id

    async def get_sender(self):
        return self.sender


class GroupHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        live_watch.RECENT_TEXT.clear()
        live_watch.RECENT_IDS.clear()
        live_watch.TARGET_ID = 123
        live_watch.CHANNEL_MARKED_ID = -100456
        live_watch.CHANNEL_OK = True
        self.sent = []

    def tearDown(self):
        live_watch.TARGET_ID = None
        live_watch.CHANNEL_MARKED_ID = None
        live_watch.CHANNEL_OK = False

    async def run_handler(self, event):
        async def fake_notify(title, body, **kwargs):
            self.sent.append((title, kwargs))

        with patch.object(live_watch, "notify", fake_notify):
            await live_watch.on_group(event)
            await asyncio.sleep(0)

    async def test_target_message_alerts(self):
        event = FakeEvent(
            -100999,
            FakeMessage(1, "Giveaway: free server"),
            FakeSender(123, "shra1v32"),
            123,
        )
        await self.run_handler(event)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("GIVEAWAY", self.sent[0][0])
        self.assertEqual(self.sent[0][1]["priority"], "urgent")

    async def test_irrelevant_member_does_not_alert(self):
        event = FakeEvent(
            -100999,
            FakeMessage(2, "hello"),
            FakeSender(999, "someone"),
            999,
        )
        await self.run_handler(event)
        self.assertEqual(self.sent, [])

    async def test_channel_post_is_skipped_when_channel_is_readable(self):
        event = FakeEvent(
            -100999,
            FakeMessage(3, "Giveaway: free server"),
            FakeSender(456, "serverhive", title="ServerHive"),
            -100456,
        )
        await self.run_handler(event)
        self.assertEqual(self.sent, [])

    async def test_third_party_site_link_alerts(self):
        event = FakeEvent(
            -100999,
            FakeMessage(4, "claim at https://giveaway.serverhive.in/"),
            FakeSender(999, "someone"),
            999,
        )
        await self.run_handler(event)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Giveaway link", self.sent[0][0])

    async def test_duplicate_text_is_skipped(self):
        event = FakeEvent(
            -100999,
            FakeMessage(5, "same text"),
            FakeSender(123, "shra1v32"),
            123,
        )
        await self.run_handler(event)
        second = FakeEvent(
            -100999,
            FakeMessage(6, "same text"),
            FakeSender(123, "shra1v32"),
            123,
        )
        await self.run_handler(second)
        self.assertEqual(len(self.sent), 1)


class FormatTests(unittest.TestCase):
    def test_post_url(self):
        self.assertEqual(
            live_watch._post_url("channel", 43),
            "https://t.me/ServerHive/43",
        )
        self.assertEqual(
            live_watch._post_url("group", 99),
            "https://t.me/ServerRentals/99",
        )


if __name__ == "__main__":
    unittest.main()
