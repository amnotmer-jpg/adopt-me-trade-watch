# Adopt Me Trade Watch (standalone)

A free, standalone replacement for a Muse cron job that checked two Adopt Me
trading sites every 15 minutes. It runs on **GitHub Actions + GitHub Pages**
and sends **push notifications via ntfy.sh** — no Muse usage, no server to
pay for.

It watches three pets:

- Sugar Axolotl
- Strawberry Shortcake Bat Dragon (SSBD)
- Monkey King (both sites list it as "Monkey King", not "King Monkey")

## What you get

- **Dashboard** (`index.html`, rebuilt every 15 minutes): new and recent
  listings in `You: [...] / Them: [...]` format, grouped by pet, with site
  labels and times. Elvebredd listings are offers, not completed trades.
- **Push alerts**: one short ntfy message per pet when new items appear,
  e.g. `3 new SSBD offers — see dashboard`, with a tap-to-open dashboard
  link.
- **Private match highlighting**: type your own pets once in the
  dashboard's inventory box. Cards whose You side you can fill exactly get
  a green ★ MATCH highlight. Your inventory lives **only in your browser's
  localStorage** — it is never in this repo, never in the notifications,
  and never sent anywhere. This matters because the repo is public (see
  below).

## Setup (no terminal, no git commands)

### 1. Create a PUBLIC GitHub repo

On github.com: click **+** (top right) → **New repository** → give it a
name → pick **Public** → **Create repository**. Public is required for
the free path: public repos get unlimited Actions minutes and free Pages.
A private repo only gets 2,000 Actions minutes a month — not enough for a
15-minute cadence (~2,900 min/month) — and Pages on private repos needs
a paid plan.

### 2. Upload these files with drag-and-drop

On your new repo page: **Add file → Upload files**. Then drag the whole
contents of this folder into the upload area — `watch.py`,
`elvebredd_browser.py`, `README.md`, `index.html`, the `data/` folder,
and the `.github/` folder — and click **Commit changes**.

Note: `.github` is a hidden folder, so some browsers won't include it in
a folder drag. If `.github/workflows/watch.yml` doesn't show up after
uploading, add it by hand: **Add file → Create new file**, type the
path `.github/workflows/watch.yml`, paste in the contents of that file
from this folder, and commit.

### 3. Turn on Pages

Repo **Settings → Pages → Build and deployment → Source: Deploy from a
branch → Branch: main, folder: / (root)** → **Save**. Your dashboard
will be at `https://<you>.github.io/<repo>/`.

### 4. Add the ntfy topic as an Actions secret

Make up a long random topic name and never put it in any file — for
example `am-watch-` followed by 16 random letters and numbers you invent.
Repo **Settings → Secrets and variables → Actions → New repository
secret**: name `NTFY_TOPIC`, value your topic. Anyone who guesses the
topic can read the notifications, so make it long and random — but note
the messages themselves are generic (pet + count + link) and contain no
inventory.

### 5. Install the free ntfy app and subscribe

- iOS: install **ntfy** from the App Store (free, no account needed).
- In the app, **Subscribe to topic**, enter the exact topic from step 4.
- Allow notifications.

### 6. Test it — and check Elvebredd cards appear

Repo **Actions → Adopt Me trade watch → Run workflow**
(workflow_dispatch). The first run **seeds** the seen-state silently (no
spam about old items) and builds the dashboard. Then:

- Open your Pages URL. You should see recent AMTV rows per pet **and**
  yellow Elvebredd cards. If the dashboard's Elvebredd status line says
  the browser step failed, open the Actions run log — the status line
  says why (a bot check, a timeout, or a site change). The AMTV half
  still works even when the browser step fails.
- To test a push: after the first run, the next run that finds a
  genuinely new row sends a notification. You can also test ntfy itself
  from the app's built-in test or any device with:
  `curl -d "test message" https://ntfy.sh/<your-topic>` — it should
  arrive on your phone within seconds.

### 7. Keep the old Muse watch running until this proves itself

Do not turn the Muse cron off yet. Run both for a day or two and compare:
the dashboard's Elvebredd cards should match what the Muse pings report
for the same window. Only retire the Muse watch once the dashboard is
catching Elvebredd listings reliably.

## Coverage and honest limits

**AMTV — fully covered.** The script reads each pet page (current RP
value) and the public `gettrades.php` snapshot (Recent Offers & Trades).
Each row is shown as You (what the offerer wants) / Them (what the
offerer offers), with badge codes, RP totals, win/lose, offerer username,
lister (when shown), status, and exact timestamp. The page's pet can sit
on either side of a row — the parser detects this from the row layout.
Dedupe uses the exact timestamp plus contents, so it is precise.
Completed rows are history and never get match highlights; only open
(Active) rows can highlight.

- Offered pets are **icon ids, not names** (AMTV renders them as images
  with no names). The dashboard shows `icon #1230 (R)` etc. Names are
  never invented. Mapping icon ids to names is a possible future
  improvement.
- Only the **Regular** snapshot tab is fetched (Neon/Mega tabs exist but
  are not covered yet). Each snapshot returns ~33 rows per pet.
- AMTV snapshot rows include both Active offers and completed trades;
  the dashboard shows each row's status verbatim.

