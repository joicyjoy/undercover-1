import type {
  CopilotConfirmResult,
  CopilotConversation,
  CopilotConversationSummary,
  CopilotMe,
  CopilotTurnStartResult,
} from "./types";

function buildHeaders(
  devHandlerId: string | null,
  withBody = false,
): Record<string, string> {
  const headers: Record<string, string> = {};
  if (withBody) headers["Content-Type"] = "application/json";
  if (devHandlerId) headers["X-Shoppie-Handler"] = devHandlerId;
  return headers;
}

async function parseErrorDetail(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: string };
    if (typeof body.detail === "string" && body.detail.trim()) {
      return body.detail;
    }
  } catch {
    // Fall back to HTTP status string
  }
  return `Request failed (${String(response.status)})`;
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

export async function fetchCopilotMe(
  devHandlerId: string | null,
): Promise<CopilotMe | null> {
  const response = await fetch("/api/copilot/me", {
    headers: buildHeaders(devHandlerId),
  });
  if (!response.ok) return null;
  const data = (await response.json()) as unknown;
  if (!isObject(data) || !isObject(data.handler)) return null;
  return data as unknown as CopilotMe;
}

export async function listConversations(
  devHandlerId: string | null,
): Promise<CopilotConversationSummary[]> {
  const response = await fetch("/api/copilot/conversations", {
    headers: buildHeaders(devHandlerId),
  });
  if (!response.ok) return [];
  const data = (await response.json()) as unknown;
  return Array.isArray(data) ? (data as CopilotConversationSummary[]) : [];
}

export async function createConversation(
  devHandlerId: string | null,
  title = "",
): Promise<CopilotConversation> {
  const response = await fetch("/api/copilot/conversations", {
    method: "POST",
    headers: buildHeaders(devHandlerId, true),
    body: JSON.stringify({ title }),
  });
  if (!response.ok) throw new Error(await parseErrorDetail(response));
  return (await response.json()) as CopilotConversation;
}

export async function fetchConversation(
  devHandlerId: string | null,
  conversationId: string,
): Promise<CopilotConversation> {
  const response = await fetch(`/api/copilot/conversations/${conversationId}`, {
    headers: buildHeaders(devHandlerId),
  });
  if (!response.ok) throw new Error(await parseErrorDetail(response));
  return (await response.json()) as CopilotConversation;
}

export async function startCopilotTurn(
  devHandlerId: string | null,
  conversationId: string,
  payload: {
    message: string;
    page_path: string;
    selected_request_id: string | null;
  },
): Promise<CopilotTurnStartResult> {
  const response = await fetch(
    `/api/copilot/conversations/${conversationId}/turns`,
    {
      method: "POST",
      headers: buildHeaders(devHandlerId, true),
      body: JSON.stringify(payload),
    },
  );
  if (!response.ok) throw new Error(await parseErrorDetail(response));
  return (await response.json()) as CopilotTurnStartResult;
}

export async function confirmCopilotTurn(
  devHandlerId: string | null,
  conversationId: string,
  turnId: string,
  payload: { approved: boolean; note?: string },
): Promise<CopilotConfirmResult> {
  const response = await fetch(
    `/api/copilot/conversations/${conversationId}/turns/${turnId}/confirm`,
    {
      method: "POST",
      headers: buildHeaders(devHandlerId, true),
      body: JSON.stringify(payload),
    },
  );
  if (!response.ok) throw new Error(await parseErrorDetail(response));
  return (await response.json()) as CopilotConfirmResult;
}

const COPILOT_STORAGE_KEY = "shoppie.copilot.open";

export function readCopilotOpen(): boolean {
  try {
    return window.localStorage.getItem(COPILOT_STORAGE_KEY) !== "false";
  } catch {
    return true;
  }
}

export function writeCopilotOpen(open: boolean): void {
  try {
    window.localStorage.setItem(COPILOT_STORAGE_KEY, open ? "true" : "false");
  } catch {
    // Ignore storage write errors
  }
}

export async function stopCopilotTurn(
  devHandlerId: string | null,
  conversationId: string,
  turnId: string,
  reason = "Stopped by handler",
): Promise<CopilotConversation> {
  const response = await fetch(
    `/api/copilot/conversations/${conversationId}/turns/${turnId}/stop`,
    {
      method: "POST",
      headers: buildHeaders(devHandlerId, true),
      body: JSON.stringify({ reason }),
    },
  );
  if (!response.ok) throw new Error(await parseErrorDetail(response));
  return (await response.json()) as CopilotConversation;
}
