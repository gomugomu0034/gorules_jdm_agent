import type { DecisionGraphType } from '@gorules/jdm-editor';

import type {
  AcceptProposalResult,
  GraphDetail,
  GraphSummary,
  Proposal,
  Session,
  SimulationResponse,
  TestCase,
  LintReport,
  TestRunReport,
  ThreadState,
  ValidationIssue,
  VersionSummary,
} from './types';

/**
 * The browser talks to FastAPI directly rather than through Next's rewrite:
 * the rewrite proxy buffers responses, which would defeat SSE progress.
 */
export const API_BASE =
  process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, '') || 'http://localhost:8000';

export class AppError extends Error {
  code: string;
  status: number;
  detail: unknown;

  constructor(code: string, message: string, status: number, detail?: unknown) {
    super(message);
    this.name = 'AppError';
    this.code = code;
    this.status = status;
    this.detail = detail;
  }
}

export function apiUrl(path: string): string {
  return `${API_BASE}${path}`;
}

/**
 * A guest session is minted lazily by the server on the first request it sees
 * with no cookie. A cold page load fires several requests at once, and each
 * would mint a *different* guest - the browser keeps only the last cookie, so
 * anything created under a discarded identity becomes invisible (a thread
 * created that way 404s on the very next call).
 *
 * So the first call establishes the session on its own and everything else
 * waits behind it. Only the first load pays for this; afterwards the cookie
 * exists and the promise is already resolved.
 */
let sessionReady: Promise<void> | null = null;

function ensureSession(): Promise<void> {
  if (!sessionReady) {
    sessionReady = fetch(apiUrl('/api/auth/me'), { credentials: 'include' })
      .then(() => undefined)
      // A failure here must not block the app: the request that follows will
      // surface the real error.
      .catch(() => undefined);
  }
  return sessionReady;
}

/** Forget the established session, e.g. after signing in or out. */
export function resetSession(): void {
  sessionReady = null;
}

async function request<T>(
  path: string,
  init?: RequestInit,
  opts: { bootstrap?: boolean } = {},
): Promise<T> {
  // `bootstrap: false` is for callers that must not wait on the session gate.
  if (opts.bootstrap !== false) await ensureSession();

  let response: Response;
  try {
    response = await fetch(apiUrl(path), {
      ...init,
      // The session lives in a cookie, so every call must carry it. Without
      // this each request would arrive as a brand-new guest.
      credentials: 'include',
      headers: {
        ...(init?.body && !(init.body instanceof FormData)
          ? { 'Content-Type': 'application/json' }
          : {}),
        ...init?.headers,
      },
    });
  } catch (cause) {
    throw new AppError(
      'NETWORK_ERROR',
      `Cannot reach the API at ${API_BASE}. Is the backend running?`,
      0,
      cause,
    );
  }

  if (response.status === 204) return undefined as T;

  const text = await response.text();
  const payload = text ? safeParse(text) : null;

  if (!response.ok) {
    const err = (payload as { error?: { code: string; message: string; detail?: unknown } })?.error;
    throw new AppError(
      err?.code ?? 'HTTP_ERROR',
      err?.message ?? `Request failed with status ${response.status}.`,
      response.status,
      err?.detail,
    );
  }

  return payload as T;
}

