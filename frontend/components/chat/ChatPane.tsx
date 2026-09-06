'use client';

import type { DecisionGraphType } from '@gorules/jdm-editor';
import { AlertCircle, ArrowUp, Sparkles, Square } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';

import { useChatStore } from '../../stores/useChatStore';
import { useGraphStore } from '../../stores/useGraphStore';
import { Button, EmptyState, cx } from '../ui';
import { MarkdownMessage } from './MarkdownMessage';
import { ProgressRail } from './ProgressRail';
import { StopRunDialog } from './StopRunDialog';

type Props = {
  canvas: DecisionGraphType;
  graphId: string | null;
  graphName: string | null;
  /** False while another assistant tab is in front. The pane stays mounted either way -
   *  it holds the run's event stream - but a hidden element has no scroll height, so the
   *  view has to be taken back to the newest message when it returns. */
  visible?: boolean;
};

// An empty canvas can only be built on; the rest need a graph to act against.
const NEW_POLICY_SUGGESTIONS = [
  'Create a ticket discount policy: students 20% off, seniors 25%, members 10%',
  'Build a shipping fee policy: free over $50, $6 under, $12 to PO boxes',
  'Draft a refund policy based on order age and customer tier',
];

// Worded to match the intent router's own patterns, so each one lands on the node it
// names without a model call to work out what was meant. The linter was missing here
// entirely: reachable in conversation since it was built, and never once offered.
const EXISTING_POLICY_SUGGESTIONS = [
  'Explain what this policy does',
  'Run the test suite',
  'Check this policy for problems',
  'Add a rule for VIP customers',
];

export function ChatPane({ canvas, graphId, graphName, visible = true }: Props) {
  const { messages, steps, pending, proposal, running, error, suggestions, send, respond, cancel } =
    useChatStore();
  // Generating a test suite is the one model call outside this conversation, and there is
  // one API key behind both. Waiting for it costs a few seconds; racing it costs a request
  // out of the daily allowance and usually a 429 for whichever call loses.
  const generatingTests = useGraphStore((s) => s.generatingTests);
  const [draft, setDraft] = useState('');
  const [confirmingStop, setConfirmingStop] = useState(false);
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
    if (!visible) return;
    // `auto` rather than `smooth` on the way back in: the reader has already missed
    // whatever arrived, and watching it scroll there is slower than being there.
    scrollRef.current?.scrollTo({
      top: scrollRef.current.scrollHeight,
      behavior: visible && messages.length ? 'smooth' : 'auto',
    });
  }, [messages, steps, pending, visible]);

  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  }, [draft]);

  const submit = () => {
    const text = draft.trim();
    if (!text || running || generatingTests) return;
    setDraft('');
    if (pending?.kind === 'text') void respond(text, canvasPayload);
    else void send(text, canvasPayload);
  };

  const composerDisabled = running || generatingTests || pending?.kind === 'choice';

  return (
    // The border and the header belong to `AssistantPane` now: this is one tab of three,
    // not the whole pane.
    <div className="flex h-full flex-col">
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

        {/* What to do next. Held back while the agent is working, while it is waiting on
            an answer of its own, and while a proposal is still under review - in all three
            the next move is already decided and a second set of choices only competes
            with it. */}
        {suggestions.length > 0 && !running && !pending && !proposal ? (
          <div className="flex flex-wrap gap-1.5 pt-0.5">
            {suggestions.map((suggestion) => (
              <button
                key={suggestion.label}
                onClick={() => {
                  if (suggestion.send) {
                    void send(suggestion.prompt, canvasPayload);
                    return;
                  }
                  // Not a complete request on its own: put it in the composer and let the
                  // user finish the sentence.
                  setDraft(suggestion.prompt);
                  textareaRef.current?.focus();
                }}
                className={cx(
                  'rounded-full border border-border px-2.5 py-1 text-2xs text-fg-muted',
                  'transition-colors hover:border-accent hover:bg-accent-subtle hover:text-accent',
                )}
              >
                {suggestion.label}
              </button>
            ))}
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
                  : generatingTests
                    ? 'Waiting for the test generator to finish…'
                    : 'Describe a change, or ask a question'
            }
            className="max-h-40 flex-1 resize-none bg-transparent px-1.5 py-1 text-sm outline-none placeholder:text-fg-subtle disabled:opacity-60"
          />
          {running ? (
            <Button
              size="sm"
              variant="ghost"
              icon={<Square size={12} />}
              onClick={() => setConfirmingStop(true)}
            >
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

      <StopRunDialog
        open={confirmingStop}
        steps={steps}
        hasProposal={Boolean(proposal)}
        onClose={() => setConfirmingStop(false)}
        onConfirm={() => {
          setConfirmingStop(false);
          void cancel();
        }}
      />
    </div>
  );
}
