"use client";

import { AppShell } from "@/components/app-shell";
import { fetchTradingRuntimeStatus } from "@/features/trading/api";
import { navigation } from "@/lib/navigation";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, type ReactNode } from "react";

export default function AppLayout({
  children,
}: Readonly<{
  children: ReactNode;
}>) {
  return (
    <Suspense fallback={<AppShell navigation={navigation} activeHref="/dashboard"><div className="p-4 text-sm text-muted-foreground">Loading workspace…</div></AppShell>}>
      <AppLayoutContent>{children}</AppLayoutContent>
    </Suspense>
  );
}

function AppLayoutContent({ children }: Readonly<{ children: ReactNode }>) {
  const pathname = usePathname();
  const router = useRouter();
  const searchParams = useSearchParams();

  useEffect(() => {
    let disposed = false;
    async function verifySession() {
      try {
        const status = await fetchTradingRuntimeStatus();
        if (!disposed && !status.appAuthenticated) {
          const search = searchParams.toString();
          const next = `${pathname && pathname !== "/" ? pathname : "/dashboard"}${search ? `?${search}` : ""}`;
          router.replace(`/login?next=${encodeURIComponent(next)}`);
        }
      } catch {
        if (!disposed) {
          const search = searchParams.toString();
          const next = `${pathname && pathname !== "/" ? pathname : "/dashboard"}${search ? `?${search}` : ""}`;
          router.replace(`/login?next=${encodeURIComponent(next)}`);
        }
      }
    }
    void verifySession();
    return () => {
      disposed = true;
    };
  }, [pathname, router, searchParams]);

  return <AppShell navigation={navigation} activeHref={pathname || "/dashboard"}>{children}</AppShell>;
}
