"""Regression coverage for issue #699's missing network-to-disk bridge."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from core.cancellation import RunCancelled
from core.state import State
from core.capability_grants import grant_from_answer
from core.tool_registry import _REGISTRY, execute as execute_tool
from nodes.experiment.tools import resource_fetch as fetch
from nodes.experiment.tools.run_contract import _classify_experiment_scope


def _state(tmp_path: Path) -> State:
    base = tmp_path / "runs"
    base.mkdir()
    state = State.new(node_type="experiment", base_dir=base, project_id="issue-699")
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Acquire the declared solver source into managed storage.",
        "stage": "toolchain_build",
        "prereg_assignment": {
            "kind": "none",
            "reason": "This resource-fetch test has no governing preregistration.",
        },
    }
    result = asyncio.run(_classify_experiment_scope(
        state,
        scope="operation",
        operation_category="toolchain_build",
        reason="Acquire the declared source before its managed native build.",
    ))
    assert result["status"] == "success"
    return state


def _run(state: State, **kwargs) -> dict:
    return asyncio.run(fetch._fetch_resource(state=state, **kwargs))


def test_networked_file_fetch_sees_only_empty_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """联网只给一次性的取物进程，它唯一可写的是空 staging；curl 自己不跟重定向（#770）。"""
    state = _state(tmp_path)
    payload = b"MOM6 source archive fixture\n"
    seen: dict = {}

    async def fake_spawn(*args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        staging = Path(kwargs["writable_roots"][0])
        assert list(staging.iterdir()) == []
        (staging / "download").write_bytes(payload)
        return (
            "done",
            0,
            b'{"url_effective":"https://codeload.github.com/mom-ocean/MOM6/tar.gz/main",'
            b'"content_type":"application/x-gzip"}',
            b"",
        )

    monkeypatch.setattr(fetch, "spawn_and_wait", fake_spawn)
    result = _run(
        state,
        url="https://codeload.github.com/mom-ocean/MOM6/tar.gz/main",
        kind="file",
    )

    assert result["status"] == "success"
    assert Path(result["destination"]).read_bytes() == payload
    assert result["sha256"] == hashlib.sha256(payload).hexdigest()
    assert result["content_type"] == "application/x-gzip"
    assert seen["kwargs"]["network_access"] is True
    assert seen["kwargs"]["readonly_roots"] == []
    assert len(seen["kwargs"]["writable_roots"]) == 1
    staging = Path(seen["kwargs"]["writable_roots"][0])
    assert staging != state.root
    assert state.root not in seen["kwargs"]["writable_roots"]
    assert "curl" == seen["args"][0]
    assert seen["args"][1] == "-q"   # 不读 ~/.curlrc；必须是第一个参数（第三会话复审 0915 P3）
    assert "--proto" in seen["args"] and "=https" in seen["args"]
    assert "--location" not in seen["args"]


def test_changed_upstream_intent_is_rejected_before_fetch_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nodes.experiment.tools.run_contract import _classify_experiment_scope

    state = State.new(node_type="experiment", base_dir=tmp_path / "runs", project_id="issue-699")
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Acquire the declared native solver source.",
        "stage": "toolchain_build",
        "prereg_assignment": {
            "kind": "none",
            "reason": "This intent-binding test has no governing preregistration.",
        },
    }
    assert asyncio.run(_classify_experiment_scope(
        state,
        scope="operation",
        operation_category="toolchain_build",
        reason="Acquire the declared source before its managed native build.",
    ))["status"] == "success"
    state.hook_state["node_inputs"]["experiment_focus"] = "Acquire an unrelated proxy package."

    async def must_not_spawn(*_args, **_kwargs):
        raise AssertionError("changed intent reached the network sandbox")

    monkeypatch.setattr(fetch, "spawn_and_wait", must_not_spawn)
    result = _run(
        state,
        url="https://github.com/example/native-solver/archive.tar.gz",
        destination="native-solver.tar.gz",
    )

    assert result["status"] == "error"
    assert result["error_code"] == "execution_intent_binding_required"
    assert result["execution_intent_binding"]["status"] == "intent_changed"
    assert not (fetch.experiment_output_dir(state, "runtime") / "acquired").exists()


def test_hash_mismatch_discards_download(tmp_path: Path, monkeypatch) -> None:
    state = _state(tmp_path)

    async def fake_spawn(*_args, **kwargs):
        (Path(kwargs["writable_roots"][0]) / "download").write_bytes(b"tampered")
        return "done", 0, b"{}", b""

    monkeypatch.setattr(fetch, "spawn_and_wait", fake_spawn)
    destination = "bad-hash.tar.gz"
    result = _run(
        state,
        url="https://github.com/example/archive.tar.gz",
        expected_sha256="0" * 64,
        destination=destination,
    )

    assert result["status"] == "error"
    assert "SHA-256 不匹配" in result["error"]
    assert not (fetch.experiment_output_dir(state, "runtime") / "acquired" / destination).exists()


def test_existing_destination_is_never_overwritten(tmp_path: Path, monkeypatch) -> None:
    """判决拆除·第三波（rf:325 删）：「已存在」的判决只剩原子导入那一处（A，永不
    覆盖）；下载照做，导入时拒绝，原文件一字不动。"""
    state = _state(tmp_path)
    runtime = fetch.experiment_output_dir(state, "runtime", create=True)
    existing = runtime / "acquired" / "source.tar.gz"
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_bytes(b"keep")
    spawned: list[tuple] = []

    async def fake_spawn(*args, **kwargs):
        spawned.append(args)
        (Path(kwargs["writable_roots"][0]) / "download").write_bytes(b"new bytes")
        return ("done", 0,
                b'{"url_effective":"https://github.com/example/source.tar.gz","content_type":"application/x-gzip"}',
                b"")

    monkeypatch.setattr(fetch, "spawn_and_wait", fake_spawn)
    result = _run(
        state,
        url="https://github.com/example/source.tar.gz",
        destination="source.tar.gz",
    )

    assert spawned, "预检已删：下载照做，判决只在导入处"
    assert result["status"] == "error"
    assert "不会覆盖" in result["error"]
    assert existing.read_bytes() == b"keep"


def test_url_fragment_is_stripped_not_rejected() -> None:
    """判决拆除·第三波（rf:76 删）：fragment 本就不发给服务器，框架机械剥掉。"""
    assert fetch._validated_https_url("https://github.com/a/b.tar.gz#readme") == (
        "https://github.com/a/b.tar.gz")


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/example/source.tar.gz",
        "ssh://git@github.com/example/repo.git",
        "https://user:secret@github.com/example/repo.git",
        "https://127.0.0.1/source.tar.gz",
        "https://localhost/source.tar.gz",
    ],
)
def test_non_public_or_credentialed_urls_fail_before_network(
    tmp_path: Path, monkeypatch, url: str
) -> None:
    state = _state(tmp_path)
    spawned: list[bool] = []

    async def must_not_spawn(*_args, **_kwargs):
        spawned.append(True)
        raise AssertionError("invalid URL reached network")

    monkeypatch.setattr(fetch, "spawn_and_wait", must_not_spawn)
    assert _run(state, url=url)["status"] == "error"
    assert spawned == []


