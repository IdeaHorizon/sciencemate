"""环境复现快照采集器（需求 ⑦.3）。

科研可复现的硬要求：一个实验跑成功了，得能"换台机器 / 过几个月照着重来一遍"。
但「用了哪个 conda env、哪些 LD_LIBRARY_PATH、哪个 git commit、什么编译 flags」
平时只散落在 transcript 的 run_bash 命令里，没结构化 → 无法复现。

本模块把这些一次性抓成结构化快照。运行 manifest 在首轮和 **on_end hook** 自动写入，
环境快照与可复现包在结束时按运行角色生成（不靠 agent 自觉——吸取 2026-06-10
审计教训：靠 LLM 记得做 = 失效）。

用法（hook 内）：
    from tools.repro_snapshot import collect_snapshot
    snap = collect_snapshot(repo_paths=[...])   # 返回结构化 dict
"""
from __future__ import annotations

import json
import hashlib
import os
import platform
import re
import shlex
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .run_contract import sha256_file
    from .path_roles import experiment_output_dir
except ImportError:
    from tools.run_contract import sha256_file
    from tools.path_roles import experiment_output_dir


# 探测的工具集（HPC 常见编译/构建/MPI/运行时链）
_PROBE_TOOLS: dict[str, list[str]] = {
    "compiler": ["gcc", "g++", "gfortran", "nvfortran", "nvc", "nvc++", "nvcc",
                 "icc", "icpc", "ifort", "ifx", "icx", "icpx", "clang", "clang++"],
    "build": ["cmake", "make", "ninja", "pkg-config"],
    "mpi": ["mpirun", "mpiexec", "mpicc", "mpicxx", "mpif90", "mpifort",
            "mpiicc", "mpiicx", "mpiicpc", "mpiicpx", "mpiifort", "mpiifx"],
    "gpu": ["nvidia-smi"],
    "io_config": ["nc-config", "nf-config", "h5cc", "h5pcc", "pnetcdf-config"],
    "math_config": ["fftw-wisdom"],
    "runtime": ["python3", "python", "pip", "conda", "mamba"],
}

# 与编译/运行可复现强相关的环境变量
_PROBE_ENV_VARS = [
    "PATH", "LD_LIBRARY_PATH", "CONDA_PREFIX", "CONDA_DEFAULT_ENV", "VIRTUAL_ENV",
    "CUDA_HOME", "CUDA_PATH", "HPC_SDK", "NVHPC_ROOT",
    "CC", "CXX", "FC", "F90",
    "CFLAGS", "CXXFLAGS", "FFLAGS", "FCFLAGS", "LDFLAGS", "CPPFLAGS",
    "CPATH", "LIBRARY_PATH", "PKG_CONFIG_PATH", "CMAKE_PREFIX_PATH",
    "OMP_NUM_THREADS", "HPC_SDK", "NETCDF", "NETCDF_PATH", "PNETCDF",
    "HDF5_DIR", "HDF5_ROOT", "FFTW_ROOT", "MKLROOT", "PETSC_DIR", "PETSC_ARCH",
]

_REPRO_MAX_COPY_BYTES = 10 * 1024 * 1024
_REPRO_MAX_CANDIDATES = 200
_PATH_RE = re.compile(r"(?:~|/)[^\s\"'`;,)]+")
_CONFIG_NAMES = {
    "input.nml", "namelist.input",
    "streams.ocean", "env_mach_specific.xml", "env_build.xml",
}
_CONFIG_SUFFIXES = {
    ".nml", ".nl", ".yaml", ".yml", ".json", ".toml", ".ini", ".cfg", ".conf",
    ".xml", ".rc", ".in", ".input", ".txt", ".patch", ".diff",
}
_OUTPUT_SUFFIXES = {
    ".stats", ".nc", ".out", ".log", ".err", ".csv", ".json", ".txt",
}
_OUTPUT_NAMES = {"exitcode"}
_BUILD_RUN_RE = re.compile(
    r"\b(make|gmake|ninja|cmake\s+--build|mpirun|mpiexec|srun|configure|"
    r"autogen|git\s+clone|git\s+submodule)\b", re.I)


def _run(cmd: str, timeout: int = 10) -> str:
    """跑一条只读探测命令，返回首要输出（失败返回空串，不抛）。"""
    env_path = os.environ.get("EXPERIMENT_ACTIVE_BUILD_ENV")
    if env_path:
        try:
            from tools.env_provision import wrap_command
        except Exception:
            try:
                from .env_provision import wrap_command
            except Exception:
                wrap_command = None
        if wrap_command is not None:
            cmd = wrap_command(cmd, env_path)
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True,
                           text=True, timeout=timeout)
        out = (r.stdout or "").strip() or (r.stderr or "").strip()
        return out
    except Exception:
        return ""


def _q(path: str | Path) -> str:
    import shlex
    return shlex.quote(str(path))


def _safe_slug(path: str | Path) -> str:
    p = str(Path(path).expanduser())
    return p.strip("/").replace("/", "__").replace(" ", "_")[:180] or "root"


def _clean_path_token(token: str) -> str:
    token = token.strip().strip("\"'`")
    token = token.rstrip("),，。:;")
    return os.path.abspath(os.path.expandvars(os.path.expanduser(token)))


def _is_harness_path(path: str | Path) -> bool:
    p = os.path.abspath(os.path.expanduser(str(path)))
    return p.endswith("/harness-framework") or "/node4-experiment/harness-framework" in p


def _git_root(path: str | Path) -> str | None:
    p = Path(os.path.expanduser(str(path)))
    if p.is_file():
        p = p.parent
    if not p.exists():
        return None
    root = _run(f"git -C {_q(p)} rev-parse --show-toplevel 2>/dev/null")
    if not root:
        return None
    root = _clean_path_token(root.splitlines()[0])
    return None if _is_harness_path(root) else root


def _existing_parent(path: str | Path) -> Path | None:
    p = Path(os.path.expanduser(str(path)))
    if p.exists():
        return p.parent if p.is_file() else p
    for parent in p.parents:
        if parent.exists():
            return parent
    return None


def _git_root_near(path: str | Path) -> str | None:
    if _is_harness_path(path):
        return None
    p = _existing_parent(path)
    if p is None:
        return None
    if _is_harness_path(p):
        return None
    return _git_root(p)


def _artifact_content_text(state: Any) -> str:
    chunks: list[str] = []
    try:
        for entry in state.list_artifacts(own_only=True):
            rec = state.read_artifact(entry["id"])
            if not isinstance(rec, dict):
                continue
            meta = rec.get("metadata") or {}
            for key in ("source_path", "target_path", "run_dir", "output_dir"):
                if meta.get(key):
                    chunks.append(str(meta[key]))
            content = rec.get("content")
            if isinstance(content, str):
                chunks.append(content)
    except Exception:
        pass
    return "\n".join(chunks)


def _path_tokens(text: str) -> list[str]:
    out: list[str] = []
    for m in _PATH_RE.finditer(text or ""):
        tok = _clean_path_token(m.group(0))
        if tok and tok not in out:
            out.append(tok)
    return out[:_REPRO_MAX_CANDIDATES]


def _transcript_commands_from_text(text: str) -> list[dict[str, Any]]:
    commands: list[dict[str, Any]] = []
    for line in (text or "").splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("event") != "tool_call":
            continue
        name = rec.get("name")
        # v0.11 改名迁移：新 run 记 safe_*，旧 transcript 是不带前缀的旧名
        if name not in ("run_bash", "safe_run_bash",
                        "execute_python", "safe_execute_python"):
            continue
        args = rec.get("args") or {}
        entry = {
            "turn": rec.get("turn"),
            "name": name,
            "at": rec.get("at"),
            "cwd": args.get("cwd"),
            "timeout": args.get("timeout"),
        }
        if name in ("run_bash", "safe_run_bash"):
            entry["cmd"] = args.get("cmd", "")
        else:
            code = args.get("code", "")
            entry["code_sha256"] = hashlib.sha256(code.encode("utf-8")).hexdigest()
            entry["code_preview"] = code[:1000]
        commands.append(entry)
    return commands


