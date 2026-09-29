"""两条「只在真有事时才花 prompt」的机械 hook。

一、turn-1 数据源可达性预检：只在有缺口时注入，判据与 fetch_resource 联网前的域名核对相同。
  - 全部可达 / 无 prereg / 解析不出主机 → 零注入（零 prompt 成本）
  - 有缺口 → 注入且含具体主机名与生效策略来源
  - 可达性判据 = 精确匹配或子域后缀匹配（与 fetch_resource 联网前核对同口径）
  - 提取保守：sst.mnmean.nc / v04r01 / 0.25° / URL 路径段都不是主机
  - 本 hook 只注入信息，不阻断任何工具调用

二、把 blocker 判给别人前先问一句（原为一句 rule，改成零成本机械层）：
  - report_blocker 显式判给非 experiment 的 owner 且本 run 没问过人 → 提醒一次
  - 不带 / 空 suggested_owner（scheduler 路径那类）→ 永不命中
  - 判给 experiment 自己、已问过人、已提醒过 → 零注入
  - 只注入提醒，不拦 report_blocker、不改其返回值
"""
from __future__ import annotations

import json
import re
import os
from pathlib import Path
from types import SimpleNamespace

import yaml

from core.bootstrap import bootstrap
from core.capability_grants import grant_from_answer
from core.skill_registry import get_skill

from core.state import State
from nodes.experiment import hooks

_PREREG_WITH_GAP = """# 预注册

## 数据源与许可前提（冻结）

| 数据源 | 用途 | 许可与获取 |
| --- | --- | --- |
| NCEP/NCAR Reanalysis 1 | 环境场（风切变、中层湿度）首选 | NOAA PSL 公开，downloads.psl.noaa.gov 匿名可下载 |
| IBTrACS v04r01 | 台风路径 | 见 https://www.ncei.noaa.gov/data/ibtracs/ |

变量文件 sst.mnmean.nc，分辨率 0.25° × 0.25°，脚本见 prepare.py。

## 分析计划

用 out-of-section.example.org 上的对照数据交叉验证。
"""


def _state(tmp_path: Path) -> State:
    return State.new("experiment", tmp_path)


def _ctx(state, turn: int = 1):
    return SimpleNamespace(state=state, turn=turn)


def _save_frozen_prereg(state: State, content: str) -> None:
    # 冻结只能来自账本的 freeze 行（save 行里的 frozen 键会被剥掉）。
    saved = state.save_artifact("pre_registration", "contract", content)
    state.mark_frozen(saved["id"])


def _run(state, turn: int = 1):
    return hooks._data_source_reachability_on_turn_start(_ctx(state, turn))


def _builtin_only(monkeypatch):
    monkeypatch.delenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", raising=False)


# ── 零注入分支 ────────────────────────────────────────────────────────────


def test_no_pre_registration_injects_nothing(tmp_path, monkeypatch):
    _builtin_only(monkeypatch)
    assert _run(_state(tmp_path)) is None


def test_unparsable_section_injects_nothing(tmp_path, monkeypatch):
    """小节找不到 → 静默，不得因为解析不出来就骚扰。"""
    _builtin_only(monkeypatch)
    state = _state(tmp_path)
    _save_frozen_prereg(state, "# 预注册\n\n## 研究问题\n\n随便写点什么。\n")
    assert _run(state) is None


def test_section_without_any_host_injects_nothing(tmp_path, monkeypatch):
    _builtin_only(monkeypatch)
    state = _state(tmp_path)
    _save_frozen_prereg(
        state,
        "## 数据源与许可前提（冻结）\n\n| NCEP/NCAR Reanalysis 1 | 环境场 | 本地共享盘已有副本 |\n",
    )
    assert _run(state) is None


def test_all_hosts_reachable_injects_nothing(tmp_path, monkeypatch):
    _builtin_only(monkeypatch)
    state = _state(tmp_path)
    _save_frozen_prereg(
        state,
        "## 数据源与许可前提（冻结）\n\n| 对照数据 | 交叉验证 | https://zenodo.org/record/123 公开 |\n",
    )
    assert _run(state) is None