def _create_git_fixture(root: Path) -> str:
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-q",
            "-m",
            "fixture",
        ],
        check=True,
    )
    return subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()


def test_public_git_clone_is_pinned_and_imported(tmp_path: Path, monkeypatch) -> None:
    state = _state(tmp_path)
    commit_holder: dict[str, str] = {}

    async def fake_spawn(*args, **kwargs):
        assert args[:2] == ("git", "-c")
        assert "protocol.file.allow=never" in args
        assert "http.followRedirects=false" in args
        assert "--recurse-submodules" not in args
        assert kwargs["network_access"] is True
        assert kwargs["readonly_roots"] == []
        staging = Path(kwargs["writable_roots"][0])
        commit_holder["commit"] = _create_git_fixture(staging / "repository")
        return "done", 0, b"", b""

    monkeypatch.setattr(fetch, "spawn_and_wait", fake_spawn)
    result = _run(
        state,
        url="https://github.com/mom-ocean/MOM6.git",
        kind="git",
        recursive=True,
    )

    assert result["status"] == "success"
    assert result["commit"] == commit_holder["commit"]
    destination = Path(result["destination"])
    assert (destination / "README.md").read_text(encoding="utf-8") == "fixture\n"
    assert result["tree_sha256"]
    assert result["entries"] > 0


def test_git_commit_mismatch_never_imports(tmp_path: Path, monkeypatch) -> None:
    state = _state(tmp_path)

    async def fake_spawn(*_args, **kwargs):
        _create_git_fixture(Path(kwargs["writable_roots"][0]) / "repository")
        return "done", 0, b"", b""

    monkeypatch.setattr(fetch, "spawn_and_wait", fake_spawn)
    result = _run(
        state,
        url="https://github.com/mom-ocean/MOM6.git",
        kind="git",
        expected_commit="0" * 40,
        destination="mom6-pinned",
    )

    assert result["status"] == "error"
    assert "Git commit 不匹配" in result["error"]
    assert not (fetch.experiment_output_dir(state, "runtime") / "acquired/mom6-pinned").exists()


def test_downloaded_tree_cannot_import_an_escaping_symlink(tmp_path: Path, monkeypatch) -> None:
    state = _state(tmp_path)

    async def fake_spawn(*_args, **kwargs):
        repository = Path(kwargs["writable_roots"][0]) / "repository"
        _create_git_fixture(repository)
        os.symlink("../../outside", repository / "escape")
        return "done", 0, b"", b""

    monkeypatch.setattr(fetch, "spawn_and_wait", fake_spawn)
    result = _run(
        state,
        url="https://github.com/example/repository.git",
        kind="git",
        destination="no-escape",
    )

    assert result["status"] == "error"
    assert "越界 symlink" in result["error"]
    assert not (fetch.experiment_output_dir(state, "runtime") / "acquired/no-escape").exists()


def test_relative_destination_never_nests_default_prefix(tmp_path: Path, monkeypatch) -> None:
    """dest 省略、传 'X' 或传 'runtime/acquired/X'，三种写法落点必须一致且无嵌套。"""
    payload = b"IBTrACS fixture\n"

    async def fake_spawn(*_args, **kwargs):
        (Path(kwargs["writable_roots"][0]) / "download").write_bytes(payload)
        return "done", 0, b"{}", b""

    monkeypatch.setattr(fetch, "spawn_and_wait", fake_spawn)
    landed: list[Path] = []
    for index, destination in enumerate(
        [None, "IBTrACS.tar.gz", "runtime/acquired/IBTrACS.tar.gz"]
    ):
        variant = tmp_path / f"variant-{index}"
        variant.mkdir()
        state = _state(variant)
        result = _run(
            state,
            url="https://github.com/example/IBTrACS.tar.gz",
            destination=destination,
        )
        assert result["status"] == "success"
        acquired = fetch.experiment_output_dir(state, "runtime") / "acquired"
        target = Path(result["destination"])
        assert target == acquired / "IBTrACS.tar.gz"
        assert target.read_bytes() == payload
        assert not (acquired / "runtime").exists()
        landed.append(target.relative_to(acquired))
    assert landed[0] == landed[1] == landed[2]


def test_destination_cannot_escape_authorized_roles(tmp_path: Path, monkeypatch) -> None:
    state = _state(tmp_path)

    async def must_not_spawn(*_args, **_kwargs):
        raise AssertionError("unauthorized destination reached network")

    monkeypatch.setattr(fetch, "spawn_and_wait", must_not_spawn)
    result = _run(
        state,
        url="https://github.com/example/source.tar.gz",
        destination=str(tmp_path / "not-a-run-root/source.tar.gz"),
    )

    assert result["status"] == "error"
    assert "destination 必须位于" in result["error"]


def test_describe_acquisition_capabilities_reports_effective_policy(
    tmp_path: Path, monkeypatch
) -> None:
    """E-9: 规划数据源前可以只读查询运行时生效的获取能力。"""
    monkeypatch.setenv(
        "HARNESS_SANDBOX_EGRESS_ALLOWLIST", "mirror.example.org, data.example.net"
    )
    result = asyncio.run(fetch._describe_acquisition_capabilities(state=_state(tmp_path)))

    assert result["status"] == "success"
    assert result["egress"]["entries"] == ["mirror.example.org", "data.example.net"]
    assert result["egress"]["source"] == "environment:HARNESS_SANDBOX_EGRESS_ALLOWLIST"
    assert result["egress"]["env_var"] == "HARNESS_SANDBOX_EGRESS_ALLOWLIST"
    assert result["supported_kinds"] == ["file", "git"]
    assert result["size_limits"] == {
        "default_max_bytes": fetch._DEFAULT_MAX_BYTES,
        "max_max_bytes": fetch._MAX_MAX_BYTES,
    }
    assert result["timeout_limits"] == {
        "default_seconds": fetch._DEFAULT_TIMEOUT_SECONDS,
        "max_seconds": fetch._MAX_TIMEOUT_SECONDS,
    }
    assert "凭据" in result["credential_policy"]


