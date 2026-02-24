"""
regression.py - Regression gate that aggregates all CI quality checks.

Every PR is tested against a configurable set of thresholds derived from the
quantum-ci.yaml file.  Failing any enabled check causes the pipeline to exit
1, blocking the merge.  All check results are surfaced in the PR comment with
per-check pass/fail badges, measured values, and threshold references.

Checks implemented
------------------
1. Behavioral Fidelity (TVD)
   Blocks if the output distribution diverges more than ``tvd_block_threshold``
   from the base branch.  Catches gate errors, parameter drift, and logic bugs
   that change observable circuit behaviour.

2. Transpilation Fidelity Decay
   Blocks if the PR's transpiled circuit requires more than
   ``transpilation_decay_threshold_pct`` percent more native 2-qubit gates
   than the base.  Prevents QPU cost regressions.

3. Circuit Depth Regression (optional)
   Blocks if circuit depth grows more than ``max_depth_increase_pct`` percent.
   Useful for latency-sensitive applications where deeper circuits are more
   susceptible to decoherence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .analyzer import CircuitStats
from .transpiler import FidelityDecayResult


# ── Data classes ────────────────────────────────────────────────────────────────


@dataclass
class RegressionCheck:
    """Result of a single named regression check."""

    name: str
    passed: bool
    value: float
    threshold: float
    unit: str = ""
    message: str = ""


@dataclass
class RegressionResult:
    """Aggregated outcome of all regression checks for one PR."""

    checks: list[RegressionCheck] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """True only when every individual check passed."""
        return all(c.passed for c in self.checks)

    @property
    def failed_checks(self) -> list[RegressionCheck]:
        return [c for c in self.checks if not c.passed]

    def add(
        self,
        name: str,
        passed: bool,
        value: float,
        threshold: float,
        unit: str = "",
        message: str = "",
    ) -> None:
        self.checks.append(
            RegressionCheck(
                name=name,
                passed=passed,
                value=value,
                threshold=threshold,
                unit=unit,
                message=message,
            )
        )


# ── Public API ─────────────────────────────────────────────────────────────────


def run_regression_checks(
    pr_stats: Optional[CircuitStats],
    base_stats: Optional[CircuitStats],
    tvd: Optional[float],
    decay_result: Optional[FidelityDecayResult],
    config: dict,
) -> RegressionResult:
    """
    Run all configured regression checks and return an aggregated result.

    Parameters
    ----------
    pr_stats:
        Structural metrics from the PR branch circuit.
    base_stats:
        Structural metrics from the base branch circuit (may be None for new
        circuits).
    tvd:
        Total Variation Distance between PR and base shot distributions.
    decay_result:
        Transpilation fidelity decay comparison result.
    config:
        Parsed quantum-ci.yaml dict.  Thresholds are read from the
        ``analysis`` sub-section.

    Returns
    -------
    RegressionResult with one RegressionCheck entry per enabled check.
    """
    result = RegressionResult()
    analysis = config.get("analysis", {})

    # Thresholds — fall back through warn threshold → hard-coded safe defaults.
    tvd_block = float(
        analysis.get(
            "tvd_block_threshold",
            analysis.get("tvd_warn_threshold", 0.1),
        )
    )
    decay_thresh = float(analysis.get("transpilation_decay_threshold_pct", 15.0))
    depth_thresh = analysis.get("max_depth_increase_pct", None)

    # ── 1. Behavioral Fidelity (TVD) ──────────────────────────────────────────
    if tvd is not None:
        ok = tvd <= tvd_block
        cmp = "≤" if ok else ">"
        result.add(
            name="Behavioral Fidelity (TVD)",
            passed=ok,
            value=round(tvd, 6),
            threshold=tvd_block,
            unit="",
            message=f"TVD `{tvd:.4f}` {cmp} block threshold `{tvd_block}`",
        )

    # ── 2. Transpilation Fidelity Decay ───────────────────────────────────────
    if decay_result is not None and decay_result.base_stats is not None:
        ok = not decay_result.exceeds_threshold
        cmp = "≤" if ok else ">"
        result.add(
            name="Transpilation Fidelity Decay",
            passed=ok,
            value=decay_result.decay_pct,
            threshold=decay_thresh,
            unit="%",
            message=(
                f"Native 2Q-gate count change: `{decay_result.decay_pct:+.1f}%` "
                f"{cmp} `{decay_thresh:.0f}%` threshold"
            ),
        )

    # ── 3. Circuit Depth Regression (optional) ────────────────────────────────
    if (
        depth_thresh is not None
        and pr_stats is not None
        and base_stats is not None
        and base_stats.depth > 0
    ):
        depth_delta_pct = (pr_stats.depth - base_stats.depth) / base_stats.depth * 100
        ok = depth_delta_pct <= float(depth_thresh)
        cmp = "≤" if ok else ">"
        result.add(
            name="Circuit Depth Regression",
            passed=ok,
            value=round(depth_delta_pct, 2),
            threshold=float(depth_thresh),
            unit="%",
            message=(
                f"Depth change `{depth_delta_pct:+.1f}%` "
                f"{cmp} `{depth_thresh:.0f}%` threshold"
            ),
        )

    return result
