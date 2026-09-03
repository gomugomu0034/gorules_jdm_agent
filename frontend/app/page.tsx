import { StudioClient } from '../components/editor/StudioClient';

/**
 * The landing screen: an empty canvas beside the assistant.
 *
 * Visitors start here rather than at the library, because with no policies of
 * their own a library is an empty room. Describing a policy to the assistant
 * is the first useful thing to do, and the result opens on the canvas as an
 * unsaved draft.
 */
export default function HomePage() {
  return <StudioClient graphId={null} />;
}
