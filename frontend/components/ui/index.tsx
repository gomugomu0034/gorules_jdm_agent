'use client';

import { Loader2 } from 'lucide-react';
import type { ButtonHTMLAttributes, HTMLAttributes, InputHTMLAttributes, ReactNode } from 'react';
import { forwardRef, useEffect } from 'react';

export function cx(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(' ');
}

// --------------------------------------------------------------------------
// Button
// --------------------------------------------------------------------------

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: 'primary' | 'secondary' | 'ghost' | 'danger';
  size?: 'sm' | 'md';
  loading?: boolean;
  icon?: ReactNode;
};

const VARIANTS: Record<string, string> = {
  primary: 'bg-accent text-accent-fg hover:bg-accent-hover border-transparent',
  secondary: 'bg-bg text-fg hover:bg-bg-subtle border-border',
  ghost: 'bg-transparent text-fg-muted hover:text-fg hover:bg-bg-subtle border-transparent',
  danger: 'bg-transparent text-danger hover:bg-danger-subtle border-transparent',
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = 'secondary', size = 'md', loading, icon, className, children, disabled, ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      disabled={disabled || loading}
      className={cx(
        'inline-flex items-center justify-center gap-1.5 rounded border font-medium',
        'transition-colors focus-ring disabled:opacity-50 disabled:pointer-events-none',
        size === 'sm' ? 'h-7 px-2.5 text-xs' : 'h-8 px-3 text-sm',
        VARIANTS[variant],
        className,
      )}
      {...rest}
    >
      {loading ? <Loader2 size={14} className="animate-spin" /> : icon}
      {children}
    </button>
  );
});

export function IconButton({
  label,
  className,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { label: string }) {
  return (
    <button
      aria-label={label}
      title={label}
      className={cx(
        'inline-flex h-7 w-7 items-center justify-center rounded text-fg-muted',
        'transition-colors hover:bg-bg-subtle hover:text-fg focus-ring',
        'disabled:opacity-40 disabled:pointer-events-none',
        className,
      )}
      {...rest}
    />
  );
}

// --------------------------------------------------------------------------
// Input
// --------------------------------------------------------------------------

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(
  function Input({ className, ...rest }, ref) {
    return (
      <input
        ref={ref}
        className={cx(
          'h-8 w-full rounded border border-border bg-bg px-2.5 text-sm text-fg',
          'placeholder:text-fg-subtle focus-ring',
          className,
        )}
        {...rest}
      />
    );
  },
);

// --------------------------------------------------------------------------
// Badge
// --------------------------------------------------------------------------

type Tone = 'neutral' | 'success' | 'danger' | 'warning' | 'accent';

const TONES: Record<Tone, string> = {
  neutral: 'bg-bg-inset text-fg-muted',
  success: 'bg-success-subtle text-success',
  danger: 'bg-danger-subtle text-danger',
  warning: 'bg-warning-subtle text-warning',
  accent: 'bg-accent-subtle text-accent',
};

export function Badge({
  tone = 'neutral',
  className,
  children,
}: { tone?: Tone; className?: string; children: ReactNode }) {
  return (
    <span
      className={cx(
        'inline-flex items-center gap-1 rounded-sm px-1.5 py-0.5 text-2xs font-medium',
        TONES[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}

// --------------------------------------------------------------------------
// Spinner and empty states
// --------------------------------------------------------------------------

export function Spinner({ className }: { className?: string }) {
  return <Loader2 size={16} className={cx('animate-spin text-fg-subtle', className)} />;
}

export function EmptyState({
  icon,
  title,
  description,
  action,
}: {
  icon?: ReactNode;
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-2 p-8 text-center">
      {icon ? <div className="text-fg-subtle">{icon}</div> : null}
      <p className="text-sm font-medium text-fg">{title}</p>
      {description ? <p className="max-w-sm text-xs text-fg-muted">{description}</p> : null}
      {action ? <div className="mt-2">{action}</div> : null}
    </div>
  );
}

// --------------------------------------------------------------------------
// Panel scaffolding
// --------------------------------------------------------------------------

export function PanelSection({
  title,
  actions,
  className,
  children,
}: {
  title?: string;
  actions?: ReactNode;
  className?: string;
  children: ReactNode;
}) {
  return (
    <div className={cx('flex min-h-0 flex-col', className)}>
      {title ? (
        <div className="flex h-9 shrink-0 items-center justify-between border-b border-border px-3">
          <span className="text-xs font-semibold uppercase tracking-wide text-fg-subtle">
            {title}
          </span>
          {actions}
        </div>
      ) : null}
      <div className="min-h-0 flex-1 overflow-auto">{children}</div>
    </div>
  );
}

// --------------------------------------------------------------------------
// Dialog
// --------------------------------------------------------------------------

export function Dialog({
  open,
  onClose,
  title,
  children,
  footer,
  width = 480,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  footer?: ReactNode;
  width?: number;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4 animate-fade-in"
      onMouseDown={(e) => e.target === e.currentTarget && onClose()}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        style={{ width }}
        className="max-h-[85vh] overflow-hidden rounded-lg border border-border bg-bg-overlay shadow-2 animate-slide-up"
      >
        <div className="flex h-11 items-center border-b border-border px-4">
          <h2 className="text-sm font-semibold">{title}</h2>
        </div>
        <div className="max-h-[60vh] overflow-auto p-4">{children}</div>
        {footer ? (
          <div className="flex justify-end gap-2 border-t border-border px-4 py-3">{footer}</div>
        ) : null}
      </div>
    </div>
  );
}

export function Divider(props: HTMLAttributes<HTMLDivElement>) {
  return <div {...props} className={cx('h-px w-full bg-border', props.className)} />;
}