**Elvebredd — covered in v2 via headless browser.** The Trade Center
page's listing cards are not in the raw HTML (only skeleton placeholders)
— they arrive over an authenticated feed that answers 401 to a plain
script. So v2 loads the page in headless Chromium (Playwright), waits
for the cards to render, and reads them from the DOM: pet identity from
`/images/pets/<Name>.png` image URLs, badges from
`/images/amoverlays/<BADGE>.svg` overlays (no overlay = plain/no-pot),
username and relative time from the card, split into You/Them sides.
The workflow installs Playwright + Chromium on every run.

- **Verification caveat — read this:** live card extraction could NOT be
  verified in the build sandbox (its network proxy refuses browser
  traffic to elvebredd.com, so Chromium there cannot load the site at
  all). The scraper code, the page structure it relies on (pet image
  URLs, overlay badges, "You" labels, relative times), and the AMTV half
  are all verified — but the first GitHub Actions run is the real
  end-to-end test for Elvebredd cards. Follow step 6 above: if the
  dashboard shows yellow Elvebredd cards, it works. If the browser step
  fails, the dashboard says so honestly and the AMTV half still runs.
- Scope is the **first page** of cards per pet (same scope the Muse
  browser watch used) — no scrolling or pagination. For fast-moving
  pets like SSBD, some listings will pass by between runs, exactly as
  with the Muse watch's partial scans.
- Cards whose You/Them split could not be determined from the DOM are
  still shown, flagged **"split unverified — sides may be swapped"**.
- If Elvebredd ever shows the runner a bot check, the browser step
  records it and skips Elvebredd for that run (no retries, no evasion)
  instead of faking listings.

**ntfy — implemented, not yet verified end-to-end.** The build sandbox
blocked all access to ntfy.sh, so the POST code path could not be tested
there. It uses the standard ntfy publish API (plain HTTPS POST, no
account, no key). GitHub Actions runners have open egress, so the first
workflow run is the real test — verify with step 6 above. If ntfy ever
fails for you, fallbacks: (a) a Discord webhook URL stored as a secret,
(b) an email via SMTP in the workflow, (c) a Telegram bot webhook. All
three are one small change to the notify step in `watch.py`.

**Rate limits and runtime.** Requests are sequential with gaps, on a
15-minute cadence. If a site answers 429/403, that site's check stops for
the run (no retries, no endpoint switching to evade a block) and the
dashboard records it. A full run takes roughly 2–5 minutes (browser load
+ searches + AMTV fetches), well inside the 15-minute cadence and the
workflow's 12-minute timeout.

## Customizing

- **Pets**: edit `PETS` in `watch.py` (name + AMTV `what-is-worth` id).
- **Cadence**: edit the `cron:` line in `.github/workflows/watch.yml`
  (note the free-tier math above if you go faster than 15 minutes).
- **Match rule**: the dashboard JS highlights a card only when every
  You-side entry is covered by your saved inventory with exact badges
  and enough count, and the card is not a Completed AMTV row.
  `Add`/`Add Big` entries never match (they are not inventory items).

## Files

- `watch.py` — the checker + dashboard generator.
- `elvebredd_browser.py` — the headless-Chromium Elvebredd scraper.
- `.github/workflows/watch.yml` — every-15-minutes run + Pages publish.
- `index.html`, `data/listings.json`, `data/seen.json` — generated by
  the workflow on each run (committed back to the repo by the workflow).
- `README.md` — this file.

No credentials, API keys, or private data live in this repo. The only
secret is `NTFY_TOPIC`, stored in GitHub Actions secrets, never in files.


## Check schedule

The workflow wakes up every 5 minutes, but a real check only runs when
`data/schedule.json` says so — so the check rhythm is adjustable without
touching the workflow:

```json
{"default_interval_min": 15,
 "windows": [{"start_hour": 12, "end_hour": 3, "interval_min": 10}],
 "boost": {"interval_min": 5, "until": "2026-10-30T10:00:00+00:00"}}
```

- `windows`: time windows in America/Chicago local time (24-hour clock;
  a window like 22 → 6 wraps overnight). The first matching window wins.
- `default_interval_min`: used when no window matches. Shipped default:
  15 minutes everywhere, which is the previous behavior.
- `boost`: a quick change — a different frequency that lasts until
  `until` (UTC), then the schedule takes over again on its own. An
  expired boost is cleared by the next real run.
- A run only counts as a check when it actually checks: skipped wake-ups
  write nothing and commit nothing. A manual workflow run always checks
  (it bypasses the gate), which is also how you force an immediate check
  after changing the schedule.
- 5 minutes is the fastest possible rhythm (the workflow's wake-up
  cadence). Speeds are rounded to what the wake-ups can actually hit.

The dashboard has a "Check-in rhythm" panel for editing all of this
directly. The dashboard is a static page, so saving from it goes through
GitHub's contents API with a fine-grained key the owner pastes once into
the panel: this repo only, read + write on Contents. The key is kept in
that browser's localStorage and sent only to api.github.com — never
pasted in chat, never committed. Without the key, the schedule can still
be edited by hand in `data/schedule.json` on GitHub, or via a manual
request in chat.
