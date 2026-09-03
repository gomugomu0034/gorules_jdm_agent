'use client';

import type { DecisionGraphType } from '@gorules/jdm-editor';
import { AlertCircle, ArrowUp, Sparkles, Square } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';

import { useChatStore } from '../../stores/useChatStore';
import { Button, EmptyState, cx } from '../ui';
import { MarkdownMessage } from './MarkdownMessage';
import { ProgressRail } from './ProgressRail';

type Props = {
  canvas: DecisionGraphType;
  graphId: string | null;
  graphName: string | null;
};

// An empty canvas can only be built on; the rest need a graph to act against.
const NEW_POLICY_SUGGESTIONS = [
  'Create a ticket discount policy: students 20% off, seniors 25%, members 10%',
  'Build a shipping fee policy: free over $50, $6 under, $12 to PO boxes',
  'Draft a refund policy based on order age and customer tier',
];

const EXISTING_POLICY_SUGGESTIONS = [
  'Explain what this policy does',
  'Run the test suite',
  'Add a rule for VIP customers',
];

export function ChatPane({ canvas, graphId, graphName }: Props) {
  const { messages, steps, pending, running, error, send, respond, cancel } = useChatStore();
  const [draft, setDraft] = useState('');
  const scrollRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // The live canvas travels with every turn, so the agent reasons about the
  // graph as it stands right now - unsaved edits included.
  const canvasPayload = { content: canvas, graph_id: graphId, name: graphName };

  // With nothing on the canvas the only sensible request is to build something,
  // which is also how the agent's own intent router reads it. A new policy
  // starts from an input/output skeleton, so "blank" means nothing beyond that
  // rather than literally zero nodes.
  const isBlank = (canvas?.nodes ?? []).every(
    (node) => node.type === 'inputNode' || node.type === 'outputNode',
  );

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' });
  }, [messages, steps, pending]);

  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  }, [draft]);

  const submit = () => {
    const text = draft.trim();
    if (!text || running) return;
    setDraft('');
    if (pending?.kind === 'text') void respond(text, canvasPayload);
    else void send(text, canvasPayload);
  };

  const composerDisabled = running || pending?.kind === 'choice';

  return (
    <div className="flex h-full flex-col border-l border-border bg-bg">
      <div className="flex h-9 shrink-0 items-center gap-1.5 border-b border-border px-3">
        <Sparkles size={13} className="text-accent" />
        <span className="text-xs font-semibold uppercase tracking-wide text-fg-subtle">
          Assistant
        </span>
      </div>

      <div ref={scrollRef} className="min-h-0 flex-1 space-y-3 overflow-y-auto px-3 py-3">
        {messages.length === 0 && !running ? (
          <EmptyState
            icon={<Sparkles size={20} />}
            title={isBlank ? 'Describe a policy' : 'Ask for a change'}
            description={
              isBlank
                ? 'Say what the rules should be. The assistant builds the graph and opens it on the canvas for you to review.'
                : 'Describe what this policy should do, and review the proposed graph before accepting it.'
            }
            action={
              <div className="flex flex-col gap-1.5">
                {(isBlank ? NEW_POLICY_SUGGESTIONS : EXISTING_POLICY_SUGGESTIONS).map((s) => (
                  <button
                    key={s}
                    onClick={() => void send(s, canvasPayload)}
                    className="rounded border border-border px-2.5 py-1.5 text-left text-xs text-fg-muted hover:bg-bg-subtle hover:text-fg"
                  >
                    {s}
                  </button>
                ))}
              </div>
            }
          />
        ) : null}

        {messages.map((message) =>
          message.role === 'user' ? (
            <div key={message.id} className="flex justify-end">
              <div className="max-w-[85%] rounded-lg bg-bg-inset px-3 py-2 text-sm">
                {message.content}
              </div>
            </div>
          ) : (
            // Assistant replies run full width: they carry tables and JSON
            // blocks that a bubble would squeeze.
            <MarkdownMessage key={message.id} content={message.content} />
          ),
        )}

        {steps.length > 0 ? <ProgressRail steps={steps} running={running} /> : null}

        {pending ? (
          <div className="rounded-lg border border-border bg-bg-subtle p-3">
            <MarkdownMessage content={pending.prompt} />
            {pending.kind === 'choice' ? (
              <div className="mt-2.5 flex flex-wrap gap-1.5">
                {pending.options.map((option) => (
                  <button
                    key={option}
                    // Echo the option verbatim: the agent compares these
                    // strings literally, emoji included.
                    onClick={() => void respond(option, canvasPayload)}
                    disabled={running}
                    className={cx(
                      'rounded border border-border-strong bg-bg px-2.5 py-1.5 text-xs font-medium',
                      'transition-colors hover:border-accent hover:bg-accent-subtle hover:text-accent',
                      'disabled:opacity-50',
                    )}
                  >
                    {option}
                  </button>
                ))}
              </div>
            ) : (
              <p className="mt-2 text-xs text-fg-subtle">Type your answer below.</p>
            )}
          </div>
        ) : null}

        {error ? (
          <div className="flex items-start gap-2 rounded border border-border bg-danger-subtle p-2.5 text-xs text-danger">
            <AlertCircle size={14} className="mt-0.5 shrink-0" />
            <span>{error}</span>
          </div>
        ) : null}
      </div>

      <div className="shrink-0 border-t border-border p-2.5">
        <div className="flex items-end gap-1.5 rounded-lg border border-border bg-bg p-1.5 focus-within:border-accent">
          <textarea
            ref={textareaRef}
            rows={1}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && (e.metaKey || e.ctrlKey || !e.shiftKey)) {
                e.preventDefault();
                submit();
              }
            }}
            disabled={composerDisabled}
            placeholder={
              pending?.kind === 'choice'
                ? 'Choose an option above'
                : running
                  ? 'Working…'
                  : 'Describe a change, or ask a question'
            }
            className="max-h-40 flex-1 resize-none bg-transparent px-1.5 py-1 text-sm outline-none placeholder:text-fg-subtle disabled:opacity-60"
          />
          {running ? (
            <Button size="sm" variant="ghost" icon={<Square size={12} />} onClick={() => void cancel()}>
              Stop
            </Button>
          ) : (
            <Button
              size="sm"
              variant="primary"
              onClick={submit}
              disabled={!draft.trim() || composerDisabled}
              className="h-7 w-7 !px-0"
              aria-label="Send"
            >
              <ArrowUp size={14} />
            </Button>
          )}
        </div>
        <p className="mt-1 px-1 text-2xs text-fg-subtle">
          Enter to send · Shift+Enter for a new line
        </p>
      </div>
    </div>
  );
}
