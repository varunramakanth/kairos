/**
 * The generated API client, pointed at the Kairos API.
 *
 * Everything under `@/client` is generated from FastAPI's OpenAPI schema by
 * `just gen-client`. Import API functions and types from there; never hand-write
 * request or response types.
 */
import { client } from "@/client/client.gen";

export const API_URL =
  process.env.NEXT_PUBLIC_KAIROS_API_URL ?? "http://localhost:8000";

client.setConfig({ baseUrl: API_URL });

export const THREADS_CHANGED = "kairos:threads-changed";

/** Tell the sidebar to refresh its thread list. */
export function announceThreadsChanged(): void {
  window.dispatchEvent(new Event(THREADS_CHANGED));
}

/** Pull a readable message out of an API error body. */
export function errorMessage(error: unknown): string {
  if (typeof error === "object" && error !== null && "detail" in error) {
    const detail = (error as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
  }
  if (error instanceof Error) return error.message;
  return `Something went wrong. Is the API running on ${API_URL}?`;
}
