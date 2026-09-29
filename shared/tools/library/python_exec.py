"""Execute Python in the same mandatory container boundary as shell tools."""
from __future__ import annotations

import json
import sys

import tempfile
from pathlib import Path
from typing import Any

from core.state import State
from core.tool_registry import ToolDefinition, register_tool
from shared.lib.cancellable_subprocess import spawn_and_wait, the_interpreter_for_model_code


async def _unsatisfied_in_sandbox(
    requirements: list[str], state: State, dependency_root: Path, *, timeout: int
) -> list[str]:
    """这些声明里，**代码真正要跑的那个解释器**仍然满足不了的那些。

    2026-09-17 实测：postprocess 的 TikZ 后端声明 pypdfium2>=4.0，包在解释器里
    装着（5.13.0，能 import），但那个 venv 没有 pip —— 于是「装不上」被当成
    「没有」，铸 figure 记录被拒，agent 花一整轮查环境最后 report_blocker。

    判据要问对对象。而且要问**代码将来会看到的那份真相**：同一个解释器、同一个
    PYTHONPATH。拿宿主的 site-packages 当答案，等于换个镜像就答错
    （见 test_requirements_are_resolved_inside_the_image…）。
    """

    probe = (
        "import json,sys\n"
        "from importlib.metadata import distributions\n"
        "from packaging.requirements import Requirement, InvalidRequirement\n"
        "from packaging.utils import canonicalize_name\n"
        "have={}\n"
        "for d in distributions():\n"
        "    n=(d.metadata or {}).get('Name'); v=getattr(d,'version',None)\n"
        "    if n and v: have.setdefault(canonicalize_name(n), v)\n"
        "miss=[]\n"
        f"for item in {requirements!r}:\n"
        "    try: r=Requirement(item)\n"
        "    except InvalidRequirement: miss.append(item); continue\n"
        "    if r.marker is not None and not r.marker.evaluate(): continue\n"
        "    got=have.get(canonicalize_name(r.name))\n"
        "    if got is None or not r.specifier.contains(got, prereleases=True):\n"
        "        miss.append(item)\n"
        "print(json.dumps(miss))\n"
    )
    try:
        status, _rc, out_b, _err_b = await spawn_and_wait(
            the_interpreter_for_model_code(), "-c", probe, state=state,
            timeout=min(timeout, 60),
            sandbox_environment=(
                {"PYTHONPATH": str(dependency_root)} if dependency_root.exists() else None
            ),
        )
        if status != "done":
            return list(requirements)
        return json.loads(out_b.decode("utf-8", errors="replace").strip().splitlines()[-1])
    except Exception:
        # 问不出来就按「还缺」处理 —— 保守，不会把真的缺件放过去。
        return list(requirements)


async def _install_requirements(requirements: list[str], state: State, target: Path) -> dict | None:
    """Install run-local dependencies without ever mutating the trusted image."""
    from packaging.requirements import InvalidRequirement, Requirement

    invalid: list[Any] = []
    for item in requirements:
        if not isinstance(item, str) or not item.strip() or item.lstrip().startswith("-") or "\x00" in item:
            invalid.append(item)
            continue
        try:
            parsed = Requirement(item)
        except InvalidRequirement:
            invalid.append(item)
            continue
        if parsed.url is not None:
            invalid.append(item)
    if invalid:
        return {"status": "error", "error": "requirements 只能包含包名/版本约束，不能传 pip 参数、URL 或本地路径",
                "invalid": [str(item)[:120] for item in invalid]}

    target.mkdir(parents=True, exist_ok=True)
    from core.sandbox import SandboxLimits

    install_limits = SandboxLimits(
        memory_bytes=2 * 1024**3,
        cpus=2,
        pids=64,
        walltime_seconds=300,
        storage_bytes=2 * 1024**3,
        output_bytes=4 * 1024**2,
    )
    # Keep the transient wheelhouse inside the run's already-frozen mount.
    # The networked downloader still sees only this empty directory; the
    # subsequent offline install can read it without widening the Attempt.
    with tempfile.TemporaryDirectory(
        prefix="harness-wheelhouse-", dir=target.parent
    ) as temporary:
        wheelhouse = Path(temporary).resolve()
        # The only networked sandbox sees an empty wheelhouse.  It cannot read
        # project/run data, so a compromised package index has nothing to
        # exfiltrate.  Only binary wheels are accepted; no source build hooks.
        status, rc, _, stderr_b = await spawn_and_wait(
            sys.executable, "-m", "pip", "download", "--quiet", "--no-cache-dir",
            "--index-url", "https://pypi.org/simple", "--only-binary=:all:",
            "--dest", str(wheelhouse), *requirements,
            state=state,
            timeout=300,
            cwd=str(wheelhouse),
            writable_roots=[wheelhouse],
            readonly_roots=[],
            sandbox_limits=install_limits,
            network_access=True,
        )
        stderr = stderr_b.decode("utf-8", errors="replace")
        if status == "done" and rc == 0:
            status, rc, _, stderr_b = await spawn_and_wait(
                sys.executable, "-m", "pip", "install", "--quiet", "--upgrade",
                "--no-index", "--find-links", str(wheelhouse),
                "--target", str(target), *requirements,
                state=state,
                timeout=300,
                cwd=str(target),
                writable_roots=[target],
                readonly_roots=[wheelhouse],
                sandbox_limits=install_limits,
                network_access=False,
            )
            stderr = stderr_b.decode("utf-8", errors="replace")
    if status == "timeout":
        return {"status": "error", "error": "依赖安装超过 5 分钟", "missing": requirements}
    if status == "spawn_failed":
        return {"status": "error", "error": f"沙盒装包器启动失败：{stderr[:500]}",
                "missing": requirements}
    if rc == 0:
        return None
    return {
        "status": "error",
        "error": f"装不上这些依赖：{', '.join(requirements)}。",
        "missing": requirements,
        "installer_error": stderr[-1200:],
    }