def test_only_runs_on_turn_one_and_only_once(tmp_path, monkeypatch):
    _builtin_only(monkeypatch)
    state = _state(tmp_path)
    _save_frozen_prereg(state, _PREREG_WITH_GAP)

    assert _run(state, turn=2) is None
    assert _run(state, turn=1) is not None
    assert _run(state, turn=1) is None          # 已注入过不再重复


# ── 有缺口分支 ────────────────────────────────────────────────────────────


def test_gap_injection_names_hosts_and_effective_policy_source(tmp_path, monkeypatch):
    _builtin_only(monkeypatch)
    state = _state(tmp_path)
    _save_frozen_prereg(state, _PREREG_WITH_GAP)

    messages = _run(state)

    assert messages is not None and len(messages) == 1
    text = messages[0].content
    assert "downloads.psl.noaa.gov" in text
    assert "www.ncei.noaa.gov" in text
    assert "builtin_default" in text            # 生效策略来源，而非"已配置白名单"
    assert "request_network_access" in text
    assert "host=" in text
    assert "out-of-section.example.org" not in text   # 小节边界：后续小节不参与
    assert len(text.strip().splitlines()) <= 12       # 篇幅硬上限


def test_gap_injection_frames_the_gap_as_a_platform_limit_not_missing_data(tmp_path, monkeypatch):
    """核心命题：平台施加的人为限制不是科学事实，不许据此改小科学设计。"""
    _builtin_only(monkeypatch)
    state = _state(tmp_path)
    _save_frozen_prereg(state, _PREREG_WITH_GAP)

    text = _run(state)[0].content
    assert "平台配置限制" in text
    assert "不要绕开" in text
    assert "不得因单个数据源停掉整个 run" in text


def test_downgrade_options_are_marked_last_resort(tmp_path, monkeypatch):
    """换替代源/降级修订只能排在授权、长期配置和账号前提之后。"""
    _builtin_only(monkeypatch)
    state = _state(tmp_path)
    _save_frozen_prereg(state, _PREREG_WITH_GAP)

    text = _run(state)[0].content
    assert "最后手段" in text
    assert "仅当授权、长期配置和账号前提都不成立" in text


