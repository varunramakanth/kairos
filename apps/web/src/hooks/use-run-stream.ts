"use client";

import { useEffect, useReducer, useRef } from "react";
import { streamRunEvents } from "@/client/sdk.gen";
import type { RunEvent } from "@/client/types.gen";
import "@/lib/api";
import {
  applyEvent,
  buildTurn,
  isRunEvent,
  type TurnView,
} from "@/lib/run-view";

/**
 * The view of one run: its stored events, plus live events while it is active.
 *
 * The stream starts after the last stored `seq`, so nothing is shown twice. The
 * generated SSE client reconnects with `Last-Event-ID` if the connection drops.
 */
export function useRunStream(
  runId: string,
  stored: readonly RunEvent[],
  active: boolean,
  onFinished?: () => void,
): TurnView {
  const [view, dispatch] = useReducer(applyEvent, stored, buildTurn);
  const finishedRef = useRef(onFinished);
  finishedRef.current = onFinished;
  // Where a (re)started stream picks up: whatever this view has already applied.
  const lastSeq = useRef(view.lastSeq);
  lastSeq.current = view.lastSeq;

  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    (async () => {
      const { stream } = await streamRunEvents({
        path: { run_id: runId },
        query: { after: lastSeq.current },
        signal: controller.signal,
        sseMaxRetryAttempts: 5,
      });
      for await (const data of stream) {
        if (!isRunEvent(data)) continue;
        dispatch(data);
        if (data.type === "run_finished") finishedRef.current?.();
      }
    })().catch(() => {
      // Aborted on unmount, or retries exhausted. Stored events are still shown.
    });
    return () => controller.abort();
  }, [runId, active]);

  return view;
}
