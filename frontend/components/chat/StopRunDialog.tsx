'use client';

import { Button, Dialog } from '../ui';
import type { RunStep } from '../../stores/useChatStore';

/**
 * Confirms stopping the assistant mid-turn.
 *
 * Stopping is not free: a build that has already spent several model calls loses all of
 * them, and on a rate-limited tier those calls are not replaceable. So the dialog says
 * what is actually in flight rather than asking an abstract "are you sure?" - and it says
 * plainly what survives, because the thing people are usually afraid of losing is the
 * graph already on the canvas, which is never at risk.
 */
export function StopRunDialog({
  open,
  steps,
  hasProposal,
  onClose,
  onConfirm,
}: {
  open: boolean;
  steps: RunStep[];
  hasProposal: boolean;
  onClose: () => void;
  onConfirm: () => void;
}) {
  const current = [...steps].reverse().find((s) => s.status === 'running') ?? steps.at(-1);
  const attempt = current?.progress?.attempt;

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="Stop the assistant?"
      width={420}
      footer={
        <>
          <Button size="sm" variant="ghost" onClick={onClose}>
            Keep going
          </Button>
          <Button size="sm" variant="danger" onClick={onConfirm}>
            Stop
          </Button>
        </>
      }
    >
      <div className="space-y-3 text-sm">
        <p>
          {current ? (
            <>
              It is on <span className="font-medium">{current.label.toLowerCase()}</span>
              {attempt ? `, attempt ${attempt}` : ''}. That work is discarded — stopping
              does not pause it.
            </>
          ) : (
            <>The turn in progress will be discarded. Stopping does not pause it.</>
          )}
        </p>
        <p className="text-fg-muted">
          {hasProposal
            ? 'The graph already proposed on the canvas stays where it is.'
            : 'Nothing already saved is affected. You can ask again straight away.'}
        </p>
      </div>
    </Dialog>
  );
}
