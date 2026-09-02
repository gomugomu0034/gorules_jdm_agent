'use client';

import { Check, Loader2 } from 'lucide-react';

import type { RunStep } from '../../stores/useChatStore';
import { cx } from '../ui';

/**
 * A vertical stepper of what the agent is doing.
 *
 * This matters because the builder can make up to eight sequential LLM calls;
 * without live attempt-level progress a long build is indistinguishable from a
 * hung one.
 */
export function ProgressRail({ steps, running }: { steps: RunStep[]; running: boolean }) {
  return (
    <div className="rounded-lg border border-border bg-bg-subtle p-2.5">
      <ol className="space-y-1.5">
        {steps.map((step, i) => {
          const active = step.status === 'running' && running;
          return (
            <li key={`${step.node}-${i}`} className="flex items-start gap-2">
              <span className="mt-0.5 flex h-3.5 w-3.5 shrink-0 items-center justify-center">
                {active ? (
                  <Loader2 size={12} className="animate-spin text-accent" />
                ) : (
                  <Check size={12} className="text-success" />
                )}
              </span>

              <div className="min-w-0 flex-1">
                <p className={cx('text-xs', active ? 'font-medium text-fg' : 'text-fg-muted')}>
                  {step.label}
                </p>
                {step.progress ? (
                  <p className="mt-0.5 text-2xs text-fg-subtle">
                    {step.progress.max_attempts > 1
                      ? `Attempt ${step.progress.attempt} of ${step.progress.max_attempts} · `
                      : ''}
                    {step.progress.message}
                  </p>
                ) : null}
              </div>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
