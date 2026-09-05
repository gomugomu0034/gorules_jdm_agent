'use client';

import type { DecisionGraphType } from '@gorules/jdm-editor';
import { create } from 'zustand';

import { api, AppError } from '../lib/api';
import type { GraphDetail, TestCase, TestRunReport, VersionSummary } from '../lib/types';

const AUTOSAVE_DELAY = 3000;

/**
 * The skeleton a new policy starts from, matching what the API creates for an
 * empty graph so a hand-drawn policy and a generated one look the same.
 */
const BLANK_GRAPH = {
  contentType: 'application/vnd.gorules.decision',
  nodes: [
    {
      id: 'input-node',
      name: 'Request',
      type: 'inputNode',
      position: { x: 120, y: 200 },
      content: { schema: '' },
    },
    {
      id: 'output-node',
      name: 'Response',
      type: 'outputNode',
      position: { x: 620, y: 200 },
      content: { schema: '' },
    },
  ],
  edges: [],
} as unknown as DecisionGraphType;

type GraphState = {
  graph: GraphDetail | null;
  content: DecisionGraphType;
  versions: VersionSummary[];
  tests: TestCase[];
  testReport: TestRunReport | null;
  /**
   * Bumped whenever the canvas changes. Panels record it when they run, so a report can
   * say it describes an earlier version of the graph instead of quietly implying it is
   * current - which is what happens when a suite is run, then the assistant's proposal is
   * accepted underneath it.
   */
  revision: number;
  testReportRevision: number | null;
  /** A model call outside the agent; the chat composer waits on it. */
  generatingTests: boolean;
  dirty: boolean;
  saving: boolean;
  /** A graph that exists only on the canvas: created by the agent or by
   *  "New policy", but not yet saved under a name. */
  isDraft: boolean;
  draftName: string | null;
  draftTests: TestCase[];
  loading: boolean;
  error: string | null;
  lastSavedAt: string | null;

  load: (id: string) => Promise<void>;
  setContent: (content: DecisionGraphType) => void;
  save: (message?: string) => Promise<void>;
  rename: (name: string) => Promise<void>;
  refreshVersions: () => Promise<void>;
  restore: (version: number) => Promise<void>;
  loadTests: () => Promise<void>;
  saveTests: (tests: TestCase[]) => Promise<void>;
  runTests: () => Promise<TestRunReport | null>;
  generateTests: () => Promise<string | null>;
  applyProposed: (content: DecisionGraphType) => void;
  /** Hold an agent proposal on the canvas without persisting it. */
  beginDraft: (content: DecisionGraphType, name: string, tests?: TestCase[]) => void;
  /** Persist the draft as a real graph under `name`; returns its id. */
  saveDraftAs: (name: string) => Promise<string | null>;
  reset: () => void;
  clearError: () => void;
};

let autosaveTimer: ReturnType<typeof setTimeout> | null = null;
/**
 * The editor normalises a graph when it mounts (filling in defaults), which
 * fires onChange with content that legitimately differs from what was stored.
 * Treating that as an edit would autosave a new version merely for opening a
 * policy, so the first change after a load is absorbed silently.
 */
let settling = false;

function beginSettling() {
  // Deliberately not time-boxed: how long the editor takes to mount varies
  // enormously (a cold container compile is seconds), and a timer that expires
  // first turns the normalisation into a phantom edit and an extra version.
  settling = true;
}

