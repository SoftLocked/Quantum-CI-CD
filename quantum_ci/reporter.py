"""
reporter.py - Markdown report generation and GitHub PR comment upsert.

Every posted comment contains MARKER (an HTML comment invisible in rendered
Markdown) so subsequent runs find and PATCH the existing comment rather than
posting new ones.

Report sections
---------------
1. Build Status
2. Regression Checks  (new — pass/fail gate summary)
3. Transpilation Fidelity  (new — QPU cost comparison)
4. Circuit Complexity
5. Integration Test Results  (new — cross-backend consistency)
6. Shot Simulation + TVD
7. Longitudinal Drift  (new — trend across recent builds)
"""

from __future__ import annotations

from typing import Optional

import requests

from .analyzer import CircuitStats
from .regression import RegressionResult
from .runner import IntegrationTestResult
from .transpiler import FidelityDecayResult

MARKER = "<!-- quantum-ci-report -->"


# ── Formatting helpers ──────────────────────────────────────────────────────────


def _icon(ok: Optional[bool]) -> str:
    if ok is None:
        return "—"
    return "✅ Pass" if ok else "❌ Fail"


def _val(stats: Optional[CircuitStats], attr: str) -> str:
    return "—" if stats is None else str(getattr(stats, attr))


def _delta(
    pr: Optional[CircuitStats], base: Optional[CircuitStats], attr: str
) -> str:
    if pr is None or base is None:
        return "—"
    diff = getattr(pr, attr) - getattr(base, attr)
    return f"+{diff}" if diff > 0 else str(diff)


def _delta_counts(
    pr: Optional[CircuitStats], base: Optional[CircuitStats], gate: str
) -> str:
    if pr is None or base is None:
        return "—"
    diff = pr.gate_counts.get(gate, 0) - base.gate_counts.get(gate, 0)
    return f"+{diff}" if diff > 0 else str(diff)


# ── Section builders ────────────────────────────────────────────────────────────


def _build_status_section(
    pr_ok: bool,
    pr_err: Optional[str],
    base_ok: Optional[bool],
    base_err: Optional[str],
) -> str:
    pr_err_col   = f"`{pr_err}`"   if pr_err   else ""
    base_err_col = f"`{base_err}`" if base_err else ""
    return (
        "### 🔨 Build Status\n\n"
        "| Branch | Status | Error |\n"
        "|--------|--------|-------|\n"
        f"| PR   | {_icon(pr_ok)}   | {pr_err_col}   |\n"
        f"| Base | {_icon(base_ok)} | {base_err_col} |\n"
    )


def _build_regression_section(regression: Optional[RegressionResult]) -> str:
    if regression is None or not regression.checks:
        return ""

    overall = "✅ All checks passed" if regression.passed else "❌ Merge blocked"
    rows = "\n".join(
        f"| {c.name} | {'✅' if c.passed else '❌'} | `{c.value}{c.unit}` | `{c.threshold}{c.unit}` | {c.message} |"
        for c in regression.checks
    )
    return (
        f"### 🔬 Regression Checks — {overall}\n\n"
        "| Check | Result | Measured | Threshold | Detail |\n"
        "|-------|--------|----------|-----------|--------|\n"
        f"{rows}\n"
    )


