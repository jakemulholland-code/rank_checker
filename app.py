#!/usr/bin/env python3
"""
Rank Checker dashboard.

Starts a small web app on this computer only (http://127.0.0.1:8765) and
opens it in your browser. Enter keywords, click Run check, and watch the
results arrive. Close this window to shut it down.

You normally start this by double-clicking "Rank Checker.command" (Mac) or
"Rank Checker.bat" (Windows), which also handle first-time setup.
"""

import base64
import json
import re
import threading
import traceback
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import importer
import rank_check as rc

HOST, PORT = "127.0.0.1", 8765
URL = f"http://{HOST}:{PORT}/"
OUT_DIR = rc.HERE / "results"
HISTORY = OUT_DIR / "history.json"
SETTINGS = OUT_DIR / "settings.json"
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
STARTER_KEYWORDS = "bad credit car finance\ncar finance with bad credit\nbad credit car loans uk"


class Job:
    """The one batch that can run at a time, plus what the dashboard shows about it."""

    def __init__(self):
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.skip = threading.Event()
        self.state = self._idle()

    @staticmethod
    def _idle():
        return {"running": False, "run_id": None, "total": 0, "done": 0, "current": None,
                "phase": "idle", "wait_until": None, "log": [], "error": None}

    def update(self, event):
        with self.lock:
            st = self.state
            kind = event.get("type")
            if kind == "run_start":
                st.update(run_id=event["run_id"], total=event["total"])
            elif kind == "keyword_start":
                st.update(current=event["keyword"], phase="searching", wait_until=None)
            elif kind == "keyword_done":
                st.update(done=event["index"], phase="between")
            elif kind == "waiting":
                st.update(phase="waiting",
                          wait_until=datetime.now().timestamp() + event["seconds"])
            elif kind == "captcha":
                st.update(phase="captcha")
            elif kind == "captcha_solved":
                st.update(phase="searching")
            elif kind == "blocked":
                st.update(phase="blocked", error=event["message"])
            elif kind == "run_done":
                st.update(phase="stopped" if event.get("stopped") else "done",
                          current=None, wait_until=None)
            st["log"] = (st["log"] + [event.get("message", "").strip()])[-40:]
        print(event.get("message", ""))

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.state))


JOB = Job()


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def worker(keywords, domain, depth, reference, pause_min, pause_max, keep_pass):
    try:
        opts = rc.make_options(depth=depth, min_delay=pause_min, max_delay=pause_max,
                               keep_captcha_pass=keep_pass)
        rc.run_checks(keywords, domain, opts, HISTORY, notify=JOB.update,
                      stop_event=JOB.stop, reference=reference, skip_event=JOB.skip)
    except Exception as exc:  # surface anything unexpected in the dashboard
        traceback.print_exc()
        with JOB.lock:
            JOB.state.update(phase="error", error=f"The check failed: {exc}")
    finally:
        with JOB.lock:
            JOB.state["running"] = False


def clean_reference(ref, keywords):
    """Keep only what the comparison needs from the imported positions."""
    if not isinstance(ref, dict) or not isinstance(ref.get("items"), dict):
        return None
    wanted = {k.lower() for k in keywords}
    items = {}
    for key, item in ref["items"].items():
        if str(key).lower() in wanted and isinstance(item, dict):
            pos = item.get("position")
            items[str(key).lower()] = {
                "keyword": str(item.get("keyword", key))[:300],
                "position": pos if isinstance(pos, int) and pos > 0 else None,
                "raw": str(item.get("raw", ""))[:40],
                "url": str(item.get("url", ""))[:1000],
                "group": str(item.get("group", ""))[:200],
            }
    return {"source": str(ref.get("source", "Wincher"))[:40],
            "filename": str(ref.get("filename", ""))[:200],
            "imported_at": str(ref.get("imported_at", ""))[:40],
            "items": items}


def clean_pause(value, default):
    try:
        return min(max(float(value), 0.0), 600.0)
    except (TypeError, ValueError):
        return default


