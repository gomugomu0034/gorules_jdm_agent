'use client';

// Type-only: these are erased at compile time. Importing any *value* from
// @gorules/jdm-editor here would pull the whole editor into the server bundle,
// which touches `window` at module scope and 500s on a direct page load.
import type { DecisionGraphType, Simulation } from '@gorules/jdm-editor';
import dynamic from 'next/dynamic';
import { useRouter } from 'next/navigation';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Group, Panel, Separator, useDefaultLayout, useGroupRef } from 'react-resizable-panels';

import { api } from '../../lib/api';
import { useChatStore } from '../../stores/useChatStore';
import { useGraphStore } from '../../stores/useGraphStore';
import { useUiStore } from '../../stores/useUiStore';
import { ChatPane } from '../chat/ChatPane';
import { Sidebar } from '../shell/Sidebar';
import { TopBar } from '../shell/TopBar';
import { SaveGraphDialog } from '../shell/SaveGraphDialog';
import { UnsavedGuard } from '../shell/UnsavedGuard';
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

/**
 * The studio, in one of two modes.
 *
 * With a `graphId` it edits a stored policy. With `null` it is a *draft*: an
 * empty canvas beside the chat, which is where a visitor starts. A draft has
 * no row in the database until it is named and saved, so there is no autosave
 * and no version history until then.
 */
