'use client';

import type { DecisionGraphType } from '@gorules/jdm-editor';
import { FlaskConical, MessagesSquare, ShieldCheck } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';

import { ProblemsPanel } from '../editor/ProblemsPanel';
import { TestRunnerPanel } from '../editor/TestRunnerPanel';
import { useChatStore } from '../../stores/useChatStore';
import { useUiStore, type AssistantTab } from '../../stores/useUiStore';
import { cx } from '../ui';
import { ChatPane } from './ChatPane';

type Props = {
  canvas: DecisionGraphType;
  graphId: string | null;
  graphName: string | null;
};

const TABS: { id: AssistantTab; label: string; icon: typeof MessagesSquare }[] = [
  { id: 'chat', label: 'Chat', icon: MessagesSquare },
  { id: 'tests', label: 'Tests', icon: FlaskConical },
  { id: 'problems', label: 'Problems', icon: ShieldCheck },
];

/**
 * The assistant pane: a conversation, a test runner and a linter, side by side with the
 * canvas rather than underneath it.
 *
 * Tests and Problems used to live in the editor's own panel rail, alongside the simulator.
 * They belong here because they answer "is this right?", which is the question the
 * conversation is about; the simulator answers "what does this do with these inputs?",
 * which only makes sense beside the graph it lights up. So it stays where it was.
 *
 * Two things this has to get right, both of them about *not* unmounting.
 *
 * The chat is always mounted and merely hidden, because it holds a live server-sent event
 * stream. The editor's rail unmounts whichever panel is not selected - which is exactly
 * why the chat was never put in it - and switching to Tests mid-build would otherwise drop
 * the connection and lose the rest of the run.
 *
 * The other two are mounted on first visit and kept from then on. Mounting all three up
 * front would make `ProblemsPanel` lint every graph the moment it loads, whether or not
 * anyone asks; unmounting them on the way out would re-lint on every visit and throw away
 * the report the user is switching back and forth to read.
 */
export function AssistantPane({ canvas, graphId, graphName }: Props) {
  const { assistantTab, setAssistantTab } = useUiStore();
  const running = useChatStore((s) => s.running);
  const messageCount = useChatStore((s) => s.messages.length);

  // Which tabs have ever been opened. Never shrinks - see the note above.
  const [visited, setVisited] = useState<Set<AssistantTab>>(() => new Set(['chat']));
  useEffect(() => {
    setVisited((seen) => (seen.has(assistantTab) ? seen : new Set(seen).add(assistantTab)));
  }, [assistantTab]);

  // Something arrived in the conversation while the reader was looking elsewhere. Without
  // this the tabs make it possible to miss a reply entirely, which the single pane never
  // could.
  const [unread, setUnread] = useState(false);
  const lastSeen = useRef(messageCount);
  useEffect(() => {
    if (assistantTab === 'chat') {
      lastSeen.current = messageCount;
      setUnread(false);
    } else if (messageCount > lastSeen.current) {
      setUnread(true);
    }
  }, [assistantTab, messageCount]);

  return (
    <div className="flex h-full flex-col border-l border-border bg-bg">
      <div role="tablist" aria-label="Assistant" className="flex h-9 shrink-0 border-b border-border">
        {TABS.map(({ id, label, icon: Icon }) => {
          const active = assistantTab === id;
          return (
            <button
              key={id}
              role="tab"
              aria-selected={active}
              onClick={() => setAssistantTab(id)}
              className={cx(
                'relative flex flex-1 items-center justify-center gap-1.5 border-b-2 px-2',
                'text-xs font-medium transition-colors',
                active
                  ? 'border-accent text-accent'
                  : 'border-transparent text-fg-subtle hover:text-fg',
              )}
            >
              <Icon size={13} />
              {label}
              {id === 'chat' && (unread || (running && !active)) ? (
                <span
                  aria-label={running ? 'The assistant is working' : 'New reply'}
                  className={cx(
                    'absolute right-2 top-2 h-1.5 w-1.5 rounded-full bg-accent',
                    running && 'animate-pulse',
                  )}
                />
              ) : null}
            </button>
          );
        })}
      </div>

      {/* Hidden, not unmounted. `display: none` keeps the event stream and the scroll
          position; removing the node from the tree would end the run's connection. */}
      <div className={cx('min-h-0 flex-1', assistantTab !== 'chat' && 'hidden')}>
        <ChatPane
          canvas={canvas}
          graphId={graphId}
          graphName={graphName}
          visible={assistantTab === 'chat'}
        />
      </div>

      {visited.has('tests') ? (
        <div className={cx('min-h-0 flex-1 overflow-auto', assistantTab !== 'tests' && 'hidden')}>
          <TestRunnerPanel />
        </div>
      ) : null}

      {visited.has('problems') ? (
        <div
          className={cx('min-h-0 flex-1 overflow-auto', assistantTab !== 'problems' && 'hidden')}
        >
          <ProblemsPanel />
        </div>
      ) : null}
    </div>
  );
}