export const useGraphStore = create<GraphState>((set, get) => ({
  graph: null,
  content: { nodes: [], edges: [] },
  versions: [],
  tests: [],
  testReport: null,
  revision: 0,
  testReportRevision: null,
  generatingTests: false,
  dirty: false,
  saving: false,
  isDraft: false,
  draftName: null,
  draftTests: [],
  loading: false,
  error: null,
  lastSavedAt: null,

  clearError: () => set({ error: null }),

  load: async (id) => {
    set({ loading: true, error: null });
    beginSettling();
    try {
      const graph = await api.getGraph(id);
      set({
        graph,
        content: graph.content,
        dirty: false,
        loading: false,
        testReport: null,
        testReportRevision: null,
        revision: get().revision + 1,
        isDraft: false,
        draftName: null,
        draftTests: [],
      });
      await Promise.all([get().refreshVersions(), get().loadTests()]);
    } catch (e) {
      set({ loading: false, error: describe(e) });
    }
  },

  setContent: (content) => {
    if (JSON.stringify(content) === JSON.stringify(get().content)) return;

    if (settling) {
      // Absorb the editor's mount-time normalisation without marking dirty.
      // Only the first change after a load is absorbed, so a real edit is at
      // worst deferred to the next one rather than lost.
      settling = false;
      set((s) => ({ content, revision: s.revision + 1 }));
      return;
    }

    set((s) => ({ content, dirty: true, revision: s.revision + 1 }));

    // Debounced autosave. The backend coalesces consecutive autosaves inside a
    // minute, so dragging a node around does not create a version per frame.
    if (autosaveTimer) clearTimeout(autosaveTimer);
    autosaveTimer = setTimeout(() => {
      const { graph, dirty, isDraft } = get();
      // A draft has no row to autosave into; it is kept until the user names it.
      if (graph && dirty && !isDraft) void get().save();
    }, AUTOSAVE_DELAY);
  },

  save: async (message) => {
    const { graph, content } = get();
    if (!graph) return;
    set({ saving: true, error: null });
    try {
      const { graph: saved, version } = await api.saveGraph(graph.id, content, {
        message,
        autosave: !message,
      });
      set({
        graph: { ...saved, content },
        dirty: false,
        saving: false,
        lastSavedAt: new Date().toISOString(),
      });
      if (message) await get().refreshVersions();
      else set((s) => ({ versions: bumpVersion(s.versions, version) }));
    } catch (e) {
      set({ saving: false, error: describe(e) });
    }
  },

  rename: async (name) => {
    const { graph } = get();
    if (!graph) return;
    try {
      const updated = await api.renameGraph(graph.id, name);
      set({ graph: { ...updated, content: get().content } });
    } catch (e) {
      set({ error: describe(e) });
    }
  },

  refreshVersions: async () => {
    const { graph } = get();
    if (!graph) return;
    try {
      const { versions } = await api.listVersions(graph.id);
      set({ versions });
    } catch {
      // Version history is supplementary; never block the editor on it.
    }
  },

  restore: async (version) => {
    const { graph } = get();
    if (!graph) return;
    try {
      const { graph: restored } = await api.restoreVersion(graph.id, version);
      set((s) => ({ graph: restored, content: restored.content, dirty: false,
                    revision: s.revision + 1, testReportRevision: null }));
      await get().refreshVersions();
    } catch (e) {
      set({ error: describe(e) });
    }
  },

  loadTests: async () => {
    const { graph } = get();
    if (!graph) return;
    try {
      const { tests } = await api.listTests(graph.id);
      set({ tests });
    } catch {
      set({ tests: [] });
    }
  },

  saveTests: async (tests) => {
    const { graph, isDraft } = get();
    if (isDraft || !graph) {
      // Held until the draft is named and saved, which writes them in one go.
      set({ tests, draftTests: tests });
      return;
    }
    try {
      const saved = await api.replaceTests(graph.id, tests);
      set({ tests: saved.tests });
    } catch (e) {
      set({ error: describe(e) });
    }
  },

  runTests: async () => {
    const { graph, content, isDraft, tests } = get();
    try {
      // A draft has no stored suite to run against, so its tests are posted
      // with the content to the stateless endpoint. Either way the *live*
      // canvas is what gets tested, so unsaved edits are covered.
      const report = isDraft || !graph
        ? tests.length
          ? await api.runAdhocTests(content, tests)
          : null
        : await api.runTests(graph.id, content);
      if (report) set({ testReport: report, testReportRevision: get().revision });
      return report;
    } catch (e) {
      set({ error: describe(e) });
      return null;
    }
  },

  /**
   * Ask the model for a suite.
   *
   * Lives in the store rather than the panel because it is the one model call outside the
   * agent, and the chat has to know it is happening: two conversations against one API key
   * is two claims on the same daily quota. Returns an error message rather than throwing,
   * so the panel can show it in place instead of an alert box.
   */
  generateTests: async () => {
    const { graph } = get();
    if (!graph || get().generatingTests) return null;
    set({ generatingTests: true });
    try {
      const { tests } = await api.generateTests(graph.id);
      await get().saveTests(tests);
      await get().loadTests();
      return null;
    } catch (e) {
      return describe(e);
    } finally {
      set({ generatingTests: false });
    }
  },

  beginDraft: (content, name, tests = []) => {
    if (autosaveTimer) clearTimeout(autosaveTimer);
    beginSettling();
    set({
      graph: null,
      content,
      isDraft: true,
      draftName: name,
      draftTests: tests,
      tests,
      versions: [],
      testReport: null,
      testReportRevision: null,
      revision: get().revision + 1,
      dirty: true,
      error: null,
    });
  },

  saveDraftAs: async (name) => {
    const { content, draftTests } = get();
    set({ saving: true, error: null });
    try {
      const created = await api.createGraph(name, content);
      if (draftTests.length) await api.replaceTests(created.id, draftTests);
      set({
        graph: created,
        isDraft: false,
        draftName: null,
        draftTests: [],
        dirty: false,
        saving: false,
        lastSavedAt: new Date().toISOString(),
      });
      await Promise.all([get().refreshVersions(), get().loadTests()]);
      return created.id;
    } catch (e) {
      set({ saving: false, error: describe(e) });
      return null;
    }
  },

  /**
   * Start on a blank canvas as a draft.
   *
   * Draft mode is on from the outset, not only once the assistant proposes
   * something: a graph drawn by hand needs the same Save action, and without
   * this there is no way to keep one.
   */
  reset: () => {
    if (autosaveTimer) clearTimeout(autosaveTimer);
    beginSettling();
    set({
      graph: null,
      content: BLANK_GRAPH,
      versions: [],
      tests: [],
      testReport: null,
      testReportRevision: null,
      revision: get().revision + 1,
      dirty: false,
      isDraft: true,
      draftName: null,
      draftTests: [],
      error: null,
      lastSavedAt: null,
    });
  },

  applyProposed: (content) => {
    set((s) => ({ content, dirty: false, revision: s.revision + 1 }));
  },
}));

function bumpVersion(versions: VersionSummary[], version: number): VersionSummary[] {
  return versions.some((v) => v.version === version) ? versions : versions;
}

function describe(error: unknown): string {
  if (error instanceof AppError) return error.message;
  return error instanceof Error ? error.message : 'Something went wrong.';
}