def test_describe_acquisition_capabilities_labels_builtin_default(
    tmp_path: Path, monkeypatch
) -> None:
    """环境变量未设置时，默认名单必须被标注为运行时回退，而不是环境配置。"""
    from core.sandbox import DEFAULT_EGRESS_ALLOWLIST

    monkeypatch.delenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", raising=False)
    result = asyncio.run(fetch._describe_acquisition_capabilities(state=_state(tmp_path)))

    assert result["egress"]["raw"] == DEFAULT_EGRESS_ALLOWLIST
    assert "未设置" in result["egress"]["source"]
    assert result["egress"]["source"].startswith("builtin_default")


def test_unlisted_host_refusal_cites_runtime_effective_egress_policy(
    tmp_path: Path, monkeypatch
) -> None:
    """拒绝文案必须回显 os.environ 里生效的白名单，不能打印 DEFAULT 常量。"""
    from core.sandbox import DEFAULT_EGRESS_ALLOWLIST

    state = _state(tmp_path)
    monkeypatch.setenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", "mirror.example.org")

    spawned: list[bool] = []

    async def must_not_spawn(*_args, **_kwargs):
        spawned.append(True)
        raise AssertionError("an unlisted host reached the network")

    monkeypatch.setattr(fetch, "spawn_and_wait", must_not_spawn)
    result = _run(state, url="https://blocked.example.com/data.nc", kind="file")

    assert result["status"] == "error"
    # 名单外的域名在联网之前就拒（#770）；不变量仍是"回显运行时生效策略"。
    assert spawned == []
    assert "联网之前拒绝" in result["error"]
    assert "不在生效 egress 白名单内" in result["error"]
    assert "来源=environment:HARNESS_SANDBOX_EGRESS_ALLOWLIST" in result["error"]
    assert "mirror.example.org" in result["error"]
    assert DEFAULT_EGRESS_ALLOWLIST not in result["error"]
    assert result["error_code"] == "network_access_required"
    request = result["request_network_access"]
    assert request["tool"] == "request_network_access"
    assert request["arguments"]["host"] == "blocked.example.com"
    assert request["arguments"]["reason"]
    assert request["arguments"]["what_for"] == "https://blocked.example.com/data.nc"


def test_per_run_grant_is_exact_and_never_becomes_a_suffix_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path)
    monkeypatch.setenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", "mirror.example.net")
    assert grant_from_answer(
        state, "data.example.org", "允许", reason="fetch declared data"
    )

    expected = {
        "data.example.org": True,
        "child.data.example.org": False,
        "sibling.example.org": False,
        "example.org": False,
    }
    for host, allowed in expected.items():
        decision = fetch._egress_access_decision(state, host)
        assert decision["allowed"] is allowed, (host, decision)
    exact = fetch._egress_access_decision(state, "data.example.org")
    assert exact["authorized_by"] == "per_run_exact_grant"
    assert exact["policy"]["deployment_entries"] == ["mirror.example.net"]
    assert exact["policy"]["per_run_granted_hosts"] == ["data.example.org"]


def test_deployment_suffixes_come_only_from_core_environment_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core's merged entries/granted projection must not widen a per-run exact grant."""
    state = _state(tmp_path)
    assert grant_from_answer(state, "data.example.org", "允许", reason="declared input")
    monkeypatch.setattr(
        "core.sandbox.effective_egress_policy",
        lambda: {
            "env_var": "HARNESS_SANDBOX_EGRESS_ALLOWLIST",
            "raw": "mirror.example.net,data.example.org",
            "entries": ["mirror.example.net", "data.example.org"],
            "from_environment": ["mirror.example.net"],
            "granted": ["data.example.org"],
            "source": "environment:HARNESS_SANDBOX_EGRESS_ALLOWLIST + 1 grant",
        },
    )

    exact = fetch._egress_access_decision(state, "data.example.org")
    child = fetch._egress_access_decision(state, "child.data.example.org")

    assert exact["allowed"] is True
    assert exact["authorized_by"] == "per_run_exact_grant"
    assert child["allowed"] is False
    assert exact["policy"]["deployment_entries"] == ["mirror.example.net"]


def test_network_access_card_redacts_query_from_what_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path)
    monkeypatch.setenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", "mirror.example.net")

    async def must_not_spawn(*_args, **_kwargs):
        raise AssertionError("an unlisted host reached the network")

    monkeypatch.setattr(fetch, "spawn_and_wait", must_not_spawn)

    result = _run(
        state,
        url="https://data.example.org/private/file.nc?token=top-secret&x=1",
        destination="private.nc",
    )

    request = result["request_network_access"]["arguments"]
    assert request["host"] == "data.example.org"
    assert request["what_for"] == "https://data.example.org/private/file.nc"
    assert "top-secret" not in json.dumps(result, ensure_ascii=False)
    assert "top-secret" not in state.transcript_path.read_text(encoding="utf-8")


# ── 失败指引按白名单成员身份分叉（2026-08-31 实弹测试抓到的误导）──────────────
#
# 实测病例：github.com 明明在白名单内，一次代理出站瞬时超时被折叠成 403，
# 旧指引一律说"让部署方把域名加入白名单" —— 对瞬时故障这是错误建议，
# agent/用户会白跑一趟部署方（重放即愈，2/2 成功）。

async def _fail_spawn(*_args, **_kwargs):
    return ("done", 22, b"", b"curl: (22) The requested URL returned error: 403\n")


def test_failure_guidance_for_allowlisted_host_says_retry_not_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path)
    monkeypatch.delenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", raising=False)
    monkeypatch.setattr(fetch, "spawn_and_wait", _fail_spawn)

    result = _run(state, url="https://raw.githubusercontent.com/o/r/main/f.txt",
                  kind="file", destination="f.txt")

    assert result["status"] == "error"
    assert "已在" in result["error"]
    assert "重试" in result["error"]
    assert "不要申请修改白名单" in result["error"]
    assert "请让部署方把" not in result["error"]


@pytest.mark.parametrize("size", [1023, 1024])
def test_file_at_or_below_max_bytes_is_imported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size: int,
) -> None:
    state = _state(tmp_path)

    async def sized_payload(*args, **kwargs):
        output = Path(args[args.index("--output") + 1])
        output.write_bytes(b"x" * size)
        return "done", 0, b'{"http_code":200}', b""

    monkeypatch.setattr(fetch, "spawn_and_wait", sized_payload)
    result = _run(
        state,
        url="https://github.com/o/r/releases/download/v1/data.bin",
        kind="file",
        destination=f"data-{size}.bin",
        max_bytes=1024,
    )

    assert result["status"] == "success", result
    assert result["size_bytes"] == size
    assert Path(result["destination"]).stat().st_size == size


