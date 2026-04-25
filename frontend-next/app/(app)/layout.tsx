"use client";

import { AppShell } from "@/components/app-shell";
import { fetchRuntimeStatus } from "@/lib/options/api";
import { navigation } from "@/lib/navigation";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useEffect, type ReactNode } from "react";

export default function AppLayout({
  children,
}: Readonly<{
  children: ReactNode;
}>) {
  const pathname = usePathname();
  const router = useRouter();
  const searchParams = useSearchParams();

  useEffect(() => {
    let disposed = false;
    async function verifySession() {
      try {
        const status = await fetchRuntimeStatus();
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
