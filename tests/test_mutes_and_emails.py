"""Offline tests: a shoe rejected in Discord stays rejected (other shops, other wordings),
and restock emails only go out in the watched sizes. Titles are real ones from the cache.
Run: python -m unittest discover -s tests -v"""
import importlib.util, os, unittest
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


def muted_cache(*entries):
    """entries: (title, link, keys) the way a 🚫 reaction stores them."""
    muted = {}
    for title, link, keys in entries:
        for k in keys:
            muted[k] = {"title": title, "link": link, "t": 1}
    return {"muted": muted}


def find(title, link="", keys=()):
    return {"title": title, "link": link, "keys": list(keys) or ["p:x::" + title]}


MUNI = ("Nike SB Dunk Low x Muni Fast Pass - Safety Orange/Wolf Grey",
        "https://richmondhood.com/products/nike-sb-dunk-low-x-muni-fast-pass",
        ["t:dunk low x muni fast pass safety orange wolf grey", "p:Richmond Hood::10338401288356"])
ZOOM_BLAZER_SPRUCE = ("Nike SB Zoom Blazer Mid Black Spruce",
                      "https://xbusa.com/products/nike-sb-zoom-blazer-mid-black-spruce",
                      ["t:zoom blazer mid black spruce", "p:Xtreme::15453558702174"])
BLAZER_BW = ("Nike SB Zoom Blazer Mid (Black/White)",
             "https://vuskateboardshop.com/products/nike-sb-zoom-blazer-mid-black-white",
             ["t:zoom blazer mid black white", "p:VU Skate Shop::8357993676967"])
DUNK_SPRUCE = ("Nike SB Dunk Low Pro Premium Black Spruce - Metallic Gold - Black",
               "https://blacksheepskateshop.com/products/nike-sb-dunk-low-pro-premium-black-spruce-metallic-gold-black",
               ["s:IQ0431-300", "p:Black Sheep NC::7680973471790"])
ISHOD = ("Nike SB Air Max Ishod (Blue Void/Black)", "", ["p:Theory Skate Shop::8206675804347"])
DUNK_WBG = ("Nike SB Dunk Low Pro - White/Black/Gum",
            "https://stratosphereskateboards.com/products/nike-sb-dunk-white-black",
            ["t:dunk low pro white black gum", "p:Stratosphere::8196457365665"])


class RejectedShoeStaysRejected(unittest.TestCase):
    """Each case here showed up again in Discord or email after it was rejected."""

    def test_html_listing_with_entities_and_size(self):
        # Jerics' page gave this exact title, and it was emailed after Muni was rejected.
        f = find("Nike SB Dunk Low Pro &quot;Muni Fast Pass&quot; Size 7.5", "https://jerics.com")
        self.assertTrue(t.is_muted(f, muted_cache(MUNI)))
        f = find("Nike SB Dunk Low Pro &amp;quot;Muni Fast Pass&amp;quot; Size 10", "https://jerics.com")
        self.assertTrue(t.is_muted(f, muted_cache(MUNI)))

    def test_store_leaves_out_zoom(self):
        for title, link in [
            ("Nike SB Blazer Mid - (Black Spruce/Corduroy Brown)",
             "https://geometricskateshop.com/products/nike-sb-blazer-mid-green-brown"),
            ("NIKE SB BLAZER MID BLACK SPRUCE / CORDUROY BROWN",
             "https://bluetilesc.com/products/nike-sb-blazer-mid-black-spruce-corduroy-brown"),
        ]:
            with self.subTest(title=title):
                self.assertTrue(t.is_muted(find(title, link), muted_cache(ZOOM_BLAZER_SPRUCE)))

    def test_plain_black_white_colorway_at_other_shops(self):
        for title, link in [
            ("NIKE SB - Zoom Blazer Mid Black/White",
             "https://deliskatesupply.com/products/nike-sb-zoom-blazer-mid-black-white"),
            ("Nike SB Blazer Mid - Black/White", "https://iconboardshop.com/products/blazer-mid-black-white"),
            ("Nike SB Zoom Blazer Mid Shoes",        # colorway only in the URL, old-style code
             "https://www.nocomplyatx.com/products/nike-sb-zoom-blazer-mid-black-white-864349-007"),
        ]:
            with self.subTest(title=title):
                self.assertTrue(t.is_muted(find(title, link), muted_cache(BLAZER_BW)))

    def test_style_code_without_dash(self):
        p = {"title": "AF1 Navy Black HM8517400 Nike SB Suede Skateboarding Shoes",
             "handle": "af1-navy-black-navy-hm8517400-nike-sb-suede-skateboarding-shoes",
             "variants": [{"sku": ""}]}
        keys = t.shoe_keys(p, p["title"], "Southside Boardshop::15477224735037")
        self.assertIn("s:HM8517-400", keys)
        muted = muted_cache(("Nike SB - Air Force 1 (Navy/Black-Navy)", "", ["s:HM8517-400"]))
        self.assertTrue(t.is_muted(find(p["title"], "", keys), muted))

    def test_old_style_code_in_handle(self):
        p = {"title": "Nike SB Zoom Blazer Mid Shoes",
             "handle": "nike-sb-zoom-blazer-mid-black-white-864349-007", "variants": []}
        self.assertEqual(t.style_code(p), "864349-007")