_MPL_HINT = ("matplotlib", "pyplot", "plt.", "pylab", "seaborn", "sns.")


def _with_matplotlib_font_preamble(code: str) -> str:
    """代码用到 matplotlib 时，前置设好 CJK 字体栈；否则原样返回。

    ## 根因（E2E v27/v28/v29）

    模型自己写 `execute_python` 脚本画图，用的是 matplotlib 默认字体 `DejaVu Sans`
    —— 它**没有 CJK 字形**，中文标题/轴标签全渲染成方块（v27/v28 实测）。
    postprocess 自己的画图工具（render.py / v2/rendering.py）会先设
    `rcParams["font.sans-serif"] = sans_stack()`，但模型手写脚本不走那条路。

    v29 里模型靠**每轮跑一段字体搜索脚本自愈**才把中文画对 —— 但那不跨 run、
    每次重来，还触发了高危审批（subprocess 找字体）。把默认应用挪到环境层：
    任何用到 matplotlib 的脚本自动拿到 CJK 栈，不必自愈。

    做法：只在代码提到 matplotlib 时前置（不强制非画图代码 import matplotlib、不
    拖慢它们）；设的是**默认**，用户代码后面自己设 font 会覆盖（尊重显式选择）。
    字体名单取 `fonts.PREFERRED_ORDER`（单一真相源），matplotlib 自动选中本机装了
    的第一个；一个都没装（Linux 未装 CJK 字体）就落到 DejaVu —— 那是字体**安装**
    问题，与本注入正交（部署机需 `fonts-noto-cjk`）。
    """
    if not any(h in code for h in _MPL_HINT):
        return code
    try:
        from nodes.postprocess.fonts import LATIN_FALLBACK, PREFERRED_ORDER
        families = [*PREFERRED_ORDER, LATIN_FALLBACK]
    except Exception:
        # fonts.py 读不到时的自足兜底（保序、含拉丁兜底）。
        families = ["Noto Sans CJK SC", "Source Han Sans SC", "WenQuanYi Zen Hei",
                    "PingFang SC", "Microsoft YaHei", "SimHei", "Arial Unicode MS",
                    "DejaVu Sans"]
    preamble = (
        "try:\n"
        "    import matplotlib as _hf_mpl\n"
        "    _hf_mpl.use('Agg')\n"
        "    import matplotlib.pyplot as _hf_plt\n"
        f"    _hf_plt.rcParams['font.sans-serif'] = {families!r}\n"
        "    _hf_plt.rcParams['axes.unicode_minus'] = False\n"
        "except Exception:\n"
        "    pass\n"
    )
    return preamble + code


import re as _re

# matplotlib 缺字形时打的两种话：
#   "Glyph 20013 (\N{CJK UNIFIED IDEOGRAPH-4E2D}) missing from font(s) DejaVu Sans."
#   "findfont: Font family 'X' not found."
_MISSING_GLYPH_RE = _re.compile(
    r"Glyph\s+\d+\b[^\n]*missing from (?:current )?font", _re.IGNORECASE)
