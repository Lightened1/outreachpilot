# Architecture notes

## Layers

| Layer | Package | Responsibility |
|---|---|---|
| Console | `app/ui` | Static operator UI (HTML, CSS, vanilla JS) served at `/ui/`. It only talks to the public REST API. |
| HTTP | `app/api`, `app/main.py` | Routing, request validation, API-key check, request IDs, security headers, error → JSON mapping. No business rules. |
| Services | `app/services` | Use cases: prospect CRUD, workflow steps, the approval gate, task persistence. They own database transactions. |
| Domain core | `app/core` | Framework-free building blocks: status transitions, error taxonomy, `TaskManager`, retry, rate limiting, logging. Unit-tested without a database or browser. |
| Integrations | `app/automation`, `app/agents` | Playwright and the LLM. They return Pydantic models (`ResearchResult`, `ExecutionResult`, `GeneratedMessage`) and raise typed `AppError`s. |
| Persistence | `app/models`, `app/database.py` | SQLAlchemy 2.x typed models; async engine; SQLite WAL + foreign keys. |

`app/services/container.py` is the **composition root**. It builds the database, task manager, browser engine, LLM client and services once per app instance. FastAPI's lifespan creates the container and shuts it down: running tasks are cancelled and recorded, the browser is closed, and the engine is disposed. Tests build the same container with a temporary database and the mock LLM.

## Workflow and the approval gate

Statuses and allowed moves are one table (`app/core/workflow.py: TRANSITIONS`). Every status change goes through `ensure_transition`, which raises `InvalidTransitionError` (HTTP 409) with the allowed next states. `WorkflowService._begin` performs each step in this order:

1. Take a per-prospect `asyncio.Lock`, so two concurrent requests can't both start work.
2. Check the transition (409), then the step's preconditions (422: for example, research must exist before generation, and an approved message must exist before execution).
3. Move the prospect into the busy state (`researching`, `generating` or `executing`), write a `*.requested` audit event and commit.
4. Submit the job. If submission fails, the status is rolled back.

When the job finishes, the success or failure hook applies the result **only if the prospect is still in that busy state**. This means a late result can't overwrite a state that changed in the meantime, for example after a cancel.

Approval is synchronous (`POST /approve`). Generation creates a new message version and marks older pending versions `superseded`. Execution loads the single `approved` message and sends exactly that text, with the idempotency key `outreachpilot-message-{message_id}`. If a send is retried after the form was already submitted, the portal returns the original confirmation instead of posting twice.

## TaskManager semantics

`app/core/tasks.py` is a small asyncio job runner:

- **Concurrency:** a semaphore bounds running jobs. During the backoff between attempts a job **releases its slot**, so a retrying job doesn't starve others.
- **Timeouts:** each attempt is wrapped in `asyncio.wait_for(timeout_s)`. A timeout becomes a retryable `TimeoutError`.
- **Retries:** a `RetryPolicy` gives exponential backoff (`base * multiplier^(n-1)`, capped, with ±jitter). Only errors classified as retryable are retried (`is_retryable`):

  | Retryable | Not retryable |
  |---|---|
  | navigation timeouts, network errors, HTTP 5xx, LLM 429/5xx/timeouts | blocked access (403/429 challenge), robots.txt, host not allowed, 404, login failure, data mismatch, invalid LLM key, drafts that keep failing validation, programming errors |

- **Cancellation:** `cancel()` cancels the asyncio task. The runner records `cancelled`, and the failure hook moves the prospect to `failed` with `task_cancelled`.
- **Persistence:** every state change is saved through the `TaskStore` protocol (`SqlTaskStore` in the app, `InMemoryTaskStore` in unit tests). On startup, `recover_interrupted()` marks leftover `queued`/`running`/`retrying` tasks failed (`interrupted`), and moves their prospects to `failed`, so nothing stays stuck in a busy state.

### Moving to Redis / Celery (or arq, Dramatiq)

The task layer was kept narrow so it can be swapped:

- Jobs are closures over **IDs only** (the prospect ID). They load fresh state from the database inside the job, so they serialise naturally as `research(prospect_id)` and so on.
- Success and failure handling lives in `WorkflowService` hooks. In Celery, these become the task body's final step or `on_success`/`on_failure` handlers.
- `RetryPolicy` and `is_retryable` map directly to `autoretry_for`, `retry_backoff` and `max_retries`.
- `TaskStore` becomes either the Celery result backend or the existing `tasks` table, updated by the worker.
- Browser contexts are per job already. A worker process would own one `BrowserEngine`.

## Browser automation

