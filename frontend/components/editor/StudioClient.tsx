'use client';

// Type-only: these are erased at compile time. Importing any *value* from
// @gorules/jdm-editor here would pull the whole editor into the server bundle,
// which touches `window` at module scope and 500s on a direct page load.
import type { DecisionGraphType, Simulation } from '@gorules/jdm-editor';
import dynamic from 'next/dynamic';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Group, Panel, Separator, useDefaultLayout } from 'react-resizable-panels';

import { api } from '../../lib/api';
import { useChatStore } from '../../stores/useChatStore';
import { useGraphStore } from '../../stores/useGraphStore';
import { useUiStore } from '../../stores/useUiStore';
import { ChatPane } from '../chat/ChatPane';
import { Sidebar } from '../shell/Sidebar';
import { TopBar } from '../shell/TopBar';
import { VersionHistory } from '../shell/VersionHistory';
import { Spinner } from '../ui';
import { DiffReviewBar } from './DiffReviewBar';

// The editor pulls antd, react-flow, monaco and dnd, and touches `window` at
// module scope, so it must never be part of a server render.
const JdmEditorClient = dynamic(() => import('./JdmEditorClient'), {
  ssr: false,
  loading: () => (
    <div className="flex h-full items-center justify-center gap-2 text-sm text-fg-muted">
      <Spinner /> Loading editor…
    </div>
  ),
});

/**
 * SSR-safe wrapper around localStorage. Reads also tolerate a browser that
 * blocks site data, where the accessor itself throws.
 */
const layoutStorage = {
  getItem(key: string): string | null {
    if (typeof window === 'undefined') return null;
    try {
      return window.localStorage.getItem(key);
    } catch {
      return null;
    }
  },
  setItem(key: string, value: string): void {
    if (typeof window === 'undefined') return;
    try {
      window.localStorage.setItem(key, value);
    } catch {
      // Private browsing; layout simply is not remembered.
    }
  },
};

export function StudioClient({ graphId }: { graphId: string }) {
  const { graph, content, loading, load, setContent, applyProposed } = useGraphStore();
  const { sidebarOpen, chatOpen } = useUiStore();
  const chat = useChatStore();

  const [historyOpen, setHistoryOpen] = useState(false);
  const [simulation, setSimulation] = useState<Simulation | undefined>();
  const [simulating, setSimulating] = useState(false);
  const [sidebarToken, setSidebarToken] = useState(0);

  // Persists the pane sizes per browser. An explicit storage shim is required
  // because the hook defaults to `localStorage`, which does not exist during
  // the server render of a direct page load.
  const layout = useDefaultLayout({ id: 'jdm-studio-layout', storage: layoutStorage });
  const openedThread = useRef<string | null>(null);

  useEffect(() => {
    void load(graphId);
  }, [graphId, load]);

  useEffect(() => {
    // Guarded because React's StrictMode runs effects twice in development,
    // which would otherwise create two chat threads per page load.
    if (openedThread.current === graphId) return;
    openedThread.current = graphId;
    void chat.open(graphId);
    return () => {
      openedThread.current = null;
      chat.reset();
    };
    // A fresh thread per graph; the store tears the old stream down itself.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [graphId]);

  const proposal = chat.proposal;
  const inDiffMode = Boolean(proposal);

  const simulate = useCallback(
    async ({ graph: g, context }: { graph: DecisionGraphType; context: unknown }) => {
      setSimulating(true);
      try {
        const response = await api.simulate(g, context);
        setSimulation(response as Simulation);
      } catch (e) {
        setSimulation({
          error: {
            title: 'Simulation failed',
            message: e instanceof Error ? e.message : 'Unknown error',
            data: {},
          },
        } as Simulation);
      } finally {
        setSimulating(false);
      }
    },
    [],
  );

  const accept = async () => {
    if (!chat.threadId || !proposal) return;
    const jdm = proposal.jdm;
    await api.acceptProposal(chat.threadId, graphId, proposal.usecase_name);
    applyProposed(jdm);
    chat.clearProposal();
    await load(graphId);
    setSidebarToken((t) => t + 1);
  };

  const reject = async (reason: string) => {
    if (!chat.threadId) return;
    await api.rejectProposal(chat.threadId, reason);
    chat.clearProposal();
    if (reason.trim()) {
      void chat.send(reason, { content, graph_id: graphId, name: graph?.name ?? null });
    }
  };

  return (
    <div className="flex h-full flex-col">
      <TopBar onOpenHistory={() => setHistoryOpen(true)} />

      <Group orientation="horizontal" className="flex min-h-0 flex-1" {...layout}>
        {sidebarOpen ? (
          <>
            <Panel id="sidebar" defaultSize="17%" minSize="180px" maxSize="34%">
              <Sidebar activeId={graphId} refreshToken={sidebarToken} />
            </Panel>
            <ResizeHandle />
          </>
        ) : null}

        <Panel id="canvas" minSize="30%">
          <div className="flex h-full flex-col">
            {proposal ? (
              <DiffReviewBar
                proposal={proposal}
                current={content}
                threadId={chat.threadId}
                onAccept={accept}
                onReject={reject}
              />
            ) : null}

            <div className="min-h-0 flex-1">
              {loading ? (
                <div className="flex h-full items-center justify-center">
                  <Spinner />
                </div>
              ) : (
                <JdmEditorClient
                  value={content}
                  // The diff overlay is computed inside the client-only editor,
                  // which is where the editor's own diff helpers can be loaded.
                  proposed={proposal?.jdm}
                  onChange={setContent}
                  name={graph?.name}
                  disabled={inDiffMode}
                  simulation={simulation}
                  simulating={simulating}
                  onSimulate={simulate}
                  onClearSimulation={() => setSimulation(undefined)}
                />
              )}
            </div>
          </div>
        </Panel>

        {chatOpen ? (
          <>
            <ResizeHandle />
            <Panel id="chat" defaultSize="26%" minSize="320px" maxSize="45%">
              <ChatPane canvas={content} graphId={graphId} graphName={graph?.name ?? null} />
            </Panel>
          </>
        ) : null}
      </Group>

      <VersionHistory open={historyOpen} onClose={() => setHistoryOpen(false)} />
    </div>
  );
}

function ResizeHandle() {
  return (
    <Separator className="relative w-px shrink-0 bg-border transition-colors hover:bg-accent data-[dragging]:bg-accent" />
  );
}
