'use client';

import { FileJson, Moon, Plus, Sun, Upload } from 'lucide-react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useEffect, useRef, useState } from 'react';

import { api, AppError } from '../../lib/api';
import type { GraphSummary } from '../../lib/types';
import { useUiStore } from '../../stores/useUiStore';
import { Button, EmptyState, IconButton, Spinner } from '../ui';

export function GraphLibrary() {
  const router = useRouter();
  const { theme, toggleTheme } = useUiStore();

  const [graphs, setGraphs] = useState<GraphSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    api
      .listGraphs()
      .then(({ graphs: list }) => setGraphs(list))
      .catch((e) => setError(e instanceof AppError ? e.message : 'Could not load policies.'))
      .finally(() => setLoading(false));
  }, []);

  const create = async () => {
    setBusy(true);
    try {
      const graph = await api.createGraph(`Untitled policy ${graphs.length + 1}`);
      router.push(`/graphs/${graph.id}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not create a policy.');
      setBusy(false);
    }
  };

  const importFile = async (file: File) => {
    setBusy(true);
    setError(null);
    try {
      const graph = await api.importGraph(file);
      router.push(`/graphs/${graph.id}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Import failed.');
      setBusy(false);
    }
  };

  return (
    <div className="flex h-full flex-col overflow-auto">
      <header className="flex h-12 shrink-0 items-center gap-2 border-b border-border px-4">
        <span className="grid h-5 w-5 place-items-center rounded bg-accent text-2xs font-semibold text-accent-fg">
          J
        </span>
        <span className="text-sm font-semibold">JDM Studio</span>
        <div className="flex-1" />
        <IconButton label="Toggle theme" onClick={toggleTheme}>
          {theme === 'dark' ? <Sun size={15} /> : <Moon size={15} />}
        </IconButton>
      </header>

      <main className="mx-auto w-full max-w-5xl px-6 py-10">
        <div className="flex items-end justify-between gap-4">
          <div>
            <h1 className="text-2xl font-semibold">Decision policies</h1>
            <p className="mt-1 text-sm text-fg-muted">
              Author GoRules JDM graphs by hand or with the assistant, then simulate, test and
              version them.
            </p>
          </div>
          <div className="flex shrink-0 gap-2">
            <Button icon={<Upload size={14} />} onClick={() => fileInput.current?.click()}>
              Import
            </Button>
            <Button variant="primary" icon={<Plus size={14} />} onClick={create} loading={busy}>
              New policy
            </Button>
          </div>
        </div>

        <input
          ref={fileInput}
          type="file"
          accept=".json,.zip"
          className="hidden"
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) void importFile(file);
          }}
        />

        {error ? (
          <div className="mt-5 rounded border border-border bg-danger-subtle px-3 py-2 text-sm text-danger">
            {error}
          </div>
        ) : null}

        <div className="mt-7">
          {loading ? (
            <div className="flex justify-center py-16">
              <Spinner />
            </div>
          ) : graphs.length === 0 ? (
            <div className="rounded-lg border border-border py-8">
              <EmptyState
                icon={<FileJson size={24} />}
                title="No policies yet"
                description="Create your first decision policy, or import an existing JDM file."
                action={
                  <Button variant="primary" icon={<Plus size={14} />} onClick={create}>
                    New policy
                  </Button>
                }
              />
            </div>
          ) : (
            <ul className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {graphs.map((graph) => (
                <li key={graph.id}>
                  <Link
                    href={`/graphs/${graph.id}`}
                    className="block h-full rounded-lg border border-border bg-bg p-4 transition-colors hover:border-accent hover:bg-bg-subtle"
                  >
                    <p className="truncate font-medium">{graph.name}</p>
                    <p className="mt-1 line-clamp-2 min-h-[2.4em] text-xs text-fg-muted">
                      {graph.description || 'No description.'}
                    </p>
                    <p className="mt-3 text-2xs text-fg-subtle">
                      v{graph.current_version} · {graph.node_count} nodes · {graph.test_count} tests
                      <br />
                      Updated {new Date(graph.updated_at).toLocaleDateString()}
                    </p>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </div>
      </main>
    </div>
  );
}