def _build_transpilation_section(decay: Optional[FidelityDecayResult]) -> str:
    if decay is None:
        return ""

    pr = decay.pr_stats
    bs = decay.base_stats

    icon = "✅" if not decay.exceeds_threshold else "❌"
    decay_str = f"`{decay.decay_pct:+.1f}%` {icon}"

    base_2q  = str(bs.transpiled_2q_count) if bs else "—"
    base_dep = str(bs.transpiled_depth)    if bs else "—"

    body = (
        "### ⚛️ Transpilation Fidelity Analysis\n\n"
        f"Backend: `{pr.backend_name}` · Optimisation level 1 · "
        f"Decay threshold: `{decay.threshold_pct:.0f}%`\n\n"
        "| Metric | Base | PR |\n"
        "|--------|------|----|\n"
        f"| Native 2Q gates (transpiled) | {base_2q} | `{pr.transpiled_2q_count}` |\n"
        f"| Circuit depth (transpiled) | {base_dep} | `{pr.transpiled_depth}` |\n"
        f"| 2Q-gate overhead vs logical | — | `{pr.overhead_pct:+.1f}%` |\n"
        f"\n**Fidelity decay vs base:** {decay_str}"
        f"  *(threshold: {decay.threshold_pct:.0f}%)*\n"
    )
    return body


def _build_complexity_section(
    pr: Optional[CircuitStats], base: Optional[CircuitStats]
) -> str:
    rows = [
        ("Depth",       "depth"),
        ("Qubits",      "num_qubits"),
        ("Clbits",      "num_clbits"),
        ("Total Gates", "size"),
    ]
    table = (
        "### 📊 Circuit Complexity\n\n"
        "| Metric | Base | PR | Δ |\n"
        "|--------|------|----|---|\n"
    )
    table += "\n".join(
        f"| {label} | {_val(base, attr)} | {_val(pr, attr)} | {_delta(pr, base, attr)} |"
        for label, attr in rows
    )

    all_gates: set[str] = set()
    if pr:
        all_gates |= set(pr.gate_counts)
    if base:
        all_gates |= set(base.gate_counts)

    if all_gates:
        gate_rows = "\n".join(
            f"| `{g}` | {base.gate_counts.get(g, 0) if base else '—'} "
            f"| {pr.gate_counts.get(g, 0) if pr else '—'} "
            f"| {_delta_counts(pr, base, g)} |"
            for g in sorted(all_gates)
        )
        breakdown = (
            "\n\n<details>\n"
            "<summary>Gate Breakdown (click to expand)</summary>\n\n"
            "| Gate | Base | PR | Δ |\n"
            "|------|------|----|---|\n"
            f"{gate_rows}\n\n"
            "</details>"
        )
    else:
        breakdown = ""

    return table + breakdown + "\n"


def _build_integration_section(integration: Optional[IntegrationTestResult]) -> str:
    if integration is None:
        return ""

    status = "✅ Consistent" if integration.consistent else "⚠️ Inconsistent"
    rows = "\n".join(
        f"| {pair} | `{tvd:.4f}` | {'✅' if tvd <= integration.consistency_threshold else '⚠️'} |"
        for pair, tvd in integration.cross_tvds.items()
    )

    return (
        f"### 🧪 Integration Test Results — {status}\n\n"
        f"Cross-backend TVD (threshold: `{integration.consistency_threshold}`)\n\n"
        "| Backend Pair | TVD | Status |\n"
        "|--------------|-----|--------|\n"
        f"{rows}\n"
    )


def _build_shots_section(
    pr_dist: Optional[dict[str, float]],
    base_dist: Optional[dict[str, float]],
    tvd: Optional[float],
    tvd_warn_threshold: float,
    shots: int,
) -> str:
    if pr_dist is None:
        return "### 🎲 Shot Simulation\n\nSkipped — PR circuit build failed.\n"

    all_states = set(pr_dist) | (set(base_dist) if base_dist else set())
    top = sorted(all_states, key=lambda s: pr_dist.get(s, 0.0), reverse=True)[:10]

    rows = "\n".join(
        f"| `{s}` | {f'{base_dist.get(s, 0.0)*100:.1f}%' if base_dist else '—'} "
        f"| {pr_dist.get(s, 0.0)*100:.1f}% |"
        for s in top
    )

    table = (
        f"### 🎲 Shot Simulation ({shots:,} shots)\n\n"
        "| State | Base | PR |\n"
        "|-------|------|----|\n"
        f"{rows}\n"
    )

    if tvd is not None:
        icon = "✅" if tvd <= tvd_warn_threshold else "⚠️"
        tvd_line = (
            f"\n**Total Variation Distance:** `{tvd:.4f}` {icon}  \n"
            f"*(warn threshold: {tvd_warn_threshold})*\n"
        )
    else:
        tvd_line = "\n**Total Variation Distance:** — *(no base circuit to compare)*\n"

    return table + tvd_line


