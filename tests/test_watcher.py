import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import watcher  # noqa: E402


class FakeNotifier:
    def __init__(self, result=True):
        self.result = result
        self.sent = []

    def send(self, title, body, **kwargs):
        self.sent.append((title, kwargs.get("priority")))
        return self.result

FIXTURE = """
<div class="tgme_widget_message js-widget_message" data-post="ServerHive/41">
  <a class="tgme_widget_message_owner_name" href="https://t.me/ServerHive"><span>ServerHive | Updates</span></a>
  <div class="tgme_widget_message_text js-message_text" dir="auto">First post<br>with a line break</div>
  <span class="tgme_widget_message_from_author" dir="auto">Shravan</span>
  <time datetime="2026-07-25T07:53:34+00:00" class="time">07:53</time>
</div>
<div class="tgme_widget_message js-widget_message" data-post="ServerHive/42">
  <div class="tgme_widget_message_text js-message_text" dir="auto">
    Giveaway alert: <a href="https://giveaway.serverhive.in/">claim here</a>
  </div>
  <span class="tgme_widget_message_from_author" dir="auto">Shravan</span>
  <time datetime="2026-07-25T07:57:26+00:00" class="time">07:57</time>
</div>
"""


class ParseChannelTests(unittest.TestCase):
    def test_parses_posts_in_order(self):
        posts = watcher.parse_channel_page(FIXTURE)
        self.assertEqual([post.post_id for post in posts], [41, 42])

    def test_keeps_text_and_links(self):
        posts = watcher.parse_channel_page(FIXTURE)
        self.assertEqual(posts[0].text, "First post\nwith a line break")
        self.assertEqual(posts[0].author, "Shravan")
        self.assertIn("https://giveaway.serverhive.in/", posts[1].links)
        self.assertTrue(watcher.is_giveaway(posts[1].text, posts[1].links))

    def test_bad_date_becomes_none(self):
        broken = FIXTURE.replace("2026-07-25T07:53:34+00:00", "not-a-date")
        posts = watcher.parse_channel_page(broken)
        self.assertIsNone(posts[0].date)


class DetectTests(unittest.TestCase):
    def test_keywords(self):
        for text in (
            "Giveaway alert",
            "Free server for two users",
            "2 winners will be chosen",
            "First come, first served",
            "Limited slots available",
        ):
            self.assertTrue(watcher.is_giveaway(text), text)

    def test_non_giveaway(self):
        for text in ("New pricing update", "ServerHive turns 2 today", ""):
            self.assertFalse(watcher.is_giveaway(text), text)

    def test_site_link_is_enough(self):
        self.assertTrue(
            watcher.is_giveaway("nothing here", ["https://giveaway.serverhive.in/"])
        )

    def test_visible_text_ignores_scripts(self):
        source = "<style>.x{color:red}</style><script>var nonce=1</script><p>Hello</p>"
        self.assertEqual(watcher.visible_text(source), "Hello")


class StateTests(unittest.TestCase):
    def test_text_dedupe(self):
        state = {}
        self.assertFalse(watcher.text_seen(state, "Giveaway alert"))
        watcher.mark_text(state, "Giveaway alert")
        self.assertTrue(watcher.text_seen(state, "  giveaway   alert  "))

    def test_empty_text_is_not_deduped(self):
        state = {}
        self.assertFalse(watcher.text_seen(state, ""))
        watcher.mark_text(state, "")
        self.assertFalse(watcher.text_seen(state, ""))


class NotifierHeaderTests(unittest.TestCase):
    def test_actions_format(self):
        value = watcher.ntfy_actions(
            ("Open Telegram", "https://t.me/ServerHive/42"),
            ("Giveaway page", "https://giveaway.serverhive.in/"),
        )
        self.assertIn("view, Open Telegram", value)
        self.assertIn("clear=true", value)
        self.assertIn("; ", value)


def post(post_id=2, text="Giveaway", age_hours=0):
    return watcher.Post(
        post_id=post_id,
        url=f"https://t.me/ServerHive/{post_id}",
        text=text,
        links=[],
        author="Shravan",
        owner="ServerHive",
        date=datetime.now(timezone.utc) - timedelta(hours=age_hours),
    )


class ChannelStateTests(unittest.TestCase):
    def test_failed_send_keeps_post_pending(self):
        state = {"channel_last_id": 1}
        with patch.object(watcher, "fetch_channel_posts", return_value=[post()]), patch.object(
            watcher, "send_channel_alert", return_value=False
        ):
            watcher.check_channel(state, FakeNotifier())
        self.assertEqual(state["channel_last_id"], 1)

    def test_successful_send_advances(self):
        state = {"channel_last_id": 1}
        with patch.object(watcher, "fetch_channel_posts", return_value=[post()]), patch.object(
            watcher, "send_channel_alert", return_value=True
        ):
            watcher.check_channel(state, FakeNotifier())
        self.assertEqual(state["channel_last_id"], 2)

    def test_baseline_does_not_replay_old_posts(self):
        state = {}
        old = post(post_id=1, text="old giveaway", age_hours=72)
        new = post(post_id=2, text="new pricing", age_hours=1)
        with patch.object(watcher, "fetch_channel_posts", return_value=[old, new]), patch.object(
            watcher, "send_channel_alert", return_value=True
        ) as send:
            watcher.check_channel(state, FakeNotifier())
        send.assert_not_called()
        self.assertEqual(state["channel_last_id"], 2)


class SiteStateTests(unittest.TestCase):
    def test_down_to_up_alerts_once(self):
        state = {"site": {"reachable": False, "hash": "old"}}
        notifier = FakeNotifier()
        with patch.object(
            watcher,
            "http_get",
            return_value=(200, b"<title>Giveaway</title><p>Claim now</p>", {}),
        ):
            watcher.check_site(state, notifier)
            self.assertEqual(len(notifier.sent), 1)
            self.assertTrue(state["site"]["reachable"])

            watcher.check_site(state, notifier)
            self.assertEqual(len(notifier.sent), 1)

    def test_failed_alert_does_not_advance_state(self):
        state = {"site": {"reachable": False, "hash": "old"}}
        notifier = FakeNotifier(result=False)
        with patch.object(
            watcher,
            "http_get",
            return_value=(200, b"<title>Giveaway</title><p>Claim now</p>", {}),
        ):
            watcher.check_site(state, notifier)
        self.assertEqual(state["site"], {"reachable": False, "hash": "old"})


if __name__ == "__main__":
    unittest.main()
