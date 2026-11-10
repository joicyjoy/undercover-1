import { useState } from "react";
import type { SyntheticEvent } from "react";
import { CalculationTable } from "./ContextUsed";
import { formatDate, formatMoney } from "./format";
import type {
  Outcome,
  QueueStatus,
  RequestDetail,
  SentBackDecision,
} from "./types";

const MAX_NOTE_LENGTH = 1000;

const outcomeLabels: Record<Outcome, string> = {
  approve: "Approve",
  decline: "Decline",
  request_information: "Request information",
};

const actionHeadings: Record<QueueStatus, string> = {
  waiting: "Waiting to be processed",
  in_progress: "The agent is reviewing this request",
  waiting_for_agent_review: "Your review is required",
  sent_back: "Sent back for agent review",
  decided: "Review complete",
};

function canBeSentBack(detail: RequestDetail): boolean {
  const rec = detail.recommendation;
  if (!rec) return false;
  if (rec.decided_by === "Security check") return false;
  return !rec.policy_citations.includes("Security check");
}

export function SentBackHistory({
  sentBack,
}: Readonly<{ sentBack: SentBackDecision[] }>) {
  if (sentBack.length === 0) return null;
  return (
    <section className="sent-back-history" aria-label="Sent-back decisions">
      {sentBack.map((entry, index) => {
        const rec = entry.decision;
        const payout = rec.payout;
        return (
          <article
            className="panel sent-back-panel"
            key={`${entry.sent_back_at}-${String(index)}`}
          >
            <div className="panel-heading">
              <div>
                <p className="eyebrow">
                  SENT-BACK DECISION{" "}
                  {sentBack.length > 1 ? `#${String(index + 1)}` : ""}
                </p>
                <h2>Previous recommendation</h2>
              </div>
              <div className="outcome-row">
                <span className={`outcome-pill outcome-${rec.outcome}`}>
                  {outcomeLabels[rec.outcome]}
                </span>
                {payout !== undefined && payout > 0 && (
                  <span className="payout mono">{formatMoney(payout)}</span>
                )}
              </div>
            </div>
            {rec.calculation && (
              <CalculationTable calculation={rec.calculation} />
            )}
            <p className="recommendation-rationale">
              {rec.rationale || rec.reason}
            </p>
            {rec.section && <p className="section-ref">{rec.section}</p>}
            <div className="sent-back-note">
              <small>REVIEWER NOTE</small>
              <p>{entry.note}</p>
              <small className="muted">
                Sent back by {entry.sent_back_by} ·{" "}
                {formatDate(entry.sent_back_at)} IST
              </small>
            </div>
          </article>
        );
      })}
    </section>
  );
}

export function ActionPanel({
  detail,
  busy,
  onReview,
  onSendBack,
}: Readonly<{
  detail: RequestDetail;
  busy: boolean;
  onReview: (outcome: Outcome, notes: string, reviewedBy: string) => void;
  onSendBack?: (note: string, sentBackBy: string) => void;
}>) {
  const [outcome, setOutcome] = useState<Outcome>("approve");
  const [notes, setNotes] = useState("");
  const [reviewedBy, setReviewedBy] = useState("");
  const [sendBackNote, setSendBackNote] = useState("");
  const [sentBackBy, setSentBackBy] = useState("");

  const onSubmitReview = (event: SyntheticEvent<HTMLFormElement>) => {
    event.preventDefault();
    onReview(outcome, notes, reviewedBy.trim());
  };

  const onSubmitSendBack = (event: SyntheticEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (onSendBack) onSendBack(sendBackNote.trim(), sentBackBy.trim());
  };

  const showSendBack =
    detail.status === "waiting_for_agent_review" &&
    canBeSentBack(detail) &&
    onSendBack !== undefined;

  return (
    <div className="action-panel panel">
      <div className="action-copy">
        <p className="eyebrow">SUPPORT SIGN-OFF</p>
        <strong>
          {detail.status === "in_progress" && detail.review
            ? "The agent is completing this reviewed request"
            : actionHeadings[detail.status]}
        </strong>
        <p>
          {detail.status === "sent_back"
            ? "This request was sent back with a note and is waiting for the next run."
            : "Recommendations support your decision. No customer or order action is performed here."}
        </p>
      </div>
      {detail.status === "waiting_for_agent_review" && (
        <div className="action-forms">
          <form className="review-form" onSubmit={onSubmitReview}>
            <label>
              Reviewed outcome
              <select
                value={outcome}
                onChange={(e) => {
                  setOutcome(e.target.value as Outcome);
                }}
              >
                {Object.entries(outcomeLabels).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Reviewed by
              <input
                type="text"
                required
                value={reviewedBy}
                onChange={(e) => {
                  setReviewedBy(e.target.value);
                }}
                placeholder="Your name"
              />
            </label>
            <label className="notes-field">
              <span>
                Notes <span className="optional">(optional)</span>
              </span>
              <textarea
                value={notes}
                onChange={(e) => {
                  setNotes(e.target.value);
                }}
                placeholder="Add context for your decision…"
                rows={2}
              />
            </label>
            <button
              className="primary-button"
              disabled={busy || !reviewedBy.trim()}
              type="submit"
            >
              Record support review
            </button>
          </form>
          {showSendBack && (
            <form className="send-back-form" onSubmit={onSubmitSendBack}>
              <p className="eyebrow">SEND BACK TO AGENT</p>
              <label>
                Sent back by
                <input
                  type="text"
                  required
                  value={sentBackBy}
                  onChange={(e) => {
                    setSentBackBy(e.target.value);
                  }}
                  placeholder="Your name"
                />
              </label>
              <label className="notes-field">
                <span>
                  What to look at again{" "}
                  <span className="optional">
                    ({String(sendBackNote.length)} / {String(MAX_NOTE_LENGTH)})
                  </span>
                </span>
                <textarea
                  required
                  maxLength={MAX_NOTE_LENGTH}
                  value={sendBackNote}
                  onChange={(e) => {
                    setSendBackNote(e.target.value);
                  }}
                  placeholder="Explain what the agent should check again…"
                  rows={2}
                />
              </label>
              <button
                className="secondary-button"
                disabled={busy || !sentBackBy.trim() || !sendBackNote.trim()}
                type="submit"
              >
                Send back
              </button>
            </form>
          )}
        </div>
      )}
    </div>
  );
}
