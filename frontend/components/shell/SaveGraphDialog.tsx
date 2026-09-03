'use client';

import { useEffect, useRef, useState } from 'react';

import { Button, Dialog, Input } from '../ui';

/**
 * Names a draft on its way to becoming a real policy.
 *
 * This is the moment a guest's work stops being disposable, so the dialog is
 * deliberately explicit rather than silently inventing a name.
 */
export function SaveGraphDialog({
  open,
  defaultName,
  onClose,
  onSave,
}: {
  open: boolean;
  defaultName: string;
  onClose: () => void;
  onSave: (name: string) => Promise<void> | void;
}) {
  const [name, setName] = useState(defaultName);
  const [saving, setSaving] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    setName(defaultName);
    // Select the placeholder name so typing replaces it outright.
    const id = window.setTimeout(() => inputRef.current?.select(), 30);
    return () => window.clearTimeout(id);
  }, [open, defaultName]);

  const submit = async () => {
    const trimmed = name.trim();
    if (!trimmed || saving) return;
    setSaving(true);
    try {
      await onSave(trimmed);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="Save policy"
      footer={
        <>
          <Button onClick={onClose} disabled={saving}>
            Cancel
          </Button>
          <Button
            variant="primary"
            onClick={submit}
            loading={saving}
            disabled={!name.trim()}
          >
            Save
          </Button>
        </>
      }
    >
      <label className="mb-1.5 block text-xs font-medium text-fg-muted" htmlFor="policy-name">
        Policy name
      </label>
      <Input
        id="policy-name"
        ref={inputRef}
        value={name}
        onChange={(e) => setName(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') void submit();
        }}
        placeholder="ticket_discount_policy"
      />
      <p className="mt-2 text-xs text-fg-muted">
        Saving creates version 1 and adds the policy to your library, along with
        any tests the assistant generated.
      </p>
    </Dialog>
  );
}
