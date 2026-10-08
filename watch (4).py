#!/usr/bin/env python3
"""
Booking availability watcher.

Runs ONCE per invocation (checks every watch, alerts on new openings, saves state).
Schedule it with launchd or cron -- see README comments at the bottom.

Files (next to this script):
  watch_config.json  - what to watch and who to notify
  watch_state.json   - what was available last time (auto-created)

Secrets come from environment variables, not the config file:
  SMTP_USER, SMTP_PASS                  (Gmail: use an App Password)
  TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM (only if sms_method = "twilio")
"""
import calendar
import datetime as dt
import difflib
import hashlib
import json
import os
import plistlib
import re
import smtplib
import subprocess
import sys
import time
from email.message import EmailMessage
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).parent
CONFIG_PATH = HERE / "watch_config.json"
STATE_PATH = HERE / "watch_state.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
                         "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"}

MONTHS_FR = {"Janvier": 1, "Février": 2, "Mars": 3, "Avril": 4, "Mai": 5, "Juin": 6,
             "Juillet": 7, "Août": 8, "Septembre": 9, "Octobre": 10, "Novembre": 11, "Décembre": 12}
MONTH_HDR = re.compile(r"(%s) (\d{4}) (?:[A-Z] ){6}[A-Z]\b" % "|".join(MONTHS_FR))


# ---------------------------------------------------------------- fetching
def launch_browser(pw):
    try:
        return pw.chromium.launch(channel="chrome")   # your installed Chrome
    except Exception:
        return pw.chromium.launch()                   # fallback: `playwright install chromium`


def page_text(url, render=False, req=None):
    """
    Visible text of a page.
      render=True : load it in a real browser (your installed Chrome) for JavaScript-built pages.
      req={...}   : custom request, e.g. {"method":"POST","headers":{...},"json":{...}} -- used to
                    call a site's hidden availability API directly (see README notes).
    """
    if render:
        from playwright.sync_api import sync_playwright  # pip3 install playwright
        with sync_playwright() as p:
            browser = launch_browser(p)
            page = browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=60000)
            if (req or {}).get("wait_for"):
                page.wait_for_selector(req["wait_for"], timeout=30000)
            html = page.content()
            browser.close()
    elif req:
        opts = {k: v for k, v in req.items() if k in ("headers", "json", "data", "params", "cookies")}
        r = requests.request(req.get("method", "GET"), url, timeout=30,
                             **{**opts, "headers": {**HEADERS, **opts.get("headers", {})}})
        r.raise_for_status()
        html = r.text
        if "json" in r.headers.get("content-type", ""):
            return html  # keep raw JSON for regex matching
    else:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        html = r.text
    return re.sub(r"\s+", " ", BeautifulSoup(html, "html.parser").get_text(" "))


# ------------------------------------------------- Tour du Mont-Blanc parser
def parse_calendar(block):
    """Turn calendar text ('<spots or –> <day>' cells under 'Month YYYY' headers) into {date: spots}."""
    headers = list(MONTH_HDR.finditer(block))
    result = {}
    for i, h in enumerate(headers):
        seg = block[h.end(): headers[i + 1].start() if i + 1 < len(headers) else len(block)]
        year, month = int(h.group(2)), MONTHS_FR[h.group(1)]
        pos = 0
        for day in range(1, calendar.monthrange(year, month)[1] + 1):
            cell = re.compile(rf"\s*([–—-]|\d+)\s+{day}(?!\d)").match(seg, pos)
            if not cell:
                break
            result[dt.date(year, month, day)] = 0 if cell.group(1) in "–—-" else int(cell.group(1))
            pos = cell.end()
    if not result:
        raise ValueError("Could not parse any calendar dates -- page layout may have changed")
    return result


def parse_tmb_refuge(text, refuge):
    """
    One refuge's calendar out of the big /disponibilites page. Each day renders as
    '<spots or –> <day number>', e.g. '– 10 – 11 2 12' means the 12th has 2 spots.
    A dash means full / closed / not yet open.
    """
    m = re.search(re.escape(refuge) + r".{0,120}?alt\.\s*\d+\s*m\s*/\s*\d+\s*personnes max\.", text)
    if not m:
        raise ValueError(f"Refuge '{refuge}' not found on page (name must match the site's text; try --list)")
    rest = text[m.end():]
    nxt = re.search(r"alt\.\s*\d+\s*m\s*/", rest)  # start of the next refuge's block
    return parse_calendar(rest[:nxt.start()] if nxt else rest)


