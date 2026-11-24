"""Offline regression tests for SB Radar: no network, no alerts, no secrets.
Run: python -m unittest discover -s tests -v"""
import importlib.util, os, sys, unittest
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("tracker", os.path.join(ROOT, "tracker.py"))
t = importlib.util.module_from_spec(spec); spec.loader.exec_module(t)


class Resp:
    def __init__(self, status=200, text="", data=None):
        self.status_code, self.text, self._data = status, text, data
    def json(self):
        return self._data


def fresh_cache():
    return {"seen": set(), "shopify": {}, "seeded_stores": set(), "stock": {}, "oos": {},
            "avail": {}, "muted": {}, "msgs": {}, "news": []}


def rss(*items):
    body = "".join(
        f"<item><title><![CDATA[{ti}]]></title><link>{li}</link>"
        f"<description><![CDATA[{de}]]></description><pubDate>{pu}</pubDate></item>"
        for ti, li, de, pu in items)
    return f'<?xml version="1.0"?><rss><channel><title>x</title>{body}</channel></rss>'


NOW = format_datetime(datetime.now(timezone.utc) - timedelta(hours=3))
OLD = format_datetime(datetime.now(timezone.utc) - timedelta(days=20))


class ClassifyKnownDrops(unittest.TestCase):
    """Every wording shops/resellers used for recent SB Dunks must read as an SB Dunk
    (#sb-drops + email), not as a regular Dunk (Discord only) or nothing."""
    TITLES = [
        "Nike SB Dunk Low Ducks of a Feather Oregon Ducks The Zoo",       # StockX / RIF
        "Ducks of a Feather x Nike SB Dunk Low 'The Zoo'",
        "Dunk Low SB 'Ducks of a Feather - The Zoo'",                       # resale naming
        "Nike Dunk Low Pro SB Shout",                                       # 'SB' after model
        "Nike SB Dunk Low Pro - Tomatillo/Gorge Green-Gum Medium Brown",
    ]

    def test_sb_dunk_titles(self):
        for title in self.TITLES:
            with self.subTest(title=title):
                self.assertEqual(t.feed_for(title, ""), "dunks")
                self.assertTrue(t.is_sb_shoe(title, ""))

    def test_non_sb_still_not_sb(self):
        for title in ["Nike Dunk Low Retro Panda", "Nike Air Max 1 Ducks of a Feather Tokyo",
                      "Nike SB Dunk Low Ducks of a Feather Hoodie"]:
            with self.subTest(title=title):
                self.assertFalse(t.feed_for(title, "Nike") == "dunks" and t.is_sb(title)
                                 and not t.is_apparel(title))


class ShopifyDetection(unittest.TestCase):
    def test_cached_no_is_rechecked_and_reseeded(self):
        cache = fresh_cache()
        cache["shopify"]["https://jerics.com"] = False     # stuck from one old timeout
        with mock.patch.object(t.requests, "get", return_value=Resp(200, '{"products": []}')):
            self.assertTrue(t.is_shopify("https://jerics.com", cache))
        self.assertTrue(cache["shopify"]["https://jerics.com"])
        self.assertIn("https://jerics.com", cache["_reseed"])

    def test_cached_yes_needs_no_request(self):
        cache = fresh_cache(); cache["shopify"]["https://kith.com"] = True
        with mock.patch.object(t.requests, "get", side_effect=AssertionError("no request")):
            self.assertTrue(t.is_shopify("https://kith.com", cache))

    def test_timeout_is_not_cached_forever(self):
        cache = fresh_cache()
        with mock.patch.object(t.requests, "get", side_effect=t.requests.Timeout()):
            self.assertFalse(t.is_shopify("https://slow.example", cache))
        with mock.patch.object(t.requests, "get", return_value=Resp(200, '{"products": []}')):
            self.assertTrue(t.is_shopify("https://slow.example", cache))


class ShopifyDropAtSkateShop(unittest.TestCase):
    """A skate shop lists the DOAF Zoo on its Shopify store -> an SB Dunk alert."""
    def test_doaf_listing_alerts(self):
        published = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        product = {"id": 987, "title": "Nike SB Dunk Low x Ducks of a Feather 'The Zoo'",
                   "handle": "nike-sb-dunk-low-doaf-the-zoo-jj7565-300", "vendor": "Nike SB",
                   "product_type": "Shoes", "published_at": published,
                   "variants": [{"option1": "10", "available": True, "price": "235.00", "sku": "JJ7565-300-10"},
                                {"option1": "11", "available": True, "price": "235.00", "sku": "JJ7565-300-11"}],
                   "images": [{"src": "https://cdn.example/zoo.jpg"}]}
        store = {"name": "Test Skate", "url": "https://testskate.example"}
        cache = fresh_cache(); cache["shopify"]["https://testskate.example"] = True

        def fake_get(url, **kw):
            return Resp(200, "{}", {"products": [product] if "page=1" in url else []})
        with mock.patch.object(t.requests, "get", side_effect=fake_get):
            finds = t.check_store(store, cache)
        self.assertEqual(len(finds), 1)
        f = finds[0]
        self.assertEqual((f["feed"], f["sb"]), ("dunks", True))
        self.assertIn("s:JJ7565-300", f["keys"])
        self.assertEqual(f["sizes"], "10, 11")
        self.assertTrue(f["link"].endswith("/products/nike-sb-dunk-low-doaf-the-zoo-jj7565-300"))
        self.assertFalse(f.get("stale"))


