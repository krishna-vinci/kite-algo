"use client";

import { AppShell } from "@/components/app-shell";
import { navigation } from "@/lib/navigation";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

export default function AppLayout({
  children,
}: Readonly<{
  children: ReactNode;
}>) {
  const pathname = usePathname();

  return <AppShell navigation={navigation} activeHref={pathname || "/dashboard"}>{children}</AppShell>;
}
