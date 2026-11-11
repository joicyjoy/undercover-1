import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { App } from "./App";
import { ProgressCard } from "./ProgressCard";
import { ActionPanel, RunPanel, SentBackHistory } from "./components";
import type { Recommendation, RequestDetail, Run } from "./types";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function urlOf(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.href;
  return input.url;
}

it("renders sent-back history, timeline step, waiting notice, and send-back form", async () => {
  const archivedRec: Recommendation = {
    outcome: "approve",
    decision: "approve",
    payout: 349,
    rationale: "Initial approval.",
    policy_citations: ["Cancellation Policy: 2. Window"],
    next_action: "Confirm.",
    priority_human_review: false,
    section: "Cancellation section 2",
    calculation: {
      lines: [{ step: "Fruit box (x1)", amount: 349 }],
      payout: 349,
    },
  };
  const waitingRec: Recommendation = {
    outcome: "decline",
    decision: "decline",
    payout: 0,
    rationale: "Dispatched before request.",
    policy_citations: ["Cancellation Policy: 2. Window"],
    next_action: "Confirm decline.",
    priority_human_review: false,
    section: "Cancellation section 2",
  };
  const sentBackDetail: RequestDetail = {
    request: {
      request_id: "REQ-7001",
      type: "cancellation",
      created_at: "2026-11-06T08:35:00+05:30",
      customer_message: "Please cancel.",
      requested_scope: "whole_order",
      evidence: [],
    },
    customer: { customer_id: "CUS-001", name: "Asha Sharma" },
    order: {
      order_id: "ORD-2026-10001",
      placed_at: "2026-11-06T08:10:00+05:30",
      fulfillment_status: "dispatched",
      payment_status: "paid",
      payment_method: "upi",
      delivery_window: null,
      items: [],
      subtotal: 349,
      delivery_fee: 0,
      total: 349,
    },
    status: "sent_back",
    attempts: 0,
    recommendation: null,
    review: null,
    sent_back: [
      {
        decision: archivedRec,
        note: "Order was already dispatched.",
        sent_back_by: "Priya Nair",
        sent_back_at: "2026-11-06T09:15:00+05:30",
      },
    ],
    steps: [
      {
        stage: "sent_back",
        message: "Sent back by Priya Nair: Order was already dispatched.",
        created_at: "2026-11-06T09:15:00+05:30",
      },
    ],
  };

  render(<SentBackHistory sentBack={sentBackDetail.sent_back ?? []} />);
  expect(screen.getByText("Previous recommendation")).toBeDefined();
  expect(screen.getByText("Initial approval.")).toBeDefined();
  expect(screen.getByText("Fruit box (x1)")).toBeDefined();
  expect(screen.getByText("Order was already dispatched.")).toBeDefined();
  expect(screen.getByText(/Sent back by Priya Nair/)).toBeDefined();

  render(<ProgressCard detail={sentBackDetail} />);
  expect(screen.getByText("Sent back")).toBeDefined();
  expect(
    screen.getByText("Sent back by Priya Nair: Order was already dispatched."),
  ).toBeDefined();

  render(
    <ActionPanel
      detail={sentBackDetail}
      busy={false}
      onReview={vi.fn()}
      onSendBack={vi.fn()}
    />,
  );
  expect(
    screen.getByText(
      "This request was sent back with a note and is waiting for the next run.",
    ),
  ).toBeDefined();
  cleanup();

  render(
    <ActionPanel
      detail={{
        ...sentBackDetail,
        status: "waiting_for_agent_review",
        recommendation: {
          ...waitingRec,
          policy_citations: ["Security check"],
          decided_by: "Security check",
        },
      }}
      busy={false}
      onReview={vi.fn()}
      onSendBack={vi.fn()}
    />,
  );
  expect(screen.queryByRole("button", { name: "Send back" })).toBeNull();
  cleanup();

  let postSent = false;
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url = urlOf(input);
    if (url === "/api/requests/REQ-7001/send_back") {
      postSent = true;
      return Promise.resolve(
        new Response(JSON.stringify(sentBackDetail), { status: 200 }),
      );
    }
    if (url === "/api/requests") {
      return Promise.resolve(
        new Response(
          JSON.stringify([
            {
              request_id: "REQ-7001",
              order_id: "ORD-2026-10001",
              customer_name: "Asha Sharma",
              type: "cancellation",
              created_at: "2026-11-06T08:35:00+05:30",
              status: postSent ? "sent_back" : "waiting_for_agent_review",
              attempts: 0,
              recommendation: postSent ? null : waitingRec,
              review: null,
            },
          ]),
        ),
      );
    }
    if (url === "/api/agent/instructions") {
      return Promise.resolve(
        new Response(JSON.stringify({ queue: "Q", request: "R" })),
      );
    }
    if (url === "/api/runs/current") {
      return Promise.resolve(new Response("null"));
    }
    return Promise.resolve(
      new Response(
        JSON.stringify(
          postSent
            ? sentBackDetail
            : {
                ...sentBackDetail,
                status: "waiting_for_agent_review",
                recommendation: waitingRec,
                sent_back: [],
                steps: [],
              },
        ),
      ),
    );
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<App />);
  const nameInput = await screen.findByLabelText("Sent back by");
  const noteInput = screen.getByLabelText(/What to look at again/);
  expect((noteInput as HTMLTextAreaElement).maxLength).toBe(1000);
  const sendButton = screen.getByRole("button", { name: "Send back" });
  expect(sendButton).toHaveProperty("disabled", true);

  fireEvent.change(nameInput, { target: { value: "Priya Nair" } });
  fireEvent.change(noteInput, {
    target: { value: "Order was already dispatched." },
  });
  expect(sendButton).toHaveProperty("disabled", false);
  fireEvent.click(sendButton);

  expect(await screen.findByText("Request sent back to agent.")).toBeDefined();
  expect(
    (await screen.findAllByText("Sent back")).length,
  ).toBeGreaterThanOrEqual(2);
  expect(fetchMock).toHaveBeenCalledWith(
    "/api/requests/REQ-7001/send_back",
    expect.objectContaining({
      method: "POST",
      body: JSON.stringify({
        note: "Order was already dispatched.",
        sent_back_by: "Priya Nair",
      }),
    }),
  );
});

