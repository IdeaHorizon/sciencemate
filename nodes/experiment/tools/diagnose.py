"""
通用诊断与修复引擎 (v2.0)

整合：错误诊断 + 修复建议 + 源码模式替换
保持完全通用 —— 不包含特定科学应用的硬编码规则。

设计原则：
- 基于可配置的 ErrorPattern / FixPattern，不硬编码
- 领域知识（具体错误模式、修哪里、怎么修）由 diagnose_patterns.yaml 提供
- 自动备份原始文件，生成 diff 预览
- **v2.0**: 从 diagnose_patterns/diagnose_patterns.yaml 加载错误模式

用法：
    from tools.diagnose import DiagnoseEngine, SourceFixer

    # 诊断日志 - 自动加载 YAML 模式
    engine = DiagnoseEngine.from_yaml_patterns()
    report = engine.analyze_log("/path/to/build.log")
    report = engine.analyze_output(stderr_text)

    # 源码模式替换
    fixer = SourceFixer()
    fixer.add_pattern(name="my_fix", search=r'old_func', replace=r'new_func',
                      file_glob="*.F90", description="Rename old_func")
    result = fixer.scan("/path/to/src")       # 扫描
    result = fixer.apply("/path/to/src")      # 干运行
    result = fixer.apply("/path/to/src", dry_run=False)  # 实际修复
"""

from __future__ import annotations

import re
import os
import shutil
import difflib
import hashlib
import json
import logging
import yaml
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Optional
from datetime import datetime
from enum import Enum


log = logging.getLogger(__name__)


# ════════════════════════════════════════════
# 通用数据类型
# ════════════════════════════════════════════

class Severity(Enum):
    CRITICAL = "critical"
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class Category(Enum):
    COMPILATION = "compilation"
    # 049-3：diagnose_patterns.yaml 从一开始就有 8 条 category: linking，而枚举里没有
    # 它——加载器静默改成 runtime（Codex 049/01 P1 实测 ld_error 报成 runtime）。
    LINKING = "linking"
    RUNTIME = "runtime"
    CONFIGURATION = "configuration"
    ENVIRONMENT = "environment"
    RESOURCE = "resource"
    INPUT = "input"
    MPI = "mpi"
    IO = "io"
    MEMORY = "memory"
    DEPENDENCY = "dependency"


@dataclass
class ErrorPattern:
    """可配置的错误识别模式"""
    pattern: str                    # 正则表达式
    category: Category
    severity: Severity
    confidence: float = 0.8
    context_hint: str = ""
    generic_fix: str = ""
    configured: bool = False


@dataclass
class Finding:
    """单条诊断发现"""
    message: str
    category: Category
    severity: Severity
    line_number: int | None = None
    matched_text: str = ""
    confidence: float = 0.0
    suggestions: list[str] = field(default_factory=list)
    generic_fix: str = ""
    context_hint: str = ""
    match_start: int = -1
    match_end: int = -1
    configured: bool = False


@dataclass
class FixPattern:
    """源码修复模式（领域知识由 SKILL.md 提供，这里只是数据结构）"""
    name: str
    search: str            # 正则搜索模式
    replace: str           # 替换内容
    file_glob: str = "*"   # 文件匹配 glob
    description: str = ""
    auto_apply: bool = False
    context_lines: int = 3


# ════════════════════════════════════════════
# YAML 模式加载 (v2.0 新增)
# ════════════════════════════════════════════