export function StudioClient({ graphId }: { graphId: string | null }) {
  const {
    graph, content, loading, isDraft, draftName,
    load, setContent, applyProposed, beginDraft, saveDraftAs, reset,
  } = useGraphStore();
  const { sidebarOpen, chatOpen } = useUiStore();
  const chat = useChatStore();
  const router = useRouter();

  const [historyOpen, setHistoryOpen] = useState(false);
  const [saveOpen, setSaveOpen] = useState(false);
  const [simulation, setSimulation] = useState<Simulation | undefined>();
  const [simulating, setSimulating] = useState(false);
  const [sidebarToken, setSidebarToken] = useState(0);

  // The panels actually rendered right now, in order. The sidebar and the chat are
  // toggleable, and the library requires the current set to be declared whenever panels
  // are conditional: "Panel ids must match the Panels rendered within the Group during
  // mount or the initial layout will be incorrect." Without it one storage key serves
  // every combination, so a layout saved with the chat closed is restored into a group
  // that has it open - and the group then never resolves a layout at all. Every panel
  // stays on its `defaultSize` fallback with `flexGrow: 0`, the size the browser paints
  // stops matching the size the library thinks it has, and the separator jams once the
  // stored value reaches a `minSize`/`maxSize` bound the visible layout is nowhere near.
  const panelIds = useMemo(
    () => [...(sidebarOpen ? ['sidebar'] : []), 'canvas', ...(chatOpen ? ['chat'] : [])],
    [sidebarOpen, chatOpen],
  );

  // Persists the pane sizes per browser. An explicit storage shim is required
  // because the hook defaults to `localStorage`, which does not exist during
  // the server render of a direct page load.
  const { defaultLayout, ...persistLayout } = useDefaultLayout({
    id: 'jdm-studio-layout',
    panelIds,
    storage: layoutStorage,
  });

  // The remembered layout is applied *after* mount rather than handed to the group as
  // `defaultLayout`, which is what the library documents. Passing it there leaves the
  // group holding a layout its own panels never receive: they stay on the `defaultSize`
  // fallback, which renders a percentage `flex-basis` and a `flexGrow` of 0. Dragging then
  // writes `flexGrow` over the top of that stale basis, so what the browser paints and
  // what the library thinks it has drift apart - and the separator stops moving as soon as
  // the stored value hits a bound the visible layout is nowhere near. Applying it once the
  // panels are subscribed is what makes them adopt it.
  const groupRef = useGroupRef();
  const restored = useRef<string | null>(null);

  useEffect(() => {
    const signature = panelIds.join(':');
    if (restored.current === signature) return;
    restored.current = signature;
    // Only a layout covering exactly the panels on screen can be applied; a stale one is
    // left alone rather than partially written.
    if (!defaultLayout || !panelIds.every((id) => typeof defaultLayout[id] === 'number')) return;

    // Deferred by a frame, and guarded. Toggling a pane changes `panelIds` before the
    // group has re-registered its own, and writing a layout for a set it has not adopted
    // yet throws - which, unguarded, took the whole studio down with an error overlay.
    // Restoring a remembered size is cosmetic; failing to must cost nothing more than the
    // panes keeping the size they already have.
    const frame = requestAnimationFrame(() => {
      try {
        groupRef.current?.setLayout(
          Object.fromEntries(panelIds.map((id) => [id, defaultLayout[id]])),
        );
      } catch {
        // The group holds a different set of panels than this layout describes.
      }
    });
    return () => cancelAnimationFrame(frame);
  }, [defaultLayout, panelIds, groupRef]);

  // The invariant everything above exists to hold: what the browser paints has to match
  // what the library thinks it has. When they drift, `minSize` and `maxSize` are still
  // enforced against *its* numbers, so the separator stops moving while the pane on screen
  // is nowhere near the bound - the "stuck to a size" this was reported as. Reverting the
  // fix above puts it 13 points out, and nothing about the page looks wrong until someone
  // tries to drag, which is why it needs an assertion rather than an eye.
  //
  // Checked on every layout change, because that is when drift appears and when the
  // authoritative layout is in hand. This repo has no browser-test harness, so the
  // assertion lives where it will actually run: every dev session. Stripped from
  // production builds.
  const onLayoutChanged = useCallback(
    (layout: Record<string, number>, meta: { isUserInteraction: boolean }) => {
      persistLayout.onLayoutChanged?.(layout, meta);
      if (process.env.NODE_ENV === 'production') return;
      requestAnimationFrame(() => {
        const total = document.querySelector<HTMLElement>('[data-group]')?.clientWidth;
        if (!total) return;
        const drifted = Object.entries(layout).flatMap(([id, expected]) => {
          const el = document.getElementById(id);
          if (!el) return [];
          const painted = (el.getBoundingClientRect().width / total) * 100;
          return Math.abs(painted - expected) > 1
            ? [`${id}: ${painted.toFixed(1)}% painted vs ${expected.toFixed(1)}% stored`]
            : [];
        });
        if (drifted.length) {
          console.error(
            '[studio] Panel layout has drifted from the layout the library is enforcing, so '
              + 'the separator will jam before the panes reach their limits:\n  '
              + drifted.join('\n  '),
          );
        }
      });
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [persistLayout.onLayoutChanged],
  );

  const openedThread = useRef<string | null>(null);

  useEffect(() => {
    if (graphId) void load(graphId);
    else reset();
  }, [graphId, load, reset]);

  useEffect(() => {
    // Guarded because React's StrictMode runs effects twice in development,
    // which would otherwise create two chat threads per page load.
    const key = graphId ?? '__draft__';
    if (openedThread.current === key) return;
    openedThread.current = key;
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

    if (!graphId) {
      // Nothing to write into yet: keep it on the canvas as a draft so it can
      // be reviewed and tested before the visitor commits to a name.
      const result = await api.acceptProposal(chat.threadId, null, proposal.usecase_name);
      beginDraft(result.content ?? jdm, result.name ?? proposal.usecase_name, result.tests ?? []);
      chat.clearProposal();
      return;
    }

    await api.acceptProposal(chat.threadId, graphId, proposal.usecase_name, true);
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
      void chat.send(reason, {
        content,
        graph_id: graphId,
        name: graph?.name ?? draftName ?? null,
      });
    }
  };

  const saveDraft = async (name: string) => {
    const id = await saveDraftAs(name);
    setSaveOpen(false);
    if (id) {
      setSidebarToken((t) => t + 1);
      router.push(`/graphs/${id}`);
    }
  };

  return (
    <div className="flex h-full flex-col">
      <TopBar
        onOpenHistory={() => setHistoryOpen(true)}
        onSaveDraft={() => setSaveOpen(true)}
      />

      <Group
        id="jdm-studio"
        groupRef={groupRef}
        orientation="horizontal"
        className="flex min-h-0 flex-1"
        {...persistLayout}
        onLayoutChanged={onLayoutChanged}
      >
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
                  name={graph?.name ?? draftName ?? undefined}
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
              <ChatPane
                canvas={content}
                graphId={graphId}
                graphName={graph?.name ?? draftName ?? null}
              />
            </Panel>
          </>
        ) : null}
      </Group>

      <VersionHistory open={historyOpen} onClose={() => setHistoryOpen(false)} />
      <SaveGraphDialog
        open={saveOpen}
        defaultName={draftName ?? 'Untitled policy'}
        onClose={() => setSaveOpen(false)}
        onSave={saveDraft}
      />
      <UnsavedGuard />
    </div>
  );
}

function ResizeHandle() {
  return (
    <Separator className="relative w-px shrink-0 bg-border transition-colors hover:bg-accent data-[dragging]:bg-accent" />
  );
}