it("displays stop_reason on a stopped run and refreshes run state on 502/409", () => {
  const failedRun: Run = {
    run_id: "RUN-STOP99",
    cap: 2,
    status: "failed",
    started_at: "2026-11-10T10:00:00Z",
    finished_at: "2026-11-10T10:05:00Z",
    summary: "The agent stopped answering",
    stop_reason: "The agent stopped answering",
    todos: [],
  };
  render(<RunPanel run={failedRun} />);
  expect(screen.getByText("Stopped")).toBeDefined();
  expect(screen.getByText("REASON STOPPED")).toBeDefined();
  expect(screen.getAllByText("The agent stopped answering")).toHaveLength(2);
});

function buildReviewDetail(
  requestId: string,
  customerName: string,
  customerMessage: string,
  rationale: string,
): RequestDetail {
  return {
    request: {
      request_id: requestId,
      type: "replacement",
      created_at: "2026-11-06T08:35:00+05:30",
      customer_message: customerMessage,
      requested_scope: "whole_order",
      evidence: [],
    },
    customer: { customer_id: "CUS-002", name: customerName },
    order: {
      order_id: "ORD-2026-20001",
      placed_at: "2026-11-05T08:10:00+05:30",
      fulfillment_status: "delivered",
      payment_status: "paid",
      payment_method: "upi",
      delivery_window: null,
      items: [],
      subtotal: 499,
      delivery_fee: 0,
      total: 499,
    },
    status: "waiting_for_agent_review",
    attempts: 1,
    recommendation: {
      outcome: "approve",
      decision: "approve",
      payout: 499,
      rationale,
      policy_citations: ["Replacement Policy: 1. Damaged"],
      next_action: "Confirm replacement.",
      priority_human_review: false,
      section: "Replacement section 1",
    },
    review: null,
    sent_back: [],
    steps: [],
  };
}

function toQueueItem(d: RequestDetail) {
  return {
    request_id: d.request.request_id,
    order_id: d.order.order_id,
    customer_name: d.customer.name,
    type: d.request.type,
    created_at: d.request.created_at,
    status: d.status,
    attempts: d.attempts,
    recommendation: d.recommendation,
    review: null,
  };
}

function stubAppFetch(
  getDetails: () => RequestDetail[],
  onPost?: (url: string) => Response | null,
) {
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = urlOf(input);
      if (init?.method === "POST" && onPost) {
        const res = onPost(url);
        if (res) return Promise.resolve(res);
      }
      const details = getDetails();
      if (url === "/api/requests") {
        return Promise.resolve(
          new Response(JSON.stringify(details.map(toQueueItem))),
        );
      }
      if (url === "/api/agent/instructions") {
        return Promise.resolve(
          new Response(JSON.stringify({ queue: "Q", request: "R" })),
        );
      }
      if (url === "/api/runs/current") {
        return Promise.resolve(new Response("null"));
      }
      const match = details.find(
        (d) => url === `/api/requests/${d.request.request_id}`,
      );
      return Promise.resolve(new Response(JSON.stringify(match ?? details[0])));
    }),
  );
}

