# Plan: Sending requests back and the Shoppie Agent on AgentCore Runtime

SIDENOTE: For consistency with the course, please read `labs/week3day4_lab2.ipynb`, which deploys the agent on AgentCore Runtime and points the app at it, without running it. Where the notebook and this document differ, follow this document. The pause in step 1 was in the course's build for self-improvement, before the security build took it out: `git show week2day5-end:agent/harness.py` has its `wait_for_sign_off` node, and `git show week2day5-end:agent/service.py` its `resume_claim` route. Lab 1's `agentcore dev` also serves on port 8080, so stop it before `npm run dev`. Include this sidenote in your plan too.

This plan adapts [`docs/RUNTIME.md`](file:///Users/joicy/projects/undercover/docs/RUNTIME.md) (plus the Render guide in `labs/week3day4_lab2free.ipynb`) for **Shoppie** and our **Render** deployment (`https://undercover-9gfu.onrender.com` backed by Render Postgres `shoppie-postgres` in the `undercover` schema), while keeping 100% compatibility with AWS Lambda and AgentCore Runtime.

---

## Adaptations for Shoppie & Render (versus the UnderCover brief)

Because Shoppie skips Week 2 Day 4 (`SWARM.md`) and Week 2 Day 5 (`IMPROVE.md`), and already operates without user sign-in:

1. **Pausing & Sending Back (`support_requests`, no auth/roles):**
   - In Shoppie, *every* support request worked by the agent moves to `waiting_for_agent_review` and pauses at `approval` (`interrupt()`) for a support agent to confirm.
   - Because Shoppie has no sign-in (`docs/SPEC.md`), any support agent on the dashboard can either **Sign off** (recording `outcome`, `reviewed_by`, and optional `notes`) OR **Send back** with a required name (`sent_back_by`) and note (1–1,000 characters) on any `waiting_for_agent_review` request that has an agent recommendation and was not flagged by `"Security check"` (keeping prompt-injected requests away from the main agent model) and is not an evaluation test copy. Requests without a recommendation ("Left for support" after two failures) cannot be sent back (`409`).
2. **Batch Work (`runs` only, no evaluations/improver rounds):**
   - Shoppie's batch job is the capped queue run (`POST /api/runs`, `POST /api/runs/{run_id}/finish`, `PUT /api/runs/{run_id}/todos`). There are no evaluation UI/sets or improver rounds/proposals, so Step 4 implements the atomic backend lock, `"work_run"` AgentCore payload, failure undo, 1-minute heartbeats, 5-minute stale recovery (`"The agent stopped answering"`), and `stop_reason` display on `runs`.
   - The `approval` pause check and `remember` guard still check `copy_of` / `evaluation_id` so existing evaluation-copy test fixtures behave cleanly without pausing or saving to memory.
3. **Resuming (`resume_request` instead of `resume_claim`):**
   - Following Shoppie's existing naming (`calculate_refund`, `save_recommendation`, `complete_request`, `review_request`), the AgentCore entrypoint operation is `resume_request` (`{"operation": "resume_request", "request_id": "..."}`) and the new backend operations are `send_back_request`, `take_sent_back`, and heartbeats (`run_heartbeat`, `request_heartbeat`), while `/api/agent/openapi.json` stays locked to its exact 6 model/request operations (`get_request_context`, `calculate_refund`, `add_request_step`, `start_request`, `save_recommendation`, `complete_request`).
4. **Render & AWS Runtime Support:**
   - **Render (live):** Can run either as one combined web service (as today) or two separate Render web services per `labs/week3day4_lab2free.ipynb` (Backend + Dashboard service calling an Agent web service on `UNDERCOVER_AGENT` with `Authorization: Bearer <UNDERCOVER_API_KEY>`, with the context MCP server running alongside the agent).
   - **AWS AgentCore Runtime & Lambda:** `setup/setup_week3day4/runtime/` and `setup/setup_week3day4/agentcore/` move to the repository root in Phase 3 without altering the platform team's files. Crucially, `agent/` currently imports `backend.models`, `backend.instructions`, and `backend.refunds` — since `agentcore/cdk/lib/cdk-stack.ts` packages only `runtime/` + `agent/` (without `backend/`), Phase 3 removes those three `agent/` -> `backend/` imports so `runtime.main` runs in complete isolation under `--only-group runtime`.

---

## Today vs Target State

**Today:**
- `agent/main.py` saves to Customer Memory (`remember`) *before* `approval` (`decide -> remember -> approval -> complete`), and a Gateway-held payment refusal in `complete` proceeds to `END` rather than staying paused.
- `agent/service.py` runs as a custom FastAPI service on port `8002`, marks running runs failed during FastAPI lifespan startup, runs `resume_request` synchronously in an HTTP handler without refusing non-paused requests, and uses `prepare_threshold=0` on a single `psycopg` connection.
- `agent/main.py` runs `load_dotenv(override=True)` at import time, `agent/api.py` constructs `httpx.Client` at import time, and `agent/` imports from `backend/`.
- Support agents can only sign off a reviewed request; there is no `sent_back` status, no reviewer feedback loop to re-decide a paused thread, and no heartbeat/atomic row lock guarding `create_run`.

**After this build (4 Phases matching Steps 1–4 of `docs/RUNTIME.md`):**
1. **Phase 1 (Step 1): Request approval pause & `resume_request`.** Memory saving moves *after* `complete` (`approval -> complete -> remember -> END`), Gateway payment holds remain paused at `approval`, evaluation copies bypass `approval`, `review_request` uses an atomic `waiting_for_agent_review` guard and logs warnings instead of failing if the agent resume call fails, and `resume_request` runs asynchronously on a background thread while refusing non-paused requests.
2. **Phase 2 (Step 2): Sign off or send back.** Support agents can send back a recommended request with a required note (<= 1,000 chars); the backend atomically archives the sent-back decision, clears the current recommendation/calculation, records the step, sets `sent_back`, and asks the agent to resume. Only one caller (background resume or next queue run) can claim a `sent_back` request via `take_sent_back`. The request graph resets `model_calls` and forgets the previous decision before passing the note back to `agent`. Queue runs pick up unworked `sent_back` requests while skipping active resumes heard from within 5 minutes, and the dashboard displays all sent-back decisions, notes, and waiting-for-next-run state.
3. **Phase 3 (Step 3): The agent as an AgentCore app (`agent/entrypoint.py`).** `runtime/` and `agentcore/` are moved to repo root via `git mv`; `agent/` is decoupled from `backend/` and free of import-time I/O; `agent/entrypoint.py` serves `BedrockAgentCoreApp` on port `8080` (`/invocations` & `/ping`), handles lazy first-request readiness (random reseed, Identity secret fetch on Runtime, `UNDERCOVER_API_KEY` validation, and `ConnectionPool(..., prepare_threshold=None)`), checks `Authorization: Bearer <UNDERCOVER_API_KEY>` off-Runtime, tracks background work via `app.add_async_task` / `complete_async_task`, adds `runtime` + `server` (`boto3`) dependency groups and `check:runtime`, and teaches the backend to invoke either local/Render `UNDERCOVER_AGENT/invocations` or AWS `UNDERCOVER_AGENT_RUNTIME` with `boto3` (single attempt, exact failure vs timeout semantics).
4. **Phase 4 (Step 4) + Verification & Documentation: Backend-owned run lock, heartbeats, E2E, live checks & docs.** An atomic single-row database gate (`work_gate`) guarantees one active run at a time even under concurrent requests; `start_run` commits before calling the agent (`{"operation": "work_run", "run_id": ...}`), undoes the run if the agent refused or never received it, and leaves it running on timeouts; 1-minute heartbeats (`run_heartbeat`, `request_heartbeat`) keep active runs and resumes alive while 5-minute silence automatically recovers `"The agent stopped answering"` with `stop_reason` shown on the dashboard; tests (offline Python, Vitest, Playwright with an AgentCore `/invocations` + `/ping` stand-in), live database checks, and docs updates (`SPEC.md`, `TECH_STACK.md`, `PROCESS.md`, `CODE_STANDARDS.md`, `STATUS.md`) complete the milestone.

---

## Phase 1 (Step 1): Request approval pause and resume

Update the request graph, backend review handling, and resume contract so paused approvals/holds wait cleanly for sign-off and save to memory only once completed:

- **Request Graph Pause & Memory Ordering (`agent/main.py`):**
  - Place `approval` *before* `remember` (`decide -> [if eval copy: complete | else: approval]`; `approval -> [review: complete] -> [if held: approval | elif memory: remember -> END | else: END]`).
  - Ensure no events are saved to Customer Memory while a request is paused waiting for support review; events are saved only after `complete` finishes without being held.
  - When `run_complete_node` catches `GatewayRefused`, return `held=True` (after calling `hold_payment`) and loop back to `approval` so the thread stays interrupted until resolved.
  - Skip the `approval` pause for evaluation copies (`copy_of` / `evaluation_id` present on context/request) so their graph finishes immediately after `decide`/`complete`.
- **Atomic Sign-off & Resilient Agent Notification (`backend/store.py`, `backend/main.py`):**
  - In `Store.record_review`, perform an atomic conditional `UPDATE support_requests ... WHERE request_id = :id AND queue_status = 'waiting_for_agent_review'` (keeping exact-duplicate review idempotency only when already `decided` with the identical review, while ensuring concurrent review and send-back cannot both succeed).
  - In `backend/main.py` (`notify_agent_resume`), call the agent's resume operation (`POST /requests/{request_id}/resume` for now, moving to `/invocations` in Phase 3) synchronously inside `review_request` (or with a logged warning on failure/timeout — never failing the sign-off response) when the request had an agent recommendation, and return the updated request (`request_id`, `status`, `review`, etc.). Log `logger.warning(...)` if calling the agent fails or times out so any failure leaves sign-off intact without raising to the client.
- **Non-blocking `resume_request` that Refuses Unpaused Requests (`agent/main.py`, `agent/service.py`, `agent/tests/`):**
  - Check `graph.get_state(config)`: a thread is paused only when `state.next` / `state.interrupts` is non-empty. If not paused, refuse with an explicit reason (`409` on `agent/service.py` in Phase 1; `{"refused": ...}` in Phase 3).
  - On accepted `resume_request` (in Phase 1's FastAPI service ahead of Phase 3's AgentCore migration), run resume work in a background task and respond immediately (`accepted`) without waiting for an active queue run. Keep `work_request` in `agent/queue.py` finishing signed-off decided/in-progress threads if needed when the agent was unreachable.
  - Update `agent/tests/test_memory.py` and `agent/tests/test_agent.py` to test: paused approvals not saving to memory until resumed; Gateway-refused payment holding and staying paused at `approval`; evaluation copy skipping `approval`; resume saving to stand-in memory after sign-off; `resume_request` refusing non-paused requests; and backend `review_request` logging a warning and succeeding even when the agent is unreachable or times out.

### Success criteria

- [x] Unit & stub tests in `agent/tests/` verify: (a) approved/recommended requests pause at `approval` (`interrupt()`) with zero memory events written beforehand; (b) resuming after sign-off completes the request and writes two events to stand-in memory; (c) Gateway payment refusal calls `hold_payment` and keeps the graph paused at `approval`; (d) an evaluation copy (`copy_of` / `evaluation_id`) finishes without pausing; and (e) `resume_request` refuses a request whose thread is not paused.
- [x] Backend tests in `backend/tests/` verify `POST /api/requests/{request_id}/review` refuses non-`waiting_for_agent_review` statuses, warns in logs without failing the response when agent resume fails/times out, and succeeds straight away.
- [x] `npm run check:python` passes with >= 80% coverage.

---

## Phase 2 (Step 2): Sign off or send back

Implement the full send-back workflow across backend, agent graph, queue runner, and dashboard:

- **Database Schema & Atomic Operations (`backend/store.py`, `backend/main.py`):**
  - Add `sent_back_json` (JSON list defaulting to `[]`) and `resume_heartbeat_at` (Text, nullable) columns on `support_requests` in `backend/store.py` (with safe schema migration/column creation or reseed handling). Return `sent_back: [...]` and `resume_heartbeat_at` on `get_request` and `list_requests` summaries (`sent_back` count/list as needed).
  - Update `GET /api/requests/waiting` (`list_waiting`) to return both `"waiting"` and `"sent_back"` requests (preserving the exact `{request_id, type, created_at}` shape).
  - Add `SendBackInput` (`sent_back_by: str = Field(min_length=1)`, `note: str = Field(min_length=1, max_length=1000)`) — all required fields, no optional inputs, respecting `api_rules.py`.
  - Add `POST /api/requests/{request_id}/send_back` (`operation_id="send_back_request"`, `dependencies=[Depends(require_support_handler)]`):
    - Reject (`409`) if request is not `waiting_for_agent_review`, has no recommendation (`recommendation is None`), was flagged by `"Security check"`, or is an evaluation copy (`copy_of` / `evaluation_id`).
    - Atomically update via `WHERE request_id = :id AND queue_status = 'waiting_for_agent_review'` (stopping concurrent sign-off + send-back races): append `{decision, note, sent_back_by, sent_back_at}` to `sent_back_json`, clear `recommendation_json = None` and delete the attempt's `calculations` row, insert a `"sent_back"` step recording who sent it back and their note, and set `queue_status = "sent_back"`.
    - Call `notify_agent_resume(request_id)` synchronously, logging a warning if unreachable/failing/timed out, and return the refreshed request record (`status` `"in_progress"` if taken immediately by agent, or `"sent_back"` if waiting for next run).
  - Add agent-only bookkeeping endpoints (protected by `require_agent_key`, omitted from `/api/agent/openapi.json` so model operations stay at 6):
    - `POST /api/requests/{request_id}/take_sent_back` (`operation_id="take_sent_back"`, body `TakeSentBackInput` with `owner: Literal["resume", "run"]`): atomically updates `WHERE request_id = :id AND queue_status = 'sent_back'` to `queue_status = 'in_progress'` (setting `resume_heartbeat_at = now()` if `owner == "resume"`, else `None`). Returns `409` if `rowcount != 1` so only the first caller wins.
    - `POST /api/requests/{request_id}/heartbeat` (`operation_id="request_heartbeat"`): updates `resume_heartbeat_at = now()` while `queue_status == "in_progress"` (returns `409` otherwise).
- **Agent Graph Send-Back Loop & Heartbeats (`agent/main.py`, `agent/queue.py`, `agent/instructions.py`, `backend/instructions.py`):**
  - Track `model_calls: int` inside `RequestState` instead of summing every historical `AIMessage`, resetting `model_calls = 0` when `approval` resumes from a send-back so the agent gets a fresh 30-call allowance (`MAX_MODEL_CALLS`).
  - When `approval` receives a send-back payload (`{"action": "send_back", "note": ..., "sent_back_by": ...}`):
    - Return `recommendation: None`, `review: None`, `held: False`, `model_calls: 0`, and append a `HumanMessage` naming who sent the decision back, quoting the note, and stating in code (independent of instructions) to treat it as a senior reviewer's feedback, verify everything against Shoppie's policies and calculations, and refer the request with a reason if the wording does not support what the note asks.
    - Route `approval` conditionally back to `"agent"` when sent back, or forward to `"complete"` when signed off.
  - In `work_request` & `resume_request`:
    - When resuming a `"sent_back"` request via `resume_request`, atomically call `take_sent_back(owner="resume")` synchronously before accepting/spawning the background resume thread; while working, run a 60-second heartbeat helper calling `request_heartbeat` inside a `try ... finally` block; resume the paused thread (if `state.next` / `interrupts` exist) with `Command(resume=...)` using the latest `sent_back` note/sender, or recover cleanly if no paused checkpoint exists.
    - When a queue run picks up a `"sent_back"` request via `work_request`, call `take_sent_back(owner="run")` and resume its paused thread with the latest sent-back note and sender (or invoke fresh with the note if not paused).
    - In `resume_in_progress` (`agent/queue.py`), leave alone any `"in_progress"` request whose `resume_heartbeat_at` is within the last 5 minutes (`< 300s`); pick up only stale/un-heartbeated `in_progress` requests (resuming them with `Command(resume=...)` if still paused on a sent-back note!).
  - Review `backend/instructions.py` & `agent/instructions.py`: update only if any wording claims a recorded decision can never be revisited (leave the rest intact).
- **Dashboard Queue & Request Detail UI (`dashboard/src/`):**
  - Update [`dashboard/src/types.ts`](file:///Users/joicy/projects/undercover/dashboard/src/types.ts) with `QueueStatus` including `"sent_back"`, `SentBackRecord`, and `sent_back: SentBackRecord[]` on `RequestDetail`.
  - Update [`dashboard/src/components.tsx`](file:///Users/joicy/projects/undercover/dashboard/src/components.tsx), [`dashboard/src/ProgressCard.tsx`](file:///Users/joicy/projects/undercover/dashboard/src/ProgressCard.tsx), [`dashboard/src/App.tsx`](file:///Users/joicy/projects/undercover/dashboard/src/App.tsx), and [`dashboard/src/app.css`](file:///Users/joicy/projects/undercover/dashboard/src/app.css) following `docs/BRAND.md` plain English and component line limits (`< 500` lines/file):
    - Show `"Sent back"` status badge on queue rows and request header.
    - Show sent-back decisions panel beside/above the current recommendation, listing each previous decision, outcome/payout/calculation, rationale, who sent it back, when (`IST`), and their note.
    - Render `"sent_back"` steps in `ProgressCard` showing the reviewer and note.
    - When `detail.status === "waiting_for_agent_review"` and `detail.recommendation` exists (not Security check), `ActionPanel` shows both the existing Sign-off form (`Record support review`) AND a **Send back** form/panel with required reviewer name (`Sent back by`) and required note (`1–1,000` chars) posting to `/api/requests/{id}/send_back`.
    - When `detail.status === "sent_back"`, clearly display that the request was sent back and is waiting for the next run.

### Success criteria

- [ ] Backend tests in `backend/tests/` cover `send_back` refusals (`409` when not waiting for review, `left_for_support`, `Security check`, evaluation copy; `422` when note empty or > 1,000 chars or blank sender), atomic race protection between concurrent `review` and `send_back`, archiving multiple sent-back decisions, `/api/agent/openapi.json` remaining at 6 operations, `take_sent_back` single-winner atomicity, and `POST /api/requests/{id}/send_back` returning the updated request when agent resume fails/times out.
- [ ] Agent tests in `agent/tests/` verify: (a) a resumed send-back resets decision state and model call budget, passes the note as feedback, and reaches a new approval or refusal/referral decision; (b) a request can be sent back more than once; (c) concurrent `resume` + queue run takes a sent-back request only once; (d) a queue run skips an `in_progress` request whose `resume_heartbeat_at` is under 5 minutes old; and (e) a send-back missed by the agent stays `"sent_back"` and is worked by the next queue run (resuming its paused thread with the note).
- [ ] Vitest tests in `dashboard/src/` verify `"Sent back"` badge/status, waiting-for-next-run copy, displaying previous sent-back decisions + notes + reviewer/timestamp, `sent_back` timeline steps, and submitting the Send back form (validating required 1–1,000 char note).
- [ ] `npm run check:python` and `npm run check:ts` pass.

---

## Phase 3 (Step 3): The agent as an AgentCore app

Convert the agent service into a `BedrockAgentCoreApp`, decouple `agent/` from `backend/`, wire AgentCore Runtime vs HTTP calling from the backend, and enable isolated `--only-group runtime` checks:

- **Stage Platform Folders & Decouple `agent/` from `backend/`:**
  - Move `setup/setup_week3day4/runtime` -> `runtime/` and `setup/setup_week3day4/agentcore` -> `agentcore/` using `git mv` (leaving their contents unmodified as platform team files).
  - Remove all non-test `agent/` -> `backend/` imports (`agent/models.py`, `agent/instructions.py`, `agent/tools.py`) by keeping `Recommendation`, `Line`, `RefundReason`, and agent instructions defined locally inside `agent/` (with a boundary test verifying `agent/` and `backend/` never import each other outside tests, and a sync test verifying instructions match).
  - Eliminate all import-time I/O and connection creation across `agent/`:
    - Remove top-level `load_dotenv(override=True)` from `agent/main.py` (invoke dotenv loading only in `agent/__main__.py` or explicitly via `--env-file .env`).
    - Make `agent/api.py`'s `httpx.Client` lazy (preserving `agent_api.backend` monkeypatching for tests while opening nothing at import time and resolving `api_url()` dynamically).
- **Pooled Checkpointer with `prepare_threshold=None` (`agent/main.py`, `pyproject.toml`):**
  - Replace single `psycopg.connect(..., prepare_threshold=0)` in `get_checkpointer()` with `psycopg_pool.ConnectionPool` (`min_size=1, max_size=2`, `kwargs={"autocommit": True, "prepare_threshold": None, "row_factory": dict_row, "options": f"-c search_path={SCHEMA}"}`), opened lazily on first use (never at import time).
- **AgentCore Entrypoint (`agent/entrypoint.py`, retiring `agent/service.py`):**
  - Create `agent/entrypoint.py` exposing `app = BedrockAgentCoreApp()` with one `@app.entrypoint` function `invoke(payload, context)`:
    - **Lazy single-process readiness (`ensure_ready()`, locked):** Runs on the first request (including `"health"`) and retries on subsequent requests if initialization failed:
      - Detects Runtime via `AGENTCORE_CREDENTIAL_*_NAME` environment variables; when on Runtime, calls `random.seed()` and fetches each credential via `@requires_api_key(provider_name=...)` applied dynamically inside readiness, storing the value in `os.environ[<NAME>]`.
      - Validates `UNDERCOVER_API_KEY` is present (`RuntimeError` if missing).
      - Initializes the Postgres checkpoint pool (`get_checkpointer()`) when `DATABASE_URL` is configured (no auto-stopping of runs on startup — moved to backend in Phase 4).
    - **Authentication (`not on_runtime()`):** Every operation other than `"health"` verifies `Authorization: Bearer <UNDERCOVER_API_KEY>` from `context.request_headers` using `secrets.compare_digest`, raising Starlette `HTTPException(401, ...)` if missing or wrong. On Runtime, AWS IAM authorizes invocations so no Bearer key is checked or sent. `"health"` requires no key anywhere.
    - **Operations & Strict Payload Validation (no optional inputs; invalid/unknown -> Starlette `HTTPException(400, ...)`):**
      - `{"operation": "health"}` -> `{"healthy": True}` (only after `ensure_ready()`).
      - `{"operation": "work_run", "run_id": "<str>"}` (also accepting/adapting run start smoothly during Phase 3 transition before Phase 4 flips `create_run` ownership) -> validates run is running, registers `task_id = app.add_async_task(...)`, runs `work_run` on a `threading.Thread` with `try ... finally: app.complete_async_task(task_id)`, returns `{"accepted": True}` (or `{"refused": "<reason>"}`).
      - `{"operation": "resume_request", "request_id": "<str>"}` -> checks paused state / atomically calls `take_sent_back`, registers `app.add_async_task(...)`, runs resume on a `threading.Thread` calling `complete_async_task(task_id)` in `finally`, returns `{"accepted": True}` or `{"refused": "<reason>"}`.
    - `/ping` automatically reflects `HealthyBusy` while async tasks are active and `Healthy` once finished/failed.
  - Delete obsolete `agent/service.py` once tests and callers point to `agent/entrypoint.py`.
- **Dual-Route Backend Invoker (`backend/agent_service.py` or `backend/main.py`, `backend/requirements`):**
  - Read `UNDERCOVER_AGENT_RUNTIME` (Runtime ARN) vs `UNDERCOVER_AGENT` (default `http://127.0.0.1:8080`).
  - **Off-Runtime HTTP route:** Single attempt (`timeout=10.0`, no retries) `POST {UNDERCOVER_AGENT}/invocations` with `Authorization: Bearer <UNDERCOVER_API_KEY>`.
  - **Runtime boto3 route:** When `UNDERCOVER_AGENT_RUNTIME` is set, call `boto3.client("bedrock-agentcore", config=Config(retries={"total_max_attempts": 1}, connect_timeout=5, read_timeout=20)).invoke_agent_runtime(agentRuntimeArn=..., runtimeSessionId=f"shoppie-{uuid4()}", payload=json.dumps(payload))` (`len(session_id) >= 33`) and parse JSON stream from `response["response"]`.
  - Distinguish outcomes uniformly across both routes:
    - `{"accepted": True}` -> accepted.
    - Never connected (`httpx.ConnectError`, `botocore.exceptions.ConnectionError`), error status (`4xx`/`5xx`, `botocore.exceptions.ClientError`), or `{"refused": "..."}` -> agent did not take the work.
    - Cut-off answer or read timeout after sending (`httpx.TimeoutException`, `botocore.exceptions.HTTPClientError`, `IncompleteReadError`, truncated JSON) -> uncertain state (change nothing, tell handler to reload to check).
- **Dependencies & Tooling (`pyproject.toml`, `package.json`, test fixtures):**
  - Add `bedrock-agentcore` and `psycopg-pool` to dependencies and `[dependency-groups] runtime`; add `boto3` to `[dependency-groups] server`.
  - Add `"runtime/**/*.py"` to Ruff and Pyright includes in `pyproject.toml`.
  - Update `package.json`: `"dev:agent"` serves `uv run --env-file .env -m uvicorn agent.entrypoint:app --port 8080 --reload --reload-dir agent`; add `"check:runtime": "uv run --isolated --only-group runtime python -c \"import runtime.main\""` to `"check:python"`.
  - Ensure pytest (`conftest.py`) and `playwright.config.ts` clear `UNDERCOVER_AGENT_RUNTIME` so no test ever calls AWS.

### Success criteria

- [ ] `agent/` and `backend/` have zero non-test imports from one another (verified by boundary test).
- [ ] Tests via Starlette `TestClient(agent.entrypoint.app)` with stand-ins verify: importing `runtime.main` / `agent.entrypoint` triggers zero DB/Identity/network work; on Runtime the first request reseeds random and fetches each credential from Identity once (retrying if readiness failed); off-Runtime requests without Bearer key return `401` except `health`; unknown/malformed payloads return `400`; `work_run` and `resume_request` accept/refuse work, keep `/ping` at `HealthyBusy` while background work runs and back to `Healthy` when finished or errored.
- [ ] Backend unit tests verify both HTTP (`/invocations` + Bearer header) and AWS Runtime (`invoke_agent_runtime` with session id >= 33 chars, 1 retry attempt, connect=5 / read=20 timeouts) routes for accepted, refused (`409`), connection failure, HTTP/`ClientError` error, and read timeout/cut-off answer.
- [ ] Checkpoint pool tests verify `ConnectionPool` configuration with `prepare_threshold=None`.
- [ ] `npm run check:runtime` passes (`uv run --isolated --only-group runtime python -c "import runtime.main"`), `ruff`, `pyright`, and `pytest` cover `runtime/` cleanly.
- [ ] With `npm run dev` stopped, `uv run --env-file .env --isolated --only-group runtime python -m runtime.main` starts on port `8080` and `POST http://127.0.0.1:8080/invocations` with `{"operation": "health"}` answers `{"healthy": true}`.

---

## Phase 4 (Step 4): Single active run in the backend, heartbeats, E2E, live checks & docs

Move concurrency locking and stale-work recovery fully to the backend database, update E2E stand-ins, run full offline & live checks, and update documentation:

- **Atomic Backend Run Gate & Stop Reason (`backend/store.py`, `backend/main.py`):**
  - Add single-row lock table `work_gate` (`id = 1`, `active_run_id`) seeded during `Store.initialize()`/`reseed()`, plus `last_heartbeat_at` (`Text`) and `stop_reason` (`Text`) columns on `runs`.
  - Remove startup `runs` failure marking from agent readiness (`agent/entrypoint.py` no longer stops runs on startup). Keep startup recovery in `Store.initialize()` only when starting locally on laptop if needed? Wait: per Step 4 item 4 & 5, runs time out cleanly after 5 minutes without heartbeat rather than one process stopping another Runtime session's run! On multi-instance / Lambda + Runtime, let the 5-minute heartbeat check govern stale runs rather than blindly killing `running` runs inside `Store()` initialization or another process! (Verify existing `test_restart_keeps_requests_in_progress_and_closes_stopped_runs` is updated accordingly.)
  - Make `Store.create_run(cap)`:
    - First inspect any active running run: if `max(started_at, last_heartbeat_at)` is `>= 5 minutes` (300 seconds) old, mark that run `status = "failed"`, `stop_reason = "The agent stopped answering"`, `summary = "The agent stopped answering"`, `finished_at = now()`, and clear `work_gate`.
    - Execute atomic conditional `UPDATE work_gate SET active_run_id = :run_id WHERE id = 1 AND active_run_id IS NULL`. If `rowcount != 1`, return `(None, active_run_summary)` -> `POST /api/runs` returns `409` naming the active run.
    - Insert the committed `runs` row before returning so the transaction commits *before* the backend calls the agent, using only one connection.
  - Update `POST /api/runs` (`start_run`):
    - Call agent `{"operation": "work_run", "run_id": run_id}` synchronously after `create_run` commits.
    - If accepted: return `{"run_id": run_id, "cap": run.cap, "status": "running"}`.
    - If agent did not take the work (refused, unreachable, error response): mark the run stopped via `finish_run(run_id, "failed", summary=..., stop_reason=reason)` releasing `work_gate`, and return `409` (if refused) or `502`/`503` with the reason.
    - If uncertain (timeout / truncated stream): leave the run `running` and return `504` with `"The agent did not answer in time. Reload the page to see whether the run started."`.
  - Add `GET /api/runs/{run_id}` (`operation_id="get_run"`, `require_shared_read`) and `POST /api/runs/{run_id}/heartbeat` (`operation_id="run_heartbeat"`, `require_agent_key`) updating `runs.last_heartbeat_at = now()` while `status == "running"`.
  - In `agent/queue.py` (`work_run`): check `run["status"] == "running"`, send `POST /api/runs/{run_id}/heartbeat` every 60 seconds in a clean background daemon thread stopped in `finally`, and on uncaught failure call `finish_run(run_id, "failed", ...)` stopping only *that* `run_id`.
- **Dashboard `stop_reason` Display (`dashboard/src/`):**
  - Update `Run` type in [`dashboard/src/types.ts`](file:///Users/joicy/projects/undercover/dashboard/src/types.ts) with `stop_reason?: string | null`.
  - Render `stop_reason` clearly in [`dashboard/src/components.tsx`](file:///Users/joicy/projects/undercover/dashboard/src/components.tsx) (`RunPanel`) when a run stopped (`status === "failed"`).
  - Update [`dashboard/src/App.tsx`](file:///Users/joicy/projects/undercover/dashboard/src/App.tsx) if needed so timeout or `409` conflict messages show cleanly in the UI banner and state refreshes cleanly.
- **Playwright E2E Stand-in & Full Offline Suite (`e2e/`, `playwright.config.ts`):**
  - Add a Node/TypeScript AgentCore stand-in server `e2e/agent_stand_in.mjs` (compatible with Windows & Mac, listed in `playwright.config.ts` on port `8003` with `/ping` readiness) accepting `/invocations` (`health`, `work_run`, `resume_request`) and interacting back with the E2E backend (`http://127.0.0.1:8000`) using `X-API-Key` so runs finish/stop and sent-back/resumed requests complete cleanly against the live E2E backend.
  - Ensure `playwright.config.ts` blanks `DATABASE_URL`, `MODEL_NAME`, and `UNDERCOVER_AGENT_RUNTIME`.
  - Add E2E tests verifying concurrent/second run refusal (`409`), sending a request back with a note on the dashboard (`waiting_for_agent_review -> sent_back -> new recommendation`), displaying both the sent-back decision/note and current decision, and showing `stop_reason` on a stopped run.
- **Documentation & Verification:**
  - Verify `.env`-moved-aside full offline run: `npm run check` (Python format/lint/type/coverage, `check:server`, `check:context`, `check:runtime`, TypeScript Prettier/ESLint/tsc/Vitest coverage, Playwright E2E) and verify Linux Python 3.12 AWS Lambda zip size remains under 50 MB after adding `boto3` to `server`.
  - Live verification after one `npm run seed` with `npm run dev` (direct route, default model, Postgres container / Render Postgres schema `undercover`):
    - Capped run works waiting requests to recommendation; starting a second run concurrently is refused with `409`.
    - Support agent sends a held request back with a note on the dashboard -> agent resumes and re-decides -> dashboard verified via Playwright MCP (showing both sent-back decision + note and new decision, queue badges, and `stop_reason`); another request is signed off cleanly.
    - Verify `uv run --env-file .env --isolated --only-group runtime python -m runtime.main` health check on port `8080`.
  - Update [`docs/SPEC.md`](file:///Users/joicy/projects/undercover/docs/SPEC.md), [`docs/TECH_STACK.md`](file:///Users/joicy/projects/undercover/docs/TECH_STACK.md) (including Deployment for Render dual/single service, AWS Lambda, and AgentCore Runtime), [`docs/PROCESS.md`](file:///Users/joicy/projects/undercover/docs/PROCESS.md), [`docs/CODE_STANDARDS.md`](file:///Users/joicy/projects/undercover/docs/CODE_STANDARDS.md) (if needed), and [`docs/STATUS.md`](file:///Users/joicy/projects/undercover/docs/STATUS.md) (recording the live run results, known timeout/memory gap per Step 1 item 5, and Lambda zip size).

### Success criteria

- [ ] Backend concurrency & lifecycle tests verify: second start refused (`409` naming active run) while a run is running; two simultaneous `create_run` threads allow only one winner on both SQLite and SQL locking semantics; a run quiet for > 5 minutes is marked failed with `"The agent stopped answering"` (`stop_reason`) so a new run starts; a start the agent refuses/fails to connect to undoes the run with its reason; a read timeout leaves the run `"running"`; a failed run stops only its own `run_id`; and `resume_request` runs beside an active run.
- [ ] End-to-end Playwright suite passes using the AgentCore `/invocations` & `/ping` stand-in with the real backend creating runs and processing send-backs.
- [ ] `npm run check` passes completely with `.env` moved out of the way (zero live model, AWS, or Postgres calls), and Lambda zip build size with `boto3` stays under 50 MB.
- [ ] Live verification passes after one `npm run seed` on `npm run dev` (capped queue run, duplicate start refused, one request sent back with a note and re-decided via Playwright MCP, another signed off, and `runtime.main` standalone health check on port `8080`).
- [ ] `docs/SPEC.md`, `docs/TECH_STACK.md`, `docs/PROCESS.md`, `docs/CODE_STANDARDS.md`, and `docs/STATUS.md` are updated and accurate; `git diff --check` passes cleanly.

---

## Not in this plan

- Creating or deleting AWS resources (AgentCore Runtime, Identity providers, IAM roles, CloudFormation stacks) or editing files inside `runtime/` or `agentcore/`.
- Adding user login, Cognito/Supabase Auth, evaluation UI/sets, or the improver agent (which belong to skipped Week 2 builds and remain out of Shoppie's scope per `AGENTS.md`).
- Running Langfuse inside AgentCore Runtime or stopping a Runtime session from the dashboard.
