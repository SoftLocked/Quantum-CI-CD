"""
telemetry.py - OpenTelemetry observability for longitudinal fidelity drift.

Every build appends a structured record to a JSONL history file, enabling
drift analysis across 50+ automated builds without an external datastore.
When an OTLP endpoint is configured, metrics and traces are also emitted
to the collector (e.g., Grafana Alloy → Prometheus → Grafana dashboard).

The opentelemetry-* packages are *optional*.  If they are not installed the
recorder silently falls back to JSONL-only mode with no loss of history data.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

# ── Optional OpenTelemetry imports ─────────────────────────────────────────────
try:
    from opentelemetry import metrics, trace
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import (
        ConsoleMetricExporter,
        PeriodicExportingMetricReader,
    )
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

    _OTEL_SDK = True
except ImportError:
    _OTEL_SDK = False

try:
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
        OTLPMetricExporter,
    )
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    _OTLP = True
except ImportError:
    _OTLP = False

# ── Constants ──────────────────────────────────────────────────────────────────

HISTORY_FILE_DEFAULT = Path(".quantum-ci-history.jsonl")
_SERVICE_NAME = "quantum-ci"
_SERVICE_VERSION = "0.2.0"
_METRIC_EXPORT_INTERVAL_MS = 5_000


# ── Internal provider setup ────────────────────────────────────────────────────


def _resource() -> Any:
    return Resource.create(
        {"service.name": _SERVICE_NAME, "service.version": _SERVICE_VERSION}
    )


def _init_tracer(otlp_endpoint: Optional[str]) -> Any:
    if not _OTEL_SDK:
        return None
    provider = TracerProvider(resource=_resource())
    if otlp_endpoint and _OTLP:
        exporter: Any = OTLPSpanExporter(endpoint=f"{otlp_endpoint}/v1/traces")
    else:
        exporter = ConsoleSpanExporter()
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return trace.get_tracer(_SERVICE_NAME)


def _init_meter(otlp_endpoint: Optional[str]) -> Any:
    if not _OTEL_SDK:
        return None
    if otlp_endpoint and _OTLP:
        exporter: Any = OTLPMetricExporter(endpoint=f"{otlp_endpoint}/v1/metrics")
    else:
        exporter = ConsoleMetricExporter()
    reader = PeriodicExportingMetricReader(
        exporter, export_interval_millis=_METRIC_EXPORT_INTERVAL_MS
    )
    provider = MeterProvider(resource=_resource(), metric_readers=[reader])
    metrics.set_meter_provider(provider)
    return metrics.get_meter(_SERVICE_NAME)


# ── No-op context manager ──────────────────────────────────────────────────────


class _NoOpSpan:
    """Returned by TelemetryRecorder.span() when the OTEL SDK is unavailable."""

    def __enter__(self) -> "_NoOpSpan":
        return self

    def __exit__(self, *_: Any) -> None:
        pass


# ── Public API ─────────────────────────────────────────────────────────────────


class TelemetryRecorder:
    """
    Records build metrics to OTLP and to a local JSONL history file.

    Usage::

        rec = TelemetryRecorder(otlp_endpoint="http://localhost:4318")
        with rec.span("quantum-ci.analysis"):
            rec.record(tvd=0.05, transpilation_decay_pct=3.2, circuit_depth_pr=6)
        rec.flush()   # appends to .quantum-ci-history.jsonl

    All ``record()`` calls accumulate into a single dict that is written
    atomically by ``flush()``.  Numeric values are also pushed to OTLP gauges
    when the SDK is available.
    """

    def __init__(
        self,
        otlp_endpoint: Optional[str] = None,
        history_file: Optional[Path] = None,
    ) -> None:
        self._tracer = _init_tracer(otlp_endpoint)
        self._meter = _init_meter(otlp_endpoint)
        self.history_file = history_file or HISTORY_FILE_DEFAULT

        # Seed the build record with CI environment metadata.
        self._record: dict[str, Any] = {
            "timestamp": time.time(),
            "run_number": os.environ.get("GITHUB_RUN_NUMBER", "local"),
            "pr_number": os.environ.get("GITHUB_PR_NUMBER", "0"),
            "sha": os.environ.get("GITHUB_SHA", "unknown"),
            "repo": os.environ.get("GITHUB_REPOSITORY", "unknown"),
        }

        # Create named OTLP gauges (no-op if SDK unavailable).
        if self._meter:
            self._g_tvd = self._meter.create_gauge(
                "quantum_ci.tvd",
                description="Total Variation Distance between PR and base output distributions",
            )
            self._g_decay = self._meter.create_gauge(
                "quantum_ci.transpilation_decay_pct",
                description="Transpilation fidelity decay percentage vs base branch",
            )
            self._g_depth = self._meter.create_gauge(
                "quantum_ci.circuit_depth",
                description="PR circuit depth after gate decomposition",
            )
            self._g_2q = self._meter.create_gauge(
                "quantum_ci.transpiled_2q_gates",
                description="Number of native 2-qubit gates after transpilation (QPU cost proxy)",
            )
        else:
            self._g_tvd = self._g_decay = self._g_depth = self._g_2q = None

    # ── Recording ──────────────────────────────────────────────────────────────

    def record(self, **kwargs: Any) -> None:
        """
        Record arbitrary key-value metrics.

        Numeric values are also pushed to the matching OTLP gauge based on a
        keyword match (``tvd``, ``decay``, ``depth``, ``2q``).
        """
        self._record.update(kwargs)
        if not self._meter:
            return
        for key, val in kwargs.items():
            if not isinstance(val, (int, float)):
                continue
            fval = float(val)
            if "tvd" in key and self._g_tvd:
                self._g_tvd.set(fval)
            elif "decay" in key and self._g_decay:
                self._g_decay.set(fval)
            elif "depth" in key and self._g_depth:
                self._g_depth.set(fval)
            elif "2q" in key and self._g_2q:
                self._g_2q.set(fval)

    def span(self, name: str) -> Any:
        """
        Return an OpenTelemetry span context manager, or a no-op if unavailable.

        Usage::

            with rec.span("quantum-ci.transpilation"):
                result = compute_fidelity_decay(...)
        """
        if self._tracer:
            return self._tracer.start_as_current_span(name)
        return _NoOpSpan()

    # ── Persistence ────────────────────────────────────────────────────────────

    def flush(self) -> None:
        """
        Append the completed build record to the JSONL history file.

        The file is the durable store for longitudinal drift analysis.  In a
        GitHub Actions environment, persist this file as a build artifact or
        commit it to a dedicated ``quantum-ci-history`` branch so that trend
        data survives across workflow runs.
        """
        self._record["completed_at"] = time.time()
        with open(self.history_file, "a") as fh:
            fh.write(json.dumps(self._record) + "\n")

    # ── History & drift analysis ───────────────────────────────────────────────

    def load_history(self, window: int = 50) -> list[dict]:
        """Return up to the last *window* build records from the history file."""
        if not self.history_file.exists():
            return []
        records: list[dict] = []
        with open(self.history_file) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        return records[-window:]

    def compute_drift(self, metric: str = "tvd", window: int = 50) -> dict:
        """
        Compute longitudinal drift statistics for *metric* over the last
        *window* builds using ordinary least-squares trend estimation.

        Returns
        -------
        dict with keys:

        * ``count``  — number of builds included in the analysis
        * ``mean``   — arithmetic mean of the metric across those builds
        * ``trend``  — OLS slope; positive = metric is rising over time
        * ``latest`` — value recorded in the most recent build

        A rising ``tvd`` trend, for example, indicates that behavioral
        regressions are accumulating and warrants investigation.
        """
        history = self.load_history(window)
        values = [
            r[metric]
            for r in history
            if metric in r and isinstance(r[metric], (int, float))
        ]
        n = len(values)
        if n < 2:
            return {
                "count": n,
                "mean": None,
                "trend": None,
                "latest": round(values[-1], 6) if values else None,
            }

        mean = sum(values) / n
        # OLS slope via the dot-product formula (normalised index ∈ [−n/2, n/2]).
        xs = [i - n / 2 for i in range(n)]
        ss_xy = sum(x * y for x, y in zip(xs, values))
        ss_xx = sum(x * x for x in xs)
        trend = ss_xy / ss_xx if ss_xx else 0.0

        return {
            "count": n,
            "mean": round(mean, 6),
            "trend": round(trend, 8),
            "latest": round(values[-1], 6),
        }
