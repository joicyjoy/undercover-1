# Plan: Shoppie Copilot

> SIDENOTE: For consistency with the course, please build the Copilot the way the course has built its agents. Its agent loop is a LangGraph `StateGraph` written by hand: a node that calls the model, LangGraph's `ToolNode`, and the loop between them, with a `PostgresSaver` checkpointer, as in `labs/week2day3.ipynb` and the claim agent's graph. Please don't use LangChain's `create_agent` for it. The pause before a sign-off is an `interrupt()` in a node of its own, resumed with `Command(resume=...)`, since LangGraph runs an interrupted node again from the top when it resumes, as the claim agent's wait for a handler is a node of its own. The handler's sign-in reaches the graph and its tools in the graph's context (`context_schema`, read through `ToolRuntime`), never in the config, whose values LangGraph saves with each checkpoint. The Copilot is an AgentCore app of its own, made with `BedrockAgentCoreApp`, with one entrypoint and each turn's work registered as an async task, as the claim agent's AgentCore app does, and it is synchronous throughout, as the claim agent is. `labs/week3day4_lab1.ipynb` and `labs/undercoverlab/` show a pause in one Runtime session resumed in another. Its memory is `AgentCoreMemoryStore` as a LangGraph store, as the claim agent's memory is, with the Memory itself made as in `labs/week3day3.ipynb`. It reaches the context layer over MCP with `MultiServerMCPClient`, as in `labs/week2day2.ipynb`, and through the Gateway as in `labs/week3day2_lab2.ipynb` and `labs/week3day3.ipynb`. `labs/week3day4_lab2.ipynb` deploys the claim agent and points the app at it, and `labs/week3day5_lab1.ipynb` deploys the Copilot beside it, so please match the names that notebook uses. Read these notebooks without running them. Where a notebook and this document differ, follow this document. Include this sidenote in your plan too.

## Where Shoppie is today vs. what COPILOT adds

Today, Shoppie has the FastAPI backend (`backend/main.py`, `backend/store.py`, `backend/agent_service.py`, `backend/refunds.py`, `backend/records.py`), the context FastMCP server (`context/main.py` on `:8006`), the synchronous LangGraph request agent (`agent/main.py`, `agent/queue.py`, `agent/entrypoint.py`, `agent/memory.py`, `agent/gateway.py`) deployed both via `runtime/main.py` + `agentcore/agentcore.json` and via `azure/main.bicep` (`shoppie-context`, `shoppie-backend`, `shoppie-agent`), and the React dashboard (`dashboard/src/`). Shoppie is deployed on Azure Container Apps with Microsoft Entra ID Easy Auth, runs in INR (`₹`) and IST, and does not include the optional `SWARM.md` or `IMPROVE.md` builds.

Adding the Copilot introduces a conversational assistant sidebar across every page of the dashboard backed by a second AgentCore / Container App service (`agent/copilot_entrypoint.py`), a hand-written LangGraph `StateGraph` with its own `PostgresSaver` checkpointing, dedicated `AgentCoreMemoryStore` per handler, Gateway-scoped context access, live step progress, and human-in-the-loop sign-off confirmation via `interrupt()` / `Command(resume=...)`.

## Adaptations for Shoppie & Azure (vs. the brief)

1. **Identity & Permissions without Supabase Auth (`Entra ID` + Local Dev Mode):**
   - Instead of Supabase Auth (`VITE_SUPABASE_URL`), Azure Container Apps uses Microsoft Entra ID Easy Auth (`X-MS-CLIENT-PRINCIPAL-ID` / `X-MS-CLIENT-PRINCIPAL-NAME` headers forwarded to `shoppie-backend`), while local development and Playwright tests run with zero login friction.
   - Seed two support-handler profiles matching the brief (`HND-01` **Dana Whitfield**, `can_sign_off_over_limit=True`; `HND-02` **Marcus Oyelaran**, `can_sign_off_over_limit=False`). In Azure Easy Auth, `X-MS-CLIENT-PRINCIPAL-*` maps to the authenticated handler profile; locally (when Easy Auth headers are absent), default cleanly to `HND-01` Dana so existing E2E tests pass unchanged, and provide a lightweight dev handler switcher in the Copilot header (`X-Shoppie-Handler` signed/scoped to dev mode only) so both roles can be tested locally.
   - The backend mints an ephemeral, short-lived HMAC-signed bearer token per handler turn (`POST /api/copilot/conversations/{id}/turns` / `/confirm`), which is forwarded to Copilot via LangGraph `context_schema` (`ToolRuntime`), used on `GET /api/copilot/me` and handler mutations, never sent with `UNDERCOVER_API_KEY`, and never persisted in checkpoints, DB tables, memory, or logs.

