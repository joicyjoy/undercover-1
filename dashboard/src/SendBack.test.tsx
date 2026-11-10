import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { App } from "./App";
import { ProgressCard } from "./ProgressCard";
import { ActionPanel, SentBackHistory } from "./components";
import type { Recommendation, RequestDetail } from "./types";

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
