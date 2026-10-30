#!/usr/bin/env python3
"""
Adopt Me trade watch — standalone checker + dashboard generator.

What it does (read-only, no login, no posting, no trading):
  * Fetches the Adopt Me Trading Values (AMTV) pet pages and the public
    "Recent Offers & Trades" snapshot endpoint for three pets:
      - Sugar Axolotl      (AMTV id 2550)
      - Strawberry Shortcake Bat Dragon / SSBD (AMTV id 1427)
      - Monkey King        (AMTV id 14)
    AMTV other-side pets are icons, not text, so offered pets are reported
    as icon ids + badge codes (F/R/N/M). Names are NEVER invented.
  * Fetches the Elvebredd Trade Center page and reads the pet value catalog
    embedded in it (current Elvebredd values for the three pets), then
    loads the same page in headless Chromium (see elvebredd_browser.py)
    to read the live listing cards the page renders over its feed —
    each card as You (what the owner wants) / Them (what the owner
    offers), with badge codes and usernames. Elvebredd listings are
    offers, not completed trades.
  * Dedupes all trades against data/seen.json (this script's OWN state).
  * Writes data/listings.json + index.html (the dashboard).
  * If NTFY_TOPIC is set and new items were found, POSTs one short push
    message per pet to ntfy.sh (free, no account). The message is generic
    (pet + count + dashboard link) and NEVER mentions inventory or matches.
    If NTFY_TOPIC is unset, it only prints what it would send.

Rate limits: requests are sequential with a small delay. On HTTP 429/403
for a site, that site's check stops for this run (no retries, no endpoint
switching to evade a block).

Usage:  python3 watch.py            (run from the repo root)
Env:    NTFY_TOPIC     private ntfy.sh topic (GitHub Actions secret)
        DASHBOARD_URL  public dashboard URL, used as the ntfy click link
Dependencies: Python 3.10+ standard library, plus Playwright +
Chromium for the Elvebredd browser step (installed by the workflow;
without them that step reports "browser unavailable" and AMTV still runs).
"""

import hashlib
import html as htmlmod
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# ---------------------------------------------------------------- config

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
SEEN_PATH = os.path.join(DATA_DIR, "seen.json")
LISTINGS_PATH = os.path.join(DATA_DIR, "listings.json")
INDEX_PATH = os.path.join(ROOT, "index.html")

# AMTV id -> pet name, from the user's master database (pet_names.json
# sits next to this script in the repo). Missing file or unknown id
# simply means the "icon #..." label stays — names are never invented.
PET_NAMES_PATH = os.path.join(ROOT, "pet_names.json")


def load_pet_names():
    try:
        with open(PET_NAMES_PATH) as fh:
            data = json.load(fh)
        return {str(k): v for k, v in data.items()
                if isinstance(v, str)}
    except Exception:
        return {}


PET_NAMES = load_pet_names()

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# AMTV pet ids verified from adoptmetradingvalues.com/what-is-worth/<id>
PETS = {
    "sugar_axolotl": {"name": "Sugar Axolotl", "amtv_id": "2550"},
    "ssbd": {"name": "Strawberry Shortcake Bat Dragon", "amtv_id": "1427"},
    "monkey_king": {"name": "Monkey King", "amtv_id": "14"},
}

AMTV_PAGE = "https://adoptmetradingvalues.com/what-is-worth/{id}"
AMTV_SNAPSHOT = ("https://adoptmetradingvalues.com/gettrades.php"
                 "?item={id}&type=Sticker&tstamp=")
ELVEBREDD_TRADE_CENTER = "https://elvebredd.com/trade-center"

REQUEST_DELAY = 2.0   # seconds between requests, keeps the cadence polite
RECENT_KEEP = 30      # recent items per pet kept for the dashboard
RECENT_SHOW = 10      # recent items per pet actually shown (older ones
                      # stay in storage as a dedupe buffer, but no longer
                      # clutter the dashboard)

BLOCKED = set()       # sites stopped this run after a 429/403


# ---------------------------------------------------------------- fetching

