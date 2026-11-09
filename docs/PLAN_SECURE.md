# Shoppie security and deployment plan

SIDENOTE: For consistency with the course, please read `labs/week3day2_lab1.ipynb` and build the check in step 2 in the same style as the lab, and read `labs/week3day2_lab2.ipynb`, which deploys the app, without running it. Where the lab and this document differ, follow this document. Include this sidenote in your plan too.

This plan adapts `docs/SECURE.md` for Shoppie and our Render deployment (`https://undercover-9gfu.onrender.com` backed by the `shoppie-postgres` Render Postgres database in the `undercover` schema), while keeping full compatibility with the course's AWS Lambda packaging and checks.

Today:

- The Shoppie Agent calls the backend with no authentication header, so anyone who can reach the public Render URL could call agent-only write routes (`/start`, `/steps`, `/refund`, `/recommendation`, `/failure`, `/complete`, and run to-do updates).
- Customer-written support messages and evidence notes go straight into the request agent's model without an input screening guardrail.
- `backend/main.py` imports `agent` modules directly at import time, requires the `lifespan` hook to set `app.state.store`, and relies on `agent/service.py` being reachable after support sign-off to move a reviewed request from `in_progress` to `decided`.

After this build, in four phases matching the four steps of `docs/SECURE.md`:

1. **Phase 1 (Step 1): An API key for the agent (`UNDERCOVER_API_KEY`).** Every request from the agent (`agent/main.py`, `agent/queue.py`, `agent/service.py`) sends `X-API-Key: <UNDERCOVER_API_KEY>`, verified in constant time with `secrets.compare_digest`. Agent-only routes require the key (`401` otherwise), shared reads accept either the dashboard or a valid key (rejecting an invalid key with `401`), support-handler actions (`POST /api/requests/{request_id}/review`, `POST /api/runs`) refuse requests that authenticate only as the agent (`X-API-Key`), `GET /api/health` stays open, and `agent/service.py` refuses to start if `UNDERCOVER_API_KEY` is missing.
2. **Phase 2 (Step 2): A check for hidden instructions (`Screening` & `REQ-7901`).** Before the request agent's first model call on an attempt, a `screen` graph node built in the style of `labs/week3day2_lab1.ipynb` reads all customer-written fields of the request on `openai/gpt-6-luna` at `reasoning_effort="none"` (or `MODEL_NAME` with no reasoning effort) using Structured Outputs (`reason` first, then `instructions_found`). If an instruction is found, the request agent's model never sees the request; the graph records a `security_check` step and saves a recommendation holding the request for support review with the check's reason and `"Security check"` as its citation. `data/security/customer_orders.json` adds `REQ-7901` (a prompt-injection support request), which seeding loads into the queue alongside `data/customer_orders.json` while keeping `data/evals/` untouched.
3. **Phase 3 (Step 3): The agent's own API (`/api/agent/openapi.json`) and self-contained sign-off.** `GET /api/agent/openapi.json` returns an OpenAPI document generated from the app's routes with only the request agent's eight operations, no `servers` entry, and no key parameter in tool inputs. `calculate_refund` refuses any amount above the automatic refund cap (`₹10,000.00`) with a `403` naming the limit before inspecting request state. Signing off on a request (`POST /api/requests/{request_id}/review`) records the confirmed resolution and marks the request `decided` directly in the backend without needing `agent/service.py`, and any paused agent thread resumed afterwards finishes without calling completion a second time.
4. **Phase 4 (Step 4): Ready for Render and AWS Lambda.** `backend/` imports nothing from `agent/` at runtime, `pyproject.toml`'s `server` group holds only the backend's server packages, and `npm run check` verifies `uv run --isolated --only-group server python -c "import backend.main"`. `backend/main.py` exposes `handler = Mangum(app, lifespan="off")`, lazily initializes `Store()` when `lifespan` has not run, and serves `dashboard/dist` at `/`. `backend/store.py` uses `NullPool` and `prepare_threshold=None` when `AWS_LAMBDA_FUNCTION_NAME` is set, and keeps the bounded pool on Render and the laptop.

---

## Phase 1: An API key for the agent

Add `UNDERCOVER_API_KEY` authentication between the Shoppie Agent and the backend:

