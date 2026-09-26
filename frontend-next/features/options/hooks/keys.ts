export const optionsKeys = {
  session: (underlying: string) => ["options", "session", underlying] as const,
  expiries: (underlying: string) => ["options", "expiries", underlying] as const,
  chain: (underlying: string, expiry: string) => ["options", "chain", underlying, expiry] as const,
  pcr: (underlying: string, expiry: string) => ["options", "pcr", underlying, expiry] as const,
  maxPain: (underlying: string, expiry: string) => ["options", "max-pain", underlying, expiry] as const,
};
