import { useCallback, useEffect, useState } from "react";
import { CopilotSidebar } from "./CopilotSidebar";
import { readCopilotOpen, writeCopilotOpen } from "./copilotApi";
import {
  ActionPanel,
  ContextUsed,
  InstructionsPanel,
  OrderCard,
  ProgressCard,
  QueuePanel,
  RecommendationCard,
  RequestOverview,
  ReviewRecord,
  RunPanel,
  SentBackHistory,
} from "./components";
import type {
  Instructions,
  Outcome,
  QueueItem,
  RequestDetail,
  Run,
} from "./types";
import "./app.css";

type QueueState = "loading" | "ready" | "error";
const DEFAULT_CAP = 5;

async function getJson<T>(url: string): Promise<T> {
  const response = await fetch(url);
  if (!response.ok)
    throw new Error(`Request failed (${String(response.status)})`);
  return response.json() as Promise<T>;
}

interface NoticeState {
  text: string;
  kind: "info" | "error";
}

function NoticeBanner({ notice }: Readonly<{ notice: NoticeState | null }>) {
  if (!notice) return null;
  return (
    <div
      className={notice.kind === "error" ? "notice notice-error" : "notice"}
      role={notice.kind === "error" ? "alert" : "status"}
    >
      {notice.text}
    </div>
  );
}

async function sendPost(url: string, body?: object): Promise<void> {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  if (!response.ok) {
    const errorBody = (await response.json()) as { detail?: string };
    throw new Error(
      errorBody.detail ?? `Request failed (${String(response.status)})`,
    );
  }
}

function useDashboardPolling(options: {
  active: boolean;
  resumingId: string | null;
  selectedId: string | null;
  loadQueue: () => Promise<void>;
  loadRun: () => Promise<void>;
  loadDetail: (id: string) => Promise<void>;
  onExpireResume: (id: string) => void;
}) {
  const {
    active,
    resumingId,
    selectedId,
    loadQueue,
    loadRun,
    loadDetail,
    onExpireResume,
  } = options;
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => {
      void loadQueue();
      void loadRun();
      if (selectedId) void loadDetail(selectedId);
    }, 1500);
    return () => {
      window.clearInterval(timer);
    };
  }, [active, loadDetail, loadQueue, loadRun, selectedId]);

  useEffect(() => {
    if (!resumingId) return;
    const timeout = window.setTimeout(() => {
      onExpireResume(resumingId);
    }, 15_000);
    return () => {
      window.clearTimeout(timeout);
    };
  }, [onExpireResume, resumingId]);
}

