export type QueueStatus =
  | "waiting"
  | "in_progress"
  | "waiting_for_agent_review"
  | "sent_back"
  | "decided";
export type Outcome = "approve" | "decline" | "request_information";

export interface QueueItem {
  request_id: string;
  order_id: string;
  customer_name: string;
  type: string;
  created_at: string;
  status: QueueStatus;
  attempts: number;
  recommendation: Recommendation | null;
  review: Review | null;
}

export interface Review {
  outcome: Outcome;
  notes: string;
  reviewed_by: string;
  reviewed_at: string;
}

export interface Source {
  file: string;
  system: string;
  owner: string;
  reviewed: string;
}

export interface CalculationStep {
  step: string;
  amount: number;
}

export interface Calculation {
  lines: CalculationStep[];
  payout: number;
}

export interface Recommendation {
  outcome: Outcome;
  rationale: string;
  policy_citations: string[];
  next_action: string;
  priority_human_review: boolean;
  decision?: string;
  payout?: number;
  calculation?: Calculation | null;
  section?: string;
  reason?: string;
  decided_by?: string;
  decided_at?: string;
  manual_version?: string;
  gateway_refusal?: string | null;
}

export interface RequestStep {
  stage: string;
  message: string;
  created_at: string;
  sources?: Source[];
}

export interface SupportRequest {
  request_id: string;
  type: string;
  created_at: string;
  customer_message: string;
  requested_scope: string;
  evidence: { type: string; reference: string; description: string }[];
}

export interface SentBackDecision {
  decision: Recommendation;
  note: string;
  sent_back_by: string;
  sent_back_at: string;
}

export interface RequestDetail {
  request: SupportRequest;
  customer: { customer_id: string; name: string };
  order: {
    order_id: string;
    placed_at: string;
    fulfillment_status: string;
    payment_status: string;
    payment_method: string;
    delivery_window: { start: string; end: string } | null;
    delivered_at?: string;
    items: {
      name: string;
      sku: string;
      quantity: number;
      unit_price: number;
      line_total: number;
    }[];
    subtotal: number;
    delivery_fee: number;
    total: number;
  };
  status: QueueStatus;
  attempts: number;
  recommendation: Recommendation | null;
  review: Review | null;
  sent_back?: SentBackDecision[];
  steps: RequestStep[];
}

export type TodoStatus = "to_do" | "in_progress" | "done" | "left_for_support";

export interface Todo {
  request_id: string;
  title: string;
  status: TodoStatus;
}

export interface Run {
  run_id: string;
  cap: number;
  status: "running" | "finished" | "failed";
  started_at: string;
  finished_at: string | null;
  summary: string | null;
  last_heartbeat_at?: string | null;
  stop_reason?: string | null;
  todos: Todo[];
}

export interface Instructions {
  queue: string;
  request: string;
}

export type HandlerRole = "senior_handler" | "handler";
export type AuthMode = "dev" | "entra";

export interface HandlerProfile {
  handler_id: string;
  name: string;
  role: HandlerRole;
  title: string;
  can_sign_off_over_limit: boolean;
  email: string;
  auth_mode: AuthMode;
}

export interface CopilotMe {
  handler: HandlerProfile;
  dev_handlers: HandlerProfile[];
}

export type CopilotTurnStatus =
  "working" | "waiting_confirmation" | "done" | "failed" | "stopped";

export interface CopilotStep {
  step_id?: number;
  turn_id: string;
  seq: number;
  kind: string;
  summary: string;
  tool_name?: string | null;
  detail?: Record<string, unknown> | null;
  duration_ms?: number | null;
  created_at: string;
}

export interface CopilotConfirmation {
  request_id: string;
  customer_name?: string;
  payout?: number;
  recommendation?: string;
  outcome?: string;
  note?: string;
}

export interface CopilotTurn {
  turn_id: string;
  conversation_id: string;
  handler_id: string;
  user_message: string;
  assistant_message?: string | null;
  status: CopilotTurnStatus;
  confirmation?: CopilotConfirmation | null;
  error?: string | null;
  created_at: string;
  updated_at: string;
  steps?: CopilotStep[];
}

export interface CopilotConversationSummary {
  conversation_id: string;
  handler_id: string;
  title: string;
  status: "idle" | CopilotTurnStatus;
  active_turn_id?: string | null;
  turn_started_at?: string | null;
  created_at: string;
  updated_at: string;
}

export interface CopilotConversation extends CopilotConversationSummary {
  turns: CopilotTurn[];
  steps: CopilotStep[];
}

export interface CopilotTurnStartResult {
  conversation_id: string;
  turn: CopilotTurn;
  call_state: string;
}

export interface CopilotConfirmResult {
  conversation: CopilotConversation;
  call_state: string;
}
