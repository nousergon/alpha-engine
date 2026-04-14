"""
Executor preflight: connectivity + safety checks run at the top of each
entrypoint before any real work starts.

Primitives live in ``alpha_engine_lib.preflight.BasePreflight``; this
module only composes them into a mode-specific sequence. See the
alpha-engine-lib README for the rationale.

Modes:

- ``"main"`` — ``executor/main.py``, the morning order-book planner.
  S3 reachable + AWS_REGION set. Does not place orders; the IB paper-
  account guard is not strictly required here, but main.py connects
  to IB for NAV/positions and the guard is available on the preflight
  instance via :meth:`check_ib_paper_account` for callers that want it.
- ``"daemon"`` — ``executor/daemon.py``, the sole order executor. Same
  S3 + env checks as ``main``. The daemon calls
  :meth:`check_ib_paper_account` after IBKRClient connects — a live-
  account connection hard-halts the daemon via SystemExit(1).
- ``"eod"`` — ``executor/eod_reconcile.py``. S3 + env only; no IB
  connection (reads trades.db + computes NAV vs SPY from S3 cache).
"""

from __future__ import annotations

from alpha_engine_lib.preflight import BasePreflight


class ExecutorPreflight(BasePreflight):
    """Preflight checks for the three executor entrypoints."""

    def __init__(self, bucket: str, mode: str):
        super().__init__(bucket)
        if mode not in ("main", "daemon", "eod"):
            raise ValueError(f"ExecutorPreflight: unknown mode {mode!r}")
        self.mode = mode

    def run(self) -> None:
        self.check_env_vars("AWS_REGION")
        self.check_s3_bucket()
