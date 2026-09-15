/**
 * The idempotency helper must work on the origins operators actually use.
 *
 * Regression: alert and screener creation called `crypto.randomUUID()` directly.
 * That function only exists in a secure context, so on
 * `http://192.168.0.128:13000` the browser threw `crypto.randomUUID is not a
 * function` and the form showed "Could not save" — creation was impossible over
 * plain HTTP.
 */

import { describe, expect, it, vi, afterEach } from "vitest";

import { hasStrongRandomness, newIdempotencyKey, randomId } from "@/lib/ids";

const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

const original = {
  randomUUID: crypto.randomUUID,
  getRandomValues: crypto.getRandomValues,
};

afterEach(() => {
  Object.defineProperty(globalThis.crypto, "randomUUID", {
    value: original.randomUUID,
    configurable: true,
    writable: true,
  });
  Object.defineProperty(globalThis.crypto, "getRandomValues", {
    value: original.getRandomValues,
    configurable: true,
    writable: true,
  });
  vi.restoreAllMocks();
});

function withoutRandomUUID() {
  Object.defineProperty(globalThis.crypto, "randomUUID", {
    value: undefined,
    configurable: true,
    writable: true,
  });
  // this is what a non-secure-context browser still provides: a CSPRNG. The
  // stub varies per call so uniqueness is observable.
  let call = 0;
  Object.defineProperty(globalThis.crypto, "getRandomValues", {
    value: (array: Uint8Array) => {
      call += 1;
      for (let index = 0; index < array.length; index += 1) {
        array[index] = (index * 31 + call * 17 + 7) & 0xff;
      }
      return array;
    },
    configurable: true,
    writable: true,
  });
}

describe("randomId", () => {
  it("uses crypto.randomUUID when the origin is secure", () => {
    const id = randomId();
    expect(id).toMatch(UUID_V4);
  });

  it("falls back to getRandomValues on a non-secure origin", () => {
    withoutRandomUUID();
    const id = randomId();
    expect(id).toMatch(UUID_V4);
    expect(hasStrongRandomness()).toBe(true);
  });

  it("produces a version-4 uuid with the RFC variant bits", () => {
    withoutRandomUUID();
    const [a, b, c, d, e] = randomId().split("-");
    expect([a, b, d, e].every((part) => /^[0-9a-f]+$/.test(part))).toBe(true);
    expect(c[0]).toBe("4");
    expect("89ab").toContain(d[0]);
  });

  it("never returns the same value twice", () => {
    withoutRandomUUID();
    const values = new Set(Array.from({ length: 200 }, () => randomId()));
    expect(values.size).toBe(200);
  });

  it("still returns a unique value when Web Crypto is unavailable entirely", () => {
    Object.defineProperty(globalThis.crypto, "randomUUID", { value: undefined, configurable: true });
    Object.defineProperty(globalThis.crypto, "getRandomValues", { value: undefined, configurable: true });
    expect(hasStrongRandomness()).toBe(false);
    const values = new Set(Array.from({ length: 50 }, () => randomId()));
    expect(values.size).toBe(50);
  });

  it("never uses Math.random for identities", () => {
    Object.defineProperty(globalThis.crypto, "randomUUID", { value: undefined, configurable: true });
    Object.defineProperty(globalThis.crypto, "getRandomValues", { value: undefined, configurable: true });
    const spy = vi.spyOn(Math, "random");
    randomId();
    expect(spy).not.toHaveBeenCalled();
  });
});

describe("newIdempotencyKey", () => {
  it("is unique per call and can be namespaced", () => {
    const first = newIdempotencyKey("alert");
    const second = newIdempotencyKey("alert");
    expect(first).toMatch(/^alert-[0-9a-f-]{36}$/);
    expect(first).not.toBe(second);
  });

  it("works on a non-secure origin", () => {
    withoutRandomUUID();
    expect(newIdempotencyKey()).toMatch(UUID_V4);
  });
});
