'use client';

import { create } from 'zustand';

import { api, AppError, resetSession } from '../lib/api';
import type { Session } from '../lib/types';

type SessionState = {
  session: Session | null;
  loading: boolean;
  error: string | null;

  /** Read the current session. Safe to call repeatedly; runs once. */
  hydrate: () => Promise<void>;
  login: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  clearError: () => void;
};

let hydrated = false;

/**
 * Leave the app and load `path` from scratch.
 *
 * Signing in or out changes who the server thinks we are, while every store in
 * the tab still holds the previous identity's data: the policy list, the open
 * graph, the chat thread. A client-side navigation keeps all of it - an
 * admin's policies stay on screen after signing out, and their chat thread
 * then 404s on every stream reconnect - so an identity change is followed by a
 * real page load, which is the only way to be sure nothing is left behind.
 */
function reloadInto(path: string): void {
  if (typeof window === 'undefined') return;
  window.location.assign(path);
}

export const useSessionStore = create<SessionState>((set) => ({
  session: null,
  loading: false,
  error: null,

  hydrate: async () => {
    if (hydrated) return;
    hydrated = true;
    set({ loading: true });
    try {
      set({ session: await api.me(), loading: false });
    } catch (e) {
      // A backend that is down must not block the shell from rendering; the
      // visitor is simply treated as a guest until the next call succeeds.
      hydrated = false;
      set({
        session: { mode: 'guest', login_enabled: false },
        loading: false,
        error: e instanceof AppError ? e.message : 'Could not reach the server.',
      });
    }
  },

  login: async (email, password) => {
    set({ loading: true, error: null });
    try {
      const next = await api.login(email, password);
      // The identity changed, so the established session is stale.
      resetSession();
      set({ session: next, loading: false });
      reloadInto('/graphs');
    } catch (e) {
      set({
        loading: false,
        error: e instanceof AppError ? e.message : 'Sign in failed.',
      });
      throw e;
    }
  },

  logout: async () => {
    set({ loading: true });
    try {
      const next = await api.logout();
      resetSession();
      set({ session: next, loading: false });
      // Back to the guest landing page, with nothing of the admin's left over.
      reloadInto('/');
    } finally {
      set({ loading: false });
    }
  },

  clearError: () => set({ error: null }),
}));
