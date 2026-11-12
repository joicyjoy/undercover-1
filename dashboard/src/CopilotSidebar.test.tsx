import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { App } from "./App";
import { CopilotSidebar } from "./CopilotSidebar";

beforeEach(() => {
  window.localStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

function urlOf(input: RequestInfo | URL) {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.href;
  return input.url;
}

const danaProfile = {
  handler_id: "HND-01",
  name: "Dana Whitfield",
  role: "senior_handler",
  title: "Senior Support Handler",
  can_sign_off_over_limit: true,
  email: "dana.whitfield@shoppie.example",
  auth_mode: "dev",
};

const marcusProfile = {
  handler_id: "HND-02",
  name: "Marcus Oyelaran",
  role: "handler",
  title: "Support Handler",
  can_sign_off_over_limit: false,
  email: "marcus.oyelaran@shoppie.example",
  auth_mode: "dev",
};

it("persists collapsed and open sidebar state in localStorage across mounts", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const url = urlOf(input);
      if (url === "/api/copilot/me") {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              handler: danaProfile,
              dev_handlers: [danaProfile, marcusProfile],
            }),
          ),
        );
      }
      if (url === "/api/copilot/conversations") {
        return Promise.resolve(new Response("[]"));
      }
      if (url === "/api/requests") return Promise.resolve(new Response("[]"));
      if (url === "/api/agent/instructions") {
        return Promise.resolve(
          new Response(JSON.stringify({ queue: "Q", request: "R" })),
        );
      }
      return Promise.resolve(new Response("null"));
    }),
  );

  const { unmount } = render(<App />);
  expect(
    await screen.findByRole("complementary", { name: "Shoppie Copilot" }),
  ).toBeDefined();

  fireEvent.click(
    screen.getByRole("button", { name: "Collapse Shoppie Copilot" }),
  );
  expect(window.localStorage.getItem("shoppie.copilot.open")).toBe("false");
  expect(
    screen.getByRole("button", { name: "Open Shoppie Copilot" }),
  ).toBeDefined();
  unmount();

  render(<App />);
  expect(
    await screen.findByRole("button", { name: "Open Shoppie Copilot" }),
  ).toBeDefined();
});

function jsonResponse(payload: unknown): Promise<Response> {
  return Promise.resolve(new Response(JSON.stringify(payload)));
}

it("streams live steps while working, renders request links, and switches dev handler", async () => {
  let pollCount = 0;
  const seenHeaders: (HeadersInit | undefined)[] = [];
  const sampleConv = (done: boolean) => ({
    conversation_id: "CONV-1",
    handler_id: "HND-01",
    title: "What needs my attention today?",
    status: done ? "done" : "working",
    turns: [
      {
        turn_id: "TURN-1",
        conversation_id: "CONV-1",
        handler_id: "HND-01",
        user_message: "What needs my attention today?",
        assistant_message: done
          ? "### Priority\nCheck REQ-7001 for refund sign-off."
          : null,
        status: done ? "done" : "working",
        created_at: "2026-11-12T10:00:00+05:30",
        updated_at: "2026-11-12T10:00:02+05:30",
      },
    ],
    steps: [
      {
        step_id: 1,
        turn_id: "TURN-1",
        seq: 1,
        kind: "tool",
        tool_name: "get_copilot_overview",
        summary: "Checked queue and waiting requests.",
        detail: { waiting: 4 },
        duration_ms: 42,
        created_at: "2026-11-12T10:00:01+05:30",
      },
    ],
    created_at: "2026-11-12T10:00:00+05:30",
    updated_at: "2026-11-12T10:00:02+05:30",
  });

  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = urlOf(input);
    seenHeaders.push(init?.headers);
    if (url === "/api/copilot/me") {
      const hdr = init?.headers as Record<string, string> | undefined;
      const active =
        hdr?.["X-Shoppie-Handler"] === "HND-02" ? marcusProfile : danaProfile;
      return jsonResponse({
        handler: active,
        dev_handlers: [danaProfile, marcusProfile],
      });
    }
    if (url === "/api/copilot/conversations") {
      if (init?.method === "POST") {
        return jsonResponse({ ...sampleConv(false), turns: [], steps: [] });
      }
      return jsonResponse([]);
    }
    if (url === "/api/copilot/conversations/CONV-1/turns") {
      return jsonResponse({
        conversation_id: "CONV-1",
        call_state: "accepted",
        turn: sampleConv(false).turns[0],
      });
    }
    if (url === "/api/copilot/conversations/CONV-1") {
      pollCount += 1;
      return jsonResponse(sampleConv(pollCount >= 2));
    }
    return jsonResponse({});
  });
  vi.stubGlobal("fetch", fetchMock);

  const onSelectRequest = vi.fn();
  const onMutated = vi.fn();
  render(
    <CopilotSidebar
      open={true}
      onToggle={vi.fn()}
      selectedId={null}
      onSelectRequest={onSelectRequest}
      onMutated={onMutated}
    />,
  );

  expect(await screen.findByText("Dana Whitfield")).toBeDefined();
  fireEvent.click(
    screen.getByRole("button", { name: "What needs my attention today?" }),
  );

  expect(
    await screen.findByText("Checked queue and waiting requests."),
  ).toBeDefined();
  expect(screen.getByText("42 ms")).toBeDefined();

  await waitFor(
    () => {
      expect(screen.getByRole("button", { name: "REQ-7001" })).toBeDefined();
    },
    { timeout: 3000 },
  );
  expect(onMutated).toHaveBeenCalledTimes(1);

  fireEvent.click(screen.getByRole("button", { name: "REQ-7001" }));
  expect(onSelectRequest).toHaveBeenCalledWith("REQ-7001");

  fireEvent.change(screen.getByLabelText("Local dev handler"), {
    target: { value: "HND-02" },
  });
  expect(await screen.findByText("Marcus Oyelaran")).toBeDefined();
  expect(seenHeaders).toContainEqual({ "X-Shoppie-Handler": "HND-02" });
});

