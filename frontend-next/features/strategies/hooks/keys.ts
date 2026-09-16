export const hostedKeys = {
  options: () => ["hosted", "options"] as const,
  strategies: () => ["hosted", "strategies"] as const,
  strategy: (strategyId: string) => ["hosted", "strategy", strategyId] as const,
  versions: (strategyId: string) => ["hosted", "versions", strategyId] as const,
  jobs: (strategyId: string) => ["hosted", "jobs", strategyId] as const,
  job: (strategyId: string, jobId: string) => ["hosted", "job", strategyId, jobId] as const,
  logs: (strategyId: string, jobId: string) => ["hosted", "logs", strategyId, jobId] as const,
  notifications: (strategyId: string, jobId: string) =>
    ["hosted", "notifications", strategyId, jobId] as const,
  reconciliation: (strategyId: string, jobId: string) =>
    ["hosted", "reconciliation", strategyId, jobId] as const,
};
