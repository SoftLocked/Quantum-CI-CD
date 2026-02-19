# Quantum CI/CD

Automated PR analysis for Qiskit quantum circuits. Every pull request gets a comment with three things:

1. **Build check** — verifies the circuit imports and instantiates without error
2. **Complexity comparison** — depth, qubit count, gate counts (with deltas vs. the base branch)
3. **Shot comparison** — runs both circuits on the Aer simulator and reports the output distribution shift via Total Variation Distance (TVD)

Results are always posted as a comment; the check never blocks merging (warn-only).

---

## Example PR comment

```
## ⚛️ Quantum CI Report

### 🔨 Build Status
| Branch | Status | Error |
|--------|--------|-------|
| PR     | ✅ Pass |       |
| Base   | ✅ Pass |       |

### 📊 Circuit Complexity
| Metric      | Base | PR | Δ  |
|-------------|------|----|----|
| Depth       | 2    | 3  | +1 |
| Qubits      | 2    | 2  | 0  |
| Clbits      | 0    | 0  | 0  |
| Total Gates | 2    | 3  | +1 |

<details>
<summary>Gate Breakdown (click to expand)</summary>
| Gate | Base | PR | Δ  |
|------|------|----|----|
| `cx` | 1    | 1  | 0  |
| `h`  | 1    | 1  | 0  |
| `t`  | 0    | 1  | +1 |
</details>

### 🎲 Shot Simulation (1,024 shots)
| State | Base  | PR    |
|-------|-------|-------|
| `11`  | 50.4% | 47.2% |
| `00`  | 49.6% | 35.9% |
| `01`  | 0.0%  | 10.1% |
| `10`  | 0.0%  | 6.8%  |

**Total Variation Distance:** `0.1632` ⚠️
*(warn threshold: 0.1)*
```

---

## Quick start

### 1. Add the config file

Create `quantum-ci.yaml` at the root of your circuit repository:

```yaml
circuit:
  module: "circuits.my_circuit"   # path to your circuit file (dot-notation)
  function: "build_circuit"       # function that returns a QuantumCircuit

runner:
  shots: 1024
  seed: 42

analysis:
  tvd_warn_threshold: 0.1   # warn if output distribution shifts more than this
```

### 2. Implement the circuit function

Your circuit file must export a zero-argument function that returns a `QuantumCircuit`:

```python
# circuits/my_circuit.py
from qiskit import QuantumCircuit

def build_circuit() -> QuantumCircuit:
    qc = QuantumCircuit(2)
    qc.h(0)
    qc.cx(0, 1)
    return qc
```

You do **not** need to add measurements — Quantum CI adds them automatically for simulation.

### 3. Add the GitHub Actions workflow

Copy [`.github/workflows/quantum-ci.yml`](.github/workflows/quantum-ci.yml) into your repository. No secrets configuration required — it uses the built-in `GITHUB_TOKEN`.

Make sure the workflow has `pull-requests: write` permissions (already set in the provided file).

---

## Config reference

| Key | Default | Description |
|-----|---------|-------------|
| `circuit.module` | *(required)* | Dotted Python path to the circuit module (e.g. `circuits.bell`) |
| `circuit.function` | *(required)* | Name of the callable returning a `QuantumCircuit` |
| `circuit.kwargs` | `{}` | Static keyword arguments forwarded to the function |
| `runner.shots` | `1024` | Number of simulation shots |
| `runner.seed` | `42` | RNG seed for reproducibility |
| `analysis.tvd_warn_threshold` | `0.1` | TVD above this value triggers a ⚠️ warning |

---

## How it works

The GitHub Actions workflow checks out **both** the PR branch and the base branch into separate directories, installs the `quantum_ci` package from the PR branch, and runs `python -m quantum_ci.cli`. The CLI:

1. Loads `quantum-ci.yaml` from the PR branch
2. Dynamically imports the circuit module from each branch using `importlib` (isolated, no `sys.modules` pollution)
3. Runs `analyze_circuit()` on each to collect structural metrics
4. Simulates each on `AerSimulator` and normalizes the count distributions
5. Computes TVD between the two distributions
6. Builds a markdown report and upserts it as a PR comment (finds existing comment by a hidden HTML marker to avoid spam on re-runs)

---

## Local development

```bash
pip install -e .

# Analyze two local checkouts (useful for testing without a live PR)
python -m quantum_ci.cli \
  --pr-path   /path/to/pr_checkout   \
  --base-path /path/to/base_checkout \
  --pr-number 1                      \
  --repo      owner/repo
# GITHUB_TOKEN must be set in the environment to post the comment
```

---

## Repository layout

```
Quantum-CI-CD/
├── .github/workflows/quantum-ci.yml   # GitHub Actions workflow (copy into your repo)
├── quantum_ci/
│   ├── __init__.py
│   ├── loader.py      # importlib-based circuit loader (isolation between branches)
│   ├── analyzer.py    # CircuitStats dataclass + analyze_circuit()
│   ├── runner.py      # run_shots() + compute_tvd()
│   ├── reporter.py    # markdown formatting + GitHub PR comment upsert
│   └── cli.py         # orchestration entry point
├── circuits/
│   └── example_bell.py   # example Bell state circuit
├── quantum-ci.yaml        # example config (points to example_bell)
├── pyproject.toml
└── requirements.txt
```