def test_gap_injection_prefers_per_run_requests_and_keeps_deployment_as_long_term(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", "pypi.org,github.com")
    state = _state(tmp_path)
    _save_frozen_prereg(state, _PREREG_WITH_GAP)

    text = _run(state)[0].content
    assert 'request_network_access(host="www.ncei.noaa.gov"' in text
    assert 'request_network_access(host="downloads.psl.noaa.gov"' in text
    assert "允许后原样重试" in text
    assert "HARNESS_SANDBOX_EGRESS_ALLOWLIST" in text
    assert "长期" in text
    assert (
        "HARNESS_SANDBOX_EGRESS_ALLOWLIST="
        "pypi.org,github.com,www.ncei.noaa.gov,downloads.psl.noaa.gov"
    ) in text


def test_turn_one_preflight_and_fetch_share_the_exact_run_grant_decision(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", "pypi.org")
    state = _state(tmp_path)
    _save_frozen_prereg(state, _PREREG_WITH_GAP)
    for host in ("www.ncei.noaa.gov", "downloads.psl.noaa.gov"):
        assert grant_from_answer(state, host, "允许", reason="frozen prereg source")

    assert _run(state) is None
    from nodes.experiment.tools.resource_fetch import _egress_access_decision

    for host in ("www.ncei.noaa.gov", "downloads.psl.noaa.gov"):
        decision = _egress_access_decision(state, host)
        assert decision["allowed"] is True
        assert decision["authorized_by"] == "per_run_exact_grant"


# ── 域名风险标注：只标注，绝不放行 ────────────────────────────────────────


def test_risk_note_reports_tld_class_and_known_institution():
    assert "downloads.psl.noaa.gov（.gov／NOAA 美国海洋与大气管理局）" == (
        hooks._host_risk_note("downloads.psl.noaa.gov"))
    assert "esgf-node.llnl.gov（.gov／ESGF 地球系统网格联盟）" == (
        hooks._host_risk_note("esgf-node.llnl.gov"))
    assert "cds.climate.copernicus.eu（其他／Copernicus 欧盟哥白尼计划）" == (
        hooks._host_risk_note("cds.climate.copernicus.eu"))
    assert hooks._host_tld_class("data.ceda.ac.uk") == ".ac.uk"
    assert hooks._host_tld_class("rda.ucar.edu") == ".edu"
    assert hooks._host_tld_class("zenodo.org") == ".org"
    assert hooks._host_tld_class("example.io") == "其他"


def test_institution_match_is_label_exact_not_bare_substring():
    """裸子串匹配会把 transfer.example.org 误标成 NSF —— 按域名标签精确匹配。"""
    assert hooks._host_risk_note("transfer.example.org") == "transfer.example.org（.org）"
    assert hooks._host_risk_note("nasadata-mirror.example.io") == (
        "nasadata-mirror.example.io（其他）")


def test_risk_annotation_never_widens_the_allowlist(tmp_path, monkeypatch):
    """不变量：风险判断只是给 owner 的判断辅助，代码里不得有据此自动放行的写入点。"""
    source = (Path(hooks.__file__)).read_text(encoding="utf-8")
    for writer in ("os.environ[", "os.environ.setdefault", "os.putenv",
                   "monkeypatch.setenv", "environ.update"):
        assert writer not in source, f"hooks.py 出现环境写入点：{writer}"
    assert "HARNESS_SANDBOX_EGRESS_ALLOWLIST" in source      # 只出现在注入文案里

    # 命中已知机构也不改变可达性判定：判据仍只看白名单。
    _builtin_only(monkeypatch)
    state = _state(tmp_path)
    from nodes.experiment.tools.resource_fetch import _egress_access_decision

    assert not _egress_access_decision(
        state, "downloads.psl.noaa.gov"
    )["allowed"]
    hooks._host_risk_note("downloads.psl.noaa.gov")
    assert not _egress_access_decision(
        state, "downloads.psl.noaa.gov"
    )["allowed"]

    _save_frozen_prereg(state, _PREREG_WITH_GAP)
    messages = _run(state)
    assert messages is not None                               # 仍然报缺口，不自动放行
    assert "最终由 owner 批准" in messages[0].content
    assert "HARNESS_SANDBOX_EGRESS_ALLOWLIST" not in os.environ  # 没被悄悄写进环境


def test_environment_allowlist_closes_the_gap(tmp_path, monkeypatch):
    """把父域加进环境变量后，子域后缀匹配即判为可达 → 零注入。"""
    monkeypatch.setenv(
        "HARNESS_SANDBOX_EGRESS_ALLOWLIST", "pypi.org,psl.noaa.gov,ncei.noaa.gov",
    )
    state = _state(tmp_path)
    _save_frozen_prereg(state, _PREREG_WITH_GAP)
    assert _run(state) is None


def test_reachability_matches_the_proxy_rule(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", "psl.noaa.gov")
    state = _state(tmp_path)
    from nodes.experiment.tools.resource_fetch import _egress_access_decision

    def allowed(host):
        return _egress_access_decision(state, host)["allowed"]

    assert allowed("psl.noaa.gov")
    assert allowed("downloads.psl.noaa.gov.")
    assert allowed("DOWNLOADS.PSL.NOAA.GOV")
    assert not allowed("psl.noaa.gov.evil.com")
    assert not allowed("evilpsl.noaa.gov")
    assert not allowed("noaa.gov")   # 父域不因子域放行


# ── 提取保守性 ────────────────────────────────────────────────────────────


def test_extraction_rejects_filenames_versions_and_path_segments():
    hosts = hooks._extract_declared_hosts(
        "变量 sst.mnmean.nc、IBTrACS v04r01、0.25° 网格、脚本 prepare.py、requirements.txt，"
        "见 https://www.ncei.noaa.gov/data/ncep.reanalysis/surface/air.sig995.nc"
    )
    assert hosts == ["www.ncei.noaa.gov"]
    for bad in ("sst.mnmean.nc", "prepare.py", "requirements.txt",
                "ncep.reanalysis", "air.sig995.nc", "0.25", "v04r01"):
        assert bad not in hosts


def test_extraction_keeps_bare_hostnames_in_table_cells():
    hosts = hooks._extract_declared_hosts(
        "| NCEP | 环境场 | NOAA PSL 公开，downloads.psl.noaa.gov 匿名可下载 |"
    )
    assert hosts == ["downloads.psl.noaa.gov"]


# ── 装配与非阻断 ──────────────────────────────────────────────────────────


def test_hook_is_enabled_in_harness_loop_hooks():
    """E-3 教训：只在 hooks.py 注册而不进 harness.yaml 的 loop_hooks 就是死代码。"""
    config = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "harness.yaml").read_text(encoding="utf-8")
    )
    # K11 起经 turn_one_briefing 合并注入（与其他开局提示拼成一条）。
    from nodes.experiment import hooks as experiment_hooks

    assert "turn_one_briefing" in config["loop_hooks"]
    assert "data_source_reachability_preflight" in dict(experiment_hooks._TURN_ONE_BRIEFING_PARTS)


def test_hook_observes_only_and_never_blocks_a_tool_call():
    hook = hooks.data_source_reachability_preflight
    assert hook.on_turn_start is not None
    assert hook.on_llm_response is None
    assert hook.on_turn_end is None
    assert hook.on_end is None
    assert hook.on_before_finish is None


# ══════════════════════════════════════════════════════════════════════════
# foreign_owner_blocker_ask_nudge —— 判给别人之前先问一句
# ══════════════════════════════════════════════════════════════════════════


def _blocker(**args) -> dict:
    return {"name": "report_blocker", "args": args,
            "result": {"status": "success", "blocker": {"blocker_id": "b1"}}}


def _nudge(state, records, turn: int = 7):
    ctx = SimpleNamespace(state=state, turn=turn, tool_call_records=records)
    return hooks._foreign_owner_blocker_nudge_on_turn_end(ctx)


# ── 零注入分支（不触发就一个字都不花）────────────────────────────────────


def test_no_blocker_call_injects_nothing(tmp_path):
    state = _state(tmp_path)
    assert _nudge(state, []) is None
    assert _nudge(state, [{"name": "safe_run_bash", "args": {}, "result": {}}]) is None


def test_scheduler_path_blocker_without_suggested_owner_never_matches(tmp_path):
    """harness.yaml 的 scheduler 路径规则要求那类 blocker 不带 suggested_owner
    且 human_action='not_applicable' —— 本 hook 绝不能把它们拽去问人。"""
    state = _state(tmp_path)
    for args in (
        {"summary": "unresolved_scheduler_path_placeholder",
         "category": "environment", "human_action": "not_applicable"},
        {"summary": "scheduler_path_not_declared", "human_action": "not_applicable"},
        {"summary": "approved_write_root_not_scheduler_usable"},
    ):
        assert _nudge(state, [_blocker(**args)]) is None


def test_empty_suggested_owner_injects_nothing(tmp_path):
    state = _state(tmp_path)
    assert _nudge(state, [_blocker(summary="x", suggested_owner="")]) is None
    assert _nudge(state, [_blocker(summary="x", suggested_owner="   ")]) is None


def test_owner_is_experiment_itself_injects_nothing(tmp_path):
    state = _state(tmp_path)
    for owner in ("experiment", "Experiment", " experiment ", "node:experiment"):
        assert _nudge(state, [_blocker(summary="x", suggested_owner=owner)]) is None


def test_already_asked_in_the_same_turn_injects_nothing(tmp_path):
    state = _state(tmp_path)
    records = [
        {"name": "request_human_input", "args": {"question": "?"}, "result": {}},
        _blocker(summary="x", suggested_owner="framework"),
    ]
    assert _nudge(state, records) is None


def test_asked_on_an_earlier_turn_injects_nothing(tmp_path):
    """问人会 pause，那一轮的 on_turn_end 未必跑过 —— 所以要回扫 transcript，
    不能只靠 hook_state 累计。"""
    state = _state(tmp_path)
    state.append_transcript("tool_call", turn=2, name="request_human_input",
                            args={"question": "egress 白名单能加吗"})
    assert _nudge(state, [_blocker(summary="x", suggested_owner="framework")]) is None


def test_network_grant_request_then_unresolved_blocker_does_not_ask_again(tmp_path):
    """能力授权卡已经问过人；授权未生效后的正式 blocker 不应再被叫去普通问答。"""
    state = _state(tmp_path)
    host = "downloads.psl.noaa.gov"
    state.append_transcript(
        "tool_call",
        turn=2,
        name="request_network_access",
        args={"host": host, "reason": "获取冻结 prereg 数据源"},
    )
    state.append_transcript(
        "resource_acquisition_failed",
        turn=3,
        error_code="network_access_request_unresolved",
        blocked_host=host,
    )

    assert _nudge(state, [_blocker(
        summary=f"{host} 的授权申请未生效",
        category="environment",
        suggested_owner="部署方或本 run 的授权审批人",
    )]) is None


def test_a_mere_mention_of_the_tool_name_is_not_an_ask(tmp_path):
    """transcript 里别的事件提到工具名不算问过人（预筛之后仍按事件类型判）。"""
    state = _state(tmp_path)
    state.append_transcript("tool_result", turn=2, name="save_artifact",
                            result_preview="下一步建议 request_human_input")
    assert _nudge(state, [_blocker(summary="x", suggested_owner="framework")]) is not None


# ── 触发分支 ──────────────────────────────────────────────────────────────


def test_foreign_owner_without_any_ask_gets_one_compact_nudge(tmp_path):
    state = _state(tmp_path)
    messages = _nudge(state, [_blocker(
        summary="egress 白名单缺 downloads.psl.noaa.gov",
        category="environment", suggested_owner="framework")])

    assert messages is not None and len(messages) == 1
    text = messages[0].content
    assert "framework" in text
    assert "request_human_input" in text
    assert len(text.strip().splitlines()) <= 3       # ≤3 行硬上限
    assert '"event": "foreign_owner_blocker_ask_nudge"' in (
        state.transcript_path.read_text(encoding="utf-8"))


def test_nudge_fires_at_most_once_per_run(tmp_path):
    state = _state(tmp_path)
    records = [_blocker(summary="x", suggested_owner="core")]
    assert _nudge(state, records) is not None
    assert _nudge(state, records) is None
    assert _nudge(state, [_blocker(summary="y", suggested_owner="user")]) is None


def _owner(**args) -> str:
    return hooks._foreign_blocker_owner(
        {"name": "report_blocker", "args": {"summary": "x", **args}})


def test_owner_classification_is_conservative():
    assert _owner(suggested_owner="framework") == "framework"
    assert _owner(suggested_owner="core") == "core"
    assert _owner(suggested_owner="user") == "user"
    assert _owner(suggested_owner="调用方") == "调用方"
    assert _owner(suggested_owner="experiment") == ""        # 自己
    assert _owner(suggested_owner="experiment_node") == ""   # 含 experiment → 当自己人
    assert _owner() == ""                                    # 字段缺失
    assert hooks._foreign_blocker_owner({"name": "save_artifact", "args": {}}) == ""
    assert hooks._foreign_blocker_owner({"name": "report_blocker", "args": None}) == ""


# ── 装配与非阻断 ──────────────────────────────────────────────────────────


def test_nudge_hook_is_enabled_in_harness_loop_hooks():
    """E-3 教训：只注册不进 harness.yaml 的 loop_hooks 就是死代码。"""
    config = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "harness.yaml").read_text(encoding="utf-8")
    )
    assert "foreign_owner_blocker_ask_nudge" in config["loop_hooks"]