def _build_drift_section(drift: Optional[dict]) -> str:
    if drift is None or drift.get("count", 0) < 2:
        return ""

    count   = drift["count"]
    mean    = drift["mean"]
    trend   = drift["trend"]
    latest  = drift["latest"]

    if trend is None:
        return ""

    direction = "↗ rising" if trend > 0 else "↘ falling"
    icon = "⚠️" if (trend > 0 and latest is not None and latest > 0.05) else "✅"

    return (
        f"### 📈 Longitudinal Fidelity Drift ({count} builds)\n\n"
        f"| Metric | Value |\n"
        f"|--------|-------|\n"
        f"| Latest TVD | `{latest:.4f}` |\n"
        f"| Mean TVD (window) | `{mean:.4f}` |\n"
        f"| OLS Trend | `{trend:+.6f}` per build ({direction}) {icon} |\n"
    )


# ── Public API ──────────────────────────────────────────────────────────────────


def build_comment(
    pr_build_ok: bool,
    pr_error: Optional[str],
    pr_stats: Optional[CircuitStats],
    pr_dist: Optional[dict[str, float]],
    base_build_ok: Optional[bool],
    base_error: Optional[str],
    base_stats: Optional[CircuitStats],
    base_dist: Optional[dict[str, float]],
    tvd: Optional[float],
    tvd_warn_threshold: float,
    shots: int,
    decay_result: Optional[FidelityDecayResult] = None,
    integration: Optional[IntegrationTestResult] = None,
    regression: Optional[RegressionResult] = None,
    drift: Optional[dict] = None,
) -> str:
    """Assemble the full PR comment markdown string."""
    parts = [
        MARKER,
        "## ⚛️ Quantum CI Report\n",
        _build_status_section(pr_build_ok, pr_error, base_build_ok, base_error),
        _build_regression_section(regression),
        _build_transpilation_section(decay_result),
        _build_complexity_section(pr_stats, base_stats),
        _build_integration_section(integration),
        _build_shots_section(pr_dist, base_dist, tvd, tvd_warn_threshold, shots),
        _build_drift_section(drift),
        "\n---\n*Posted by [Quantum CI/CD](https://github.com/hariv/Quantum-CI-CD)*",
    ]
    return "\n".join(p for p in parts if p)


# ── GitHub REST API ─────────────────────────────────────────────────────────────


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _find_existing_comment(repo: str, pr_number: int, token: str) -> Optional[int]:
    """Return the comment ID of the existing quantum-ci report, or None."""
    url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
    params: dict = {"per_page": 100, "page": 1}

    while True:
        resp = requests.get(url, headers=_headers(token), params=params, timeout=30)
        resp.raise_for_status()
        comments = resp.json()
        if not comments:
            break
        for c in comments:
            if MARKER in c.get("body", ""):
                return c["id"]
        if len(comments) < 100:
            break
        params["page"] += 1

    return None


def upsert_comment(repo: str, pr_number: int, token: str, body: str) -> None:
    """Post a new PR comment or PATCH the existing quantum-ci report."""
    existing_id = _find_existing_comment(repo, pr_number, token)

    if existing_id is not None:
        url  = f"https://api.github.com/repos/{repo}/issues/comments/{existing_id}"
        resp = requests.patch(url, headers=_headers(token), json={"body": body}, timeout=30)
    else:
        url  = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
        resp = requests.post(url, headers=_headers(token), json={"body": body}, timeout=30)

    resp.raise_for_status()
