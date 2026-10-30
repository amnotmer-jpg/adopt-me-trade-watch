#!/usr/bin/env python3
"""Elvebredd Trade Center scraper — Playwright + headless Chromium.

Why a browser is needed: the Trade Center page serves only skeleton
placeholders in raw HTML. The real listing cards arrive over an
authenticated WebSocket feed that a plain script cannot use (it answers
401 without a browser session). A headless Chromium session loads the
page exactly like a normal visitor, so the feed connects and the cards
render — and this module reads them from the rendered DOM.

What a card looks like in the DOM (verified from the page's rendered
text and the Muse browser watch's card format):
  * Pet identity comes from image URLs: /images/pets/<Pet Name>.png
    (e.g. "Add.png" is an Add item, "Albino Bat.png" is that pet).
  * Badges come from overlay images: /images/amoverlays/<BADGE>.svg
    (e.g. MFR.svg). A pet image with no overlay badge is plain/no-pot.
  * Each card carries a "You" label, a username, a relative time
    ("2s ago"), and a trades count.

Extraction strategy (tolerant, because the site's class names change):
  1. Wait for pet images to render, then give the live feed a few
     seconds to fill in.
  2. Try the page's Search box ("What are you looking for?") per pet:
     type the pet name, wait, extract. If the search box is missing or
     filtering yields nothing, fall back to one unfiltered extraction
     and filter client-side by pet name.
  3. Group pet images into cards: for each pet image, walk up to the
     smallest ancestor whose text contains a relative-time string
     ("…ago") — that ancestor is the card container.
  4. Split You/Them inside a card: pet images cluster into two groups
     (the You side and the owner's side). With two clusters, DOM order
     is You first, owner second (the "You" label leads the card). Any
     other cluster count is still emitted, but flagged
     split_verified=False so the dashboard can say so honestly.
  5. Username: the card's heading text that looks like a username
     (alphanumeric + underscore, not a number, not "You", not "Add").
     Relative time: the first "…ago" string in the card.

Substring-fragment guard: Elvebredd's search matches by substring, so
searching "Monkey King" also matches "Monkey King Crown", and the
search hides the listing's non-matching items. A card that renders
under that search as exactly "pet alone" on one side and only
"pet + extra words" items on the other is a fragment of a bigger
listing (verified Oct 2026: 7 such Monkey King / Monkey King Crown
cards were fragments of listings that also involved Sugar Axolotls),
not a real offer — so search-path cards matching that shape are
dropped and counted in the status. The search flow also tries to
click the site's exact item chip (role=option, exact name) before
extracting, falling back to the text search when no such chip exists.

Scope: first page of cards only (the same scope the Muse browser watch
used). No scrolling, no pagination, no login, no posting, no trading.

Output records mirror watch.py's AMTV record shape so the two sources
merge: {pet, source="elvebredd", you_side, them_side, username,
rel_time, status="offer", fingerprint, split_verified}.
Side entries are {name, badges, count}; duplicate pet+badge entries on
one side collapse into count (the dashboard renders "xN").

Rate limits: one page load per run (plus one search per pet when the
search box works), sequential, polite waits. On any failure the
function returns a status string and no cards — it never raises into
the caller, so the AMTV half of a run still completes.
"""

import hashlib
import json
import os
import re
import time
import urllib.parse

TRADE_CENTER = "https://elvebredd.com/trade-center"

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# Badge codes seen on Elvebredd overlays. Anything else in an overlay
# filename is passed through verbatim; no badge at all means plain.
KNOWN_BADGES = {"FR", "NFR", "MFR", "F", "R", "N", "M", "NR", "MR", "NF"}

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,30}$")
AGO_RE = re.compile(r"\d+\s*(?:s|sec|secs|second|seconds|m|min|mins|"
                    r"minute|minutes|h|hr|hrs|hour|hours|d|day|days)"
                    r"\s*ago", re.I)
SKIP_WORDS = {"you", "add", "trades", "trade", "ago", "offer", "offers",
              "listing", "listings", "value", "difference", "search",
              "create"}


def _pet_name_from_src(src):
    """'/images/pets/Albino Bat.png?...' -> 'Albino Bat'."""
    path = urllib.parse.urlparse(src).path
    name = os.path.basename(path)
    if name.lower().endswith(".png"):
        name = name[:-4]
    return urllib.parse.unquote(name)


def _badge_from_overlay_src(src):
    """'/images/amoverlays/MFR.svg?...' -> 'MFR' (else None)."""
    path = urllib.parse.urlparse(src).path
    base = os.path.basename(path)
    if base.lower().endswith(".svg"):
        return base[:-4]
    return None


def _collapse(entries):
    """Merge duplicate (name, badges) entries into counts, keeping order."""
    merged = {}
    order = []
    for name, badges in entries:
        key = (name, badges)
        if key not in merged:
            merged[key] = {"name": name, "badges": badges, "count": 0}
            order.append(key)
        merged[key]["count"] += 1
    return [merged[k] for k in order]