function useDashboardController() {
  const [queue, setQueue] = useState<QueueItem[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<RequestDetail | null>(null);
  const [instructions, setInstructions] = useState<Instructions | null>(null);
  const [run, setRun] = useState<Run | null>(null);
  const [cap, setCap] = useState(DEFAULT_CAP);
  const [queueState, setQueueState] = useState<QueueState>("loading");
  const [detailError, setDetailError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<NoticeState | null>(null);
  const [resumingId, setResumingId] = useState<string | null>(null);
  const [copilotOpen, setCopilotOpen] = useState<boolean>(readCopilotOpen);

  const loadQueue = useCallback(async () => {
    setQueueState("loading");
    try {
      const items = await getJson<QueueItem[]>("/api/requests");
      setQueue(items);
      setQueueState("ready");
      setSelectedId((current) => chooseRequest(items, current));
    } catch {
      setQueueState("error");
    }
  }, []);

  const loadInstructions = useCallback(async () => {
    try {
      setInstructions(await getJson<Instructions>("/api/agent/instructions"));
    } catch {
      const unavailable = "Agent instructions are unavailable.";
      setInstructions({ queue: unavailable, request: unavailable });
    }
  }, []);

  const loadRun = useCallback(async () => {
    try {
      setRun(await getJson<Run | null>("/api/runs/current"));
    } catch {
      setRun(null);
    }
  }, []);

  const loadDetail = useCallback(async (requestId: string) => {
    try {
      const result = await getJson<RequestDetail>(`/api/requests/${requestId}`);
      setDetail(result);
      setDetailError(null);
      if (result.status !== "sent_back") {
        setResumingId((current) => (current === requestId ? null : current));
      }
    } catch {
      setDetail(null);
      setDetailError("We could not load this request. Try selecting it again.");
    }
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      void loadQueue();
      void loadInstructions();
      void loadRun();
    }, 0);
    return () => {
      window.clearTimeout(timer);
    };
  }, [loadInstructions, loadQueue, loadRun]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      if (selectedId) void loadDetail(selectedId);
      else setDetail(null);
    }, 0);
    return () => {
      window.clearTimeout(timer);
    };
  }, [loadDetail, selectedId]);

  const waitingForResume =
    !busy && resumingId !== null && resumingId === selectedId;
  const active =
    run?.status === "running" ||
    detail?.status === "in_progress" ||
    waitingForResume ||
    queue.some((item) => item.status === "in_progress");

  const expireResume = useCallback((id: string) => {
    setResumingId((current) => (current === id ? null : current));
  }, []);

  useDashboardPolling({
    active,
    resumingId,
    selectedId,
    loadQueue,
    loadRun,
    loadDetail,
    onExpireResume: expireResume,
  });

  async function post(
    url: string,
    done: string,
    body?: object,
  ): Promise<boolean> {
    setBusy(true);
    setNotice(null);
    try {
      await sendPost(url, body);
      setNotice({ text: done, kind: "info" });
      return true;
    } catch (error) {
      setNotice({
        text: error instanceof Error ? error.message : "Something went wrong.",
        kind: "error",
      });
      return false;
    } finally {
      await Promise.all([
        loadQueue(),
        loadRun(),
        selectedId ? loadDetail(selectedId) : Promise.resolve(),
      ]);
      setBusy(false);
    }
  }

  const maxCap = Math.max(queue.length, 1);
  const runCap = Math.min(cap, maxCap);

  const handleCopilotMutated = useCallback(() => {
    void Promise.all([
      loadQueue(),
      loadRun(),
      selectedId ? loadDetail(selectedId) : Promise.resolve(),
    ]);
  }, [loadDetail, loadQueue, loadRun, selectedId]);

  return {
    queue,
    selectedId,
    detail,
    instructions,
    run,
    queueState,
    detailError,
    busy,
    active,
    notice,
    runCap,
    maxCap,
    copilotOpen,
    setCap,
    selectRequest: (id: string) => {
      setSelectedId(id);
      setNotice(null);
      setResumingId(null);
    },
    refreshQueue: () => void loadQueue(),
    initiateProcessing: () => {
      void post(
        "/api/runs",
        `Processing started for up to ${String(runCap)} waiting requests.`,
        { cap: runCap },
      );
    },
    recordReview: (outcome: Outcome, notes: string, reviewedBy: string) => {
      if (!selectedId) return;
      void post(`/api/requests/${selectedId}/review`, "Review recorded.", {
        outcome,
        notes,
        reviewed_by: reviewedBy,
      });
    },
    sendBackRequest: (note: string, sentBackBy: string) => {
      if (!selectedId) return;
      setResumingId(selectedId);
      void post(
        `/api/requests/${selectedId}/send_back`,
        "Request sent back to agent.",
        { note, sent_back_by: sentBackBy },
      ).then((ok) => {
        if (!ok) {
          setResumingId((current) => (current === selectedId ? null : current));
        }
      });
    },
    handleCopilotMutated,
    toggleCopilot: () => {
      setCopilotOpen((current) => {
        const next = !current;
        writeCopilotOpen(next);
        return next;
      });
    },
  };
}

export function App() {
  const state = useDashboardController();
  const {
    queue,
    selectedId,
    detail,
    instructions,
    run,
    queueState,
    detailError,
    busy,
    active,
    notice,
    runCap,
    maxCap,
    copilotOpen,
    setCap,
    selectRequest,
    refreshQueue,
    initiateProcessing,
    recordReview,
    sendBackRequest,
    handleCopilotMutated,
    toggleCopilot,
  } = state;
  const selected = queue.find((item) => item.request_id === selectedId);

  return (
    <div className={copilotOpen ? "app-frame copilot-open" : "app-frame"}>
      <main className="shell">
        <Header
          busy={busy || active}
          cap={runCap}
          maxCap={maxCap}
          onCapChange={setCap}
          onInitiate={initiateProcessing}
          onRefresh={refreshQueue}
        />
        <NoticeBanner notice={notice} />
        <RunPanel run={run} />
        <div className="workspace">
          <QueuePanel
            items={queue}
            selectedId={selectedId}
            state={queueState}
            onSelect={selectRequest}
            onRefresh={refreshQueue}
          />
          <DetailColumn
            detail={detail}
            detailError={detailError}
            selectedOrderId={selected?.order_id}
            busy={busy}
            onReview={recordReview}
            onSendBack={sendBackRequest}
          />
        </div>
        <InstructionsPanel instructions={instructions} />
        <footer>
          <span>Shoppie Support Pilot</span>
          <span>
            Sample policies · Agent recommendations require human review
          </span>
        </footer>
      </main>
      <CopilotSidebar
        open={copilotOpen}
        onToggle={toggleCopilot}
        selectedId={selectedId}
        onSelectRequest={selectRequest}
        onMutated={handleCopilotMutated}
      />
    </div>
  );
}

