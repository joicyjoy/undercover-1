import { formatDate, sourceOf } from "./format";
import type { RequestDetail, RequestStep } from "./types";

function StepBody({ step }: Readonly<{ step: RequestStep }>) {
  if (step.stage === "screening" || step.stage === "security_check") {
    return (
      <div className="step-marked">
        <span className="step-label">Security check</span> {step.message}
      </div>
    );
  }
  if (step.stage === "tool_call") {
    return (
      <p>
        <span className="step-label">Called</span>{" "}
        <code className="mono">{step.message}</code>
      </p>
    );
  }
  if (step.stage === "tool_result") {
    const summary =
      step.message.length > 90
        ? `${step.message.slice(0, 90)}...`
        : step.message;
    return (
      <details>
        <summary>
          <span className="step-label">Result</span>{" "}
          <code className="mono">{summary}</code>
        </summary>
        <pre className="step-result">{step.message}</pre>
      </details>
    );
  }
  if (step.stage === "lookup") {
    return (
      <div className="step-marked">
        <div>
          <span className="step-label">Looked up</span> {step.message}
        </div>
        {!step.sources || step.sources.length === 0 ? (
          <p className="step-sources small muted">Found nothing</p>
        ) : (
          <ul className="step-sources small muted">
            {step.sources.map((source) => (
              <li key={source.file}>
                <span className="mono">{source.file}</span>: {sourceOf(source)}
              </li>
            ))}
          </ul>
        )}
      </div>
    );
  }
  if (step.stage === "memory_search") {
    return (
      <div className="step-marked">
        <span className="step-label">Searched memory</span>{" "}
        <span className="step-text">{step.message}</span>
      </div>
    );
  }
  if (step.stage === "memory_save") {
    return (
      <div className="step-marked">
        <span className="step-label">Saved to memory</span>{" "}
        <span className="step-text">{step.message}</span>
      </div>
    );
  }
  if (step.stage === "refusal") {
    return (
      <p>
        <span className="chip step-refused">Refused</span>{" "}
        <code className="mono">{step.message}</code>
      </p>
    );
  }
  if (step.stage === "error") {
    return (
      <p>
        <span className="chip step-failed">Failed</span> {step.message}
      </p>
    );
  }
  return <p>{step.message}</p>;
}

export function ProgressCard({ detail }: Readonly<{ detail: RequestDetail }>) {
  const sectionsRead = detail.steps
    .filter((step) => step.stage === "read_section")
    .map((step) => step.message.replace(/^Read /, "").replace(/\.$/, ""));
  return (
    <article className="panel timeline-panel">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">REQUEST ACTIVITY</p>
          <h2>Progress</h2>
        </div>
        {detail.status === "in_progress" && (
          <span className="working-indicator">
            <i /> Working
          </span>
        )}
      </div>
      {sectionsRead.length > 0 && (
        <div className="citation-block sections-read">
          <small>POLICY SECTIONS READ</small>
          <ul>
            {[...new Set(sectionsRead)].map((section) => (
              <li key={section}>{section}</li>
            ))}
          </ul>
        </div>
      )}
      {detail.steps.length === 0 ? (
        <p className="muted">No agent activity yet.</p>
      ) : (
        <ol className="timeline">
          {detail.steps.map((step, index) => (
            <li
              className="timeline-step"
              key={`${String(index)}-${step.created_at}`}
            >
              <span
                className={`timeline-dot ${index === detail.steps.length - 1 && detail.status === "in_progress" ? "active" : ""}`}
              />
              <div>
                <StepBody step={step} />
                <time>{formatDate(step.created_at)} IST</time>
              </div>
            </li>
          ))}
        </ol>
      )}
    </article>
  );
}
