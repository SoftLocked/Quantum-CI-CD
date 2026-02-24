"""
runner.py - Quantum circuit simulation, integration testing, and TVD.

Simulation pipeline
-------------------
1. run_shots()            — primary shot simulation on the statevector backend.
2. run_integration_tests() — validates consistency across multiple Aer backends
                              (statevector + density_matrix).  A large TVD
                              between backends signals implementation issues
                              that would surface as noise sensitivity on real QPUs.
3. compute_tvd()          — Total Variation Distance between any two distributions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from qiskit import QuantumCircuit, transpile
from qiskit_aer import AerSimulator


# ── Measurement injection ───────────────────────────────────────────────────────


def _ensure_measurements(circuit: QuantumCircuit) -> QuantumCircuit:
    """
    Return a copy of *circuit* that is guaranteed to have measurement gates.

    If the circuit has no classical bits (e.g. a pure-state circuit), a full
    measure_all() is appended to a *copy* so the original is never mutated.
    """
    if not circuit.clbits:
        measured = circuit.copy()
        measured.measure_all()
        return measured
    return circuit.copy()


# ── Primary simulation ──────────────────────────────────────────────────────────


def run_shots(
    circuit: QuantumCircuit,
    shots: int = 1024,
    seed: int = 42,
) -> dict[str, float]:
    """
    Simulate *circuit* on AerSimulator (statevector method) and return a
    normalised probability distribution over measurement outcomes.

    Parameters
    ----------
    circuit:
        The QuantumCircuit to simulate.  Measurements are injected if absent.
    shots:
        Number of simulation shots.
    seed:
        Random seed for reproducibility.

    Returns
    -------
    Dict mapping bitstring outcomes (e.g. ``"00"``, ``"11"``) to probabilities
    in [0, 1] that sum to 1.0 within floating-point tolerance.
    """
    backend = AerSimulator(seed_simulator=seed)
    measured = _ensure_measurements(circuit)
    compiled = transpile(measured, backend, optimization_level=0)
    job = backend.run(compiled, shots=shots)
    raw: dict[str, int] = job.result().get_counts(compiled)
    total = sum(raw.values())
    return {state: count / total for state, count in raw.items()}


# ── Integration testing ─────────────────────────────────────────────────────────


@dataclass
class IntegrationTestResult:
    """Results from a multi-backend consistency check."""

    distributions: dict[str, dict[str, float]] = field(default_factory=dict)
    # TVD between pairs of backends: key = "backend_a vs backend_b"
    cross_tvds: dict[str, float] = field(default_factory=dict)
    # True when all pairwise TVDs are below the consistency threshold.
    consistent: bool = True
    consistency_threshold: float = 0.05


_INTEGRATION_BACKENDS: list[dict] = [
    {"method": "statevector"},
    {"method": "density_matrix"},
]


def run_integration_tests(
    circuit: QuantumCircuit,
    shots: int = 1024,
    seed: int = 42,
    consistency_threshold: float = 0.05,
) -> IntegrationTestResult:
    """
    Execute *circuit* across multiple Aer simulator backends and compare the
    resulting distributions via TVD.

    A large cross-backend TVD (above *consistency_threshold*) indicates that
    the circuit's behaviour is simulator-implementation-dependent, which is a
    red flag for real QPU runs where backend differences are amplified by noise.

    Parameters
    ----------
    circuit:
        Circuit under test.
    shots:
        Shots per backend.
    seed:
        Shared RNG seed so randomness is not the source of divergence.
    consistency_threshold:
        Maximum tolerated TVD between any two backends.

    Returns
    -------
    IntegrationTestResult with per-backend distributions and all pairwise TVDs.
    """
    result = IntegrationTestResult(consistency_threshold=consistency_threshold)

    for cfg in _INTEGRATION_BACKENDS:
        backend = AerSimulator(**cfg, seed_simulator=seed)
        measured = _ensure_measurements(circuit)
        compiled = transpile(measured, backend, optimization_level=0)
        job = backend.run(compiled, shots=shots)
        raw: dict[str, int] = job.result().get_counts(compiled)
        total = sum(raw.values())
        method = cfg["method"]
        result.distributions[method] = {s: c / total for s, c in raw.items()}

    # Compute all pairwise TVDs.
    names = list(result.distributions.keys())
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            tvd = compute_tvd(result.distributions[a], result.distributions[b])
            key = f"{a} vs {b}"
            result.cross_tvds[key] = tvd
            if tvd > consistency_threshold:
                result.consistent = False

    return result


# ── TVD ────────────────────────────────────────────────────────────────────────


def compute_tvd(
    dist_a: dict[str, float],
    dist_b: dict[str, float],
) -> float:
    """
    Compute Total Variation Distance between two probability distributions.

    TVD = 0.5 × Σ_x |P(x) − Q(x)|

    Returns a value in [0.0, 1.0].  0.0 means the distributions are identical.
    """
    all_states = set(dist_a) | set(dist_b)
    tvd = 0.5 * sum(
        abs(dist_a.get(s, 0.0) - dist_b.get(s, 0.0)) for s in all_states
    )
    return round(tvd, 6)