interface DetailColumnProps {
  detail: RequestDetail | null;
  detailError: string | null;
  selectedOrderId: string | undefined;
  busy: boolean;
  onReview: (outcome: Outcome, notes: string, reviewedBy: string) => void;
  onSendBack: (note: string, sentBackBy: string) => void;
}

function DetailColumn({
  detail,
  detailError,
  selectedOrderId,
  busy,
  onReview,
  onSendBack,
}: Readonly<DetailColumnProps>) {
  return (
    <section className="detail-column" aria-label="Request details">
      {detailError && (
        <div className="panel state-message error-state" role="alert">
          {detailError}
        </div>
      )}
      {!detail && !detailError && (
        <div className="panel state-message">
          Select a request to see its details.
        </div>
      )}
      {detail && selectedOrderId && (
        <>
          <div className="detail-grid">
            <RequestOverview detail={detail} orderId={selectedOrderId} />
            <OrderCard detail={detail} />
          </div>
          {detail.recommendation && (
            <RecommendationCard recommendation={detail.recommendation} />
          )}
          <SentBackHistory sentBack={detail.sent_back ?? []} />
          <ContextUsed steps={detail.steps} />
          <ReviewRecord detail={detail} />
          <ActionPanel
            key={detail.request.request_id}
            detail={detail}
            busy={busy}
            onReview={onReview}
            onSendBack={onSendBack}
          />
          <ProgressCard detail={detail} />
        </>
      )}
    </section>
  );
}

interface HeaderProps {
  busy: boolean;
  cap: number;
  maxCap: number;
  onCapChange: (cap: number) => void;
  onInitiate: () => void;
  onRefresh: () => void;
}

function Header({
  busy,
  cap,
  maxCap,
  onCapChange,
  onInitiate,
  onRefresh,
}: Readonly<HeaderProps>) {
  return (
    <>
      <header className="topbar">
        <a className="brand" href="#queue" aria-label="Shoppie Support home">
          <span className="brand-mark">S</span>
          <span>shoppie</span>
          <span className="brand-divider">/</span>
          <span className="brand-subtitle">support desk</span>
        </a>
        <div className="topbar-meta">
          <span className="sample-pill">
            <span /> Pilot workspace
          </span>
          <span className="agent-avatar" aria-label="Support agent">
            SA
          </span>
        </div>
      </header>
      <section className="page-heading" id="queue">
        <div>
          <p className="eyebrow">CUSTOMER OPERATIONS</p>
          <h1>Support requests</h1>
          <p className="subheading">
            Review order issues and recommendations from the Shoppie Agent.
          </p>
        </div>
        <div className="heading-actions">
          <button className="secondary-button" onClick={onRefresh}>
            <span aria-hidden="true">↻</span> Refresh queue
          </button>
          <label className="cap-input">
            Requests to work
            <input
              type="number"
              min={1}
              max={maxCap}
              value={cap}
              onChange={(event) => {
                onCapChange(
                  Math.min(
                    maxCap,
                    Math.max(1, Number(event.target.value) || 1),
                  ),
                );
              }}
            />
          </label>
          <button
            className="primary-button"
            disabled={busy}
            onClick={onInitiate}
          >
            {busy ? "Processing…" : "Initiate processing"}
          </button>
        </div>
      </section>
    </>
  );
}

function chooseRequest(items: QueueItem[], current: string | null) {
  if (current && items.some((item) => item.request_id === current))
    return current;
  return items[0]?.request_id ?? null;
}
