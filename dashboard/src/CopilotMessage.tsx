import { Fragment, useState } from "react";
import type { ReactNode } from "react";
import { formatMoney, humanize } from "./format";
import type { CopilotConfirmation, CopilotStep, CopilotTurn } from "./types";

const TOKEN_SPLIT_RE = /(\*\*[^*]+\*\*|`[^`]+`|REQ-\d+)/g;
const REQUEST_ID_RE = /^REQ-\d+$/;

export function CopilotMark({ size = 18 }: Readonly<{ size?: number }>) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden="true"
    >
      <path
        d="M12 2.5L14.5 9.5L21.5 12L14.5 14.5L12 21.5L9.5 14.5L2.5 12L9.5 9.5L12 2.5Z"
        stroke="currentColor"
        strokeWidth="1.9"
        strokeLinejoin="round"
      />
      <circle cx="18.5" cy="5.5" r="1.5" fill="currentColor" />
    </svg>
  );
}

function formatDuration(ms?: number | null): string | null {
  if (ms === null || ms === undefined || ms < 0) return null;
  if (ms < 1000) return `${String(ms)} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

function StepIcon({
  toolName,
  kind,
}: Readonly<{ toolName?: string | null; kind: string }>) {
  const name = (toolName ?? kind).toLowerCase();
  let glyph = "◈";
  if (
    name.includes("confirm") ||
    name.includes("sign_off") ||
    name.includes("review")
  ) {
    glyph = "✓";
  } else if (name.includes("run") || name.includes("criteria")) {
    glyph = "▸";
  } else if (name.includes("send_back")) {
    glyph = "↩";
  } else if (name.includes("policy") || name.includes("context")) {
    glyph = "§";
  } else if (name.includes("memory")) {
    glyph = "✦";
  } else if (name.includes("refund") || name.includes("calc")) {
    glyph = "₹";
  }
  return (
    <span className="copilot-step-icon" aria-hidden="true">
      {glyph}
    </span>
  );
}

function renderInline(
  text: string,
  onSelectRequest: (requestId: string) => void,
): ReactNode[] {
  const parts = text.split(TOKEN_SPLIT_RE);
  return parts.map((part, idx) => {
    const key = `${String(idx)}-${part}`;
    if (REQUEST_ID_RE.test(part)) {
      return (
        <button
          key={key}
          type="button"
          className="copilot-request-link"
          onClick={() => {
            onSelectRequest(part);
          }}
        >
          {part}
        </button>
      );
    }
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) {
      return <strong key={key}>{part.slice(2, -2)}</strong>;
    }
    if (part.startsWith("`") && part.endsWith("`") && part.length > 2) {
      return (
        <code key={key} className="copilot-inline-code">
          {part.slice(1, -1)}
        </code>
      );
    }
    return <Fragment key={key}>{part}</Fragment>;
  });
}

function parseHeading(line: string): string | null {
  for (const prefix of ["### ", "## ", "# "]) {
    if (line.startsWith(prefix) && line.length > prefix.length) {
      return line.slice(prefix.length).trim();
    }
  }
  return null;
}

function AssistantMarkdown({
  text,
  onSelectRequest,
}: Readonly<{
  text: string;
  onSelectRequest: (requestId: string) => void;
}>) {
  const blocks = text.split(/\n{2,}/).filter((b) => b.trim().length > 0);
  return (
    <div className="copilot-markdown">
      {blocks.map((block, idx) => {
        const lines = block
          .split("\n")
          .map((line) => line.trimEnd())
          .filter((line) => line.length > 0);
        const key = `${String(idx)}-${lines[0] ?? ""}`;
        if (lines.every((line) => /^[-*]\s+/.test(line.trimStart()))) {
          return (
            <ul key={key} className="copilot-list">
              {lines.map((line, li) => (
                <li key={`${key}-${String(li)}`}>
                  {renderInline(
                    line.trimStart().replace(/^[-*]\s+/, ""),
                    onSelectRequest,
                  )}
                </li>
              ))}
            </ul>
          );
        }
        const headingText =
          lines.length === 1 ? parseHeading(lines[0] ?? "") : null;
        if (headingText) {
          return (
            <h4 key={key} className="copilot-heading">
              {renderInline(headingText, onSelectRequest)}
            </h4>
          );
        }
        return (
          <p key={key}>
            {lines.map((line, lineIdx) => (
              <Fragment key={`${key}-${String(lineIdx)}`}>
                {lineIdx > 0 && <br />}
                {renderInline(line, onSelectRequest)}
              </Fragment>
            ))}
          </p>
        );
      })}
    </div>
  );
}

