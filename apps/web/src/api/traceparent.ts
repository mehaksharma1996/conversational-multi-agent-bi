/**
 * W3C Trace Context `traceparent` for outgoing API requests (issue #15).
 *
 * Only random identifiers are sent: no user, workspace, file or question data, and no `tracestate` or
 * `baggage`. The API turns the value into the parent of its server span when tracing is enabled and
 * otherwise ignores it. The browser exports no spans of its own, so the parent span ID names a span that
 * a backend will not hold; the trace simply starts at the API root.
 */

function randomHex(byteLength: number): string {
  const bytes = new Uint8Array(byteLength);
  // An all-zero trace or span ID is invalid in the specification, so draw again.
  do {
    crypto.getRandomValues(bytes);
  } while (bytes.every((byte) => byte === 0));
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

export function newTraceparent(): string {
  // version 00, sampled flag 01 so a head-sampling API keeps the trace
  return `00-${randomHex(16)}-${randomHex(8)}-01`;
}