class NewsFeed(unittest.TestCase):
    """Releases that skip skate shops (DOAF Zoo: Flight Club + GOAT) come in via news."""
    def setUp(self):
        self.store = {"name": "Sneaker News", "url": "https://sneakernews.example/feed/", "news_feed": True}

    def run_feed(self, xml, cache=None, store=None):
        cache = cache or fresh_cache()
        with mock.patch.object(t.requests, "get", return_value=Resp(200, xml)):
            return t.check_store(store or self.store, cache), cache

    def test_doaf_found_once_and_filtered(self):
        xml = rss(
            ("Nike SB Dunk Low “The Zoo” Ducks of a Feather Release Date",
             "https://sneakernews.example/2026/11/09/nike-sb-dunk-low-ducks-of-a-feather-zoo-jj7565-300/",
             "Style Code: JJ7565-300 Release Date: November 26th, 2026 Price: $235", NOW),
            ("Where To Buy The Ducks of a Feather x Nike SB Dunk Low The Zoo",
             "https://sneakernews.example/2026/11/20/where-to-buy-doaf-zoo/",
             "Available at Flight Club and GOAT. JJ7565-300", NOW),          # same release
            ("Nike SB Dunk Low Ducks of a Feather Shout",
             "https://sneakernews.example/2026/10/28/nike-sb-dunk-low-oregon-ducks-of-a-feather-jj7564-200/",
             "Style code JJ7564-200", NOW),                                    # different pair
            ("Air Jordan 4 Retro Release Date", "https://sneakernews.example/aj4/", "", NOW),
            ("Nike Dunk Low Panda Restock", "https://sneakernews.example/panda/", "", NOW),
            ("Nike SB Dunk Low Old News", "https://sneakernews.example/old/", "XX1234-001", OLD),
            ("Sneaker Deals: Nike SB Dunk Low Under Retail", "https://sneakernews.example/deals/", "", NOW),
        )
        finds, cache = self.run_feed(xml)
        codes = sorted(k for f in finds for k in f["keys"] if k.startswith("s:"))
        self.assertEqual(codes, ["s:JJ7564-200", "s:JJ7565-300"])
        for f in finds:
            self.assertEqual((f["feed"], f["sb"], f["news"]), ("dunks", True, True))
        self.assertIn("Sneaker News", cache["_fetched"])

    def test_second_feed_does_not_repeat_release(self):
        first = rss(("Nike SB Dunk Low The Zoo Ducks of a Feather", "https://a.example/1", "JJ7565-300", NOW))
        finds, cache = self.run_feed(first)
        self.assertEqual(len(finds), 1)
        for f in finds:
            cache["seen"].add(f["id"])
        other = {"name": "Sneaker Files", "url": "https://sf.example/feed/", "news_feed": True}
        again = rss(("Ducks of a Feather x Nike SB Dunk Low ‘The Zoo’ Releases Holiday 2026",
                     "https://sf.example/doaf-zoo/", "no code in this one", NOW))
        finds2, _ = self.run_feed(again, cache, other)
        self.assertEqual(finds2, [])

    def test_broken_feed_is_harmless(self):
        with mock.patch.object(t.requests, "get", side_effect=t.requests.ConnectionError("down")):
            self.assertEqual(t.check_store(self.store, fresh_cache()), [])


class FirstLoadOfNewSourceIsSilent(unittest.TestCase):
    """A news feed's first successful run seeds quietly, so adding feeds can't flood alerts."""
    def test_run_seeds_then_alerts(self):
        sent = []
        stores = [{"name": "Sneaker News", "url": "https://sneakernews.example/feed/", "news_feed": True}]
        cache = fresh_cache(); cache["seen"] = {"placeholder"}
        cache.update(apparel_seeded=True, nike_dunk_seeded=True, boutique_reseed=True)
        feed = [rss(("Nike SB Dunk Low The Zoo Ducks of a Feather", "https://a.example/zoo", "JJ7565-300", NOW))]
        with mock.patch.object(t, "STORES", stores), \
             mock.patch.object(t, "load_cache", return_value=cache), \
             mock.patch.object(t, "save_cache"), \
             mock.patch.object(t, "maybe_send_weekly_digest"), \
             mock.patch.object(t, "send_discord", side_effect=lambda f: sent.append(("discord", f["title"]))), \
             mock.patch.object(t, "send_email", side_effect=lambda fs, s=None: sent.append(("email", s))), \
             mock.patch.object(t.requests, "get", side_effect=lambda *a, **k: Resp(200, feed[0])):
            t.run()                                   # seed pass
            self.assertEqual(sent, [])
            feed[0] = rss(("Nike SB Dunk Low Shout Ducks of a Feather", "https://a.example/shout", "JJ7564-200", NOW))
            t.run()                                   # genuinely new release
        self.assertIn(("discord", "Nike SB Dunk Low Shout Ducks of a Feather"), sent)
        self.assertIn(("email", "SB Radar: 1 new SB Dunk(s) detected"), sent)


if __name__ == "__main__":
    unittest.main()
