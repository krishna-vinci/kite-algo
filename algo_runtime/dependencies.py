from __future__ import annotations

from typing import Dict, Iterable, Set

from .models import DependencySpec


class DependencyAggregator:
    def aggregate(self, dependency_specs: Iterable[DependencySpec]) -> DependencySpec:
        aggregated = DependencySpec()
        for dependency_spec in dependency_specs:
            aggregated = aggregated.merged_with(dependency_spec)
        return aggregated

    def summarize(self, dependency_specs: Iterable[DependencySpec]) -> Dict[str, object]:
        specs = list(dependency_specs)
        aggregated = self.aggregate(specs)
        return {
            "market_tokens": dict(aggregated.market_tokens),
            "candle_series": [spec.key for spec in aggregated.candle_series],
            "indicator_keys": [spec.key for spec in aggregated.indicators],
            "option_reads": [
                f"{spec.underlying}:{spec.expiry_mode.value}:{spec.view.value}:{spec.strikes_around_atm}:{spec.expiry or ''}"
                for spec in aggregated.option_reads
            ],
            "account_scopes": self.account_scopes(specs),
            "triggers": {trigger.value for trigger in aggregated.triggers},
        }

    def account_scopes(self, dependency_specs: Iterable[DependencySpec]) -> Set[str]:
        return {spec.account_scope for spec in dependency_specs if spec.account_scope}
