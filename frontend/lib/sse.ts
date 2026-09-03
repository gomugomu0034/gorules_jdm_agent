import { apiUrl } from './api';
import type { ChatEvent } from './types';

type Options = {
  threadId: string;
  fromSeq: number;
  onEvent: (event: ChatEvent) => void;
  onError?: (error: Event) => void;
  /** The thread no longer exists for this session; reconnecting is pointless. */
  onGone?: () => void;
};

/**
 * Is this thread permanently unreachable, rather than momentarily unavailable?
 *
 * `EventSource` reports every failure as the same contentless `error` event, so
 * a thread that has been swept, deleted, or was created under a session this
 * browser no longer holds is indistinguishable from a backend that is briefly
 * down - and retrying the former logs a 404 every few seconds for as long as
 * the tab stays open. The status code is only reachable through a plain fetch.
 */
async function isGone(threadId: string): Promise<boolean> {
  try {
    const response = await fetch(apiUrl(`/api/chat/threads/${threadId}`), {
      credentials: 'include',
    });
    return response.status === 404 || response.status === 401 || response.status === 403;
  } catch {
    // Could not reach the API at all: that is the retryable case.
    return false;
  }
}

/**
 * Subscribes to a thread's event stream.
 *
 * Every event is persisted with a sequence number server-side, so reconnecting
 * with `from_seq` replays whatever arrived while the connection was down - a
 * laptop that sleeps through a four-minute build loses nothing.
 */
export function createEventStream({ threadId, fromSeq, onEvent, onError, onGone }: Options) {
  let lastSeq = fromSeq;
  let source: EventSource | null = null;
  let retryDelay = 1000;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let closed = false;

  const reconnect = () => {
    if (closed) return;
    reconnectTimer = setTimeout(connect, retryDelay);
    retryDelay = Math.min(retryDelay * 2, 8000);
  };

  const connect = () => {
    if (closed) return;
    // Whether *this* attempt got as far as an open connection. A stream that
    // dies mid-run is worth retrying; one that never opened may be fatal.
    let opened = false;

    source = new EventSource(
      apiUrl(`/api/chat/threads/${threadId}/stream?from_seq=${lastSeq}`),
      // The stream is owner-scoped like every other route, and EventSource
      // cannot set headers - which is exactly why the session is a cookie.
      { withCredentials: true },
    );

    source.onopen = () => {
      opened = true;
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

      if (opened) {
        reconnect();
        return;
      }
      void isGone(threadId).then((gone) => {
        if (closed) return;
        if (!gone) {
          reconnect();
          return;
        }
        closed = true;
        onGone?.();
      });
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
