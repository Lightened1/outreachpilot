"""Start OutreachPilot for a demo with one command: ``python start.py`` (or double-click start.bat).

Starts the practice website the browser automation visits (port 8010) and the API with its
console (port 8000), loads the seven demo prospects on first run, and opens the console in your
browser. Press Ctrl+C to stop everything.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEMO_CSV = ROOT / "docs" / "demo_prospects.csv"


def port_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def request(method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if os.getenv("API_KEY"):
        headers["X-API-Key"] = os.environ["API_KEY"]
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, {}


def wait_until_up(url: str, timeout: float = 60) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if request("GET", url)[0] == 200:
                return True
        except OSError:
            pass
        time.sleep(0.3)
    return False


def after_start(api: str, portal: str, seed: bool, open_browser: bool) -> None:
    if not wait_until_up(f"{api}/health"):
        print("\nThe app did not start within a minute. Look for an error message above.")
        return
    note = ""
    if seed and DEMO_CSV.is_file():
        status, stats = request("GET", f"{api}/stats")
        if status == 200 and stats.get("total") == 0:
            csv_text = DEMO_CSV.read_text(encoding="utf-8").replace("http://127.0.0.1:8010", portal)
            status, report = request("POST", f"{api}/prospects/import", {"csv": csv_text})
            if status == 200:
                note = f"Loaded {report['created']} demo prospects.\n  "
    print(
        "\n"
        "  OutreachPilot is running.\n\n"
        f"  {note}Open the app:      {api}\n"
        f"  Practice website:  {portal}  (the fictional site the robot browser signs in to;\n"
        "                     you don't need to open it)\n\n"
        "  Try it: open Priya Raman, then Research profile, Write draft, Approve, Send message.\n"
        "  Press Ctrl+C in this window to stop.\n",
        flush=True,
    )
    if open_browser:
        webbrowser.open(api)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8000, help="port for the app (default 8000)")
    parser.add_argument("--demo-port", type=int, default=8010, help="port for the practice website (default 8010)")
    parser.add_argument("--no-browser", action="store_true", help="don't open the browser")
    parser.add_argument("--no-seed", action="store_true", help="don't load the demo prospects on first run")
    args = parser.parse_args()

    for port, what in ((args.port, "the app"), (args.demo_port, "the practice website")):
        if not port_free(port):
            print(
                f"Port {port} (for {what}) is already in use. Close any other OutreachPilot window "
                "(for example one running `python -m demo` or uvicorn) and try again."
            )
            return 1

    portal = f"http://127.0.0.1:{args.demo_port}"
    api = f"http://127.0.0.1:{args.port}"
    os.environ["DEMO_PORTAL_URL"] = portal
    os.environ.setdefault("LOG_LEVEL", "WARNING")  # keep this window readable during a demo

    import uvicorn

    from demo.app import create_demo_app

    demo_server = uvicorn.Server(
        uvicorn.Config(create_demo_app(), host="127.0.0.1", port=args.demo_port, log_level="warning")
    )
    threading.Thread(target=demo_server.run, name="practice-website", daemon=True).start()
    threading.Thread(target=after_start, args=(api, portal, not args.no_seed, not args.no_browser), daemon=True).start()

    print("Starting OutreachPilot...", flush=True)
    try:
        # Main thread, no --reload: on Windows this keeps the event loop that can launch the browser.
        uvicorn.run("app.main:app", host="127.0.0.1", port=args.port, log_level="warning")
    finally:
        demo_server.should_exit = True
    return 0


if __name__ == "__main__":
    sys.exit(main())
