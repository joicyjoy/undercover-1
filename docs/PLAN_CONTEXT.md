# Plan: The context layer, memory and the Gateway for the Shoppie Agent

SIDENOTE: For consistency with the course, please read `labs/week3day3.ipynb`, which deploys the app and the context layer, creates the Gateway and the memory, and runs the agent against them, without running it. Where the notebook and this document differ, follow this document. Include this sidenote in your plan too.

This plan adapts `docs/CONTEXT.md` for Shoppie and our Render deployment (`https://undercover-9gfu.onrender.com` backed by Render Postgres `shoppie-postgres` in the `undercover` schema), while maintaining full compatibility with the course's AWS Lambda and AgentCore deployment architecture.

Today:
- The Shoppie Agent's tools call the backend directly with its API key (`UNDERCOVER_API_KEY`), so an AgentCore Gateway Policy (such as refusing payouts/refunds over a limit) never intercepts model actions or decisions.
- What the agent needs to know (plans, rules, term definitions, reference data, policies, and manuals) is either hardcoded or spread across disjoint sources. Data governance owns the files in `context/`: each file names its source system, owner, and review date.
- Our decision records lack the full audit details required by section 14 of the operations manual: they do not capture the step-by-step refund calculation, who/what made the decision, or the manual's active version.
- The agent forgets each customer between requests.
- Support agents see policy sections read, but not context lookups, memory searches, or tool calls.

After this build, in six phases matching the steps of `docs/CONTEXT.md`:
1. **Phase 1 (Step 1): Tools from the Gateway.** When `UNDERCOVER_GATEWAY_URL` is set, the agent calls its tools through the AgentCore Gateway as MCP tools over streamable HTTP, signed with AWS SigV4 for `bedrock-agentcore`. Without it, the agent uses the direct route (app via API key, context layer via unsigned MCP at `UNDERCOVER_CONTEXT` or `http://127.0.0.1:8006/mcp`). Gateway refusals are recorded on the request, and a Gateway-refused refund/payment holds the request for support sign-off.
2. **Phase 2 (Step 2): The Context Layer.** An independent FastMCP service in `context/` serves the data governance files in `context/` with structured sources (`system`, `owner`, `reviewed`), providing 7 tools (`lookup_term`, `describe_fields`, `get_plan`, `get_claim_rules`, `search_reference_data`, `search_documents`, `read_section`). It runs on the laptop on port 8006 and on Lambda with IAM auth. The app's agent OpenAPI schema retains 6 operations.
3. **Phase 3 (Step 3): A Complete Decision Record.** Section 14 checks in `backend/records.py` enforce that every decision record holds the decision, refund, line-by-line calculation, section relied on, reason in plain language, maker (`decided_by`), and manual version, refusing invalid submissions with 422 naming section 14.
4. **Phase 4 (Step 4): Memory of Each Customer.** When `UNDERCOVER_MEMORY_ID` is set, the agent records request and decision events into AgentCore Memory (`langgraph-checkpoint-aws`), providing the model with a `search_memory` tool scoped strictly to the current customer's history.
5. **Phase 5 (Step 5): Every Step on the Request Page.** The dashboard displays tool calls (with `<target>___<tool>` naming when on Gateway), context lookups, memory steps, highlighted refusals, a **Context used** panel grouped by source file, and the full decision record with line-by-line calculation.
6. **Phase 6: Live Verification, Reseed & Documentation.** Reseed the database, verify all offline and live routes, update all project documentation, and confirm clean test execution.

---

## Design

### Three Sources of Data

| Source | Owner | What it holds | How the Shoppie Agent reaches it |
| --- | --- | --- | --- |
| **The context layer** | Data governance | The 7 files in `context/` | Read-only: via the Gateway's `undercover-context` target, or directly at `UNDERCOVER_CONTEXT` |
| **Memory** | The agent | Notes on each customer | AgentCore Memory directly, with AWS CLI credentials, when `UNDERCOVER_MEMORY_ID` is set |
| **The database** | The app | Requests, orders, customers, decisions, refund calculations, steps, runs | System of record reached through the app API; checkpoints in Postgres/SQLite |

### Modules and Structure