class DifferentShoeIsNotMuted(unittest.TestCase):
    """Rejecting one shoe must never hide a different one."""
    CASES = [
        (BLAZER_BW, "Nike SB Zoom Blazer Low Pro GT - Black/White", ""),         # other height
        (BLAZER_BW, "Nike SB Zoom Blazer Mid (White/Black)", ""),                # colors reversed
        (BLAZER_BW, "Nike SB Zoom Blazer Mid - Black/White/Gum", ""),            # extra color
        (BLAZER_BW, "Nike SB Dunk Low Pro Black/White", ""),                     # other model
        (BLAZER_BW, "Nike Blazer Mid '77 Black/White", ""),                      # not SB
        (BLAZER_BW, "Nike SB Zoom Blazer Mid x Anti-Hero Black/White", ""),      # a collab
        (DUNK_SPRUCE, "Nike SB Blazer Mid - (Black Spruce/Corduroy Brown)", ""), # same colors, other model
        (ZOOM_BLAZER_SPRUCE, "Nike SB Dunk Low Pro PRM WC Black Spruce", ""),
        (ISHOD, "Nike SB Air Max Ishod (Blue Void/White)", ""),
        (DUNK_WBG, "Nike SB Dunk Low Pro-Black/White/Gum", ""),
        (MUNI, "Nike SB Dunk Low Pro Size 7.5", "https://jerics.com"),
        (MUNI, "Nike SB Dunk Low Pro - Safety Orange/Wolf Grey/Baroque Brown", ""),
    ]

    def test_cases(self):
        for muted, title, link in self.CASES:
            with self.subTest(muted=muted[0], title=title):
                self.assertFalse(t.is_muted(find(title, link), muted_cache(muted)))


class WatchedSizes(unittest.TestCase):
    def test_setting(self):
        self.assertEqual(t._watch_sizes(None), ("11", "11.5"))
        self.assertEqual(t._watch_sizes(""), ("11", "11.5"))
        self.assertEqual(t._watch_sizes("11.5"), ("11.5",))
        self.assertEqual(t._watch_sizes(" 11, 11.5 ,12"), ("11", "11.5", "12"))
        self.assertEqual(t._watch_sizes("none"), ())

    def test_labels(self):
        sizes = ("11", "11.5")
        for label, want in [("11", "11"), ("11.0", "11"), ("US 11.5", "11.5"), ("M 11 / W 12.5", "11"),
                            ("11.5 M", "11.5"), ("10.5", None), ("12", None), ("W 11", None), ("", None)]:
            with self.subTest(label=label):
                self.assertEqual(t.watched_label(label, sizes), want)
        self.assertIsNone(t.watched_label("11", ()))       # restock emails off


SHOP = {"name": "Test Skate", "url": "https://testskate.example"}


def dunk(pid, sizes, title="Nike SB Dunk Low Pro - Court Purple/Gum", sku="HF3704-500"):
    published = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    return {"id": pid, "title": title, "handle": f"dunk-{pid}", "vendor": "Nike SB", "product_type": "Shoes",
            "published_at": published,
            "variants": [{"option1": s, "available": a, "price": "125.00", "sku": f"{sku}-{s}"} for s, a in sizes],
            "images": []}