def start_job(raw_keywords, domain, depth=10, reference=None, pause_min=None, pause_max=None,
              keep_pass=True):
    keywords, seen = [], set()
    for line in raw_keywords.splitlines():
        kw = line.strip()
        if kw and kw.lower() not in seen:
            seen.add(kw.lower())
            keywords.append(kw)
    if not keywords:
        return 400, {"error": "Add at least one keyword, one per line."}
    depth = 20 if depth == 20 else 10
    pause_min = clean_pause(pause_min, rc.DEFAULTS["min_delay"])
    pause_max = max(clean_pause(pause_max, rc.DEFAULTS["max_delay"]), pause_min)
    keep_pass = keep_pass is not False
    reference = clean_reference(reference, keywords) if reference else None
    with JOB.lock:
        if JOB.state["running"]:
            return 409, {"error": "A check is already running."}
        JOB.stop.clear()
        JOB.skip.clear()
        JOB.state = Job._idle()
        JOB.state.update(running=True, total=len(keywords), phase="starting")
    settings = read_json(SETTINGS, {})
    settings["domain"] = rc.clean_domain(domain)
    settings["pause_min"], settings["pause_max"] = pause_min, pause_max
    settings["keep_captcha_pass"] = keep_pass
    if reference is None:  # typed keywords are remembered; imported ones aren't
        settings["keywords"] = "\n".join(keywords)
    SETTINGS.write_text(json.dumps(settings), encoding="utf-8")
    threading.Thread(target=worker, args=(keywords, domain, depth, reference, pause_min, pause_max, keep_pass),
                     daemon=True).start()
    return 200, {"ok": True}


def handle_import(body):
    data_b64 = str(body.get("data", ""))
    if not data_b64:
        return 400, {"error": "No file was received."}
    if len(data_b64) > MAX_UPLOAD_BYTES * 4 / 3:
        return 400, {"error": "That file is over 15 MB. Export just the keywords you need."}
    try:
        data = base64.b64decode(data_b64.split(",")[-1])
        return 200, importer.parse_upload(str(body.get("filename", "")), data)
    except importer.ImportError_ as exc:
        return 400, {"error": str(exc)}
    except Exception:
        traceback.print_exc()
        return 400, {"error": "That file couldn't be read. Export it from Wincher as CSV or "
                              "Excel (.xlsx) and try again."}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the console readable
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            html = rc.TEMPLATE_PATH.read_text(encoding="utf-8")
            html = html.replace("/*__LIVE__*/false", "true")
            self._send(200, html.encode(), "text/html; charset=utf-8")
        elif path == "/api/state":
            settings = {"keywords": STARTER_KEYWORDS, "domain": "",
                        "pause_min": rc.DEFAULTS["min_delay"], "pause_max": rc.DEFAULTS["max_delay"],
                        "keep_captcha_pass": rc.DEFAULTS["keep_captcha_pass"],
                        **read_json(SETTINGS, {})}
            self._send(200, {"history": read_json(HISTORY, {"runs": []}),
                             "job": JOB.snapshot(), "settings": settings,
                             "now": datetime.now().timestamp()})
        elif path == "/api/export-dashboard":
            query = parse_qs(self.path.partition("?")[2])
            run_id = query.get("run", [None])[0]
            history = read_json(HISTORY, {"runs": []})
            _, domain, _ = rc.client_history(history, run_id)
            slug = re.sub(r"[^a-z0-9]+", "-", domain.lower()).strip("-") or "client"
            stamp = f"{datetime.now():%Y-%m-%d}"
            if query.get("format", [""])[0] == "zip":
                data, _ = rc.client_site_zip(history, run_id)
                name, ctype = f"{slug}-rank-check-site-{stamp}.zip", "application/zip"
            else:
                data = rc.render_dashboard(history, run_id, client=True).encode()
                name, ctype = f"{slug}-rank-check-{stamp}.html", "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self._send(404, {"error": "Not found"})

    def do_POST(self):
        # Only accept requests from the dashboard itself, not other websites.
        origin = self.headers.get("Origin")
        if origin and origin.rstrip("/") != URL.rstrip("/"):
            return self._send(403, {"error": "Forbidden"})
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self._send(415, {"error": "Expected JSON"})
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > MAX_UPLOAD_BYTES * 1.4:
                return self._send(413, {"error": "That file is over 15 MB."})
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._send(400, {"error": "Bad request"})

        path = self.path.split("?")[0]
        if path == "/api/run":
            code, payload = start_job(str(body.get("keywords", "")), str(body.get("domain", "")),
                                      body.get("depth", 10), body.get("reference"),
                                      body.get("pause_min"), body.get("pause_max"),
                                      body.get("keep_captcha_pass", True))
            self._send(code, payload)
        elif path == "/api/import":
            self._send(*handle_import(body))
        elif path == "/api/stop":
            JOB.stop.set()
            self._send(200, {"ok": True})
        elif path == "/api/skip":
            JOB.skip.set()
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "Not found"})


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        print(f"Rank Checker already seems to be running. Opening {URL}")
        webbrowser.open(URL)
        return
    print(f"Rank Checker is running at {URL}")
    print("Leave this window open while you use it. Close it to shut Rank Checker down.\n")
    threading.Timer(0.8, webbrowser.open, [URL]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        JOB.stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