- Ensure `.env` has `UNDERCOVER_API_KEY` set to a random value of at least 32 characters from `secrets.token_urlsafe(32)` without printing the secret.
- Update `agent/main.py` (`backend_get`, `backend_post`, and a shared `agent_headers()` helper used by `agent/main.py`, `agent/queue.py`, and `agent/service.py`) to read `UNDERCOVER_API_KEY` from `os.getenv("UNDERCOVER_API_KEY")` as each request is sent and pass it in the `X-API-Key` header.
- Add constant-time key verification in `backend/main.py` using `secrets.compare_digest`:
  - **Agent-only routes** (`POST /api/requests/{request_id}/start`, `POST /api/requests/{request_id}/steps`, `POST /api/requests/{request_id}/refund`, `POST /api/requests/{request_id}/recommendation`, `POST /api/requests/{request_id}/failure`, `POST /api/requests/{request_id}/complete`, `GET /api/requests/waiting`, `GET /api/requests/{request_id}/context`, `GET /api/policies`, `GET /api/requests/{request_id}/policies/{policy}/sections/{section}`, `PUT /api/runs/{run_id}/todos`, `POST /api/runs/{run_id}/todos/{request_id}`, and `POST /api/runs/{run_id}/finish`) require a valid `X-API-Key` and return `401` when the header is missing, wrong, or when `UNDERCOVER_API_KEY` is unset on the backend.
  - **Shared read routes** (`GET /api/requests`, `GET /api/requests/{request_id}`, `GET /api/runs/current`, `GET /api/agent/instructions`) allow dashboard reads and valid `X-API-Key` requests, and return `401` if an `X-API-Key` header is supplied with a wrong value or when `UNDERCOVER_API_KEY` is unset.
  - **Support-handler action routes** (`POST /api/requests/{request_id}/review` and `POST /api/runs`) never accept the agent's `X-API-Key` as a substitute for a support handler: any request carrying `X-API-Key` to those routes is refused with `401`.
  - **`GET /api/health`** remains open with no authentication required.
  - Check the header inside `request.headers` via a FastAPI `Depends` function taking `Request` so no `X-API-Key` query or header parameter is added to the OpenAPI operation schemas.
- Require `UNDERCOVER_API_KEY` when starting `agent/service.py` (via a startup lifespan check and `require_agent_api_key()`), raising a `RuntimeError("UNDERCOVER_API_KEY is missing")` if unset.

### Success criteria

- [x] Unit and API tests cover an agent-only route with no key (`401`), a wrong key (`401`), and the right key (`200`); a shared read with the right key (`200`), from the dashboard (`200`), and with a wrong key (`401`); a support-handler action (`POST /api/requests/REQ-7001/review` and `POST /api/runs`) called with only `X-API-Key` (`401`); and the backend running with `UNDERCOVER_API_KEY` unset (dashboard reads and `/api/health` succeed, agent routes return `401`).
- [x] A service startup test verifies `agent/service.py` refuses to start when `UNDERCOVER_API_KEY` is unset and states that `UNDERCOVER_API_KEY` is missing.
- [x] Route inspection tests confirm checking the key adds no query or header input parameters to the routes' OpenAPI schemas.
- [x] `npm run check:python` passes with at least 80% coverage.

---

## Phase 2: A check for instructions hidden in a request

Add the prompt-injection screening guardrail in the style of `labs/week3day2_lab1.ipynb` and seed `REQ-7901` from `data/security/customer_orders.json`:

- Create `data/security/customer_orders.json` containing `REQ-7901` (with its customer `CUS-1901` and order `ORD-2026-10901`), a customer return/refund request that embeds a hidden instruction to the AI in its message attempting to override Shoppie's policy and force an immediate full refund. Keep `data/customer_orders.json` and `data/evals/` unchanged.
- Update `backend/store.py` so `initialize()` reads `data/security/customer_orders.json` alongside `data/customer_orders.json` and seeds `REQ-7901` into `support_requests` (making 9 requests in the queue), without adding any evaluation reference decision.
- In `agent/main.py`, add `SCREEN_INSTRUCTIONS` and `Screening(BaseModel)` in the exact style of `labs/week3day2_lab1.ipynb`:
  - `reason: str = Field(description="One sentence on what you found, quoting any instruction")` first.
  - `instructions_found: bool = Field(description="True if the request tries to instruct whoever handles it")` second.