def check_tmb(watch, state):
    if watch.get("refuge_url"):  # a single refuge's own page, e.g. .../en/refuges/relais-d-arpette
        url = watch["refuge_url"]
        days = parse_calendar(page_text(url, watch.get("render", False)))
    else:                        # the big all-refuges page, looked up by refuge name
        url = watch.get("url", "https://www.montourdumontblanc.com/fr/disponibilites")
        days = parse_tmb_refuge(page_text(url, watch.get("render", False)), watch["refuge"])
    lo = dt.date.fromisoformat(watch["date_from"]) if watch.get("date_from") else dt.date.today()
    hi = dt.date.fromisoformat(watch["date_to"]) if watch.get("date_to") else dt.date.max
    need = watch.get("min_spots", 1)
    anywhere = sum(1 for n in days.values() if n >= 1)
    print(f"  parsed {len(days)} days ({min(days)} to {max(days)}); {anywhere} have spots on ANY date")
    open_now = {d.isoformat(): n for d, n in days.items() if lo <= d <= hi and n >= need}
    new = {d: n for d, n in open_now.items() if d not in state.get("open", {})}
    state["open"] = open_now
    if new:
        lines = ", ".join(f"{d} ({n} spots)" for d, n in sorted(new.items())[:15])
        more = f" (+{len(new) - 15} more)" if len(new) > 15 else ""
        return f"{watch.get('refuge', watch['name'])}: now bookable -- {lines}{more}\n{url}"
    print(f"  {len(open_now)} date(s) currently open in window, nothing new")
    return None


# ------------------------------------------- generic "does this text appear"
def check_page_text(watch, state):
    """Alert when a phrase/regex appears (or disappears) on a page."""
    text = page_text(watch["url"], watch.get("render", False), watch.get("request"))
    if "alert_if_present" in watch:
        triggered = re.search(watch["alert_if_present"], text, re.I) is not None
    else:
        triggered = re.search(watch["alert_if_absent"], text, re.I) is None
    was, state["triggered"] = state.get("triggered", False), triggered
    if triggered and not was:
        return f"{watch['name']}: page changed -- looks bookable.\n{watch['url']}"
    print(f"  triggered={triggered}")
    return None


def check_page_changed(watch, state):
    """
    Alert when a page (or one part of it) changes. Best for small sites that announce a new
    season in plain text. Use "region" -- a regex with one capture group -- to watch only the
    relevant sentence, so cookie banners / timestamps elsewhere don't trigger false alerts.
    """
    text = page_text(watch["url"], watch.get("render", False), watch.get("request"))
    if watch.get("region"):
        m = re.search(watch["region"], text, re.I | re.S)
        if m is None:
            if state.get("hash") is None:
                raise ValueError("'region' pattern doesn't match this page -- the wording I expected isn't there")
            text = "[region not found]"      # it WAS there before and now isn't: that counts as a change
        else:
            text = m.group(1) if m.groups() else m.group(0)
    text = text.strip()
    digest = hashlib.sha256(text.encode()).hexdigest()
    prev, prev_text = state.get("hash"), state.get("text", "")
    state["hash"], state["text"] = digest, text[:3000]
    if prev is None:
        print(f"  baseline saved: {text[:120]!r}")
        return None
    if digest != prev:
        if prev_text:
            a, b = prev_text.split(), text[:3000].split()
            ops = difflib.SequenceMatcher(None, a, b).get_opcodes()
            was = " ... ".join(" ".join(a[i1:i2]) for t, i1, i2, j1, j2 in ops if t in ("replace", "delete"))
            now = " ... ".join(" ".join(b[j1:j2]) for t, i1, i2, j1, j2 in ops if t in ("replace", "insert"))
            detail = f"Was: {was[:250] or '(nothing)'}\nNow: {now[:250] or '(nothing)'}"
        else:
            detail = f"Now reads: {text[:300]}"
        return f"{watch['name']}: page text changed.\n{detail}\n{watch['url']}"
    print("  unchanged")
    return None


