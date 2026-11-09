import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { App } from "./App";
import { CalculationTable, ContextUsed } from "./ContextUsed";
import { ProgressCard } from "./ProgressCard";
import { RecommendationCard } from "./components";
import type { Calculation, Recommendation, RequestDetail } from "./types";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const mockSourceManual = {
  file: "claims_manual.md",
  system: "Claims operations",
  owner: "Head of claims operations",
  reviewed: "2026-09-01",
};

const mockSourceGlossary = {
  file: "glossary.yaml",
  system: "Customer support",
  owner: "Support Lead",
  reviewed: "2026-08-15",
};

it("renders ContextUsed grouped by file and displays found nothing", () => {
  const steps = [
    {
      stage: "lookup",
      message: "Claims manual, section 9: Repeat claims",
      created_at: "2026-11-06T09:00:00+05:30",
      sources: [mockSourceManual],
    },
    {
      stage: "lookup",
      message: "Claims manual, section 14: Records",
      created_at: "2026-11-06T09:01:00+05:30",
      sources: [mockSourceManual],
    },
    {
      stage: "lookup",
      message: 'Term "unattended"',
      created_at: "2026-11-06T09:02:00+05:30",
      sources: [mockSourceGlossary],
    },
    {
      stage: "lookup",
      message: 'Unknown "xyz"',
      created_at: "2026-11-06T09:03:00+05:30",
      sources: [],
    },
  ];

  render(<ContextUsed steps={steps} />);

  expect(screen.getByText("Context used")).toBeDefined();
  expect(screen.getByText("claims_manual.md")).toBeDefined();
  expect(
    screen.getByText(
      "Claims operations, owned by the Head of claims operations, reviewed Sep 1, 2026",
    ),
  ).toBeDefined();
  expect(
    screen.getByText("Claims manual, section 9: Repeat claims"),
  ).toBeDefined();
  expect(screen.getByText("Claims manual, section 14: Records")).toBeDefined();

  expect(screen.getByText("glossary.yaml")).toBeDefined();
  expect(
    screen.getByText(
      "Customer support, owned by Support Lead, reviewed Aug 15, 2026",
    ),
  ).toBeDefined();
  expect(screen.getByText('Term "unattended"')).toBeDefined();

  expect(screen.getByText("Found nothing")).toBeDefined();
  expect(screen.getByText('Unknown "xyz"')).toBeDefined();
});

it("renders CalculationTable with lines and total payout", () => {
  const calculation: Calculation = {
    lines: [
      { step: "Covered item refund (Qty 1)", amount: 349 },
      { step: "Deductible applied", amount: -50 },
    ],
    payout: 299,
  };

  render(<CalculationTable calculation={calculation} />);

  expect(screen.getByText("How the payout was worked out")).toBeDefined();
  expect(screen.getByText("Covered item refund (Qty 1)")).toBeDefined();
  expect(screen.getByText("Deductible applied")).toBeDefined();
  expect(screen.getByText("Payout")).toBeDefined();
});

it("renders RecommendationCard with full decision record", () => {
  const recommendation: Recommendation = {
    outcome: "approve",
    decision: "approve",
    payout: 349,
    rationale: "Covered cancellation within window.",
    reason: "Order not dispatched.",
    section: "Section 2, Cancellations",
    policy_citations: ["Cancellation Policy: 2. Window"],
    next_action: "Confirm cancellation",
    priority_human_review: false,
    decided_by: "Shoppie Agent",
    decided_at: "2026-11-06T09:10:00+05:30",
    manual_version: "2026.2",
    gateway_refusal: "Payout over limit",
    calculation: {
      lines: [{ step: "Line total", amount: 349 }],
      payout: 349,
    },
  };

  render(<RecommendationCard recommendation={recommendation} />);

  expect(screen.getByText("Approve")).toBeDefined();
  expect(screen.getByText("Line total")).toBeDefined();
  expect(screen.getByText("Section 2, Cancellations")).toBeDefined();
  expect(screen.getByText(/Decided by Shoppie Agent at/)).toBeDefined();
  expect(screen.getByText("Claims manual version 2026.2")).toBeDefined();
  expect(screen.getByText("Gateway refused: Payout over limit")).toBeDefined();
});

