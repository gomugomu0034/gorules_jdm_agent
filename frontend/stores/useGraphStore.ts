'use client';

import type { DecisionGraphType } from '@gorules/jdm-editor';
import { create } from 'zustand';

import { api, AppError } from '../lib/api';
import type { GraphDetail, TestCase, TestRunReport, VersionSummary } from '../lib/types';

const AUTOSAVE_DELAY = 3000;

type GraphState = {
  graph: GraphDetail | null;
  content: DecisionGraphType;
  versions: VersionSummary[];
  tests: TestCase[];
  testReport: TestRunReport | null;
  dirty: boolean;
  saving: boolean;
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
  applyProposed: (content: DecisionGraphType) => void;
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
  dirty: false,
  saving: false,
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
      set({ content });
      return;
    }

    set({ content, dirty: true });

    // Debounced autosave. The backend coalesces consecutive autosaves inside a
    // minute, so dragging a node around does not create a version per frame.
    if (autosaveTimer) clearTimeout(autosaveTimer);
    autosaveTimer = setTimeout(() => {
      const { graph, dirty } = get();
      if (graph && dirty) void get().save();
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
      set({ graph: restored, content: restored.content, dirty: false });
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
    const { graph } = get();
    if (!graph) return;
    try {
      const saved = await api.replaceTests(graph.id, tests);
      set({ tests: saved.tests });
    } catch (e) {
      set({ error: describe(e) });
    }
  },

  runTests: async () => {
    const { graph, content } = get();
    if (!graph) return null;
    try {
      // Send the live canvas so unsaved edits are what actually gets tested.
      const report = await api.runTests(graph.id, content);
      set({ testReport: report });
      return report;
    } catch (e) {
      set({ error: describe(e) });
      return null;
    }
  },

  applyProposed: (content) => {
    set({ content, dirty: false });
  },
}));

function bumpVersion(versions: VersionSummary[], version: number): VersionSummary[] {
  return versions.some((v) => v.version === version) ? versions : versions;
}

function describe(error: unknown): string {
  if (error instanceof AppError) return error.message;
  return error instanceof Error ? error.message : 'Something went wrong.';
}
