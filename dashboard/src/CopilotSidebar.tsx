import { useCallback, useEffect, useRef, useState } from "react";
import type { SyntheticEvent } from "react";
import { CopilotMark, TurnThreadItem } from "./CopilotMessage";
import {
  confirmCopilotTurn,
  createConversation,
  fetchConversation,
  fetchCopilotMe,
  listConversations,
  startCopilotTurn,
  stopCopilotTurn,
} from "./copilotApi";
import type {
  CopilotConversation,
  CopilotConversationSummary,
  CopilotMe,
} from "./types";

export interface CopilotSidebarProps {
  open: boolean;
  onToggle: () => void;
  selectedId: string | null;
  onSelectRequest: (id: string) => void;
  onMutated: () => void;
}

const STARTER_PROMPTS = [
  "What needs my attention today?",
  "Explain REQ-7001's decision",
  "Start a run for 5 pending requests",
  "How are we tracking against pilot targets?",
];

function buildStarterPrompts(selectedId: string | null): string[] {
  if (!selectedId) return STARTER_PROMPTS;
  return [
    STARTER_PROMPTS[0] ?? "",
    `Explain ${selectedId}'s decision`,
    STARTER_PROMPTS[2] ?? "",
    STARTER_PROMPTS[3] ?? "",
  ].filter(Boolean);
}

function SidebarHeader({
  me,
  devHandlerId,
  conversations,
  activeId,
  onSelectHandler,
  onSelectConversation,
  onNewChat,
  onClose,
}: Readonly<{
  me: CopilotMe | null;
  devHandlerId: string | null;
  conversations: CopilotConversationSummary[];
  activeId: string | null;
  onSelectHandler: (handlerId: string) => void;
  onSelectConversation: (conversationId: string) => void;
  onNewChat: () => void;
  onClose: () => void;
}>) {
  const showDevSwitcher =
    me?.handler.auth_mode === "dev" && me.dev_handlers.length > 0;
  return (
    <header className="copilot-header">
      <div className="copilot-title-row">
        <div className="copilot-brand">
          <span className="copilot-mark-badge">
            <CopilotMark size={16} />
          </span>
          <div>
            <p className="eyebrow">SHOPPIE COPILOT</p>
            <h2>Support assistant</h2>
          </div>
        </div>
        <button
          type="button"
          className="copilot-icon-button"
          aria-label="Collapse Shoppie Copilot"
          onClick={onClose}
        >
          Collapse
        </button>
      </div>
      {me && (
        <div className="copilot-handler-bar">
          <div className="copilot-handler-meta">
            <strong>{me.handler.name}</strong>
            <span>{me.handler.title}</span>
          </div>
          {showDevSwitcher && (
            <label className="copilot-select-label">
              <span className="sr-only">Switch local handler role</span>
              <select
                aria-label="Local dev handler"
                value={devHandlerId ?? me.handler.handler_id}
                onChange={(event) => {
                  onSelectHandler(event.target.value);
                }}
              >
                {me.dev_handlers.map((item) => (
                  <option key={item.handler_id} value={item.handler_id}>
                    {item.name} (
                    {item.can_sign_off_over_limit ? "Senior" : "Handler"})
                  </option>
                ))}
              </select>
            </label>
          )}
        </div>
      )}
      <div className="copilot-thread-controls">
        <select
          aria-label="Copilot conversation"
          value={activeId ?? ""}
          onChange={(event) => {
            if (event.target.value) onSelectConversation(event.target.value);
          }}
        >
          <option value="">
            {conversations.length > 0 ? "Select a chat…" : "New chat"}
          </option>
          {conversations.map((conv) => (
            <option key={conv.conversation_id} value={conv.conversation_id}>
              {conv.title}
            </option>
          ))}
        </select>
        <button
          type="button"
          className="secondary-button copilot-new-button"
          onClick={onNewChat}
        >
          + New chat
        </button>
      </div>
    </header>
  );
}

