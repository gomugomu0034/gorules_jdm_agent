'use client';
import { JdmConfigProvider, DecisionGraph, DecisionGraphType } from '@gorules/jdm-editor';
import '@gorules/jdm-editor/dist/style.css';

interface Props {
  value: DecisionGraphType;
  onChange: (val: DecisionGraphType) => void;
}

export default function JdmEditorClient({ value, onChange }: Props) {
  return (
    <JdmConfigProvider>
      <DecisionGraph value={value} onChange={onChange as any} />
    </JdmConfigProvider>
  );
}