`BrowserEngine` owns one Chromium instance and gives each job an isolated `BrowserContext` through `new_page()`, bounded by `BROWSER_MAX_PAGES`. Login state is saved with `storage_state` to `.sessions/` (git-ignored) and reused, so jobs don't log in again every time.

Every navigation goes through `goto()`:

```
host allow-list ─► robots.txt (cached per origin) ─► per-host token bucket ─► page.goto(timeout)
   ─► status: 403/429 → AccessBlockedError · 404 → PageNotFoundError · 5xx → NavigationError (retryable)
   ─► challenge-page detection (captcha / verification markers) → AccessBlockedError
```

`AccessBlockedError` is deliberately non-retryable, and the prospect moves to `blocked`. The project's position is to stop and report, never to work around a site's access controls.

Selectors are `data-testid` attributes, which are more stable than CSS classes or text. The portal client also checks that the page shows the expected person (`DataMismatchError`), so a stale URL can never cause a message to go to the wrong profile.

## LLM agent and the hallucination guard

`MessageAgent.generate()` works in four steps:

1. **Build facts.** The prospect's name, title and company are merged with the research result. Internal notes, screenshots and URLs are excluded.
2. **Prompt.** A system prompt with hard rules (use only the facts, no invented numbers, events or shared history, JSON output with `facts_used`) is combined with a tone guide, the sender profile and the facts as a JSON block.
3. **Validate.** `validate_draft` rejects the draft if it has any of the following:
   - placeholders;
   - numbers, URLs or capitalised names that appear nowhere in the facts, sender profile or user instructions;
   - known fabrication patterns ("as we discussed", "congrats on the promotion", "mutual connection");
   - references to posts or talks when there is no recent activity;
   - an over-length body;
   - a missing first-name greeting;
   - `facts_used` keys that don't exist.
4. **Rewrite.** The problems are sent back to the model as feedback, up to `MESSAGE_VALIDATION_ATTEMPTS`. After that, a non-retryable `MessageValidationError` puts the prospect in `failed` with the reasons, for a human to look at.

The validator is intentionally strict and cheap: no second model call. It is a guard, not a guarantee, which is why approval is mandatory.

`OpenAICompatibleClient` speaks plain `/chat/completions` over httpx, with JSON mode, a request rate limiter and error mapping:

- 401/403 → `LLMAuthError` (not retryable)
- 429/5xx/timeouts → `LLMUnavailableError` (retryable)
- anything else → `LLMError`

With no key, `LLM_MODE=auto` falls back to the deterministic `MockLLMClient` and logs a warning. The mock can also replay scripted replies, which is how the tests exercise the rewrite loop.

## Operator console

`app/ui` is plain HTML, CSS and JavaScript with no framework or build step. It is served by FastAPI's `StaticFiles` under a strict Content-Security-Policy (`script-src 'self'`, no inline code). Every value from the API is inserted as text through a small DOM builder, never as HTML.

- **Evidence highlighting.** The draft is matched against the same facts the agent was given: name, title and company, plus the research result. Every matching phrase gets a highlighter mark linked to its fact. Hovering or focusing a mark highlights the fact, and facts listed in `facts_used` carry a marker.
- **Review rules mirror the API.** Rejecting needs a reason and both decisions need a reviewer name. The buttons offered come from `allowed_next_statuses`, so the UI never offers an illegal transition.
- **Live work.** When a prospect is busy, the console polls `GET /tasks/{id}` once a second, shows the attempt and retry history, and offers cancel. When the task finishes, the prospect, list and counts are reloaded.
- **Versions.** `GET /prospects/{id}/messages` returns every draft. The console shows a word-level diff (LCS) against the previous version.
- **Auth.** On a 401, the console asks for the API key once and stores it in `localStorage` for that browser.

`tests/test_ui.py` drives the real files in Chromium under the production CSP, with an in-memory fake of the API that uses the real transition table.

## Security and privacy

- With `API_KEY` set (mandatory in production), every route except `/health` and the static console needs `X-API-Key`. The comparison uses `secrets.compare_digest`.
- Every response carries `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` and an `X-Request-ID`. The console's pages also carry a CSP. Swagger UI at `/docs` is left without a CSP because it loads assets from a CDN.

- Secrets are `SecretStr`. `/health` returns `public_summary()` only.
- `RedactingFilter` masks API keys (`sk-…`), bearer tokens, `password=`/`token=`/`api_key=` values and email addresses in every log record, including the `extra` fields.
- `.env`, `data/`, `artifacts/` and `.sessions/` are git-ignored and docker-ignored.
- Configuration is validated at startup. For example, `LLM_MODE=openai` without a key is refused, and the demo portal host must be on the allow-list.
- Prospect `notes` are never sent to the LLM.
