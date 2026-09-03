import { StudioClient } from '../../../components/editor/StudioClient';

export default function GraphPage({ params }: { params: { id: string } }) {
  return <StudioClient graphId={params.id} />;
}
