# SB Radar

Monitors skate shop websites for new Nike SB Dunk listings and alerts you the
moment one shows up — Discord message (with product image when available)
plus an email. It's a tracker only: it never buys anything for you.

This repo is **public** on purpose — GitHub Actions is only free and
unlimited on public repos ([docs](https://docs.github.com/en/actions/administering-github-actions/usage-limits-billing-and-administration)).
The store list and script are visible to anyone; your Discord webhook URL and
Gmail app password are not — they live only in this repo's encrypted
Actions secrets, never in the code.

---

## How it works

- Runs on a GitHub Actions schedule every 5 minutes (`.github/workflows/tracker.yml`),
  one pass per run — not a long-lived process.
- For each store: if it runs Shopify, hits its public `/products.json` API;
  otherwise falls back to scraping the homepage/collection pages for SB
  mentions.
- Keeps a `seen_products.json` cache of everything already seen, committed
  back to the repo after each run, so only genuinely new listings alert.
- First run seeds the cache silently (no alert flood for things already
  listed) — only drops that appear *after* that count.
- Local San Diego / North County shops are flagged 📍 **LOCAL** in the alert
  since those may only carry stock in-store.

## Store list

~109 stores, narrowed down from a larger list by actually checking each
domain resolves (DNS lookup, done Sep 2026) rather than trusting the source
list at face value — about a third of the original entries didn't exist.
Some stores will still occasionally fail (Cloudflare bot-protection, site
redesign, etc.) — the tracker logs a warning and moves on, it won't crash.

## Setup

### 1. Discord webhook
Already created for this project — a server named "SB Radar" with a
`#sb-drops` channel and its own webhook.

### 2. Gmail app password
Generate one at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
(requires 2-factor auth to be enabled on the account). This is **not** your
normal Gmail password — it's a separate 16-character code scoped just to
this use.

### 3. Repo secrets
In this repo: **Settings → Secrets and variables → Actions → New repository secret**.
Add:

| Secret | Value |
|---|---|
| `DISCORD_WEBHOOK_URL` | The SB Radar Discord webhook URL |
| `GMAIL_ADDRESS` | The Gmail address sending the alert email |
| `GMAIL_APP_PASSWORD` | The app password from step 2 |
| `EMAIL_TO` | Where alert emails should land (can be the same Gmail address) |

### 4. Enable the workflow
Actions run automatically once these secrets exist and the workflow file is
on the default branch. You can also trigger a run manually from the
**Actions** tab → **SB Radar Tracker** → **Run workflow**, useful for
confirming everything's wired up without waiting for the next 5-minute tick.

## Adding more stores

Open `tracker.py`, find `STORES = [`, and add:
```python
{"name": "Store Name", "url": "https://storename.com"},
```
For local SD/North County shops, add `"local_sd": True`.

## Troubleshooting

**Not getting alerts?** Check the Actions tab → latest run → logs. Confirm
all four secrets are set (Settings → Secrets and variables → Actions).

**A store never shows anything?** Some stores block automated requests
(Cloudflare, bot detection). The tracker logs a warning and skips them —
that's expected, not a bug.

**Too many alerts after a change?** If `seen_products.json` gets reset or
deleted, the next run treats everything currently listed as "new" again and
re-seeds silently — you won't get flooded, but you also won't get alerts for
that one seeding run.
