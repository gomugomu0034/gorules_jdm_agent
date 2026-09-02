'use client';

import { GraphSimulator, type DecisionGraphType } from '@gorules/jdm-editor';

type Props = {
  onRun: (payload: { graph: DecisionGraphType; context: unknown }) => void;
  loading?: boolean;
  onClear: () => void;
};

/**
 * The editor's own simulator, wired to POST /api/simulate.
 *
 * Zen's `evaluate(ctx, {trace: true})` already returns `{performance, result,
 * trace}`, which is exactly the editor's `SimulationOk` minus `snapshot` - the
 * backend adds that, so no translation happens on either side.
 */
export function SimulatorPanel({ onRun, loading, onClear }: Props) {
  return (
    <GraphSimulator
      loading={loading}
      onRun={onRun}
      onClear={onClear}
      defaultRequest={JSON.stringify({}, null, 2)}
    />
  );
}