def test_file_above_max_bytes_returns_a_typed_nonretryable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path)

    async def oversized_payload(*args, **kwargs):
        output = Path(args[args.index("--output") + 1])
        output.write_bytes(b"x" * 1025)
        return "done", 0, b'{"http_code":200}', b""

    monkeypatch.setattr(fetch, "spawn_and_wait", oversized_payload)
    result = _run(
        state,
        url="https://github.com/o/r/releases/download/v1/large.bin",
        kind="file",
        destination="large.bin",
        max_bytes=1024,
    )

    assert result["status"] == "error"
    assert result["error_code"] == "max_bytes_exceeded"
    assert result["requested_max_bytes"] == 1024
    assert result["max_bytes_source"] == "explicit"
    assert result["actual_size_bytes"] == 1025
    assert result["retryable"] is False
    assert result["retryable_after_change"] is True
    assert "默认 512 MiB" in result["recovery"]
    assert "最高 8 GiB" in result["recovery"]
    assert not (fetch.experiment_output_dir(state, "runtime") / "acquired/large.bin").exists()


def test_curl_max_filesize_error_uses_default_source_and_has_no_hidden_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path)
    calls: list[tuple] = []

    async def max_filesize(*args, **_kwargs):
        calls.append(args)
        return "done", 63, b"", b"curl: (63) Maximum file size exceeded\n"

    monkeypatch.setattr(fetch, "spawn_and_wait", max_filesize)
    result = _run(
        state,
        url="https://github.com/o/r/releases/download/v1/large.bin",
        kind="file",
        destination="default-limit.bin",
    )

    assert result["error_code"] == "max_bytes_exceeded"
    assert result["requested_max_bytes"] == fetch._DEFAULT_MAX_BYTES
    assert result["max_bytes_source"] == "default"
    assert result["retryable"] is False
    assert len(calls) == 1
    assert "--retry" not in calls[0]
    assert "--retry-all-errors" not in calls[0]


