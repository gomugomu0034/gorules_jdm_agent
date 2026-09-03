'use client';

import {
  AlertTriangle,
  ChevronRight,
  CircleSlash,
  Play,
  Sparkles,
  XCircle,
  CheckCircle2,
} from 'lucide-react';
import { useState } from 'react';

import { api } from '../../lib/api';
import type { TestResult } from '../../lib/types';
import { useGraphStore } from '../../stores/useGraphStore';
import { Badge, Button, EmptyState, Spinner, cx } from '../ui';

const ICONS = {
  passed: <CheckCircle2 size={14} className="text-success" />,
  failed: <XCircle size={14} className="text-danger" />,
  errored: <AlertTriangle size={14} className="text-warning" />,
  skipped: <CircleSlash size={14} className="text-fg-subtle" />,
};

export function TestRunnerPanel() {
  const { graph, tests, testReport, runTests, saveTests, loadTests } = useGraphStore();
  const [running, setRunning] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);

  const run = async () => {
    setRunning(true);
    try {
      await runTests();
    } finally {
      setRunning(false);
    }
  };

  const generate = async () => {
    if (!graph) return;
    setGenerating(true);
    try {
      const { tests: generated } = await api.generateTests(graph.id);
      await saveTests(generated);
      await loadTests();
    } catch (e) {
      window.alert(e instanceof Error ? e.message : 'Could not generate tests.');
    } finally {
      setGenerating(false);
    }
  };

  const summary = testReport?.summary;

  return (
    <div className="flex h-full flex-col bg-bg">
      <div className="flex shrink-0 items-center gap-2 border-b border-border px-3 py-2">
        <Button
          size="sm"
          variant="primary"
          icon={<Play size={12} />}
          onClick={run}
          loading={running}
          disabled={tests.length === 0}
        >
          Run {tests.length > 0 ? `${tests.length} test${tests.length === 1 ? '' : 's'}` : 'tests'}
        </Button>
        <Button size="sm" icon={<Sparkles size={12} />} onClick={generate} loading={generating}>
          Generate
        </Button>

        {summary ? (
          <div className="ml-auto flex items-center gap-1">
            <Badge tone={summary.passed === summary.total ? 'success' : 'danger'}>
              {summary.passed}/{summary.total} passed
            </Badge>
            <span className="text-2xs text-fg-subtle">{summary.duration_ms}ms</span>
          </div>
        ) : null}
      </div>

      <div className="min-h-0 flex-1 overflow-auto">
        {summary?.compile_error ? (
          <div className="m-3 rounded border border-border bg-danger-subtle p-3 text-xs text-danger">
            <p className="font-semibold">This graph does not compile</p>
            <p className="mt-1 font-mono leading-relaxed">{summary.compile_error}</p>
          </div>
        ) : null}

        {tests.length === 0 ? (
          <EmptyState
            title="No test cases yet"
            description="Generate a suite from the current graph, or ask the assistant to write one."
          />
        ) : !testReport ? (
          <ul className="divide-y divide-border">
            {tests.map((test) => (
              <li key={test.id ?? test.name} className="px-3 py-2 text-sm text-fg-muted">
                {test.name}
              </li>
            ))}
          </ul>
        ) : (
          <ul className="divide-y divide-border">
            {testReport.results.map((result) => (
              <ResultRow
                key={result.test_id ?? result.name}
                result={result}
                expanded={expanded === (result.test_id ?? result.name)}
                onToggle={() =>
                  setExpanded((current) =>
                    current === (result.test_id ?? result.name)
                      ? null
                      : (result.test_id ?? result.name),
                  )
                }
              />
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function ResultRow({
  result,
  expanded,
  onToggle,
}: {
  result: TestResult;
  expanded: boolean;
  onToggle: () => void;
}) {
  return (
    <li>
      <button
        onClick={onToggle}
        className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-bg-subtle"
      >
        <ChevronRight
          size={13}
          className={cx('shrink-0 text-fg-subtle transition-transform', expanded && 'rotate-90')}
        />
        {ICONS[result.status]}
        <span className="min-w-0 flex-1 truncate text-sm">{result.name}</span>
        {result.performance ? (
          <span className="shrink-0 font-mono text-2xs text-fg-subtle">{result.performance}</span>
        ) : null}
      </button>

      {expanded ? (
        <div className="space-y-2 border-t border-border bg-bg-subtle px-3 py-2.5 text-xs">
          {result.error ? (
            <Field label="Error" tone="danger" value={result.error} />
          ) : null}

          {result.mismatches.length > 0 ? (
            <div>
              <p className="mb-1 font-semibold text-fg-muted">Mismatches</p>
              <ul className="space-y-1">
                {result.mismatches.map((m) => (
                  <li key={m.path} className="font-mono text-2xs leading-relaxed">
                    <span className="text-fg">{m.path}</span>
                    {': expected '}
                    <span className="text-success">{JSON.stringify(m.expected)}</span>
                    {', got '}
                    <span className="text-danger">{JSON.stringify(m.actual)}</span>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

          <Field label="Input" value={JSON.stringify(result.input, null, 2)} />
          <Field label="Actual output" value={JSON.stringify(result.actual, null, 2)} />

          {Object.keys(result.trace ?? {}).length > 0 ? (
            <details>
              <summary className="cursor-pointer font-semibold text-fg-muted">
                Node trace ({Object.keys(result.trace).length} nodes)
              </summary>
              <pre className="mt-1 max-h-56 overflow-auto rounded bg-bg p-2 font-mono text-2xs">
                {JSON.stringify(result.trace, null, 2)}
              </pre>
            </details>
          ) : null}
        </div>
      ) : null}
    </li>
  );
}

function Field({ label, value, tone }: { label: string; value: string; tone?: 'danger' }) {
  return (
    <div>
      <p className={cx('mb-1 font-semibold', tone === 'danger' ? 'text-danger' : 'text-fg-muted')}>
        {label}
      </p>
      <pre className="max-h-40 overflow-auto rounded bg-bg p-2 font-mono text-2xs leading-relaxed">
        {value}
      </pre>
    </div>
  );
}