_FINDFONT_FALLBACK_RE = _re.compile(
    r"findfont:[^\n]*(?:not found|Falling back)", _re.IGNORECASE)


def _missing_glyph_summary(output: str) -> str | None:
    """输出里有没有豆腐块信号；有则给一句醒目、可操作的告警，否则 None。

    只看信号存在与否 + 数一下条数，不逐条堆（一张中文图能刷几百条同样的行）。
    """
    if not output:
        return None
    n_glyph = len(_MISSING_GLYPH_RE.findall(output))
    findfont = bool(_FINDFONT_FALLBACK_RE.search(output))
    if not n_glyph and not findfont:
        return None
    bits = []
    if n_glyph:
        bits.append(f"matplotlib 报了 {n_glyph} 处缺字形（Glyph … missing from font）")
    if findfont:
        bits.append("有 findfont 回退（点名的字体没找到）")
    return (
        "⚠️ 图里可能有豆腐块（方块字）：" + "；".join(bits) + "。"
        "多半是中文/特殊字符用了没有对应字形的字体。"
        "改法：画图前设 `plt.rcParams['font.sans-serif']` 到一个含 CJK 字形的字体"
        "（如 Noto Sans CJK SC / PingFang SC）、`plt.rcParams['axes.unicode_minus']=False`；"
        "本机没有任何 CJK 字体时联系平台装 fonts-noto-cjk。"
        "**别把带豆腐块的图交进论文** —— 先确认渲染正常再往下走。"
    )