2. **Shoppie Domain, Currency & Skipped `SWARM` / `IMPROVE` Features:**
   - Shoppie stores `support_requests` (`REQ-7001`, etc.), `₹10,000.00` refund cap in `backend/refunds.py`, INR amounts, and IST timestamps (`docs/BRAND.md`), and skipped `SWARM.md` and `IMPROVE.md` (no settings UI, no multi-agent swarm, no evaluations tab in the dashboard).
   - To satisfy Sections 3, 4, and 10 of `docs/COPILOT.md` cleanly without building out-of-scope pages, Phase 1 adds backend pilot settings (`GET /api/settings` with `approval_limit=10000.0`, `straight_through_target=0.70`, `accuracy_objective=0.85`, `default_work_cap=5`), an `evaluations` table + backend trigger/status routes (`POST /api/evaluations`, `GET /api/evaluations/current`, `GET /api/evaluations/{eval_id}`) reusing existing `is_evaluation` support in `agent/main.py` / `backend/main.py` (while strictly hiding `data/evals/` reference decisions from the Copilot), and deterministic Python computation of the three pilot success criteria (`#1` straight-through `>= 70%`, `#2` accuracy `>= 85%`, `#3` over-limit payouts all signed off—explicitly explaining when none have been paid over `₹10,000.00` yet).

3. **Dual Deployment (Azure Container Apps Option B + AWS AgentCore Runtime):**
   - Keep full naming parity with `labs/week3day5_lab1.ipynb` (`copilot` runtime in `agentcore/agentcore.json`, `runtime/copilot_main.py`, `UNDERCOVER_COPILOT` defaulting locally to `http://127.0.0.1:8081`, `UNDERCOVER_COPILOT_RUNTIME`, `UNDERCOVER_COPILOT_MEMORY_ID`, Memory name `undercover_copilot_memory`).
   - Also extend `azure/main.bicep`, `azure/Dockerfile.copilot`, and `azure/README.md` with an internal `shoppie-copilot` Container App on `:8080` wired to `shoppie-backend` via `UNDERCOVER_COPILOT=http://shoppie-copilot`.

---

## Phases

### Phase 1: Handler Identity, Copilot Persistence, Settings & Backend APIs

Build the backend foundation in `backend/` so handlers have verified roles, conversations and step logs persist in PostgreSQL (and SQLite in tests), and the Copilot can query pilot settings, evaluations, customer histories, and deterministic overview metrics using only the caller's handler token.

**Changes:**
- `backend/handlers.py`: Handler directory (`HND-01` Dana Whitfield `can_sign_off_over_limit=True`, `HND-02` Marcus Oyelaran `can_sign_off_over_limit=False`), Entra ID Easy Auth resolution + local dev fallback, and ephemeral HMAC-signed handler bearer token minting/verification (`create_handler_token`, `verify_handler_token`).
- `backend/store.py`: Add `copilot_conversations`, `copilot_turns`, `copilot_steps`, and `evaluations` tables in `undercover` schema; store pilot settings defaults and query helpers for customer order/request history and success-criteria aggregates.
- `backend/main.py`: Enforce handler permission on `POST /api/requests/{request_id}/review` (reject over-limit sign-off with `403` when `not handler.can_sign_off_over_limit`), add `GET /api/copilot/me`, `GET /api/settings`, `GET /api/copilot/overview`, `GET /api/copilot/customers/{customer_id}`, `POST /api/evaluations`, `GET /api/evaluations/current`, `GET /api/evaluations/{eval_id}`, and conversation/turn/step endpoints (`GET/POST /api/copilot/conversations`, `GET/PATCH /api/copilot/conversations/{conversation_id}`, `POST /api/copilot/conversations/{conversation_id}/steps`, `POST /api/copilot/conversations/{conversation_id}/turns`, `POST /api/copilot/conversations/{conversation_id}/turns/{turn_id}/confirm`, `POST /api/copilot/conversations/{conversation_id}/turns/{turn_id}/stop`). All routes follow `docs/TECH_STACK.md` (`operation_id`, one-line docstring, no optional inputs).

