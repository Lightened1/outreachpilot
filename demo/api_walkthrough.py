"""Drive the full workflow through the REST API (standard library only).

Needs the API (port 8000) and the demo portal (port 8010) running; see README.

    python -m demo.api_walkthrough                      # prompts you to approve
    python -m demo.api_walkthrough --reject-first --yes  # reject, regenerate, then approve
    docker compose exec api python -m demo.api_walkthrough --yes
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any


class Api:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"null")
        except urllib.error.URLError as exc:
            raise SystemExit(f"Cannot reach the API at {self.base} ({exc.reason}). Is it running?") from None

    def ok(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        status, payload = self.call(method, path, body)
        print(f"{method} {path} -> {status}")
        if status >= 400:
            print(json.dumps(payload, indent=2))
            sys.exit(1)
        return payload

    def wait(self, task_id: str, timeout: float = 90) -> dict[str, Any]:
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            _, task = self.call("GET", f"/tasks/{task_id}")
            if task["status"] != last:
                print(f"   task {task_id[:8]}: {task['status']} (attempt {task['attempts']}/{task['max_attempts']})")
                last = task["status"]
            if task["status"] in ("succeeded", "failed", "cancelled"):
                return task
            time.sleep(0.5)
        raise SystemExit(f"task {task_id} did not finish in {timeout}s")


def section(title: str) -> None:
    print(f"\n== {title} " + "=" * max(0, 64 - len(title)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default=os.getenv("OUTREACHPILOT_API", "http://127.0.0.1:8000"))
    parser.add_argument(
        "--portal",
        default=os.getenv("DEMO_PORTAL_URL", "http://127.0.0.1:8010"),
        help="portal URL as seen by the API process",
    )
    parser.add_argument("--person", type=int, default=1, help="demo portal person id (1-7)")
    parser.add_argument("--reject-first", action="store_true", help="reject the first draft, then regenerate")
    parser.add_argument("--yes", action="store_true", help="approve without prompting")
    args = parser.parse_args()
    api = Api(args.api)

    section("Health")
    print(json.dumps(api.ok("GET", "/health"), indent=2))

    section("1. Import + validate")
    names = {
        1: "Priya Raman",
        2: "Marcus Oyelaran",
        3: "Elena Varga",
        4: "Tomás Ferreira",
        5: "Hana Sato",
        6: "Daniel Kim",
        7: "Sofia Lindqvist",
    }
    payload = {
        "name": names.get(args.person, "Priya Raman"),
        "source_url": f"{args.portal}/{'private/people' if args.person == 7 else 'people'}/{args.person}",
        "notes": "Internal note: met at a trade fair. (Never sent to the model.)",
    }
    status, prospect = api.call("POST", "/prospects", payload)
    if status == 409:  # already imported in an earlier run: reuse it
        prospect = {"id": prospect["error"]["details"]["existing_id"]}
        prospect = api.ok("GET", f"/prospects/{prospect['id']}")
        print(f"(prospect already exists, reusing id {prospect['id']} in status {prospect['status']})")
    else:
        print(f"POST /prospects -> {status}")
    pid = prospect["id"]
    print(f"prospect {pid}: {prospect['name']} -> {prospect['status']}")

    if prospect["status"] in ("validated", "failed", "blocked"):
        section("2. Research (Playwright, background task)")
        task = api.ok("POST", f"/prospects/{pid}/research")
        task = api.wait(task["id"])
        prospect = api.ok("GET", f"/prospects/{pid}")
        if prospect["status"] != "researched":
            print(json.dumps(prospect["last_error"], indent=2))
            raise SystemExit(f"research ended in status {prospect['status']}")
        print(json.dumps({k: v for k, v in prospect["research_data"].items() if k != "about"}, indent=2))

    def generate(tone: str) -> dict[str, Any]:
        task = api.ok("POST", f"/prospects/{pid}/generate-message", {"tone": tone})
        api.wait(task["id"])
        p = api.ok("GET", f"/prospects/{pid}")
        if p["status"] != "pending_approval":
            print(json.dumps(p["last_error"], indent=2))
            raise SystemExit(f"generation ended in status {p['status']}")
        msg = p["latest_message"]
        print(
            f"\nSubject: {msg['subject']}\n\n{msg['body']}\n\nfacts_used={msg['facts_used']} "
            f"version={msg['version']} attempts={msg['generation_attempts']}"
        )
        return p

    if prospect["status"] in ("researched", "rejected", "approved", "pending_approval"):
        section("3. Generate (LLM, validated against researched facts)")
        prospect = generate("professional")

        if args.reject_first:
            section("4a. Human rejects the draft")
            prospect = api.ok(
                "POST",
                f"/prospects/{pid}/approve",
                {"decision": "reject", "reviewer": "ami", "reason": "Too formal; make it shorter."},
            )
            print(f"status -> {prospect['status']}")
            section("4b. Regenerate with a different tone")
            prospect = generate("concise")

        section("4. Human approval")
        answer = "y" if args.yes else input("Approve this message? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            prospect = api.ok(
                "POST",
                f"/prospects/{pid}/approve",
                {"decision": "reject", "reviewer": "ami", "reason": "Rejected in walkthrough."},
            )
            print(f"status -> {prospect['status']}; nothing was sent.")
            return
        prospect = api.ok("POST", f"/prospects/{pid}/approve", {"decision": "approve", "reviewer": "ami"})
        print(f"status -> {prospect['status']}")

    if prospect["status"] == "approved":
        section("5. Execute (Playwright fills and submits the compose form)")
        task = api.ok("POST", f"/prospects/{pid}/execute")
        api.wait(task["id"])
        prospect = api.ok("GET", f"/prospects/{pid}")
        print(f"status -> {prospect['status']}")
        print(json.dumps(prospect["latest_message"]["execution_result"], indent=2))

    section("6. Audit trail")
    for event in api.ok("GET", f"/prospects/{pid}/events"):
        print(f"{event['created_at'][:19]}  {event['event_type']:<22} {event['actor']}")


if __name__ == "__main__":
    main()