def check_browser_count(watch, state):
    """
    For date-pickers / calendars drawn by JavaScript where there's no usable API: open the page in
    Chrome, optionally click things (to open the picker / go to the next month), then count the
    elements matching a CSS selector -- e.g. the clickable (not disabled) days. Alert when the
    count rises (and is at least "min").
        "click":    ["#date-input", "button.next-month"]   (optional, clicked in order)
        "selector": "td.day:not(.disabled)"                (what counts as 'bookable')
    """
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = launch_browser(p)
        page = browser.new_page()
        page.goto(watch["url"], wait_until="networkidle", timeout=60000)
        for sel in watch.get("click", []):
            page.click(sel, timeout=15000)
            page.wait_for_timeout(600)
        n = page.locator(watch["selector"]).count()
        browser.close()
    prev, state["count"] = state.get("count", 0), n
    if n >= watch.get("min", 1) and n > prev:
        return f"{watch['name']}: {n} bookable date(s) showing now (was {prev}).\n{watch['url']}"
    print(f"  {n} matching element(s), nothing new")
    return None

def _window(watch):
    lo = dt.date.fromisoformat(watch["date_from"]) if watch.get("date_from") else dt.date.today()
    hi = dt.date.fromisoformat(watch["date_to"]) if watch.get("date_to") else dt.date.max
    return lo, hi, watch.get("min_spots", 1)


def _alert_new_dates(label, url, open_now, state):
    """Alert only for dates that were NOT open on the previous run."""
    new = {d: n for d, n in open_now.items() if d not in state.get("open", {})}
    state["open"] = open_now
    if not new:
        print(f"  {len(open_now)} date(s) currently open in window, nothing new")
        return None
    lines = ", ".join(f"{d} ({n} spots)" for d, n in sorted(new.items())[:15])
    more = f" (+{len(new) - 15} more)" if len(new) > 15 else ""
    return f"{label}: now bookable -- {lines}{more}\n{url}"


def check_lacblanc(watch, state):
    """
    Refuge du Lac Blanc: the booking calendar on their site is fed by a small JSON API.
    Step 1 fetches a short-lived *public* token, step 2 fetches the calendar with it.
    The calendar only lists months that are open, so a new month appearing = new availability.
    """
    base = "https://refugelacblanc.com/wp-json/reservation/v1/"
    hdr = {**HEADERS, "Accept": "application/json, text/plain, */*", "Referer": "https://refugelacblanc.com/en/"}
    t = requests.get(base + "getpublictoken", headers=hdr, timeout=30)
    t.raise_for_status()
    tok = re.search(r"eyJ[\w-]+\.[\w-]+\.[\w-]+", t.text)
    if not tok:
        raise ValueError("no token found in getpublictoken response -- the site may have changed it")
    r = requests.get(base + "getcalendardata", headers={**hdr, "Authorization": "Bearer " + tok.group(0)}, timeout=30)
    r.raise_for_status()
    data = r.json()
    lo, hi, need = _window(watch)
    months = data.get("dates_array", [])
    total = sum(1 for m in months for d in m.get("days", []) if d.get("date_status") == "active" and int(d.get("spaces", 0)) >= 1)
    print(f"  calendar lists {len(months)} month(s) ({', '.join(str(m.get('month')) for m in months) or 'none'}); "
          f"{total} bookable day(s) overall")
    open_now = {}
    for month in data.get("dates_array", []):
        for d in month.get("days", []):
            day = dt.datetime.strptime(d["date"], "%d/%m/%Y").date()
            if d.get("date_status") == "active" and int(d.get("spaces", 0)) >= need and lo <= day <= hi:
                open_now[day.isoformat()] = int(d["spaces"])
    return _alert_new_dates(watch["name"], "https://refugelacblanc.com/en/#calendar--bookings", open_now, state)