**Success criteria:**
- [x] `HND-01` (Dana Whitfield, can sign off over limit) and `HND-02` (Marcus Oyelaran, cannot sign off over limit) resolve cleanly from Azure Easy Auth headers or local dev mode, and `GET /api/copilot/me` returns the active handler identity.
- [x] Ephemeral handler bearer tokens expire cleanly, reject tampering or wrong audience, never equal `UNDERCOVER_API_KEY`, and are rejected on agent-only endpoints (`/api/requests/{id}/start`, etc.).
- [x] `POST /api/requests/{id}/review` returns `403` if a handler without `can_sign_off_over_limit` attempts to confirm an over-limit refund (`> ₹10,000.00`).
- [x] `GET /api/copilot/overview` computes straight-through rate (`>= 70%` target), evaluation accuracy (`>= 85%` target), and over-limit human sign-off coverage deterministically in Python (handling zero over-limit payouts cleanly) and never reads or exposes `data/evals/` expected answers.
- [x] Conversations, turns, and steps persist per `handler_id`; another handler receives `403` or `404` when reading or posting to someone else's conversation.
- [x] `uv run pytest backend/tests` passes with new unit tests covering handler tokens, role enforcement, conversations/steps CRUD, evaluations, and metrics.

---

### Phase 2: Copilot Tools, Context Gateway Routing & Handler Memory

Create the Copilot's synchronous tool suite and memory adapter under `agent/` so the assistant can inspect records, call context tools strictly through Gateway (`undercover-context`) in cloud or direct `UNDERCOVER_CONTEXT` locally, manage isolated handler memory, and report structured step progress.

**Changes:**
- `agent/copilot_memory.py`: `CopilotMemoryStore` wrapping `AgentCoreMemoryStore` using `UNDERCOVER_COPILOT_MEMORY_ID` (memory name `undercover_copilot_memory`, namespaces `/handlers/{actorId}/preferences` and `/handlers/{actorId}/facts`, session-free cross-conversation recall scoped strictly to `handler_id`), gracefully returning no-op locally when unset.
- `agent/copilot_tools.py`: Read `CopilotContext` via `ToolRuntime` (`runtime.context`), send only `Authorization: Bearer <handler_token>` on Shoppie backend calls, never read or pass `UNDERCOVER_API_KEY`, and emit structured step events (`POST /api/copilot/conversations/{id}/steps`) for every tool invocation with concise one-line human summaries, elapsed `duration_ms`, and expandable JSON details.
- Tool groups in `agent/copilot_tools.py`:
  - Request & customer lookups (`list_requests`, `get_request`, `list_waiting_requests`, `get_request_context`, `get_customer_history`, `get_copilot_overview`).
  - Context layer via MCP (`search_policies`, `read_policy`, `get_policy_section`, `search_manual`, `read_manual_section`, `search_cases`, `read_case`) routing through `Gateway` with target filter `undercover-context` only (never `undercover-app`), or direct `UNDERCOVER_CONTEXT` when running locally without Gateway.
  - Operations (`start_work_run`, `run_evaluation`, `wait_for_run`, `wait_for_evaluation`, `send_back_request`, `prepare_sign_off` / confirmation intent marker) with deterministic wait bounds and honest progress reporting if still running.
  - Handler memory (`save_handler_memory`, `search_handler_memory`).

