import http from "node:http";

const PORT = 8004;
const DEFAULT_BACKEND_URL = "http://127.0.0.1:8000";
const API_KEY = "e2e-agent-secret-key-0123456789abcdef";

async function handlerRequest(
  baseUrl,
  handlerToken,
  path,
  method = "GET",
  body = undefined,
) {
  const headers = { Authorization: `Bearer ${handlerToken}` };
  const init = { method, headers };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  return fetch(`${baseUrl}${path}`, init);
}

async function fetchMeAndSettings(baseUrl, handlerToken) {
  const [meRes, settingsRes] = await Promise.all([
    handlerRequest(baseUrl, handlerToken, "/api/copilot/me"),
    handlerRequest(baseUrl, handlerToken, "/api/settings"),
  ]);
  const me = meRes.ok ? await meRes.json() : { handler: {} };
  const settings = settingsRes.ok
    ? await settingsRes.json()
    : { approval_limit: 10000 };
  return { handler: me.handler ?? {}, settings };
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function extractRequestId(text, fallback) {
  const match = /REQ-\d+/.exec(text);
  return match ? match[0] : fallback;
}

async function postStepAndComplete(ctx, stepPayload, completePayload) {
  await handlerRequest(
    ctx.baseUrl,
    ctx.token,
    `/api/copilot/conversations/${ctx.convId}/steps`,
    "POST",
    { turn_id: ctx.turnId, ...stepPayload },
  );
  await handlerRequest(
    ctx.baseUrl,
    ctx.token,
    `/api/copilot/conversations/${ctx.convId}/turns/${ctx.turnId}/complete`,
    "POST",
    completePayload,
  );
}

async function handleSendBackTurn(ctx, handler) {
  const reqId = extractRequestId(ctx.text, "REQ-7003");
  await handlerRequest(
    ctx.baseUrl,
    ctx.token,
    `/api/requests/${reqId}/send_back`,
    "POST",
    {
      note: "Please verify packaging photo.",
      sent_back_by: handler.name || "Copilot Handler",
    },
  );
  await postStepAndComplete(
    ctx,
    {
      kind: "tool",
      tool_name: "send_back_request",
      summary: `Sent ${reqId} back for agent re-evaluation.`,
      detail: { request_id: reqId, note: "Please verify packaging photo." },
      duration_ms: 48,
    },
    {
      status: "done",
      assistant_message: `Sent ${reqId} back to the agent cleanly.`,
    },
  );
}

async function handleSignOffTurn(ctx, handler, settings) {
  const reqId = extractRequestId(ctx.text, "REQ-7006");
  const reqRes = await handlerRequest(
    ctx.baseUrl,
    ctx.token,
    `/api/requests/${reqId}`,
  );
  const detail = reqRes.ok ? await reqRes.json() : {};
  const rec = detail.recommendation ?? {};
  const requestedOverLimit = /12500/.test(ctx.text) ? 12500 : null;
  const payout =
    requestedOverLimit ?? (typeof rec.payout === "number" ? rec.payout : 2500);
  const customerName = detail.customer?.name || "Customer";
  const limit = Number(settings.approval_limit ?? 10000);

  if (!handler.can_sign_off_over_limit && payout > limit) {
    await postStepAndComplete(
      ctx,
      {
        kind: "confirmation",
        tool_name: "prepare_sign_off",
        summary: `Sign-off blocked by ₹${limit.toLocaleString("en-IN")} role limit.`,
        detail: { request_id: reqId, payout, approval_limit: limit },
        duration_ms: 15,
      },
      {
        status: "done",
        assistant_message: `${handler.name} cannot sign off payouts above ₹${limit.toLocaleString("en-IN")}.00 for ${reqId}.`,
      },
    );
    return;
  }

  await postStepAndComplete(
    ctx,
    {
      kind: "confirmation",
      tool_name: "prepare_sign_off",
      summary: `Prepared sign-off confirmation for ${reqId}.`,
      detail: { request_id: reqId, customer_name: customerName, payout },
      duration_ms: 28,
    },
    {
      status: "waiting_confirmation",
      confirmation: {
        request_id: reqId,
        customer_name: customerName,
        payout,
        outcome: "approve",
        note: `Confirmed approve by ${handler.name || "Handler"}`,
      },
    },
  );
}

async function runCopilotTurn(payload) {
  const ctx = {
    baseUrl: payload.backend_url || DEFAULT_BACKEND_URL,
    token: payload.handler_token,
    convId: payload.conversation_id,
    turnId: payload.turn_id,
    text: String(payload.message || ""),
  };
  const lower = ctx.text.toLowerCase();
  const { handler, settings } = await fetchMeAndSettings(
    ctx.baseUrl,
    ctx.token,
  );
  await delay(150);

  if (lower.includes("send back")) {
    await handleSendBackTurn(ctx, handler);
    return;
  }
  if (lower.includes("sign off") || lower.includes("approve")) {
    await handleSignOffTurn(ctx, handler, settings);
    return;
  }
  await postStepAndComplete(
    ctx,
    {
      kind: "tool",
      tool_name: "get_copilot_overview",
      summary: "Checked queue counts and pilot targets.",
      detail: { waiting: 6 },
      duration_ms: 35,
    },
    {
      status: "done",
      assistant_message:
        "### Today's queue\nReview REQ-7006 in the request panel.",
    },
  );
}

async function runCopilotResume(payload) {
  const baseUrl = payload.backend_url || DEFAULT_BACKEND_URL;
  const token = payload.handler_token;
  const convId = payload.conversation_id;
  const turnId = payload.turn_id;
  const { handler } = await fetchMeAndSettings(baseUrl, token);

  const convRes = await handlerRequest(
    baseUrl,
    token,
    `/api/copilot/conversations/${convId}`,
  );
  const conv = convRes.ok ? await convRes.json() : { turns: [] };
  const turn = (conv.turns || []).find((item) => item.turn_id === turnId) || {};
  const conf = turn.confirmation || {};
  const reqId = conf.request_id || "REQ-7006";
  const ctx = { baseUrl, token, convId, turnId };

  await delay(100);

  if (payload.approved) {
    await handlerRequest(
      baseUrl,
      token,
      `/api/requests/${reqId}/review`,
      "POST",
      {
        outcome: conf.outcome || "approve",
        notes: payload.note || conf.note || "Confirmed via Copilot",
        reviewed_by: handler.name || "Dana Whitfield",
      },
    );
    await postStepAndComplete(
      ctx,
      {
        kind: "confirmation",
        tool_name: "review_request",
        summary: `Signed off ${reqId}.`,
        detail: { request_id: reqId, approved: true },
        duration_ms: 30,
      },
      {
        status: "done",
        assistant_message: `Signed off ${reqId} cleanly.`,
      },
    );
    return;
  }

  await postStepAndComplete(
    ctx,
    {
      kind: "confirmation",
      tool_name: "review_request",
      summary: `Handler cancelled sign-off for ${reqId}.`,
      detail: { request_id: reqId, approved: false },
      duration_ms: 0,
    },
    {
      status: "done",
      assistant_message: `Cancelled sign-off for ${reqId}.`,
    },
  );
}

function readJson(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on("data", (chunk) => chunks.push(chunk));
    req.on("end", () => {
      try {
        const text = Buffer.concat(chunks).toString("utf8");
        resolve(text ? JSON.parse(text) : {});
      } catch (err) {
        reject(err);
      }
    });
    req.on("error", reject);
  });
}