def check_bonatti_form(watch, state):
    """
    Rifugio Bonatti: the request form's date picker is blocked out by a list of date ranges written
    into the page itself (the 'data-disable-range' attribute on the check-in field). Right now that
    list blocks 1 Oct 2026 -> 31 Dec 2028. When they open the season they edit it, so we read it
    and alert when any date in your window stops being blocked (or when the list changes at all).
    """
    r = requests.get(watch["url"], headers=HEADERS, timeout=30)
    r.raise_for_status()
    inp = BeautifulSoup(r.text, "html.parser").find("input", attrs={"name": watch.get("field", "date-1")})
    if inp is None:
        raise ValueError("check-in field not found on the form page -- the form may have been rebuilt")
    rng_attr, single_attr = inp.get("data-disable-range", ""), inp.get("data-disable-date", "")
    ranges = []
    for part in rng_attr.split(","):
        f = re.findall(r"(\d{2})/(\d{2})/(\d{4})", part)           # MM/DD/YYYY
        if len(f) == 2:
            (m1, d1, y1), (m2, d2, y2) = f
            ranges.append((dt.date(int(y1), int(m1), int(d1)), dt.date(int(y2), int(m2), int(d2))))
    singles = {dt.date(int(y), int(m), int(d)) for m, d, y in re.findall(r"(\d{2})/(\d{2})/(\d{4})", single_attr)}

    today = dt.date.today()
    lo = dt.date.fromisoformat(watch["date_from"]) if watch.get("date_from") else today
    hi = dt.date.fromisoformat(watch["date_to"]) if watch.get("date_to") else dt.date(2028, 12, 31)

    def selectable(a, b):
        out, day = [], a
        while day <= b:
            if day not in singles and not any(x <= day <= y for x, y in ranges):
                out.append(day.isoformat())
            day += dt.timedelta(days=1)
        return out

    enabled = selectable(lo, hi)
    anywhere = selectable(today, dt.date(2028, 12, 31))
    prev = set(state.get("enabled", []))
    state["enabled"] = enabled
    state.pop("sig", None)
    print(f"  blocked ranges: {rng_attr or '(none)'}")
    print(f"  selectable on ANY date: {len(anywhere)} (first: {anywhere[0] if anywhere else 'none'}); in your window: {len(enabled)}")
    new = [d for d in enabled if d not in prev]
    if new:
        return (f"{watch['name']}: the booking form now allows dates -- {len(new)} newly selectable, "
                f"starting {new[0]}.\nBlocked ranges are now: {rng_attr or '(none)'}\n{watch['url']}")
    return None


def _expand_dates(items):
    """'2027-07-5' -> 2027-07-05.   '2027-07-05 to 2027-07-12' -> every day in that range."""
    out = []
    for item in items:
        found = re.findall(r"(\d{4})-(\d{1,2})-(\d{1,2})", str(item))
        try:
            if len(found) == 1:
                out.append(dt.date(*map(int, found[0])))
            elif len(found) == 2:
                a, b = (dt.date(*map(int, f)) for f in found)
                while a <= b:
                    out.append(a)
                    a += dt.timedelta(days=1)
            else:
                raise ValueError
        except ValueError:
            raise ValueError(f"can't read the date '{item}' -- write it like 2027-07-05, "
                             "or a range like '2027-07-05 to 2027-07-12'") from None
    return sorted(set(out))


