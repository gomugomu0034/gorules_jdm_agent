'use client';

import { useEffect } from 'react';

import { useUiStore } from '../../stores/useUiStore';

/** Syncs the store with the theme the inline head script already applied. */
export function ThemeBoot() {
  const hydrate = useUiStore((s) => s.hydrate);
  useEffect(() => hydrate(), [hydrate]);
  return null;
}