async def _execute_python(
    state: State,
    code: str,
    timeout: int = 300,
    cwd: str | None = None,
    requirements: list[str] | None = None,
    **internal: Any,
) -> dict:
    _sandbox_writable_roots = internal.pop("_sandbox_writable_roots", None)
    _sandbox_readonly_roots = internal.pop("_sandbox_readonly_roots", None)
    resource_profile = internal.pop("resource_profile", None)
    # code 非空这一处**保留在函数体**而不搬进 schema：本函数被
    # nodes/experiment/tools/safe_bash._safe_execute_python 直调转发模型给的
    # code（不经 execute_python 的 parameters_schema），共享检查只能留在这里一份。
    if not code or not code.strip():
        return {"status": "error", "error": "code 不能为空"}

    # v3.2（2026-07）：框架级高危命令拦截（跟 shared/tools/builtin.py:_run_bash 同款）。
    from shared.lib import dangerous_commands as _dc

    # ── 第 0 道：越界写入 = 硬拒（deny，不问人、不受 /bypass 影响）─────────
    boundary = _dc.match_boundary_violation(code, mode="python")
    if boundary:
        state.append_transcript(
            "boundary_write_blocked", tool="execute_python",
            code_preview=code[:200], category=boundary)
        return {"status": "error",
                "error": _dc.BOUNDARY_DENY_MESSAGE.format(category=boundary)}

    category = _dc.match_high_risk(code, mode="python")
    if category:
        if _dc.bypass_enabled():
            state.append_transcript(
                "highrisk_python_bypass", code_preview=code[:200], category=category)
        elif _dc.is_confirmed(state, code):
            _dc.consume_confirmation(state, code)
            state.append_transcript(
                "highrisk_python_confirmed_run", code_preview=code[:200], category=category)
        else:
            state.append_transcript(
                "highrisk_python_blocked_pending_confirm",
                code_preview=code[:200], category=category)
            return _dc.build_pause_payload(
                state, tool="execute_python", text=code, category=category, preview=code)

    from core.project_workspace import validate_tool_cwd

    try:
        workspace = validate_tool_cwd(state, cwd)
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    workspace.mkdir(parents=True, exist_ok=True)

    # 声明了 requirements 就先装齐 —— 但**目的是"跑之前这些包能 import"**，
    # 不是"一定要跑一次装包器"。已经满足的直接跳过：否则解释器里缺个 pip
    # （uv 建的 venv 默认就不装 pip）会让每一次带 requirements 的调用都硬失败，
    # 哪怕那些包早就在。E2E v19 实测就栽在这上面：agent 声明 numpy（venv 里
    # 本来就有），拿到一句"pip install 失败"，只能绕道 safe_run_bash。
    dependency_root = Path(state.root) / ".harness" / "python-packages"
    if requirements:
        # 上面那段注释承诺「已经满足的直接跳过」，但代码从来没做过这件事 ——
        # 每一次带 requirements 的调用都照样去跑装包器。2026-09-17 实测：
        # postprocess 的 TikZ 后端声明 pypdfium2>=4.0，包在 venv 里装着
        # （5.13.0，能 import），但这个 venv 没有 pip，于是「装不上」→ 铸
        # figure 记录被拒，agent 花一整轮查环境最后 report_blocker。
        #
        # 判据要问对对象：「跑之前这些包能不能 import 到满足版本」，不是
        # 「装包器跑不跑得起来」。装不上不等于没有 —— 去**真正要跑代码的那个
        # 解释器**里问一句；宿主的 site-packages 说了不算（沙箱可能是另一个
        # 镜像，这条不变量由 test_requirements_are_resolved_inside_the_image
        # 守着）。
        failure = await _install_requirements(requirements, state, dependency_root)
        if failure is not None:
            still_missing = await _unsatisfied_in_sandbox(
                requirements, state, dependency_root, timeout=timeout
            )
            if still_missing:
                failure["missing"] = still_missing
                return failure

    # 用 matplotlib 画图时，前置一小段设好 CJK 字体栈 —— 否则中文标题/轴标签
    # 渲染成豆腐块。见下方 _with_matplotlib_font_preamble 的根因说明。
    code_to_run = _with_matplotlib_font_preamble(code)

    # 跑代码。等**进程退出**、输出落文件 —— 见 builtin.run_bash 同处注释：
    # 代码里 subprocess.Popen 起的后台进程会继承管道，接管道就永远等不到 EOF。
    # 模型代码关进写沙箱（同 run_bash）：本节点目录 + run-local + scratch 可写。
    from core.sandbox import limits_for_profile, model_tool_roots

    _writable, _readonly = (
        (_sandbox_writable_roots, _sandbox_readonly_roots or [])
        if _sandbox_writable_roots is not None
        else model_tool_roots(state)
    )
    status, rc, out_b, err_b = await spawn_and_wait(
        the_interpreter_for_model_code(), "-c", code_to_run, state=state, timeout=timeout,
        cwd=str(workspace), writable_roots=_writable, readonly_roots=_readonly,
        sandbox_environment={"PYTHONPATH": str(dependency_root)} if dependency_root.exists() else None,
        sandbox_limits=limits_for_profile(
            resource_profile, walltime_seconds=timeout
        ))
    out = out_b.decode("utf-8", errors="replace")
    err = err_b.decode("utf-8", errors="replace")
    if status == "spawn_failed":
        return {"status": "error", "error": f"启动 Python 失败：{err[:300]}"}
    if status == "timeout":
        result = {"status": "timeout", "timeout_s": timeout,
                  "stdout_tail": out[-3000:], "stderr_tail": err[-1500:],
                  "workspace": str(workspace), "safe_to_retry": False,
                  "retry_guidance": (
                      "The code may have partial writes. Inspect outputs before an explicit retry."
                  )}
        if "HARNESS_SANDBOX_LIMIT walltime" in err:
            result.update({
                "error_code": "sandbox_resource_exhausted",
                "resource": "walltime",
            })
        return result
    if status == "cancelled":
        return {
            "status": "cancelled",
            "returncode": rc,
            "stdout_tail": out[-3000:],
            "stderr_tail": err[-1500:],
            "workspace": str(workspace),
        }

    result = {
        "status": "success" if rc == 0 else "error",
        "returncode": rc,
        "stdout_tail": out[-3000:],
        "stderr_tail": err[-1500:],
        "workspace": str(workspace),
    }
    # 同 run_bash：envelope 里留一句给人看的失败原因，否则下游只剩
    # "Tool execution failed"（core/tool_errors.command_failure_note）。
    if rc != 0:
        from core import tool_errors as _errs

        result["error_code"] = _errs.COMMAND_FAILED
        result["error"] = _errs.command_failure_note(rc, result["stderr_tail"])
    if rc in {125, 126, 137, 138} and "HARNESS_SANDBOX_LIMIT" in err:
        result.update(
            {
                "error_code": "sandbox_resource_exhausted",
                "resource": {
                    125: "storage",
                    126: "output",
                    137: "memory",
                    138: "pids",
                }[int(rc)],
                "safe_to_retry": False,
                "retry_guidance": (
                    "Do not rerun automatically: inspect partial outputs first, then "
                    "explicitly resume or select a larger initial resource_profile."
                ),
            }
        )

    # 图里有豆腐块 → 抬成醒目告警。matplotlib 渲染缺字形时**必然**往 stderr 打
    # "Glyph NNN missing from font(s)"，但它埋在一堆输出里、rc 还是 0 —— v27/v28
    # 就是这么把满屏方块的图静默交付出去的（谁都没看那行）。把它从 stderr 捞出来
    # 抬到 envelope 顶层，让写这段画图代码的 agent 当场看见、当场改，而不是等到
    # 论文里才发现。#651 已把默认 CJK 字体栈补上，这里管的是残留（某字形连 CJK
    # 字体也没有 / 部署机没装字体）。
    _tofu = _missing_glyph_summary(out + "\n" + err)
    if _tofu:
        result["figure_glyph_warning"] = _tofu
    return result


