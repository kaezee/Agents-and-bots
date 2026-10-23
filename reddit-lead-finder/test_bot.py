import unittest
from datetime import datetime, timezone
from pathlib import Path

import bot

HERE = Path(__file__).resolve().parent

POSTS = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry>
  <author><name>/u/alice</name></author>
  <category term="nextjs" label="r/nextjs"/>
  <content type="html">&lt;!-- SC_OFF --&gt;&lt;div class=&quot;md&quot;&gt;&lt;p&gt;Launched last month. I don&amp;#39;t know if users like the new editor.&lt;/p&gt;&lt;/div&gt;&lt;!-- SC_ON --&gt; &amp;#32; submitted by &amp;#32; &lt;a href=&quot;https://www.reddit.com/user/alice&quot;&gt; /u/alice &lt;/a&gt;</content>
  <id>t3_aaa</id>
  <link href="https://www.reddit.com/r/nextjs/comments/aaa/x/"/>
  <published>2026-10-23T10:00:00+00:00</published>
  <title>How do you collect feedback?</title>
</entry>
<entry>
  <author><name>/u/bob</name></author>
  <category term="nextjs" label="r/nextjs"/>
  <content type="html">&lt;p&gt;Need a feedback widget for my React Native app&lt;/p&gt;</content>
  <id>t3_bbb</id>
  <link href="https://www.reddit.com/r/nextjs/comments/bbb/y/"/>
  <published>2026-10-23T10:01:00+00:00</published>
  <title>Feedback widget?</title>
</entry>
</feed>"""

COMMENTS = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry>
  <author><name>/u/carol</name></author>
  <category term="SaaS" label="r/SaaS"/>
  <content type="html">&lt;div class=&quot;md&quot;&gt;&lt;p&gt;Anyone tried Formbricks?&lt;/p&gt;&lt;/div&gt;</content>
  <id>t1_ccc</id>
  <link href="https://www.reddit.com/r/SaaS/comments/zzz/t/ccc/"/>
  <updated>2026-10-23T10:02:00+00:00</updated>
  <title>/u/carol on Best survey tools in 2026</title>
</entry>
</feed>"""


class BotTests(unittest.TestCase):
    def setUp(self):
        self.settings, products = bot.load_config(HERE / "products.toml")
        self.augur = products[0]

    def test_parse_posts(self):
        items = bot.parse_feed(POSTS, "nextjs", "new")
        self.assertEqual(len(items), 2)
        a = items[0]
        self.assertEqual((a.id, a.kind, a.subreddit, a.author), ("t3_aaa", "post", "nextjs", "alice"))
        self.assertEqual(a.body, "Launched last month. I don't know if users like the new editor.")
        self.assertEqual(a.published, datetime(2026, 10, 23, 10, tzinfo=timezone.utc))

    def test_parse_comment_title(self):
        c = bot.parse_feed(COMMENTS, "SaaS", "comments")[0]
        self.assertEqual((c.kind, c.title, c.body), ("comment", "Best survey tools in 2026", "Anyone tried Formbricks?"))

    def test_keyword_match_and_exclude(self):
        a, b = bot.parse_feed(POSTS, "nextjs", "new")
        m = bot.match_item(self.augur, a, "keyword")
        self.assertEqual((m.keyword, m.group), ("don't know if users", "the_problem"))
        self.assertIsNone(bot.match_item(self.augur, b, "keyword"))  # react native
        c = bot.parse_feed(COMMENTS, "SaaS", "comments")[0]
        self.assertEqual(bot.match_item(self.augur, c, "keyword").group, "competitors")

    def test_nps_is_whole_word(self):
        it = bot.Item("t3_x", "post", "SaaS", "Snapshots of our feedback widget", "", "", "", None)
        self.assertIsNotNone(bot.match_item(self.augur, it, "keyword"))
        it.title = "Feedback widget vs NPS"
        self.assertIsNone(bot.match_item(self.augur, it, "keyword"))

    def test_every_post_mode(self):
        it = bot.Item("t3_x", "post", "betatests", "Beta testers wanted", "my todo app", "", "", None)
        self.assertEqual(self.augur.watches("betatests", "new"), "every_post")
        self.assertIsNone(self.augur.watches("betatests", "comments"))
        self.assertIsNotNone(bot.match_item(self.augur, it, "every_post"))
        it.body = "built with Flutter"
        self.assertIsNone(bot.match_item(self.augur, it, "every_post"))

    def test_lovable_not_watched(self):
        self.assertIsNone(self.augur.watches("lovable", "new"))

    def test_feeds_deduplicated(self):
        feeds = bot.feeds_for([self.augur, self.augur])
        self.assertEqual(len(feeds), len(set(feeds)))
        self.assertEqual(len(feeds), 10 * 2 + 2)

    def test_payload(self):
        m = bot.match_item(self.augur, bot.parse_feed(POSTS, "nextjs", "new")[0], "keyword")
        e = bot.discord_payload(m)["embeds"][0]
        self.assertEqual(e["url"], "https://www.reddit.com/r/nextjs/comments/aaa/x/")
        self.assertIn("r/nextjs", [f["value"] for f in e["fields"]])


if __name__ == "__main__":
    unittest.main()