The context layer (`context/` package):
- `context/files.py`: Reads the 7 context files on process startup. Parses YAML with `source` metadata; splits markdown documents at `^## (\d+)\. (.+)$` into numbered sections without front matter. Every lookup returns answers with `sources`: `[{"file": ..., "system": ..., "owner": ..., "reviewed": ...}]`.
- `context/server.py`: `build_server()` creates a FastMCP server with `stateless_http=True`, `json_response=True`, exposing the 7 read-only tools with one-line descriptions. `handler(event, context)` provides the Lambda entry point under `Mangum(lifespan="auto")` with `TransportSecuritySettings(enable_dns_rebinding_protection=False)`.
- `context/__main__.py`: Serves the context server locally at `http://127.0.0.1:8006/mcp`.
- `context/tests/`: Comprehensive unit and Lambda handler tests.

The backend:
- `backend/context_files.py`: Isolated helper used only by seeding to read plans and document versions from `context/` without importing `context.files`.
- `backend/records.py`: Deterministic section 14 validators enforcing reason, section, maker, non-zero approval, zero decline/referral, and calculation matching.
- `backend/store.py`: Persists calculation history per attempt, decision audit fields (`decided_by`, `manual_version`, `calculation`, `gateway_refusal`), and step sources.
- `backend/main.py`: Removes legacy policy reading endpoints (moved to context layer); exposes `hold_payment` (or `POST /api/requests/{request_id}/hold`) for Gateway payment refusal; updates `/api/agent/openapi.json` to 6 operations.

The agent:
- `agent/routes.py`: Defines the `Route` protocol, `Target` (`undercover-app`, `undercover-context`), and `GatewayRefused`.
- `agent/mcp_tools.py`: Synchronous bridge executing async MCP tool calls over streamable HTTP with `MultiServerMCPClient`. Maintains an isolated event loop per call to protect the main thread's loop for Mangum.
- `agent/gateway.py`: Gateway route implementation. Uses `SigV4Auth` for `bedrock-agentcore` signing; maps tool calls to `<target>___<operation>`.
- `agent/direct.py`: Direct route implementation. App operations via API key; context operations via unsigned MCP.
- `agent/memory.py`: Wraps `AgentCoreMemoryStore` (`langgraph-checkpoint-aws`), saving request/decision events and recalling memories scoped to `/customers/{customer_id}` excluding the current request.
- `agent/tools.py`: Binds route-aware model tools and `search_memory` (when memory is enabled and run is not an evaluation).
- `agent/main.py`: Synchronous LangGraph request graph including `screen`, `agent`, `tools`, `decide`, `pay`/`hold`, `remember`, and `approval`/`complete`.

The dashboard:
- `dashboard/src/types.ts`: Adds new step types (`tool_call`, `lookup`, `memory_search`, `memory_save`, `refusal`), step `sources`, and calculation line items.
- `dashboard/src/components.tsx` & `dashboard/src/App.tsx`: Renders the new step timeline, the **Context used** panel, and the complete decision panel.

---

## Phase 1: Tools from the Gateway

Implement route abstraction and Gateway support:
- Create `agent/routes.py` with `Route` protocol, `Target`, and `GatewayRefused`.
- Create `agent/mcp_tools.py` for synchronous invocation of async streamable HTTP MCP tools, creating fresh event loops per call without touching the main thread's loop.
- Create `agent/gateway.py` with `SigV4Auth` signing requests for `bedrock-agentcore`, translating calls to `<target>___<operation>`, and raising MCP Policy errors as `GatewayRefused`.
- Create `agent/direct.py` routing backend operations with `UNDERCOVER_API_KEY` and context operations to `UNDERCOVER_CONTEXT` (or `http://127.0.0.1:8006/mcp`).
- Update `agent/tools.py` so model tools read request context from state and omit the request ID from model inputs.
- Add `hold_payment` in `backend/main.py` (`POST /api/requests/{request_id}/hold`, `operation_id="hold_payment"`), protected by `UNDERCOVER_API_KEY`, moving an approved request to waiting for sign-off with a Gateway refusal message.
- Update `agent/main.py`:
  - Handle tool refusals from Gateway as `refusal` steps while continuing model execution.
  - If a payment is refused by Gateway Policy, call `hold_payment` and transition to waiting for sign-off.
  - Read `UNDERCOVER_GATEWAY_URL`, `UNDERCOVER_CONTEXT`, and `UNDERCOVER_MEMORY_ID` dynamically when compiling graphs.

### Success criteria

