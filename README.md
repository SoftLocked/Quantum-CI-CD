# Quantum CI/CD

A hermetic CI/CD engine for Qiskit quantum circuits. Every pull request is automatically analysed for behavioral regressions, QPU cost regressions, and implementation correctness — and results are posted as a rich PR comment. Failing checks block the merge.

---

## What it does

Four automated quality gates run on every PR:

| Gate | What it checks | Blocks merge? |
|------|---------------|---------------|
| **Build** | Circuit imports and instantiates without error | ✅ Yes |
| **Behavioral TVD** | Output distribution shift vs. base branch (Total Variation Distance) | ✅ Yes (if > threshold) |
| **Transpilation Fidelity** | Native 2-qubit gate count increase after QPU compilation | ✅ Yes (if > 15% by default) |
| **Integration tests** | Cross-backend consistency (statevector vs. density\_matrix) | ⚠️ Warn only |

Longitudinal metrics are emitted to OpenTelemetry and stored in a JSONL history file for drift analysis across builds.

---

## Example PR comment

```
## ⚛️ Quantum CI Report

### 🔨 Build Status
| Branch | Status | Error |
|--------|--------|-------|
| PR     | ✅ Pass |       |
| Base   | ✅ Pass |       |

### 🔬 Regression Checks — ✅ All checks passed
| Check                       | Result | Measured  | Threshold | Detail                                   |
|-----------------------------|--------|-----------|-----------|------------------------------------------|
| Behavioral Fidelity (TVD)   | ✅     | 0.0471    | 0.1       | TVD `0.0471` ≤ block threshold `0.1`    |
| Transpilation Fidelity Decay| ✅     | +3.2%     | 15%       | Native 2Q-gate count change: `+3.2%` ≤ `15%` threshold |

### ⚛️ Transpilation Fidelity Analysis
Backend: `fake_generic_5q` · Optimisation level 1 · Decay threshold: `15%`

| Metric                       | Base | PR  |
|------------------------------|------|-----|
| Native 2Q gates (transpiled) | 2    | `2` |
| Circuit depth (transpiled)   | 6    | `7` |
| 2Q-gate overhead vs logical  | —    | `+0.0%` |

**Fidelity decay vs base:** `+3.2%` ✅  *(threshold: 15%)*

### 📊 Circuit Complexity
| Metric      | Base | PR | Δ  |
|-------------|------|----|----|
| Depth       | 2    | 3  | +1 |
| Qubits      | 2    | 2  | 0  |
| Total Gates | 2    | 3  | +1 |

<details>
<summary>Gate Breakdown (click to expand)</summary>

| Gate | Base | PR | Δ  |
|------|------|----|----|
| `cx` | 1    | 1  | 0  |
| `h`  | 1    | 1  | 0  |
| `t`  | 0    | 1  | +1 |
</details>

### 🧪 Integration Test Results — ✅ Consistent
Cross-backend TVD (threshold: 0.05)

| Backend Pair                        | TVD    | Status |
|-------------------------------------|--------|--------|
| statevector vs density_matrix       | 0.0000 | ✅     |

### 🎲 Shot Simulation (1,024 shots)
| State | Base  | PR    |
|-------|-------|-------|
| `11`  | 50.4% | 47.2% |
| `00`  | 49.6% | 35.9% |

**Total Variation Distance:** `0.0471` ✅
*(warn threshold: 0.05)*

### 📈 Longitudinal Fidelity Drift (24 builds)
| Metric            | Value      |
|-------------------|------------|
| Latest TVD        | `0.0471`   |
| Mean TVD (window) | `0.0389`   |
| OLS Trend         | `+0.000012` per build (↗ rising) ✅ |
```

---

## Architecture

