# Shoppie - ecommerce platform

Shoppie is an ecommerce support platform for agents who process customer cancellation, return and refund requests.

## How support works today

Customers contact support about orders they want to cancel, items they want to return, or payments they want refunded. Support agents review each request, check the order and its history against Shoppie's policies, and decide whether to approve it, decline it or ask for more information.

## The pilot

We want an AI agent, the Shoppie Agent, to help agents work through the support queue. For each request it should:

- read the customer's request, order details and relevant history
- look up the sections of the shopping, cancellation and returns policies the request needs, rather than reading whole policies
- query the enterprise context layer and search past customer interactions in customer memory
- work out the eligible refund amount from Shoppie's policy rules when a refund is in play, creating an audited calculation breakdown
- recommend whether to approve, decline or ask for more information conforming to claims manual section 14 audit standards
- explain the recommendation and cite the policy sections and context sources it relied on

## Working the queue

One run works the whole queue. A support agent starts the agent once, with a cap on how many requests the run may work, and the backend allows only one active run at a time, refusing a second start with a conflict naming the active run. While the agent works a run or a resumed request, it sends a heartbeat every minute; work silent for five minutes is marked stopped with `"The agent stopped answering"` so new work can go ahead. The agent plans the run as a to-do list, chooses the order, works each waiting or sent-back request up to the cap, and finishes with a short summary. Each request attempt runs on its own checkpointed thread and pauses once its recommendation is ready for support review.

When a support agent signs off on a held request with their name, the platform records the confirmed resolution and the request is decided straight away, whether or not the agent service is reachable; any paused request thread is then closed without completing the request a second time. Alternatively, a support agent may send a recommended request back to the agent with their name and a required note (up to 1,000 characters), archiving the earlier decision and asking the agent to resume its paused thread with the note and a fresh model-call budget; requests left for support or held by Security check cannot be sent back. One named request can also be worked on its own.

A long run will meet timeouts, errors and process restarts. If a run or service stops partway through an attempt, the next run resumes that request from its last saved checkpoint on the same attempt (or picks up a sent-back request that the agent never received), rather than starting over, while skipping requests whose active resume heartbeat is under five minutes old. A request whose attempt fails before it has a recommendation goes back in the queue and is tried again on a fresh attempt, and one that fails twice is left for a support agent. No request gets two sign-off reviews. Before it finishes, the agent checks it has not missed a waiting request within its cap.

The agent supports the support team; agents remain responsible for processing requests and confirming decisions.

## What the team needs to see

- The queue, with where each request stands: waiting, in progress, waiting for agent review, sent back, or decided, and which requests were left for a support agent.
- The current run's to-do list, with each item to do, in progress, done or left for a support agent, the reason if the run stopped (`stop_reason`), and the run's summary when it ends.
- Each request in full, with any previous sent-back decisions and reviewer notes beside the current recommendation, reasons, manual version, maker, and calculation breakdown line by line.
- What the agent is doing on a request while it works, step by step: tool calls (with Gateway target names when routed via Gateway), context lookups, memory searches and saves, refusal chips, and errors.
- The Context used panel, listing every looked-up item grouped by file with each file's source metadata (`system`, `owner`, `reviewed`).
- The agent's instructions for running the queue and for working a request, read only.

## Rules

- Recommendations follow Shoppie's cancellation, return and refund policies.
- Decision records strictly follow claims manual section 14: non-empty reason, section, and decided_by; approvals require a payout matching the attempt's calculation on file; declines and referrals have a payout of zero.
- The agent must not claim that an action has been completed unless the system confirms it.
- Requests that need judgment or information should be held for an agent to review.
- Customer and order data must be limited to what is needed to process the request. Customer memory stores only request message and decision record under customer actor ID.

## Security

- The agent calls the platform with its own API key (`UNDERCOVER_API_KEY`). The routes only the agent uses refuse calls without it, and the key never stands in for a support agent: it cannot sign off on a request or start a run.
- Before the agent's model sees a request, a security check reads everything the customer wrote. A request that tries to instruct whoever handles it is held for a support agent with the check's reason, cited as "Security check", and the agent's model never sees it. Flagged requests never save events to memory.
- The agent's API is published on its own (`/api/agent/openapi.json`) with only the six request-agent operations.
- No refund calculation above ₹10,000.00 is given automatically; the platform refuses it, naming the limit, whatever state the request is in.
- The agent can be routed directly or through the AgentCore Gateway (`UNDERCOVER_GATEWAY_URL`) with SigV4 signing. Refusals from the Gateway or application are captured cleanly as refusal steps without breaking execution.

## Our records

The platform uses three primary data sources: order records (`customer_orders.json`), customer support requests, and data governance context files (`context/`). These records support the queue, the context layer, and the agent's recommendations.

## Shoppie Copilot

The dashboard includes a collapsible right-hand **Shoppie Copilot** sidebar available across the queue and request detail views:

- **Queue & request intelligence:** Copilot answers questions about priority requests, policy citations, refund breakdowns, customer history, queue runs, and pilot quality criteria by calling real backend, policy, and enterprise context tools rather than guessing.
- **Handler roles & memory:** Support handlers identify via Azure Entra ID headers in production or a local dev switcher between Dana Whitfield (`HND-01`, Senior Support Handler) and Marcus Oyelaran (`HND-02`, Support Handler). Handler preferences and facts are stored strictly in that handler's isolated memory namespace.
- **Human-in-the-loop actions:** Copilot executes send-backs immediately when asked, starts capped queue runs and tracks progress, and pauses with an explicit `ConfirmationCard` before recording any support sign-off (`Confirm sign off` / `Cancel`). Payouts over ₹10,000.00 require senior handler authority and are refused immediately for non-senior handlers.

## Not for now

- No real cancellations, returns or refunds.
- No messages to customers.