def test_tool_dispatch_preserves_omitted_and_explicit_max_bytes_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public dispatcher must not materialize schema defaults into tool kwargs."""
    state = _state(tmp_path)
    calls: list[tuple] = []

    async def max_filesize(*args, **kwargs):
        calls.append((args, kwargs))
        return "done", 63, b"", b"curl: (63) Maximum file size exceeded\n"

    # Some full-suite workers import node tools through the standalone
    # ``tools.*`` bootstrap alias before this canonical package alias.  Patch
    # the executor actually registered at the public boundary so this test
    # exercises dispatch semantics rather than module import order.
    executor = _REGISTRY.executors["fetch_resource"]
    monkeypatch.setitem(executor.__globals__, "spawn_and_wait", max_filesize)
    omitted = asyncio.run(execute_tool(
        "fetch_resource",
        state,
        url="https://github.com/o/r/releases/download/v1/default.bin",
        destination="default.bin",
    ))
    explicit = asyncio.run(execute_tool(
        "fetch_resource",
        state,
        url="https://github.com/o/r/releases/download/v1/explicit.bin",
        destination="explicit.bin",
        max_bytes=fetch._DEFAULT_MAX_BYTES,
    ))

    assert omitted["max_bytes_source"] == "default"
    assert omitted["requested_max_bytes"] == fetch._DEFAULT_MAX_BYTES
    assert explicit["max_bytes_source"] == "explicit"
    assert explicit["requested_max_bytes"] == fetch._DEFAULT_MAX_BYTES
    assert len(calls) == 2


def test_redirect_target_max_filesize_error_is_typed_without_retrying_the_hop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path)
    source = "https://github.com/o/r/releases/download/v1/large.bin"
    target = "https://objects.githubusercontent.com/o/r/large.bin"
    calls: list[tuple] = []

    async def redirect_then_oversize(*args, **kwargs):
        calls.append(args)
        url = args[-1]
        output = Path(args[args.index("--output") + 1])
        if url == source:
            output.write_bytes(b"redirect")
            metadata = {
                "http_code": 302,
                "redirect_url": target,
                "url_effective": source,
            }
            return "done", 0, json.dumps(metadata).encode(), b""
        return "done", 63, b"", b"curl: (63) Maximum file size exceeded\n"

    monkeypatch.setattr(fetch, "spawn_and_wait", redirect_then_oversize)
    result = _run(
        state,
        url=source,
        kind="file",
        destination="redirect-large.bin",
        max_bytes=1024,
    )

    assert result["error_code"] == "max_bytes_exceeded"
    assert result["requested_max_bytes"] == 1024
    assert result["max_bytes_source"] == "explicit"
    assert result["retryable"] is False
    assert len(calls) == 2
    assert [call[-1] for call in calls] == [source, target]


def test_transient_curl_failure_is_retried_without_curl_retry_all_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path)
    calls: list[tuple] = []
    delays: list[float] = []

    async def transient_then_success(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            return "done", 22, b"", b"curl: (22) proxy returned 403\n"
        output = Path(args[args.index("--output") + 1])
        output.write_bytes(b"replayed successfully\n")
        return "done", 0, b'{"http_code":200}', b""

    async def record_backoff(_state, delay_seconds, _deadline):
        delays.append(delay_seconds)

    monkeypatch.setattr(fetch, "spawn_and_wait", transient_then_success)
    monkeypatch.setattr(fetch, "_sleep_for_curl_retry", record_backoff)
    result = _run(
        state,
        url="https://github.com/o/r/releases/download/v1/transient.bin",
        kind="file",
        destination="transient.bin",
        max_bytes=1024,
    )

    assert result["status"] == "success", result
    assert len(calls) == 2
    assert delays == [1.0]
    assert "--retry-all-errors" not in calls[0]


def test_transient_retries_share_one_deadline_and_keep_the_full_first_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path)
    clock = [100.0]
    clock_reads = [0]
    timeouts: list[int] = []
    delays: list[float] = []

    def monotonic_with_launch_overhead():
        clock_reads[0] += 1
        if clock_reads[0] == 2:
            clock[0] += 0.001
        return clock[0]

    async def two_failures_then_success(*args, **kwargs):
        timeouts.append(kwargs["timeout"])
        clock[0] += 2.0
        if len(timeouts) < 3:
            return "done", 28, b"", b"curl: (28) timed out\n"
        output = Path(args[args.index("--output") + 1])
        output.write_bytes(b"eventually available\n")
        return "done", 0, b'{"http_code":200}', b""

    async def advance_backoff(_state, delay_seconds, deadline):
        assert clock[0] + delay_seconds < deadline
        delays.append(delay_seconds)
        clock[0] += delay_seconds

    monkeypatch.setattr(fetch, "monotonic", monotonic_with_launch_overhead)
    monkeypatch.setattr(fetch, "spawn_and_wait", two_failures_then_success)
    monkeypatch.setattr(fetch, "_sleep_for_curl_retry", advance_backoff)
    result = _run(
        state,
        url="https://github.com/o/r/releases/download/v1/deadline.bin",
        destination="deadline.bin",
        max_bytes=1024,
        timeout_seconds=10,
    )

    assert result["status"] == "success", result
    assert timeouts == [10, 7, 3]
    assert delays == [1.0, 2.0]


def test_retry_is_skipped_when_backoff_would_exhaust_the_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path)
    clock = [200.0]
    calls: list[int] = []

    async def late_transient_failure(*_args, **kwargs):
        calls.append(kwargs["timeout"])
        clock[0] += 9.5
        return "done", 28, b"", b"curl: (28) timed out\n"

    async def must_not_backoff(*_args, **_kwargs):
        raise AssertionError("backoff started without enough deadline remaining")

    monkeypatch.setattr(fetch, "monotonic", lambda: clock[0])
    monkeypatch.setattr(fetch, "spawn_and_wait", late_transient_failure)
    monkeypatch.setattr(fetch, "_sleep_for_curl_retry", must_not_backoff)
    result = _run(
        state,
        url="https://github.com/o/r/releases/download/v1/late.bin",
        destination="late.bin",
        timeout_seconds=10,
    )

    assert result["status"] == "error"
    assert calls == [10]


def test_retry_backoff_observes_sticky_run_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path)
    clock = [300.0]
    calls: list[tuple] = []

    async def transient_failure(*args, **kwargs):
        calls.append((args, kwargs))
        return "done", 28, b"", b"curl: (28) timed out\n"

    async def cancel_during_sleep(delay_seconds):
        clock[0] += delay_seconds
        state.hook_state["kill_signal"] = {"reason": "audit stop"}

    monkeypatch.setattr(fetch, "monotonic", lambda: clock[0])
    monkeypatch.setattr(fetch, "spawn_and_wait", transient_failure)
    monkeypatch.setattr(fetch, "async_sleep", cancel_during_sleep)

    with pytest.raises(RunCancelled, match="audit stop"):
        _run(
            state,
            url="https://github.com/o/r/releases/download/v1/cancel.bin",
            destination="cancel.bin",
            timeout_seconds=10,
        )
    assert len(calls) == 1


def test_git_tree_above_max_bytes_uses_the_same_typed_postcheck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path)
    calls: list[tuple] = []

    async def oversized_checkout(*args, **kwargs):
        calls.append((args, kwargs))
        repository = Path(kwargs["writable_roots"][0]) / "repository"
        _create_git_fixture(repository)
        (repository / "large.bin").write_bytes(b"x" * 2048)
        return "done", 0, b"", b""

    monkeypatch.setattr(fetch, "spawn_and_wait", oversized_checkout)
    result = _run(
        state,
        url="https://github.com/o/r.git",
        kind="git",
        destination="large-tree",
        max_bytes=1024,
    )

    assert result["error_code"] == "max_bytes_exceeded"
    assert result["requested_max_bytes"] == 1024
    assert result["max_bytes_source"] == "explicit"
    assert result["actual_size_bytes"] > 1024
    assert result["retryable"] is False
    assert len(calls) == 1
    assert not (
        fetch.experiment_output_dir(state, "runtime") / "acquired/large-tree"
    ).exists()


def test_unlisted_host_is_refused_before_network_and_names_domain_to_add(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path)
    monkeypatch.delenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", raising=False)
    spawned: list[bool] = []

    async def must_not_spawn(*_args, **_kwargs):
        spawned.append(True)
        raise AssertionError("an unlisted host reached the network")

    monkeypatch.setattr(fetch, "spawn_and_wait", must_not_spawn)

    result = _run(state, url="https://downloads.psl.noaa.gov/psl/x.nc",
                  kind="file", destination="x.nc")

    assert result["status"] == "error"
    assert spawned == []
    assert "downloads.psl.noaa.gov 不在" in result["error"]
    assert "HARNESS_SANDBOX_EGRESS_ALLOWLIST" in result["error"]
    assert "request_network_access" in result["error"]
    assert result["request_network_access"]["arguments"]["host"] == (
        "downloads.psl.noaa.gov"
    )
    assert "不要改用其它镜像域名" in result["error"]


def test_allowlist_membership_is_exact_or_subdomain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """判定口径：host==d，或 host 是 d 的子域；后缀伪装不放行。"""
    raw = "github.com,githubusercontent.com"
    monkeypatch.setenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", raw)
    state = _state(tmp_path)
    cases = {
        "https://github.com/o/r": True,
        "https://codeload.github.com/o/r/tar.gz/main": True,   # 子域放行
        "https://raw.githubusercontent.com/o/r/f": True,
        "https://notgithub.com/o": False,                      # 后缀伪装不放行
        "https://github.com.evil.example/o": False,
    }
    for url, expected in cases.items():
        decision = fetch._egress_access_decision(state, url)
        assert decision["allowed"] is expected, (url, decision)


# ── #770 节点侧：联网之前核对域名，重定向与子模块逐跳/逐层核对 ────────────────


def test_dns_failure_on_an_allowlisted_host_is_not_blamed_on_the_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """curl rc=6 是 DNS 解析失败；此前名单外的指引会把它说成「不在白名单」。"""
    state = _state(tmp_path)
    monkeypatch.delenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", raising=False)

    async def dns_failure(*_args, **_kwargs):
        return "done", 6, b"", b"curl: (6) Could not resolve host: github.com\n"

    monkeypatch.setattr(fetch, "spawn_and_wait", dns_failure)
    result = _run(state, url="https://github.com/o/r/archive/v1.tar.gz",
                  kind="file", destination="v1.tar.gz")

    assert result["status"] == "error"
    assert "解析失败" in result["error"]
    assert "不是白名单问题" in result["error"]
    assert "请让部署方把" not in result["error"]


def _redirecting_spawn(calls: list, chain: dict[str, str], payload: bytes):
    """chain: url → 下一跳；不在 chain 里的 url 返回 200 并写出正文。"""

    async def fake_spawn(*args, **kwargs):
        calls.append(args)
        url = args[-1]
        output = Path(args[args.index("--output") + 1])
        if url in chain:
            output.write_bytes(b"redirect body")
            meta = {"http_code": 302, "redirect_url": chain[url], "url_effective": url}
        else:
            output.write_bytes(payload)
            meta = {"http_code": 200, "url_effective": url}
        return "done", 0, json.dumps(meta).encode(), b""

    return fake_spawn


def test_success_result_and_node_events_redact_query_but_argv_keeps_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """节点展示面不落签名 query；真实 curl 参数仍使用完整 URL。"""
    state = _state(tmp_path)
    monkeypatch.setenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", "github.com")
    source = "https://github.com/o/r/archive.tar.gz?token=source-secret"
    target = "https://codeload.github.com/o/r/tar.gz/main?signature=redirect-secret"
    calls: list = []
    monkeypatch.setattr(
        fetch,
        "spawn_and_wait",
        _redirecting_spawn(calls, {source: target}, b"archive payload"),
    )

    result = _run(state, url=source, destination="redacted-success.tar.gz")

    assert result["status"] == "success", result
    assert [call[-1] for call in calls] == [source, target]
    assert result["url"] == "https://github.com/o/r/archive.tar.gz"
    assert result["final_url"] == "https://codeload.github.com/o/r/tar.gz/main"
    assert result["redirects"] == [{
        "from": "https://github.com/o/r/archive.tar.gz",
        "to": "https://codeload.github.com/o/r/tar.gz/main",
        "http_code": 302,
    }]
    public = json.dumps(result, ensure_ascii=False)
    assert "source-secret" not in public
    assert "redirect-secret" not in public

    events = [
        json.loads(line)
        for line in state.transcript_path.read_text(encoding="utf-8").splitlines()
        if '"event": "resource_acquisition_' in line
    ]
    assert {event["event"] for event in events} == {
        "resource_acquisition_started", "resource_acquisition_completed",
    }
    event_text = json.dumps(events, ensure_ascii=False)
    assert "source-secret" not in event_text
    assert "redirect-secret" not in event_text


def test_file_redirect_failure_redacts_signed_url_but_argv_keeps_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path)
    monkeypatch.setenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", "github.com")
    source = "https://github.com/o/r/archive.tar.gz"
    target = "https://codeload.github.com/o/r/tar.gz/main?signature=file-secret"
    calls: list[tuple] = []

    async def fake_spawn(*args, **kwargs):
        calls.append(args)
        requested = args[-1]
        output = Path(args[args.index("--output") + 1])
        if requested == source:
            output.write_bytes(b"redirect")
            return (
                "done", 0,
                json.dumps({
                    "http_code": 302,
                    "redirect_url": target,
                    "url_effective": source,
                }).encode(),
                b"",
            )
        return (
            "done", 22, b"",
            f"curl: failed to fetch {target}\n".encode(),
        )

    async def skip_retry_delay(*_args, **_kwargs):
        return None

    monkeypatch.setattr(fetch, "spawn_and_wait", fake_spawn)
    monkeypatch.setattr(fetch, "_sleep_for_curl_retry", skip_retry_delay)

    result = _run(state, url=source, destination="failed-signed-file.tar.gz")

    assert result["status"] == "error", result
    assert [call[-1] for call in calls] == [source] + [
        target
    ] * fetch._MAX_CURL_ATTEMPTS
    public = json.dumps(result, ensure_ascii=False)
    assert "file-secret" not in public
    assert "https://codeload.github.com/o/r/tar.gz/main" in result["error"]
    transcript = state.transcript_path.read_text(encoding="utf-8")
    assert "file-secret" not in transcript


def test_git_failure_redacts_signed_url_but_argv_keeps_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path)
    monkeypatch.setenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", "github.com")
    source = "https://github.com/o/private.git?token=git-secret"
    calls: list[tuple] = []

    async def fake_spawn(*args, **_kwargs):
        calls.append(args)
        return (
            "done", 128, b"",
            f"fatal: unable to access '{source}': authorization failed\n".encode(),
        )

    monkeypatch.setattr(fetch, "spawn_and_wait", fake_spawn)

    result = _run(state, url=source, kind="git", destination="failed-signed-git")

    assert result["status"] == "error", result
    assert source in calls[0]
    public = json.dumps(result, ensure_ascii=False)
    assert "git-secret" not in public
    assert "https://github.com/o/private.git" in result["error"]
    transcript = state.transcript_path.read_text(encoding="utf-8")
    assert "git-secret" not in transcript


def test_redirects_are_followed_hop_by_hop_within_the_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path)
    monkeypatch.delenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", raising=False)
    source = "https://github.com/o/r/releases/download/v1/x.tar.gz"
    target = "https://objects.githubusercontent.com/o/r/x.tar.gz"
    calls: list = []
    payload = b"release tarball\n"
    monkeypatch.setattr(fetch, "spawn_and_wait",
                        _redirecting_spawn(calls, {source: target}, payload))

    result = _run(state, url=source, kind="file", destination="x.tar.gz")

    assert result["status"] == "success", result
    assert len(calls) == 2
    assert all("--location" not in call for call in calls)
    assert result["redirects"] == [{"from": source, "to": target, "http_code": 302}]
    assert result["final_url"] == target
    assert Path(result["destination"]).read_bytes() == payload


def test_a_redirect_to_an_unlisted_host_is_refused_before_it_is_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path)
    monkeypatch.delenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", raising=False)
    source = "https://github.com/o/r/releases/download/v1/x.tar.gz"
    calls: list = []
    monkeypatch.setattr(fetch, "spawn_and_wait", _redirecting_spawn(
        calls, {source: "https://evil.example.com/x.tar.gz"}, b"never"))

    result = _run(state, url=source, kind="file", destination="x.tar.gz")

    assert result["status"] == "error"
    assert len(calls) == 1, "名单外的那一跳不能被请求"
    assert "evil.example.com" in result["error"]
    assert "重定向" in result["error"]
    assert not (fetch.experiment_output_dir(state, "runtime") / "acquired/x.tar.gz").exists()


def test_deployment_allowed_redirect_origin_is_not_called_unauthorized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path)
    monkeypatch.setenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", "github.com")
    source = "https://github.com/o/r/releases/download/v1/x.tar.gz"
    target = "https://cdn.example.org/x.tar.gz"
    calls: list = []
    monkeypatch.setattr(
        fetch, "spawn_and_wait", _redirecting_spawn(calls, {source: target}, b"never")
    )

    result = _run(state, url=source, destination="redirect-card.tar.gz")

    request = result["request_network_access"]["arguments"]
    assert request["host"] == "cdn.example.org"
    assert "redirect_of" not in request
    assert "部署白名单放行" in request["reason"]
    assert "本次并没有被授权" not in result["error"]


def test_exact_run_grant_fetches_but_redirect_subdomain_still_stops_before_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path)
    monkeypatch.setenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", "mirror.example.net")
    source = "https://data.example.org/archive.nc"
    child = "https://child.data.example.org/archive.nc"
    assert grant_from_answer(state, "data.example.org", "允许", reason="declared input")
    calls: list = []
    monkeypatch.setattr(
        fetch,
        "spawn_and_wait",
        _redirecting_spawn(calls, {source: child}, b"never requested"),
    )

    result = _run(state, url=source, kind="file", destination="archive.nc")

    assert result["status"] == "error", result
    assert result["error_code"] == "network_access_required"
    assert len(calls) == 1, "the exact source grant must not widen to its subdomain"
    request = result["request_network_access"]
    assert request["arguments"]["host"] == "child.data.example.org"
    assert request["arguments"]["redirect_of"] == "data.example.org"
    assert "redirect_of" in request["call"]


def test_exact_run_grant_allows_the_requested_host_to_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path)
    monkeypatch.setenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", "mirror.example.net")
    source = "https://data.example.org/archive.nc"
    assert grant_from_answer(state, source, "允许", reason="declared input")
    calls: list = []
    payload = b"granted data\n"
    monkeypatch.setattr(
        fetch, "spawn_and_wait", _redirecting_spawn(calls, {}, payload)
    )

    result = _run(state, url=source, kind="file", destination="archive.nc")

    assert result["status"] == "success", result
    assert len(calls) == 1
    assert Path(result["destination"]).read_bytes() == payload


def test_resource_fetch_never_writes_the_deployment_allowlist() -> None:
    source = Path(fetch.__file__).read_text(encoding="utf-8")
    for writer in (
        "os.environ[",
        "os.environ.setdefault",
        "os.putenv",
        "environ.update",
    ):
        assert writer not in source, writer


def test_a_redirect_chain_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path)
    monkeypatch.delenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", raising=False)
    chain = {f"https://github.com/hop/{i}": f"https://github.com/hop/{i + 1}"
             for i in range(20)}
    calls: list = []
    monkeypatch.setattr(fetch, "spawn_and_wait", _redirecting_spawn(calls, chain, b"x"))

    result = _run(state, url="https://github.com/hop/0", kind="file", destination="hop")

    assert result["status"] == "error"
    assert "重定向超过" in result["error"]
    assert len(calls) == fetch._MAX_REDIRECT_HOPS + 1


_REAL_SPAWN_AND_WAIT = fetch.spawn_and_wait


def _git_fixture_with_submodules(repository: Path, origin: str, submodules: dict[str, str]) -> None:
    """一个真实 git 仓库：origin 指向源 URL，.gitmodules 声明子模块，索引里有对应 gitlink。

    `git submodule init` 只登记索引里真有 gitlink 的子模块，所以光写 .gitmodules 不够。
    """
    commit = _create_git_fixture(repository)
    subprocess.run(["git", "-C", str(repository), "remote", "add", "origin", origin], check=True)
    for name, url in submodules.items():
        path = f"vendor/{name}"
        for key, value in (("path", path), ("url", url)):
            subprocess.run(["git", "-C", str(repository), "config", "-f", ".gitmodules",
                            f"submodule.{name}.{key}", value], check=True)
        subprocess.run(["git", "-C", str(repository), "update-index", "--add",
                        "--cacheinfo", f"160000,{commit},{path}"], check=True)


def _offline_git_is_real(calls: list, on_network, *, real_sandbox: bool = False):
    """联网的 git 步骤（clone、submodule update）用替身；不联网的步骤交给真实 git 执行。

    相对子模块 URL 怎么解析是这里要钉的事实，只能由真实 git 回答。real_sandbox=True 时
    不联网的步骤走真正的受管沙箱（`spawn_and_wait`），否则直接在测试进程里跑 git。
    """

    async def fake_spawn(*args, **kwargs):
        calls.append((args, kwargs["network_access"]))
        if kwargs["network_access"]:
            return await on_network(args, kwargs)
        assert "clone" not in args and "update" not in args, "联网步骤不得在断网时发起"
        if real_sandbox:
            return await _REAL_SPAWN_AND_WAIT(*args, **kwargs)
        result = subprocess.run(list(args), cwd=kwargs.get("cwd"), capture_output=True, check=False)
        return "done", result.returncode, result.stdout, result.stderr

    return fake_spawn


def _network_calls(calls: list) -> list:
    return [args for args, network in calls if network]


def _clone_with(submodules: dict[str, str], origin: str = "https://github.com/mom-ocean/MOM6.git"):
    async def on_network(args, kwargs):
        assert "clone" in args, args
        assert "--recurse-submodules" not in args
        assert "http.followRedirects=false" in args
        _git_fixture_with_submodules(
            Path(kwargs["writable_roots"][0]) / "repository", origin, submodules)
        return "done", 0, b"", b""
    return on_network


@pytest.mark.parametrize("url", [
    "https://evil.example.com/evil.git",
    # urljoin 会把它算回 github.com，git 实际解析成 evil.example.com（d0541a1e 审查实测）。
    "../../../evil.example.com/x.git",
])
def test_a_submodule_git_would_fetch_from_an_unlisted_host_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str,
) -> None:
    state = _state(tmp_path)
    monkeypatch.delenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", raising=False)
    calls: list = []
    monkeypatch.setattr(fetch, "spawn_and_wait",
                        _offline_git_is_real(calls, _clone_with({"m": url})))

    result = _run(state, url="https://github.com/mom-ocean/MOM6.git",
                  kind="git", recursive=True, destination="mom6-evil-sub")

    assert result["status"] == "error", result
    assert "evil.example.com" in result["error"]
    assert "子模块" in result["error"]
    assert len(_network_calls(calls)) == 1, "只允许 clone 联网，submodule update 不得发出"
    assert not (fetch.experiment_output_dir(state, "runtime") / "acquired/mom6-evil-sub").exists()


def test_allowlisted_submodules_are_fetched_level_by_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path)
    monkeypatch.delenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", raising=False)
    calls: list = []
    cloned = _clone_with({"fms": "../FMS.git"})  # git 解析为 github.com/mom-ocean/FMS.git

    async def on_network(args, kwargs):
        if "clone" in args:
            return await cloned(args, kwargs)
        assert args[-4:] == ("submodule", "update", "--depth", "1"), args
        repository = Path(kwargs["writable_roots"][0]) / "repository"
        (repository / "vendor" / "fms").mkdir(parents=True)
        (repository / "vendor" / "fms" / "fms.F90").write_text("module fms\n", encoding="utf-8")
        return "done", 0, b"", b""

    monkeypatch.setattr(fetch, "spawn_and_wait", _offline_git_is_real(calls, on_network))
    result = _run(state, url="https://github.com/mom-ocean/MOM6.git",
                  kind="git", recursive=True, destination="mom6-with-fms")

    assert result["status"] == "success", result
    assert len(_network_calls(calls)) == 2
    assert (Path(result["destination"]) / "vendor" / "fms" / "fms.F90").exists()


def test_every_checkout_in_a_level_is_checked_before_any_of_the_level_is_fetched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """第二层有两个检出：a 的子模块合规，b 的指向名单外。整层核对不过，谁都不拉。"""
    state = _state(tmp_path)
    monkeypatch.delenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", raising=False)
    calls: list = []
    cloned = _clone_with({"a": "../A.git", "b": "../B.git"})

    async def on_network(args, kwargs):
        if "clone" in args:
            return await cloned(args, kwargs)
        repository = Path(kwargs["writable_roots"][0]) / "repository"
        assert args[args.index("-C") + 1] == str(repository), "第二层不得发起任何 update"
        (repository / "vendor").mkdir(exist_ok=True)
        _git_fixture_with_submodules(repository / "vendor" / "a",
                                     "https://github.com/mom-ocean/A.git",
                                     {"nested": "../Nested.git"})
        _git_fixture_with_submodules(repository / "vendor" / "b",
                                     "https://github.com/mom-ocean/B.git",
                                     {"evil": "https://evil.example.com/x.git"})
        return "done", 0, b"", b""

    monkeypatch.setattr(fetch, "spawn_and_wait", _offline_git_is_real(calls, on_network))
    result = _run(state, url="https://github.com/mom-ocean/MOM6.git",
                  kind="git", recursive=True, destination="mom6-level")

    assert result["status"] == "error", result
    assert "evil.example.com" in result["error"]
    assert len(_network_calls(calls)) == 2, [args for args in _network_calls(calls)]


@pytest.mark.skipif(not __import__("core.sandbox", fromlist=["availability"]).availability()[0],
                    reason="managed sandbox backend is unavailable")
def test_submodule_urls_are_resolved_by_git_inside_the_real_sandbox_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L237：不联网的 git 步骤真的经受管沙箱执行，负向判据落在 git 的真实解析结果上。"""
    state = _state(tmp_path)
    monkeypatch.delenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", raising=False)
    calls: list = []
    monkeypatch.setattr(fetch, "spawn_and_wait", _offline_git_is_real(
        calls, _clone_with({"m": "../../../evil.example.com/x.git"}), real_sandbox=True))

    result = _run(state, url="https://github.com/mom-ocean/MOM6.git",
                  kind="git", recursive=True, destination="mom6-sandboxed")

    assert result["status"] == "error", result
    assert "evil.example.com" in result["error"], result
    offline = [args for args, network in calls if not network]
    assert any("init" in args for args in offline), calls
    assert len(_network_calls(calls)) == 1


