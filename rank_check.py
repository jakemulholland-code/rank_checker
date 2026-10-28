#!/usr/bin/env python3
"""
Google rank spot checker: the engine behind the dashboard.

Most people won't run this file directly. Double-click "Rank Checker.command"
(Mac) or "Rank Checker.bat" (Windows) instead, which opens the dashboard.

Command-line use is still supported:
    python rank_check.py keywords.txt --domain firstresponsefinance.co.uk
    python rank_check.py --rebuild-dashboard

Opens a fresh, signed-out browser session per keyword, reads the organic
top 10, and resolves Google's /goto and /url redirect links to their real
destinations. Intended for occasional spot checks, not bulk tracking.
"""

import argparse
import html
import json
import random
import re
import sys
import threading
import time
from types import SimpleNamespace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse

try:
    from playwright.sync_api import Error as PWError
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright
except ImportError:  # allows --rebuild-dashboard without Playwright installed
    sync_playwright = None
    PWError = PWTimeout = Exception

HERE = Path(__file__).resolve().parent
TEMPLATE_PATH = HERE / "dashboard_template.html"
GOOGLE_HOST_RE = re.compile(r"(^|\.)google\.[a-z]{2,3}(\.[a-z]{2})?$")

# Google's markup changes often. If results stop being picked up, this is the
# part to adjust. It takes one listing per result block (so sitelinks under a
# result are not counted as extra positions) and skips carousels and
# People Also Ask. Ads live outside #rso, so they are never included.
EXTRACT_JS = r"""
() => {
  const root = document.querySelector('#rso') || document.querySelector('#search');
  if (!root) return { found: false, results: [] };

  let blocks = Array.from(root.querySelectorAll('.MjjYud'))
    .filter(b => !b.parentElement || !b.parentElement.closest('.MjjYud'));
  if (blocks.length === 0) blocks = Array.from(root.children);

  const skip = 'g-scrolling-carousel, g-section-with-header, [data-initq], .related-question-pair, #tads, #bottomads';
  const results = [];
  for (const block of blocks) {
    const h3 = Array.from(block.querySelectorAll('a h3')).find(h => !h.closest(skip));
    if (!h3) continue;
    const a = h3.closest('a');
    const href = a && a.getAttribute('href');
    if (!href || href.startsWith('#')) continue;
    const cite = block.querySelector('cite');
    results.push({
      title: h3.innerText.trim(),
      href: new URL(href, location.href).href,
      cite: cite ? cite.innerText.trim() : ''
    });
  }
  return { found: true, results };
}
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_google(url):
    host = (urlparse(url).hostname or "").lower()
    return bool(GOOGLE_HOST_RE.search(host))


def link_type(url):
    if not is_google(url):
        return "direct"
    path = urlparse(url).path
    if path.startswith("/goto"):
        return "goto"
    if path == "/url":
        return "url"
    return "google"


def domain_of(url):
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def normalise(url):
    p = urlparse(url)
    return f"{domain_of(url)}{p.path.rstrip('/')}?{p.query}".lower()


def _target_from_url_link(url):
    qs = parse_qs(urlparse(url).query)
    target = (qs.get("q") or qs.get("url") or [None])[0]
    return target if target and target.startswith("http") and not is_google(target) else None


def _unescape_js(raw):
    raw = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), raw)
    raw = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), raw)
    return raw.replace("\\/", "/")


def _find_redirect_in_body(body, base):
    patterns = [
        r"""http-equiv=["']?refresh["']?[^>]*?url=([^"'>]+)""",
        r"""location\.(?:replace|assign)\(\s*["']([^"']+)""",
        r"""location(?:\.href)?\s*=\s*["']([^"']+)""",
    ]
    for pat in patterns:
        m = re.search(pat, body, re.I)
        if m:
            return urljoin(base, _unescape_js(html.unescape(m.group(1))).strip())
    return None


# ---------------------------------------------------------------------------
# Redirect resolution
# ---------------------------------------------------------------------------

def _resolve_via_request(context, href, timeout_ms):
    """Ask Google where the link goes without loading the destination site."""
    url = href
    for _ in range(5):
        try:
            resp = context.request.get(url, max_redirects=0, timeout=timeout_ms)
        except PWError:
            return None
        loc = resp.headers.get("location")
        if loc:
            nxt = urljoin(url, loc)
        else:
            try:
                nxt = _find_redirect_in_body(resp.text(), url)
            except PWError:
                return None
            if not nxt:
                return None
        if not is_google(nxt):
            return nxt
        if link_type(nxt) == "url":
            target = _target_from_url_link(nxt)
            if target:
                return target
        url = nxt
    return None


def _resolve_via_page(context, href, timeout_ms):
    """Fallback: open the link in a tab and catch the first off-Google navigation.
    That navigation gets a blank page, so the destination site is never contacted."""
    captured = {}
    page = context.new_page()

    def handle(route):
        req = route.request
        if (req.is_navigation_request() and req.frame.parent_frame is None
                and not is_google(req.url)):
            captured.setdefault("url", req.url)
            # Answer with a blank page rather than aborting: aborted redirects can leave
            # the tab hanging, and this still never contacts the destination site.
            route.fulfill(status=200, content_type="text/html", body="")
        else:
            route.fallback()

    try:
        page.route("**/*", handle)
        try:
            page.goto(href, timeout=timeout_ms, wait_until="commit")
        except PWError:
            pass
        deadline = time.time() + timeout_ms / 1000
        while "url" not in captured and time.time() < deadline:
            if page.url.startswith("http") and not is_google(page.url):
                break
            page.wait_for_timeout(250)
        # Some redirects aren't routed; in that case read where the tab ended up.
        if "url" not in captured and page.url.startswith("http") and not is_google(page.url):
            captured["url"] = page.url
    finally:
        page.close()
    return captured.get("url")


def resolve_link(context, href, timeout_ms=15000):
    kind = link_type(href)
    if kind == "direct":
        return href, kind
    if kind == "url":
        target = _target_from_url_link(href)
        if target:
            return target, kind
    target = _resolve_via_request(context, href, timeout_ms)
    if not target:
        target = _resolve_via_page(context, href, timeout_ms)
    return target, kind


# ---------------------------------------------------------------------------
# Search page handling
# ---------------------------------------------------------------------------

def dismiss_consent(page):
    """Click 'Reject all' on Google's cookie consent if it appears."""
    try:
        btn = page.get_by_role("button", name=re.compile(r"^\s*Reject all\s*$", re.I))
        if btn.count():
            btn.first.click(timeout=5000)
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(800)
    except (PWTimeout, PWError):
        pass


def is_blocked(page):
    if "/sorry/" in page.url:
        return True
    try:
        return page.locator("#captcha-form, iframe[src*='recaptcha']").count() > 0
    except PWError:
        return False


def wait_for_human(page, notify, minutes=5):
    notify({"type": "captcha", "minutes": minutes,
            "message": "Google is asking for a CAPTCHA. Solve it in the Chrome window "
                       f"and the check will carry on (waiting up to {minutes} minutes)."})
    deadline = time.time() + minutes * 60
    while time.time() < deadline:
        page.wait_for_timeout(2000)
        if not is_blocked(page):
            notify({"type": "captcha_solved", "message": "CAPTCHA solved, carrying on."})
            return True
    return False


def build_search_url(args, keyword, start=0):
    url = (f"https://{args.google_domain}/search?q={quote_plus(keyword)}"
           f"&hl={args.hl}&gl={args.gl}&pws=0")
    return url + (f"&start={start}" if start else "")


def check_keyword(browser, keyword, args, notify=lambda e: None):
    record = {"keyword": keyword, "checked_at": now_iso(), "status": "ok",
              "error": None, "results": []}
    context = browser.new_context(locale=args.locale,
                                  viewport={"width": 1366, "height": 900})
    page = context.new_page()
    try:
        depth = int(getattr(args, "depth", 10) or 10)
        raw, start = [], 0
        while True:
            page.goto(build_search_url(args, keyword, start),
                      wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(random.randint(1000, 2000))
            dismiss_consent(page)

            if is_blocked(page):
                if args.headless or not wait_for_human(page, notify):
                    if raw:  # keep what page 1 gave us rather than losing it
                        break
                    record["status"] = "blocked"
                    record["error"] = "Google returned a CAPTCHA page, so no results were read."
                    return record

            try:
                page.wait_for_selector("#rso, #search", timeout=15000)
            except PWTimeout:
                if raw:  # a later page didn't load; use what we have
                    break
                record["status"] = "error"
                record["error"] = ("No results were found on the page. Google's layout may "
                                   "have changed, or the page didn't finish loading.")
                return record

            found = page.evaluate(EXTRACT_JS)["results"]
            raw.extend(found)
            start += 10
            if len(raw) >= depth or start >= depth + 10 or not found:
                break
            page.wait_for_timeout(random.randint(3000, 6000))  # pause before the next page

        seen = set()
        for item in raw:
            if len(record["results"]) >= depth:
                break
            target, kind = resolve_link(context, item["href"])
            final = target or item["href"]
            key = normalise(final)
            if key in seen:
                continue
            seen.add(key)
            record["results"].append({
                "position": len(record["results"]) + 1,
                "title": item["title"],
                "url": final,
                "domain": domain_of(final) if target or kind == "direct" else "",
                "link_type": kind,
                "resolved": bool(target) or kind == "direct",
            })
            if kind != "direct":
                page.wait_for_timeout(random.randint(300, 900))

        if not record["results"]:
            record["status"] = "error"
            record["error"] = ("The page loaded but no organic results matched the selectors. "
                               "Check EXTRACT_JS in rank_check.py.")
    except PWError as exc:
        record["status"] = "error"
        record["error"] = f"Browser error: {str(exc).splitlines()[0]}"
    finally:
        context.close()
    return record


# ---------------------------------------------------------------------------
# Storage and dashboard
# ---------------------------------------------------------------------------

def load_history(path):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            backup = path.with_suffix(".corrupt.json")
            path.rename(backup)
            print(f"history.json couldn't be read, so it was moved to {backup.name}.")
    return {"runs": []}


def save_history(path, history):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(history, indent=2), encoding="utf-8")
    for attempt in range(10):  # Windows can briefly lock the file while the dashboard reads it
        try:
            tmp.replace(path)
            return
        except PermissionError:
            time.sleep(0.1 * (attempt + 1))
    tmp.replace(path)


def write_dashboard(out_dir, history):
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    payload = json.dumps(history).replace("</", "<\\/")
    page = template.replace("/*__DATA__*/null", payload).replace("/*__LIVE__*/false", "false")
    target = out_dir / "dashboard.html"
    target.write_text(page, encoding="utf-8")
    return target


def load_keywords(path):
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    seen, keywords = set(), []
    for line in lines:
        kw = line.strip()
        if kw and not kw.startswith("#") and kw.lower() not in seen:
            seen.add(kw.lower())
            keywords.append(kw)
    return keywords


def print_event(event):
    print(event["message"])


def launch(p, headless, notify=print_event):
    try:
        return p.chromium.launch(channel="chrome", headless=headless)
    except PWError:
        notify({"type": "info",
                "message": "Chrome wasn't found, so the bundled Chromium browser is being used."})
        return p.chromium.launch(headless=headless)


# ---------------------------------------------------------------------------
# Running a batch (shared by the dashboard app and the command line)
# ---------------------------------------------------------------------------

DEFAULTS = dict(google_domain="www.google.co.uk", gl="uk", hl="en", locale="en-GB",
                min_delay=20.0, max_delay=40.0, headless=False, depth=10)


def make_options(**overrides):
    opts = dict(DEFAULTS)
    opts.update({k: v for k, v in overrides.items() if v is not None})
    return SimpleNamespace(**opts)


def clean_domain(domain):
    d = (domain or "").strip().lower()
    d = re.sub(r"^https?://", "", d)
    d = d.removeprefix("www.")
    return d.split("/")[0]


def run_checks(keywords, domain, opts, history_path, notify=print_event, stop_event=None,
               reference=None):
    """Check each keyword in turn, saving after every one.

    notify receives event dicts with a human-readable "message".
    Setting stop_event ends the run after the current keyword."""
    stop_event = stop_event or threading.Event()
    domain = clean_domain(domain)
    history = load_history(history_path)
    run = {
        "run_id": datetime.now().strftime("%Y%m%d-%H%M%S"),
        "started_at": now_iso(),
        "finished_at": None,
        "settings": {"google_domain": opts.google_domain, "gl": opts.gl,
                     "hl": opts.hl, "target_domain": domain, "depth": opts.depth},
        "keywords": [],
    }
    if reference:
        run["reference"] = reference  # e.g. Wincher positions to compare against
    history["runs"].append(run)
    save_history(history_path, history)
    total = len(keywords)
    notify({"type": "run_start", "run_id": run["run_id"], "total": total,
            "message": f"Checking {total} keyword{'s' if total != 1 else ''}."})

    with sync_playwright() as p:
        browser = launch(p, opts.headless, notify)
        try:
            for i, kw in enumerate(keywords, 1):
                if stop_event.is_set():
                    break
                notify({"type": "keyword_start", "index": i, "total": total, "keyword": kw,
                        "message": f"[{i}/{total}] {kw}"})
                rec = check_keyword(browser, kw, opts, notify)
                run["keywords"].append(rec)
                save_history(history_path, history)

                if rec["status"] == "ok":
                    hit = next((r["position"] for r in rec["results"]
                                if domain and (r["domain"] == domain or r["domain"].endswith("." + domain))), None)
                    msg = f"    {len(rec['results'])} results"
                    if domain:
                        msg += f", {domain} at #{hit}" if hit else f", {domain} not in the top {opts.depth}"
                else:
                    msg = f"    {rec['error']}"
                notify({"type": "keyword_done", "index": i, "total": total, "keyword": kw,
                        "status": rec["status"], "message": msg})

                if rec["status"] == "blocked":
                    notify({"type": "blocked", "message": "Stopped: Google is blocking searches. "
                                                          "Try again in an hour or so."})
                    break
                if i < total and not stop_event.is_set():
                    wait = random.uniform(opts.min_delay, opts.max_delay)
                    notify({"type": "waiting", "seconds": round(wait),
                            "message": f"    pausing {wait:.0f}s before the next search"})
                    stop_event.wait(wait)
        finally:
            run["finished_at"] = now_iso()
            save_history(history_path, history)
            try:
                browser.close()
            except PWError:
                pass

    stopped = stop_event.is_set() and len(run["keywords"]) < total
    notify({"type": "run_done", "stopped": stopped,
            "message": "Stopped early." if stopped else "All done."})
    return run


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def parse_args():
    ap = argparse.ArgumentParser(description="Spot-check Google top 10 rankings.")
    ap.add_argument("keywords", nargs="?", help="Text file with one keyword per line")
    ap.add_argument("--domain", default="", help="Domain to highlight, e.g. example.co.uk")
    ap.add_argument("--google-domain", default=DEFAULTS["google_domain"])
    ap.add_argument("--gl", default=DEFAULTS["gl"])
    ap.add_argument("--hl", default=DEFAULTS["hl"])
    ap.add_argument("--min-delay", type=float, default=DEFAULTS["min_delay"])
    ap.add_argument("--max-delay", type=float, default=DEFAULTS["max_delay"])
    ap.add_argument("--headless", action="store_true",
                    help="Hide the browser. CAPTCHAs then stop the run instead of pausing.")
    ap.add_argument("--out-dir", default=str(HERE / "results"))
    ap.add_argument("--rebuild-dashboard", action="store_true",
                    help="Regenerate a static dashboard.html from saved results and exit")
    return ap.parse_args()


def main():
    args = parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    history_path = out_dir / "history.json"

    if not args.rebuild_dashboard:
        if not args.keywords:
            sys.exit("Give a keywords file, e.g. python rank_check.py keywords.txt\n"
                     "(or double-click the Rank Checker launcher to use the dashboard)")
        if sync_playwright is None:
            sys.exit("Playwright isn't installed. Run: pip install playwright && playwright install chromium")
        keywords = load_keywords(args.keywords)
        if not keywords:
            sys.exit("The keywords file is empty.")
        opts = make_options(google_domain=args.google_domain, gl=args.gl, hl=args.hl,
                            min_delay=args.min_delay, max_delay=args.max_delay,
                            headless=args.headless)
        stop = threading.Event()
        try:
            run_checks(keywords, args.domain, opts, history_path,
                       notify=print_event, stop_event=stop)
        except KeyboardInterrupt:
            print("\nStopped early. Keywords checked so far are saved.")

    print(f"Dashboard written to {write_dashboard(out_dir, load_history(history_path))}")


if __name__ == "__main__":
    main()
