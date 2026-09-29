"""PBS command dialect discovery shared by status and recovery paths.

``qstat -x`` is history on PBS Pro/OpenPBS and XML output on Torque.  The
dialect therefore has to be observed before composing an argv.  The probe is
process-scoped: all Experiment consumers share this module and reuse the first
conclusive result.  Unknown results are retried because they may reflect a
transiently unavailable scheduler client.
"""
from __future__ import annotations

import re
import threading
from collections.abc import Callable
from typing import Any

PBS_PRO = "pbs_pro_openpbs"
TORQUE = "torque"
UNKNOWN = "unknown"

QueryRunner = Callable[..., dict[str, Any]]

_cache_lock = threading.Lock()
_flavor_cache: dict[str, Any] | None = None

_HISTORY_NOT_CONFIGURED = (
    "not configured to maintain job history",
    "job history is not configured",
    "job history is not enabled",
    "job history is disabled",
)


def _invoke(runner: QueryRunner, argv: list[str]) -> dict[str, Any]:
    try:
        result = runner(argv, timeout=10)
    except Exception as exc:
        return {
            "ok": False,
            "returncode": None,
            "stdout": "",
            "stderr": f"{type(exc).__name__}: {exc}",
            "_argv": list(argv),
        }
    if not isinstance(result, dict):
        return {
            "ok": False,
            "returncode": None,
            "stdout": "",
            "stderr": "query runner returned non-object",
            "_argv": list(argv),
        }
    return {**result, "_argv": list(argv)}


def _classify_version(result: dict[str, Any]) -> str:
    text = "\n".join((
        str(result.get("stdout") or ""),
        str(result.get("stderr") or ""),
    ))
    if (
        re.search(r"(?im)^\s*pbs_version\s*=", text)
        or re.search(r"(?i)\bopenpbs\b|\bpbs[ _-]?pro\b", text)
    ):
        return PBS_PRO
    if (
        re.search(r"(?i)\btorque\b", text)
        or re.search(r"(?im)^\s*version\s*:", text)
    ):
        return TORQUE
    return UNKNOWN


def pbs_flavor(runner: QueryRunner) -> dict[str, Any]:
    """Return the observed qstat dialect and its actual probe.

    Only a known dialect is process-cached.  A transiently unavailable or
    unrecognised ``qstat`` must be probed again so the process can recover when
    the scheduler client later becomes available.
    """
    global _flavor_cache
    cached = _flavor_cache
    if cached is None:
        with _cache_lock:
            cached = _flavor_cache
            if cached is None:
                probe = _invoke(runner, ["qstat", "--version"])
                observed = {
                    "flavor": _classify_version(probe),
                    "probe": probe,
                }
                cached = observed
                if observed["flavor"] in {PBS_PRO, TORQUE}:
                    _flavor_cache = observed
    return {
        "flavor": cached.get("flavor", UNKNOWN),
        "probe": dict(cached.get("probe") or {}),
    }


def pbs_history_not_configured(result: dict[str, Any]) -> bool:
    text = "\n".join((
        str(result.get("stdout") or ""),
        str(result.get("stderr") or ""),
    )).casefold()
    return any(marker in text for marker in _HISTORY_NOT_CONFIGURED)


def pbs_qstat_argv(*, job_id: str | None = None, history: bool = False) -> list[str]:
    argv = ["qstat"]
    if history:
        argv.append("-x")
    argv.append("-f")
    if job_id:
        argv.append(str(job_id))
    return argv


def query_pbs_job(runner: QueryRunner, job_id: str) -> dict[str, Any]:
    """Query one job with the strongest safe argv for the observed dialect.

    PBS history can be administratively disabled even when the binary is PBS
    Pro/OpenPBS.  In that one known case we retry the active-only query and
    retain why historical terminal state remains unavailable.
    """
    detected = pbs_flavor(runner)
    flavor = str(detected["flavor"])
    if flavor != PBS_PRO:
        active = _invoke(runner, pbs_qstat_argv(job_id=job_id))
        return {
            **active,
            "pbs_flavor": flavor,
            "pbs_flavor_probe": detected["probe"],
            "pbs_query_mode": "active_only",
            "pbs_history_available": None,
        }

    history = _invoke(
        runner, pbs_qstat_argv(job_id=job_id, history=True),
    )
    if history.get("ok") or not pbs_history_not_configured(history):
        return {
            **history,
            "pbs_flavor": flavor,
            "pbs_flavor_probe": detected["probe"],
            "pbs_query_mode": "history",
            "pbs_history_available": bool(history.get("ok")),
        }

    active = _invoke(runner, pbs_qstat_argv(job_id=job_id))
    return {
        **active,
        "pbs_flavor": flavor,
        "pbs_flavor_probe": detected["probe"],
        "pbs_query_mode": "active_only_after_history_unavailable",
        "pbs_history_available": False,
        "reason": "pbs_history_not_configured",
        "pbs_history_error": {
            "returncode": history.get("returncode"),
            "stderr": str(history.get("stderr") or ""),
        },
    }


def reset_pbs_flavor_cache_for_tests() -> None:
    """Test isolation only; production callers never invalidate the probe."""
    global _flavor_cache
    with _cache_lock:
        _flavor_cache = None
