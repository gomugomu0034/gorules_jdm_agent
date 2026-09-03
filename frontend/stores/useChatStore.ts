'use client';

import type { DecisionGraphType } from '@gorules/jdm-editor';
import { create } from 'zustand';

import { api, AppError } from '../lib/api';
import { createEventStream } from '../lib/sse';
import type {
  ChatEvent,
  ChatMessage,
  PendingInterrupt,
  ProgressEvent,
  Proposal,
  TestRunReport,
} from '../lib/types';

export type RunStep = {
  node: string;
  label: string;
  status: 'running' | 'done';
  progress?: ProgressEvent;
};

type Canvas = { content: DecisionGraphType; graph_id?: string | null; name?: string | null };

type ChatState = {
  threadId: string | null;
  /** The graph this conversation belongs to; `null` for a draft. */
  graphId: string | null;
  messages: ChatMessage[];
  steps: RunStep[];
  pending: PendingInterrupt | null;
  proposal: Proposal | null;
  testReport: TestRunReport | null;
  running: boolean;
  error: string | null;
  connected: boolean;

  open: (graphId: string | null) => Promise<void>;
  /** Replace a conversation the server no longer has with a fresh one. */
  restart: () => Promise<void>;
  send: (text: string, canvas: Canvas) => Promise<void>;
  respond: (value: string, canvas: Canvas) => Promise<void>;
  cancel: () => Promise<void>;
  clearProposal: () => void;
  reset: () => void;
};

let stream: ReturnType<typeof createEventStream> | null = null;

// A conversation the server has lost is replaced rather than reconnected to,
// but only so many times: if the replacement is unreachable too, something
// systemic is wrong and silently minting threads would hide it. The budget is
// per visit - `reset` runs when the studio unmounts.
const MAX_RESTARTS = 2;
let restarts = 0;

const initial = {
  threadId: null,
  graphId: null,
  messages: [] as ChatMessage[],
  steps: [] as RunStep[],
  pending: null,
  proposal: null,
  testReport: null,
  running: false,
  error: null,
  connected: false,
};

export const useChatStore = create<ChatState>((set, get) => ({
  ...initial,

  reset: () => {
    stream?.close();
    stream = null;
    restarts = 0;
    set({ ...initial });
  },

  open: async (graphId) => {
    stream?.close();
    stream = null;
    set({ ...initial });

    try {
      const { id } = await api.createThread(graphId);
      const state = await api.getThread(id);

      set({
        threadId: id,
        graphId,
        messages: state.messages.map((m, i) => ({ id: `restored-${i}`, ...m })),
        pending: state.pending_interrupt,
        proposal: state.proposal,
        running: state.status === 'running',
      });

      stream = createEventStream({
        threadId: id,
        fromSeq: state.last_seq,
        onEvent: (event) => apply(set, get, event),
        onError: () => set({ connected: false }),
        onGone: () => void get().restart(),
      });
      set({ connected: true });
    } catch (e) {
      set({ error: describe(e) });
    }
  },

  /**
   * The thread vanished from under us - swept with an expired guest session,
   * deleted, or created under an identity this browser has since traded for
   * another. Its history is unrecoverable either way, and the stream would
   * otherwise reconnect to a 404 for as long as the tab stayed open, so open a
   * new conversation in its place and say so.
   */
  restart: async () => {
    if (restarts >= MAX_RESTARTS) {
      stream?.close();
      stream = null;
      set({
        connected: false,
        running: false,
        error: 'Lost the connection to the assistant. Reload the page to start again.',
      });
      return;
    }
    restarts += 1;
    const { graphId } = get();
    await get().open(graphId);
    if (!get().error) {
      set({ error: 'That conversation is no longer available, so a new one was started.' });
    }
  },

  send: async (text, canvas) => {
    const { threadId, running } = get();
    if (!threadId || running) return;

    set((s) => ({
      messages: [...s.messages, { id: `local-${Date.now()}`, role: 'user', content: text }],
      running: true,
      steps: [],
      error: null,
      pending: null,
    }));

    try {
      await api.sendMessage(threadId, text, canvas);
    } catch (e) {
      set({ running: false, error: describe(e) });
    }
  },

  respond: async (value, canvas) => {
    const { threadId } = get();
    if (!threadId) return;

    // Echo the exact chip label; the agent compares these strings literally.
    set((s) => ({
      messages: [...s.messages, { id: `local-${Date.now()}`, role: 'user', content: value }],
      pending: null,
      running: true,
      steps: [],
      error: null,
    }));

    try {
      await api.resume(threadId, value, canvas);
    } catch (e) {
      set({ running: false, error: describe(e) });
    }
  },

  cancel: async () => {
    const { threadId } = get();
    if (!threadId) return;
    try {
      await api.cancelRun(threadId);
    } catch {
      // The done event settles the real state either way.
    }
  },

  /**
   * Resolve the proposal, and with it the approval gate that offered it.
   *
   * Accepting or rejecting on the canvas *is* the answer to the agent's
   * "Approve & Save" question, so leaving the chips up would both duplicate a
   * decision already made and keep the composer disabled - blocking the very
   * follow-up ("now add a rule for...") the flow is built around.
   */
  clearProposal: () =>
    set((s) => ({
      proposal: null,
      pending: s.pending?.kind === 'choice' ? null : s.pending,
    })),
}));

type Setter = (partial: Partial<ChatState> | ((s: ChatState) => Partial<ChatState>)) => void;

function apply(set: Setter, get: () => ChatState, event: ChatEvent) {
  switch (event.type) {
    case 'run_started':
      set({ running: true, steps: [], error: null });
      break;

    case 'node_start':
      set((s) => ({
        steps: [...s.steps, { node: event.node, label: event.label, status: 'running' }],
      }));
      break;

    case 'node_end':
      set((s) => ({
        steps: s.steps.map((step) =>
          step.node === event.node && step.status === 'running'
            ? { ...step, status: 'done' as const }
            : step,
        ),
      }));
      break;

    case 'progress':
      set((s) => {
        const steps = [...s.steps];
        for (let i = steps.length - 1; i >= 0; i -= 1) {
          if (steps[i].node === event.node) {
            steps[i] = { ...steps[i], progress: event };
            break;
          }
        }
        return { steps };
      });
      break;

    case 'message':
      set((s) =>
        s.messages.some((m) => m.id === event.message_id)
          ? {}
          : {
              messages: [
                ...s.messages,
                { id: event.message_id, role: event.role, content: event.content },
              ],
            },
      );
      break;

    case 'interrupt':
      set({
        pending: { prompt: event.prompt, options: event.options, kind: event.kind },
      });
      break;

    case 'graph_proposed':
      set({
        proposal: {
          thread_id: get().threadId ?? '',
          graph_id: null,
          jdm: event.jdm,
          tests: event.tests,
          usecase_name: event.usecase_name,
          base_version: event.base_version,
          report: event.test_report,
        },
      });
      break;

    case 'test_report':
      set({ testReport: event.report });
      break;

    case 'error':
      set({ error: event.message, running: false });
      break;

    case 'done':
      set((s) => ({
        running: false,
        steps: s.steps.map((step) => ({ ...step, status: 'done' as const })),
      }));
      break;
  }
}

function describe(error: unknown): string {
  if (error instanceof AppError) return error.message;
  return error instanceof Error ? error.message : 'Something went wrong.';
}
