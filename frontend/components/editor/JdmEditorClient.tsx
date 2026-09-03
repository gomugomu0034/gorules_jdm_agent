'use client';

import {
  calculateDiffGraph,
  DecisionGraph,
  JdmConfigProvider,
  nodeSpecification,
  type DecisionGraphType,
  type Simulation,
} from '@gorules/jdm-editor';
import '@gorules/jdm-editor/dist/style.css';
import { FlaskConical, Play } from 'lucide-react';
import { useMemo } from 'react';

import { antdTheme } from '../../lib/theme';
import { useUiStore } from '../../stores/useUiStore';
import { SimulatorPanel } from './SimulatorPanel';
import { TestRunnerPanel } from './TestRunnerPanel';

type Props = {
  value: DecisionGraphType;
  /** When set, the canvas renders a diff of this against `value`. */
  proposed?: DecisionGraphType;
  onChange: (value: DecisionGraphType) => void;
  name?: string;
  disabled?: boolean;
  simulation?: Simulation;
  onSimulate: (payload: { graph: DecisionGraphType; context: unknown }) => void;
  simulating?: boolean;
  onClearSimulation: () => void;
};

export default function JdmEditorClient({
  value,
  proposed,
  onChange,
  name,
  disabled,
  simulation,
  onSimulate,
  simulating,
  onClearSimulation,
}: Props) {
  const theme = useUiStore((s) => s.theme);

  /**
   * While a proposal is live the canvas shows the editor's own diff overlay:
   * calculateDiffGraph stamps `_diff` on nodes and edges and DecisionGraph
   * renders the add/remove/change styling natively.
   */
  const displayed = useMemo(() => {
    if (!proposed) return value;
    try {
      return calculateDiffGraph(proposed, value, {
        components: nodeSpecification as never,
        customNodes: [],
      }) as DecisionGraphType;
    } catch {
      return proposed;
    }
  }, [proposed, value]);

  // Keyed on the theme so antd recomputes its palette when the toggle flips.
  const config = useMemo(() => antdTheme(theme), [theme]);

  // The simulator and test runner belong in the editor's own panel rail: they
  // are per-graph inspectors. The chat pane deliberately does not, because the
  // rail unmounts whichever panel is not selected and that would kill its
  // event stream mid-run.
  const panels = useMemo(
    () => [
      {
        id: 'simulator',
        title: 'Simulate',
        icon: <Play size={14} />,
        renderPanel: () => (
          <SimulatorPanel
            onRun={onSimulate}
            loading={simulating}
            onClear={onClearSimulation}
          />
        ),
      },
      {
        id: 'tests',
        title: 'Tests',
        icon: <FlaskConical size={14} />,
        renderPanel: () => <TestRunnerPanel />,
      },
    ],
    [onSimulate, simulating, onClearSimulation],
  );

  return (
    <JdmConfigProvider key={theme} theme={config}>
      <div className="jdm-surface">
        <DecisionGraph
          value={displayed}
          onChange={onChange as never}
          disabled={disabled}
          name={name}
          panels={panels}
          // No panel opens by default: the canvas is the primary surface, and
          // the simulator would otherwise claim half of it before it is asked for.
          simulate={simulation}
          mode="dev"
        />
      </div>
    </JdmConfigProvider>
  );
}
