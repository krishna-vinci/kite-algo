import { redirect } from "next/navigation";

export default async function AnalyticsCostsRedirect({
  searchParams,
}: {
  searchParams?: Promise<Record<string, string | string[] | undefined>>;
}) {
  const resolved = (await searchParams) ?? {};
  const sp = new URLSearchParams();
  for (const [key, value] of Object.entries(resolved)) {
    if (typeof value === "string") sp.set(key, value);
    else if (Array.isArray(value) && value[0]) sp.set(key, value[0]);
  }
  const qs = sp.toString();
  redirect(`/journal/analytics/costs${qs ? `?${qs}` : ""}`);
}
