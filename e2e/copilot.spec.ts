import { expect, test } from "@playwright/test";
import type { APIRequestContext } from "@playwright/test";

const BACKEND_URL = "http://127.0.0.1:8000";
const AGENT_HEADERS = { "X-API-Key": "e2e-agent-secret-key-0123456789abcdef" };

async function seedRecommendation(
  request: APIRequestContext,
  requestId: string,
): Promise<number> {
  const startRes = await request.post(
    `${BACKEND_URL}/api/requests/${requestId}/start`,
    { headers: AGENT_HEADERS },
  );
  expect(startRes.ok()).toBeTruthy();
  const detailRes = await request.get(
    `${BACKEND_URL}/api/requests/${requestId}`,
  );
  expect(detailRes.ok()).toBeTruthy();
  const detail = (await detailRes.json()) as {
    request: { type: string };
    order: { items: { item_id: string }[] };
  };
  const firstItem = detail.order.items[0]?.item_id;
  expect(firstItem).toBeTruthy();
  const reason =
    detail.request.type === "cancellation"
      ? "cancellation"
      : "damaged_or_defective";
  const calcRes = await request.post(
    `${BACKEND_URL}/api/requests/${requestId}/refund`,
    {
      headers: AGENT_HEADERS,
      data: {
        reason,
        lines: [{ item_id: firstItem, quantity: 1 }],
      },
    },
  );
  expect(calcRes.ok()).toBeTruthy();
  const calc = (await calcRes.json()) as { refund: number };
  const recRes = await request.post(
    `${BACKEND_URL}/api/requests/${requestId}/recommendation`,
    {
      headers: AGENT_HEADERS,
      data: {
        decision: "approve",
        payout: calc.refund,
        section: "Cancellation Policy: 2. Refunds",
        reason: "Evaluated in E2E setup.",
        decided_by: "Shoppie Agent",
        outcome: "approve",
        rationale: "Evaluated in E2E setup.",
        policy_citations: ["Cancellation Policy: 2. Refunds"],
        next_action: "Confirm refund.",
        priority_human_review: false,
      },
    },
  );
  expect(recRes.ok()).toBeTruthy();
  return calc.refund;
}

test("copilot sidebar collapses cleanly, persists preference, and navigates to requests from answers", async ({
  page,
}) => {
  await page.goto("/");

  const sidebar = page.getByLabel("Shoppie Copilot", { exact: true });
  await expect(sidebar).toBeVisible();
  await expect(
    sidebar.getByText("Dana Whitfield", { exact: true }),
  ).toBeVisible();

  await sidebar
    .getByRole("button", { name: "Collapse Shoppie Copilot" })
    .click();
  await expect(sidebar).toBeHidden();
  await expect(
    page.getByRole("button", { name: "Open Shoppie Copilot" }),
  ).toBeVisible();

  await page.reload();
  await expect(
    page.getByRole("button", { name: "Open Shoppie Copilot" }),
  ).toBeVisible();

  await page.getByRole("button", { name: "Open Shoppie Copilot" }).click();
  await expect(sidebar).toBeVisible();

  await sidebar
    .getByRole("button", { name: "What needs my attention today?" })
    .click();

  await expect(
    sidebar.getByText("Checked queue counts and pilot targets."),
  ).toBeVisible();
  const reqLink = sidebar.getByRole("button", { name: "REQ-7006" });
  await expect(reqLink).toBeVisible();

  await reqLink.click();
  await expect(
    page.getByLabel("Request details").getByText("REQ-7006"),
  ).toBeVisible();
});

test("copilot pauses for sign-off confirmation, handles cancel and approve, send-back, and Marcus permission guard", async ({
  page,
  request,
}) => {
  await seedRecommendation(request, "REQ-7003");
  await seedRecommendation(request, "REQ-7006");
  const req5Payout = await seedRecommendation(request, "REQ-7005");
  expect(req5Payout).toBeGreaterThan(0);

  await page.goto("/");
  const sidebar = page.getByLabel("Shoppie Copilot", { exact: true });
  await sidebar.getByRole("button", { name: "+ New chat" }).click();

  const input = sidebar.getByLabel("Message Shoppie Copilot");

  // 1. Send back executes directly without confirmation card and refreshes request status
  await input.fill("Please send back REQ-7003 to check packaging photo");
  await sidebar.getByRole("button", { name: "Send" }).click();
  await expect(
    sidebar.getByText("Sent REQ-7003 back to the agent cleanly."),
  ).toBeVisible();
  await expect
    .poll(async () => {
      const res = await request.get(`${BACKEND_URL}/api/requests/REQ-7003`);
      const body = (await res.json()) as { status: string };
      return body.status;
    })
    .toBe("waiting_for_agent_review");
  await page
    .getByLabel("Support request queue")
    .getByRole("button", { name: /REQ-7003/ })
    .click();
  await expect(
    page.getByText("Please verify packaging photo.", { exact: true }),
  ).toBeVisible();

  // 2. Sign-off pauses with confirmation card -> Cancel leaves request waiting_for_agent_review
  await input.fill("Sign off REQ-7006");
  await sidebar.getByRole("button", { name: "Send" }).click();
  const confirmCard = sidebar.getByRole("region", {
    name: "Sign-off confirmation",
  });
  await expect(confirmCard).toBeVisible();
  await expect(confirmCard.getByText("REQ-7006")).toBeVisible();

  await confirmCard.getByRole("button", { name: "Cancel" }).click();
  await expect(
    sidebar.getByText("Cancelled sign-off for REQ-7006.", { exact: true }),
  ).toBeVisible();

  // 3. Sign off REQ-7006 -> Confirm sign off marks request decided
  await input.fill("Approve and sign off REQ-7006");
  await sidebar.getByRole("button", { name: "Send" }).click();
  await expect(confirmCard).toBeVisible();
  await confirmCard.getByRole("button", { name: "Confirm sign off" }).click();
  await expect(
    sidebar.getByText("Signed off REQ-7006 cleanly.", { exact: true }),
  ).toBeVisible();
  await page
    .getByLabel("Support request queue")
    .getByRole("button", { name: /REQ-7006/ })
    .click();
  await expect(page.getByText("SUPPORT REVIEW RECORDED")).toBeVisible();

  // 4. Switch local handler to Marcus Oyelaran and confirm over-limit refusal without ConfirmationCard
  await sidebar.getByLabel("Local dev handler").selectOption("HND-02");
  await expect(
    sidebar.getByText("Marcus Oyelaran", { exact: true }),
  ).toBeVisible();
  await input.fill("Sign off REQ-7005 for ₹12500");
  await sidebar.getByRole("button", { name: "Send" }).click();
  await expect(
    sidebar.getByText(/Marcus Oyelaran cannot sign off payouts above ₹10,000/),
  ).toBeVisible();
  await expect(confirmCard).toBeHidden();
});