- [x] Unit and stub tests verify model tools have identical names and schemas on both Gateway and direct routes, with request ID omitted from model inputs.
- [x] Stub test (Gateway route): A request execution against a stand-in Gateway calls `undercover-app___get_request_context`, `undercover-app___calculate_refund`, `undercover-app___save_recommendation`, and `undercover-app___complete_request` signed with SigV4 for `bedrock-agentcore`.
- [x] Stub test (Gateway route): Bookkeeping, security screening, and runs reach the backend directly with `X-API-Key` and never touch the Gateway.
- [x] Stub test: `get_request_context` produces identical output on both direct and Gateway routes.
- [x] Stub test: An app refusal returns a clean refusal message and records a `refusal` step without halting the graph.
- [x] Stub test: A Gateway Policy tool refusal returns `"Refused by the Gateway: ..."` and records a `refusal` step naming the Gateway.
- [x] Stub test: A Gateway payment refusal marks the request waiting for support sign-off with Gateway refusal recorded.
- [x] Unit test: `POST /api/requests/{request_id}/hold` requires `UNDERCOVER_API_KEY` and moves an approved request to waiting for sign-off.
- [x] Unit test: Verifies `asyncio.get_event_loop()` remains functional on the main thread after MCP execution.
- [x] `npm run check:python` passes with at least 80% coverage.

---

## Phase 2: The Context Layer

Build the context layer FastMCP service:
- Populate `context/` with the 7 data governance files from `upstream/week3day3-start:context/` (`data_dictionary.yaml`, `glossary.yaml`, `plans.yaml`, `claim_rules.yaml`, `reference_data.yaml`, `policy_wording.md`, `claims_manual.md`) while keeping Shoppie's policies.
- Create `context/files.py` to load and cache the context data once on import and provide search/read lookups that return answers with structured `sources`.
- Create `context/server.py` using FastMCP with 7 tools (`lookup_term`, `describe_fields`, `get_plan`, `get_claim_rules`, `search_reference_data`, `search_documents`, `read_section`) with one-line docstrings and required inputs. Provide `handler = Mangum(build_server().streamable_http_app(), lifespan="auto")` with `TransportSecuritySettings(enable_dns_rebinding_protection=False)`.
- Create `context/__main__.py` to run the context server on `http://127.0.0.1:8006/mcp`.
- Update `pyproject.toml` with `[dependency-groups] context` (`mangum`, `mcp`, `pyyaml`), add `pyyaml` to `server`, and configure ruff/pyright/pytest/coverage to include `context/`.
- Update `package.json` with `dev:context` and `check:context`.
- Update backend seeding (`backend/seed.py`, `backend/context_files.py`) to read plans and document versions from `context/`.
- Remove legacy `read_policy_section` from the backend API, reducing `/api/agent/openapi.json` to 6 operations.

### Success criteria

- [x] Unit tests against real `context/` files verify `lookup_term` (case-insensitive and unknown term listing), `describe_fields`, `get_plan`, `get_claim_rules`, `search_reference_data` (multi-word match), `search_documents`, and `read_section` (valid and invalid sections).
- [x] Unit tests verify all context tool responses include `sources` metadata (`file`, `system`, `owner`, `reviewed`) and no section includes markdown front matter.
- [x] Unit tests verify all 7 context tools have one-line descriptions, required inputs, and read-only annotations.
- [x] Lambda handler test verifies calling `context.server.handler` twice in one process with Function URL events succeeds (listing tools and calling a tool).
- [x] `npm run check:context` passes in isolation with only the `context` dependency group installed.
- [x] Local server test verifies `uv run -m context` answers `tools/list` on `http://127.0.0.1:8006/mcp`.
- [x] Seeding test verifies plans and document versions are loaded from `context/` into the database.
- [x] API test verifies `/api/agent/openapi.json` contains exactly 6 operations.
- [x] `npm run check:python` passes with at least 80% coverage.

---

## Phase 3: A Complete Decision Record

Enforce claims manual section 14 audit standards:
- Create `backend/records.py` with deterministic validation for decision records:
  - Validates non-empty `reason`, `section`, and `decided_by`.
  - Enforces that an approval must have a refund amount > 0, and decline/referral must have refund = 0.
  - Enforces that an approval must have an existing calculation on file for the current attempt, and the refund amount must exactly match the calculation.
  - Refuses non-compliant submissions with HTTP 422 naming section 14 (`"Claims manual section 14: ..."`).
- Update `backend/store.py` to record calculations per attempt, and persist `decided_by`, `manual_version`, `calculation`, and `gateway_refusal` on the decision record.
- Update `POST /api/requests/{request_id}/recommendation` to execute section 14 checks.
- Update test helpers in backend, agent, and e2e test suites to record matching calculations before testing approvals.

### Success criteria