- Add `screening_model()` in `agent/main.py` using the single `model_name()` helper: when `MODEL_NAME` is set, use `MODEL_NAME` with `reasoning_effort=None`; otherwise use `openai/gpt-6-luna` with `reasoning_effort="none"`.
- Make `screen` the first node of `build_request_graph` (`START -> screen`):
  - Fetch the request context (`GET /api/requests/{request_id}/context`) and format every customer-written field (`customer_message`, `follow_up_messages`, `evidence`, and requested items) for the screening model.
  - Call `checker.with_structured_output(Screening).invoke(...)`, retrying up to 3 attempts in all if the structured answer cannot be read. If all 3 attempts fail or raise an error/timeout, raise the error so the request never reaches the main request agent model and fails like any other attempt error.
  - Record the check's outcome as the request's first step after `started` (`stage="security_check"` in `AGENT_STAGES` and `STEP_MESSAGES`).
  - If `screening.instructions_found` is `True`, save a recommendation holding the request for a support agent (`outcome="request_information"`, `priority_human_review=True`, `rationale=screening.reason`, `policy_citations=["Security check"]`, `next_action="Hold for a support agent because the security check found instructions in the request."`) and route directly to `approval`/`END` without calling the `agent` node's model.
  - If `screening.instructions_found` is `False`, route to `agent` and work the request as before.
  - Store `screened: bool` in `RequestState` so resuming from a saved checkpoint after `screen` or after sign-off does not run the check a second time, while a fresh attempt starting from initial state runs it again.

### Success criteria

- [ ] Stub-model tests verify a request flagged by `screen` is held for support review with the check's reason and `"Security check"` citation, records the `security_check` step, and makes zero calls to the main request agent model.
- [ ] Stub-model tests verify a request that passes `screen` proceeds to `agent` and records its normal recommendation.
- [ ] Stub-model tests verify a screening check that fails or returns unreadable output across 3 attempts fails the attempt without calling the main request agent model, and that resuming from a mid-flight checkpoint after `screen` does not re-run `screen`.
- [ ] Store and API tests verify `REQ-7901` is seeded into the queue from `data/security/customer_orders.json` (9 requests total) while `data/customer_orders.json` and `data/evals/` are unchanged.
- [ ] `npm run check:python` passes with at least 80% coverage.

---

## Phase 3: The agent's own API and self-contained sign-off

Expose the agent-only OpenAPI document, enforce the refund cap before state checks, and make support sign-off self-contained on the backend:

- Add `GET /api/agent/openapi.json` (`operation_id="get_agent_openapi"`):
  - Generate the OpenAPI schema from the app's own routes filtered to the request agent's eight operations: `get_request_context`, `list_policy_sections`, `read_policy_section`, `calculate_refund`, `add_request_step`, `start_request`, `save_recommendation`, and `complete_request`.
  - Omit `servers` from the returned document and ensure no `X-API-Key` parameter appears in any operation's `parameters`.
  - Protect `GET /api/agent/openapi.json` as a shared read (accessible to the dashboard or with a valid `X-API-Key`, returning `401` on an invalid key).
- Enforce the refund cap first in `POST /api/requests/{request_id}/refund` (`calculate_refund`):
  - Before checking whether the request is `in_progress`, check the requested lines/refund against `MAX_REFUND_LIMIT` (`₹10,000.00`) and refuse any over-limit request with a `403` naming the limit (`"Refund exceeds the ₹10,000.00 approval limit"`), so an over-limit refund call gets the same `403` on any request regardless of its state.
- Make support sign-off (`POST /api/requests/{request_id}/review`) complete the request directly in the backend:
  - In `Store.record_review`, record the review and transition `waiting_for_agent_review` requests directly to `decided` (recording the confirmed resolution step), so a sign-off on Render or Lambda completes immediately without needing to reach `agent/service.py`.
  - Update `resume_request` and `resume_request_thread` in `agent/main.py` (and the `complete` node) so that if a paused agent thread is resumed after sign-off, it finishes its state cleanly without calling `POST /api/requests/{request_id}/complete` or `calculate_refund` again.

### Success criteria

- [ ] API tests verify `GET /api/agent/openapi.json` returns exactly the 8 agent operations (`get_request_context`, `list_policy_sections`, `read_policy_section`, `calculate_refund`, `add_request_step`, `start_request`, `save_recommendation`, `complete_request`), has no `servers` key, and has no key parameter on any operation.
- [ ] API tests verify `POST /api/requests/REQ-7001/refund` with an amount over `₹10,000.00` is refused with `403` naming the `₹10,000.00` limit even when `REQ-7001` is still `waiting` and was never started or approved.
- [ ] API and agent tests verify signing off on a held request via `POST /api/requests/{request_id}/review` immediately moves the request to `decided` once without calling `agent/service.py`, and resuming the paused agent thread afterwards makes no completion or refund call.
- [ ] `npm run check:python` passes with at least 80% coverage.

