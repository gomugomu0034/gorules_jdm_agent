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
  // Findings live in the store rather than here, because the agent produces them too: a
  // LINT turn puts its findings straight into this panel, which a privately owned piece of
  // state could not be told about.
  const { graph, content, revision, lintFindings: findings, lintRevision: checkedRevision,
          setLint } = useGraphStore();
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
      // Stamped with the graph it describes: findings name specific nodes, and pointing at
      // a node the user has since deleted is worse than saying nothing.
      setLint(report.findings, revision);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not check this policy.');
    } finally {
      setRunning(false);
    }
  }, [graph, content, revision, setLint]);

  const stale = findings !== null && checkedRevision !== revision;

  // Check once when the panel is first opened, and only if nothing has already checked
  // this version of the graph - the agent puts its own findings here on a LINT turn, and
  // re-running would throw away the answer the user just asked for to compute it again.
  // Not re-run on every keystroke either: linting compiles the graph, and a half-typed
  // expression is not a finding worth interrupting anyone with.
  useEffect(() => {
    if (findings === null || checkedRevision !== revision) void check();
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
        {stale ? (
          <div className="border-b border-border bg-warning-subtle px-3 py-2 text-2xs text-warning">
            The graph has changed since this check. Run it again for current findings.
          </div>
        ) : null}

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