def test_nudge_only_injects_and_never_touches_the_blocker_result(tmp_path):
    hook = hooks.foreign_owner_blocker_ask_nudge
    assert hook.on_turn_end is not None
    assert hook.on_turn_start is None
    assert hook.on_llm_response is None          # 拿不到响应 → 改不了工具调度
    assert hook.on_end is None
    assert hook.on_before_finish is None

    record = _blocker(summary="x", suggested_owner="framework")
    before = json.dumps(record, ensure_ascii=False, sort_keys=True)
    assert _nudge(_state(tmp_path), [record]) is not None
    assert json.dumps(record, ensure_ascii=False, sort_keys=True) == before


# ══════════════════════════════════════════════════════════════════════════
# platform_limit_ask_nudge —— 撞上平台限制时请求拆墙，而不是改设计/停车
# ══════════════════════════════════════════════════════════════════════════


def _rec(name: str, result) -> dict:
    return {"name": name, "args": {}, "result": result}


def _limit(state, records, turn: int = 5):
    ctx = SimpleNamespace(state=state, turn=turn, tool_call_records=records)
    return hooks._platform_limit_nudge_on_turn_end(ctx)


# ── 零注入分支（不触发就一个字都不花）────────────────────────────────────


def test_no_tool_calls_injects_nothing(tmp_path):
    assert _limit(_state(tmp_path), []) is None