def fetch_text(url, timeout=30):
    """GET a URL as text. Returns (status, text) or (None, error-string).

    On 429/403 the caller should stop using that site for this run.
    Handles truncated chunked responses by returning the partial body.
    """
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        return exc.code, body
    except Exception as exc:  # IncompleteRead has .partial
        partial = getattr(exc, "partial", b"")
        if partial:
            return 200, partial.decode("utf-8", "replace")
        return None, f"{type(exc).__name__}: {exc}"


def site_blocked(site, status):
    """Record a 429/403 block and stop that site for this run."""
    if status in (429, 403):
        BLOCKED.add(site)
        print(f"[{site}] HTTP {status} — stopping this site for this run")
        return True
    return False


# ---------------------------------------------------------------- AMTV

def parse_amtv_value(page_html):
    """Current RP value from a pet page, e.g. 'Value</td><td>13.6 RP'."""
    m = re.search(r"Value</td><td>\s*([\d.]+)\s*RP", page_html)
    return float(m.group(1)) if m else None


def _badge_letters(div_html):
    """Badge code letters from a badge div (spans with rounded styling)."""
    letters = []
    for span in re.findall(r"<span[^>]*>([A-Za-z])</span>", div_html):
        if span in "FRNM":
            letters.append(span)
    return "".join(letters)


def _offer_icons(cell_html):
    """Offered pets: list of {icon_id, codes} in card order.

    Icons are image ids only — AMTV does not put names on offered pets.
    """
    icons = []
    # <img src='/images/21.png'> on pet pages, <image src="images\21.png">
    # in snapshots. Both are handled.
    for m in re.finditer(
            r'<div class="imgcontainer">\s*'
            r'<im\w*\s+src="images[\\/](\d+)\.png"[^>]*>'
            r'\s*<div class="bottom-right-mini">(.*?)</div>',
            cell_html, re.S):
        icons.append({"icon_id": m.group(1),
                      "codes": _badge_letters(m.group(2))})
    return icons


def parse_amtv_snapshot(snap_html, pet_key):
    """Parse a gettrades.php snapshot table into trade records.

    Each row has 3 cells: wanted side | offered side | status + times.
    The page's pet is the 100px image; it sits in cell A when the pet is
    listed and in cell B when the pet itself was offered. In both
    orientations, cell A = what the offerer WANTS (You) and cell B = what
    the offerer OFFERS (Them), and the offerer is the "Offered On By"
    username in cell B. Offered pets are icon ids only — never named.
    """
    records = []
    rows = re.findall(
        r'<tr style="vertical-align:bottom; text-align:center;">(.*?)</tr>',
        snap_html, re.S)
    for row in rows:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(cells) < 3:
            continue
        cell_a, cell_b, meta_cell = cells[0], cells[1], cells[2]

        def cell_side(cell):
            """Entries + RP total for one table cell."""
            if re.search(
                    r'<im\w*\s+src="images[\\/]\d+\.png"[^>]*height:100px',
                    cell):
                badges = ""
                m = re.search(
                    r"<div class=['\"]bottom-right['\"]>(.*?)</div>",
                    cell, re.S)
                if m:
                    badges = _badge_letters(m.group(1))
                entries = [{"name": PETS[pet_key]["name"],
                            "badges": badges or "plain", "count": 1}]
            else:
                entries = [{"name": f"icon #{o['icon_id']}",
                            "badges": o["codes"] or "plain", "count": 1}
                           for o in _offer_icons(cell)]
            m = re.search(r"font-weight:bold;[^>]*>([\d.]+)\s*RP", cell)
            rp = float(m.group(1)) if m else None
            return entries, rp

        you_side, you_rp = cell_side(cell_a)
        them_side, them_rp = cell_side(cell_b)
        wl = None
        m = re.search(r"class='[^']*wfl-(win|lose)[^']*'[^>]*>([^<]+)",
                      cell_b)
        if m:
            wl = m.group(2).strip()
        username = None
        m = re.search(r"Offered On By:<br>([^<]+)<br>", cell_b)
        if m:
            username = htmlmod.unescape(m.group(1)).strip()
        lister = None
        m = re.search(r"Listed By:<br>([^<]+)", cell_a)
        if m:
            lister = htmlmod.unescape(m.group(1)).strip()

        status = None
        m = re.search(r"history-status-badge[^>]*>([^<]+)</span>", meta_cell)
        if m:
            status = m.group(1).strip()
        rel_time = None
        m = re.search(r">([^<>]*ago[^<>]*)<", meta_cell)
        if m:
            rel_time = m.group(1).strip()
        exact_time = None
        m = re.search(r"\((\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\)", meta_cell)
        if m:
            exact_time = m.group(1)

        fingerprint_src = json.dumps(
            [pet_key, exact_time or rel_time, you_side, them_side,
             username, status], sort_keys=True)
        # Attach the AMTV item id AFTER fingerprinting so existing rows keep
        # their fingerprints (no re-notification spam). "icon #1588" links to
        # adoptmetradingvalues.com/what-is-worth/1588 on the dashboard.
        for _side in (you_side, them_side):
            for _e in _side:
                _m = re.match(r"icon #(\d+)$", _e["name"])
                if _m:
                    _e["amtv_id"] = _m.group(1)
        records.append({
            "pet": pet_key,
            "source": "amtv",
            "you_side": you_side,
            "them_side": them_side,
            "you_rp": you_rp,
            "them_rp": them_rp,
            "win_lose": wl,
            "username": username,
            "lister": lister,
            "status": status,
            "rel_time": rel_time,
            "exact_time": exact_time,
            "fingerprint": hashlib.sha256(
                fingerprint_src.encode()).hexdigest()[:20],
        })
    return records