class SizeRestockEmails(unittest.TestCase):
    def check(self, product, cache, watch):
        with mock.patch.object(t, "WATCH_SIZES", watch), \
             mock.patch.object(t.requests, "get",
                               side_effect=lambda url, **kw: Resp(200, "{}", {"products": [product] if "page=1" in url else []})):
            return t.check_store(SHOP, cache)

    def setup_cache(self, pid):
        cache = fresh_cache()
        cache["shopify"][SHOP["url"]] = True
        cache["seen"].add(pid); cache["stock"][pid] = True
        cache["avail"][pid] = {"l": ["9"], "m": {}}        # only size 9 was in stock before
        return cache

    def test_size_back_only_counts_for_watched_size(self):
        p = dunk(1, [("9", True), ("11", True)])
        finds = self.check(p, self.setup_cache("Test Skate::1"), ("11.5",))
        self.assertFalse([f for f in finds if f.get("size_only")])
        finds = self.check(p, self.setup_cache("Test Skate::1"), ("11", "11.5"))
        self.assertEqual([f["size_back"] for f in finds if f.get("size_only")], [["11"]])

    def test_restock_emails_off(self):
        p = dunk(1, [("9", True), ("11", True), ("11.5", True)])
        finds = self.check(p, self.setup_cache("Test Skate::1"), ())
        self.assertFalse([f for f in finds if f.get("size_only")])


class RunEmails(unittest.TestCase):
    """End to end through run(): what reaches Discord and what reaches email."""

    def run_once(self, products, cache, watch=("11", "11.5")):
        sent = []
        with mock.patch.object(t, "STORES", [SHOP]), \
             mock.patch.object(t, "WATCH_SIZES", watch), \
             mock.patch.object(t, "load_cache", return_value=cache), \
             mock.patch.object(t, "save_cache"), \
             mock.patch.object(t, "maybe_send_weekly_digest"), \
             mock.patch.object(t, "refresh_sizes", side_effect=lambda fs, c: fs), \
             mock.patch.object(t, "send_discord", side_effect=lambda f: sent.append(("discord", f["title"]))), \
             mock.patch.object(t, "send_email", side_effect=lambda fs, s=None: sent.append(("email", s, [f["title"] for f in fs]))), \
             mock.patch.object(t.requests, "get",
                               side_effect=lambda url, **kw: Resp(200, "{}", {"products": products if "page=1" in url else []})):
            t.run()
        return sent

    def base_cache(self):
        cache = fresh_cache()
        cache["shopify"][SHOP["url"]] = True
        cache["seeded_stores"] = {SHOP["name"]}
        cache.update(apparel_seeded=True, nike_dunk_seeded=True, boutique_reseed=True)
        return cache

    def sold_out_listing(self, pid):
        cache = self.base_cache()
        cache["seen"] = {f"Test Skate::{pid}"}
        cache["stock"] = {f"Test Skate::{pid}": False}         # nothing buyable last run
        cache["avail"] = {f"Test Skate::{pid}": {"l": [], "m": {}}}
        return cache

    def test_back_in_stock_without_his_size_is_not_emailed(self):
        sent = self.run_once([dunk(5, [("9", True), ("10", True), ("11", False)])], self.sold_out_listing(5))
        self.assertIn(("discord", "Nike SB Dunk Low Pro - Court Purple/Gum"), sent)
        self.assertFalse([s for s in sent if s[0] == "email"])

    def test_back_in_stock_in_his_size_is_emailed(self):
        sent = self.run_once([dunk(5, [("9", True), ("11.5", True)])], self.sold_out_listing(5))
        emails = [s for s in sent if s[0] == "email"]
        self.assertTrue(emails)
        self.assertIn("NOW AVAILABLE", emails[0][1])

    def test_rejected_shoe_is_never_emailed(self):
        cache = self.base_cache()
        cache["seen"] = {"placeholder"}
        cache.update(muted_cache(MUNI))
        p = dunk(7, [("11", True)], title="Nike SB Dunk Low Pro Muni Fast Pass", sku="IR1888-800")
        p["published_at"] = datetime.now(timezone.utc).isoformat()     # a brand-new listing
        sent = self.run_once([p], cache)
        self.assertEqual(sent, [])
        # and the same shoe coming back in stock later stays quiet too
        cache2 = self.sold_out_listing(7); cache2.update(muted_cache(MUNI))
        self.assertEqual(self.run_once([p], cache2), [])

    def test_new_drop_still_emailed(self):
        cache = self.base_cache(); cache["seen"] = {"placeholder"}
        p = dunk(8, [("8", True)], title="Nike SB Dunk Low x Ducks of a Feather 'The Zoo'", sku="JJ7565-300")
        p["published_at"] = datetime.now(timezone.utc).isoformat()
        sent = self.run_once([p], cache, watch=("11.5",))
        self.assertIn(("email", "SB Radar: 1 new SB Dunk(s) detected", ["Nike SB Dunk Low x Ducks of a Feather 'The Zoo'"]), sent)


if __name__ == "__main__":
    unittest.main()
