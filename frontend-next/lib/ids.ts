/**
 * Client-side identity helpers.
 *
 * Idempotency keys are how the API deduplicates an *uncertain* request: if a
 * create or a launch is retried with the same key, the server returns the
 * original result instead of doing the work twice. That makes the key's
 * uniqueness and stability both load-bearing:
 *
 * - it must be unique across operators and attempts (a collision could adopt
 *   someone else's resource, or silently replay an old request), and
 * - it must NOT change while the operator retries the same request.
 *
 * `crypto.randomUUID()` is the obvious source, but it only exists in a **secure
 * context**: an operator who reaches the app over plain HTTP on a LAN address
 * (`http://192.168.x.x:13000`) gets `crypto.randomUUID is not a function`, which
 * is exactly how alert and screener creation failed in the deployed stack.
 *
 * `crypto.getRandomValues` has no such restriction, so this module builds a
 * UUIDv4 from it when `randomUUID` is missing. Only if Web Crypto is entirely
 * absent (very old browser, or a test environment that stubs it away) does it
 * fall back to timestamp + counter — never `Math.random()`, which is neither
 * collision-safe nor unpredictable enough to serve as an identity.
 */

const HEX = "0123456789abcdef";

let counter = 0;

/** Format 16 random bytes as a UUIDv4 (RFC 4122 §4.4). */
function formatUuidV4(bytes: Uint8Array): string {
  let hex = "";
  for (let index = 0; index < 16; index += 1) {
    hex += `${HEX[bytes[index] >> 4]}${HEX[bytes[index] & 0x0f]}`;
  }
  // 32 hex chars: [12] is the version nibble, [16] the RFC 4122 variant nibble.
  const variant = HEX[(parseInt(hex[16], 16) & 0x3) | 0x8];
  const shaped = `${hex.slice(0, 12)}4${hex.slice(13, 16)}${variant}${hex.slice(17)}`;
  return (
    `${shaped.slice(0, 8)}-${shaped.slice(8, 12)}-${shaped.slice(12, 16)}` +
    `-${shaped.slice(16, 20)}-${shaped.slice(20)}`
  );
}

function fromGetRandomValues(): string | null {
  if (typeof crypto === "undefined" || typeof crypto.getRandomValues !== "function") {
    return null;
  }
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return formatUuidV4(bytes);
}

function fromClock(): string {
  counter += 1;
  const now = Date.now();
  const fractional =
    typeof performance !== "undefined" && typeof performance.now === "function"
      ? Math.floor(performance.now() * 1000)
      : 0;
  // Uniqueness inside this tab comes from the counter; across tabs/operators
  // from the millisecond clock + high-resolution remainder.
  return `${now.toString(36)}-${counter.toString(36)}-${fractional.toString(36)}`;
}

/**
 * A fresh, unique identifier for one request. Safe on non-secure origins.
 */
export function randomId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return fromGetRandomValues() ?? fromClock();
}

/** A fresh idempotency key, optionally namespaced for readability in logs. */
export function newIdempotencyKey(prefix?: string): string {
  const value = randomId();
  return prefix ? `${prefix}-${value}` : value;
}

/** True when this browser can produce cryptographic identifiers. */
export function hasStrongRandomness(): boolean {
  return (
    typeof crypto !== "undefined" &&
    (typeof crypto.randomUUID === "function" || typeof crypto.getRandomValues === "function")
  );
}