```
┌────────────────────────────────────────────────────────────────────────────┐
│                          GitHub Actions Runner                              │
│                                                                            │
│   PR branch checkout ──────┐                                               │
│                            ▼                                               │
│                    ┌───────────────┐   importlib sandbox   ┌────────────┐ │
│                    │  loader.py    │ ──── hermetic load ──▶ │ Circuit A  │ │
│                    │ (SHA-256 key) │                        └─────┬──────┘ │
│                    └───────────────┘                             │        │
│   Base branch checkout ────┐              ┌──────────────────────▼──────┐ │
│                            ▼              │      analyzer.py            │ │
│                    ┌───────────────┐      │  depth · qubits · T-count   │ │
│                    │  loader.py    │ ────▶│      gate_counts            │ │
│                    └───────────────┘      └──────────────────────┬──────┘ │
│                                                                  │        │
│                                           ┌──────────────────────▼──────┐ │
│                                           │      transpiler.py          │ │
│                                           │  GenericBackendV2 · OL=1   │ │
│                                           │  FidelityDecayResult        │ │
│                                           └──────────────────────┬──────┘ │
│                                                                  │        │
│                                           ┌──────────────────────▼──────┐ │
│                                           │       runner.py             │ │
│                                           │  AerSimulator (statevector) │ │
│                                           │  Integration: density_matrix│ │
│                                           │  TVD computation            │ │
│                                           └──────────────────────┬──────┘ │
│                                                                  │        │
│                                           ┌──────────────────────▼──────┐ │
│                                           │      regression.py          │ │
│                                           │  TVD check · Decay check    │ │
│                                           │  → exit 0 or exit 1         │ │
│                                           └──────────────────────┬──────┘ │
│                                                                  │        │
│                  ┌──────────────────────────────────────────────▼──────┐  │
│                  │                   telemetry.py                      │  │
│                  │    OTLP metrics → Collector → Prometheus → Grafana  │  │
│                  │    JSONL history → longitudinal drift analysis       │  │
│                  └──────────────────────────────────────────────┬──────┘  │
│                                                                 │         │
│                  ┌──────────────────────────────────────────────▼──────┐  │
│                  │                   reporter.py                       │  │
│                  │  Build markdown · GitHub REST API · upsert comment  │  │
│                  └─────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────────────┘
```

### Hermetic sandbox (`loader.py`)

The same circuit module may exist in both the PR and base checkouts. To prevent `sys.modules` collisions, each load uses a unique module name keyed to the SHA-256 hash of the absolute checkout path. After `exec_module()` completes, **all** newly registered keys are scrubbed from `sys.modules` — including sibling imports — so no state leaks between the two loads.

### Transpilation fidelity decay (`transpiler.py`)

Both circuits are compiled to a `GenericBackendV2` backend using `optimization_level=1` and a fixed transpiler seed. The percentage increase in native 2-qubit gate count (CX/ECR/CZ) from base to PR is the **fidelity decay**. Because 2Q gates are the dominant QPU cost driver (10–100× noisier than 1Q gates), a >15% increase signals a meaningful regression in QPU efficiency.

### Regression gate (`regression.py`)

All checks are aggregated into a `RegressionResult`. Any failing check causes `cli.py` to `sys.exit(1)`, which fails the required GitHub status check and blocks the merge. Checks are configurable via `quantum-ci.yaml`.

### Observability-as-Code (`telemetry.py`)

Every build appends a structured record to `.quantum-ci-history.jsonl`. When an OTLP endpoint is configured, metrics (TVD, decay%, circuit depth, 2Q gate count) are pushed via `opentelemetry-sdk` to any compatible collector. The local docker-compose stack wires the full pipeline: OTLP → Prometheus → Grafana.

---

## Quick start

### 1. Add `quantum-ci.yaml` to your circuit repository

```yaml
circuit:
  module: "circuits.my_circuit"   # circuits/my_circuit.py
  function: "build_circuit"       # returns a QuantumCircuit

runner:
  shots: 1024
  seed: 42

analysis:
  tvd_warn_threshold: 0.05        # warn at 5% distribution shift
  tvd_block_threshold: 0.1        # block merge at 10% shift
  transpilation_decay_threshold_pct: 15.0   # block if 2Q gates increase >15%

observability:
  otlp_endpoint: ""               # e.g. http://localhost:4318
  history_file: ".quantum-ci-history.jsonl"
```

### 2. Implement the circuit function

```python
# circuits/my_circuit.py
from qiskit import QuantumCircuit

def build_circuit() -> QuantumCircuit:
    qc = QuantumCircuit(2)
    qc.h(0)
    qc.cx(0, 1)
    return qc
```

Measurements are injected automatically for simulation — do not add them manually.

### 3. Add the GitHub Actions workflow

Copy [`.github/workflows/quantum-ci.yml`](.github/workflows/quantum-ci.yml) into your repository. No secrets configuration is required — it uses the built-in `GITHUB_TOKEN`.

```yaml
# .github/workflows/quantum-ci.yml  (in YOUR circuit repository)
name: Quantum CI

on:
  pull_request:

permissions:
  contents: read
  pull-requests: write

jobs:
  quantum-analysis:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: hariv/Quantum-CI-CD@main
        with:
          tvd-block-threshold: '0.1'
          transpilation-decay-threshold: '15.0'
```

---

## Configuration reference

### `circuit`

| Key | Required | Description |
|-----|----------|-------------|
| `module` | ✅ | Dotted Python path to the circuit module (e.g. `circuits.bell`) |
| `function` | ✅ | Callable that returns a `QuantumCircuit` |
| `kwargs` | | Static keyword arguments forwarded to the function |

### `runner`

| Key | Default | Description |
|-----|---------|-------------|
| `shots` | `1024` | Simulation shots per run |
| `seed` | `42` | RNG seed for reproducibility |

### `analysis`

