"use client";

import { CopyIcon } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";

type OneTimeSecretDialogProps = Readonly<{
  open: boolean;
  title: string;
  description: string;
  /** The revealed secret. Held in component state ONLY — never query state. */
  secret: string | null;
  onClose: () => void;
}>;

/**
 * One-time reveal.
 *
 * The secret is passed in as a plain prop and lives in the caller's component
 * state for exactly as long as the dialog is open. It is deliberately NOT read
 * from a mutation result at render time: query/mutation caches persist across
 * navigation and remounts, so a secret left there would be re-rendered later
 * from memory. Callers copy the value into state and reset the mutation.
 */
export function OneTimeSecretDialog({
  open,
  title,
  description,
  secret,
  onClose,
}: OneTimeSecretDialogProps) {
  const [copied, setCopied] = useState(false);

  return (
    <Dialog open={open} onOpenChange={(next) => (!next ? onClose() : undefined)}>
      <DialogContent showCloseButton={false}>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-2">
          <Label htmlFor="one-time-secret">Secret</Label>
          <code
            id="one-time-secret"
            className="block break-all rounded-md border border-border/70 bg-background/60 p-3 font-mono text-xs"
          >
            {secret ?? "—"}
          </code>
          <p className="text-xs text-rose-300">
            This is the only time this value is shown. It is stored as a hash and cannot be
            retrieved again — if you lose it, revoke this credential and issue a new one.
          </p>
        </div>

        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => {
              if (secret) {
                void navigator.clipboard?.writeText(secret);
                setCopied(true);
              }
            }}
          >
            <CopyIcon className="size-4" aria-hidden />
            {copied ? "Copied" : "Copy"}
          </Button>
          <Button onClick={onClose}>I have stored it</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
