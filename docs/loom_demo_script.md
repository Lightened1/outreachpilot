# Demo video script (about 6 minutes)

Before recording, start everything and watch the browser work:

```bash
BROWSER_HEADLESS=false BROWSER_SLOW_MO_MS=300 python start.py      # macOS / Linux
```
```powershell
$env:BROWSER_HEADLESS="false"; $env:BROWSER_SLOW_MO_MS="300"; python start.py   # Windows PowerShell
```

Have these open: the console (`http://127.0.0.1:8000`), the Swagger UI (`/docs`), the portal (`http://127.0.0.1:8010`, login `demo` / `demo-password`), a terminal and the code editor. `start.py` loads the demo prospects on first run, so the list is never empty.

| # | Time | Show | Say |
|---|---|---|---|
| 1 | 0:00 | README header + Mermaid diagram | "OutreachPilot researches prospects in a browser, drafts grounded messages with an LLM, and won't send anything until a human approves. It runs against a local demo site, so no real platform is touched." |
| 2 | 0:30 | Portal: people list, one profile, `/robots.txt` | "This is the test site: login, search, profiles, a message form, plus deliberately slow, flaky, blocked and robots-disallowed profiles." |
| 3 | 1:00 | Terminal: `python -m demo.browser_demo --headed --slow-mo 400` | Point out the login, search, structured extraction and screenshot, then the failure cases: **503 retried with backoff**, **timeout**, **429 challenge → stops, not retried**, **robots.txt respected**, **non-allow-listed host refused**, and the **idempotent send**. |
| 4 | 2:15 | Console: import the CSV again | The per-row report: every row is now a **duplicate**, and an invalid row explains why. |
| 5 | 2:45 | Console: open Priya → **Research profile** | The task panel shows live attempts while the browser works; the facts appear. |
| 6 | 3:15 | Swagger: `POST /prospects/{id}/execute` **before** approval | **409 invalid_transition**: "Sending without approval is impossible." |
| 7 | 3:30 | Console: **Write draft** (friendly) | The draft sits beside the facts. Hover a highlighted phrase to light up its fact: "every claim is traceable." |
| 8 | 4:00 | Terminal: `python -m demo.pipeline_demo --show-hallucination-guard` | The model invents "73%" and "Gartner"; the validator rejects it and the rewrite is clean. |
| 9 | 4:30 | Console: **Reject** with a reason → **Rewrite** (concise) → tick *Show changes from version 1* → **Approve** | Versions with a word diff; reviewer and reason recorded. |
| 10 | 5:00 | Console: **Send message** → confirm | Watch the browser fill and submit the form; the confirmation ID appears. Show the portal's `/api/outbox`. |
| 11 | 5:30 | Console: the Activity timeline | The full audit trail, from import to send. |
| 12 | 5:45 | Console: research Hana Sato | **Blocked** after 1 attempt, with the reason: "It stops and reports instead of trying to get around it." |
| 13 | 6:00 | Terminal: `pytest -q` | Close on the test count and `ruff check`. |

Shortcut for a single take: `python -m demo.api_walkthrough --reject-first --yes` runs steps 4–11 against the running API.