# ---------------------------------------------------------------- Elvebredd

def parse_elvebredd_catalog(page_html):
    """Current Elvebredd values for the three pets, from the pet catalog
    JSON embedded in the Trade Center page. Returns {pet_name: values}."""
    # The catalog sits inside a JS string in the page, so its quotes arrive
    # backslash-escaped (\"name\":...). Unescape quotes before matching.
    page_html = page_html.replace('\\"', '"')
    catalog = {}
    # Catalog objects are flat: {"image":"/images/pets/<Name>.png", ...,
    # "rvalue":N, "nvalue":N, "mvalue":N, "rvalue - nopotion":N, ...,
    # "name":"<Name>", ...}. The same catalog appears twice; dedupe.
    for obj in re.findall(r"\{[^{}]*\"image\":\"/images/pets/[^{}]+\}",
                          page_html):
        name_m = re.search(r'"name":"([^"]+)"', obj)
        if not name_m:
            continue
        name = name_m.group(1)
        if name in catalog:
            continue

        def num(key):
            mm = re.search(r'"' + re.escape(key) + r'":([\d.]+)', obj)
            return float(mm.group(1)) if mm else None

        catalog[name] = {
            "regular": num("rvalue"),
            "no_potion": num("rvalue - nopotion"),
            "neon": num("nvalue"),
            "mega": num("mvalue"),
        }
    wanted = {p["name"] for p in PETS.values()}
    return {k: v for k, v in catalog.items() if k in wanted}


# ---------------------------------------------------------------- ntfy

def notify_ntfy(topic, title, message, click_url):
    """POST one push message to ntfy.sh. Returns True on 2xx."""
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}", data=message.encode("utf-8"),
        method="POST",
        headers={"Title": title, "Click": click_url} if click_url
        else {"Title": title})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return 200 <= resp.status < 300
    except Exception as exc:
        print(f"[ntfy] POST failed: {type(exc).__name__}: {exc}")
        return False


# ---------------------------------------------------------------- dashboard