register_tool(
    ToolDefinition(
        name="execute_python",
        description=(
            "在强制容器沙盒里执行 Python 代码字符串；默认无网络、宿主文件仅挂载本节点"
            "工作区和 run 目录，并强制内存/CPU/PID/时间/输出上限。"
            "默认 cwd = state.root/workspace/，timeout=300s。"
            "同一 RunAttempt 内与 Bash 共用严格 FIFO 队列；每次解释器都是全新进程，"
            "不会继承上一条命令的 cwd、环境变量或后台进程。"
            "可选择初始 resource_profile，接近资源上限时框架自动扩容到冻结天花板；"
            "资源失败后不会盲目重跑可能已有部分写入的代码。"
            "可传 requirements=['numpy', 'pandas'] 让工具先 pip install 再跑。"
            "返回 stdout / stderr 的尾部 + returncode。"
            "适合：数值计算、画图（用 matplotlib 存到 workspace）、解析数据。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "要执行的 Python 代码。"},
                "timeout": {"type": "integer", "default": 300, "minimum": 1, "maximum": 7200},
                "cwd": {"type": "string", "description": "可选：工作目录。默认 state.root/workspace。"},
                "resource_profile": {
                    "type": "string",
                    "enum": ["small", "standard", "large", "xlarge"],
                    "default": "standard",
                    "description": (
                        "初始容量：small=1GiB/1CPU，standard=4GiB/2CPU，"
                        "large=16GiB/4CPU，xlarge=32GiB/8CPU；随后可自动扩容。"
                    ),
                },
                "requirements": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选：要先 pip install 的包列表。",
                },
            },
            "required": ["code"],
        },
        # P6-b（2026-08-08）：raw python 对 postprocess 放行 —— 它就是出图
        # renderer（模型写 matplotlib 是训练分布内能力）。防伪造靠铸造权而非
        # 禁用 python：figure_review / figure_package 是 typed-only 铸造，
        # 越界写有 dangerous_commands 硬拒，高危模式有 pause 闸。
        # experiment 用自己的 safe_execute_python 包装（path-role 守卫），
        # 不在这张表里。
        #
        # 2026-08-22 补 observation：它的 system_prompt 有一整节「你不做实验，
        # 但你会算」，白名单里也写着 execute_python —— 而这张表把它滤掉了，
        # 一次也没授予过。检视式取证要合并效应量、跑统计、做文本挖掘，算是本职。
        # 模态边界不靠禁用 python 守：判据是**有没有让世界产生新数据**
        # （见 docs/evidence-modalities.md §9，那里已经写着「不得自己跑出一份
        # 新数据充数」），跟工具箱无关 —— 拿 bash 一样能跑出新数据。
        # derivation（2026-08-22）：exploratory 模式要做数值实验**找猜想的形式**
        # —— 扫参数看规律、特例摸底、构造反例。这不是"产生新数据当证据"
        # （那条红线由 harness rules + 冻结门管，且 exploratory 不许勾账），
        # 是演绎开工前的探路。
        # _orchestrator（2026-08-31，wangd 拍板）：轻量计算通道。"用户随口要核
        # 个数"不该起完整节点。安全依据不在这份名单，在三道既有墙的组合：
        # 强制沙箱把写根钉死在自己作用域 + run 目录（write_roots_for）、
        # 代码文本过 _PROTECTED_SIG 边界、落地走 deliverable_writes 的门。
        # experiment 仍然除外：它的算力走受治理的作业提交，不走裸 python
        # （见 test_experiment_cannot_reach_raw_shell）。
        allowed_node_types=["postprocess", "observation", "derivation",
                            "_orchestrator", "orchestrator"],
        internal_only=False,
        risk_level="high",         # Python 进程访问系统 = 高风险
    ),
    _execute_python,
)
