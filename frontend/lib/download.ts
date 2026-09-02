import { apiUrl } from './api';

export type ExportFormat = 'jdm' | 'tests' | 'bundle';

/**
 * Triggers a download by clicking a transient anchor at the API URL.
 *
 * The file is built server-side and sent with Content-Disposition, so there is
 * no blob to build or revoke and large bundles stream rather than buffering in
 * memory.
 */
function click(href: string) {
  const anchor = document.createElement('a');
  anchor.href = href;
  anchor.rel = 'noopener';
  anchor.style.display = 'none';
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
}

export function downloadGraph(graphId: string, format: ExportFormat, version?: number) {
  const query = new URLSearchParams({ format });
  if (version !== undefined) query.set('version', String(version));
  click(apiUrl(`/api/graphs/${graphId}/export?${query}`));
}

/** Download what the agent just produced, before deciding whether to keep it. */
export function downloadProposal(threadId: string, format: ExportFormat) {
  click(apiUrl(`/api/chat/threads/${threadId}/proposal/export?format=${format}`));
}

/** Client-side download for content that only exists in the browser. */
export function downloadJson(filename: string, data: unknown) {
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }),
  );
  click(url);
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