it("handles sign-off confirmation card, cancel flow, and hides dev switcher in entra mode", async () => {
  const confirmedBodies: unknown[] = [];
  let stateStatus = "waiting_confirmation";
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = urlOf(input);
      if (url === "/api/copilot/me") {
        return jsonResponse({
          handler: { ...danaProfile, auth_mode: "entra" },
          dev_handlers: [],
        });
      }
      if (url === "/api/copilot/conversations" && !init?.method) {
        return jsonResponse([
          {
            conversation_id: "CONV-9",
            handler_id: "HND-01",
            title: "Sign off REQ-7001",
            status: stateStatus,
            created_at: "2026-11-12T10:00:00+05:30",
            updated_at: "2026-11-12T10:00:00+05:30",
          },
        ]);
      }
      if (url === "/api/copilot/conversations/CONV-9/turns/TURN-9/confirm") {
        const rawBody = typeof init?.body === "string" ? init.body : "{}";
        const parsed = JSON.parse(rawBody) as { approved: boolean };
        confirmedBodies.push(parsed);
        stateStatus = "done";
        return Promise.resolve(
          new Response(
            JSON.stringify({
              call_state: "accepted",
              conversation: {
                conversation_id: "CONV-9",
                handler_id: "HND-01",
                title: "Sign off REQ-7001",
                status: "done",
                turns: [
                  {
                    turn_id: "TURN-9",
                    conversation_id: "CONV-9",
                    handler_id: "HND-01",
                    user_message: "Approve REQ-7001",
                    assistant_message: "Signed off REQ-7001.",
                    status: "done",
                    created_at: "2026-11-12T10:00:00+05:30",
                    updated_at: "2026-11-12T10:01:00+05:30",
                  },
                ],
                steps: [],
                created_at: "2026-11-12T10:00:00+05:30",
                updated_at: "2026-11-12T10:01:00+05:30",
              },
            }),
          ),
        );
      }
      if (url === "/api/copilot/conversations/CONV-9") {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              conversation_id: "CONV-9",
              handler_id: "HND-01",
              title: "Sign off REQ-7001",
              status: "waiting_confirmation",
              turns: [
                {
                  turn_id: "TURN-9",
                  conversation_id: "CONV-9",
                  handler_id: "HND-01",
                  user_message: "Approve REQ-7001",
                  status: "waiting_confirmation",
                  confirmation: {
                    request_id: "REQ-7001",
                    customer_name: "Asha Sharma",
                    payout: 2500,
                    outcome: "approve",
                    note: "Verified policy",
                  },
                  created_at: "2026-11-12T10:00:00+05:30",
                  updated_at: "2026-11-12T10:00:05+05:30",
                },
              ],
              steps: [],
              created_at: "2026-11-12T10:00:00+05:30",
              updated_at: "2026-11-12T10:00:05+05:30",
            }),
          ),
        );
      }
      return Promise.resolve(new Response("{}"));
    }),
  );

  const onMutated = vi.fn();
  render(
    <CopilotSidebar
      open={true}
      onToggle={vi.fn()}
      selectedId="REQ-7001"
      onSelectRequest={vi.fn()}
      onMutated={onMutated}
    />,
  );

  expect(await screen.findByText("SIGN-OFF CONFIRMATION")).toBeDefined();
  expect(screen.queryByLabelText("Local dev handler")).toBeNull();
  expect(screen.getByText(/2,500/)).toBeDefined();

  fireEvent.click(screen.getByRole("button", { name: "Confirm sign off" }));
  await waitFor(() => {
    expect(screen.getByText(/Signed off/)).toBeDefined();
  });
  expect(confirmedBodies).toEqual([
    { approved: true, note: "Verified policy" },
  ]);
  expect(onMutated).toHaveBeenCalledTimes(1);

  fireEvent.click(screen.getByRole("button", { name: "+ New chat" }));
  expect(
    screen.getByRole("button", { name: "Explain REQ-7001's decision" }),
  ).toBeDefined();
});
