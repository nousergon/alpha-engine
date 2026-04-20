"""
Resolve and load risk.yaml from the private config repo or legacy local
fallback. The example template is NEVER a valid fallback — it ships
placeholder bucket names (``"your-research-bucket-name"``) that would
silently point downstream consumers at nonexistent S3 buckets. Hit
2026-04-20 via the backtester spot path: missing risk.yaml → this
loader fell through to the example → executor built an ArcticDB URI
against the placeholder bucket → 404 surfaced as a cryptic
``KeyNotFoundException: Not found: [C:universe]`` ~100 lines deep in
the executor-sim call chain.

Search order (example template NOT a fallback — copyable only):
  1. ~/alpha-engine-config/executor/risk.yaml  (EC2 — config repo cloned at home)
  2. {repo_root}/../alpha-engine-config/executor/risk.yaml  (local dev — sibling directory)
  3. {repo_root}/config/risk.yaml  (legacy fallback)
"""

import os

import yaml

_REPO_ROOT = os.path.dirname(os.path.dirname(__file__))

_SEARCH_PATHS = [
    os.path.expanduser("~/alpha-engine-config/executor/risk.yaml"),
    os.path.join(_REPO_ROOT, "..", "alpha-engine-config", "executor", "risk.yaml"),
    os.path.join(_REPO_ROOT, "config", "risk.yaml"),
]


def get_config_path() -> str:
    """Return the first existing risk.yaml path.

    Raises ``FileNotFoundError`` with every candidate named if none
    exist. The example template at ``config/risk.yaml.example`` is NOT
    a candidate — copy it to ``config/risk.yaml`` and fill in real
    values for the intended environment.
    """
    for p in _SEARCH_PATHS:
        resolved = os.path.realpath(p)
        if os.path.isfile(resolved):
            return resolved
    raise FileNotFoundError(
        "executor risk.yaml not found in any of:\n  "
        + "\n  ".join(_SEARCH_PATHS)
        + "\nCopy config/risk.yaml.example → config/risk.yaml and fill in real "
          "values, or clone alpha-engine-config so the config-repo paths resolve. "
          "The .example template is intentionally NOT searched — it ships "
          "placeholder bucket names that silently break downstream ArcticDB + S3 reads."
    )


CONFIG_PATH = get_config_path()


def load_config() -> dict:
    """Load and return the risk.yaml config dict."""
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)