**Success criteria:**
- [ ] Every Copilot tool reads `handler_token` and `handler_id` exclusively from `ToolRuntime.context` and never touches LangGraph `config` or `UNDERCOVER_API_KEY`.
- [ ] In Gateway mode, Copilot discovers and calls only `undercover-context___*` tools and never exposes or invokes `undercover-app___*` agent tools; locally without `UNDERCOVER_GATEWAY_URL`, it connects directly to `UNDERCOVER_CONTEXT`.
- [ ] Before requesting sign-off confirmation, code verifies `handler.can_sign_off_over_limit` against `payout` and `approval_limit` (`₹10,000.00`) and refuses immediately without pausing if the handler lacks permission.
- [ ] `CopilotMemoryStore` scopes reads and writes to `/handlers/{handler_id}/preferences` and `/handlers/{handler_id}/facts` so Dana never sees Marcus's preferences or facts.
- [ ] Step emission truncates/sanitizes payloads so bearer tokens never appear in step details, memory entries, or logs, and unit tests in `agent/tests/test_copilot_tools.py` and `agent/tests/test_copilot_memory.py` pass against gateway/memory stand-ins.

---

### Phase 3: Hand-Written LangGraph Graph, Confirmation Interrupt & AgentCore App

Implement the hand-crafted synchronous LangGraph `StateGraph` (`model -> ToolNode -> model`) with a dedicated `confirm_sign_off` node calling `interrupt(...)`, PostgreSQL checkpointing, and the Copilot `BedrockAgentCoreApp` HTTP/Runtime service.

**Changes:**
- `agent/copilot_graph.py`:
  - `CopilotState` (`MessagesState` + pending `sign_off_request`, handler metadata, `page_context`, `steps_recorded`) and `CopilotContext` dataclass (`context_schema`).
  - Synchronous `StateGraph`: `load_memory` -> `model` (bound LLM with Copilot system prompt tailored to Shoppie INR/IST, handler identity, current dashboard route/request context, instructions to verify via tools rather than guessing and never invent numbers) -> `tools` (`ToolNode`) -> `model`, plus dedicated `confirm_sign_off` node calling `interrupt(payload)` before executing `POST /api/requests/{id}/review` on `Command(resume={"approved": True | False})`.
  - Synchronous `PostgresSaver` checkpointer keyed by `thread_id = conversation_id` (SQLite/memory checkpointer in tests).
- `agent/copilot_entrypoint.py`:
  - `BedrockAgentCoreApp` (`on_runtime()`, `_require_bearer_key()`, `_start_async_task()`, `ensure_ready()`) listening on `:8081` locally (via uvicorn in dev) and `:8080` on Runtime, handling `health`, `copilot_turn`, and `copilot_resume`.
  - Runs each turn synchronously on an async-task worker thread, verifies handler identity on startup (`GET /api/copilot/me` with `handler_token`), persists assistant response / `waiting_confirmation` / `done` / `failed` state back to backend via `/api/copilot/conversations/{id}`, and cleanly handles cross-session Runtime resumes (`Command(resume=...)`).
- `runtime/copilot_main.py`: Thin runtime launcher running `agent.copilot_entrypoint:app` (`host="0.0.0.0", port=8080`), matching `runtime/main.py`.

**Success criteria:**
- [ ] `agent/copilot_graph.py` constructs a manual `StateGraph` (`model`, `tools` `ToolNode`, `confirm_sign_off`) without `create_agent`, remaining synchronous throughout.
- [ ] `confirm_sign_off` contains only the `interrupt()` pause and post-resume decision routing so re-running the node from the top on `Command(resume=...)` never duplicates side effects.
- [ ] Resuming a paused conversation from a brand-new checkpointer/Runtime instance (`thread_id=conversation_id`) with a fresh ephemeral `handler_token` completes the sign-off (`approved=True`) or cancels cleanly (`approved=False`) and generates the assistant's closing confirmation.
- [ ] No auth tokens appear inside saved `PostgresSaver` checkpoint rows or state `messages`.
- [ ] `uv run pytest agent/tests/test_copilot_graph.py agent/tests/test_copilot_entrypoint.py` passes including pause/resume across separate graph instances and permission denial cases.

---

### Phase 4: Backend Turn Orchestration, Timeout Recovery & Local Dev Wiring

Connect the backend conversation/turn APIs to the Copilot AgentCore service over HTTP locally or `bedrock-agentcore` on AWS Runtime, enforce turn concurrency and the 20-minute stale-turn cutoff, and wire local scripts in `package.json`.