def test_successful_results_never_trigger(tmp_path):
    """成功路径零成本 —— 哪怕返回里出现了这些词。"""
    state = _state(tmp_path)
    for result in (
        {"status": "success", "note": "egress allowlist 已包含该域名"},
        {"status": "success", "returncode": 0, "stdout": "max_bytes=1048576"},
        {"status": "ok", "policy": {"entries": ["pypi.org"]}},
        "纯字符串返回",
        None,
    ):
        assert _limit(state, [_rec("resource_fetch", result)]) is None


def test_ordinary_scientific_failure_does_not_trigger(tmp_path):
    """科学/代码失败不是平台限制 —— 不该拿这条去骚扰 owner。"""
    state = _state(tmp_path)
    for result in (
        {"status": "error", "error": "ValueError: shape mismatch (3,) vs (4,)"},
        {"status": "error", "returncode": 1, "stderr": "ModuleNotFoundError: xarray"},
        {"status": "error", "error": "站点已下线：HTTP 404 Not Found"},
    ):
        assert _limit(state, [_rec("safe_run_bash", result)]) is None


# ── 五类平台限制形状都能识别 ──────────────────────────────────────────────


def test_each_platform_limit_shape_is_recognised(tmp_path):
    cases = {
        "egress 白名单": {
            "status": "error",
            "error": "fetch failed: HTTP 403 Forbidden (egress allowlist)",
        },
        "沙箱不可用": {
            "status": "error",
            "error": ("启动 shell 失败：the linux backend cannot enforce the write "
                      "boundary (I1) on this host: bwrap: not found"),
        },
        "walltime / 超时上限": {
            "status": "error", "returncode": 124,
            "stderr": "HARNESS_SANDBOX_LIMIT walltime\n",
        },
        "文件大小上限": {
            "status": "error",
            "stderr": "HARNESS_SANDBOX_LIMIT storage\n",
        },
        "路径能力 / 可写根": {
            "status": "blocked", "reason": "path_capability_required",
        },
    }
    for expected, result in cases.items():
        category, tool = hooks._platform_limit_category(_rec("safe_run_bash", result))
        assert category == expected, f"{expected} 未被识别：{result}"
        assert tool == "safe_run_bash"
        assert _limit(_state(tmp_path / expected.replace(" ", "").replace("/", "")),
                      [_rec("safe_run_bash", result)]) is not None


