'use client';

import { create } from 'zustand';

import { resolveInitialTheme, THEME_STORAGE_KEY, type ThemeMode } from '../lib/theme';

/** Which surface the assistant pane is showing. */
export type AssistantTab = 'chat' | 'tests' | 'problems';

type UiState = {
  theme: ThemeMode;
  sidebarOpen: boolean;
  chatOpen: boolean;
  assistantTab: AssistantTab;
  hydrated: boolean;
  setTheme: (mode: ThemeMode) => void;
  toggleTheme: () => void;
  toggleSidebar: () => void;
  toggleChat: () => void;
  setAssistantTab: (tab: AssistantTab) => void;
  hydrate: () => void;
};

function applyTheme(mode: ThemeMode) {
  if (typeof document === 'undefined') return;
  document.documentElement.setAttribute('data-theme', mode);
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, mode);
  } catch {
    // Private browsing; the in-memory theme still applies.
  }
}

export const useUiStore = create<UiState>((set, get) => ({
  theme: 'light',
  sidebarOpen: true,
  chatOpen: true,
  assistantTab: 'chat',
  hydrated: false,

  setTheme: (mode) => {
    applyTheme(mode);
    set({ theme: mode });
  },
  toggleTheme: () => get().setTheme(get().theme === 'dark' ? 'light' : 'dark'),
  toggleSidebar: () => set((s) => ({ sidebarOpen: !s.sidebarOpen })),
  setAssistantTab: (assistantTab) => set({ assistantTab }),
  toggleChat: () => set((s) => ({ chatOpen: !s.chatOpen })),

  // Runs on the client only, so the server render stays deterministic.
  hydrate: () => {
    if (get().hydrated) return;
    const mode = resolveInitialTheme();
    applyTheme(mode);
    set({ theme: mode, hydrated: true });
  },
}));