**Changes:**
- `backend/agent_service.py`: Add `copilot_service_url()` (`UNDERCOVER_COPILOT` defaulting locally to `http://127.0.0.1:8081`), `copilot_runtime_arn()` (`UNDERCOVER_COPILOT_RUNTIME`), `copilot_runtime_session_id(conversation_id)` (stable AgentCore session id per conversation so Runtime resumes reuse or cleanly restart sessions), and `call_copilot(payload)` with transient conflict/start-stop retry handling matching `call_agent`.
- `backend/main.py`: Wire `POST /api/copilot/conversations/{id}/turns` and `/confirm` to mint the fresh ephemeral handler bearer token, enforce one active turn per conversation, mark turns running longer than 20 minutes (`STALE_COPILOT_SECONDS = 1200`) as `stopped` automatically so the handler can retry or send a follow-up, and return structured `CallState` (`accepted` / `refused` / `did_not_take` / `uncertain`).
- `package.json`: Add `dev:copilot` (`uv run uvicorn agent.copilot_entrypoint:app --port 8081 --reload`), include it in `npm run dev`, and extend `check:runtime` to verify `runtime/copilot_main.py` compiles cleanly too.

**Success criteria:**
- [ ] Starting a turn creates a `working` turn record, dispatches asynchronously to `UNDERCOVER_COPILOT` or `UNDERCOVER_COPILOT_RUNTIME`, and rejects a concurrent second turn while one is still active (`< 20` minutes).
- [ ] When a turn exceeds 20 minutes without completion, reading or posting to the conversation transitions the stale turn to `stopped` cleanly so the handler is never stuck.
- [ ] Confirming or cancelling a `waiting_confirmation` turn dispatches `copilot_resume` with a freshly minted ephemeral handler token and validates that only the conversation owner can resume it.
- [ ] `npm run dev` starts `backend`, `agent`, `copilot`, `context`, and `dashboard` cleanly together, and `backend/tests/test_copilot.py` + `backend/tests/test_agent_service.py` pass.

---

### Phase 5: Dashboard Copilot Sidebar, Live Steps & Confirmation Card

Build the persistent, collapsible Copilot sidebar inside `dashboard/` matching `docs/BRAND.md`, live polling every second while working, structured tool/step cards, formatted markdown responses with clickable request links, confirmation card for sign-off only, and dev handler switcher.

**Changes:**
- `dashboard/src/types.ts` & `dashboard/src/copilotApi.ts`: Strongly typed Copilot interfaces (`HandlerProfile`, `CopilotConversation`, `CopilotTurn`, `CopilotStep`, `ConfirmationPrompt`) and typed API helpers with zero `any`.
- `dashboard/src/CopilotSidebar.tsx`:
  - Docked right-hand panel present across every page (`Requests`, `Request detail` / `Waiting`), collapsible to a slim tab with a custom Copilot SVG mark, persisted in `localStorage` (`shoppie.copilot.open`), and opening as a clean overlay drawer on narrow viewports (`<= 1080px`) without breaking side-by-side usability on desktop.
  - Header with title, conversation selector + `+ New chat`, local dev handler switcher (`Dana Whitfield` vs `Marcus Oyelaran` shown only when `auth_mode !== "entra"`), and collapse button.
  - Empty state with domain-accurate starter prompts (*"What needs my attention today?"*, *"Explain REQ-7001's decision"*, *"Start a run for 5 pending requests"*, *"How are we tracking against pilot targets?"*).
  - Message stream rendering handler prompts, live 1s-polled step cards while `working` (icon by tool kind, concise one-line status, duration pill, expandable details disclosure), plain formatted markdown assistant answers (converting request IDs like `REQ-7001` and citations into clickable SPA navigation links), and the `ConfirmationCard` when `status === "waiting_confirmation"` (`Confirm sign off` / `Cancel` wired to `/confirm`).
  - Notifies `App.tsx` via `onMutated` callback whenever a turn triggers/completes a mutation or finishes so the visible page refreshes live.
- `dashboard/src/App.tsx` & `dashboard/src/app.css`: Layout integration across Light (`#F0F4F5`/`#FFFFFF`) and Dark (`#04232E`/`#02151C`) palettes, crisp contrast, US English copy, INR (`₹`) + IST formatting, and zero `!` in UI text.