def _fingerprint(pet_key, username, you_side, them_side):
    """Stable id for dedupe. Relative time is deliberately excluded:
    Elvebredd times drift as the feed refreshes, and the Muse watch
    dedupes by username + contents, not by time."""
    src = json.dumps([pet_key, username, you_side, them_side],
                     sort_keys=True)
    return "elve-" + hashlib.sha256(src.encode()).hexdigest()[:20]


# ---------------------------------------------------------------- JS

# Runs inside the page. Returns a list of raw cards:
# {username, rel_time, clusters: [[(pet, badge), ...], ...]}
# where clusters are the pet-image groups in DOM order.
EXTRACT_JS = r"""
() => {
  const agoRe = /\d+\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)\s*ago/i;
  const userRe = /^[A-Za-z0-9_]{3,30}$/;
  const skip = new Set(["you","add","trades","trade","ago","offer","offers",
                        "listing","listings","value","difference","search","create"]);
  const petImgs = [...document.querySelectorAll('img[src*="/images/pets/"]')];
  // Group pet images by card container: smallest ancestor containing an
  // "ago" time string.
  const cards = new Map();
  for (const img of petImgs) {
    let card = null, el = img;
    while (el) {
      if (agoRe.test(el.innerText || '')) { card = el; break; }
      el = el.parentElement;
    }
    if (!card) card = document.body;
    if (!cards.has(card)) cards.set(card, []);
    cards.get(card).push(img);
  }
  const out = [];
  for (const [card, imgs] of cards) {
    const text = card.innerText || '';
    const timeM = text.match(agoRe);
    // Username: first heading-like text that looks like a username.
    let username = null;
    const heads = [...card.querySelectorAll('h1,h2,h3,h4,h5')];
    for (const h of heads) {
      const t = (h.innerText || '').trim();
      if (userRe.test(t) && !skip.has(t.toLowerCase())) { username = t; break; }
    }
    if (!username) {
      for (const tok of text.split(/\s+/)) {
        const t = tok.trim();
        if (userRe.test(t) && !skip.has(t.toLowerCase())
            && !/^\d+$/.test(t)) { username = t; break; }
      }
    }
    // Badge for one pet image: an overlay image inside the same wrapper.
    function badgeFor(img) {
      let el = img.parentElement;
      for (let d = 0; d < 3 && el; d++) {
        const ov = el.querySelector('img[src*="/images/amoverlays/"]');
        if (ov) {
          const m = ov.getAttribute('src').match(/\/amoverlays\/([^\/\?]+)\.svg/i);
          if (m) return m[1];
        }
        el = el.parentElement;
      }
      return null;
    }
    // Clusters: consecutive pet images sharing an immediate parent.
    const clusters = [];
    let curParent = null, cur = [];
    for (const img of imgs) {
      const p = img.parentElement;
      if (p !== curParent) {
        if (cur.length) clusters.push(cur);
        curParent = p; cur = [];
      }
      const src = img.getAttribute('src') || '';
      const name = decodeURIComponent(src.split('/').pop().replace(/\.png.*$/i, ''));
      cur.push([name, badgeFor(img)]);
    }
    if (cur.length) clusters.push(cur);
    out.push({username, rel_time: timeM ? timeM[0] : null, clusters});
  }
  return out;
}
"""


def _is_substring_fragment(you_side, them_side, pet_name):
    """True for the phantom shape: one side is exactly the tracked
    pet alone, and the other side holds ONLY items whose names are
    the pet name plus extra words (e.g. "Monkey King Crown" for
    "Monkey King"). Such cards arrive through Elvebredd's substring
    search, which hides the listing's non-matching items, so the
    rendered card is a fragment of the real listing, not the listing
    itself. Genuine pure pet-for-extended-item offers match too and
    are dropped deliberately: they are junk lowballs (Crown 3.25 vs
    Monkey King no-pot 67 on Elvebredd) and keeping them misleads
    the dashboard."""
    prefix = pet_name.lower() + " "
    for alone, other in ((you_side, them_side), (them_side, you_side)):
        if (len(alone) == 1 and alone[0]["name"] == pet_name
                and other
                and all(e["name"].lower().startswith(prefix)
                        for e in other)):
            return True
    return False


