'use client';

import type { DecisionGraphType } from '@gorules/jdm-editor';
import { Check, Download, GitCompare, X } from 'lucide-react';
import { useMemo, useState } from 'react';

import { downloadProposal } from '../../lib/download';
import type { Proposal } from '../../lib/types';
import { Badge, Button, cx } from '../ui';

type Props = {
  proposal: Proposal;
  current: DecisionGraphType;
  threadId: string | null;
  onAccept: () => Promise<void> | void;
  onReject: (reason: string) => Promise<void> | void;
};

/** Node-level counts, so the bar states plainly what is about to change. */
function summarise(current: DecisionGraphType, proposed: DecisionGraphType) {
  const before = new Map((current.nodes ?? []).map((n) => [n.id, n]));
  const after = new Map((proposed.nodes ?? []).map((n) => [n.id, n]));

  let added = 0;
  let modified = 0;
  after.forEach((node, id) => {
    if (!before.has(id)) added += 1;
    else if (JSON.stringify(before.get(id)) !== JSON.stringify(node)) modified += 1;
  });
  const removed = [...before.keys()].filter((id) => !after.has(id)).length;

  return { added, removed, modified };
}

export function DiffReviewBar({ proposal, current, threadId, onAccept, onReject }: Props) {
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);

  const counts = useMemo(() => summarise(current, proposal.jdm), [current, proposal.jdm]);
  const report = proposal.report;
  const testsPass = report ? report.summary.passed === report.summary.total : null;

  const act = async (fn: () => Promise<void> | void) => {
    setBusy(true);
    try {
      await fn();
    } finally {
      setBusy(false);
      setRejecting(false);
      setReason('');
    }
  };

  return (
    <div className="border-b border-border bg-accent-subtle px-3 py-2 animate-slide-up">
      <div className="flex flex-wrap items-center gap-2">
        <GitCompare size={15} className="text-accent" />
        <span className="text-sm font-medium">
          The assistant proposed <span className="font-semibold">{proposal.usecase_name}</span>
        </span>

        <div className="flex items-center gap-1">
          {counts.added > 0 ? (
            <Badge tone="success">+{counts.added} added</Badge>
          ) : null}
          {counts.removed > 0 ? (
            <Badge tone="danger">−{counts.removed} removed</Badge>
          ) : null}
          {counts.modified > 0 ? (
            <Badge tone="warning">~{counts.modified} changed</Badge>
          ) : null}
          {report ? (
            <Badge tone={testsPass ? 'success' : 'danger'}>
              {report.summary.passed}/{report.summary.total} tests pass
            </Badge>
          ) : null}
        </div>

        <div className="flex-1" />

        {threadId ? (
          <Button
            size="sm"
            icon={<Download size={12} />}
            onClick={() => downloadProposal(threadId, 'bundle')}
          >
            Download
          </Button>
        ) : null}

        <Button
          size="sm"
          variant="danger"
          icon={<X size={12} />}
          onClick={() => setRejecting((v) => !v)}
          disabled={busy}
        >
          Reject
        </Button>

        <Button
          size="sm"
          variant="primary"
          icon={<Check size={12} />}
          onClick={() => act(onAccept)}
          loading={busy}
        >
          Accept
        </Button>
      </div>

      {rejecting ? (
        <div className="mt-2 flex items-center gap-2">
          <input
            autoFocus
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && act(() => onReject(reason))}
            placeholder="What should be different? (optional — this goes back to the assistant)"
            className={cx(
              'h-8 flex-1 rounded border border-border bg-bg px-2.5 text-sm',
              'placeholder:text-fg-subtle focus-ring',
            )}
          />
          <Button size="sm" onClick={() => act(() => onReject(reason))} loading={busy}>
            Send
          </Button>
        </div>
      ) : null}
    </div>
  );
}