function ComposerForm({
  input,
  selectedId,
  disabled,
  isWorking,
  isWaitingConfirmation,
  onInputChange,
  onSend,
}: Readonly<{
  input: string;
  selectedId: string | null;
  disabled: boolean;
  isWorking: boolean;
  isWaitingConfirmation: boolean;
  onInputChange: (value: string) => void;
  onSend: (text: string) => void;
}>) {
  function handleSubmit(event: SyntheticEvent<HTMLFormElement>) {
    event.preventDefault();
    onSend(input);
  }
  const placeholder = isWaitingConfirmation
    ? "Confirm or cancel the sign-off above first…"
    : "Ask Copilot about queue, requests, or runs…";
  return (
    <form className="copilot-composer" onSubmit={handleSubmit}>
      <label className="copilot-composer-label">
        <span className="sr-only">Message Shoppie Copilot</span>
        <textarea
          rows={2}
          value={input}
          onChange={(event) => {
            onInputChange(event.target.value);
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              onSend(input);
            }
          }}
          placeholder={placeholder}
          disabled={disabled}
        />
      </label>
      <div className="copilot-composer-footer">
        <span className="copilot-context-chip">
          {selectedId ? `Focused: ${selectedId}` : "Queue overview"}
        </span>
        <button
          type="submit"
          className="primary-button"
          disabled={disabled || !input.trim()}
        >
          {isWorking ? "Working…" : "Send"}
        </button>
      </div>
    </form>
  );
}

