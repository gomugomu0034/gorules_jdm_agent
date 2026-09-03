import { apiUrl } from './api';
import type { ChatEvent } from './types';

type Options = {
  threadId: string;
  fromSeq: number;
  onEvent: (event: ChatEvent) => void;
  onError?: (error: Event) => void;
};

/**
 * Subscribes to a thread's event stream.
 *
 * Every event is persisted with a sequence number server-side, so reconnecting
 * with `from_seq` replays whatever arrived while the connection was down - a
 * laptop that sleeps through a four-minute build loses nothing.
 */
export function createEventStream({ threadId, fromSeq, onEvent, onError }: Options) {
  let lastSeq = fromSeq;
  let source: EventSource | null = null;
  let retryDelay = 1000;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let closed = false;

  const connect = () => {
    if (closed) return;

    source = new EventSource(
      apiUrl(`/api/chat/threads/${threadId}/stream?from_seq=${lastSeq}`),
    );

    source.onopen = () => {
      retryDelay = 1000;
    };

    source.onmessage = handle;
    // The server names each frame, so listen for the union's members explicitly.
    for (const type of [
      'run_started',
      'node_start',
      'node_end',
      'progress',
      'message',
      'interrupt',
      'graph_proposed',
      'test_report',
      'error',
      'done',
    ]) {
      source.addEventListener(type, handle as EventListener);
    }

    source.onerror = (event) => {
      onError?.(event);
      source?.close();
      source = null;
      if (closed) return;
      reconnectTimer = setTimeout(connect, retryDelay);
      retryDelay = Math.min(retryDelay * 2, 8000);
    };
  };

  function handle(event: MessageEvent) {
    let parsed: ChatEvent;
    try {
      parsed = JSON.parse(event.data);
    } catch {
      return;
    }
    if (typeof parsed.seq === 'number') {
      if (parsed.seq <= lastSeq) return; // already applied
      lastSeq = parsed.seq;
    }
    onEvent(parsed);
  }

  connect();

  return {
    close() {
      closed = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      source?.close();
      source = null;
    },
    get lastSeq() {
      return lastSeq;
    },
  };
}
