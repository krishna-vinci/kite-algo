import { apiFetch } from "@/lib/api/client";
import type { JournalEnvironment } from "./types";

function normalizeJournalEnvironment(item: Record<string, unknown>): JournalEnvironment {
  return {
    id: String(item.id ?? ""),
    mode: String(item.mode ?? "paper") as JournalEnvironment["mode"],
    account_scope: String(item.account_scope ?? ""),
    display_name: item.display_name != null ? String(item.display_name) : null,
    broker_user_id: item.broker_user_id != null ? String(item.broker_user_id) : null,
    paper_account_key: item.paper_account_key != null ? String(item.paper_account_key) : null,
    environment_epoch: Number(item.environment_epoch ?? 1),
    metadata: (item.metadata as Record<string, unknown> | undefined) ?? {},
  };
}

export async function fetchJournalEnvironments(): Promise<JournalEnvironment[]> {
  const response = await apiFetch<{ items?: Array<Record<string, unknown>> }>("/api/journal/v2/environments");
  return (response.items ?? []).map(normalizeJournalEnvironment);
}