def test_the_real_isolation_refusal_lands_in_the_sandbox_category(tmp_path, monkeypatch):
    """标记对着真实产出方：原生后端守不住写边界时，受管咽喉把 core/isolation 的原文
    以 spawn_failed 带回，safe_run_bash 包成「启动 shell 失败：…」。
    SandboxUnavailable 那句只有无人调用的 require_available() 会抛（#775）。"""
    import asyncio

    from core import isolation
    from shared.lib.cancellable_subprocess import spawn_and_wait

    class _NoWriteBoundary:
        unavailable_reason = "bwrap: not found; landlock: ABI too old"

        def capabilities(self):
            return frozenset()

    monkeypatch.delenv(isolation.EXECUTOR_ENV, raising=False)
    monkeypatch.setattr(isolation, "_auto_cache", None)
    monkeypatch.setattr(isolation, "native_backend_name", lambda: "linux")
    monkeypatch.setattr(isolation, "_instantiate", lambda name: _NoWriteBoundary())
    status, _rc, _out, err = asyncio.run(spawn_and_wait(
        "true", state=_state(tmp_path / "run"), timeout=5, shell=True,
        writable_roots=[tmp_path]))
    assert status == "spawn_failed"
    result = {"status": "error",
              "error": f"启动 shell 失败：{err.decode('utf-8', errors='replace')[:500]}"}
    category, tool = hooks._platform_limit_category(_rec("safe_run_bash", result))
    assert (category, tool) == ("沙箱不可用", "safe_run_bash"), result