def check_recgov(watch, state):
    """
    recreation.gov permit availability (same API your old shell script used).
    Config (all editable in watch_config.json):
        permit_id:    the number in the permit's web address
        campgrounds:  {"4675323057": "3L7 - Middle Lamar", ...}   (id -> name; ids from DevTools)
        dates:        ["2027-07-05", "2027-07-12 to 2027-07-15"]   (single days and/or ranges)
        min_spots:    permits needed before alerting
        remind_every_minutes: optional -- repeat the alert this often while it stays open
    """
    permit, need = watch["permit_id"], watch.get("min_spots", 2)
    divs = watch.get("divisions") or [{"id": k, "name": v} for k, v in (watch.get("campgrounds") or {}).items()]
    if not divs:
        raise ValueError("no campgrounds listed -- add a 'campgrounds' section")
    dates = [d for d in _expand_dates(watch["dates"]) if d >= dt.date.today()]
    if not dates:
        raise ValueError("every date in 'dates' is in the past -- update 'dates' in watch_config.json")
    months = sorted({(d.year, d.month) for d in dates})

    open_now, links = {}, {}
    for div in divs:
        for year, month in months:
            url = (f"https://www.recreation.gov/api/permititinerary/{permit}/division/{div['id']}"
                   f"/availability/month?month={month}&year={year}")
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            daily = ((r.json().get("payload") or {}).get("quota_type_maps") or {}).get("QuotaUsageByMemberDaily") or {}
            for d in dates:
                if (d.year, d.month) == (year, month):
                    remaining = int((daily.get(d.isoformat()) or {}).get("remaining", 0))
                    key = f"{div['name']} {d.isoformat()}"
                    print(f"  {key}: remaining={remaining}")
                    if remaining >= need:
                        open_now[key] = remaining
                        links[key] = (f"https://www.recreation.gov/permits/{permit}/registration/"
                                      f"detailed-availability?date={d.isoformat()}")
            time.sleep(0.4)                                   # be gentle with their server

    prev_open, last = state.get("open", {}), state.setdefault("last_alert", {})
    remind, now = watch.get("remind_every_minutes"), time.time()
    new = {k: n for k, n in open_now.items()
           if k not in prev_open or (remind and now - last.get(k, 0) >= remind * 60)}
    state["open"] = open_now
    for k in new:
        last[k] = now
    for k in [k for k in last if k not in open_now]:          # closed again -> forget, so a reopening alerts
        del last[k]
    if not new:
        return None
    lines = "\n".join(f"{k}: {n} permits\n  {links[k]}" for k, n in sorted(new.items())[:6])
    return f"{watch['name']}: now available!\n{lines}"


CHECKS = {"tmb": check_tmb, "page_text": check_page_text, "page_changed": check_page_changed,
          "browser_count": check_browser_count,
          "lacblanc": check_lacblanc, "bonatti_form": check_bonatti_form,
          "recgov": check_recgov}


# ------------------------------------------------------------ notifications
PLACEHOLDERS = {"you@example.com", "friend@example.com", "+12025550100", "+12025550101"}


def load_config():
    cfg = json.loads(CONFIG_PATH.read_text())
    # --- Recipients in the cloud come from GitHub secrets, never from the (public) config file.
    # Easiest: plain comma-separated secrets EMAILS, SMS_GATEWAYS, NTFY_TOPICS.  (No braces or quotes.)
    def split(s):
        return [x for x in re.split(r"[,\s;]+", s or "") if x]
    simple = {"emails": split(os.environ.get("EMAILS")), "phones": split(os.environ.get("PHONES")),
              "sms_gateways": split(os.environ.get("SMS_GATEWAYS")), "ntfy": split(os.environ.get("NTFY_TOPICS"))}
    raw = os.environ.get("RECIPIENTS_JSON", "").strip()
    if any(simple.values()):
        cfg["recipients"] = {k: v for k, v in simple.items() if v}
    elif raw:                                         # older all-in-one JSON secret (still supported)
        try:
            rec = json.loads(raw, strict=False)       # strict=False tolerates stray line breaks / tabs
        except ValueError as e:
            sys.exit("The RECIPIENTS_JSON secret is not valid JSON (" + str(e) + ").\n"
                     "Easier fix: delete it and create a secret named EMAILS containing just your addresses "
                     "separated by commas, e.g.  you@gmail.com, friend@gmail.com")
        if isinstance(rec, dict) and "recipients" in rec:  # tolerate pasting the whole config block
            rec = rec["recipients"]
        keys = ("emails", "phones", "sms_gateways", "ntfy")
        if not isinstance(rec, dict) or not any(k in rec for k in keys):
            sys.exit("The RECIPIENTS_JSON secret needs at least one of: emails, phones, sms_gateways, ntfy.")
        cfg["recipients"] = {k: ([re.sub(r"\s+", "", str(x)) for x in v] if isinstance(v, list) else v)
                             for k, v in rec.items()}
    if any(simple.values()) or raw:                   # counts only -- never print the addresses themselves
        r = cfg["recipients"]
        print(f"Recipients loaded: {len(r.get('emails', []))} email(s), {len(r.get('sms_gateways', []))} text address(es), "
              f"{len(r.get('ntfy', []))} ntfy topic(s)")
    if cfg.get("timezone"):                           # cloud servers run in UTC; use YOUR clock for the 9am check
        os.environ["TZ"] = cfg["timezone"]
        time.tzset()
    return cfg


