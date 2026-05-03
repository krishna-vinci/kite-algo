import { apiFetch } from "@/lib/api/client";

export async function loginApp(payload: { username: string; password: string }): Promise<{ user?: { username: string; role: string } }> {
  return apiFetch("/api/auth/login", {
    method: "POST",
    json: payload,
  });
}
