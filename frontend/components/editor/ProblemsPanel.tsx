'use client';

import { AlertTriangle, CheckCircle2, Info, Lightbulb, XCircle } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';

import { api } from '../../lib/api';
import type { LintFinding, LintSeverity } from '../../lib/types';
import { useGraphStore } from '../../stores/useGraphStore';
import { Button, EmptyState, Spinner, cx } from '../ui';

const ICONS: Record<LintSeverity, JSX.Element> = {
  error: <XCircle size={14} className="text-danger" />,
  warning: <AlertTriangle size={14} className="text-warning" />,
  hint: <Lightbulb size={14} className="text-fg-subtle" />,
};

const LABELS: Record<LintSeverity, string> = {
  error: 'Errors',
  warning: 'Warnings',
  hint: 'Suggestions',
};

const ORDER: LintSeverity[] = ['error', 'warning', 'hint'];

export function ProblemsPanel() {
  const { graph, content } = useGraphStore();
  const [findings, setFindings] = useState<LintFinding[] | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const check = useCallback(async () => {
    setRunning(true);
    setError(null);
    try {
      // A draft has no graph row, so it is checked by content alone.
      const report = graph
        ? await api.lintGraph(graph.id, content)
        : await api.lintContent(content);
      setFindings(report.findings);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not check this policy.');
    } finally {
      setRunning(false);
    }
  }, [graph, content]);

  // Check once when the panel is opened. It is not re-run on every keystroke:
  // linting compiles the graph, and a half-typed expression is not a finding
  // worth interrupting someone with.
  useEffect(() => {
    void check();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const counts = ORDER.map((s) => [s, (findings ?? []).filter((f) => f.severity === s).length] as const);
  const total = findings?.length ?? 0;

  return (
    <div className="flex h-full flex-col bg-bg">
      <div className="flex shrink-0 items-center gap-2 border-b border-border px-3 py-2">
        <Button size="sm" onClick={() => void check()} loading={running}>
          Check policy
        </Button>
        <div className="flex-1" />
        {findings ? (
          <span className="flex items-center gap-3 text-2xs text-fg-subtle">
            {counts.map(([severity, n]) => (
              <span key={severity} className={cx('flex items-center gap-1', n === 0 && 'opacity-40')}>
                {ICONS[severity]}
                {n}
              </span>
            ))}
          </span>
        ) : null}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {running && findings === null ? (
          <div className="flex items-center justify-center gap-2 py-10 text-sm text-fg-subtle">
            <Spinner className="h-4 w-4" /> Checking
          </div>
        ) : error ? (
          <EmptyState
            icon={<AlertTriangle size={18} />}
            title="Could not check this policy"
            description={error}
          />
        ) : total === 0 && findings !== null ? (
          <EmptyState
            icon={<CheckCircle2 size={18} className="text-success" />}
            title="Nothing to fix"
            description="This policy passes every check."
          />
        ) : (
          ORDER.map((severity) => {
            const group = (findings ?? []).filter((f) => f.severity === severity);
            if (group.length === 0) return null;
            return (
              <section key={severity}>
                <h3 className="sticky top-0 flex items-center gap-1.5 border-b border-border bg-bg-subtle px-3 py-1.5 text-2xs font-medium uppercase tracking-wide text-fg-subtle">
                  {ICONS[severity]}
                  {LABELS[severity]}
                  <span className="opacity-60">({group.length})</span>
                </h3>
                {group.map((finding, i) => (
                  <Finding key={`${finding.code}-${i}`} finding={finding} />
                ))}
              </section>
            );
          })
        )}
      </div>
    </div>
  );
}

function Finding({ finding }: { finding: LintFinding }) {
  return (
    <article className="border-b border-border px-3 py-2.5">
      <div className="flex items-baseline gap-2">
        <span className="shrink-0 font-mono text-2xs text-fg-subtle">{finding.code}</span>
        {finding.nodeName ? (
          <span className="truncate text-2xs text-accent">{finding.nodeName}</span>
        ) : null}
      </div>
      <p className="mt-1 text-sm leading-snug text-fg">{finding.message}</p>
      {finding.fix ? (
        <p className="mt-1.5 flex gap-1.5 text-2xs leading-snug text-fg-subtle">
          <Info size={12} className="mt-0.5 shrink-0" />
          {finding.fix}
        </p>
      ) : null}
    </article>
  );
}
