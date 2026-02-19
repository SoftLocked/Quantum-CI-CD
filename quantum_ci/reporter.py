"""
reporter.py - Markdown report generation and GitHub PR comment upsert.

Every posted comment contains MARKER (an HTML comment invisible in rendered
markdown) so that subsequent runs can find and update the existing comment
rather than posting a new one.
"""

from __future__ import annotations

from typing import Optional

import requests

from .analyzer import CircuitStats

MARKER = "<!-- quantum-ci-report -->"


# ── Markdown formatting ────────────────────────────────────────────────────────

def _status_icon(ok: Optional[bool]) -> str:
    if ok is None:
        return "N/A"
    return "✅ Pass" if ok else "❌ Fail"


def _val(stats: Optional[CircuitStats], attr: str) -> str:
    return "N/A" if stats is None else str(getattr(stats, attr))


def _delta(pr: Optional[CircuitStats], base: Optional[CircuitStats], attr: str) -> str:
    if pr is None or base is None:
        return "N/A"
    diff = getattr(pr, attr) - getattr(base, attr)
    return f"+{diff}" if diff > 0 else str(diff)


def _build_status_section(
    pr_ok: bool,
    pr_err: Optional[str],
    base_ok: Optional[bool],
    base_err: Optional[str],
) -> str:
    pr_err_col = f"`{pr_err}`" if pr_err else ""
    base_err_col = f"`{base_err}`" if base_err else ""
    return (
        "### 🔨 Build Status\n\n"
        "| Branch | Status | Error |\n"
        "|--------|--------|-------|\n"
        f"| PR | {_status_icon(pr_ok)} | {pr_err_col} |\n"
        f"| Base | {_status_icon(base_ok)} | {base_err_col} |\n"
    )


def _build_complexity_section(
    pr: Optional[CircuitStats],
    base: Optional[CircuitStats],
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

    # Collapsible gate breakdown
    all_gates: set[str] = set()
    if pr:
        all_gates |= set(pr.gate_counts)
    if base:
        all_gates |= set(base.gate_counts)

    if all_gates:
        gate_rows = "\n".join(
            f"| `{g}` | {base.gate_counts.get(g, 0) if base else 'N/A'} "
            f"| {pr.gate_counts.get(g, 0) if pr else 'N/A'} "
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


def _delta_counts(
    pr: Optional[CircuitStats],
    base: Optional[CircuitStats],
    gate: str,
) -> str:
    if pr is None or base is None:
        return "N/A"
    diff = pr.gate_counts.get(gate, 0) - base.gate_counts.get(gate, 0)
    return f"+{diff}" if diff > 0 else str(diff)


def _build_shots_section(
    pr_dist: Optional[dict[str, float]],
    base_dist: Optional[dict[str, float]],
    tvd: Optional[float],
    tvd_threshold: float,
    shots: int,
) -> str:
    if pr_dist is None:
        return "### 🎲 Shot Simulation\n\nSkipped — PR circuit build failed.\n"

    all_states = set(pr_dist) | (set(base_dist) if base_dist else set())
    top = sorted(all_states, key=lambda s: pr_dist.get(s, 0.0), reverse=True)[:10]

    rows = "\n".join(
        f"| `{s}` | {f'{base_dist.get(s, 0.0)*100:.1f}%' if base_dist else 'N/A'} "
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
        icon = "✅" if tvd <= tvd_threshold else "⚠️"
        tvd_line = (
            f"\n**Total Variation Distance:** `{tvd:.4f}` {icon}  \n"
            f"*(warn threshold: {tvd_threshold})*\n"
        )
    else:
        tvd_line = "\n**Total Variation Distance:** N/A *(no base circuit to compare)*\n"

    return table + tvd_line


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
    tvd_threshold: float,
    shots: int,
) -> str:
    """Assemble the full PR comment markdown string."""
    parts = [
        MARKER,
        "## ⚛️ Quantum CI Report\n",
        _build_status_section(pr_build_ok, pr_error, base_build_ok, base_error),
        _build_complexity_section(pr_stats, base_stats),
        _build_shots_section(pr_dist, base_dist, tvd, tvd_threshold, shots),
        "\n---\n*Posted by [Quantum CI/CD](https://github.com/hariv/Quantum-CI-CD)*",
    ]
    return "\n".join(parts)


# ── GitHub API ─────────────────────────────────────────────────────────────────

def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _find_existing_comment(repo: str, pr_number: int, token: str) -> Optional[int]:
    """Return the ID of the existing quantum-ci comment, or None."""
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
    """Post a new PR comment or update the existing quantum-ci comment."""
    existing_id = _find_existing_comment(repo, pr_number, token)

    if existing_id is not None:
        url = f"https://api.github.com/repos/{repo}/issues/comments/{existing_id}"
        resp = requests.patch(url, headers=_headers(token), json={"body": body}, timeout=30)
    else:
        url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
        resp = requests.post(url, headers=_headers(token), json={"body": body}, timeout=30)

    resp.raise_for_status()