function safeParse(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

const json = (body: unknown): RequestInit => ({ body: JSON.stringify(body) });

export const api = {
  health: () => request<{ status: string; db: string; zen: string }>('/health'),

  // Graphs -----------------------------------------------------------------
  listGraphs: (q?: string) =>
    request<{ graphs: GraphSummary[] }>(`/api/graphs${q ? `?q=${encodeURIComponent(q)}` : ''}`),

  getGraph: (id: string, version?: number) =>
    request<GraphDetail>(`/api/graphs/${id}${version ? `?version=${version}` : ''}`),

  createGraph: (name: string, content?: DecisionGraphType) =>
    request<GraphDetail>('/api/graphs', { method: 'POST', ...json({ name, content }) }),

  saveGraph: (
    id: string,
    content: DecisionGraphType,
    opts: { message?: string; baseVersion?: number; autosave?: boolean } = {},
  ) =>
    request<{ graph: GraphDetail; version: number }>(`/api/graphs/${id}`, {
      method: 'PUT',
      ...json({
        content,
        message: opts.message ?? '',
        base_version: opts.baseVersion,
        autosave: opts.autosave ?? false,
      }),
    }),

  renameGraph: (id: string, name: string) =>
    request<GraphDetail>(`/api/graphs/${id}`, { method: 'PATCH', ...json({ name }) }),

  archiveGraph: (id: string) => request<void>(`/api/graphs/${id}`, { method: 'DELETE' }),

  validate: (content: DecisionGraphType) =>
    request<{ valid: boolean; errors: ValidationIssue[] }>('/api/graphs/validate', {
      method: 'POST',
      ...json({ content }),
    }),

  // Versions ---------------------------------------------------------------
  listVersions: (id: string) =>
    request<{ versions: VersionSummary[] }>(`/api/graphs/${id}/versions`),

  getVersion: (id: string, version: number) =>
    request<{ version: number; content: DecisionGraphType; message: string; author: string; created_at: string }>(
      `/api/graphs/${id}/versions/${version}`,
    ),

  restoreVersion: (id: string, version: number) =>
    request<{ graph: GraphDetail; version: number }>(
      `/api/graphs/${id}/versions/${version}/restore`,
      { method: 'POST' },
    ),

  // Import -----------------------------------------------------------------
  importGraph: (file: File, name?: string) => {
    const form = new FormData();
    form.append('file', file);
    if (name) form.append('name', name);
    return request<GraphDetail & { tests_imported: number }>('/api/graphs/import', {
      method: 'POST',
      body: form,
    });
  },

  // Simulation and tests ---------------------------------------------------
  simulate: (content: DecisionGraphType, context: unknown) =>
    request<SimulationResponse>('/api/simulate', {
      method: 'POST',
      ...json({ content, context, trace: true }),
    }),

  listTests: (id: string) => request<{ tests: TestCase[] }>(`/api/graphs/${id}/tests`),

  replaceTests: (id: string, tests: TestCase[]) =>
    request<{ tests: TestCase[] }>(`/api/graphs/${id}/tests`, { method: 'PUT', ...json({ tests }) }),

  runTests: (id: string, content?: DecisionGraphType) =>
    request<TestRunReport>(`/api/graphs/${id}/tests/run`, { method: 'POST', ...json({ content }) }),

  lintGraph: (id: string, content?: DecisionGraphType) =>
    request<LintReport>(`/api/graphs/${id}/lint`, { method: 'POST', ...json({ content }) }),

  // A draft has no graph row yet, so it is linted by content alone.
  lintContent: (content: DecisionGraphType) =>
    request<LintReport>('/api/lint', { method: 'POST', ...json({ content }) }),

  runAdhocTests: (content: DecisionGraphType, tests: TestCase[]) =>
    request<TestRunReport>('/api/tests/run', { method: 'POST', ...json({ content, tests }) }),

  generateTests: (id: string) =>
    request<{ tests: TestCase[] }>(`/api/graphs/${id}/tests/generate`, { method: 'POST' }),

  // Chat -------------------------------------------------------------------
  createThread: (graphId?: string | null) =>
    request<{ id: string }>('/api/chat/threads', { method: 'POST', ...json({ graph_id: graphId }) }),

  getThread: (threadId: string) => request<ThreadState>(`/api/chat/threads/${threadId}`),

  sendMessage: (
    threadId: string,
    text: string,
    canvas: { content: DecisionGraphType; graph_id?: string | null; name?: string | null },
  ) =>
    request<{ run_id: string }>(`/api/chat/threads/${threadId}/messages`, {
      method: 'POST',
      ...json({ text, canvas }),
    }),

  resume: (
    threadId: string,
    value: string,
    canvas?: { content: DecisionGraphType; graph_id?: string | null; name?: string | null },
  ) =>
    request<{ run_id: string }>(`/api/chat/threads/${threadId}/resume`, {
      method: 'POST',
      ...json({ value, canvas }),
    }),

  cancelRun: (threadId: string) =>
    request<{ cancelled: boolean }>(`/api/chat/threads/${threadId}/cancel`, { method: 'POST' }),

  /**
   * Take the agent's proposal.
   *
   * With `persist` false and no target graph the server returns the content as
   * an unsaved draft, so it can be shown on the canvas and tested before the
   * user commits to keeping it.
   */
  acceptProposal: (
    threadId: string,
    graphId?: string | null,
    name?: string,
    persist = false,
  ) =>
    request<AcceptProposalResult>(`/api/chat/threads/${threadId}/proposal/accept`, {
      method: 'POST',
      ...json({ graph_id: graphId, name, persist }),
    }),

  rejectProposal: (threadId: string, reason: string) =>
    request<{ rejected: boolean }>(`/api/chat/threads/${threadId}/proposal/reject`, {
      method: 'POST',
      ...json({ reason }),
    }),

  getProposal: (threadId: string) =>
    request<ThreadState>(`/api/chat/threads/${threadId}`).then((s) => s.proposal as Proposal | null),

  // Session ----------------------------------------------------------------
  me: () => request<Session>('/api/auth/me'),

  login: (email: string, password: string) =>
    request<Session>('/api/auth/login', { method: 'POST', ...json({ email, password }) }),

  logout: () => request<Session>('/api/auth/logout', { method: 'POST' }),
};
