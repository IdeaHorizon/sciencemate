"""MPI runtime failure classification and safe Open MPI retry recipes.

This module never runs or resubmits a job. It only recognizes a narrow,
well-known UCX/HCOLL failure family and prepares a changed foreground command
for the caller to review and submit through the normal high-risk gate.
"""
from __future__ import annotations

import re
from typing import Any


_UCX_RE = re.compile(
    r"\b(?:ucx|ucp_|uct_|ucs_|failed to receive ucx worker address|"
    r"destination is unreachable)\b", re.IGNORECASE)
_HCOLL_RE = re.compile(r"\b(?:hcoll|coll_hcoll)\b", re.IGNORECASE)
_OPENMPI_RE = re.compile(r"\b(?:open mpi|openrte|ompi_|orted|prterun)\b", re.IGNORECASE)
_LAUNCHER_RE = re.compile(r"(?<![\w.-])(mpirun|mpiexec)(?![\w.-])")
_MCA_PML_RE = re.compile(r"(?:--mca\s+pml\s+\S+|OMPI_MCA_pml=\S+)")
_MCA_BTL_RE = re.compile(r"(?:--mca\s+btl\s+\S+|OMPI_MCA_btl=\S+)")
_MCA_HCOLL_RE = re.compile(r"(?:--mca\s+coll_hcoll_enable\s+\S+|OMPI_MCA_coll_hcoll_enable=\S+)")

_TCP_NO_HCOLL_ARGS = "--mca pml ob1 --mca btl tcp,self --mca coll_hcoll_enable 0"


def _evidence(text: str, limit: int = 3) -> list[str]:
    matches = []
    for line in text.splitlines():
        if _UCX_RE.search(line) or _HCOLL_RE.search(line):
            matches.append(line.strip()[:240])
        if len(matches) >= limit:
            break
    return matches


def mpi_runtime_remediation(command: str, log_text: str) -> dict[str, Any] | None:
    """Return one safe retry recipe for a confirmed Open MPI UCX/HCOLL failure.

    The fallback deliberately applies only when both the log and command make
    the diagnosis specific enough. It does not overwrite caller-provided MCA
    settings and it never produces a second recipe after the fallback is in
    the command already.
    """
    text = str(log_text or "")
    command = str(command or "")
    has_ucx = bool(_UCX_RE.search(text))
    has_hcoll = bool(_HCOLL_RE.search(text))
    if not (has_ucx or has_hcoll):
        return None
    launcher = _LAUNCHER_RE.search(command)
    openmpi_evidence = bool(_OPENMPI_RE.search(text)) or has_hcoll
    if not launcher or not openmpi_evidence:
        return {
            "kind": "openmpi_ucx_hcoll_runtime_failure",
            "confidence": "high" if openmpi_evidence else "medium",
            "evidence": _evidence(text),
            "status": "manual_retry_required",
            "reason": (
                "日志命中 UCX/HCOLL，但原作业命令没有可安全改写的 mpirun/mpiexec "
                "launcher；请先确认实际 MPI launcher 与模块环境。"
            ),
        }
    if (_MCA_PML_RE.search(command) or _MCA_BTL_RE.search(command)
            or _MCA_HCOLL_RE.search(command)):
        return {
            "kind": "openmpi_ucx_hcoll_runtime_failure",
            "confidence": "high",
            "evidence": _evidence(text),
            "status": "already_configured_or_conflicting",
            "reason": (
                "作业命令已显式设置 Open MPI MCA 参数；框架不会覆盖它们。"
                "请比较现有参数与节点/集群管理员建议。"
            ),
        }
    retry_command = (
        command[:launcher.end()] + " " + _TCP_NO_HCOLL_ARGS + command[launcher.end():])
    return {
        "kind": "openmpi_ucx_hcoll_runtime_failure",
        "confidence": "high",
        "evidence": _evidence(text),
        "status": "retry_recipe_ready",
        "retry_limit": 1,
        "retry_command": retry_command,
        "added_launcher_args": _TCP_NO_HCOLL_ARGS,
        "reason": (
            "UCX/HCOLL 初始化或通信失败：本次仅切换到 Open MPI 的 ob1 + tcp,self "
            "并禁用 HCOLL，避免 UCX/HCOLL 通信栈；不改变科学输入、MPI ranks 或线程布局。"
        ),
        "submission_policy": (
            "这是新的真实作业提交，必须使用新的 output_paths，并经 submit_job 的人工确认；"
            "框架不会自动重投。"
        ),
    }