function StepCard({ step }: Readonly<{ step: CopilotStep }>) {
  const duration = formatDuration(step.duration_ms);
  const hasDetail = step.detail && Object.keys(step.detail).length > 0;
  return (
    <div className="copilot-step-card">
      <div className="copilot-step-row">
        <StepIcon toolName={step.tool_name} kind={step.kind} />
        <span className="copilot-step-summary">{step.summary}</span>
        {duration && <span className="copilot-duration mono">{duration}</span>}
      </div>
      {hasDetail && (
        <details className="copilot-step-detail">
          <summary>Details</summary>
          <pre>{JSON.stringify(step.detail, null, 2)}</pre>
        </details>
      )}
    </div>
  );
}

function ConfirmationCard({
  confirmation,
  busy,
  onSelectRequest,
  onConfirm,
}: Readonly<{
  confirmation: CopilotConfirmation;
  busy: boolean;
  onSelectRequest: (requestId: string) => void;
  onConfirm: (approved: boolean, note: string) => void;
}>) {
  const [note, setNote] = useState(confirmation.note ?? "");
  const payout = confirmation.payout ?? 0;
  const label = humanize(
    confirmation.outcome ?? confirmation.recommendation ?? "approve",
  );
  return (
    <div
      className="copilot-confirm-card"
      role="region"
      aria-label="Sign-off confirmation"
    >
      <p className="eyebrow">SIGN-OFF CONFIRMATION</p>
      <div className="copilot-confirm-meta">
        <div>
          <button
            type="button"
            className="copilot-request-link"
            onClick={() => {
              onSelectRequest(confirmation.request_id);
            }}
          >
            {confirmation.request_id}
          </button>
          <span className="copilot-confirm-customer">
            {confirmation.customer_name ?? "Customer"}
          </span>
        </div>
        <div className="copilot-confirm-amount">
          <span className="outcome-pill outcome-approve">{label}</span>
          {payout > 0 && (
            <strong className="mono">{formatMoney(payout)}</strong>
          )}
        </div>
      </div>
      <label className="copilot-confirm-note">
        Review note
        <input
          type="text"
          value={note}
          onChange={(event) => {
            setNote(event.target.value);
          }}
          placeholder="Optional note for audit record"
          disabled={busy}
        />
      </label>
      <div className="copilot-confirm-actions">
        <button
          type="button"
          className="primary-button"
          disabled={busy}
          onClick={() => {
            onConfirm(true, note);
          }}
        >
          Confirm sign off
        </button>
        <button
          type="button"
          className="secondary-button"
          disabled={busy}
          onClick={() => {
            onConfirm(false, note);
          }}
        >
          Cancel
        </button>
      </div>
    </div>
  );
}

export function TurnThreadItem({
  turn,
  steps,
  busy,
  onSelectRequest,
  onConfirm,
  onStop,
}: Readonly<{
  turn: CopilotTurn;
  steps: CopilotStep[];
  busy: boolean;
  onSelectRequest: (requestId: string) => void;
  onConfirm: (turnId: string, approved: boolean, note: string) => void;
  onStop: (turnId: string) => void;
}>) {
  const turnSteps = steps.filter((step) => step.turn_id === turn.turn_id);
  const isWorking = turn.status === "working";
  return (
    <div className="copilot-turn">
      <div className="copilot-user-bubble">{turn.user_message}</div>
      {turnSteps.length > 0 && (
        <div className="copilot-steps" aria-label="Copilot steps">
          {turnSteps.map((step) => (
            <StepCard key={`${step.turn_id}-${String(step.seq)}`} step={step} />
          ))}
        </div>
      )}
      {isWorking && (
        <div className="copilot-working-bar" role="status">
          <span className="working-indicator">
            <i /> Working on your request…
          </span>
          <button
            type="button"
            className="copilot-stop-button"
            disabled={busy}
            onClick={() => {
              onStop(turn.turn_id);
            }}
          >
            Stop
          </button>
        </div>
      )}
      {turn.status === "waiting_confirmation" && turn.confirmation && (
        <ConfirmationCard
          confirmation={turn.confirmation}
          busy={busy}
          onSelectRequest={onSelectRequest}
          onConfirm={(approved, note) => {
            onConfirm(turn.turn_id, approved, note);
          }}
        />
      )}
      {turn.assistant_message && (
        <div className="copilot-assistant-bubble">
          <AssistantMarkdown
            text={turn.assistant_message}
            onSelectRequest={onSelectRequest}
          />
        </div>
      )}
      {turn.error && turn.status !== "done" && (
        <div
          className={
            turn.status === "failed"
              ? "copilot-turn-alert copilot-turn-error"
              : "copilot-turn-alert"
          }
          role={turn.status === "failed" ? "alert" : "status"}
        >
          {turn.error}
        </div>
      )}
    </div>
  );
}