DASHBOARD_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="900">
<title>Adopt Me Trade Watch</title>
<style>
body{font-family:system-ui,-apple-system,sans-serif;max-width:900px;margin:0 auto;padding:16px;background:#fafafa;color:#222}
h1{font-size:1.5em} h2{margin-top:2em;border-bottom:2px solid #ddd;padding-bottom:4px}
.card{background:#fff;border:1px solid #ddd;border-radius:10px;padding:12px;margin:10px 0}
.card.match{border:2px solid #2a9d3f;background:#f0fff4}
.card.match::before{content:"★ MATCH — your inventory covers this You side";display:block;font-weight:700;color:#2a9d3f;margin-bottom:6px}
.badge{display:inline-block;background:#eee;border-radius:6px;padding:1px 7px;font-size:.85em;margin-right:4px}
.badge.amtv{background:#dbeafe} .badge.elve{background:#fef3c7}
.side{margin:4px 0} .side b{display:inline-block;min-width:48px}
.meta{color:#666;font-size:.85em}
textarea{width:100%;height:110px;font-family:inherit}
button{padding:8px 16px;margin:6px 6px 0 0;cursor:pointer}
.note{background:#fff7ed;border:1px solid #fcd34d;border-radius:8px;padding:10px;margin:12px 0}
small{color:#666}
</style>
</head>
<body>
<h1>🐾 Adopt Me Trade Watch</h1>
<p class="meta">Last check: __GENERATED_AT__ · refreshes every 15 minutes.
AMTV rows are snapshot offers/trades; Elvebredd listings are offers, not completed trades.</p>

<div class="note"><b>Your inventory (private to this browser):</b> type the pets you can
trade, one per line, e.g. <code>FR Crow</code>,
<code>Turtle</code> (no badge = plain/no-pot),
<code>Ride Potion x2</code>. Saved only in this
browser's localStorage — it is never sent anywhere and never appears in the
page source. Cards whose <b>You</b> side you can fill exactly get a green
★ MATCH highlight. Badges must match exactly (a plain pet does not fill an
FR ask, and 2× FR does not fill from 1× FR). A match only ever comes
from the You side (what you'd give), never from the Them side (what's
offered) — plain, FR, MFR and the rest are all different to the matcher.
<br><textarea id="inv" placeholder="FR Crow&#10;Turtle&#10;Ride Potion x2"></textarea>
<br><button onclick="saveInv()">Save inventory</button>
<button onclick="clearInv()">Clear</button>
<span id="invStatus" class="meta"></span>
<br><label><input type="checkbox" id="onlyMine" onclick="toggleOnlyMine()"> For me only — show just cards whose You side I can fill exactly</label>
<br><label><input type="checkbox" id="activeOnly" onclick="toggleActiveOnly()"> Active only — hide completed history</label>
<span id="onlyMineHint" class="meta"></span></div>

<div id="sections"></div>

<script type="application/json" id="watch-data">__DATA_JSON__</script>
<script>
// Parse an inventory line into {name, badges, count}. "FR Pet" -> badges "FR";
// no badge words -> "plain". "Ride Potion x3" -> {name:"Ride Potion", count:3}.
function parseInvLine(line){
  line=line.trim(); if(!line) return null;
  let count=1; const cm=line.match(/x(\d+)\s*$/i);
  if(cm){count=parseInt(cm[1],10); line=line.slice(0,cm.index).trim();}
  const bm=line.match(/^(FR|NFR|MFR|NR|MR|NF|F|R|N|M)\s+(.+)$/);
  if(bm) return {name:bm[2].trim(), badges:bm[1], count};
  return {name:line, badges:"plain", count};
}
function getInv(){
  try{return (JSON.parse(localStorage.getItem("amw-inventory")||"[]"));}
  catch(e){return [];}
}
function saveInv(){
  const items=document.getElementById("inv").value.split("\n")
    .map(parseInvLine).filter(Boolean);
  localStorage.setItem("amw-inventory", JSON.stringify(items));
  document.getElementById("invStatus").textContent="Saved ("+items.length+" entries).";
  render();
}
function clearInv(){
  localStorage.removeItem("amw-inventory");
  document.getElementById("inv").value="";
  document.getElementById("invStatus").textContent="Cleared.";
  render();
}
// A card matches when every You-side entry is covered by inventory:
// same pet name, exact same badges, enough count. "Add"/"Add Big"/"Add Small"
// and unknown extras do not match (they are not in the inventory format).
function isMatch(youSide, inv){
  const stock={};
  inv.forEach(it=>{stock[it.name+"|"+it.badges]=(stock[it.name+"|"+it.badges]||0)+it.count;});
  for(const it of youSide){
    const key=it.name+"|"+it.badges;
    if(!stock[key]||stock[key]<it.count) return false;
    stock[key]-=it.count;
  }
  return youSide.length>0;
}
function sideHtml(side){
  if(!side||!side.length) return "<i>—</i>";
  return side.map(it=>{
    const b=it.badges==="plain"?"plain":it.badges;
    const label=it.amtv_id
      ?"<a href='https://adoptmetradingvalues.com/what-is-worth/"+it.amtv_id+"' target='_blank'>"+it.name+"</a>"
      :it.name;
    return label+" ("+b+")"+(it.count>1?" ×"+it.count:"");
  }).join(", ");
}
// A card is "for me" only when its You side (what you'd give) is exactly
// covered by inventory. The Them side (what's offered) never counts, and
// badges must match exactly: plain, FR, MFR etc. are all different.
function youSideOf(it){
  return (it.you_side||[]).map(s=>({name:s.name,badges:s.badges,count:s.count||1}));
}
function isForMe(it, inv){
  return it.status!=="Completed" && isMatch(youSideOf(it), inv);
}
function toggleOnlyMine(){
  try{localStorage.setItem("amw-only-mine", document.getElementById("onlyMine").checked?"1":"0");}catch(e){}
  render();
}
function toggleActiveOnly(){
  try{localStorage.setItem("amw-active-only", document.getElementById("activeOnly").checked?"1":"0");}catch(e){}
  render();
}
// Live relative time for Elvebredd cards. Their scraped "12s ago"
// string is frozen at scrape time and drifts, so it must never be
// shown as if live. first_seen is a real UTC timestamp recorded
// when the watcher first saw the card, so the dashboard ticks
// "first seen X ago" from it instead (refreshed every 30s).
function relAgo(utcStr){
  try{
    const d=new Date(utcStr.replace(" ","T").replace(" UTC","Z"));
    if(isNaN(d)) return null;
    let s=Math.floor((Date.now()-d.getTime())/1000);
    if(s<0) s=0;
    if(s<60) return s+"s ago";
    if(s<3600) return Math.floor(s/60)+"m ago";
    if(s<86400) return Math.floor(s/3600)+"h ago";
    return Math.floor(s/86400)+"d ago";
  }catch(e){ return null; }
}
function tickLiveAgo(){
  document.querySelectorAll(".live-ago").forEach(el=>{
    const ago=relAgo(el.getAttribute("data-ts")||"");
    if(ago) el.textContent="first seen "+ago;
  });
}
function render(){
  const data=JSON.parse(document.getElementById("watch-data").textContent);
  const inv=getInv();
  document.getElementById("inv").value=inv.map(it=>
    (it.badges==="plain"?"":it.badges+" ")+it.name+(it.count>1?" x"+it.count:"")).join("\n");
  const cb=document.getElementById("onlyMine");
  let onlyMine=false;
  if(cb){
    let pref=null; try{pref=localStorage.getItem("amw-only-mine");}catch(e){}
    onlyMine = pref!==null ? pref==="1" : inv.length>0;
    if(!inv.length) onlyMine=false;
    cb.checked=onlyMine;
    document.getElementById("onlyMineHint").textContent =
      inv.length ? "" : "Save your inventory above to use For me only.";
  }
  const ab=document.getElementById("activeOnly");
  let activeOnly=false;
  if(ab){
    try{activeOnly=localStorage.getItem("amw-active-only")==="1";}catch(e){}
    ab.checked=activeOnly;
  }
  function sectionHtml(items){
    const enriched=items.map(it=>({it, mine:isForMe(it, inv)}));
    enriched.sort((a,b)=>(b.mine-a.mine));
    let shown=enriched;
    if(onlyMine&&inv.length) shown=shown.filter(e=>e.mine);
    if(activeOnly) shown=shown.filter(e=>e.it.status!=="Completed");
    return {html: shown.map(e=>cardHtml(e.it)).join(""), shown: shown.length, total: items.length};
  }
  let out="";
  for(const pet of data.pets){
    out+="<h2>"+pet.name+"</h2>";
    out+="<p class='meta'>AMTV value: "+(pet.amtv_value??"—")+" RP · Elvebredd: "+
      "regular "+(pet.elvebredd.regular??"—")+", no-pot "+(pet.elvebredd.no_potion??"—")+
      ", neon "+(pet.elvebredd.neon??"—")+", mega "+(pet.elvebredd.mega??"—")+
      "<br>AMTV check: "+pet.amtv_status+" · Elvebredd feed: "+pet.elvebredd_status+"</p>";
    const filtering=(onlyMine&&inv.length)||activeOnly;
    let tag="";
    if(onlyMine&&inv.length) tag+=" — for you";
    if(activeOnly) tag+=" — active only";
    if(pet.new_items.length){
      const s=sectionHtml(pet.new_items);
      out+="<h3>New this check"+tag+(filtering?" ("+s.shown+" of "+s.total+")":" ("+s.total+")")+"</h3>";
      out+=s.html||"<p class='meta'>"+(filtering?"Nothing here with these filters.":"None yet.")+"</p>";
    }
    const r=sectionHtml(pet.recent_items);
    out+="<h3>Recent"+tag+(filtering?" ("+r.shown+" of "+r.total+")":" ("+r.total+")")+"</h3>";
    out+=r.html||"<p class='meta'>"+(filtering?"Nothing here with these filters.":"None yet.")+"</p>";
  }
  function cardHtml(it){
    const actionable=it.status!=="Completed";
    const cls=(actionable&&isMatch(youSideOf(it),inv))?"card match":"card";
    const src=it.source==="amtv"?"AMTV":"Elvebredd";
    // Elvebredd rows carry no absolute listing time, so their frozen
    // scraped rel_time is replaced by a live "first seen X ago"
    // ticking from first_seen. AMTV rows keep their static time.
    let whenHtml=it.exact_time||it.rel_time||it.first_seen||"";
    if(it.source!=="amtv" && it.first_seen){
      const ago=relAgo(it.first_seen);
      whenHtml=ago
        ?"<span class='live-ago' data-ts='"+it.first_seen+"'>first seen "+ago+"</span>"
        :("first seen "+it.first_seen);
    }
    return "<div class='"+cls+"'><span class='badge "+(it.source==="amtv"?"amtv":"elve")+"'>"+src+
      "</span> <span class='meta'>"+whenHtml+(it.username?" · offered by @"+it.username:"")+
      (it.lister?" · listed by @"+it.lister:"")+
      (it.status?" · "+it.status:"")+
      (it.you_rp!=null?" · You "+it.you_rp+" RP":"")+
      (it.them_rp!=null?" · Them "+it.them_rp+" RP":"")+"</span>"+
      "<div class='side'><b>You:</b> "+sideHtml(it.you_side)+"</div>"+
      "<div class='side'><b>Them:</b> "+sideHtml(it.them_side)+"</div>"+
      (it.note?"<div class='meta'>"+it.note+"</div>":"")+"</div>";
  }
  document.getElementById("sections").innerHTML=out;
}
render();
setInterval(tickLiveAgo, 30000);
</script>
</body>
</html>
"""


def amtvs_to_cards(records):
    """Convert snapshot/listing records to dashboard cards.

    You = what the offerer wants, Them = what the offerer offers.
    Cards carry their source so nothing is misread across sites;
    Completed AMTV rows are history, not actionable offers. Elvebredd
    cards whose You/Them split could not be verified carry a note.
    """
    cards = []
    for r in records:
        # Backfill AMTV item ids for stored rows parsed before the link
        # change: "icon #1588" -> amtv_id "1588", so old rows link too.
        # Fingerprints are unaffected (computed at parse time, not here).
        for _side in (r["you_side"], r["them_side"]):
            for _e in _side:
                if "amtv_id" not in _e:
                    _m = re.match(r"icon #(\d+)$", _e["name"])
                    if _m:
                        _e["amtv_id"] = _m.group(1)
                # Resolve a known AMTV id to its pet name; unknown ids
                # keep the "icon #..." label (never invent a name).
                _id = _e.get("amtv_id")
                if _id and _id in PET_NAMES:
                    _e["name"] = PET_NAMES[_id]
        note = None
        if r["win_lose"]:
            note = f"Offer vs listed value: {r['win_lose']}"
        if r.get("split_verified") is False:
            note = ((note + " · ") if note else "") + \
                "split unverified — sides may be swapped"
        cards.append({
            "source": r.get("source", "amtv"),
            "you_side": r["you_side"],
            "them_side": r["them_side"],
            "username": r["username"],
            "lister": r["lister"],
            "status": r["status"],
            "rel_time": r["rel_time"],
            "exact_time": r["exact_time"],
            "you_rp": r["you_rp"],
            "them_rp": r["them_rp"],
            "note": note,
            "first_seen": r.get("first_seen"),
            "fingerprint": r["fingerprint"],
        })
    return cards


def _is_substring_fragment(rec, pet_name):
    """True for the phantom shape in stored Elvebredd rows: one side
    is exactly the tracked pet alone, and the other side holds ONLY
    items whose names are the pet name plus extra words (e.g.
    "Monkey King Crown" for "Monkey King"). Such rows are search
    fragments (see elvebredd_browser.py), not real offers, so they
    are kept out of the dashboard's new/recent lists. Genuine pure
    pet-for-extended-item offers match too and are hidden
    deliberately: they are junk lowballs and keeping them misleads
    the dashboard. AMTV rows are never affected."""
    if rec.get("source") != "elvebredd":
        return False
    prefix = pet_name.lower() + " "
    sides = (rec.get("you_side") or [], rec.get("them_side") or [])
    for alone, other in (sides, (sides[1], sides[0])):
        if (len(alone) == 1 and alone[0].get("name") == pet_name
                and other
                and all((e.get("name") or "").lower().startswith(prefix)
                        for e in other)):
            return True
    return False


# ---------------------------------------------------------------- main

def load_seen():
    if os.path.exists(SEEN_PATH):
        try:
            with open(SEEN_PATH) as fh:
                data = json.load(fh)
            if isinstance(data, dict) and "fingerprints" in data:
                return data
        except Exception:
            pass
    return {"fingerprints": {}, "records": {}}


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    seen = load_seen()
    first_run = not seen["fingerprints"]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # --- Elvebredd: catalog values (page HTML) + WS feed attempt ---
    elve_values, elve_status = {}, "not checked"
    if "elvebredd" not in BLOCKED:
        # The page is ~1.3 MB and chunked reads sometimes truncate it
        # before the catalog block, so retry until the catalog parses.
        for _attempt in range(3):
            status, page = fetch_text(ELVEBREDD_TRADE_CENTER)
            if status is None:
                elve_status = f"fetch error: {page}"
                break
            if site_blocked("elvebredd", status):
                elve_status = f"HTTP {status} — stopped"
                break
            if status != 200:
                elve_status = f"HTTP {status}"
                break
            elve_values = parse_elvebredd_catalog(page)
            if elve_values:
                elve_status = ("catalog read "
                               f"({len(elve_values)} of 3 pets found)")
                break
            elve_status = "catalog empty (page truncated?) — retrying"
            time.sleep(REQUEST_DELAY)
    # --- Elvebredd: listings via headless Chromium (never fatal) ---
    elve_cards = {key: [] for key in PETS}
    elve_browser_status = "not attempted"
    try:
        from elvebredd_browser import fetch_elvebredd_browser
        browser_res = fetch_elvebredd_browser(
            {k: p["name"] for k, p in PETS.items()})
        elve_cards = browser_res["cards"]
        elve_browser_status = browser_res["status"]
    except Exception as exc:
        elve_browser_status = f"browser step error: {exc}"
    print(f"[elvebredd] browser: {elve_browser_status}")
    time.sleep(REQUEST_DELAY)

    # --- AMTV per pet ---
    dashboard_pets = []
    notify_lines = []
    for key, pet in PETS.items():
        amtv_status = "not checked"
        value = None
        records = []
        if "amtv" not in BLOCKED:
            status, page = fetch_text(AMTV_PAGE.format(id=pet["amtv_id"]))
            if status is None:
                amtv_status = f"page fetch error: {page}"
            elif site_blocked("amtv", status):
                amtv_status = f"HTTP {status} — stopped"
            elif status != 200:
                amtv_status = f"HTTP {status}"
            else:
                value = parse_amtv_value(page)
                amtv_status = "page ok"
            time.sleep(REQUEST_DELAY)
            if "amtv" not in BLOCKED:
                status, snap = fetch_text(
                    AMTV_SNAPSHOT.format(id=pet["amtv_id"]))
                if status is None:
                    amtv_status += f"; snapshot error: {snap}"
                elif site_blocked("amtv", status):
                    amtv_status += "; snapshot stopped"
                elif status != 200:
                    amtv_status += f"; snapshot HTTP {status}"
                else:
                    records = parse_amtv_snapshot(snap, key)
                    amtv_status += f"; snapshot ok ({len(records)} rows)"
            time.sleep(REQUEST_DELAY)

        known = set(seen["fingerprints"].get(key, []))
        # AMTV records + Elvebredd browser records share the per-pet
        # fingerprint space (Elvebredd ids are "elve-…" prefixed).
        all_records = records + elve_cards.get(key, [])
        new_records = []
        for rec in all_records:
            if rec["fingerprint"] not in known:
                rec["first_seen"] = now
                new_records.append(rec)
        # Keep the newest RECENT_KEEP records (snapshot order = newest
        # first) merged with previously seen ones, newest first.
        # Substring-fragment Elvebredd rows (phantom cards) are kept
        # out of the recent list, which also purges already-stored
        # ones from seen.json on the next run; their fingerprints
        # stay in the fingerprint set, so a re-scraped fragment
        # never counts as new.
        merged = {r["fingerprint"]: r for r in
                  seen["records"].get(key, [])}
        for rec in all_records:
            merged.setdefault(rec["fingerprint"], rec)
        recent = sorted(
            [r for r in merged.values()
             if not _is_substring_fragment(r, pet["name"])],
            key=lambda r: (r.get("exact_time") or r.get("first_seen")
                           or "", r["fingerprint"]),
            reverse=True)[:RECENT_KEEP]
        seen["fingerprints"][key] = sorted(
            {r["fingerprint"] for r in merged.values()})
        seen["records"][key] = recent

        # First run seeds silently: nothing counts as "new" yet.
        new_cards = [] if first_run else amtvs_to_cards(
            [r for r in new_records
             if not _is_substring_fragment(r, pet["name"])])
        recent_cards = amtvs_to_cards(recent)[:RECENT_SHOW]
        if not first_run and new_cards:
            notify_lines.append((pet["name"], len(new_cards)))
        dashboard_pets.append({
            "key": key,
            "name": pet["name"],
            "amtv_value": value,
            "elvebredd": elve_values.get(pet["name"], {}),
            "amtv_status": amtv_status,
            "elvebredd_status": f"{elve_status}; {elve_browser_status}",
            "new_items": new_cards,
            "recent_items": recent_cards,
        })
        print(f"[{key}] AMTV value={value} rows={len(records)} "
              f"elve={len(elve_cards.get(key, []))} "
              f"new={len(new_cards)} status={amtv_status}")

    # --- write outputs ---
    with open(SEEN_PATH, "w") as fh:
        json.dump(seen, fh, indent=2)
    dashboard = {"generated_at": now, "pets": dashboard_pets}
    with open(LISTINGS_PATH, "w") as fh:
        json.dump(dashboard, fh, indent=2)
    page_html = (DASHBOARD_TEMPLATE
                 .replace("__DATA_JSON__",
                          json.dumps(dashboard).replace("</", "<\\/"))
                 .replace("__GENERATED_AT__", now))
    with open(INDEX_PATH, "w") as fh:
        fh.write(page_html)
    print(f"wrote {INDEX_PATH} and {LISTINGS_PATH}")

    # --- notifications (generic: pet + count + link only) ---
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    dash_url = os.environ.get("DASHBOARD_URL", "").strip()
    if notify_lines:
        for pet_name, count in notify_lines:
            msg = f"{count} new {pet_name} offer(s) — see dashboard"
            if topic:
                ok = notify_ntfy(topic, "Adopt Me trade watch", msg,
                                 dash_url)
                print(f"[ntfy] {pet_name}: {'sent' if ok else 'FAILED'}")
            else:
                print(f"[ntfy] (no topic set — would send): {msg}")
    elif first_run:
        print("first run: seeded seen state, no notifications sent")
    else:
        print("no new items — no notifications")
    return 0


if __name__ == "__main__":
    sys.exit(main())