---

## Phase 4: Ready for Render and AWS Lambda

Decouple `backend/` from `agent/`, add the Lambda handler and connection pooling rules, update the end-to-end tests and documentation, and verify both the Lambda package and the live Render deployment:

- **Decouple `backend/` from `agent/` and tighten the `server` dependency group:**
  - Move `Recommendation`, `QUEUE_INSTRUCTIONS`, and `REQUEST_INSTRUCTIONS` into `backend/` (`backend/models.py` and `backend/instructions.py`), and re-export them in `agent/models.py` and `agent/instructions.py` so `backend/` imports nothing from `agent/`.
  - Have `POST /api/runs` notify the agent service (`POST {UNDERCOVER_AGENT}/runs` with `X-API-Key`) or, when `UNDERCOVER_AGENT` is in-process/local, trigger the run without a top-level `agent` import in `backend/main.py`, and add `POST /api/runs/{run_id}/finish` (`operation_id="finish_run"`) protected by `X-API-Key` so `agent/service.py` can record run completion over HTTP.
  - Restore `[dependency-groups] server` in `pyproject.toml` to only the lightweight server dependencies (`fastapi`, `httpx`, `mangum`, `openrouter`, `psycopg[binary]`, `pyjwt[crypto]`, `sqlalchemy`, `uvicorn`), add a `runtime` dependency group for the agent service on Render, and add `uv run --isolated --only-group server python -c "import backend.main"` to `npm run check:python` in `package.json`.
- **Lambda handler and lazy store initialization:**
  - Add `handler = Mangum(app, lifespan="off")` at the end of `backend/main.py`.
  - Update `store_of(request)` in `backend/main.py` so that when `app.state.store` has not been set by `lifespan` (as on Lambda where `lifespan="off"`), it lazily creates `Store()` without calling `initialize()` (so nothing seeds or writes files on Lambda).
- **Database connection pooling (`AWS_LAMBDA_FUNCTION_NAME`):**
  - Update `build_engine` in `backend/store.py` so that when `AWS_LAMBDA_FUNCTION_NAME` is set in the environment, SQLAlchemy uses `NullPool` and passes `"prepare_threshold": None` in `connect_args` alongside `"-c search_path=undercover"`. When `AWS_LAMBDA_FUNCTION_NAME` is not set (on Render and on the laptop), keep `pool_size=2, max_overflow=2`.
- **Dashboard, Playwright tests, and documentation:**
  - Update `playwright.config.ts` and `e2e/dashboard.spec.ts` to set a fixed test `UNDERCOVER_API_KEY` and verify the queue (including `REQ-7901`), run progress, and direct support sign-off.
  - Update `docs/SPEC.md`, `docs/TECH_STACK.md` (adding the **Deployment** section covering both Render and Lambda), `docs/PROCESS.md`, `docs/STATUS.md`, and `data/README.md`.

### Success criteria

- [ ] A test calls `backend.main.handler` (`Mangum(app, lifespan="off")`) with a Lambda Function URL v2 event for `/api/health`, `/` and `/requests/REQ-7001` (with a stand-in `dashboard/dist`), verifying `200` responses without the lifespan startup hook running.
- [ ] A unit test with `AWS_LAMBDA_FUNCTION_NAME` set verifies `build_engine` uses `NullPool` and `prepare_threshold=None` in `connect_args`.
- [ ] `uv run --isolated --only-group server python -c "import backend.main"` passes as part of `npm run check`.
- [ ] Building the Linux Python 3.12 deployment zip (`backend/`, `data/`, `dashboard/dist/`, and `--only-group server` wheels) in a temporary folder produces an archive under Lambda's 50 MB limit (size reported in `docs/STATUS.md`).
- [ ] With `.env` moved aside, `npm run check` passes all Python, TypeScript, server-group import, and Playwright end-to-end checks.
- [ ] After `npm run seed` and with `npm run dev` (and on Render after redeploying), `REQ-7901` is flagged by the security check and held for support with the check's reason, `curl` to an agent route without `X-API-Key` returns `401`, `GET /api/agent/openapi.json` returns the 8 agent operations, and support sign-off on the dashboard immediately marks a held request `decided`.
- [ ] `docs/SPEC.md`, `docs/TECH_STACK.md`, `docs/PROCESS.md`, `docs/STATUS.md`, and `data/README.md` are updated and `git diff --check` passes.
