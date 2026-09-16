"use client";

/**
 * The unsaved-work guard.
 *
 * Dirty editors register `beforeunload` (tab close, refresh) and route their
 * OWN exit affordances (the header chevron) through `attemptExit`, which asks
 * before leaving. Browser back/forward and AppShell sidebar clicks are not
 * interceptable in the App Router — that limitation is accepted in the design;
 * refresh/close, the dominant loss path, is covered.
 */

import { useCallback, useEffect, useState, type ReactNode } from "react";
import { useRouter } from "next/navigation";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

export function useDirtyGuard(isDirty: boolean): { attemptExit: (href: string) => void; dialog: ReactNode } {
  const router = useRouter();
  const [pendingHref, setPendingHref] = useState<string | null>(null);

  useEffect(() => {
    if (!isDirty) return;
    const handler = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [isDirty]);

  const attemptExit = useCallback(
    (href: string) => {
      if (!isDirty) {
        router.push(href);
        return;
      }
      setPendingHref(href);
    },
    [isDirty, router],
  );

  const confirmExit = useCallback(() => {
    const href = pendingHref;
    setPendingHref(null);
    if (href) router.push(href);
  }, [pendingHref, router]);

  const cancelExit = useCallback(() => setPendingHref(null), []);

  const dialog = (
    <Dialog
      open={pendingHref !== null}
      onOpenChange={(open) => {
        if (!open) cancelExit();
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Leave without saving?</DialogTitle>
          <DialogDescription>
            Your changes to this draft have not been saved. Leaving now discards them.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button type="button" variant="outline" onClick={cancelExit}>
            Keep editing
          </Button>
          <Button type="button" variant="destructive" onClick={confirmExit}>
            Leave without saving
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );

  return { attemptExit, dialog };
}