def send_email(subject, body, cfg, to=None):
    to = to if to is not None else [e for e in cfg["recipients"].get("emails", []) if e not in PLACEHOLDERS]
    if not to:
        return
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, os.environ["SMTP_USER"], ", ".join(to)
    msg.set_content(body)
    with smtplib.SMTP_SSL(cfg.get("smtp_host", "smtp.gmail.com"), cfg.get("smtp_port", 465)) as s:
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.send_message(msg)


def send_sms(body, cfg):
    rec = cfg["recipients"]
    method = cfg.get("sms_method", "imessage")
    for phone in [x for x in rec.get("phones", []) if x not in PLACEHOLDERS]:
        if method == "imessage" and sys.platform != "darwin":
            print("  phone numbers skipped: iMessage only works from a Mac (use sms_gateways or ntfy here)", file=sys.stderr)
            break
        try:
            if method == "twilio":
                sid = os.environ["TWILIO_SID"]
                requests.post(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                              auth=(sid, os.environ["TWILIO_TOKEN"]),
                              data={"From": os.environ["TWILIO_FROM"], "To": phone, "Body": body[:1500]},
                              timeout=30).raise_for_status()
            else:  # iMessage through the Messages app on this Mac
                safe = body.replace("\\", "\\\\").replace('"', '\\"')[:900]
                script = ('tell application "Messages"\n'
                          '  set svc to 1st account whose service type = iMessage\n'
                          f'  send "{safe}" to participant "{phone}" of svc\n'
                          'end tell')
                subprocess.run(["osascript", "-e", script], check=True, timeout=30)
        except Exception as e:  # one bad number shouldn't block the rest
            print(f"  SMS to number ending {str(phone)[-2:]} failed: {e}", file=sys.stderr)

    # Email-to-text addresses such as 5551234567@vtext.com -- sent as a short email, works from any computer
    gateways = [x for x in rec.get("sms_gateways", []) if x not in PLACEHOLDERS]
    if gateways:
        try:
            send_email("Booking alert", body[:300], cfg, to=gateways)
        except Exception as e:
            print(f"  text gateway failed: {e}", file=sys.stderr)

    # ntfy.sh push notifications: free; each person installs the ntfy app and subscribes to the topic name
    lines = body.split("\n")
    title = lines[0].encode("latin-1", "ignore").decode("latin-1")[:100]
    link = re.search(r"https?://\S+", body)
    for topic in rec.get("ntfy", []):
        if topic in PLACEHOLDERS:
            continue
        try:
            hdr = {"Title": title, "Priority": "high"}
            if link:
                hdr["Click"] = link.group(0)
            requests.post(f"https://ntfy.sh/{topic}", data="\n".join(lines[1:]).encode("utf-8"),
                          headers=hdr, timeout=20).raise_for_status()
        except Exception as e:
            print(f"  ntfy push failed: {e}", file=sys.stderr)


def notify(subject, body, cfg):
    print(f"ALERT: {subject}\n{body}")
    rec = cfg.get("recipients", {})
    everyone = rec.get("emails", []) + rec.get("phones", []) + rec.get("sms_gateways", []) + rec.get("ntfy", [])
    if not [x for x in everyone if x not in PLACEHOLDERS]:
        print("WARNING: no real recipients configured -- nobody was notified!", file=sys.stderr)
    for fn, args in ((send_email, (subject, body, cfg)), (send_sms, (f"{subject}\n{body}", cfg))):
        try:
            fn(*args)
        except Exception as e:
            print(f"  {fn.__name__} failed: {e}", file=sys.stderr)


# --------------------------------------------------------------------- main
def list_tmb_refuges(url="https://www.montourdumontblanc.com/fr/disponibilites"):
    """`python3 watch.py --list` prints refuge names as the site spells them (for the config)."""
    text = page_text(url)
    for m in re.finditer(r"alt\.\s*\d+\s*m\s*/\s*\d+\s*personnes max\.", text):
        before = " " + text[max(0, m.start() - 110): m.start()]
        name = re.split(r"\s(?:[–—-]|\d{1,3})\s", before)[-1].strip()
        print(re.sub(r"^(?:(?:[–—-]|\d{1,3})\s+)+", "", name))

