'use client';

import { Bot, Download, RotateCcw, Upload, User } from 'lucide-react';
import { useEffect } from 'react';

import { downloadGraph } from '../../lib/download';
import { useGraphStore } from '../../stores/useGraphStore';
import { Badge, Button, Dialog, IconButton, cx } from '../ui';

const AUTHOR_ICON = {
  agent: <Bot size={12} />,
  user: <User size={12} />,
  import: <Upload size={12} />,
};

export function VersionHistory({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { graph, versions, refreshVersions, restore } = useGraphStore();

  useEffect(() => {
    if (open) void refreshVersions();
  }, [open, refreshVersions]);

  return (
    <Dialog open={open} onClose={onClose} title="Version history" width={560}>
      {versions.length === 0 ? (
        <p className="py-6 text-center text-sm text-fg-muted">No versions recorded yet.</p>
      ) : (
        <ul className="divide-y divide-border">
          {versions.map((version) => {
            const current = version.version === graph?.current_version;
            return (
              <li key={version.version} className="flex items-center gap-3 py-2.5">
                <span
                  className={cx(
                    'grid h-7 w-7 shrink-0 place-items-center rounded-full text-2xs font-semibold',
                    current ? 'bg-accent text-accent-fg' : 'bg-bg-inset text-fg-muted',
                  )}
                >
                  v{version.version}
                </span>

                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm">
                    {version.message || (version.is_autosave ? 'Autosave' : 'Saved')}
                  </p>
                  <p className="mt-0.5 flex items-center gap-1.5 text-2xs text-fg-subtle">
                    {AUTHOR_ICON[version.author]}
                    {version.author}
                    <span>·</span>
                    {new Date(version.created_at).toLocaleString()}
                    <span>·</span>
                    {version.node_count} nodes
                  </p>
                </div>

                {current ? <Badge tone="accent">Current</Badge> : null}

                <IconButton
                  label={`Download version ${version.version}`}
                  onClick={() => graph && downloadGraph(graph.id, 'jdm', version.version)}
                >
                  <Download size={13} />
                </IconButton>

                {!current ? (
                  <Button
                    size="sm"
                    icon={<RotateCcw size={12} />}
                    onClick={async () => {
                      await restore(version.version);
                      onClose();
                    }}
                  >
                    Restore
                  </Button>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
    </Dialog>
  );
}
