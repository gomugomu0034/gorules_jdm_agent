'use client';

import {
  Check,
  ChevronDown,
  Download,
  History,
  MessageSquare,
  Moon,
  PanelLeft,
  Save,
  Sun,
} from 'lucide-react';
import { useEffect, useRef, useState } from 'react';

import { downloadGraph } from '../../lib/download';
import { useGraphStore } from '../../stores/useGraphStore';
import { useUiStore } from '../../stores/useUiStore';
import { Badge, Button, IconButton, Spinner, cx } from '../ui';

export function TopBar({ onOpenHistory }: { onOpenHistory: () => void }) {
  const { graph, dirty, saving, rename, save } = useGraphStore();
  const { theme, toggleTheme, toggleSidebar, toggleChat, chatOpen } = useUiStore();

  const [editing, setEditing] = useState(false);
  const [draftName, setDraftName] = useState('');
  const [exportOpen, setExportOpen] = useState(false);
  const exportRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!exportOpen) return;
    const onClick = (e: MouseEvent) => {
      if (!exportRef.current?.contains(e.target as Node)) setExportOpen(false);
    };
    document.addEventListener('mousedown', onClick);
    return () => document.removeEventListener('mousedown', onClick);
  }, [exportOpen]);

  const commitName = async () => {
    const name = draftName.trim();
    setEditing(false);
    if (graph && name && name !== graph.name) await rename(name);
  };

  return (
    <header className="flex h-12 shrink-0 items-center gap-2 border-b border-border bg-bg px-3">
      <IconButton label="Toggle the policy list" onClick={toggleSidebar}>
        <PanelLeft size={15} />
      </IconButton>

      <div className="flex items-center gap-1.5 font-semibold">
        <span className="grid h-5 w-5 place-items-center rounded bg-accent text-2xs text-accent-fg">
          J
        </span>
        <span className="text-sm">JDM Studio</span>
      </div>

      <div className="mx-1 h-5 w-px bg-border" />

      {graph ? (
        <div className="flex min-w-0 items-center gap-2">
          {editing ? (
            <input
              autoFocus
              value={draftName}
              onChange={(e) => setDraftName(e.target.value)}
              onBlur={commitName}
              onKeyDown={(e) => {
                if (e.key === 'Enter') void commitName();
                if (e.key === 'Escape') setEditing(false);
              }}
              className="h-7 rounded border border-border bg-bg px-2 text-sm focus-ring"
            />
          ) : (
            <button
              onClick={() => {
                setDraftName(graph.name);
                setEditing(true);
              }}
              className="truncate rounded px-1.5 py-1 text-sm font-medium hover:bg-bg-subtle"
              title="Rename"
            >
              {graph.name}
            </button>
          )}

          <Badge tone="neutral">v{graph.current_version}</Badge>

          <span className="flex items-center gap-1 text-2xs text-fg-subtle">
            {saving ? (
              <>
                <Spinner className="h-3 w-3" /> Saving
              </>
            ) : dirty ? (
              <>
                <span className="h-1.5 w-1.5 rounded-full bg-warning" /> Unsaved
              </>
            ) : (
              <>
                <Check size={11} className="text-success" /> Saved
              </>
            )}
          </span>
        </div>
      ) : null}

      <div className="flex-1" />

      {graph ? (
        <>
          <Button
            size="sm"
            icon={<Save size={13} />}
            onClick={() => void save('Manual save')}
            disabled={!dirty || saving}
          >
            Save
          </Button>

          <Button size="sm" icon={<History size={13} />} onClick={onOpenHistory}>
            History
          </Button>

          <div className="relative" ref={exportRef}>
            <Button
              size="sm"
              icon={<Download size={13} />}
              onClick={() => setExportOpen((v) => !v)}
            >
              Export <ChevronDown size={12} />
            </Button>
            {exportOpen ? (
              <div className="absolute right-0 z-40 mt-1 w-56 overflow-hidden rounded border border-border bg-bg-overlay shadow-2 animate-slide-up">
                {(
                  [
                    ['jdm', 'JDM graph', '.json'],
                    ['tests', 'Test suite', '.json'],
                    ['bundle', 'Everything', '.zip'],
                  ] as const
                ).map(([format, label, ext]) => (
                  <button
                    key={format}
                    onClick={() => {
                      downloadGraph(graph.id, format);
                      setExportOpen(false);
                    }}
                    className="flex w-full items-center justify-between px-3 py-2 text-left text-sm hover:bg-bg-subtle"
                  >
                    {label}
                    <span className="font-mono text-2xs text-fg-subtle">{ext}</span>
                  </button>
                ))}
              </div>
            ) : null}
          </div>
        </>
      ) : null}

      <div className="mx-1 h-5 w-px bg-border" />

      <IconButton
        label={chatOpen ? 'Hide the assistant' : 'Show the assistant'}
        onClick={toggleChat}
        className={cx(chatOpen && 'bg-bg-inset text-fg')}
      >
        <MessageSquare size={15} />
      </IconButton>

      <IconButton
        label={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
        onClick={toggleTheme}
      >
        {theme === 'dark' ? <Sun size={15} /> : <Moon size={15} />}
      </IconButton>
    </header>
  );
}