def _command_path_candidates(cmd: str, cwd: str | None = None) -> list[str]:
    candidates: list[str] = []
    cd_re = re.compile(r"(?:^|[\n;&|])\s*cd\s+([^;&|\n]+)")
    candidates.extend(m.group(1) for m in cd_re.finditer(cmd or ""))
    flag_res = [
        re.compile(r"\b(?:make|gmake|ninja)\s+(?:[^\n;&|]*\s)?-C\s+([^;&|\n]+)"),
        re.compile(r"\bcmake\b[^\n;&|]*\s-S\s+([^;&|\n]+)"),
        re.compile(r"\bcmake\s+--build\s+([^;&|\n]+)"),
        re.compile(r"\bgit\s+-C\s+([^;&|\n]+)"),
    ]
    for rx in flag_res:
        candidates.extend(m.group(1) for m in rx.finditer(cmd or ""))
    if cwd:
        candidates.append(cwd)
    out: list[str] = []
    for c in candidates:
        p = _clean_path_token(str(c))
        if p and not _is_harness_path(p) and p not in out:
            out.append(p)
    return out


def _declared_route_paths(state: Any) -> tuple[list[str], list[str], list[str]]:
    """Return (source/target candidates, declared inputs, declared outputs)."""
    candidates: list[str] = []
    inputs: list[str] = []
    outputs: list[str] = []

    def add_path(dst: list[str], value: str) -> None:
        for tok in _path_tokens(str(value)):
            if tok not in dst:
                dst.append(tok)

    try:
        for entry in state.list_artifacts("declared_route", own_only=True):
            rec = state.read_artifact(entry["id"])
            if not isinstance(rec, dict):
                continue
            meta = rec.get("metadata") or {}
            for key in ("source_path", "target_path", "build_dir", "run_dir", "output_dir"):
                if meta.get(key):
                    add_path(candidates, str(meta[key]))
            for key in ("input_paths", "inputs"):
                val = meta.get(key)
                if isinstance(val, list):
                    for item in val:
                        add_path(inputs, str(item))
            for key in ("output_paths", "outputs"):
                val = meta.get(key)
                if isinstance(val, list):
                    for item in val:
                        add_path(outputs, str(item))

            content = rec.get("content") or ""
            try:
                from tools.build_contract import parse_contract
            except Exception:
                try:
                    from .build_contract import parse_contract
                except Exception:
                    parse_contract = None
            contract = parse_contract(content) if parse_contract else {}
            if contract:
                roles = contract.get("path_roles") or {}
                if isinstance(roles, dict):
                    for spec in roles.values():
                        value = spec.get("path") if isinstance(spec, dict) else spec
                        if value:
                            add_path(candidates, str(value))
                # Compatibility ingestion for legacy frozen contracts.
                for key in (
                        "source_baseline_root", "source_worktree_root",
                        "source_patch_root", "experiment_root", "build_root",
                        "run_root", "dependency_root", "source_root",
                        "build_src", "workdir", "output_dir"):
                    if contract.get(key):
                        add_path(candidates, str(contract[key]))
                for item in contract.get("expected_artifacts") or []:
                    if isinstance(item, dict) and item.get("path"):
                        add_path(candidates, str(item["path"]))
                        add_path(outputs, str(item["path"]))
            for key in ("source_path", "target_path", "build_dir", "run_dir", "output_dir"):
                for m in re.finditer(rf"{key}[*\"'\s]*[:=][*\"'\s]*([^\n]+)", content, re.I):
                    add_path(candidates, m.group(1))
            for key, dst in (("input_paths", inputs), ("inputs", inputs),
                             ("output_paths", outputs), ("outputs", outputs)):
                for m in re.finditer(rf"{key}[*\"'\s]*[:=][*\"'\s]*([^\n]+)", content, re.I):
                    add_path(dst, m.group(1))
    except Exception:
        pass
    return candidates, inputs, outputs


def discover_repo_paths(state: Any, transcript_text: str = "") -> list[str]:
    """从可靠来源识别真实源码 git root；禁止 cwd/harness fallback。

    优先级：min-run 输出/工作目录 → declared_route target/source/run path →
    真实 build/run 命令目录 → source_recon/platform_profile metadata → 文本路径兜底。
    """
    min_ev = _read_min_run_evidence(state)
    commands = _transcript_commands_from_text(transcript_text)

    tiers: list[list[str]] = []
    min_paths: list[str] = []
    for ev in min_ev:
        for key in ("cwd", "inferred_cwd"):
            if ev.get(key):
                min_paths.append(str(ev[key]))
        for item in ev.get("outputs") or []:
            if item.get("path"):
                min_paths.append(str(item["path"]))
    tiers.append(min_paths)

    declared_paths, _decl_inputs, _decl_outputs = _declared_route_paths(state)
    tiers.append(declared_paths)

    cmd_paths: list[str] = []
    for c in commands:
        cmd = c.get("cmd") or ""
        if _BUILD_RUN_RE.search(cmd):
            cmd_paths.extend(_command_path_candidates(cmd, c.get("cwd")))
    tiers.append(cmd_paths)

    artifact_paths: list[str] = []
    try:
        for kind in ("source_recon", "platform_profile"):
            for entry in state.list_artifacts(kind, own_only=True):
                meta = (state.read_artifact(entry["id"]) or {}).get("metadata") or {}
                sp = meta.get("source_path")
                if sp:
                    artifact_paths.append(str(sp))
    except Exception:
        pass
    tiers.append(artifact_paths)

    text = _artifact_content_text(state) + "\n" + (transcript_text or "")
    tiers.append(_path_tokens(text))

    roots: list[str] = []
    seen: set[str] = set()
    for tier in tiers:
        for c in tier:
            root_path = _git_root_near(c)
            if root_path and root_path not in seen:
                seen.add(root_path)
                roots.append(root_path)
    return roots


def _is_config_like(path: Path) -> bool:
    return path.name in _CONFIG_NAMES or path.suffix.lower() in _CONFIG_SUFFIXES


def _is_output_like(path: Path) -> bool:
    if path.name in _OUTPUT_NAMES:
        return True
    suffixes = "".join(path.suffixes[-2:]).lower()
    return path.suffix.lower() in _OUTPUT_SUFFIXES or suffixes.endswith(".stats.nc")


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.expanduser().resolve().relative_to(root.expanduser().resolve())
        return True
    except Exception:
        return False


def _is_framework_internal_path(path: Path, state_root: Path) -> bool:
    if not state_root:
        return False
    try:
        p = path.expanduser().resolve()
        root = state_root.expanduser().resolve()
    except Exception:
        return False
    if not _is_within(p, root):
        return False
    rel = p.relative_to(root)
    return bool(rel.parts and rel.parts[0] in {"artifacts", "logs", "repro"})


def _is_source_build_metadata(path: Path) -> bool:
    name = path.name.lower()
    return name in {
        "cmakelists.txt",
        "makefile",
        "gnumakefile",
        "makefile.in",
        "makefile.am",
        "configure.ac",
        "config.status",
        "config.log",
    }


def _copy_or_reference(src: Path, dest_dir: Path, label: str,
                       max_bytes: int = _REPRO_MAX_COPY_BYTES) -> dict[str, Any] | None:
    try:
        src = src.expanduser().resolve()
        if not src.is_file():
            return None
        size = src.stat().st_size
        rec = {
            "label": label,
            "source_path": str(src),
            "size_bytes": size,
            "sha256": sha256_file(src),
            "copied": False,
        }
        if size <= max_bytes:
            dest_dir.mkdir(parents=True, exist_ok=True)
            dst = dest_dir / _safe_slug(src)
            shutil.copy2(src, dst)
            rec["copied"] = True
            rec["bundle_path"] = str(dst)
        return rec
    except Exception as e:
        return {"label": label, "source_path": str(src), "error": f"{type(e).__name__}: {e}"}