def test_the_airsea_regression_egress_403_is_caught(tmp_path):
    """airsea 冻结 prereg v17→v18 就是被这个形状的失败逼着改小了科学对照。"""
    state = _state(tmp_path)
    messages = _limit(state, [_rec("fetch_resource", {
        "status": "error", "url": "https://downloads.psl.noaa.gov/Datasets/godas/",
        "error": "curl: (22) The requested URL returned error: 403",
        "egress_policy": {"source": "builtin_default", "entries": ["pypi.org"]},
    })])
    assert messages is not None
    text = messages[0].content
    assert "平台限制，不是科学事实" in text
    assert "不要绕开去改科学设计" in text
    assert "不要因此停掉整个 run" in text
    assert "request_human_input" in text
    assert len(text.strip().splitlines()) <= 8            # 篇幅硬上限


def test_nudge_asks_for_the_five_things_owner_needs_to_approve(tmp_path):
    text = _limit(_state(tmp_path), [_rec("safe_run_bash", {
        "status": "error", "reason": "path_capability_required"})])[0].content
    for needed in ("哪道墙", "需要什么", "为什么需要", "风险判断", "现成可执行的解除操作"):
        assert needed in text


def test_limit_nudge_fires_at_most_once_per_run(tmp_path):
    state = _state(tmp_path)
    walltime = [_rec("safe_run_bash", {
        "status": "error", "returncode": 124,
        "stderr": "HARNESS_SANDBOX_LIMIT walltime\n"})]
    assert _limit(state, walltime) is not None
    assert _limit(state, walltime) is None
    assert _limit(state, [_rec("fetch_resource", {
        "status": "error",
        "error": "no native isolation backend for sunos5"})]) is None
    assert '"event": "platform_limit_ask_nudge"' in (
        state.transcript_path.read_text(encoding="utf-8"))


# ── 装配与非阻断 ──────────────────────────────────────────────────────────


def test_limit_hook_is_enabled_in_harness_loop_hooks():
    """E-3 教训：只注册不进 harness.yaml 的 loop_hooks 就是死代码。"""
    config = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "harness.yaml").read_text(encoding="utf-8")
    )
    assert "platform_limit_ask_nudge" in config["loop_hooks"]


def test_limit_hook_only_injects_and_never_mutates_a_tool_result(tmp_path):
    hook = hooks.platform_limit_ask_nudge
    assert hook.on_turn_end is not None
    assert hook.on_turn_start is None
    assert hook.on_llm_response is None          # 拿不到响应 → 改不了工具调度
    assert hook.on_end is None
    assert hook.on_before_finish is None

    record = _rec("fetch_resource", {"status": "error", "error": "403 Forbidden egress"})
    before = json.dumps(record, ensure_ascii=False, sort_keys=True)
    assert _limit(_state(tmp_path), [record]) is not None
    assert json.dumps(record, ensure_ascii=False, sort_keys=True) == before


# ── 阶段 0.5 阶梯 ②：客观缺失 vs 平台限制 ─────────────────────────────────
#
# 阶梯正文原先写在 `legacy_system_prompt` 里 —— 那个键 core/loader.py 从不读取，
# 所以那段文字**从未进过任何运行时 prompt**。2026-08-31 死块整体删除，阶梯正文迁进
# `skills/feasibility-ladder/SKILL.md`，由活 system_prompt 的 SOP 路由表按需
# `load_skill` 拉取。下面几条从"断言死文本长什么样"改成"断言活 skill 长什么样"。


def _config() -> dict:
    return yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "harness.yaml").read_text(encoding="utf-8")
    )


def _feasibility_skill() -> str:
    return (
        Path(__file__).resolve().parents[1]
        / "skills" / "feasibility-ladder" / "SKILL.md"
    ).read_text(encoding="utf-8")


def test_acquire_rung_splits_objective_absence_from_platform_limits():
    skill = _feasibility_skill()
    assert "**客观缺失**" in skill
    assert "**平台限制**" in skill
    assert "请求拆墙" in skill
    # 客观缺失那条的原文保留不动
    assert "缺上游数据先看本 run 转发的 artifact 或 `search_kb`" in skill
    assert "捏造\"实验**结果**/ground truth\"不合法" in skill


def test_acquire_rung_makes_redesign_the_last_resort():
    skill = _feasibility_skill()
    assert "找替代源与改预注册是**最后手段**" in skill
    assert "不能是\"平台不让我拿\"" in skill


