"""
runner.py - Quantum circuit simulation and distribution comparison.
"""

from __future__ import annotations

from qiskit import QuantumCircuit, transpile
from qiskit_aer import AerSimulator


def _ensure_measurements(circuit: QuantumCircuit) -> QuantumCircuit:
    """
    Return a copy of the circuit guaranteed to have measurement instructions.

    If no classical bits exist, measure_all() is applied to a copy so that
    the original circuit passed to analyze_circuit() is never mutated.
    """
    if not circuit.clbits:
        measured = circuit.copy()
        measured.measure_all()
        return measured
    return circuit.copy()


def run_shots(
    circuit: QuantumCircuit,
    shots: int = 1024,
    seed: int = 42,
) -> dict[str, float]:
    """
    Simulate `circuit` on AerSimulator and return a normalized probability
    distribution over measurement outcomes.

    Parameters
    ----------
    circuit:
        The QuantumCircuit to simulate. Measurements are added automatically
        if absent.
    shots:
        Number of simulation shots.
    seed:
        Random seed for reproducibility.

    Returns
    -------
    Dict mapping bitstring outcomes (e.g. "00", "11") to probabilities in
    [0, 1] that sum to 1.0 within floating-point tolerance.
    """
    backend = AerSimulator(seed_simulator=seed)
    measured = _ensure_measurements(circuit)
    compiled = transpile(measured, backend, optimization_level=0)

    job = backend.run(compiled, shots=shots)
    raw_counts: dict[str, int] = job.result().get_counts(compiled)

    total = sum(raw_counts.values())
    return {state: count / total for state, count in raw_counts.items()}


def compute_tvd(
    dist_a: dict[str, float],
    dist_b: dict[str, float],
) -> float:
    """
    Compute Total Variation Distance between two probability distributions.

    TVD = 0.5 * Σ_x |P(x) - Q(x)|

    Returns a value in [0.0, 1.0]. 0.0 means identical distributions.
    """
    all_states = set(dist_a) | set(dist_b)
    tvd = 0.5 * sum(
        abs(dist_a.get(s, 0.0) - dist_b.get(s, 0.0))
        for s in all_states
    )
    return round(tvd, 6)
