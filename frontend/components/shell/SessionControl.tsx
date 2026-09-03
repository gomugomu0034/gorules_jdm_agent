'use client';

import { LogIn, LogOut } from 'lucide-react';
import Link from 'next/link';
import { useEffect } from 'react';

import { useSessionStore } from '../../stores/useSessionStore';
import { IconButton } from '../ui';

/**
 * Who you are signed in as, and the way back out.
 *
 * Shared by the studio's top bar and the library header: signing in lands on
 * the library, so the way out has to be reachable from there too. Signing out
 * reloads the app - see `useSessionStore` for why.
 */
export function SessionControl() {
  const { session, hydrate, logout } = useSessionStore();

  useEffect(() => {
    void hydrate();
  }, [hydrate]);

  if (session?.mode === 'admin') {
    return (
      <div className="flex items-center gap-1.5 pl-1">
        <span
          className="max-w-[10rem] truncate text-2xs text-fg-muted"
          title={session.email ?? undefined}
        >
          {session.email}
        </span>
        <IconButton label="Sign out" onClick={() => void logout()}>
          <LogOut size={15} />
        </IconButton>
      </div>
    );
  }

  if (session?.login_enabled) {
    return (
      <Link
        href="/login"
        className="ml-1 inline-flex h-7 items-center gap-1.5 rounded border border-border px-2.5 text-xs font-medium hover:bg-bg-subtle focus-ring"
      >
        <LogIn size={13} /> Sign in
      </Link>
    );
  }

  return null;
}
