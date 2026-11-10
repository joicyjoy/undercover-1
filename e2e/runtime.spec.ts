import { expect, test } from "@playwright/test";

const STAND_IN_URL = "http://127.0.0.1:8003";
const AGENT_HEADERS = { "X-API-Key": "e2e-agent-secret-key-0123456789abcdef" };

test("real backend run gate refuses a concurrent start and displays stop_reason", async ({
  page,
  request,
}) => {
  await request.post(`${STAND_IN_URL}/_mode`, { data: { holdRun: true } });

  await page.goto("/");
  await page.getByLabel("Requests to work").fill("1");
  await page.getByRole("button", { name: "Initiate processing" }).click();

  const runPanel = page.getByLabel("Agent run");
  await expect(runPanel.getByText("Running")).toBeVisible();
  await expect(runPanel.getByText("REQ-7001 cancellation")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Processing…" }),
  ).toBeDisabled();

  const secondStart = await request.post("http://127.0.0.1:8000/api/runs", {
    data: { cap: 1 },
  });
  expect(secondStart.status()).toBe(409);
  const secondBody = (await secondStart.json()) as { detail: string };
  expect(secondBody.detail).toMatch(/Run RUN-.* is already in progress/);

  await request.post(`${STAND_IN_URL}/_finish_held`, {
    data: { status: "failed", summary: "Agent stopped cleanly in E2E." },
  });

  await expect(runPanel.getByText("Stopped", { exact: true })).toBeVisible();
  await expect(runPanel.getByText("REASON STOPPED")).toBeVisible();
  await expect(
    runPanel.getByText("Agent stopped cleanly in E2E.").first(),
  ).toBeVisible();
});

test("support agent sends back a request and sees archived note plus new decision", async ({
  page,
  request,
}) => {
  await request.post("http://127.0.0.1:8000/api/requests/REQ-7002/start", {
    headers: AGENT_HEADERS,
  });
  const calcRes = await request.post(
    "http://127.0.0.1:8000/api/requests/REQ-7002/refund",
    {
      headers: AGENT_HEADERS,
      data: {
        reason: "damaged_or_defective",
        lines: [{ item_id: "ITM-10002-1", quantity: 1 }],
      },
    },
  );
  const calc = (await calcRes.json()) as { refund: number };
  await request.post(
    "http://127.0.0.1:8000/api/requests/REQ-7002/recommendation",
    {
      headers: AGENT_HEADERS,
      data: {
        decision: "approve",
        payout: calc.refund,
        section: "Returns and Refunds Policy: 2. Damaged items",
        reason: "Initial photo shows possible damage.",
        decided_by: "Shoppie Agent",
        outcome: "approve",
        rationale: "Initial photo shows possible damage.",
        policy_citations: ["Returns and Refunds Policy: 2. Damaged items"],
        next_action: "Confirm refund.",
        priority_human_review: false,
      },
    },
  );

  await page.goto("/");
  await page.getByRole("button", { name: /REQ-7002/ }).click();
  await expect(
    page.getByText("Initial photo shows possible damage."),
  ).toBeVisible();

  await page.getByLabel("Sent back by").fill("Priya Nair");
  await page
    .getByLabel(/What to look at again/)
    .fill("Check photo for hairline crack.");
  await page.getByRole("button", { name: "Send back" }).click();

  await expect
    .poll(async () => {
      const res = await request.get(
        "http://127.0.0.1:8000/api/requests/REQ-7002",
      );
      const body = (await res.json()) as { status: string };
      return body.status;
    })
    .toBe("waiting_for_agent_review");

  await page.reload();
  await page.getByRole("button", { name: /REQ-7002/ }).click();

  await expect(page.getByText("SENT-BACK DECISION")).toBeVisible();
  await expect(page.getByText("Previous recommendation")).toBeVisible();
  await expect(
    page.getByText("Check photo for hairline crack.", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText(
      "Re-checked photo evidence; claim is declined under verification.",
    ),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Recommendation", exact: true }),
  ).toBeVisible();
});