function useCopilotSession(selectedId: string | null, onMutated: () => void) {
  const [devHandlerId, setDevHandlerId] = useState<string | null>(null);
  const [me, setMe] = useState<CopilotMe | null>(null);
  const [conversations, setConversations] = useState<
    CopilotConversationSummary[]
  >([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [conversation, setConversation] = useState<CopilotConversation | null>(
    null,
  );
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const prevStatusRef = useRef<string | null>(null);

  const loadConversation = useCallback(
    async (handlerOverride: string | null, convId: string) => {
      try {
        const loaded = await fetchConversation(handlerOverride, convId);
        setConversation(loaded);
        setActiveId(loaded.conversation_id);
        setError(null);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Could not load chat.");
      }
    },
    [],
  );

  const refreshCopilotMeta = useCallback(
    async (handlerOverride: string | null) => {
      try {
        const [profile, list] = await Promise.all([
          fetchCopilotMe(handlerOverride),
          listConversations(handlerOverride),
        ]);
        setMe(profile);
        setConversations(list);
        return list;
      } catch {
        return [];
      }
    },
    [],
  );

  useEffect(() => {
    let cancelled = false;
    async function bootstrap() {
      const list = await refreshCopilotMeta(devHandlerId);
      if (cancelled || list.length === 0) return;
      const firstId = list[0]?.conversation_id;
      if (firstId) await loadConversation(devHandlerId, firstId);
    }
    void bootstrap();
    return () => {
      cancelled = true;
    };
  }, [devHandlerId, loadConversation, refreshCopilotMeta]);

  const status = conversation?.status ?? null;
  const isWorking = status === "working";
  const isWaitingConfirmation = status === "waiting_confirmation";

  useEffect(() => {
    if (prevStatusRef.current === "working" && status && status !== "working") {
      onMutated();
      void refreshCopilotMeta(devHandlerId);
    }
    prevStatusRef.current = status;
  }, [devHandlerId, onMutated, refreshCopilotMeta, status]);

  useEffect(() => {
    if (!isWorking || !activeId) return;
    const timer = window.setInterval(() => {
      void loadConversation(devHandlerId, activeId);
    }, 1000);
    return () => {
      window.clearInterval(timer);
    };
  }, [activeId, devHandlerId, isWorking, loadConversation]);

  async function sendPrompt(text: string) {
    const trimmed = text.trim();
    if (!trimmed || busy || isWorking || isWaitingConfirmation) return;
    setBusy(true);
    setError(null);
    try {
      let targetId = activeId;
      if (!targetId) {
        const created = await createConversation(
          devHandlerId,
          trimmed.slice(0, 48),
        );
        targetId = created.conversation_id;
        setActiveId(targetId);
      }
      await startCopilotTurn(devHandlerId, targetId, {
        message: trimmed,
        page_path: window.location.pathname,
        selected_request_id: selectedId,
      });
      setInput("");
      await Promise.all([
        loadConversation(devHandlerId, targetId),
        refreshCopilotMeta(devHandlerId),
      ]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not send message.");
    } finally {
      setBusy(false);
    }
  }

  async function confirmTurn(turnId: string, approved: boolean, note: string) {
    if (!activeId || busy) return;
    setBusy(true);
    setError(null);
    try {
      const res = await confirmCopilotTurn(devHandlerId, activeId, turnId, {
        approved,
        note,
      });
      setConversation(res.conversation);
      if (res.conversation.status !== "working") onMutated();
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Could not confirm sign-off.",
      );
    } finally {
      setBusy(false);
    }
  }

  async function stopTurn(turnId: string) {
    if (!activeId || busy) return;
    setBusy(true);
    try {
      const updated = await stopCopilotTurn(devHandlerId, activeId, turnId);
      setConversation(updated);
      onMutated();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not stop turn.");
    } finally {
      setBusy(false);
    }
  }

  function resetThread(nextHandlerId?: string) {
    prevStatusRef.current = null;
    setActiveId(null);
    setConversation(null);
    setError(null);
    if (nextHandlerId !== undefined) setDevHandlerId(nextHandlerId);
  }

  return {
    me,
    devHandlerId,
    conversations,
    activeId,
    conversation,
    input,
    setInput,
    busy,
    error,
    isWorking,
    isWaitingConfirmation,
    loadConversation: (id: string) => loadConversation(devHandlerId, id),
    sendPrompt,
    confirmTurn,
    stopTurn,
    switchHandler: (nextId: string) => {
      resetThread(nextId);
    },
    startNewChat: () => {
      resetThread();
    },
  };
}

export function CopilotSidebar({
  open,
  onToggle,
  selectedId,
  onSelectRequest,
  onMutated,
}: Readonly<CopilotSidebarProps>) {
  const session = useCopilotSession(selectedId, onMutated);

  if (!open) {
    return (
      <aside className="copilot-rail" aria-label="Shoppie Copilot collapsed">
        <button
          type="button"
          className="copilot-collapsed-tab"
          aria-label="Open Shoppie Copilot"
          onClick={onToggle}
        >
          <CopilotMark size={18} />
          <span>Copilot</span>
        </button>
      </aside>
    );
  }

  const turns = session.conversation?.turns ?? [];
  const steps = session.conversation?.steps ?? [];
  const prompts = buildStarterPrompts(selectedId);
  const composerDisabled =
    session.busy || session.isWorking || session.isWaitingConfirmation;

  return (
    <>
      <button
        type="button"
        className="copilot-backdrop"
        aria-label="Close Shoppie Copilot"
        onClick={onToggle}
      />
      <aside className="copilot-sidebar panel" aria-label="Shoppie Copilot">
        <SidebarHeader
          me={session.me}
          devHandlerId={session.devHandlerId}
          conversations={session.conversations}
          activeId={session.activeId}
          onSelectHandler={session.switchHandler}
          onSelectConversation={(id) => {
            void session.loadConversation(id);
          }}
          onNewChat={session.startNewChat}
          onClose={onToggle}
        />

        <div className="copilot-messages">
          {turns.length === 0 ? (
            <div className="copilot-empty">
              <p className="copilot-empty-title">
                Ask about queue priorities, refund math, policies, or sign-off
                readiness.
              </p>
              <div className="copilot-starters">
                {prompts.map((prompt) => (
                  <button
                    key={prompt}
                    type="button"
                    className="copilot-starter-button"
                    disabled={session.busy}
                    onClick={() => {
                      void session.sendPrompt(prompt);
                    }}
                  >
                    {prompt}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            turns.map((turn) => (
              <TurnThreadItem
                key={turn.turn_id}
                turn={turn}
                steps={steps}
                busy={session.busy}
                onSelectRequest={onSelectRequest}
                onConfirm={(tId, approved, note) => {
                  void session.confirmTurn(tId, approved, note);
                }}
                onStop={(tId) => {
                  void session.stopTurn(tId);
                }}
              />
            ))
          )}
        </div>

        {session.error && (
          <div className="copilot-sidebar-error" role="alert">
            {session.error}
          </div>
        )}

        <ComposerForm
          input={session.input}
          selectedId={selectedId}
          disabled={composerDisabled}
          isWorking={session.isWorking}
          isWaitingConfirmation={session.isWaitingConfirmation}
          onInputChange={session.setInput}
          onSend={(text) => {
            void session.sendPrompt(text);
          }}
        />
      </aside>
    </>
  );
}
