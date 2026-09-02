import type { DecisionGraphType } from '@gorules/jdm-editor';

import type {
  GraphDetail,
  GraphSummary,
  Proposal,
  SimulationResponse,
  TestCase,
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

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(apiUrl(path), {
      ...init,
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

  acceptProposal: (threadId: string, graphId?: string | null, name?: string) =>
    request<{ graph_id: string; version: number }>(
      `/api/chat/threads/${threadId}/proposal/accept`,
      { method: 'POST', ...json({ graph_id: graphId, name }) },
    ),

  rejectProposal: (threadId: string, reason: string) =>
    request<{ rejected: boolean }>(`/api/chat/threads/${threadId}/proposal/reject`, {
      method: 'POST',
      ...json({ reason }),
    }),

  getProposal: (threadId: string) =>
    request<ThreadState>(`/api/chat/threads/${threadId}`).then((s) => s.proposal as Proposal | null),
};
