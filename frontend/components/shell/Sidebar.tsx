'use client';

import { FileJson, Plus, Search, Upload } from 'lucide-react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useEffect, useMemo, useRef, useState } from 'react';

import { api } from '../../lib/api';
import type { GraphSummary } from '../../lib/types';
import { Button, EmptyState, IconButton, Input, Spinner, cx } from '../ui';

type Props = {
  activeId?: string;
  /** Bumping this refetches the list (e.g. after the agent saves a graph). */
  refreshToken?: number;
};

export function Sidebar({ activeId, refreshToken = 0 }: Props) {
  const router = useRouter();
  const [graphs, setGraphs] = useState<GraphSummary[]>([]);
  const [query, setQuery] = useState('');
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api
      .listGraphs()
      .then(({ graphs: list }) => !cancelled && setGraphs(list))
      .catch(() => !cancelled && setGraphs([]))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [refreshToken]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return q ? graphs.filter((g) => g.name.toLowerCase().includes(q)) : graphs;
  }, [graphs, query]);

  const createGraph = async () => {
    setBusy(true);
    try {
      const graph = await api.createGraph(`Untitled policy ${graphs.length + 1}`);
      router.push(`/graphs/${graph.id}`);
    } finally {
      setBusy(false);
    }
  };

  const importGraph = async (file: File) => {
    setBusy(true);
    try {
      const graph = await api.importGraph(file);
      router.push(`/graphs/${graph.id}`);
    } catch (e) {
      window.alert(e instanceof Error ? e.message : 'Import failed.');
    } finally {
      setBusy(false);
      if (fileInput.current) fileInput.current.value = '';
    }
  };

  return (
    <aside className="flex h-full w-full flex-col border-r border-border bg-bg-subtle">
      <div className="flex flex-col gap-2 border-b border-border p-2.5">
        <div className="flex items-center gap-1.5">
          <div className="relative flex-1">
            <Search
              size={13}
              className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-fg-subtle"
            />
            <Input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search policies"
              className="pl-7"
            />
          </div>
          <IconButton
            label="Import a JDM file or bundle"
            onClick={() => fileInput.current?.click()}
            disabled={busy}
          >
            <Upload size={14} />
          </IconButton>
        </div>
        <Button variant="primary" icon={<Plus size={14} />} onClick={createGraph} loading={busy}>
          New policy
        </Button>
        <input
          ref={fileInput}
          type="file"
          accept=".json,.zip"
          className="hidden"
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) void importGraph(file);
          }}
        />
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-1.5">
        {loading ? (
          <div className="flex justify-center p-6">
            <Spinner />
          </div>
        ) : filtered.length === 0 ? (
          <EmptyState
            icon={<FileJson size={22} />}
            title={query ? 'No matches' : 'No policies yet'}
            description={query ? undefined : 'Create one, or import an existing JDM file.'}
          />
        ) : (
          <ul className="flex flex-col gap-0.5">
            {filtered.map((graph) => (
              <li key={graph.id}>
                <Link
                  href={`/graphs/${graph.id}`}
                  className={cx(
                    'block rounded px-2.5 py-2 transition-colors',
                    graph.id === activeId
                      ? 'bg-accent-subtle text-accent'
                      : 'text-fg hover:bg-bg-inset',
                  )}
                >
                  <span className="block truncate text-sm font-medium">{graph.name}</span>
                  <span className="mt-0.5 block text-2xs text-fg-subtle">
                    v{graph.current_version} · {graph.node_count} nodes · {graph.test_count} tests
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </div>
    </aside>
  );
}
