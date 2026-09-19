"""
Nike SB Dunk Tracker — SB Radar
- Monitors Nike-adjacent skate shop sites (national list, DNS-verified Sep 2026)
  plus San Diego / North County local shops
- Runs ONCE per invocation (designed for a GitHub Actions cron schedule, not a
  long-lived process) — loads the "seen" cache from disk, does one pass over
  every store, alerts on anything new, then writes the cache back out so the
  workflow can commit it. See .github/workflows/tracker.yml.
- Alerts go to a Discord webhook (with a product image embed when available)
  and, for real drops (not the first-run seed), an email via Gmail SMTP.
"""

import os
import json
import re
import smtplib
import logging
import concurrent.futures
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from html import escape as _html_escape
from datetime import datetime
from pathlib import Path

import requests

# ── Config ────────────────────────────────────────────────────────────────────
CACHE_FILE      = Path("seen_products.json")
REQUEST_TIMEOUT = 6
MAX_WORKERS     = 50

SB_KEYWORDS = [
    "sb dunk", "dunk sb", "nike sb", " sb low", " sb high",
    "sb mid", "sb zoom", "skate barge",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/json,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── Discord ───────────────────────────────────────────────────────────────────
# Supports posting to multiple Discord servers — set DISCORD_WEBHOOK_URL for the
# primary server, and optionally DISCORD_WEBHOOK_URL_2 (a second server, e.g. a
# friend's), DISCORD_WEBHOOK_URL_3, etc. Any unset ones are just skipped.
DISCORD_WEBHOOKS = [
    v for v in (
        os.environ.get("DISCORD_WEBHOOK_URL"),
        os.environ.get("DISCORD_WEBHOOK_URL_2"),
        os.environ.get("DISCORD_WEBHOOK_URL_3"),
    )
    if v
]

def send_discord(find: dict):
    if not DISCORD_WEBHOOKS:
        log.warning("No DISCORD_WEBHOOK_URL* env vars set — skipping Discord alert")
        return
    local_line = "\n📍 **LOCAL — CHECK IN-STORE**" if find.get("local") else ""
    embed = {
        "title": find["title"],
        "url": find["link"],
        "description": (
            f"**{find['store']}**{local_line}\n"
            f"💵 ${find['price']}\n"
            f"📏 {find['sizes']}"
        ),
        "color": 0xFF6A00,
    }
    if find.get("image"):
        embed["image"] = {"url": find["image"]}
    payload = {"content": "🛹 New SB's detected", "embeds": [embed]}
    for webhook_url in DISCORD_WEBHOOKS:
        try:
            resp = requests.post(webhook_url, json=payload, timeout=10)
            resp.raise_for_status()
        except requests.RequestException as e:
            log.warning(f"Discord post failed for one webhook: {e}")

# ── Email (Gmail SMTP) ───────────────────────────────────────────────────────
GMAIL_ADDRESS      = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
EMAIL_TO           = os.environ.get("EMAIL_TO", GMAIL_ADDRESS)

def send_email(finds: list):
    if not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD and EMAIL_TO):
        log.warning("Gmail SMTP env vars not set — skipping email alert")
        return

    # Plain-text fallback (for clients that don't render HTML)
    text_lines = []
    for f in finds:
        local_tag = " (LOCAL — check in-store)" if f.get("local") else ""
        text_lines.append(
            f"{f['store']}{local_tag}\n"
            f"{f['title']} — ${f['price']}\n"
            f"Sizes: {f['sizes']}\n"
            f"{f['link']}\n"
        )
    text_body = "\n---\n".join(text_lines)

    # HTML version — includes the product image (same URL used in the Discord embed)
    cards = []
    for f in finds:
        local_tag = " 📍 <b>LOCAL — check in-store</b>" if f.get("local") else ""
        img_html = (
            f'<img src="{_html_escape(f["image"])}" alt="" '
            f'style="max-width:320px;width:100%;border-radius:8px;'
            f'margin:8px 0;display:block;">'
            if f.get("image") else ""
        )
        cards.append(f"""
        <div style="margin-bottom:28px;padding-bottom:24px;border-bottom:1px solid #e2e2e2;">
          <div style="font-weight:600;font-size:14px;color:#555;">{_html_escape(f['store'])}{local_tag}</div>
          <a href="{_html_escape(f['link'])}" style="font-size:16px;font-weight:700;color:#ff6a00;text-decoration:none;">
            {_html_escape(f['title'])}
          </a>
          <div style="font-size:14px;margin-top:4px;">💵 ${_html_escape(str(f['price']))}</div>
          <div style="font-size:14px;">📏 {_html_escape(f['sizes'])}</div>
          {img_html}
          <a href="{_html_escape(f['link'])}" style="font-size:13px;color:#1a73e8;">{_html_escape(f['link'])}</a>
        </div>""")
    html_body = f"""
    <html><body style="font-family:-apple-system,Helvetica,Arial,sans-serif;color:#111;max-width:480px;margin:0 auto;">
      <h2 style="color:#ff6a00;">🛹 SB Radar — {len(finds)} new drop(s)</h2>
      {''.join(cards)}
    </body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"SB Radar: {len(finds)} new SB's detected"
    msg["From"]    = GMAIL_ADDRESS
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, [EMAIL_TO], msg.as_string())

# ── Store list ────────────────────────────────────────────────────────────────
STORES = [
    # ── NATIONAL (verified-reachable, DNS-checked Sep 2026) ─────────────────
    {"name": "Doubler Kicks", "url": "https://doublerkicks.com"},
    {"name": "Humidity Skateshop", "url": "https://humiditynola.com"},
    {"name": "Bluetile Skateboards", "url": "https://bluetilesc.com"},
    {"name": "The Block Skate Supply", "url": "https://theblockskatesupply.com"},
    {"name": "Unheardof", "url": "https://unheardofbrand.com"},
    {"name": "Flatspot", "url": "https://www.flatspot.com"},
    {"name": "Stardust Skate Shop", "url": "https://www.stardustskateshop.com"},
    {"name": "Reset Mercantile", "url": "https://resetmercantile.com"},
    {"name": "Sequence", "url": "https://sequencealaska.com"},
    {"name": "BLX Skateshop", "url": "https://blxskateshop.com"},
    {"name": "Cowtown Skateboards", "url": "https://cowtownskate.com"},
    {"name": "Sidewalk Surfer", "url": "https://sidewalksurfer.com"},
    {"name": "The Berrics", "url": "https://theberrics.com"},
    {"name": "Boarders Topanga", "url": "https://boarders.com"},
    {"name": "Crown Apparel", "url": "https://crownapparelco.com"},
    {"name": "Defy Boardshop", "url": "https://defyboardshop.com"},
    {"name": "Icon Boardshop", "url": "https://iconboardshop.com"},
    {"name": "Innercity Skate", "url": "https://innercityskate.com"},
    {"name": "Kingswell CA", "url": "https://kingswell.com"},
    {"name": "Brooklyn Projects", "url": "https://brooklynprojects.com"},
    {"name": "New Star", "url": "https://newstarshop.com"},
    {"name": "Pawnshop Skate", "url": "https://pawnshopskate.com"},
    {"name": "Transport Skate", "url": "https://transportskate.com"},
    {"name": "Val Surf", "url": "https://valsurf.com"},
    {"name": "Asylum Skate", "url": "https://asylumskate.com"},
    {"name": "Contenders", "url": "https://contendersclothing.com"},
    {"name": "East 4th", "url": "https://east4th.com"},
    {"name": "Furnace Skate", "url": "https://furnaceskate.com"},
    {"name": "Jacks Surfboards", "url": "https://jackssurfboards.com"},
    {"name": "Long Beach Skate", "url": "https://longbeachskate.com"},
    {"name": "Sixes and Sevens", "url": "https://sixesandsevensskate.com"},
    {"name": "Urban Feet", "url": "https://urbanfeet.com"},
    {"name": "Pride Skate", "url": "https://prideskate.com"},
    {"name": "PLA Folsom", "url": "https://plaskate.com"},
    {"name": "510 Skateboarding", "url": "https://510skateboarding.com"},
    {"name": "Bills Wheels", "url": "https://billswheels.com"},
    {"name": "DLX SF", "url": "https://dlxsf.com"},
    {"name": "FTC SF", "url": "https://ftcsf.com"},
    {"name": "Skate Warehouse", "url": "https://skatewarehouse.com"},
    {"name": "Proof Lab", "url": "https://prooflab.com"},
    {"name": "303 Annex", "url": "https://303boards.com"},
    {"name": "Satellite Boardshop", "url": "https://satelliteboardshop.com"},
    {"name": "Skateboard Market", "url": "https://skateboardmarket.com"},
    {"name": "Relief Skate Supply", "url": "https://reliefskatesupply.com"},
    {"name": "Westside Skate Shop", "url": "https://westsideskate.com"},
    {"name": "Skatepark of Tampa", "url": "https://skateparkoftampa.com"},
    {"name": "The Compound FL", "url": "https://thecompoundfl.com"},
    {"name": "Island Water Sports", "url": "https://islandwatersports.com"},
    {"name": "Sunrise Skate", "url": "https://sunriseskate.com"},
    {"name": "Stratosphere", "url": "https://stratosphereskateboards.com"},
    {"name": "Clockwork Skate", "url": "https://clockworkskate.com"},
    {"name": "Uprise Skateshop", "url": "https://upriseskateshop.com"},
    {"name": "Jerics Skate", "url": "https://jerics.com"},
    {"name": "Fargo Skate", "url": "https://fargoskate.com"},
    {"name": "Rise Fort Wayne", "url": "https://risefortwayne.com"},
    {"name": "Minus Skate Shop", "url": "https://minusskateshop.com"},
    {"name": "Subsect Skate", "url": "https://subsect.com"},
    {"name": "Premier MI", "url": "https://thepremierstore.com"},
    {"name": "Olympia Skate Shop", "url": "https://olympiaskateshop.com"},
    {"name": "Brush Alley", "url": "https://brushalley.com"},
    {"name": "Exodus Rideshop", "url": "https://exodusrideshop.com"},
    {"name": "Modern Skate", "url": "https://modernskate.com"},
    {"name": "Plus Skateboarding", "url": "https://plusskateboarding.com"},
    {"name": "Skaters Advocate", "url": "https://skatersadvocate.com"},
    {"name": "Familia Skateboard Shop", "url": "https://familiaskateshop.com"},
    {"name": "Cal Surf MN", "url": "https://calsurf.com"},
    {"name": "Swellophonic", "url": "https://swellophonic.com"},
    {"name": "Infinity Skate", "url": "https://infinityskate.com"},
    {"name": "Coureur Goods", "url": "https://coureurgoods.com"},
    {"name": "NJ Skateshop", "url": "https://njskateshop.com"},
    {"name": "Underground Skateshop", "url": "https://undergroundskateshop.com"},
    {"name": "Travel Skate Shop", "url": "https://travelskateshop.com"},
    {"name": "Seasons Skate Shop", "url": "https://seasonsskateshop.com"},
    {"name": "Richmond Hood", "url": "https://richmondhood.com"},
    {"name": "LICK NYC", "url": "https://licknyc.com"},
    {"name": "Bunger Sayville", "url": "https://bungersayville.com"},
    {"name": "Moms Skate Shop NY", "url": "https://momsskateshop.com"},
    {"name": "Supreme", "url": "https://supremenewyork.com"},
    {"name": "Tenant NY", "url": "https://tenantny.com"},
    {"name": "Homegrown NY", "url": "https://homegrownny.com"},
    {"name": "Black Sheep NC", "url": "https://blacksheepskateshop.com"},
    {"name": "Backdoor Skate Shop", "url": "https://backdoorskateshop.com"},
    {"name": "Endless Grind", "url": "https://endlessgrind.com"},
    {"name": "Permanent Vacation", "url": "https://permanentvacationshop.com"},
    {"name": "Push Skate Shop", "url": "https://pushskateshop.com"},
    {"name": "Stolen Skate Shop", "url": "https://stolenskateshop.com"},
    {"name": "Manifest Skate Shop", "url": "https://manifestskateshop.com"},
    {"name": "One Love Skate", "url": "https://oneloveskate.com"},
    {"name": "Core Boardshop", "url": "https://coreboardshop.com"},
    {"name": "CCS", "url": "https://ccs.com"},
    {"name": "Tactics", "url": "https://tactics.com"},
    {"name": "Home Base Skate Shop", "url": "https://homebaseskateshop.com"},
    {"name": "Nocturnal Skate", "url": "https://nocturnalskate.com"},
    {"name": "Radio Skateshop", "url": "https://radioskateshop.com"},
    {"name": "Continuum Skate Shop", "url": "https://continuumskateshop.com"},
    {"name": "Comfort Skateshop", "url": "https://comfortskateshop.com"},
    {"name": "No Comply TX", "url": "https://nocomply.com"},
    {"name": "Select TX", "url": "https://selecttx.com"},
    {"name": "Southside Skatepark", "url": "https://southsideskatepark.com"},
    {"name": "Fast Forward TX", "url": "https://fastforwardskate.com"},
    {"name": "Maven VT", "url": "https://mavenskate.com"},
    {"name": "Wonder Skate Shop", "url": "https://wonderskateshop.com"},
    {"name": "Skate Supply VA", "url": "https://skatesupplyva.com"},
    {"name": "35th North", "url": "https://35thnorth.com"},
    {"name": "New Yak City", "url": "https://newyakcity.com"},
    {"name": "Alumni Skate", "url": "https://alumniskate.com"},

    # ── SAN DIEGO / NORTH COUNTY LOCAL ───────────────────────────────────────
    {"name": "Pharmacy Temecula", "url": "https://pharmacyboardshop.com", "local_sd": True},
    {"name": "Sun Diego Boardshop", "url": "https://sundiego.com", "local_sd": True},
    {"name": "Rose Street Encinitas", "url": "https://rosestreetskateshop.com", "local_sd": True},
    {"name": "McGill's Skate Shop", "url": "https://www.mcgillsskateshop.com", "local_sd": True},
    {"name": "Heritage SKTBDS", "url": "https://heritageskateboards.com", "local_sd": True},
    {"name": "Status Skateshop SD", "url": "https://statusskateshop.com", "local_sd": True},
    {"name": "Soul Grind SD", "url": "https://soulgrind.com", "local_sd": True},
    {"name": "Slappy's Garage", "url": "https://slappysgarage.com", "local_sd": True},
    {"name": "Western Skate Co", "url": "https://westernskate.com", "local_sd": True},
    {"name": "House of Vista", "url": "https://houseofvista.bigcartel.com", "local_sd": True},
    # ── SOLEFEED 2-YEAR SB DUNK AUDIT (added Sep 2026) ─────────────────────
    {"name": "Embassy", "url": "https://embassyboardshop.com"},
    {"name": "Familia Skateshop", "url": "https://familiaskate.com"},
    {"name": "Kinetic", "url": "https://kineticskateboarding.com"},
    {"name": "Labor Skate Shop", "url": "https://laborskateshop.com"},
    {"name": "LB Skate", "url": "https://lbskate.com"},
    {"name": "Mainland", "url": "https://mainlandskateandsurf.com"},
    {"name": "Orchard", "url": "https://orchardshop.com"},
    {"name": "Overload", "url": "https://shopoverload.com", "local_sd": True},
    {"name": "Southside Boardshop", "url": "https://southsideskateshop.com"},
    {"name": "Arts & Rec", "url": "https://artsandrecstore.com", "local_sd": True},
    {"name": "No-Comply", "url": "https://www.nocomplyatx.com"},
    {"name": "Rock City Kicks", "url": "https://rockcitykicks.com"},
    {"name": "Drift House", "url": "https://drifthouse.com"},
    {"name": "After Hours Skateshop", "url": "https://afterhoursskateshop.com"},
    {"name": "Renarts", "url": "https://renarts.com"},
    {"name": "APB Store", "url": "https://apbstore.com"},
    {"name": "Civil", "url": "https://wearecivil.com"},
    {"name": "Theory Skate Shop", "url": "https://theoryskate.com"},
    {"name": "Recess Skate & Snow", "url": "https://recessrideshop.com"},
    {"name": "Xtreme", "url": "https://xbusa.com"},
    {"name": "People Skate and Snowboard", "url": "https://peopleskateandsnowboard.com"},
    {"name": "Barewires", "url": "https://barewiresurfshop.com"},
    {"name": "Pastime Skateshop", "url": "https://pastimeskateshop.com"},
    {"name": "VU Skate Shop", "url": "https://vuskateboardshop.com"},
    {"name": "Andrew", "url": "https://andrewdowntown.com"},
    {"name": "Deli Skate Supply", "url": "https://deliskatesupply.com"},
    {"name": "Blacklist", "url": "https://blacklistboardshop.com"},
    {"name": "Time Machine", "url": "https://timemachineskateshop.com"},
    {"name": "Blue Flowers", "url": "https://blueflowers.com"},
    {"name": "Swellophonic", "url": "https://chane.com"},
    {"name": "Geometric Skate Shop", "url": "https://geometricskateshop.com"},
    {"name": "Magnolia Skateshop", "url": "https://magnoliaskateshop.com"},
    {"name": "Detroit City Skateboards", "url": "https://detroitcityskateboards.com"},
    {"name": "303 Boards", "url": "https://303boards-com.myshopify.com"},
    {"name": "Cal Surf", "url": "https://cal-surf.com"},
    {"name": "Suite160", "url": "https://suite160.com"},
]

# ── Cache ─────────────────────────────────────────────────────────────────────

def load_cache() -> dict:
    if CACHE_FILE.exists():
        data = json.loads(CACHE_FILE.read_text())
        if isinstance(data, list):
            return {"seen": set(data), "shopify": {}, "seeded_stores": set()}
        return {
            "seen": set(data.get("seen", [])),
            "shopify": data.get("shopify", {}),
            "seeded_stores": set(data.get("seeded_stores", [])),
        }
    return {"seen": set(), "shopify": {}, "seeded_stores": set()}

def save_cache(cache: dict):
    CACHE_FILE.write_text(json.dumps({
        "seen": sorted(cache["seen"]),
        "shopify": cache["shopify"],
        "seeded_stores": sorted(cache["seeded_stores"]),
    }, indent=0))

# ── Detection & fetching ──────────────────────────────────────────────────────

def is_sb(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in SB_KEYWORDS)

def is_dunk(text: str) -> bool:
    return "dunk" in text.lower()

APPAREL_KEYWORDS = [
    "hoodie", "t-shirt", "tee", "shirt", "jacket", "crewneck", "sweatshirt",
    "sweatpants", "pants", "shorts", "hat", "cap", "beanie", "socks",
    "backpack", "bag", "tote", "sweater", "pullover", "jersey", "vest",
    "sticker", "poster", "grip tape", "wheels", "trucks", "bearings",
    "gloves", "belt", "wallet", "deck",
]

def is_apparel(title: str, product_type: str = "") -> bool:
    text = f"{title} {product_type}".lower()
    return any(re.search(rf"\b{re.escape(kw)}\b", text) for kw in APPAREL_KEYWORDS)

def is_shopify(url: str, cache: dict) -> bool:
    base = url.rstrip("/")
    if base in cache["shopify"]:
        return cache["shopify"][base]
    try:
        r = requests.get(f"{base}/products.json?limit=1", timeout=REQUEST_TIMEOUT, headers=HEADERS)
        result = r.status_code == 200 and "products" in r.text
    except Exception:
        result = False
    cache["shopify"][base] = result
    return result

def fetch_shopify(store: dict) -> list:
    all_products, page = [], 1
    base = store["url"].rstrip("/")
    while True:
        try:
            r = requests.get(
                f"{base}/products.json?limit=250&page={page}",
                timeout=REQUEST_TIMEOUT, headers=HEADERS,
            )
            if r.status_code != 200:
                break
            products = r.json().get("products", [])
            if not products:
                break
            all_products.extend(products)
            if len(products) < 250:
                break
            page += 1
        except Exception as e:
            log.warning(f"{store['name']} Shopify error p{page}: {e}")
            break
    return all_products

def fetch_html(store: dict) -> list:
    base  = store["url"].rstrip("/")
    found = []
    urls  = [base, f"{base}/collections/nike-sb", f"{base}/collections/nike", f"{base}/products"]
    for url in urls:
        try:
            r = requests.get(url, timeout=REQUEST_TIMEOUT, headers=HEADERS)
            if r.status_code != 200 or not is_sb(r.text.lower()):
                continue
            html = r.text
            candidates = re.findall(
                r'(?:alt|title|aria-label|data-name)=["\']([^"\'<>\n]{5,80})["\']', html, re.IGNORECASE
            )
            candidates += re.findall(r'<h[123][^>]*>([^<]{5,100})</h[123]>', html, re.IGNORECASE)
            for c in candidates:
                c = c.strip()
                if is_sb(c) and not is_apparel(c):
                    found.append({
                        "id":    f"{store['name']}::html::{c.lower()[:60]}",
                        "title": c,
                        "price": "See site",
                        "sizes": "Check site for sizes",
                        "link":  url,
                        "image": None,
                    })
            if found:
                break
        except Exception as e:
            log.warning(f"{store['name']} HTML error: {e}")
    return found

def format_sizes(product: dict) -> str:
    available = []
    for v in product.get("variants", []):
        if v.get("available", True):
            size = v.get("option1") or v.get("title", "")
            if size and size != "Default Title":
                available.append(size)
    if not available:
        return "Check site"
    def key(s):
        try: return float(re.sub(r"[^\d.]", "", s) or 999)
        except Exception: return 999
    return ", ".join(sorted(set(available), key=key))

def product_image(product: dict) -> str | None:
    images = product.get("images") or []
    if images:
        return images[0].get("src")
    if product.get("image"):
        return product["image"].get("src")
    return None

def check_store(store: dict, cache: dict) -> list:
    seen  = cache["seen"]
    finds = []

    if is_shopify(store["url"], cache):
        for p in fetch_shopify(store):
            title = p.get("title", "")
            if not is_sb(title):
                continue
            product_type = p.get("product_type", "")
            if is_apparel(title, product_type):
                continue
            pid = f"{store['name']}::{p.get('id')}"
            if pid in seen:
                continue
            v = p.get("variants", [{}])
            finds.append({
                "id":    pid,
                "store": store["name"],
                "title": title,
                "price": v[0].get("price", "?") if v else "?",
                "sizes": format_sizes(p),
                "link":  f"{store['url'].rstrip('/')}/products/{p.get('handle','')}",
                "image": product_image(p),
                "local": store.get("local_sd", False),
            })
    else:
        for f in fetch_html(store):
            if f["id"] not in seen:
                finds.append({**f, "store": store["name"], "local": store.get("local_sd", False)})

    for f in finds:
        log.info(f"  NEW SB [{store['name']}]: {f['title']} — ${f['price']}")
    return finds

# ── Main (single pass) ────────────────────────────────────────────────────────

def run():
    log.info("=" * 60)
    log.info("SB Radar | %d stores | single-pass run @ %s", len(STORES), datetime.now().isoformat(timespec="seconds"))
    log.info("=" * 60)

    cache     = load_cache()
    first_run = len(cache["seen"]) == 0
    if first_run:
        log.info("First run — seeding cache silently. Only NEW drops will alert from here on.")

    all_finds = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        future_to_store = {ex.submit(check_store, store, cache): store for store in STORES}
        for future in concurrent.futures.as_completed(future_to_store):
            store = future_to_store[future]
            try:
                finds = future.result()
                all_finds.extend(finds)
            except Exception as e:
                log.error("Error on %s: %s", store["name"], e)

    for f in all_finds:
        cache["seen"].add(f["id"])

    # New stores get one silent seed pass (like the global first-run seed, but
    # scoped per-store) so adding a store never floods alerts for its whole
    # existing catalog — only genuinely new drops alert from its second check on.
    checked_names = {store["name"] for store in STORES}
    new_stores = checked_names - cache["seeded_stores"]
    if new_stores:
        log.info("Silently seeding %d new store(s): %s", len(new_stores), ", ".join(sorted(new_stores)))
    alertable_finds = [f for f in all_finds if f["store"] not in new_stores]
    cache["seeded_stores"] |= checked_names
    save_cache(cache)

    if first_run:
        log.info("Cache seeded: %d products tracked. No alerts sent on first run.", len(cache["seen"]))
        return

    if not alertable_finds:
        log.info("No new SBs this run.")
        return

    for f in alertable_finds:
        try:
            send_discord(f)
        except Exception as e:
            log.error("Discord alert failed for %s: %s", f["title"], e)

    dunk_finds = [f for f in alertable_finds if is_dunk(f["title"])]
    if dunk_finds:
        try:
            send_email(dunk_finds)
        except Exception as e:
            log.error("Email alert failed: %s", e)
    else:
        log.info("No SB Dunks this run (%d non-Dunk SB find(s)) — email skipped, Discord still alerted.", len(all_finds))

    log.info("Run complete: %d new drop(s) alerted.", len(alertable_finds))


if __name__ == "__main__":
    run()