def _read_transcript_commands(transcript_path: Path | None) -> list[dict[str, Any]]:
    if not transcript_path or not transcript_path.exists():
        return []
    try:
        return _transcript_commands_from_text(
            transcript_path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return []


def _infer_cmd_cwd(cmd: str, fallback: str | None = None) -> str | None:
    cd_re = re.compile(r"(?:^|[\n;&|])\s*cd\s+([^;&|\n]+)")
    matches = list(cd_re.finditer(cmd or ""))
    if matches:
        cand = _clean_path_token(matches[-1].group(1))
        if Path(cand).is_dir():
            return cand
    if fallback and Path(os.path.expanduser(fallback)).is_dir():
        return _clean_path_token(fallback)
    return None


def _read_min_run_evidence(state: Any) -> list[dict[str, Any]]:
    p = experiment_output_dir(state, "repro/min_run_evidence.jsonl")
    legacy = (
        Path(getattr(state, "root", "") or "")
        / "repro" / "min_run_evidence.jsonl"
    )
    if not p.exists() and legacy.exists():
        p = legacy
    if not p.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    except Exception:
        pass
    return rows


def _git_source_state(repo: str, dest: Path) -> dict[str, Any]:
    dest.mkdir(parents=True, exist_ok=True)
    status_porcelain = _run(f"git -C {_q(repo)} status --porcelain 2>/dev/null", timeout=30)
    untracked = _run(f"git -C {_q(repo)} ls-files --others --exclude-standard 2>/dev/null", timeout=30)
    info = {
        "path": repo,
        "commit": _run(f"git -C {_q(repo)} rev-parse HEAD 2>/dev/null"),
        "branch": _run(f"git -C {_q(repo)} rev-parse --abbrev-ref HEAD 2>/dev/null"),
        "dirty": bool(status_porcelain),
        "changed_entries": len(status_porcelain.splitlines()) if status_porcelain else 0,
        "untracked_entries": len(untracked.splitlines()) if untracked else 0,
    }
    files = {
        "status.txt": f"git -C {_q(repo)} status --short --branch 2>/dev/null",
        "diff_stat.txt": f"git -C {_q(repo)} diff --stat 2>/dev/null",
        "diff.patch": f"git -C {_q(repo)} diff --submodule=log -- 2>/dev/null",
        "submodule_status.txt": f"git -C {_q(repo)} submodule status --recursive 2>/dev/null",
        "remote.txt": f"git -C {_q(repo)} remote -v 2>/dev/null",
        "sparse_checkout.txt": f"git -C {_q(repo)} sparse-checkout list 2>/dev/null",
    }
    for name, cmd in files.items():
        try:
            (dest / name).write_text(_run(cmd, timeout=30), encoding="utf-8")
        except Exception:
            pass
    try:
        (dest / "untracked_files.txt").write_text(untracked, encoding="utf-8")
    except Exception:
        pass

    submodules: list[dict[str, Any]] = []
    sub_status = _run(f"git -C {_q(repo)} submodule status --recursive 2>/dev/null", timeout=30)
    for line in sub_status.splitlines():
        parts = line.strip().split()
        if len(parts) < 2:
            continue
        rel = parts[1]
        sp = Path(repo) / rel
        if not sp.exists():
            submodules.append({"path": str(sp), "status": parts[0], "present": False})
            continue
        sdest = dest / "submodules" / _safe_slug(rel)
        sdest.mkdir(parents=True, exist_ok=True)
        sporcelain = _run(f"git -C {_q(sp)} status --porcelain 2>/dev/null", timeout=30)
        suntracked = _run(f"git -C {_q(sp)} ls-files --others --exclude-standard 2>/dev/null", timeout=30)
        sinfo = {
            "path": str(sp),
            "relative_path": rel,
            "status": parts[0],
            "commit": _run(f"git -C {_q(sp)} rev-parse HEAD 2>/dev/null", timeout=30),
            "branch": _run(f"git -C {_q(sp)} rev-parse --abbrev-ref HEAD 2>/dev/null", timeout=30),
            "dirty": bool(sporcelain),
            "changed_entries": len(sporcelain.splitlines()) if sporcelain else 0,
            "untracked_entries": len(suntracked.splitlines()) if suntracked else 0,
            "present": True,
        }
        for name, cmd in {
            "status.txt": f"git -C {_q(sp)} status --short --branch 2>/dev/null",
            "diff_stat.txt": f"git -C {_q(sp)} diff --stat 2>/dev/null",
            "diff.patch": f"git -C {_q(sp)} diff --submodule=log -- 2>/dev/null",
            "untracked_files.txt": f"git -C {_q(sp)} ls-files --others --exclude-standard 2>/dev/null",
            "remote.txt": f"git -C {_q(sp)} remote -v 2>/dev/null",
        }.items():
            try:
                (sdest / name).write_text(_run(cmd, timeout=30), encoding="utf-8")
            except Exception:
                pass
        (sdest / "git_info.json").write_text(json.dumps(sinfo, ensure_ascii=False, indent=2),
                                             encoding="utf-8")
        submodules.append(sinfo)
    info["submodules"] = submodules
    (dest / "git_info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
    return info


def _tool_info(tool: str) -> dict[str, str] | None:
    """探测单个工具：路径 + 版本首行。不存在返回 None。"""
    path = _run(f"command -v {tool} 2>/dev/null")
    if not path:
        return None
    ver = _run(f"{tool} --version 2>&1 | head -1") or _run(f"{tool} -V 2>&1 | head -1")
    return {"path": path, "version": ver[:200]}


def collect_snapshot(repo_paths: list[str] | None = None,
                     extra_libs: list[str] | None = None) -> dict[str, Any]:
    """采集当前实验环境的可复现快照。

    Args:
      repo_paths: 要记录 git 版本的源码目录（如 ~/E3SM_src）。
      extra_libs: 要记录 ldd 版本的关键 .so（如 libpnetcdf.so）。

    Returns: 结构化 dict —— toolchain / env_vars / hardware / source_versions / libs。
    """
    snap: dict[str, Any] = {}

    # ① 工具链版本
    toolchain: dict[str, dict] = {}
    for category, names in _PROBE_TOOLS.items():
        for t in names:
            info = _tool_info(t)
            if info:
                toolchain[t] = {**info, "category": category}
    snap["toolchain"] = toolchain

    # ② 可复现相关环境变量（只记非空的）
    snap["env_vars"] = {
        k: os.environ[k] for k in _PROBE_ENV_VARS if os.environ.get(k)
    }

    # ③ 硬件 / OS
    snap["hardware"] = {
        "os": _run("uname -srm"),
        "platform": platform.platform(),
        "nproc": _run("nproc"),
        "mem_total": _run("free -h 2>/dev/null | awk 'NR==2{print $2}'"),
        "gpu": _run("nvidia-smi --query-gpu=name,driver_version,memory.total "
                    "--format=csv,noheader 2>/dev/null"),
        "cuda_runtime": _run("nvcc --version 2>/dev/null | grep -i release"),
    }

    # ④ 源码版本（git）
    repos: dict[str, dict] = {}
    for rp in (repo_paths or []):
        p = Path(os.path.expanduser(rp))
        if not p.exists():
            continue
        commit = _run(f"git -C {_q(p)} rev-parse HEAD 2>/dev/null")
        if not commit:
            continue
        porcelain = _run(f"git -C {_q(p)} status --porcelain 2>/dev/null")
        branch = _run(f"git -C {_q(p)} rev-parse --abbrev-ref HEAD 2>/dev/null")
        repos[str(p)] = {
            "commit": commit[:40],
            "branch": branch,
            "dirty": bool(porcelain),
            "changed_files": len(porcelain.splitlines()) if porcelain else 0,
        }
    snap["source_versions"] = repos

    # ⑤ 关键动态库版本（ldd / 软链目标）
    libs: dict[str, str] = {}
    for lib in (extra_libs or []):
        found = _run(f"ldconfig -p 2>/dev/null | grep -m1 {lib}") or \
                _run(f"find {os.environ.get('CONDA_PREFIX','/usr')}/lib -name '{lib}*' 2>/dev/null | head -1")
        if found:
            libs[lib] = found[:200]
    if libs:
        snap["libs"] = libs

    return snap


_MPI_BACKEND_FLAGS: dict[str, list[tuple[str, str]]] = {
    # Intel MPI classic wrapper names can be retargeted to oneAPI LLVM backends.
    # This catches the common oneAPI >=2024 case where ifx/icx/icpx exist but
    # ifort/icc/icpc were removed, so the default wrapper backend is unusable.
    "mpiifort": [("-fc=ifx", "ifx"), ("-fc=ifort", "ifort")],
    "mpiifx": [("-fc=ifx", "ifx"), ("-fc=ifort", "ifort")],
    "mpiicc": [("-cc=icx", "icx"), ("-cc=icc", "icc")],
    "mpiicx": [("-cc=icx", "icx"), ("-cc=icc", "icc")],
    "mpiicpc": [("-cxx=icpx", "icpx"), ("-cxx=icpc", "icpc")],
    "mpiicpx": [("-cxx=icpx", "icpx"), ("-cxx=icpc", "icpc")],
}


def _mpi_show(wrapper: str, flags: str = "") -> str:
    parts = [wrapper]
    if flags:
        parts.append(flags)
    parts.append("-show")
    cmd = " ".join(parts)
    return _run(f"{cmd} 2>/dev/null") or _run(f"{wrapper} {flags} -showme 2>/dev/null")


def _mpi_underlying(wrapper: str) -> dict[str, Any]:
    """mpicc/mpif90 -show 看 wrapper 底层实际调用的 compiler（审计 E2：mpifort→conda gfortran 混用）。"""
    show = _mpi_show(wrapper)
    underlying = show.split()[0] if show else None
    upath = _run(f"command -v {underlying} 2>/dev/null") if underlying else ""
    info: dict[str, Any] = {"show": show[:200], "underlying": underlying, "underlying_path": upath}

    alternatives: dict[str, dict[str, Any]] = {}
    for flag, expected in _MPI_BACKEND_FLAGS.get(wrapper, []):
        alt_show = _mpi_show(wrapper, flag)
        if not alt_show:
            continue
        alt_underlying = alt_show.split()[0]
        alternatives[flag] = {
            "expected_backend": expected,
            "show": alt_show[:200],
            "underlying": alt_underlying,
            "underlying_path": _run(f"command -v {alt_underlying} 2>/dev/null"),
        }
    if alternatives:
        info["backend_alternatives"] = alternatives
    return info


def _config_tool_profile(tool: str) -> dict[str, Any]:
    if not _run(f"command -v {tool} 2>/dev/null"):
        return {}
    prof = {
        "path": _run(f"command -v {tool} 2>/dev/null"),
        "prefix": _run(f"{tool} --prefix 2>/dev/null"),
        "version": _run(f"{tool} --version 2>/dev/null")[:120],
        "libs": _run(f"{tool} --libs 2>/dev/null")[:300],
        "cflags": _run(f"{tool} --cflags 2>/dev/null")[:300],
    }
    hp = _run(f"{tool} --has-parallel 2>/dev/null").lower()
    if hp:
        prof["has_parallel"] = "yes" in hp or hp.strip() in {"1", "true"}
    return {k: v for k, v in prof.items() if v not in ("", None)}


def _pkg_config_profile(names: list[str]) -> dict[str, Any]:
    if not _run("command -v pkg-config 2>/dev/null"):
        return {}
    out: dict[str, Any] = {}
    for name in names:
        exists = _run(f"pkg-config --exists {shlex.quote(name)} 2>/dev/null && echo yes")
        if not exists:
            continue
        out[name] = {
            "version": _run(f"pkg-config --modversion {shlex.quote(name)} 2>/dev/null")[:80],
            "libs": _run(f"pkg-config --libs {shlex.quote(name)} 2>/dev/null")[:300],
            "cflags": _run(f"pkg-config --cflags {shlex.quote(name)} 2>/dev/null")[:300],
        }
    return out


def _platform_domains(base: dict[str, Any], mpi: dict[str, dict],
                      netcdf: dict[str, dict], hdf5: dict[str, Any]) -> dict[str, Any]:
    env = base.get("env_vars", {})
    toolchain = base.get("toolchain", {})
    return {
        "compiler": {
            "tools": {k: v for k, v in toolchain.items()
                      if k in {"gcc", "g++", "gfortran", "nvfortran", "nvc", "nvc++",
                               "nvcc", "icc", "icpc", "ifort", "ifx", "icx", "icpx",
                               "clang", "clang++"}},
            "env": {k: env.get(k) for k in ("CC", "CXX", "FC", "F90", "CFLAGS",
                                            "CXXFLAGS", "FFLAGS", "FCFLAGS") if env.get(k)},
        },
        "mpi": {"wrappers": mpi},
        "gpu": {
            "tools": {k: v for k, v in toolchain.items() if k in {"nvcc", "nvidia-smi"}},
            "env": {k: env.get(k) for k in ("CUDA_HOME", "CUDA_PATH", "HPC_SDK", "NVHPC_ROOT")
                    if env.get(k)},
        },
        "io_libraries": {
            "netcdf": netcdf,
            "hdf5": hdf5,
            "pnetcdf": _config_tool_profile("pnetcdf-config"),
            "pkg_config": _pkg_config_profile(["netcdf", "netcdf-fortran", "hdf5", "pnetcdf"]),
        },
        "math_libraries": {
            "pkg_config": _pkg_config_profile(["blas", "lapack", "openblas", "fftw3", "petsc"]),
            "env": {k: env.get(k) for k in ("MKLROOT", "FFTW_ROOT", "PETSC_DIR", "PETSC_ARCH")
                    if env.get(k)},
        },
        "python_runtime": {
            "tools": {k: v for k, v in toolchain.items() if k in {"python", "python3", "pip", "conda", "mamba"}},
            "env": {k: env.get(k) for k in ("CONDA_PREFIX", "CONDA_DEFAULT_ENV", "VIRTUAL_ENV")
                    if env.get(k)},
        },
        "build_discovery": {
            "tools": {k: v for k, v in toolchain.items() if k in {"cmake", "make", "ninja", "pkg-config"}},
            "env": {k: env.get(k) for k in ("CMAKE_PREFIX_PATH", "PKG_CONFIG_PATH", "CPATH",
                                            "LIBRARY_PATH", "LD_LIBRARY_PATH", "LDFLAGS",
                                            "CPPFLAGS") if env.get(k)},
        },
    }


def collect_platform_profile(env_path: str | None = None) -> dict[str, Any]:
    """构建前平台画像（第2步主任务）。

    在 collect_snapshot 基础上加：MPI wrapper 底层 compiler / Conda MPI 污染 /
    NetCDF-C·Fortran 来源与并行 / HDF5 并行。
    与 environment_snapshot（结束复现）分离：本函数供构建前 gate / 诊断使用。

    **结论边界**：wrapper 路径 / 底层 compiler / conda 来源是【客观事实】；
    `unresolved_risks` 是【自动检出的待核验信号】，**不是已确认的冲突，也不得作为
    硬 gate 依据**——是否构成实际冲突需结合目标 compiler / declared_route / 目标功能
    （如是否需并行 IO）判断（留给第3步 gate 的 compatibility_questions / LLM）。
    """
    if env_path:
        old = os.environ.get("EXPERIMENT_ACTIVE_BUILD_ENV")
        os.environ["EXPERIMENT_ACTIVE_BUILD_ENV"] = str(env_path)
        try:
            return collect_platform_profile()
        finally:
            if old is None:
                os.environ.pop("EXPERIMENT_ACTIVE_BUILD_ENV", None)
            else:
                os.environ["EXPERIMENT_ACTIVE_BUILD_ENV"] = old

    active_env_path = os.environ.get("EXPERIMENT_ACTIVE_BUILD_ENV")
    base = collect_snapshot()
    prof: dict[str, Any] = {
        "schema_version": "1.0",
        "toolchain": base.get("toolchain", {}),
        "env_vars": base.get("env_vars", {}),
        "hardware": base.get("hardware", {}),
    }
    risks: list[str] = []
    conda_prefix = os.environ.get("CONDA_PREFIX", "")

    # ① MPI wrapper 底层 compiler
    #    含 Intel oneAPI/classic wrapper：classic（mpiicc/mpiicpc/mpiifort）默认调用
    #    classic 后端（icc/icpc/ifort），LLVM（mpiicx/mpiicpx/mpiifx）默认调用 icx/icpx/ifx。
    #    oneAPI ≥2024 已移除 classic 后端，探到 classic wrapper 才能让 coherence_check
    #    检出“wrapper 默认后端不存在”这类错误。
    mpi: dict[str, dict] = {}
    for w in ("mpicc", "mpif90", "mpifort", "mpicxx", "mpic++",
              "mpiicc", "mpiicx", "mpiifort", "mpiifx", "mpiicpc", "mpiicpx"):
        if _run(f"command -v {w} 2>/dev/null"):
            mpi[w] = _mpi_underlying(w)
    prof["mpi_wrappers"] = mpi

    # ② Conda MPI 污染：mpirun 来自 conda env
    mpirun_path = _run("command -v mpirun 2>/dev/null")
    if mpirun_path and conda_prefix and conda_prefix in mpirun_path:
        risks.append(f"Conda MPI 污染：mpirun 来自 conda env（{mpirun_path}），"
                     "跨节点/HPC 集群运行时可能与系统 MPI 冲突")

    # ③ 工具链混用：wrapper 底层 compiler 来自 conda（与 nvfortran/系统编译器混用）
    for w, info in mpi.items():
        up = info.get("underlying_path") or ""
        if conda_prefix and up and conda_prefix in up:
            risks.append(f"{w} 底层 compiler 来自 conda（{info.get('underlying')} @ {up}），"
                         "与系统/nvfortran 混用易致 ABI / .mod 不兼容")

    # ④ NetCDF-C / NetCDF-Fortran 来源 + 并行能力
    ncconf: dict[str, dict] = {}
    for tool in ("nc-config", "nf-config"):
        if _run(f"command -v {tool} 2>/dev/null"):
            ncconf[tool] = {
                "path": _run(f"command -v {tool} 2>/dev/null"),
                "prefix": _run(f"{tool} --prefix 2>/dev/null"),
                "version": _run(f"{tool} --version 2>/dev/null")[:80],
                "has_parallel": "yes" in _run(f"{tool} --has-parallel 2>/dev/null").lower(),
                "cc": _run(f"{tool} --cc 2>/dev/null")[:120],
                "fc": _run(f"{tool} --fc 2>/dev/null")[:120],
                "libs": _run(f"{tool} --libs 2>/dev/null")[:300],
                "flibs": _run(f"{tool} --flibs 2>/dev/null")[:300],
            }
    prof["netcdf"] = ncconf
    ncp = ncconf.get("nc-config", {}).get("prefix")
    nfp = ncconf.get("nf-config", {}).get("prefix")
    if ncp and nfp and ncp != nfp:
        risks.append(f"NetCDF-C（{ncp}）与 NetCDF-Fortran（{nfp}）来源不一致，存在 ABI 风险")
    # 并行能力不一致（真测暴露：nc 并行/nf 非并行 → 并行 IO 目标隐患）
    ncpar = ncconf.get("nc-config", {}).get("has_parallel")
    nfpar = ncconf.get("nf-config", {}).get("has_parallel")
    if ncconf.get("nc-config") and ncconf.get("nf-config") and ncpar != nfpar:
        risks.append(f"NetCDF 并行能力不一致：nc-config has_parallel={ncpar}、"
                     f"nf-config has_parallel={nfpar}，若目标需并行 IO 需先统一")

    # ⑤ HDF5 并行能力
    h5par = (_run("h5pcc -showconfig 2>/dev/null | grep -i 'parallel hdf5'")
             or _run("h5cc -showconfig 2>/dev/null | grep -i 'parallel hdf5'"))
    prof["hdf5"] = {"parallel_config": h5par[:120],
                    "parallel_available": bool(_run("command -v h5pcc 2>/dev/null"))}

    prof["domains"] = _platform_domains(base, mpi, ncconf, prof["hdf5"])
    prof["unresolved_risks"] = risks
    prof["risk_signals"] = risks
    if active_env_path:
        try:
            from tools.env_provision import coherence_check, profile_metadata
        except Exception:
            try:
                from .env_provision import coherence_check, profile_metadata
            except Exception:
                coherence_check = None
                profile_metadata = None
        if coherence_check is not None:
            prof["coherence"] = coherence_check(prof)
        if profile_metadata is not None:
            prof["env_provision"] = profile_metadata(active_env_path, prof)
            prof["env_fingerprint"] = prof["env_provision"].get("env_fingerprint")
            prof["generated_by"] = "framework_probe_under_env"
    return prof


# ─────────────────────────────────────────────────────────────────────────────
# 可信度 / verdict 提取（需求 ⑦.6）
# ─────────────────────────────────────────────────────────────────────────────

# verdict / credibility 的同义词表（canon → [变体]，含中英文）。
# 设计：不假设 agent 严格写 `## Credibility` + 单一英文词（06-10 审计证明 agent
# 不一定遵守格式）。多写法都认，认不出诚实标 unknown，绝不编造。
_VERDICT_WORDS = {
    "validated":    ["validated", "verified", "confirmed", "成立", "验证通过", "支持假设"],
    "refuted":      ["refuted", "rejected", "falsified", "证伪", "否定", "不成立"],
    "inconclusive": ["inconclusive", "undetermined", "存疑", "无法判定", "不确定"],
    "provisional":  ["provisional", "tentative", "暂定", "初步"],
}
_CRED_WORDS = {
    "invalid":      ["invalid", "unreliable", "untrustworthy", "无效", "不可信", "不可靠"],
    "questionable": ["questionable", "uncertain", "dubious", "存疑", "可疑", "不确定"],
    "reliable":     ["reliable", "trustworthy", "credible", "可靠", "可信"],
}


def _match_canon(text: str, word_table: dict[str, list[str]]) -> str | None:
    """在 text 里找 word_table 任一变体，返回 canon 名。按表顺序优先（invalid > questionable > reliable，
    宁可保守判低）。ascii 词用词边界，中文词直接匹配。"""
    for canon, variants in word_table.items():
        for v in variants:
            pat = rf"\b{re.escape(v)}\b" if v.isascii() else re.escape(v)
            if re.search(pat, text, re.I):
                return canon
    return None


def _find_section(content: str, *headers: str) -> str | None:
    """找任意 header 变体的 markdown 段（## / ### / **bold** 都认）。"""
    for h in headers:
        m = re.search(rf"(?:^|\n)\s*(?:#{{1,4}}\s*|\*\*)\s*{re.escape(h)}\b.*?"
                      rf"(?=\n\s*#{{1,4}}\s|\Z)", content, re.S | re.I)
        if m:
            return m.group(0)
    return None


def _extract_explicit_label(
        text: str, labels: tuple[str, ...],
        word_table: dict[str, list[str]],
) -> tuple[bool, str | None]:
    """Extract a machine-labelled status without scanning surrounding prose.

    The boolean distinguishes an absent label from an explicit but unknown
    value such as ``verdict: infeasible``.  In the latter case we must not fall
    through to prose scanning and turn a nearby word like ``validated`` into a
    false status.
    """
    names = "|".join(re.escape(label) for label in labels)
    match = re.search(
        rf"(?im)^\s*(?:[-*]\s*)?(?:\*\*)?\s*(?:{names})\s*[:：]\s*"
        r"\*{0,2}([A-Za-z一-鿿_-]+)", text or "")
    if not match:
        return False, None
    return True, _match_canon(match.group(1), word_table)


def extract_verdict_credibility(experiment_log_content: str) -> dict[str, Any]:
    """从 experiment_log content 提取结构化 verdict + credibility，鲁棒应对多种写法。

    提取顺序（机器标签优先，避免叙述性文字污染）：
      1. 相关段内的独立 `verdict: X` / `credibility: X` 行；
      2. 文档中的同类独立标签行（兼容缺少标准段标题的旧 artifact）；
      3. 没有机器标签时，才在相关段内匹配同义词；
      4. 都没有 → 该字段缺省（hook 视为 unknown，不 promote）。
         显式但未知的值也不回退到全文词语扫描。
    """
    out: dict[str, Any] = {}
    c = experiment_log_content or ""

    # ── Verdict ──
    vseg = _find_section(c, "Hypothesis Verdict", "Verdict", "假设判定", "判定", "结论")
    explicit_found = False
    if vseg:
        explicit_found, canon = _extract_explicit_label(
            vseg, ("verdict", "结论", "判定"), _VERDICT_WORDS)
        if explicit_found:
            if canon:
                out["verdict"] = canon
        else:
            canon = _match_canon(vseg, _VERDICT_WORDS)
            if canon:
                out["verdict"] = canon
        cid = re.search(r"(claim__[\w]+|claim_[0-9a-f]{6,})", vseg)
        if cid:
            out["verdict_claim_id"] = cid.group(1)
    if not explicit_found:
        # 行内回退：只认独立标签，不让自然语言中的“verdict”触发。
        explicit_found, canon = _extract_explicit_label(
            c, ("verdict",), _VERDICT_WORDS)
        if explicit_found:
            if canon:
                out["verdict"] = canon
    if "verdict_claim_id" not in out:
        cid = re.search(r"(claim__[\w]+|claim_[0-9a-f]{6,})", c)
        if cid:
            out["verdict_claim_id"] = cid.group(1)

    # ── Credibility ──
    cseg = _find_section(c, "Credibility", "可信度", "可靠性", "Reliability")
    credibility_explicit = False
    if cseg:
        credibility_explicit, canon = _extract_explicit_label(
            cseg, ("credibility", "可信度", "可靠性"), _CRED_WORDS)
        if credibility_explicit:
            if canon:
                out["credibility"] = canon
        else:
            canon = _match_canon(cseg, _CRED_WORDS)
            if canon:
                out["credibility"] = canon
    if not credibility_explicit:
        # 行内回退（只认明确的 credibility: X，不在全文裸搜单词）。
        credibility_explicit, canon = _extract_explicit_label(
            c, ("credibility", "可信度", "可靠性"), _CRED_WORDS)
        if credibility_explicit:
            if canon:
                out["credibility"] = canon

    return out


def select_experiment_log_result(state: Any) -> tuple[Path | None, dict[str, Any]]:
    """Pick the real experiment_log and extract verdict/credibility.

    Agents sometimes save route notes with artifact_type=experiment_log, producing ids like
    experiment_log__declared_route. Do not let those empty/non-final logs hide the real
    frozen experiment_log that contains ## Verdict / ## Credibility.
    """
    best_path: Path | None = None
    best_info: dict[str, Any] = {}
    best_score = -1

    for entry in state.list_artifacts("experiment_log", own_only=True):
        artifact_id = str(entry.get("id") or "")
        rec = state.read_artifact(artifact_id)
        if not isinstance(rec, dict) or rec.get("type") != "experiment_log":
            continue

        content = rec.get("content") or ""
        metadata = rec.get("metadata") or {}
        info = extract_verdict_credibility(content)
        name = str(rec.get("name") or artifact_id)

        score = 0
        score += 100 * len(info)
        if metadata.get("frozen"):
            score += 50
        if re.search(r"(^|\n)\s*#{1,4}\s*(Verdict|Hypothesis Verdict|Credibility)\b",
                     content, re.I):
            score += 30
        if name == "declared_route" or "declared_route" in artifact_id:
            score -= 100
        score += min(len(content) // 1000, 20)

        if score > best_score:
            best_score = score
            best_path = state.find_artifact_path(artifact_id)
            best_info = info

    return best_path, best_info
def _candidate_files_from_text(text: str, predicate) -> list[Path]:
    out: list[Path] = []
    seen: set[str] = set()
    for tok in _path_tokens(text):
        p = Path(tok)
        if p.is_file() and predicate(p):
            rp = str(p.resolve())
            if rp not in seen:
                seen.add(rp)
                out.append(p)
    return out[:_REPRO_MAX_CANDIDATES]


def _changed_config_files(repo: str) -> list[Path]:
    rows = _run(f"git -C {_q(repo)} status --porcelain 2>/dev/null", timeout=20).splitlines()
    out: list[Path] = []
    for row in rows:
        if not row.strip():
            continue
        # porcelain: "XY <path>"；_run() 会 strip，形如 "M path" 也要兼容。
        parts = row.split(maxsplit=1)
        if len(parts) < 2:
            continue
        rel = parts[1].split(" -> ")[-1].strip()
        p = Path(repo) / rel
        if p.is_file() and _is_config_like(p):
            out.append(p)
    return out


def _min_run_output_candidates(evidence: list[dict[str, Any]]) -> list[Path]:
    out: list[Path] = []
    seen: set[str] = set()
    patterns = [
        "ocean.stats", "*.stats", "*.stats.nc", "exitcode", "time_stamp.out",
        "CPU_stats", "*.out", "*.log", "*.nc",
    ]
    for ev in evidence:
        for item in ev.get("outputs") or []:
            p = Path(os.path.expanduser(str(item.get("path") or "")))
            if p.is_file():
                rp = str(p.resolve())
                if rp not in seen:
                    seen.add(rp)
                    out.append(p)
        cwd = ev.get("cwd") or ev.get("inferred_cwd")
        if cwd and Path(os.path.expanduser(str(cwd))).is_dir():
            base = Path(os.path.expanduser(str(cwd)))
            for pat in patterns:
                for p in base.glob(pat):
                    if p.is_file():
                        rp = str(p.resolve())
                        if rp not in seen:
                            seen.add(rp)
                            out.append(p)
    return out[:_REPRO_MAX_CANDIDATES]


def _runtime_cwd_output_candidates(runtime_configs: list[dict[str, Any]]) -> list[Path]:
    out: list[Path] = []
    seen: set[str] = set()
    patterns = [
        "ocean.stats", "*.stats", "*.stats.nc", "exitcode", "time_stamp.out",
        "CPU_stats", "result.txt", "result.json", "result.csv", "*.nc",
        "*.out", "*.log",
    ]
    for cfg in runtime_configs:
        cwd = cfg.get("cwd")
        if not cwd:
            continue
        base = Path(os.path.expanduser(str(cwd)))
        if not base.is_dir():
            continue
        for pat in patterns:
            for p in base.glob(pat):
                if not p.is_file():
                    continue
                rp = str(p.resolve())
                if rp not in seen:
                    seen.add(rp)
                    out.append(p)
    return out[:_REPRO_MAX_CANDIDATES]


def _runtime_config_from_cmd(cmd: str, cwd: str | None = None) -> dict[str, Any]:
    cfg: dict[str, Any] = {}
    c = cmd or ""
    m = re.search(r"(?:^|[\s;&])OMP_NUM_THREADS=(\d+)", c)
    if m:
        cfg["omp_threads"] = int(m.group(1))
    mpi = re.search(r"\b(mpirun|mpiexec|srun)\b", c)
    if mpi:
        cfg["launcher"] = mpi.group(1)
    ranks = re.search(r"\b(?:mpirun|mpiexec|srun)\b[^\n;&|]*?(?:-np|-n|--ntasks)\s+(\d+)", c)
    if ranks:
        cfg["mpi_ranks"] = int(ranks.group(1))
    inferred = _infer_cmd_cwd(c, cwd)
    if inferred:
        cfg["cwd"] = inferred
    try:
        words = shlex.split(c.split("&&")[-1])
    except Exception:
        words = []
    while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
        words = words[1:]
    exe = None
    if words:
        if words[0] in ("mpirun", "mpiexec", "srun"):
            skip_next = False
            for w in words[1:]:
                if skip_next:
                    skip_next = False
                    continue
                if w in ("-np", "-n", "--ntasks", "-c", "--cpus-per-task"):
                    skip_next = True
                    continue
                if w.startswith("-"):
                    continue
                exe = w
                break
        elif "=" not in words[0]:
            exe = words[0]
    if exe:
        cfg["executable"] = exe
    return cfg


def _runtime_configs(commands: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    configs: list[dict[str, Any]] = []
    for ev in evidence:
        cfg = dict(ev.get("runtime_config") or {})
        if not cfg:
            cfg = _runtime_config_from_cmd(ev.get("cmd") or "", ev.get("cwd") or ev.get("inferred_cwd"))
        if cfg:
            cfg["source"] = "min_run_evidence"
            configs.append(cfg)
    for cmd_rec in commands:
        cmd = cmd_rec.get("cmd") or ""
        if re.search(r"\b(mpirun|mpiexec|srun)\b", cmd):
            cfg = _runtime_config_from_cmd(cmd, cmd_rec.get("cwd"))
            if cfg:
                cfg["source"] = "transcript"
                cfg["turn"] = cmd_rec.get("turn")
                configs.append(cfg)
    return configs


def _external_dependencies_from_commands(commands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deps: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for rec in commands:
        cmd = rec.get("cmd") or ""
        for m in re.finditer(r"(?:^|[\n;&|])\s*(git\s+clone\b[^\n;&|]+)", cmd):
            segment = m.group(1)
            try:
                words = shlex.split(segment)
            except Exception:
                words = segment.split()
            url = None
            dest = None
            i = 2  # git clone
            opts_with_arg = {"--branch", "-b", "--depth", "--origin", "-o", "--config", "-c",
                             "--reference", "--reference-if-able", "--separate-git-dir"}
            positional: list[str] = []
            while i < len(words):
                w = words[i]
                if w in {">", "1>", "2>", ">>", "1>>", "2>>", "&>"} or re.match(r"^\d?>", w):
                    break
                if w in opts_with_arg:
                    i += 2
                    continue
                if w.startswith("--depth=") or w.startswith("--branch="):
                    i += 1
                    continue
                if w.startswith("-"):
                    i += 1
                    continue
                positional.append(w)
                i += 1
            if positional:
                url = positional[0]
                dest = positional[1] if len(positional) > 1 else None
            if not url:
                continue
            key = ("git_clone", url)
            if key in seen:
                continue
            seen.add(key)
            seen.add(("url", url))
            dep = {
                "type": "git_clone",
                "url": url,
                "dest": dest,
                "turn": rec.get("turn"),
                "not_copied": True,
            }
            if dest:
                dpath = _clean_path_token(dest)
                root = _git_root_near(dpath)
                if root:
                    dep["resolved_path"] = root
                    dep["commit"] = _run(f"git -C {_q(root)} rev-parse HEAD 2>/dev/null", timeout=30)
            deps.append(dep)
        for url in re.findall(r"https?://[^\s\"'<>]+", cmd):
            key = ("url", url)
            if key not in seen:
                seen.add(key)
                deps.append({"type": "download_url", "url": url, "turn": rec.get("turn"),
                             "not_copied": True})
    return deps


def _looks_rerun_relevant(cmd: str) -> bool:
    c = (cmd or "").strip()
    if not c:
        return False
    return bool(re.search(
        r"\b(make|gmake|ninja|cmake\s+--build|mpirun|mpiexec|srun|python\s+run_|"
        r"sed\s+-i|patch\b|git\s+sparse-checkout|git\s+submodule|configure|autogen)\b",
        c, re.I))


def _write_rerun_script(path: Path, source_paths: list[str], commands: list[dict[str, Any]]) -> None:
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        "# Auto-generated replay skeleton, not a guaranteed one-command reproduction.",
        "# Restore environment, external dependencies, source commits/submodules, and patches first.",
        "# Review every command before execution.",
    ]
    if source_paths:
        lines += [f"cd {_q(source_paths[0])}", ""]
    lines += [
        "# The complete tool command log is in commands.jsonl.",
        "# Candidate build/run/edit commands observed during the original run:",
    ]
    n = 0
    for entry in commands:
        cmd = entry.get("cmd") or ""
        if not _looks_rerun_relevant(cmd):
            continue
        lines.append(cmd)
        n += 1
        if n >= 80:
            lines.append("# ... truncated; see commands.jsonl for the full command stream")
            break
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        path.chmod(0o755)
    except Exception:
        pass


def _bundle_summary_md(manifest: dict[str, Any]) -> str:
    lines = [
        "# Reproducibility Bundle",
        "",
        f"- run_id: `{manifest.get('run_id')}`",
        f"- created_at: `{manifest.get('created_at')}`",
        f"- bundle_path: `{manifest.get('bundle_path')}`",
        f"- source_repos: {len(manifest.get('source_repos') or [])}",
        f"- commands: {manifest.get('commands_count', 0)}",
        f"- inputs copied/referenced: {len(manifest.get('inputs') or [])}",
        f"- outputs copied/referenced: {len(manifest.get('outputs') or [])}",
        f"- min_run_success: {manifest.get('min_run_success')}",
        f"- external_dependencies: {len(manifest.get('external_dependencies') or [])}",
        "",
        "## Source Repos",
    ]
    for repo in manifest.get("source_repos") or []:
        lines.append(f"- `{repo.get('path')}` @ `{(repo.get('commit') or '')[:12]}` dirty={repo.get('dirty')}")
    if manifest.get("runtime_config"):
        lines += ["", "## Runtime Config",
                  "```json",
                  json.dumps(manifest.get("runtime_config"), ensure_ascii=False, indent=2),
                  "```"]
    lines += ["", "## Entry Points", "- `manifest.json` is the machine-readable index.",
              "- `rerun.sh` is a replay skeleton; inspect before running.",
              "- `commands.jsonl` contains the full captured command stream."]
    return "\n".join(lines)


def create_repro_bundle(state: Any, snap: dict[str, Any], result_info: dict,
                        experiment_log_path: Path | None = None,
                        transcript_text: str = "",
                        force: bool = False) -> dict[str, Any] | None:
    """生成 runs/<id>/outputs/experiment/repro 可复现包并保存 artifact。

    触发条件：min-run 证据成功、experiment_log credibility=reliable，或
    ``force=True``（正式主运行即使失败也需要留存复现证据）。
    不复制整棵源码；保存 commit/submodule/status/diff，并复制小型输入与关键输出。
    """
    root = Path(getattr(state, "root", "") or "")
    if not root:
        return None
    run_id = getattr(state, "run_id", root.name)
    repro_dir = experiment_output_dir(state, "repro", create=True)

    min_evidence = _read_min_run_evidence(state)
    min_success = any(bool(e.get("milestone")) for e in min_evidence)
    if not force and not min_success and result_info.get("credibility") != "reliable":
        return None

    transcript_path = Path(getattr(state, "transcript_path", root / "transcript.jsonl"))
    commands = _read_transcript_commands(transcript_path)
    runtime_configs = _runtime_configs(commands, min_evidence)
    discovered_sources = discover_repo_paths(state, transcript_text=transcript_text)
    source_paths = discovered_sources or [
        p for p in snap.get("source_versions", {}).keys() if not _is_harness_path(p)
    ]

    # Core records.
    (repro_dir / "commands.jsonl").write_text(
        "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in commands),
        encoding="utf-8",
    )
    (repro_dir / "min_run_evidence.jsonl").write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in min_evidence),
        encoding="utf-8",
    )
    (repro_dir / "environment_snapshot.json").write_text(
        json.dumps(snap, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if experiment_log_path and Path(experiment_log_path).exists():
        shutil.copy2(experiment_log_path, repro_dir / Path(experiment_log_path).name)
    try:
        # 复现包是**自包含的导出**：把本 run 的记录（正文 + 账本上的事实）写成一份
        # JSON 放进去。研究记录本身是原生文件 + 账本（core/ledger），这里只是抄件。
        art_dst = repro_dir / "artifacts"
        art_dst.mkdir(exist_ok=True)
        for entry in state.list_artifacts(own_only=True):
            rec = state.read_artifact(entry["id"])
            if not isinstance(rec, dict):
                continue
            blob = json.dumps({"id": entry["id"], **rec}, ensure_ascii=False, indent=2)
            if len(blob.encode("utf-8")) <= _REPRO_MAX_COPY_BYTES:
                (art_dst / f"{entry['id']}.json").write_text(blob, encoding="utf-8")
    except Exception:
        pass

    source_records: list[dict[str, Any]] = []
    for repo in source_paths:
        if _is_harness_path(repo):
            continue
        source_records.append(_git_source_state(repo, repro_dir / "source_state" / _safe_slug(repo)))

    text = transcript_text + "\n" + _artifact_content_text(state)
    _declared_paths, declared_inputs, declared_outputs = _declared_route_paths(state)

    output_candidates: list[tuple[str, Path]] = [
        ("declared_output", Path(p)) for p in declared_outputs if Path(p).expanduser().is_file()
    ]
    output_candidates.extend(("min_run_output", p) for p in _min_run_output_candidates(min_evidence))
    output_candidates.extend(("runtime_cwd_output", p) for p in _runtime_cwd_output_candidates(runtime_configs))
    if not output_candidates:
        output_candidates.extend(
            ("heuristic_output", p) for p in _candidate_files_from_text(
                text,
                lambda p: (
                    _is_output_like(p)
                    and not _is_framework_internal_path(p, root)
                    and not _is_source_build_metadata(p)
                ),
            )
        )

    known_output_paths = {
        str(Path(p).expanduser().resolve())
        for _label, p in output_candidates
        if Path(p).expanduser().is_file()
    }

    input_candidates: list[tuple[str, Path]] = [
        ("declared_input", Path(p)) for p in declared_inputs
        if Path(p).expanduser().is_file()
        and str(Path(p).expanduser().resolve()) not in known_output_paths
    ]
    for repo in source_paths:
        input_candidates.extend(
            ("git_changed_config", p) for p in _changed_config_files(repo)
            if str(Path(p).expanduser().resolve()) not in known_output_paths
        )
    if not input_candidates:
        input_candidates.extend(
            ("heuristic_input", p) for p in _candidate_files_from_text(text, _is_config_like)
            if str(Path(p).expanduser().resolve()) not in known_output_paths
        )

    inputs: list[dict[str, Any]] = []
    seen_inputs: set[str] = set()
    for label, p in input_candidates:
        rp = str(Path(p).expanduser().resolve())
        if rp in seen_inputs:
            continue
        seen_inputs.add(rp)
        rec = _copy_or_reference(Path(p), repro_dir / "inputs", label)
        if rec:
            inputs.append(rec)

    outputs: list[dict[str, Any]] = []
    seen_outputs: set[str] = set()
    for label, p in output_candidates:
        rp = str(Path(p).expanduser().resolve())
        if rp in seen_outputs:
            continue
        seen_outputs.add(rp)
        rec = _copy_or_reference(Path(p), repro_dir / "outputs", label)
        if rec:
            outputs.append(rec)

    # Optional environment locks. Best-effort and local-only.
    env_dir = repro_dir / "env"
    env_dir.mkdir(exist_ok=True)
    conda_explicit = _run("conda list --explicit 2>/dev/null", timeout=60)
    if conda_explicit:
        (env_dir / "conda-list-explicit.txt").write_text(conda_explicit, encoding="utf-8")
    conda_export = _run("conda env export --no-builds 2>/dev/null", timeout=60)
    if conda_export:
        (env_dir / "conda-env-no-builds.yml").write_text(conda_export, encoding="utf-8")

    _write_rerun_script(repro_dir / "rerun.sh", source_paths, commands)
    primary_runtime_config = next(
        (c for c in runtime_configs if c.get("source") == "min_run_evidence"),
        runtime_configs[-1] if runtime_configs else {},
    )
    external_dependencies = _external_dependencies_from_commands(commands)

    manifest = {
        "schema_version": "1.0",
        "run_id": run_id,
        "project_id": getattr(state, "project_id", None),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "bundle_path": str(repro_dir),
        "source_repos": source_records,
        "commands_count": len(commands),
        "inputs": inputs,
        "outputs": outputs,
        "runtime_config": primary_runtime_config,
        "runtime_configs": runtime_configs,
        "external_dependencies": external_dependencies,
        "repro_notes": [
            "rerun.sh is a replay skeleton, not a guaranteed one-command reproduction.",
            "External dependencies are recorded from command logs and are not copied into the bundle.",
            "Large files are referenced by path/size/sha256 instead of copied.",
        ],
        "min_run_success": min_success,
        "min_run_evidence_count": len(min_evidence),
        "result_info": result_info,
    }
    manifest_path = repro_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest["manifest_sha256"] = sha256_file(manifest_path)

    try:
        state.save_artifact(
            artifact_type="repro_bundle",
            name=f"repro_bundle_{run_id}",
            content=_bundle_summary_md(manifest),
            metadata={
                "run_id": run_id,
                "project_id": getattr(state, "project_id", None),
                "bundle_path": str(repro_dir),
                "manifest_path": str(manifest_path),
                "manifest_sha256": manifest["manifest_sha256"],
                "source_repos": [r.get("path") for r in source_records],
                "commands_count": len(commands),
                "inputs_count": len(inputs),
                "outputs_count": len(outputs),
                "external_dependencies_count": len(external_dependencies),
                "min_run_success": min_success,
                **result_info,
            },
        )
    except Exception:
        pass
    return manifest


# ─────────────────────────────────────────────────────────────────────────────
# 实验组管理（需求 ⑦.5）—— 按 hypothesis 聚合跨 run 的实验，比较可复现性
# ─────────────────────────────────────────────────────────────────────────────

def render_markdown(snap: dict[str, Any]) -> str:
    """把快照渲染成人类可读 markdown（存进 artifact content）。"""
    lines = ["# Environment Reproducibility Snapshot", ""]

    tc = snap.get("toolchain", {})
    if tc:
        lines.append("## Toolchain")
        for name, info in sorted(tc.items()):
            lines.append(f"- **{name}** (`{info.get('path','')}`): {info.get('version','')}")
        lines.append("")

    src = snap.get("source_versions", {})
    if src:
        lines.append("## Source versions (git)")
        for path, info in src.items():
            dirty = " ⚠️ DIRTY" if info.get("dirty") else ""
            nchanged = info.get("changed_files") or 0
            changed = f" — {nchanged} changed" if nchanged else ""
            commit12 = (info.get("commit") or "")[:12]
            lines.append(f"- `{path}` @ {commit12} ({info.get('branch','')}){dirty}{changed}")
        lines.append("")

    hw = snap.get("hardware", {})
    if hw:
        lines.append("## Hardware / OS")
        for k in ("os", "nproc", "mem_total", "gpu", "cuda_runtime"):
            if hw.get(k):
                lines.append(f"- **{k}**: {hw[k]}")
        lines.append("")

    ev = snap.get("env_vars", {})
    if ev:
        lines.append("## Environment variables")
        for k, v in ev.items():
            shown = v if len(v) <= 300 else v[:300] + " …"
            lines.append(f"- `{k}` = {shown}")
        lines.append("")

    libs = snap.get("libs", {})
    if libs:
        lines.append("## Key libraries")
        for k, v in libs.items():
            lines.append(f"- {k}: {v}")
        lines.append("")

    return "\n".join(lines)