function sendJson(res, status, body) {
  res.writeHead(status, { "Content-Type": "application/json" });
  res.end(JSON.stringify(body));
}

async function routeRequest(req, res) {
  if (req.method === "GET" && req.url === "/ping") {
    sendJson(res, 200, { status: "Healthy" });
    return;
  }
  if (req.method !== "POST" || req.url !== "/invocations") {
    sendJson(res, 404, { error: "Not found" });
    return;
  }
  const payload = await readJson(req);
  if (payload.action === "health" || payload.operation === "health") {
    sendJson(res, 200, { healthy: true });
    return;
  }
  if (req.headers.authorization !== `Bearer ${API_KEY}`) {
    sendJson(res, 401, { error: "Unauthorized" });
    return;
  }
  if (payload.action === "copilot_turn") {
    sendJson(res, 200, { status: "accepted" });
    setImmediate(() => {
      void runCopilotTurn(payload);
    });
    return;
  }
  if (payload.action === "copilot_resume") {
    sendJson(res, 200, { status: "accepted" });
    setImmediate(() => {
      void runCopilotResume(payload);
    });
    return;
  }
  sendJson(res, 400, { error: "Unknown copilot action" });
}

const server = http.createServer((req, res) => {
  void routeRequest(req, res);
});

server.listen(PORT, "127.0.0.1");