- [x] API tests verify section 14 refusals (HTTP 422) for missing reason, missing section, missing `decided_by`, zero-refund approval, non-zero decline/referral, approval without prior calculation, and refund amount mismatch.
- [x] API tests verify successful recording of complete approvals (with line-by-line calculation), declines, referrals, and security-check referrals, persisting `decided_by`, `manual_version`, and `calculation`.
- [x] Store tests verify calculation history is tracked per request attempt and replaced on recalculation.
- [x] `npm run check:python` passes with at least 80% coverage.

---

## Phase 4: Customer Memory

Add customer memory integration using AgentCore Memory:
- Add `langgraph-checkpoint-aws>=1.2.4` to dependencies.
- Create `agent/memory.py`:
  - `memory_store()` instantiates `AgentCoreMemoryStore` when `UNDERCOVER_MEMORY_ID` is set, or returns `None`.
  - `remember(store, request, decision)` saves two events (request message and decision record) under customer actor ID and request session ID. Skips saving for requests flagged by security check or evaluation copies.
  - `recall(store, customer_id, request_id, query)` searches `/customers/{customer_id}`, filtering out the current request's own summary.
  - Handles AgentCore connection failures gracefully (logs warnings on save; returns refusals on search).
- In `agent/tools.py`, add `search_memory(query)` available only when memory is enabled and the run is not an evaluation.
- In `agent/main.py`, integrate the `remember` node following decision recording.

### Success criteria

- [ ] Unit tests with a stand-in memory store verify two events are saved after a decision.
- [ ] Unit tests verify no events are saved for security-flagged requests or evaluation runs.
- [ ] Unit tests verify `recall` searches only within the customer's namespace and excludes the current request's session summary.
- [ ] Unit tests verify graceful error handling when AgentCore Memory is unreachable.
- [ ] Unit tests verify `search_memory` is omitted when `UNDERCOVER_MEMORY_ID` is unset.
- [ ] `npm run check:python` passes with at least 80% coverage.

---

## Phase 5: Every Step on the Request Page

Update the dashboard to visualize context and memory:
- Update `dashboard/src/types.ts` to include `tool_call`, `lookup`, `memory_search`, `memory_save`, and `refusal` step types, step `sources`, and decision calculation lines.
- In `dashboard/src/components.tsx` and `dashboard/src/App.tsx`:
  - Render tool calls (named `<target>___<tool>` when on Gateway), context lookups, and memory interactions.
  - Highlight refusals distinctly.
  - Add the **Context used** panel (replacing "Sections read"), listing context items grouped by file with source metadata (`system`, `owner`, `reviewed`).
  - Update the decision panel to display the complete record, including line-by-line calculation breakdown, maker, and manual version.
- Update Vitest tests in `dashboard/src/App.test.tsx` and Playwright tests in `e2e/dashboard.spec.ts`.

### Success criteria

- [ ] Vitest unit tests verify rendering of each step kind, refusal badges, Context used panel grouped by file, and full decision calculation breakdown.
- [ ] Playwright E2E tests verify the complete request page with lookups, Context used panel, and decision calculations.
- [ ] `npm run check:ts` and `npm run e2e` pass.

---

## Phase 6: Live Verification, Reseed & Documentation

Final end-to-end verification and documentation update:
- Reseed the database with `npm run seed`.
- Run `npm run check` with `.env` moved aside to verify complete offline test execution.
- Update documentation:
  - `docs/SPEC.md`: Reflect three data sources, context layer, Gateway routing, customer memory, complete decision records, and dashboard updates.
  - `docs/TECH_STACK.md`: Document `context/` server, dual routes, `mcp_tools.py`, memory, `records.py`, 6-operation agent schema, and deployment settings.
  - `docs/PROCESS.md`: Document context server dev and check scripts.
  - `docs/STATUS.md`: Document completed capabilities and verification results.
  - `data/README.md`: Document context seeding source files.
- Verify `git diff --check` passes with no whitespace or formatting errors.

### Success criteria

- [ ] `npm run seed` resets the database cleanly.
- [ ] With `.env` moved aside, `npm run check` passes all Python, TypeScript, server-group import, context-group import, and Playwright end-to-end tests.
- [ ] `docs/SPEC.md`, `docs/TECH_STACK.md`, `docs/PROCESS.md`, `docs/STATUS.md`, and `data/README.md` are updated.
- [ ] `git diff --check` passes with 0 errors.

---

## Not in this plan

- Creating or deleting AWS resources (Gateway, Memory, IAM roles) directly in code or tests; deployment scripts and notebooks handle AWS resources.
- Memory in evaluation runs.
- Masking personal fields for the agent or requesting documents from customers.
- Modifying the underlying content of files in `context/`.
