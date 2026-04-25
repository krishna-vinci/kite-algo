"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { cancelControlStrategyOrders, exitControlStrategy } from "@/features/trading/api";
import type { ControlStrategyGroup } from "@/features/trading/types";

type Props = {
  strategy: ControlStrategyGroup;
};

export function ControlStrategyActions({ strategy }: Props) {
  const queryClient = useQueryClient();
  const canExit = strategy.allowedActions.includes("exit_strategy");
  const canCancel = strategy.allowedActions.includes("cancel_orders");

  const exitMutation = useMutation({
    mutationFn: () => exitControlStrategy(strategy.strategyRunId, { reason: "operator_exit" }),
    onSuccess: async () => {
      toast.success("Exit submitted");
      await queryClient.invalidateQueries({ queryKey: ["control-plane", "strategy-positions"] });
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "Exit failed"),
  });

  const cancelMutation = useMutation({
    mutationFn: () => cancelControlStrategyOrders(strategy.strategyRunId, { reason: "operator_cancel" }),
    onSuccess: async () => {
      toast.success("Cancel submitted");
      await queryClient.invalidateQueries({ queryKey: ["control-plane", "strategy-positions"] });
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "Cancel failed"),
  });

  return (
    <div className="flex flex-wrap items-center gap-2">
      <button
        type="button"
        disabled={!canExit || exitMutation.isPending}
        title={!canExit ? strategy.actionReasons.exit_strategy : undefined}
        onClick={() => exitMutation.mutate()}
        className="rounded-md border border-rose-500/40 px-2 py-1 text-[10px] font-semibold uppercase tracking-wider text-rose-300 disabled:cursor-not-allowed disabled:border-border/40 disabled:text-foreground/30"
      >
        Exit strategy
      </button>
      <button
        type="button"
        disabled={!canCancel || cancelMutation.isPending}
        title={!canCancel ? strategy.actionReasons.cancel_orders : undefined}
        onClick={() => cancelMutation.mutate()}
        className="rounded-md border border-amber-500/40 px-2 py-1 text-[10px] font-semibold uppercase tracking-wider text-amber-300 disabled:cursor-not-allowed disabled:border-border/40 disabled:text-foreground/30"
      >
        Cancel orders
      </button>
    </div>
  );
}
