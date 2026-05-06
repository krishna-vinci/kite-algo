export type RuntimeStatus = {
  brokerConnected: boolean;
  brokerStatus: "connected" | "reconnecting" | "degraded" | "disconnected" | "unknown";
  brokerMode: "system";
  brokerLastSuccessAt: string | null;
  brokerLastFailureAt: string | null;
  brokerLastError: string | null;
  brokerNextRefreshAt: string | null;
  websocketStatus: string;
  paperAvailable: boolean;
  appAuthenticated: boolean;
};
