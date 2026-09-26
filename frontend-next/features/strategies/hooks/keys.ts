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
  authorization: (strategyId: string) => ["hosted", "authorization", strategyId] as const,
  grants: (strategyId: string) => ["hosted", "grants", strategyId] as const,
  executionRequests: (strategyId: string) =>
    ["hosted", "execution-requests", strategyId] as const,
  pendingApprovals: () => ["hosted", "pending-approvals"] as const,
  admissionPolicy: (strategyId: string) => ["hosted", "admission-policy", strategyId] as const,
  schedule: (strategyId: string) => ["hosted", "schedule", strategyId] as const,
  scheduleOccurrences: (strategyId: string) =>
    ["hosted", "schedule-occurrences", strategyId] as const,
  plan: (strategyId: string, proposalId: string) =>
    ["hosted", "plan", strategyId, proposalId] as const,
  positions: (strategyId: string, environment: string) =>
    ["hosted", "positions", strategyId, environment] as const,
  calendar: (exchange: string, segment: string) =>
    ["hosted", "calendar", exchange, segment] as const,
  optionRuns: (strategyId: string) => ["hosted", "option-runs", strategyId] as const,
  optionRun: (strategyId: string, optionRunId: string) =>
    ["hosted", "option-run", strategyId, optionRunId] as const,
  optionRunRepair: (strategyId: string, optionRunId: string) =>
    ["hosted", "option-run-repair", strategyId, optionRunId] as const,
  pendingWork: (strategyId: string) => ["hosted", "pending-work", strategyId] as const,
  deadSubmission: (strategyId: string, planId: string, stepNo: number) =>
    ["hosted", "dead-submission", strategyId, planId, stepNo] as const,
  optionExit: (strategyId: string, optionRunId: string) =>
    ["hosted", "option-exit", strategyId, optionRunId] as const,
  flatten: (strategyId: string) => ["hosted", "flatten", strategyId] as const,
};
