'use client';

import { useEffect } from 'react';

import { useGraphStore } from '../../stores/useGraphStore';

/**
 * Warns before the tab is closed with work that is not stored anywhere.
 *
 * A draft lives only in this tab's memory, and a dirty saved graph may have
 * edits the 3s autosave has not written yet. In-app navigation is guarded
 * separately by `useUnsavedPrompt`, because `beforeunload` does not fire for
 * client-side route changes.
 */
export function UnsavedGuard() {
  const dirty = useGraphStore((s) => s.dirty);

  useEffect(() => {
    // `dirty` alone is the condition: an untouched blank canvas is a draft too,
    // and warning about that on every visit would train the user to dismiss the
    // prompt without reading it.
    if (!dirty) return;
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      // Browsers show their own wording; a non-empty returnValue is what
      // actually triggers the prompt.
      event.returnValue = '';
    };
    window.addEventListener('beforeunload', onBeforeUnload);
    return () => window.removeEventListener('beforeunload', onBeforeUnload);
  }, [dirty]);

  return null;
}

/**
 * Confirms leaving unsaved work behind, for in-app actions such as opening
 * another policy or starting a new one.
 */
export function confirmDiscardUnsaved(isDraft: boolean, dirty: boolean): boolean {
  if (!dirty) return true;
  return window.confirm(
    isDraft
      ? 'This policy has not been saved yet and will be lost. Continue?'
      : 'This policy has unsaved changes. Continue without saving?',
  );
}