def _raw_to_records(raw_cards, pet_key, pet_name, from_search=False):
    """Filter raw cards to one pet and convert to watch.py records.
    Returns (records, dropped): dropped counts search-path cards
    discarded by the substring-fragment rule. The rule applies only
    to the search path, where the site hides non-matching items;
    unfiltered-path cards are complete and are never dropped."""
    records = []
    dropped = 0
    for raw in raw_cards:
        clusters = raw["clusters"]
        if not clusters:
            continue
        # Split: two clusters -> You first, owner second. Otherwise
        # everything goes on Them with the split flagged unverified
        # (a single cluster cannot be assigned to a side honestly).
        if len(clusters) == 2:
            you_raw, them_raw = clusters
            split_verified = True
        else:
            you_raw, them_raw = [], [e for c in clusters for e in c]
            split_verified = False
        you_side = _collapse([(n, b or "plain") for n, b in you_raw])
        them_side = _collapse([(n, b or "plain") for n, b in them_raw])
        # Does this card mention the tracked pet on either side?
        mentioned = any(e["name"] == pet_name
                        for e in you_side + them_side)
        if not mentioned:
            continue
        if from_search and _is_substring_fragment(you_side, them_side,
                                                  pet_name):
            dropped += 1
            continue
        records.append({
            "pet": pet_key,
            "source": "elvebredd",
            "you_side": you_side,
            "them_side": them_side,
            "you_rp": None,
            "them_rp": None,
            "win_lose": None,
            "username": raw["username"],
            "lister": None,
            "status": "offer",
            "rel_time": raw["rel_time"],
            "exact_time": None,
            "split_verified": split_verified,
            "fingerprint": _fingerprint(pet_key, raw["username"],
                                        you_side, them_side),
        })
    return records, dropped


def _find_search_box(page):
    """Locate the Trade Center search input, if one is usable."""
    for loc in [
        page.locator('input[placeholder*="earch"]'),
        page.locator('input[type="search"]'),
        page.get_by_role("textbox").first,
    ]:
        try:
            if loc.count() and loc.first.is_visible(timeout=3000):
                return loc.first
        except Exception:
            continue
    return None


def fetch_elvebredd_browser(pet_names, wait_s=4.0):
    """Scrape Elvebredd Trade Center with headless Chromium.

    pet_names: {pet_key: display name}. Returns
    {"status": str, "method": str, "cards": {pet_key: [records]}}.
    Never raises: any failure is reported in "status" with no cards.
    """
    result = {"status": "not attempted", "method": "none",
              "cards": {k: [] for k in pet_names}}
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        result["status"] = ("browser unavailable "
                            "(playwright not installed)")
        return result

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"])
            ctx = browser.new_context(user_agent=UA,
                                      viewport={"width": 1280,
                                                "height": 900})
            page = ctx.new_page()
            try:
                page.goto(TRADE_CENTER, timeout=60000,
                          wait_until="domcontentloaded")
                try:
                    page.wait_for_selector(
                        'img[src*="/images/pets/"]', timeout=30000)
                except Exception:
                    pass
                page.wait_for_timeout(int(wait_s * 1000))

                search = _find_search_box(page)
                if search is not None:
                    # Filtered path: one search per pet.
                    method = "search-text"
                    total_dropped = 0
                    for key, name in pet_names.items():
                        try:
                            search.fill("")
                            search.fill(name)
                            page.wait_for_timeout(1500)
                            # Prefer the site's exact item chip over
                            # its substring text search: typing
                            # "Monkey King" also matches
                            # "Monkey King Crown". Only click a
                            # proper option element with the exact
                            # name; otherwise keep the text search.
                            try:
                                chip = page.get_by_role(
                                    "option", name=name,
                                    exact=True).first
                                if chip.is_visible(timeout=2000):
                                    chip.click()
                                    method = "search-chip"
                                    page.wait_for_timeout(2000)
                            except Exception:
                                pass
                            page.wait_for_timeout(3000)
                            raw = page.evaluate(EXTRACT_JS)
                            recs, dropped = _raw_to_records(
                                raw, key, name, from_search=True)
                            result["cards"][key] = recs
                            total_dropped += dropped
                        except Exception as exc:
                            print(f"[elvebredd browser] {key} search "
                                  f"failed: {type(exc).__name__}")
                    total = sum(len(v)
                                for v in result["cards"].values())
                    if total:
                        result["method"] = method
                        result["status"] = (
                            f"browser ok ({total} cards via {method}; "
                            f"dropped {total_dropped} substring-"
                            f"fragment cards)")
                    else:
                        result["method"] = "search-filter-empty"
                        result["status"] = (
                            "browser loaded but search filtering "
                            "yielded no cards — falling back")
                        search = None  # fall through to unfiltered
                if search is None:
                    # Unfiltered path: one extraction, client-side filter.
                    raw = page.evaluate(EXTRACT_JS)
                    for key, name in pet_names.items():
                        recs, _ = _raw_to_records(raw, key, name,
                                                  from_search=False)
                        result["cards"][key] = recs
                    total = sum(len(v)
                                for v in result["cards"].values())
                    result["method"] = "unfiltered+client-filter"
                    result["status"] = (
                        f"browser ok ({total} cards, unfiltered feed)")
            finally:
                browser.close()
    except Exception as exc:
        result["status"] = (f"browser failed: {type(exc).__name__}: "
                            f"{str(exc)[:150]}")
        result["cards"] = {k: [] for k in pet_names}
    return result