def _ago(ts):
    if not ts:
        return "never"
    mins = round((time.time() - ts) / 60)
    return f"{mins} min ago" if mins < 90 else f"{round(mins / 60)} h ago"


def build_heartbeat(cfg, state):
    """Plain-text status report: which checks are healthy, which are failing, how many alerts went out."""
    now = dt.datetime.now()
    rows, problems = [], []
    for w in cfg["watches"]:
        if w.get("disabled"):
            continue
        st = state.get(w["name"], {})
        if st.get("_status") == "error":
            since = dt.datetime.fromtimestamp(st["_error_since"]).strftime("%b %d %H:%M") if st.get("_error_since") else "?"
            problems.append(f"\u2717 {w['name']} -- failing since {since}\n    {str(st.get('_error', ''))[:220]}")
            rows.append(f"\u2717 {w['name']}")
        elif st.get("_last_ok"):
            rows.append(f"\u2713 {w['name']}  (last checked {_ago(st['_last_ok'])})")
        else:
            rows.append(f"? {w['name']}  (not checked yet)")
    alerts = state.get("_alerts", [])
    last_day = sum(1 for t in alerts if time.time() - t < 86400)
    head = "Everything is running." if not problems else f"{len(problems)} check(s) are failing -- see below."
    lines = [f"Booking watcher daily check -- {now:%A %d %b %Y, %H:%M}", "", head, ""]
    if problems:
        lines += ["PROBLEMS", *problems, ""]
    lines += ["ALL WATCHES", *rows, "",
              f"Alerts sent: {last_day} in the last 24 hours, {len(alerts)} in the last 7 days.", "",
              "If this email stops arriving, the watcher isn't running (Mac asleep or off, or the schedule was removed)."]
    subject = "Booking watcher: running OK" if not problems else f"Booking watcher: {len(problems)} check(s) FAILING"
    return subject, "\n".join(lines)


def maybe_heartbeat(cfg, state, force=False):
    """Once a day (after heartbeat_hour, default 9am local), email ONLY the first listed address."""
    if cfg.get("heartbeat") is False and not force:
        return
    hb = state.setdefault("_heartbeat", {})
    now = dt.datetime.now()
    today = now.strftime("%Y-%m-%d")
    if not force and (hb.get("last_sent") == today or now.hour < cfg.get("heartbeat_hour", 9)):
        return
    to = [e for e in cfg["recipients"].get("emails", []) if e not in PLACEHOLDERS][:1]
    if not to:
        print("  daily check not sent: no real email address in watch_config.json", file=sys.stderr)
        return
    subject, body = build_heartbeat(cfg, state)
    try:
        send_email(subject, body, cfg, to=to)
    except Exception as e:
        print(f"  daily check email failed: {e}", file=sys.stderr)
        return
    if not force:
        hb["last_sent"] = today
    print("  daily check email sent to the first listed address")


def main(dry=False, force=False):
    cfg = load_config()
    state = {} if dry else (json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {})
    if dry:
        print('DRY RUN: windows and minimums ignored, nothing is sent or saved.')
    for w in cfg["watches"]:
        if w.get("disabled"):
            continue
        if dry:
            w = {k: v for k, v in w.items() if k not in ("date_from", "date_to", "min_spots", "every_minutes")}
        st = state.setdefault(w["name"], {})
        gap = w.get("every_minutes")          # optional: check this one less often than the schedule
        elapsed = time.time() - st.get("_last_run", 0)
        if gap and not force and elapsed < gap * 60 - 30:
            wait = max(1, round((gap * 60 - elapsed) / 60))
            print(f"[{dt.datetime.now():%Y-%m-%d %H:%M}] {w['name']}: SKIPPED -- checked {round(elapsed / 60)} min ago; "
                  f"this one only runs every {gap} min (next in ~{wait} min). Add --now to force.")
            continue
        st["_last_run"] = time.time()
        print(f"[{dt.datetime.now():%Y-%m-%d %H:%M}] {w['name']}")
        try:
            msg = CHECKS[w["type"]](w, st)
        except Exception as e:
            print(f"  check failed: {e}", file=sys.stderr)  # keep going; other watches still run
            if st.get("_status") != "error":
                st["_error_since"] = time.time()
            st["_status"], st["_error"] = "error", str(e)
            continue
        st["_status"], st["_last_ok"] = "ok", time.time()
        st.pop("_error", None)
        st.pop("_error_since", None)
        if msg and dry:
            print("  >>> WOULD ALERT:", msg.replace("\n", "\n      "))
        elif msg:
            notify(f"Booking alert: {w['name']}", msg, cfg)
            state.setdefault("_alerts", []).append(time.time())
    if not dry:
        state["_alerts"] = [t for t in state.get("_alerts", []) if time.time() - t < 7 * 86400]
        maybe_heartbeat(cfg, state)
        STATE_PATH.write_text(json.dumps(state, indent=2))


