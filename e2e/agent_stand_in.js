import http from "node:http";

const PORT = 8003;
const BACKEND_URL = "http://127.0.0.1:8000";
const API_KEY = "e2e-agent-secret-key-0123456789abcdef";

const state = {
  holdRun: false,
  heldRunId: null,
};

async function backendRequest(path, method = "GET", body = undefined) {
  const headers = { "X-API-Key": API_KEY };
  const init = { method, headers };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  return fetch(`${BACKEND_URL}${path}`, init);
}

async function handleWorkRun(runId) {
  await backendRequest(`/api/runs/${runId}/heartbeat`, "POST");
  await backendRequest(`/api/runs/${runId}/todos`, "PUT", {
    todos: [
      {
        request_id: "REQ-7001",
        title: "REQ-7001 cancellation",
        status: "in_progress",
      },
    ],
  });
  if (state.holdRun) {
    state.heldRunId = runId;
    return;
  }
  await backendRequest(`/api/runs/${runId}/todos/REQ-7001`, "POST", {
    status: "done",
  });
  await backendRequest(`/api/runs/${runId}/finish`, "POST", {
    status: "finished",
    summary: "Worked REQ-7001 cleanly.",
  });
}

async function handleResumeRequest(requestId) {
  const detailRes = await backendRequest(`/api/requests/${requestId}`);
  if (!detailRes.ok) return;
  const detail = await detailRes.json();
  if (detail.status !== "sent_back") return;
  const take = await backendRequest(
    `/api/requests/${requestId}/take_sent_back`,
    "POST",
    { owner: "resume" },
  );
  if (!take.ok) return;
  await backendRequest(`/api/requests/${requestId}/steps`, "POST", {
    stage: "checking_policy",
    message: "Re-evaluated after support agent note.",
  });
  await backendRequest(`/api/requests/${requestId}/recommendation`, "POST", {
    decision: "decline",
    payout: 0,
    section: "Returns and Refunds Policy: 3. Verification",
    reason: "Re-checked photo evidence; claim is declined under verification.",
    decided_by: "The Shoppie Agent 1",
    outcome: "decline",
    rationale:
      "Re-checked photo evidence; claim is declined under verification.",
    policy_citations: ["Returns and Refunds Policy: 3. Verification"],
    next_action: "Confirm decline with customer.",
    priority_human_review: false,
  });
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

async function handleInvocation(req, res) {
  const payload = await readJson(req);
  if (payload.operation === "health") {
    sendJson(res, 200, { healthy: true });
    return;
  }
  if (req.headers.authorization !== `Bearer ${API_KEY}`) {
    sendJson(res, 401, { error: "Unauthorized" });
    return;
  }
  if (payload.operation === "work_run" && typeof payload.run_id === "string") {
    sendJson(res, 200, { accepted: true });
    setImmediate(() => {
      void handleWorkRun(payload.run_id);
    });
    return;
  }
  if (
    payload.operation === "resume_request" &&
    typeof payload.request_id === "string"
  ) {
    sendJson(res, 200, { accepted: true });
    setImmediate(() => {
      void handleResumeRequest(payload.request_id);
    });
    return;
  }
  sendJson(res, 400, { error: "Unknown operation" });
}

async function routeRequest(req, res) {
  if (req.method === "GET" && req.url === "/ping") {
    sendJson(res, 200, { status: state.heldRunId ? "HealthyBusy" : "Healthy" });
    return;
  }
  if (req.method === "POST" && req.url === "/_mode") {
    const body = await readJson(req);
    state.holdRun = Boolean(body.holdRun);
    if (!state.holdRun) state.heldRunId = null;
    sendJson(res, 200, { holdRun: state.holdRun });
    return;
  }
  if (req.method === "POST" && req.url === "/_finish_held") {
    const body = await readJson(req);
    const runId = state.heldRunId;
    state.heldRunId = null;
    state.holdRun = false;
    if (runId) {
      await backendRequest(`/api/runs/${runId}/finish`, "POST", {
        status: body.status ?? "finished",
        summary: body.summary ?? "Finished held run.",
      });
    }
    sendJson(res, 200, { run_id: runId });
    return;
  }
  if (req.method === "POST" && req.url === "/invocations") {
    await handleInvocation(req, res);
    return;
  }
  sendJson(res, 404, { error: "Not found" });
}

const server = http.createServer((req, res) => {
  void routeRequest(req, res);
});

server.listen(PORT, "127.0.0.1");