| Key | Default | Description |
|-----|---------|-------------|
| `tvd_warn_threshold` | `0.1` | TVD above this triggers a ⚠️ warning (informational) |
| `tvd_block_threshold` | `0.1` | TVD above this blocks the merge (exit 1) |
| `transpilation_decay_threshold_pct` | `15.0` | % increase in native 2Q gates that blocks the merge |
| `max_depth_increase_pct` | *(disabled)* | Optional: block if circuit depth grows beyond this % |

### `observability`

| Key | Default | Description |
|-----|---------|-------------|
| `otlp_endpoint` | `""` | OTLP HTTP endpoint (e.g. `http://localhost:4318`) |
| `history_file` | `.quantum-ci-history.jsonl` | JSONL file for longitudinal drift tracking |

### GitHub Action inputs

| Input | Default | Description |
|-------|---------|-------------|
| `python-version` | `3.11` | Python version for simulation |
| `tvd-block-threshold` | *(from config)* | Override `tvd_block_threshold` |
| `transpilation-decay-threshold` | *(from config)* | Override `transpilation_decay_threshold_pct` |
| `otlp-endpoint` | `""` | OTLP HTTP endpoint |
| `history-file` | `""` | Override history file path |

---

## Observability stack (local)

Spin up the full OTLP → Prometheus → Grafana pipeline locally:

```bash
docker compose -f docker-compose.observability.yml up -d
```

Then run Quantum CI with the OTLP endpoint:

```bash
quantum-ci \
  --pr-path   /path/to/pr   \
  --base-path /path/to/base \
  --pr-number 1             \
  --repo      owner/repo    \
  --otlp-endpoint http://localhost:4318
```

Open **Grafana** at [http://localhost:3000](http://localhost:3000). The *Quantum CI — Fidelity & Resource Dashboard* is auto-provisioned and shows:

- **TVD trend** — longitudinal behavioral fidelity across builds
- **Transpilation decay %** — QPU cost regression over time
- **Native 2Q gate count** — hardware cost proxy
- **Circuit depth** — complexity over builds

### Persisting drift history in GitHub Actions

The workflow uploads `.quantum-ci-history.jsonl` as a workflow artifact with a 90-day retention window. For cross-run persistence, download the previous artifact at the start of each run:

```yaml
- name: Download drift history
  uses: actions/download-artifact@v4
  with:
    name: quantum-ci-history
  continue-on-error: true   # first run has no history yet
```

---

## Repository layout

```
Quantum-CI-CD/
├── .github/workflows/
│   └── quantum-ci.yml              # Workflow — copy into your circuit repo
├── action.yml                      # GitHub composite action definition
├── quantum_ci/
│   ├── __init__.py
│   ├── loader.py                   # Hermetic importlib sandbox (SHA-256 keyed)
│   ├── analyzer.py                 # CircuitStats + analyze_circuit()
│   ├── runner.py                   # Shot simulation + integration tests + TVD
│   ├── transpiler.py               # Transpilation fidelity decay analysis
│   ├── regression.py               # Aggregated regression gate (all checks)
│   ├── telemetry.py                # OpenTelemetry + JSONL drift history
│   ├── reporter.py                 # Markdown + GitHub REST API upsert
│   └── cli.py                      # Pipeline orchestrator (entry point)
├── circuits/
│   └── example_bell.py             # Bell state example circuit
├── grafana/
│   └── provisioning/
│       ├── datasources/
│       │   └── prometheus.yml      # Auto-wired Prometheus datasource
│       └── dashboards/
│           ├── provider.yml
│           └── quantum-ci.json     # Fidelity & resource dashboard
├── docker-compose.observability.yml  # OTEL Collector + Prometheus + Grafana
├── otel-collector-config.yml
├── prometheus.yml
├── quantum-ci.yaml                 # Example config (Bell circuit)
├── pyproject.toml
└── requirements.txt
```

---

## Local development

```bash
# Install with observability extras
pip install -e ".[observability]"

# Run the full pipeline locally (no live PR needed)
python -m quantum_ci.cli \
  --pr-path   /path/to/pr_checkout   \
  --base-path /path/to/base_checkout \
  --pr-number 1                      \
  --repo      owner/repo
# Set GITHUB_TOKEN to post the comment

# Quick sanity check using the example Bell circuit
python test_script.py
```

---

## How the exit code works

| Condition | Exit code | Effect |
|-----------|-----------|--------|
| PR circuit fails to build | `1` | Merge blocked |
| TVD > `tvd_block_threshold` | `1` | Merge blocked |
| Transpilation decay > threshold | `1` | Merge blocked |
| All checks pass | `0` | Merge allowed |
| No `quantum-ci.yaml` in PR | `0` | No-op, comment posted |

The GitHub required status check must be set to **"Quantum CI / quantum-analysis"** in your branch protection rules.