**Success criteria:**
- [ ] Sidebar opens on initial load, collapses to a slim tab with its own icon, restores collapsed/open preference from `localStorage`, and overlays smoothly on narrow screens.
- [ ] Underlying request queue, request detail, and action controls remain fully usable alongside the open sidebar and update automatically after the Copilot completes a run, send-back, or sign-off.
- [ ] While working, live step cards update at ~1s cadence with icon, one-line status, duration, and expandable JSON/detail toggle; errors and `stopped` turns display clearly with recovery.
- [ ] Sign-off pauses display a `ConfirmationCard` listing request ID, customer, INR payout, and `Confirm sign off` / `Cancel`; `send_back` executes without confirmation; over-limit attempts by Marcus show an immediate permission explanation instead of a confirmation card.
- [ ] `npm run check:ts` and Vitest (`dashboard/src/*.test.tsx` including new `CopilotSidebar.test.tsx`) pass with zero warnings/errors.

---

### Phase 6: Deployment Configs (AWS AgentCore + Azure Container Apps), E2E & Docs

Finalize deployment manifests for both AWS AgentCore Runtime and Azure Container Apps Option B, add Playwright coverage with a stand-in Copilot server, run full offline and live verification (both light and dark themes), and bring all foundational docs up to date.

**Changes:**
- `agentcore/agentcore.json`: Add `copilot` runtime (`"name": "copilot"`, `"entrypoint": "copilot_main.py"`) alongside `agent` so `labs/week3day5_lab1.ipynb` deploys `undercover_default_copilot` beside `undercover_default_agent`.
- `agentcore/cdk/lib/cdk-stack.ts`: Add `'UNDERCOVER_COPILOT_MEMORY_ID'` to `SETTINGS` so deployed runtimes receive the Copilot Memory ID from `agentcore/.env.local`.
- `azure/Dockerfile.copilot`, `azure/main.bicep`, `azure/README.md`: Add internal `shoppie-copilot` Container App (`:8080`, `UNDERCOVER_API`, `UNDERCOVER_CONTEXT`, `POSTGRES_URL`, LLM key) and pass `UNDERCOVER_COPILOT=http://shoppie-copilot` to `shoppie-backend`.
- `e2e/copilot_stand_in.js` & `e2e/copilot.spec.ts`: Local stand-in Copilot server + Playwright tests exercising sidebar collapse persistence, multi-step streaming answer + request navigation link, `interrupt()` sign-off confirmation approve/cancel, `send_back` direct execution, and Marcus over-limit permission guard; confirm existing `e2e/dashboard.spec.ts` and `e2e/runtime.spec.ts` pass with the sidebar open.
- Docs sync: Update `docs/SPEC.md`, `docs/TECH_STACK.md`, `docs/CODE_STANDARDS.md`, `docs/PROCESS.md`, and `docs/STATUS.md` to document the Copilot architecture, run commands, routes, and deployment layout accurately.

**Success criteria:**
- [ ] `agentcore/agentcore.json` and `agentcore/cdk/lib/cdk-stack.ts` match `labs/week3day5_lab1.ipynb` naming (`copilot`, `copilot_main.py`, `UNDERCOVER_COPILOT_MEMORY_ID`).
- [ ] `azure/main.bicep` validates cleanly (when `az bicep` is checked) and defines `shoppie-copilot` beside `shoppie-agent`, `shoppie-backend`, and `shoppie-context`.
- [ ] With `.env` temporarily moved aside, `npm run check` (`check:python`, `check:server`, `check:runtime`, `check:context`, `check:ts`, Vitest, and Playwright E2E including `e2e/dashboard.spec.ts`, `e2e/runtime.spec.ts`, and `e2e/copilot.spec.ts`) passes completely.
- [ ] Live smoke verification with `npm run seed`, the default model, and Playwright browser inspection confirms working end-to-end Copilot queries, runs, send-backs, sign-off confirmations, and clean rendering in both light and dark modes (`BRAND.md`).
- [ ] `docs/SPEC.md`, `docs/TECH_STACK.md`, `docs/CODE_STANDARDS.md`, `docs/PROCESS.md`, and `docs/STATUS.md` reflect the completed COPILOT build.