it("marks failed action notices as alerts and success notices as status", async () => {
  const detail = buildReviewDetail(
    "REQ-8001",
    "Asha Sharma",
    "Handle cracked in transit.",
    "Clear transit damage.",
  );
  let runAttempt = 0;

  stubAppFetch(
    () => [detail],
    (url) => {
      if (url !== "/api/runs") return null;
      runAttempt += 1;
      if (runAttempt === 1) {
        return new Response(
          JSON.stringify({ detail: "A run is already in progress" }),
          { status: 409 },
        );
      }
      return new Response(JSON.stringify({ run_id: "RUN-1", status: "done" }), {
        status: 200,
      });
    },
  );

  render(<App />);
  const startButton = await screen.findByRole("button", {
    name: "Initiate processing",
  });

  fireEvent.click(startButton);
  const alertBanner = await screen.findByRole("alert");
  expect(alertBanner.textContent).toBe("A run is already in progress");
  expect(alertBanner.className).toContain("notice-error");

  fireEvent.click(startButton);
  const statusBanner = await screen.findByRole("status");
  expect(statusBanner.textContent).toBe(
    "Processing started for up to 1 waiting requests.",
  );
  expect(statusBanner.className).not.toContain("notice-error");
  expect(screen.queryByRole("alert")).toBeNull();
});

it("resets ActionPanel draft inputs when switching requests", async () => {
  const first = buildReviewDetail(
    "REQ-8001",
    "Asha Sharma",
    "Handle cracked in transit.",
    "Approve first.",
  );
  const second = buildReviewDetail(
    "REQ-8002",
    "Rohan Verma",
    "Box arrived wet.",
    "Approve second.",
  );
  stubAppFetch(() => [first, second]);

  render(<App />);
  const reviewedBy = await screen.findByLabelText("Reviewed by");
  fireEvent.change(reviewedBy, { target: { value: "Priya Nair" } });
  fireEvent.change(screen.getByLabelText(/Notes/), {
    target: { value: "Draft note" },
  });
  fireEvent.change(screen.getByLabelText("Sent back by"), {
    target: { value: "Priya Nair" },
  });
  fireEvent.change(screen.getByLabelText(/What to look at again/), {
    target: { value: "Check label" },
  });

  fireEvent.click(screen.getByRole("button", { name: /REQ-8002/ }));
  expect(await screen.findByText("Box arrived wet.")).toBeDefined();

  expect(screen.getByLabelText("Reviewed by")).toHaveProperty("value", "");
  expect(screen.getByLabelText(/Notes/)).toHaveProperty("value", "");
  expect(screen.getByLabelText("Sent back by")).toHaveProperty("value", "");
  expect(screen.getByLabelText(/What to look at again/)).toHaveProperty(
    "value",
    "",
  );
});

it("polls briefly after send_back until a new recommendation arrives or 15s elapses", async () => {
  const initial = buildReviewDetail(
    "REQ-9001",
    "Asha Sharma",
    "Please cancel my order.",
    "Initial approval.",
  );
  const sentBack: RequestDetail = {
    ...initial,
    status: "sent_back",
    recommendation: null,
  };
  const resumed = buildReviewDetail(
    "REQ-9001",
    "Asha Sharma",
    "Please cancel my order.",
    "Updated after dispatch verification.",
  );
  const detailByState = {
    initial,
    sent_back: sentBack,
    resumed,
  };

  let state: "initial" | "sent_back" | "resumed" = "initial";
  let detailPolls = 0;

  stubAppFetch(
    () => {
      detailPolls += 1;
      return [detailByState[state]];
    },
    (url) => {
      if (url !== "/api/requests/REQ-9001/send_back") return null;
      state = "sent_back";
      return new Response(JSON.stringify(sentBack));
    },
  );

  render(<App />);
  const sentBackBy = await screen.findByLabelText("Sent back by");
  fireEvent.change(sentBackBy, { target: { value: "Priya Nair" } });
  fireEvent.change(screen.getByLabelText(/What to look at again/), {
    target: { value: "Check carrier scan." },
  });

  const flush = async (ms = 0) => {
    if (ms > 0) {
      await vi.advanceTimersByTimeAsync(ms);
    }
    for (let i = 0; i < 4; i += 1) {
      await new Promise<void>((resolve) => {
        setImmediate(resolve);
      });
    }
  };

  vi.useFakeTimers({
    toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"],
  });
  try {
    fireEvent.click(screen.getByRole("button", { name: "Send back" }));
    await flush();
    expect(screen.getByText("Request sent back to agent.")).toBeDefined();

    state = "resumed";
    await flush(1600);
    expect(
      screen.getByText("Updated after dispatch verification."),
    ).toBeDefined();

    const pollsAtResume = detailPolls;
    await flush(4500);
    expect(detailPolls).toBe(pollsAtResume);

    fireEvent.change(screen.getByLabelText("Sent back by"), {
      target: { value: "Priya Nair" },
    });
    fireEvent.change(screen.getByLabelText(/What to look at again/), {
      target: { value: "Second check." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send back" }));
    await flush();

    await flush(15_200);
    const pollsAtTimeout = detailPolls;
    await flush(5000);
    expect(detailPolls).toBe(pollsAtTimeout);
  } finally {
    vi.useRealTimers();
  }
});