it("renders every step kind in ProgressCard", () => {
  const detail: RequestDetail = {
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
      fulfillment_status: "confirmed",
      payment_status: "paid",
      payment_method: "upi",
      delivery_window: null,
      items: [],
      subtotal: 100,
      delivery_fee: 0,
      total: 100,
    },
    status: "in_progress",
    attempts: 1,
    recommendation: null,
    review: null,
    steps: [
      {
        stage: "security_check",
        message: "No hidden instructions found.",
        created_at: "2026-11-06T09:00:00+05:30",
      },
      {
        stage: "tool_call",
        message: 'undercover-context___lookup_term(term="unattended")',
        created_at: "2026-11-06T09:01:00+05:30",
      },
      {
        stage: "tool_result",
        message: "undercover-context___lookup_term: { result: true }",
        created_at: "2026-11-06T09:02:00+05:30",
      },
      {
        stage: "lookup",
        message: 'Term "unattended"',
        created_at: "2026-11-06T09:03:00+05:30",
        sources: [mockSourceGlossary],
      },
      {
        stage: "lookup",
        message: "Missing context",
        created_at: "2026-11-06T09:04:00+05:30",
        sources: [],
      },
      {
        stage: "memory_search",
        message: '"cancellations" - none found',
        created_at: "2026-11-06T09:05:00+05:30",
      },
      {
        stage: "memory_save",
        message: "Request REQ-7001 approved",
        created_at: "2026-11-06T09:06:00+05:30",
      },
      {
        stage: "refusal",
        message: "Refused: Section 14 check failed",
        created_at: "2026-11-06T09:07:00+05:30",
      },
      {
        stage: "error",
        message: "Something went wrong",
        created_at: "2026-11-06T09:08:00+05:30",
      },
    ],
  };

  render(<ProgressCard detail={detail} />);

  expect(screen.getByText("Security check")).toBeDefined();
  expect(screen.getByText("No hidden instructions found.")).toBeDefined();

  expect(screen.getByText("Called")).toBeDefined();
  expect(
    screen.getByText('undercover-context___lookup_term(term="unattended")'),
  ).toBeDefined();

  expect(screen.getByText("Result")).toBeDefined();

  expect(screen.getAllByText("Looked up")).toHaveLength(2);
  expect(screen.getByText("glossary.yaml")).toBeDefined();
  expect(screen.getByText("Found nothing")).toBeDefined();

  expect(screen.getByText("Searched memory")).toBeDefined();
  expect(screen.getByText('"cancellations" - none found')).toBeDefined();

  expect(screen.getByText("Saved to memory")).toBeDefined();
  expect(screen.getByText("Request REQ-7001 approved")).toBeDefined();

  expect(screen.getByText("Refused")).toBeDefined();
  expect(screen.getByText("Refused: Section 14 check failed")).toBeDefined();

  expect(screen.getByText("Failed")).toBeDefined();
  expect(screen.getByText("Something went wrong")).toBeDefined();
});

function urlOf(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.href;
  return input.url;
}

it("renders full app page with Context used, decision calculation, and memory steps", async () => {
  const detail: RequestDetail = {
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
      fulfillment_status: "confirmed",
      payment_status: "paid",
      payment_method: "upi",
      delivery_window: null,
      items: [
        {
          name: "Fruit box",
          sku: "FR-1",
          quantity: 1,
          unit_price: 349,
          line_total: 349,
        },
      ],
      subtotal: 349,
      delivery_fee: 0,
      total: 349,
    },
    status: "waiting_for_agent_review",
    attempts: 1,
    recommendation: {
      outcome: "approve",
      decision: "approve",
      payout: 349,
      rationale: "Approved cancellation.",
      reason: "Policy covered.",
      section: "Section 2, Cancellation",
      policy_citations: ["Cancellation Policy: 2. Cancellation"],
      next_action: "Review decision",
      priority_human_review: false,
      calculation: {
        lines: [{ step: "Fruit box refund", amount: 349 }],
        payout: 349,
      },
      decided_by: "Shoppie Agent",
      decided_at: "2026-11-06T09:10:00+05:30",
      manual_version: "2026.2",
    },
    review: null,
    steps: [
      {
        stage: "lookup",
        message: "Claims manual, section 2: Cancellation",
        created_at: "2026-11-06T09:00:00+05:30",
        sources: [mockSourceManual],
      },
      {
        stage: "memory_search",
        message: "No previous claims found",
        created_at: "2026-11-06T09:01:00+05:30",
      },
    ],
  };

  const queue = [
    {
      request_id: "REQ-7001",
      order_id: "ORD-2026-10001",
      customer_name: "Asha Sharma",
      type: "cancellation",
      created_at: "2026-11-06T08:35:00+05:30",
      status: "waiting_for_agent_review" as const,
      attempts: 1,
      recommendation: detail.recommendation,
      review: null,
    },
  ];

  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const url = urlOf(input);
      let body: unknown = detail;
      if (url === "/api/requests") body = queue;
      if (url === "/api/agent/instructions") {
        body = { queue: "Instructions queue", request: "Instructions request" };
      }
      if (url === "/api/runs/current") body = null;
      return Promise.resolve(
        new Response(JSON.stringify(body), { status: 200 }),
      );
    }),
  );

  render(<App />);

  expect(await screen.findByText("Context used")).toBeDefined();
  expect(
    (await screen.findAllByText("claims_manual.md")).length,
  ).toBeGreaterThanOrEqual(1);
  expect(await screen.findByText("Fruit box refund")).toBeDefined();
  expect(await screen.findByText("Searched memory")).toBeDefined();
  expect(await screen.findByText("No previous claims found")).toBeDefined();
});