# ── 沙箱 git 不读宿主全局配置（第三会话复审 0914c 第九节，首轮复审残余 1）──────────────
#
# 沙箱把宿主 HOME 原样带进去：git 读到 ~/.gitconfig 的 credential.helper=store 就会用上宿主
# 保存的凭据，url.<x>.insteadOf 还会在节点核对 URL 之后改写目标主机。


def _host_git_config_home(root: Path, marker: Path) -> Path:
    home = root / "fake-home"
    home.mkdir(parents=True)
    (home / ".gitconfig").write_text(
        "[credential]\n"
        f"\thelper = !echo called >> {marker}\n"
        '[url "https://evil.example.com/"]\n'
        "\tinsteadOf = https://github.com/\n",
        encoding="utf-8",
    )
    return home


def _git_env(home: Path, **extra: str) -> dict:
    env = {name: value for name, value in os.environ.items()
           if not name.startswith(("GIT_", "SSH_ASKPASS"))}
    return {**env, "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
            "GIT_TERMINAL_PROMPT": "0", **extra}


def test_every_git_spawn_carries_the_environment_that_hides_host_git_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path)
    calls: list = []
    environments: list = []
    inner = _offline_git_is_real(calls, _clone_with({"m": "https://evil.example.com/evil.git"}))

    async def recording(*args, **kwargs):
        environments.append((args, kwargs.get("sandbox_environment")))
        return await inner(*args, **kwargs)

    monkeypatch.setattr(fetch, "spawn_and_wait", recording)
    _run(state, url="https://github.com/mom-ocean/MOM6.git", kind="git",
         recursive=True, destination="mom6-env")

    assert {args[0] for args, _env in environments} == {"git"}
    assert any("clone" in args for args, _env in environments)
    assert any("init" in args for args, _env in environments)
    assert all(env == {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
               for _args, env in environments), environments


def test_the_git_environment_stops_host_credential_helpers_and_url_rewrites(tmp_path: Path) -> None:
    marker = tmp_path / "helper-called"
    home = _host_git_config_home(tmp_path, marker)
    probe = ["git", "config", "--get-regexp", r"^(credential|url)\."]
    fill = ["git", "credential", "fill"]
    request = "protocol=https\nhost=github.com\n\n"

    leaked = subprocess.run(probe, env=_git_env(home), capture_output=True, text=True)
    subprocess.run(fill, input=request, env=_git_env(home), capture_output=True, text=True,
                   timeout=20)
    # 对照：不隔离时，宿主配置读得到、凭据助手被调用。
    assert "credential.helper" in leaked.stdout and "insteadof" in leaked.stdout.lower()
    assert marker.exists()
    marker.unlink()

    isolated_env = _git_env(home, **fetch._GIT_SANDBOX_ENVIRONMENT)
    isolated = subprocess.run(probe, env=isolated_env, capture_output=True, text=True)
    subprocess.run(fill, input=request, env=isolated_env, capture_output=True, text=True,
                   timeout=20)

    assert isolated.returncode == 1 and isolated.stdout == "", isolated
    assert not marker.exists()


@pytest.mark.skipif(not __import__("core.sandbox", fromlist=["availability"]).availability()[0],
                    reason="需要受管沙箱")
def test_the_real_sandbox_applies_the_git_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.sandbox import SandboxLimits

    state = _state(tmp_path)
    staging = fetch.experiment_output_dir(state, "runtime", create=True) / "git-env-probe"
    home = _host_git_config_home(staging, staging / "helper-called")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    limits = SandboxLimits(memory_bytes=256 * 1024**2, cpus=1, pids=32, walltime_seconds=30,
                           storage_bytes=16 * 1024**2, storage_entries=1024,
                           output_bytes=1024**2, tmpfs_bytes=16 * 1024**2)

    async def probe(environment):
        return await _REAL_SPAWN_AND_WAIT(
            "git", "config", "--get-regexp", r"^(credential|url)\.", state=state, timeout=30,
            cwd=str(staging), writable_roots=[staging], readonly_roots=[],
            sandbox_limits=limits, network_access=False, sandbox_environment=environment)

    _status, _rc, leaked, _err = asyncio.run(probe(None))
    status, returncode, isolated, err = asyncio.run(probe(dict(fetch._GIT_SANDBOX_ENVIRONMENT)))

    assert b"credential.helper" in leaked, (leaked, _err)   # 对照：沙箱确实带着宿主 HOME
    assert status == "done" and returncode == 1 and isolated == b"", (status, returncode, err)
