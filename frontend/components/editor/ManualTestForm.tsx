'use client';

import type { DecisionGraphType } from '@gorules/jdm-editor';
import { Check, Play, X } from 'lucide-react';
import { useState } from 'react';

import { api } from '../../lib/api';
import type { CheckTestResult, TestCase } from '../../lib/types';
import { Button, cx } from '../ui';

type Props = {
  content: DecisionGraphType;
  onSave: (test: TestCase) => void;
  onCancel: () => void;
};

function parse(text: string): { value: unknown; error: string | null } {
  const trimmed = text.trim();
  if (!trimmed) return { value: {}, error: null };
  try {
    const value = JSON.parse(trimmed);
    if (value === null || typeof value !== 'object' || Array.isArray(value)) {
      return { value: null, error: 'Needs to be a JSON object, like {"orderTotal": 80}' };
    }
    return { value, error: null };
  } catch (e) {
    return { value: null, error: e instanceof Error ? e.message : 'Not valid JSON' };
  }
}

/**
 * Write a test case by hand, and put it to the policy before committing to it.
 *
 * The useful question when typing a case is not "does it pass" - you already believe it
 * should - but "which of us is wrong, the policy or my expectation". So Check answers three
 * things separately: whether the graph runs the input at all, whether the fields being
 * asserted are ones it can produce, and where the values differ. A misspelt field name
 * reads as a typo rather than as a policy defect, which is the mistake people actually
 * make at a JSON textarea.
 *
 * Saving is allowed whatever Check said. A case that fails on purpose - written to pin a
 * bug before fixing it - is a legitimate thing to keep, and a form that refused it would
 * be enforcing an opinion the user did not ask for.
 */
export function ManualTestForm({ content, onSave, onCancel }: Props) {
  const [name, setName] = useState('');
  const [inputText, setInputText] = useState('');
  const [expectedText, setExpectedText] = useState('');
  const [result, setResult] = useState<CheckTestResult | null>(null);
  const [checking, setChecking] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);

  const input = parse(inputText);
  const expected = parse(expectedText);
  const malformed = input.error ?? expected.error;

  const check = async () => {
    if (malformed) return;
    setChecking(true);
    setFailed(null);
    try {
      setResult(await api.checkTest(content, input.value, expected.value));
    } catch (e) {
      setFailed(e instanceof Error ? e.message : 'Could not check this case.');
      setResult(null);
    } finally {
      setChecking(false);
    }
  };

  const save = () => {
    onSave({
      id: null,
      name: name.trim() || 'Untitled case',
      input: input.value,
      expectedOutput: expected.value,
      enabled: true,
      order: 0,
    });
  };

  return (
    <div className="space-y-2.5 border-b border-border bg-bg-subtle px-3 py-3 text-xs">
      <div className="flex items-center gap-2">
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="What this case pins down"
          className="min-w-0 flex-1 rounded border border-border bg-bg px-2 py-1 text-xs outline-none focus:border-accent"
        />
        <button onClick={onCancel} aria-label="Discard this case" className="text-fg-subtle hover:text-fg">
          <X size={14} />
        </button>
      </div>

      <JsonField
        label="Input"
        hint={result?.accepts.length ? `reads ${result.accepts.join(', ')}` : undefined}
        value={inputText}
        onChange={setInputText}
        error={input.error}
        placeholder={'{ "orderTotal": 80 }'}
      />
      <JsonField
        label="Expected output"
        hint={result?.produces.length ? `writes ${result.produces.join(', ')}` : undefined}
        value={expectedText}
        onChange={setExpectedText}
        error={expected.error}
        placeholder={'{ "shippingFee": 0 }'}
      />

      <div className="flex items-center gap-2">
        <Button size="sm" icon={<Play size={12} />} onClick={check} loading={checking}
                disabled={Boolean(malformed)}>
          Check
        </Button>
        <Button size="sm" variant="primary" icon={<Check size={12} />} onClick={save}
                disabled={Boolean(malformed)}>
          Save
        </Button>
        {malformed ? <span className="text-2xs text-danger">Fix the JSON first</span> : null}
      </div>

      {failed ? <p className="text-2xs text-danger">{failed}</p> : null}
      {result ? <Verdict result={result} /> : null}
    </div>
  );
}

function JsonField({
  label, hint, value, onChange, error, placeholder,
}: {
  label: string;
  hint?: string;
  value: string;
  onChange: (v: string) => void;
  error: string | null;
  placeholder: string;
}) {
  return (
    <div>
      <div className="mb-1 flex items-baseline gap-2">
        <span className="font-semibold text-fg-muted">{label}</span>
        {/* Filled in by the first Check: the graph's own field names, so the author is not
            guessing at spelling from memory. */}
        {hint ? <span className="truncate font-mono text-2xs text-fg-subtle">{hint}</span> : null}
      </div>
      <textarea
        rows={2}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        spellCheck={false}
        className={cx(
          'w-full resize-y rounded border bg-bg px-2 py-1.5 font-mono text-2xs outline-none',
          error ? 'border-danger' : 'border-border focus:border-accent',
        )}
      />
      {error ? <p className="mt-0.5 text-2xs text-danger">{error}</p> : null}
    </div>
  );
}

function Verdict({ result }: { result: CheckTestResult }) {
  if (!result.ran) {
    return (
      <div className="rounded border border-danger bg-danger-subtle p-2 text-2xs text-danger">
        <p className="font-semibold">The policy could not run this input</p>
        <p className="mt-0.5 font-mono leading-relaxed">{result.error}</p>
      </div>
    );
  }

  return (
    <div className="space-y-1.5">
      {result.unknown_fields.length > 0 ? (
        // Ahead of the value comparison, because it explains it: an expectation on a field
        // nothing writes will always read as "expected 5, got null", which looks like the
        // policy is broken when the name is simply misspelt.
        <div className="rounded border border-warning bg-warning-subtle p-2 text-2xs text-warning">
          <span className="font-semibold">
            {result.unknown_fields.join(', ')}
          </span>
          {result.unknown_fields.length === 1 ? ' is not something' : ' are not things'} this
          policy produces{result.produces.length ? ` — it writes ${result.produces.join(', ')}` : ''}.
        </div>
      ) : null}

      <div
        className={cx(
          'rounded border p-2 text-2xs',
          result.matches
            ? 'border-success bg-success-subtle text-success'
            : 'border-border bg-bg text-fg-muted',
        )}
      >
        <p className="font-semibold">
          {result.matches ? 'Matches — the policy agrees' : 'Does not match'}
        </p>
        <p className="mt-1 font-mono leading-relaxed text-fg">
          got {JSON.stringify(result.actual)}
        </p>
        {result.mismatches.map((m) => (
          <p key={m.path} className="mt-0.5 font-mono leading-relaxed">
            <span className="text-fg">{m.path}</span>
            {' expected '}
            <span className="text-success">{JSON.stringify(m.expected)}</span>
            {' but the policy returns '}
            <span className="text-danger">{JSON.stringify(m.actual)}</span>
          </p>
        ))}
      </div>
    </div>
  );
}