def test_infeasible_declaration_protocol_matches_what_the_framework_reads():
    """阶梯第 ④ 级的字段名必须和判决侧逐字对齐，否则强制 REDIRECT 永远不触发。"""
    skill = _feasibility_skill()
    feasibility_src = (
        Path(__file__).resolve().parents[3] / "shared" / "lib" / "feasibility.py"
    ).read_text(encoding="utf-8")
    for field in ("infeasible: true", "infeasible_reason", "redirect_target"):
        assert field in skill, field
        assert field.split(":")[0] in feasibility_src, field
    assert "## Feasibility" in skill
    assert "verdict: infeasible" in skill


def test_feasibility_ladder_is_reachable_from_the_live_sop_router():
    """迁进 skill 不算数，活 prompt 里得有那一行路由，agent 才知道去 load。"""
    bootstrap(force=True)            # skill 注册表按需装载，不 bootstrap 就是空的
    assert "feasibility-ladder" in _config()["system_prompt"]
    assert get_skill("feasibility-ladder") is not None


def test_live_system_prompt_did_not_grow():
    """预算不变量：阶梯正文进的是 skill，不是常驻 prompt。"""
    live = _config()["system_prompt"]
    assert "阶段 0.5" not in live
    assert "客观缺失" not in live
    # node20 的 23a0fe78 把口诀压成了 `probe→acquire→ask→infeasible`（去掉 declare），
    # 所以这里只钉前三级 —— 判据在 skill 里，rules 只需保住这个指针。
    assert "probe→acquire→ask→" in "\n".join(_config()["rules"])


def test_harness_yaml_has_no_orphan_top_level_keys():
    """通用门：harness.yaml 里每个顶层键都必须真的被其生效 reader 读取。

    core/loader.py 是白名单式 `raw.get(...)`，对未知顶层键零校验 —— 改错一个键名
    等于静默删除整段配置，没有任何报错。experiment 的 `legacy_system_prompt` /
    `legacy_rules` 就这样死了 13 天、被 18 个提交继续编辑。这条断言把那个类别钉死：
    往 harness.yaml 里加一个没有任何生效 reader 的键，立刻红。
    """
    root = Path(__file__).resolve().parents[3]
    loader_src = (root / "core" / "loader.py").read_text(encoding="utf-8")
    # `\braw\.` 的词边界只匹配顶层 raw，不会误收 handoff_raw / summ_raw 等嵌套读取
    read_keys = set(re.findall(r'\braw\.get\(\s*"([a-z_]+)"', loader_src)) | set(
        re.findall(r'\braw\[\s*"([a-z_]+)"\s*\]', loader_src)
    )
    # Node-private declarations are intentionally invisible to Core loader, but
    # must have a live node reader.  Do not whitelist their names here: extract
    # the reader's raw.get calls just as we do for Core.
    task_prose_reader_src = (
        root / "nodes" / "experiment" / "task_prose_inputs.py"
    ).read_text(encoding="utf-8")
    read_keys |= set(re.findall(r'\braw\.get\(\s*"([a-z_]+)"', task_prose_reader_src)) | set(
        re.findall(r'\braw\[\s*"([a-z_]+)"\s*\]', task_prose_reader_src)
    )
    assert len(read_keys) > 20, f"reader 读取键提取失败，只找到 {read_keys}"

    declared = set(_config())
    orphans = sorted(declared - read_keys)
    assert orphans == [], (
        f"这些顶层键没有任何生效 reader，写了等于没写：{orphans}。"
        "要么接进 loader，要么删掉，不要留在 yaml 里假装生效。"
    )


def test_the_rule_no_longer_carries_this_criterion():
    """判据落在机械层，rules 里那半句必须消失（否则每个 run 都白付一遍 prompt 成本）。

    依据 AGENTS.md：「提示词或 Skill 只能提供操作指引；确定性要求必须落实到工具、
    钩子、门禁或回归测试。」`suggested_owner` 非 experiment 时先问一句这条硬约束由
    foreign_owner_blocker_ask_nudge 承担，不触发就零字。node20 的 23a0fe78 因为分叉
    自 0a42d563（该 clause 尚在 rules 里的那一版），合并时会把它带回来 —— 这条测试
    就是防这个。
    """
    config = _config()
    assert "foreign_owner_blocker_ask_nudge" in config["loop_hooks"]
    rules = config["rules"]
    assert len(rules) == 16
    assert not any("suggested_owner" in rule for rule in rules)
