'use client';

import { LogIn } from 'lucide-react';
import { useRouter } from 'next/navigation';
import { useEffect, useState } from 'react';

import { useSessionStore } from '../../stores/useSessionStore';
import { Button, Input } from '../ui';

export function LoginForm() {
  const router = useRouter();
  const { session, error, loading, hydrate, login, clearError } = useSessionStore();

  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');

  useEffect(() => {
    void hydrate();
  }, [hydrate]);

  // Already signed in: nothing to do here.
  useEffect(() => {
    if (session?.mode === 'admin') router.replace('/graphs');
  }, [session?.mode, router]);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    clearError();
    try {
      await login(email, password);
      router.replace('/graphs');
    } catch {
      // The store holds the message; keep the form mounted so it can show.
    }
  };

  const disabled = session?.login_enabled === false;

  return (
    <div className="flex h-full items-center justify-center bg-bg-subtle p-4">
      <div className="w-full max-w-sm">
        <div className="mb-6 text-center">
          <div className="mx-auto mb-3 flex h-10 w-10 items-center justify-center rounded-lg bg-accent text-white">
            <span className="text-lg font-bold">J</span>
          </div>
          <h1 className="text-lg font-semibold text-fg">Sign in to JDM Studio</h1>
          <p className="mt-1 text-xs text-fg-muted">
            You can keep working without an account — sign in only to reach the
            shared policy library.
          </p>
        </div>

        <form
          onSubmit={submit}
          className="rounded-lg border border-border bg-bg p-5 shadow-1"
        >
          {disabled ? (
            <p className="mb-4 rounded border border-warning/30 bg-warning-subtle p-2.5 text-xs text-warning">
              No admin account is configured. Set <code>ADMIN_PASSWORD</code> in{' '}
              <code>backend/.env</code> and restart the API.
            </p>
          ) : null}

          <label className="mb-1 block text-xs font-medium text-fg-muted" htmlFor="email">
            Email
          </label>
          <Input
            id="email"
            type="email"
            autoComplete="username"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="admin@example.com"
            required
            disabled={disabled}
          />

          <label
            className="mb-1 mt-3 block text-xs font-medium text-fg-muted"
            htmlFor="password"
          >
            Password
          </label>
          <Input
            id="password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
            disabled={disabled}
          />

          {error ? (
            <p role="alert" className="mt-3 text-xs text-danger">
              {error}
            </p>
          ) : null}

          <Button
            type="submit"
            variant="primary"
            icon={<LogIn size={14} />}
            loading={loading}
            disabled={disabled}
            className="mt-4 w-full"
          >
            Sign in
          </Button>
        </form>

        <p className="mt-4 text-center text-xs text-fg-muted">
          <a href="/" className="text-accent hover:underline">
            Continue as a guest
          </a>
        </p>
      </div>
    </div>
  );
}