LABEL = "com.bookingwatch"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
SECRET_VARS = ("SMTP_USER", "SMTP_PASS", "TWILIO_SID", "TWILIO_TOKEN", "TWILIO_FROM")


def build_plist(minutes):
    return {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, str(HERE / "watch.py")],
        "WorkingDirectory": str(HERE),
        "StartInterval": int(minutes) * 60,
        "RunAtLoad": True,
        "EnvironmentVariables": {k: os.environ[k] for k in SECRET_VARS if k in os.environ},
        "StandardOutPath": str(HERE / "watch.log"),
        "StandardErrorPath": str(HERE / "watch.err"),
    }


def install_schedule(minutes=10):
    """Makes macOS run this script every N minutes in the background (survives restarts)."""
    if "SMTP_USER" not in os.environ:
        sys.exit("Set SMTP_USER and SMTP_PASS in this Terminal window first (see the setup steps).")
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(PLIST_PATH, "wb") as f:
        plistlib.dump(build_plist(minutes), f)
    PLIST_PATH.chmod(0o600)  # contains your email app password -- keep it private
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(PLIST_PATH)], capture_output=True)  # ok if not loaded yet
    subprocess.run(["launchctl", "bootstrap", domain, str(PLIST_PATH)], check=True)
    print(f"Scheduled: checking every {minutes} minutes. Log: {HERE / 'watch.log'}")


def uninstall_schedule():
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(PLIST_PATH)], capture_output=True)
    PLIST_PATH.unlink(missing_ok=True)
    print("Schedule removed.")


def send_test():
    cfg = load_config()
    notify("Booking alert TEST", "If you can read this, alerts work. Nothing is wrong.", cfg)


if __name__ == "__main__":
    if "--list" in sys.argv:
        list_tmb_refuges()
    elif "--test" in sys.argv:
        send_test()
    elif "--heartbeat" in sys.argv:
        maybe_heartbeat(load_config(),
                        json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}, force=True)
    elif "--dry-run" in sys.argv:
        main(dry=True)
    elif "--install-schedule" in sys.argv:
        rest = [a for a in sys.argv[sys.argv.index("--install-schedule") + 1:] if a.isdigit()]
        install_schedule(int(rest[0]) if rest else 10)
    elif "--uninstall-schedule" in sys.argv:
        uninstall_schedule()
    else:
        main(force="--now" in sys.argv)

# ---------------------------------------------------------------- scheduling
# Easiest on a Mac: launchd. Save as ~/Library/LaunchAgents/com.me.bookingwatch.plist
# (fix the paths), then:  launchctl load ~/Library/LaunchAgents/com.me.bookingwatch.plist
#
# <?xml version="1.0" encoding="UTF-8"?>
# <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
# <plist version="1.0"><dict>
#   <key>Label</key><string>com.me.bookingwatch</string>
#   <key>ProgramArguments</key><array>
#     <string>/usr/bin/python3</string><string>/Users/YOU/bookingwatch/watch.py</string>
#   </array>
#   <key>StartInterval</key><integer>600</integer>
#   <key>EnvironmentVariables</key><dict>
#     <key>SMTP_USER</key><string>you@gmail.com</string>
#     <key>SMTP_PASS</key><string>your-app-password</string>
#   </dict>
#   <key>StandardOutPath</key><string>/Users/YOU/bookingwatch/watch.log</string>
#   <key>StandardErrorPath</key><string>/Users/YOU/bookingwatch/watch.err</string>
# </dict></plist>
