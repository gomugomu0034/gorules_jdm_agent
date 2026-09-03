import type { DecisionGraphType } from '@gorules/jdm-editor';

export type GraphSummary = {
  id: string;
  name: string;
  slug: string;
  description: string;
  current_version: number;
  node_count: number;
  test_count: number;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
};

export type GraphDetail = GraphSummary & {
  content: DecisionGraphType;
  version: number;
};

export type VersionSummary = {
  version: number;
  message: string;
  author: 'user' | 'agent' | 'import';
  is_autosave: boolean;
  thread_id: string | null;
  created_at: string;
  node_count: number;
};

export type ValidationIssue = {
  path: string;
  message: string;
  severity: 'error' | 'warning';
};

export type TestCase = {
  id?: string | null;
  name: string;
  input: unknown;
  expectedOutput: unknown;
  enabled?: boolean;
  order?: number;
};

export type Mismatch = {
  path: string;
  expected: unknown;
  actual: unknown;
};

export type TestResult = {
  test_id: string | null;
  name: string;
  status: 'passed' | 'failed' | 'errored' | 'skipped';
  input: unknown;
  expected: unknown;
  actual: unknown;
  mismatches: Mismatch[];
  performance: string | null;
  trace: Record<string, unknown>;
  error: string | null;
};

export type TestRunReport = {
  summary: {
    total: number;
    passed: number;
    failed: number;
    errored: number;
    skipped: number;
    duration_ms: number;
    compile_error?: string;
  };
  results: TestResult[];
};

export type SimulationResponse = {
  result?: {
    performance: string | null;
    result: unknown;
    snapshot: DecisionGraphType;
    trace: Record<string, unknown>;
  };
  error?: {
    title?: string;
    message?: string;
    data: { nodeId?: string };
  };
};

export type ChatRole = 'user' | 'assistant';

export type ChatMessage = {
  id: string;
  role: ChatRole;
  content: string;
};

export type PendingInterrupt = {
  prompt: string;
  options: string[];
  kind: 'choice' | 'text';
};

export type Proposal = {
  thread_id: string;
  graph_id: string | null;
  jdm: DecisionGraphType;
  tests: TestCase[];
  usecase_name: string;
  base_version: number | null;
  report: TestRunReport | null;
};

export type ThreadState = {
  id: string;
  graph_id: string | null;
  status: 'idle' | 'running' | 'awaiting_input' | 'error' | 'completed' | 'cancelled';
  messages: { role: ChatRole; content: string }[];
  pending_interrupt: PendingInterrupt | null;
  proposal: Proposal | null;
  last_seq: number;
};

export type ProgressEvent = {
  node: string;
  attempt: number;
  max_attempts: number;
  phase: 'llm' | 'parse' | 'compile' | 'evaluate';
  message: string;
};

/** Mirrors the backend's SSE event union. */
export type ChatEvent =
  | { seq: number; type: 'run_started'; run_id: string; thread_id: string }
  | { seq: number; type: 'node_start'; node: string; label: string; step: number; of: number }
  | { seq: number; type: 'node_end'; node: string; status: 'ok' | 'error' }
  | ({ seq: number; type: 'progress' } & ProgressEvent)
  | { seq: number; type: 'message'; role: ChatRole; content: string; message_id: string }
  | ({ seq: number; type: 'interrupt'; interrupt_id: string } & PendingInterrupt)
  | {
      seq: number;
      type: 'graph_proposed';
      jdm: DecisionGraphType;
      tests: TestCase[];
      usecase_name: string;
      base_version: number | null;
      test_report: TestRunReport | null;
    }
  | { seq: number; type: 'test_report'; report: TestRunReport; generated?: boolean }
  | { seq: number; type: 'error'; code: string; message: string; node?: string; recoverable: boolean }
  | {
      seq: number;
      type: 'done';
      status: 'awaiting_input' | 'completed' | 'error' | 'cancelled';
    };