def load_yaml_patterns() -> list[ErrorPattern]:
    """从 diagnose_patterns/diagnose_patterns.yaml 加载错误模式

    YAML 规则作为一个配置单元加载。任一正则无效时拒绝整组配置并
    回退到内置规则，避免部分规则悄悄生效而产生不可预测的优先级。

    Returns:
        ErrorPattern 列表
    """
    yaml_file = Path(__file__).parent / "diagnose_patterns" / "diagnose_patterns.yaml"
    if not yaml_file.exists():
        log.warning("诊断规则文件不存在，回退到内置规则: %s", yaml_file)
        return []

    patterns = []
    try:
        with open(yaml_file, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not isinstance(data, dict) or not isinstance(data.get("patterns"), list):
            log.warning("诊断规则文件缺少 patterns 列表，回退到内置规则: %s", yaml_file)
            return []

        for p in data["patterns"]:
            # 映射 category 字符串到 Category 枚举
            cat_str = p.get("category", "runtime")
            try:
                cat = Category(cat_str)
            except ValueError:
                # 不拒整份文件（一条拼错不该让所有诊断消失），但也不静默改类：
                # 写错的 category 会让按类别做的建议与统计漂移，日志里要能看见。
                log.warning("诊断规则 %s 的 category=%r 不在枚举里，按 runtime 处理",
                            p.get("id"), cat_str)
                cat = Category.RUNTIME

            # 映射 severity 字符串到 Severity 枚举
            sev_str = p.get("severity", "error")
            try:
                sev = Severity(sev_str)
            except ValueError:
                log.warning("诊断规则 %s 的 severity=%r 不在枚举里，按 error 处理",
                            p.get("id"), sev_str)
                sev = Severity.ERROR

            pattern_text = p["regex"]
            # Validate at the configuration boundary.  Deferring this to
            # analyze_output would let one bad optional rule disable every
            # built-in diagnosis for that turn.
            re.compile(pattern_text, re.IGNORECASE)

            patterns.append(ErrorPattern(
                pattern=pattern_text,
                category=cat,
                severity=sev,
                confidence=p.get("confidence", 0.8),
                context_hint=p.get("context_hint", ""),
                generic_fix=p.get("generic_fix", ""),
                configured=True,
            ))
    except Exception as e:
        log.warning("诊断规则文件读取失败，回退到内置规则 %s: %s", yaml_file, e)
        patterns = []

    return patterns


# ════════════════════════════════════════════
# 诊断引擎
# ════════════════════════════════════════════

class DiagnoseEngine:
    """通用日志/输出诊断引擎"""

    # 内置通用错误签名 —— 适用于绝大多数 HPC/科学计算工具
    BUILTIN: list[ErrorPattern] = [
        # 编译
        ErrorPattern(r"(?i)error:|undefined reference|cannot find.*library|ld:.*error",
                     Category.COMPILATION, Severity.ERROR, 0.9, "编译阶段"),
        ErrorPattern(r"(?i)warning:|deprecated",
                     Category.COMPILATION, Severity.WARNING, 0.7, "编译阶段"),

        # 运行时
        ErrorPattern(r"(?i)segmentation fault|segfault|sigsegv",
                     Category.RUNTIME, Severity.CRITICAL, 0.95, "运行阶段"),
        ErrorPattern(r"(?i)floating point exception|sigfpe|divide by zero",
                     Category.RUNTIME, Severity.CRITICAL, 0.95, "运行阶段"),
        ErrorPattern(r"(?i)abort|assertion.*failed",
                     Category.RUNTIME, Severity.ERROR, 0.85, "运行阶段"),

        # 环境
        ErrorPattern(r"(?i)command not found|no such file or directory",
                     Category.ENVIRONMENT, Severity.ERROR, 0.9, "环境检查"),
        ErrorPattern(r"(?i)permission denied",
                     Category.ENVIRONMENT, Severity.ERROR, 0.95, "权限检查"),
        ErrorPattern(r"(?i)module not found|no module named|import error",
                     Category.DEPENDENCY, Severity.ERROR, 0.9, "Python 环境"),

        # MPI
        ErrorPattern(r"(?i)mpi_init|mpi error|mpi_abort",
                     Category.MPI, Severity.ERROR, 0.85, "MPI 运行"),
        ErrorPattern(r"(?i)rank \d+ died|process \d+ exited|mpi.*terminate",
                     Category.MPI, Severity.CRITICAL, 0.9, "MPI 运行"),
        ErrorPattern(r"(?i)\b(?:ucx|hcoll)\b|failed to receive ucx worker address|destination is unreachable",
                     Category.MPI, Severity.CRITICAL, 0.98, "Open MPI/HPC-X 通信栈"),

        # 资源
        ErrorPattern(r"(?i)out of memory|oom|memory allocation|cannot allocate",
                     Category.MEMORY, Severity.CRITICAL, 0.9, "资源限制"),
        ErrorPattern(r"(?i)disk full|no space left|quota exceeded",
                     Category.RESOURCE, Severity.CRITICAL, 0.95, "存储限制"),

        # 输入
        ErrorPattern(r"(?i)input.*error|invalid.*parameter|bad.*value|namelist.*error",
                     Category.INPUT, Severity.ERROR, 0.8, "输入验证"),

        # 文件 I/O
        ErrorPattern(r"(?i)cannot open.*file|failed to open|io error|read error|write error",
                     Category.IO, Severity.ERROR, 0.85, "文件操作"),
    ]

    def __init__(self, extra_patterns: list[ErrorPattern] | None = None):
        self.patterns = list(self.BUILTIN)
        if extra_patterns:
            self.patterns.extend(extra_patterns)

    @classmethod
    def from_yaml_patterns(cls) -> "DiagnoseEngine":
        """从 diagnose_patterns.yaml 创建诊断引擎实例"""
        yaml_patterns = load_yaml_patterns()
        return cls(extra_patterns=yaml_patterns)

    def add(self, ep: ErrorPattern) -> None:
        self.patterns.append(ep)

    def analyze_log(self, log_path: str, max_lines: int = 500) -> dict:
        """分析日志文件，返回诊断报告"""
        path = Path(log_path)
        if not path.exists():
            return {"source": str(log_path), "summary": f"日志文件不存在: {log_path}",
                    "findings": [], "statistics": {}}

        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except Exception as e:
            return {"source": str(log_path), "summary": f"无法读取: {e}",
                    "findings": [], "statistics": {}}

        lines = lines[-max_lines:] if len(lines) > max_lines else lines
        return self._analyze(lines, str(log_path))

    def analyze_output(self, text: str, source: str = "command output") -> dict:
        """分析命令输出文本"""
        return self._analyze(text.split("\n"), source)

    def _analyze(self, lines: list[str], source: str) -> dict:
        findings: list[Finding] = []
        for line_no, line in enumerate(lines, 1):
            stripped = line.strip()
            if not stripped:
                continue
            for sig in self.patterns:
                m = re.search(sig.pattern, stripped, re.IGNORECASE)
                if m:
                    findings.append(Finding(
                        message=f"[{sig.category.value.upper()}] {stripped[:120]}",
                        category=sig.category, severity=sig.severity,
                        line_number=line_no, matched_text=m.group(0),
                        confidence=sig.confidence,
                        suggestions=self._suggest(sig, stripped),
                        generic_fix=sig.generic_fix,
                        context_hint=sig.context_hint,
                        match_start=m.start(),
                        match_end=m.end(),
                        configured=sig.configured,
                    ))

        # Resolve only mechanically subsumed interpretations.  Preserve the
        # highest severity from every suppressed finding so this presentation
        # cleanup cannot change whether the runtime hook injects the failure.
        findings_by_line: dict[int | None, list[Finding]] = {}
        for finding in findings:
            findings_by_line.setdefault(finding.line_number, []).append(finding)

        survivors: list[Finding] = []
        for line_findings in findings_by_line.values():
            for finding in line_findings:
                if any(
                    other is not finding and self._dominates(other, finding)
                    for other in line_findings
                ):
                    continue
                dominated = [
                    other for other in line_findings
                    if other is not finding and self._dominates(finding, other)
                ]
                finding.severity = max(
                    (item.severity for item in [finding, *dominated]),
                    key=self._severity_rank,
                )
                survivors.append(finding)
        findings = survivors

        # One regex contributes at most one finding per line.  Distinct rules
        # that survive the explicit dominance policy are distinct evidence;
        # in particular, equal-span actionable interpretations are preserved.
        unique = findings

        stats = {
            "total_lines": len(lines), "findings_count": len(unique),
            "critical_count": sum(1 for f in unique if f.severity == Severity.CRITICAL),
            "error_count": sum(1 for f in unique if f.severity == Severity.ERROR),
            "warning_count": sum(1 for f in unique if f.severity == Severity.WARNING),
        }

        parts = []
        if stats["critical_count"]: parts.append(f"{stats['critical_count']} 个严重问题")
        if stats["error_count"]: parts.append(f"{stats['error_count']} 个错误")
        if stats["warning_count"]: parts.append(f"{stats['warning_count']} 个警告")
        summary = f"发现 {'，'.join(parts)}" if parts else "未发现明显错误"

        return {
            "source": source, "summary": summary,
            "findings": [{
                "message": f.message, "category": f.category.value,
                "severity": f.severity.value, "line": f.line_number,
                "confidence": f.confidence, "suggestions": f.suggestions,
                "generic_fix": f.generic_fix,
                "context_hint": f.context_hint,
                "configured": f.configured,
            } for f in unique],
            "statistics": stats,
        }

    @staticmethod
    def _actionable_configured(finding: Finding) -> bool:
        return finding.configured and bool(
            finding.generic_fix or finding.context_hint
        )

    @staticmethod
    def _severity_rank(severity: Severity) -> int:
        return {
            Severity.INFO: 0,
            Severity.WARNING: 1,
            Severity.ERROR: 2,
            Severity.CRITICAL: 3,
        }[severity]

    @classmethod
    def _dominates(cls, candidate: Finding, target: Finding) -> bool:
        """Return whether a specific interpretation subsumes a generic one.

        Coverage dominance is defined mechanically: both findings are on the
        same line and the specific finding's match interval *strictly
        contains* the generic interval.  The containing rule must be a
        configured actionable rule whose confidence is no lower.  Category,
        rule name, and message length are never used.  Different lines,
        partially overlapping intervals, and disjoint intervals are kept.

        One explicit equal-interval tie rule exists: configured actionable
        guidance replaces an otherwise non-actionable interpretation of the
        exact same text.  If both or neither are actionable, both are kept.
        """
        if candidate.line_number != target.line_number:
            return False
        if candidate.match_start > target.match_start:
            return False
        if candidate.match_end < target.match_end:
            return False

        same_span = (
            candidate.match_start == target.match_start
            and candidate.match_end == target.match_end
        )
        candidate_actionable = cls._actionable_configured(candidate)
        target_actionable = cls._actionable_configured(target)
        if same_span:
            return candidate_actionable and not target_actionable

        return (
            candidate_actionable
            and candidate.confidence >= target.confidence
        )

    def _suggest(self, sig: ErrorPattern, _line: str) -> list[str]:
        """根据错误类别和 YAML 提示生成修复建议"""
        cat = sig.category
        suggestions = []

        # 基础建议
        if cat == Category.MEMORY:
            suggestions = ["检查系统可用内存", "减少并行进程数", "优化内存使用或分块处理"]
        elif cat == Category.MPI:
            if "ucx" in _line.lower() or "hcoll" in _line.lower():
                suggestions = [
                    "确认 launcher 和 libmpi 来自同一 Open MPI/HPC-X 环境",
                    "若受管作业健康检查给出 retry_recipe，使用其 --mca pml ob1 --mca btl tcp,self --mca coll_hcoll_enable 0 配方重投一次",
                    "不得覆盖已有 --mca 设置；新的真实提交仍需人工确认",
                ]
            else:
                suggestions = ["检查 MPI 环境配置", "确认进程数不超过可用核心数", "检查网络/通信配置"]
        elif cat == Category.INPUT:
            suggestions = ["检查输入文件格式", "对照文档验证参数", "检查数值范围是否合理"]
        elif cat == Category.DEPENDENCY:
            suggestions = ["安装缺失的包", "检查 Python 版本兼容性", "确认虚拟环境激活状态"]
        elif cat == Category.COMPILATION:
            suggestions = ["检查编译器版本和标志", "确认依赖库已安装", "查看完整编译日志"]
        elif cat == Category.LINKING:
            suggestions = ["确认库已安装且链接顺序正确", "检查 LD_LIBRARY_PATH / -L 路径", "查看完整链接日志"]
        elif cat == Category.ENVIRONMENT:
            suggestions = ["检查 PATH 环境变量", "确认工具已正确安装", "检查文件权限"]
        elif cat == Category.RUNTIME and ("segfault" in _line.lower() or "segmentation" in _line.lower()):
            suggestions = ["检查数组/指针越界", "确认输入数据格式正确", "检查内存访问模式"]
        elif cat == Category.RUNTIME:
            suggestions = ["检查运行参数", "查看详细日志", "尝试简化输入测试"]
        else:
            suggestions = ["查看相关文档，搜索错误信息"]

        # YAML owns the rule-specific remediation. Keep category-level
        # suggestions as context, but put the configured fix first.
        if sig.generic_fix:
            suggestions.insert(0, sig.generic_fix)

        # 添加上下文提示（来自 YAML）
        if sig.context_hint:
            suggestions.append(f"提示: {sig.context_hint}")

        return suggestions


# ════════════════════════════════════════════
# 源码修复器
# ════════════════════════════════════════════

class SourceFixer:
    """通用源码模式替换器

    领域知识（搜什么、改成什么）由 SKILL.md 提供。
    这里只提供：扫描 → 预览 diff → 备份 → 替换 → 报告 的安全流水线。
    """

    def __init__(self, backup_dir: str | None = None):
        self.patterns: list[FixPattern] = []
        self._backup_dir = backup_dir

    @staticmethod
    def _env_roots(name: str) -> list[Path]:
        try:
            values = json.loads(os.environ.get(name, "[]"))
        except Exception:
            return []
        if not isinstance(values, list):
            return []
        return [
            Path(str(value)).expanduser().resolve(strict=False)
            for value in values if isinstance(value, str) and value.strip()
        ]

    @staticmethod
    def _under(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def _authorized_worktree(self, root_dir: str) -> tuple[bool, str]:
        target = Path(root_dir).expanduser().resolve(strict=False)
        roots = self._env_roots("EXPERIMENT_SOURCE_WORKTREE_ROOTS")
        if any(self._under(target, root) for root in roots):
            return True, ""
        return False, (
            "实际源码写入被阻止：目标不在当前运行声明的 "
            "source_worktree_root 内；source_baseline_root 永远不可写。"
        )

    def _record_root(self) -> Path | None:
        if self._backup_dir:
            return Path(self._backup_dir).expanduser().resolve(strict=False)
        patch_roots = self._env_roots("EXPERIMENT_SOURCE_PATCH_ROOTS")
        if patch_roots:
            return patch_roots[0]
        repro_root = os.environ.get("EXPERIMENT_REPRO_ROOT")
        if repro_root:
            return Path(repro_root).expanduser().resolve(strict=False) / "source_patches"
        return None

    def add_pattern(self, *, name: str, search: str, replace: str,
                    file_glob: str = "*", description: str = "",
                    auto_apply: bool = False) -> None:
        """注册一个修复模式"""
        self.patterns.append(FixPattern(
            name=name, search=search, replace=replace,
            file_glob=file_glob, description=description,
            auto_apply=auto_apply,
        ))

    def scan(self, root_dir: str, pattern_filter: list[str] | None = None) -> list[dict]:
        """扫描目录，返回所有匹配（不修改文件）"""
        results = []
        for fp in self.patterns:
            if pattern_filter and fp.name not in pattern_filter:
                continue
            regex = re.compile(fp.search, re.IGNORECASE)
            for fpath in Path(root_dir).rglob(fp.file_glob):
                if not fpath.is_file():
                    continue
                try:
                    content = fpath.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                matches = list(regex.finditer(content))
                if matches:
                    results.append({
                        "file": str(fpath), "pattern": fp.name,
                        "matches": len(matches), "description": fp.description,
                        "auto_apply": fp.auto_apply,
                        "preview": [{
                            "line": content[:m.start()].count('\n') + 1,
                            "match": m.group(0),
                        } for m in matches[:5]],  # 最多显示前5个
                    })
        return results

    def apply(self, root_dir: str, dry_run: bool = True,
              pattern_filter: list[str] | None = None,
              auto_only: bool = False) -> dict:
        """应用修复。dry_run=True 只预览不修改。"""
        authorized, role_error = self._authorized_worktree(root_dir)
        if not dry_run and (
                os.environ.get("HARNESS_ALLOW_SOURCE_WRITE") != "1"
                or not authorized):
            return {
                "summary": {"total_files": 0, "total_changes": 0, "failed": 1},
                "results": [{
                    "file": str(root_dir),
                    "pattern": "<all>",
                    "changes": 0,
                    "ok": False,
                    "message": (
                        role_error or
                        "实际源码写入被阻止：需要由进程 owner 显式设置 "
                        "HARNESS_ALLOW_SOURCE_WRITE=1；当前只允许 dry_run 预览。"
                    ),
                }],
                "dry_run": False,
            }
        results = []
        for fp in self.patterns:
            if pattern_filter and fp.name not in pattern_filter:
                continue
            if auto_only and not fp.auto_apply:
                continue

            regex = re.compile(fp.search, re.IGNORECASE)
            for fpath in Path(root_dir).rglob(fp.file_glob):
                if not fpath.is_file():
                    continue
                try:
                    original = fpath.read_text(encoding="utf-8", errors="ignore")
                except Exception as e:
                    results.append({"file": str(fpath), "pattern": fp.name,
                                    "changes": 0, "ok": False, "message": str(e)})
                    continue

                new_content, count = regex.subn(fp.replace, original)
                if count == 0:
                    continue

                if dry_run:
                    results.append({"file": str(fpath), "pattern": fp.name,
                                    "changes": count, "ok": True,
                                    "message": f"[DRY RUN] 将修改 {count} 处"})
                    continue

                # 实际修复：备份 → 写入
                try:
                    record_root = self._record_root()
                    if record_root is None:
                        raise RuntimeError(
                            "缺少 patch/diff 记录位置：声明 source_patch_root，"
                            "或由 safe_execute_python 提供 EXPERIMENT_REPRO_ROOT")
                    record_root.mkdir(parents=True, exist_ok=True)
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    rel_tag = hashlib.sha256(
                        str(fpath.resolve()).encode("utf-8")).hexdigest()[:10]
                    backup_path = record_root / f"{fpath.name}.{rel_tag}.{ts}.bak"
                    shutil.copy2(fpath, backup_path)

                    fpath.write_text(new_content, encoding="utf-8")
                    diff = "".join(difflib.unified_diff(
                        original.splitlines(keepends=True),
                        new_content.splitlines(keepends=True),
                        fromfile=str(fpath), tofile=str(fpath),
                    ))
                    diff_path = record_root / f"{fpath.name}.{rel_tag}.{ts}.patch"
                    diff_path.write_text(diff, encoding="utf-8")
                    results.append({"file": str(fpath), "pattern": fp.name,
                                    "changes": count, "ok": True,
                                    "message": (
                                        f"已修复 {count} 处，备份: {backup_path}，"
                                        f"patch: {diff_path}")})
                except Exception as e:
                    results.append({"file": str(fpath), "pattern": fp.name,
                                    "changes": 0, "ok": False, "message": str(e)})

        total = sum(r["changes"] for r in results if r["ok"])
        failed = [r for r in results if not r["ok"]]
        return {
            "summary": {"total_files": len(set(r["file"] for r in results)),
                        "total_changes": total, "failed": len(failed)},
            "results": results,
            "dry_run": dry_run,
        }


# ════════════════════════════════════════════
# 便捷函数（供 execute_python 调用）
# ════════════════════════════════════════════

def analyze_log(path: str, use_yaml: bool = True) -> dict:
    """分析日志文件

    Args:
        path: 日志文件路径
        use_yaml: 是否加载 diagnose_patterns/*.yaml 中的模式
    """
    if use_yaml:
        return DiagnoseEngine.from_yaml_patterns().analyze_log(path)
    return DiagnoseEngine().analyze_log(path)


def analyze_text(text: str, source: str = "text", use_yaml: bool = True) -> dict:
    """分析文本

    Args:
        text: 要分析的文本
        source: 来源标识
        use_yaml: 是否加载 diagnose_patterns/*.yaml 中的模式
    """
    if use_yaml:
        return DiagnoseEngine.from_yaml_patterns().analyze_output(text, source)
    return DiagnoseEngine().analyze_output(text, source)


def scan_source(root: str, patterns: list[dict]) -> list[dict]:
    """用给定模式扫描源码目录

    patterns 格式: [{"name": "...", "search": "regex", "replace": "...",
                      "file_glob": "*.F90", "description": "...", "auto_apply": bool}]
    """
    fixer = SourceFixer()
    for p in patterns:
        fixer.add_pattern(**p)
    return fixer.scan(root)


def fix_source(root: str, patterns: list[dict], dry_run: bool = True, auto_only: bool = False) -> dict:
    """用给定模式修复源码目录"""
    fixer = SourceFixer()
    for p in patterns:
        fixer.add_pattern(**p)
    return fixer.apply(root, dry_run=dry_run, auto_only=auto_only)


def get_pattern_stats() -> dict | None:
    """获取诊断模式库统计信息"""
    yaml_file = Path(__file__).parent / "diagnose_patterns" / "diagnose_patterns.yaml"
    if not yaml_file.exists():
        return None

    try:
        with open(yaml_file, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        patterns = data.get("patterns", [])
        categories = {}
        for p in patterns:
            cat = p.get("category", "unknown")
            categories[cat] = categories.get(cat, 0) + 1

        return {
            "description": data.get("description", ""),
            "schema_version": data.get("schema_version", ""),
            "total_patterns": len(patterns),
            "categories": categories
        }
    except Exception as e:
        return {"error": str(e)}


# ════════════════════════════════════════════
# 闭环1：错误聚焦 + 多维故障指纹（消费 safe_bash 落盘的完整日志）
# ════════════════════════════════════════════

# 高价值错误行（广义候选）
_HIGH_VALUE_ERR = re.compile(
    r"(error:|error #\d+|fatal error|undefined reference|cannot find -l|"
    r"cannot open module|no such file|segmentation fault|corrupt or old module|"
    r"catastrophic|undefined symbol|permission denied|no rule to make|"
    r"ld returned|relocation truncated|recipe for target.*failed|"
    r"nvfortran-[sf]-|cmake error|configure:\s*error)", re.IGNORECASE)

# 强错误：即使落在 expected-failure 行也不该被过滤掉（防漏真失败）
_STRONG_ERROR = re.compile(
    r"(fatal error|configure:\s*error|cmake error|undefined reference|"
    r"cannot open module|corrupt or old module|segmentation fault|"
    r"nvfortran-[sf]-|error #\d+|undefined symbol|cannot find -l)", re.IGNORECASE)

# 预期失败/探针/噪声（仅在非强错误时才过滤）
_EXPECTED_FAIL = re.compile(
    r"(check for working|detecting .* compiler|performing test|-- check|"
    r"compiler probe|try_compile|\b0 errors?\b|\bno errors?\b|"
    r"expected to fail|expected failure|previously reported|see above)", re.IGNORECASE)
_WARN_ONLY = re.compile(r"^\s*(warning|note|remark|info)\s*[:：]", re.IGNORECASE)

_MAX_SCAN_BYTES = 2_000_000   # >2MB 日志用 head+tail 窗口（各 1MB），不只扫尾部


def _err_confidence(line: str) -> str:
    """单行错误的置信度分级（high/medium/low）。"""
    l = line.lower()
    if re.search(r"(fatal error|corrupt or old module|cannot open module|"
                 r"undefined reference|cmake error|configure:\s*error|nvfortran-f-)", l):
        return "high"
    if re.search(r"(error:|error #\d+|nvfortran-s-|undefined symbol|cannot find -l)", l):
        return "medium"
    return "low"   # make target failed / ld returned 等汇总性错误


def focus_error(*, text: str | None = None, log_path: str | None = None,
                context: int = 8) -> dict:
    """提取首个高价值错误段 + 候选 + 置信度。text 与 log_path 互斥（显式接口）。

    大日志（>2MB）用 head+tail 窗口（各 1MB），并标 scan_strategy/scan_incomplete，
    不在只扫尾部时谎称"首个错误"。强错误优先于 expected-failure 过滤（防漏真失败）。
    """
    if (text is None) == (log_path is None):
        raise ValueError("focus_error: text 与 log_path 必须二选一（互斥）")

    scan_strategy = "full"
    scan_incomplete = False
    if log_path is not None:
        p = Path(log_path)
        if not p.exists():
            return {"primary_error": None, "primary_confidence": "none", "alternative_count": 0,
                    "focus": "", "later_high_value_error_count": 0, "cascade_possible": False,
                    "scan_strategy": "none", "scan_incomplete": False, "bytes_scanned": 0,
                    "log_size": 0, "error": "log_path 不存在"}
        log_size = p.stat().st_size
        if log_size > _MAX_SCAN_BYTES:
            half = _MAX_SCAN_BYTES // 2
            with open(p, "rb") as f:
                head = f.read(half)
                f.seek(-half, os.SEEK_END)
                tail = f.read(half)
            text = (head.decode("utf-8", errors="replace")
                    + "\n...[middle truncated]...\n"
                    + tail.decode("utf-8", errors="replace"))
            scan_strategy = "head+tail"
            scan_incomplete = True
            bytes_scanned = len(head) + len(tail)
        else:
            text = p.read_text(encoding="utf-8", errors="replace")
            bytes_scanned = log_size
    else:
        bytes_scanned = len(text.encode("utf-8"))
        log_size = bytes_scanned

    lines = text.splitlines()
    # 候选 = 高价值错误，且（非预期失败 或 是强错误）、非纯 warning
    candidates = [
        i for i, l in enumerate(lines)
        if _HIGH_VALUE_ERR.search(l)
        and (not _EXPECTED_FAIL.search(l) or _STRONG_ERROR.search(l))
        and not _WARN_ONLY.match(l)
    ]
    if not candidates:
        return {"primary_error": None, "primary_confidence": "none", "alternative_count": 0,
                "focus": "\n".join(lines[-context:]), "later_high_value_error_count": 0,
                "cascade_possible": False, "scan_strategy": scan_strategy,
                "scan_incomplete": scan_incomplete, "bytes_scanned": bytes_scanned,
                "log_size": log_size}
    first = candidates[0]
    seg = lines[max(0, first - 2): first + context]
    later = len([c for c in candidates if c > first])
    return {
        "primary_error": lines[first].strip()[:200],
        "primary_confidence": _err_confidence(lines[first]),
        "alternative_count": len(candidates) - 1,
        "focus": "\n".join(seg),
        "later_high_value_error_count": later,   # 客观计数
        "cascade_possible": later > 0,           # 仅提示，不触发硬性路线切换
        "scan_strategy": scan_strategy,
        "scan_incomplete": scan_incomplete,
        "bytes_scanned": bytes_scanned,
        "log_size": log_size,
    }


_FINGERPRINT_VERSION = "1.0"
# 稳定错误类型：medium 置信度下也可硬判重（错误标识稳定，不易误判）
_STABLE_TYPES = {"module_incompat", "module_missing", "missing_lib", "link_undefined"}
# 常见编译器（未识别时不强断言）
_COMPILERS = ("nvfortran", "nvc++", "nvc", "ifx", "ifort", "icx", "icpx",
              "gfortran", "g++", "gcc", "flang", "amdflang", "clang++", "clang")
_MPI_WRAPPERS = ("mpiifx", "mpiifort", "mpif90", "mpifort", "mpicxx", "mpic++",
                 "mpicc", "mpiexec", "mpirun")
_BUILD_TOOLS = ("cmake", "ninja", "make", "configure")


def _contains_tool(command: str, tool: str) -> bool:
    """工具名边界匹配，正确处理 nvc++/g++/clang++（\\b 对 + 不可靠）。"""
    return re.search(rf"(?<![\w.+\-]){re.escape(tool)}(?![\w.+\-])", command) is not None


def fault_fingerprint(cmd: str, error_text: str, stage: str | None = None) -> dict:
    """多维故障指纹。信息不足 → strict_key/family_key=None + confidence=low（不硬判重）。

    - compiler / mpi_wrapper / build_tool 各自独立维度（不折叠），strict_key 全保留
    - source 保留 source_basename + source_path_tail（末 3 级，防同名 utils.F90 误聚）
    - module/symbol 提取在空白规范化文本上做（跨行）；source 从原始文本提取
    """
    e = error_text or ""
    el = e.lower()
    e_norm = re.sub(r"\s+", " ", e)   # 规范化空白：处理"pio.mod 在下一行"等跨行情况
    cl = (cmd or "").lower()

    # source 位置（从原始文本，保留路径末段）
    m = re.search(r"([\w./\-]+\.(?:F90|f90|F|f|c|cpp|cc|cxx|h|hpp|cu))[:\(](\d+)", e)
    source_basename = source_path_tail = source_line = None
    if m:
        full = m.group(1)
        parts = full.split("/")
        source_basename = parts[-1]
        source_path_tail = "/".join(parts[-3:])
        source_line = m.group(2)

    # symbol：undefined ref / lib / undefined symbol（规范化文本，跨行）
    ms = re.search(r"undefined reference to [`']?([\w:]+)|cannot find -l([\w.]+)|"
                   r"undefined symbol:?\s*([\w:]+)", e_norm, re.IGNORECASE)
    symbol = next((g for g in (ms.groups() if ms else []) if g), None)
    # module：捕获完整路径再取 basename（兼容路径中的点/斜杠/连字符、错误提示与路径跨行）
    if symbol is None:
        mm = re.search(r"(?:corrupt or old module file|cannot open module file)\s+"
                       r"[`']?([^\s`']+\.mod)", e_norm, re.IGNORECASE)
        if mm:
            symbol = mm.group(1).split("/")[-1]   # basename

    # 工具识别用 _contains_tool（正确处理 ++）。注意 _COMPILERS 顺序：nvc++ 在 nvc 前。
    # mpiifx 等只识别为 wrapper，不据此推断底层 compiler（应来自平台画像）。
    compiler = next((c for c in _COMPILERS if _contains_tool(cl, c)), None)
    mpi_wrapper = next((w for w in _MPI_WRAPPERS if _contains_tool(cl, w)), None)
    build_tool = next((b for b in _BUILD_TOOLS if _contains_tool(cl, b)), None)

    if "undefined reference" in el or "undefined symbol" in el:
        etype = "link_undefined"
    elif "corrupt or old module" in el:
        etype = "module_incompat"
    elif "cannot open module" in el:
        etype = "module_missing"
    elif "cannot find -l" in el:
        etype = "missing_lib"
    elif "no such file" in el:
        etype = "missing_file"
    elif "no rule to make" in el or "recipe for target" in el:
        etype = "make_target"
    elif "fatal error" in el or "error:" in el or "error #" in el or "nvfortran-" in el:
        etype = "compile_error"
    elif "segmentation fault" in el:
        etype = "segfault"
    else:
        etype = "unknown"

    base = {
        "fingerprint_version": _FINGERPRINT_VERSION, "error_type": etype,
        "compiler": compiler, "mpi_wrapper": mpi_wrapper, "build_tool": build_tool,
        "source_basename": source_basename, "source_path_tail": source_path_tail,
        "source_line": source_line, "symbol": symbol, "stage": stage,
    }
    # 信息不足 → 不生成硬判重 key
    if etype == "unknown" or (source_basename is None and symbol is None):
        return {**base, "strict_key": None, "family_key": None, "confidence": "low"}

    # strict_key 保留全部独立工具维度（不折叠）+ 路径末段（防同名误聚）
    strict_key = (f"{stage or '?'}|c={compiler or '?'}|w={mpi_wrapper or '?'}|"
                  f"b={build_tool or '?'}|{etype}|{source_path_tail or '?'}|{symbol or '?'}")
    family_key = f"{etype}|{symbol or source_basename}"
    # 置信度：稳定类型+symbol → high；有 symbol 或 source → medium；粗 compile_error 无 symbol → low
    if symbol and etype in _STABLE_TYPES:
        confidence = "high"
    elif etype == "compile_error" and not symbol:
        confidence = "low"   # 粗粒度编译错误仅凭文件名 → 不硬判重
    elif symbol or source_basename:
        confidence = "medium"
    else:
        confidence = "low"
    return {**base, "strict_key": strict_key, "family_key": family_key, "confidence": confidence}


def is_hard_repeat_eligible(fp: dict) -> bool:
    """该指纹是否够格进入【硬判重】（推进 repeated_error 的 tier 升级）。

    - high confidence → 可硬判重
    - medium 仅限稳定类型（module_incompat/module_missing/missing_lib/link_undefined）且有 symbol/source
    - 普通 compile_error 不得仅凭文件名硬判重；low/无 strict_key → 只"疑似重复"提示
    """
    if not fp or not fp.get("strict_key"):
        return False
    conf = fp.get("confidence")
    if conf == "high":
        return True
    if conf == "medium" and fp.get("error_type") in _STABLE_TYPES \
            and (fp.get("symbol") or fp.get("source_basename")):
        return True
    return False


# ════════════════════════════════════════════
# 049-3：配置规则的修法投影（给工具结果与受管作业 health 用）
# ════════════════════════════════════════════
_GUIDANCE_SEVERITY_RANK = {"critical": 3, "error": 2, "warning": 1, "info": 0}


def configured_guidance(text: str, *, limit: int = 3, source: str = "tool output",
                        engine: "DiagnoseEngine | None" = None) -> list[dict]:
    """把 YAML 规则里带修法的命中压成给模型看的几条：一行一条、一条规则一次、按严重度排。

    只收 configured 且带 generic_fix / context_hint 的命中——内置规则只有分类没有
    修法，模型从 stderr 里自己就能看到，不值一条。同一行上有多条配置规则命中时
    （`_analyze` 对同跨度的两条都保留，例如 generic_compile_error 与一条整行的
    应用签名），这里只取置信度最高的那条：投影给模型看的是最具体的解释。
    纯信息：调用方不得拿它判定成败。
    """
    if not text or not text.strip():
        return []
    try:
        report = (engine or DiagnoseEngine.from_yaml_patterns()).analyze_output(text, source)
    except Exception as exc:  # 诊断永远不能让工具结果失败
        log.warning("configured_guidance 失败，跳过: %s", exc)
        return []
    actionable = [
        f for f in report.get("findings", [])
        if f.get("configured") and (f.get("generic_fix") or f.get("context_hint"))
    ]
    best_per_line: dict[int | None, dict] = {}
    for finding in actionable:
        current = best_per_line.get(finding.get("line"))
        if current is None or float(finding.get("confidence") or 0) > float(current.get("confidence") or 0):
            best_per_line[finding.get("line")] = finding
    seen: set[tuple[str, str]] = set()
    picked: list[dict] = []
    findings = sorted(
        best_per_line.values(),
        key=lambda f: (-_GUIDANCE_SEVERITY_RANK.get(str(f.get("severity")), 0), f.get("line") or 0),
    )
    for finding in findings:
        fix = str(finding.get("generic_fix") or "")
        context = str(finding.get("context_hint") or "")
        key = (fix, context)
        if key in seen:
            continue
        seen.add(key)
        picked.append({
            "category": finding.get("category"),
            "severity": finding.get("severity"),
            "line": finding.get("line"),
            "matched": " ".join(str(finding.get("message") or "").split())[:200],
            "fix": fix,
            "context": context,
        })
        if len(picked) >= limit:
            break
    return picked
