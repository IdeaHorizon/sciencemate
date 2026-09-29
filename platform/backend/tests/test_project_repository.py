"""Git-native Project repository invariants."""

import hashlib
import json
from pathlib import Path, PurePosixPath

import pytest
import yaml

from app.services.project_repository import GitProjectRepository, ProjectRepositoryError


def _repository(tmp_path: Path) -> GitProjectRepository:
    return GitProjectRepository(tmp_path / "repositories", tmp_path / "worktrees")


def _payload_hash(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def test_project_initialization_is_fixed_portable_and_idempotent(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    created = repository.initialize_project(
        project_id="project-1",
        name="Routing safety",
        description="Evaluate robust routing.",
        research_domain="machine learning",
        owner_id="user-1",
    )
    root = Path(created.path)

    assert created.branch == "main"
    assert created.clean is True
    assert len(created.head_commit) == 40
    assert (root / "project.yaml").is_file()
    assert yaml.safe_load((root / "project.yaml").read_text())["schema_version"] == 2
    assert (root / "PROJECT.md").is_file()
    assert (root / "MEMORY.md").is_file()
    # `resources/{registry,environments.lock,secrets.refs}.yaml` 不再在建项目时
    # 写空壳。它们的**唯一**写者是 `project_record`，整份从库里的
    # ProjectResource 行生成。init 时先摆一个 `resources: []` 的壳，等于让同一个
    # 问题有两份答案，而其中一份永远是空的 —— 没有任何代码读过那份壳。
    assert not (root / "resources/registry.yaml").exists()
    assert (root / "access/nodes.yaml").is_file()
    # 节点目录靠 `.gitkeep` 占位。建仓不再写 README 所有权样板：那是这个仓库里
    # 第三份"哪些目录归谁"的名单，而且 orientation hook 本来就把它滤掉
    # （`core.loop_hooks_builtin._BOILERPLATE_README`）—— 框架一边生成一边过滤。
    assert (root / "experiments/.gitkeep").is_file()
    assert not (root / "experiments/README.md").exists()
    assert not (root / "artifacts").exists()
    assert not (root / "memory").exists()
    assert repository.policy_hash("project-1", created.head_commit)
    node_capabilities = yaml.safe_load((root / "access/nodes.yaml").read_text())
    assert node_capabilities["nodes"]["experiment"]["write"] == ["experiments"]

    repeated = repository.initialize_project(
        project_id="project-1",
        name="Ignored on replay",
        description=None,
        research_domain=None,
        owner_id="user-1",
    )
    assert repeated.head_commit == created.head_commit


def test_session_revision_diff_and_linear_publish_preserve_audit_ref(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    project = repository.initialize_project(
        project_id="project-2",
        name="Project",
        description=None,
        research_domain=None,
        owner_id="owner",
    )
    workspace = repository.ensure_session_workspace(
        project_id="project-2",
        session_id="session-1",
        base_commit=project.head_commit,
        title="Write report",
        created_by="owner",
    )
    checksum = repository.content_hash("# Result\n\nEvidence.\n")
    written = repository.write_artifact_revision(
        project_id="project-2",
        session_id="session-1",
        artifact_id="artifact-1",
        artifact_type="analysis_report",
        name="Result",
        content="# Result\n\nEvidence.\n",
        mime_type="text/markdown",
        checksum=checksum,
        version=1,
        actor_id="owner",
        change_set_id="change-1",
        expected_head_commit=workspace.head_commit,
        resource_key="reports/result",
    )
    assert written.commit_sha != workspace.head_commit
    diff = repository.diff(
        project_id="project-2",
        session_id="session-1",
        paths=[written.repository_path],
    )
    assert "+# Result" in diff.patch
    assert diff.additions >= 2

    manifest_path = str(Path(written.repository_path).with_name("manifest.yaml"))
    published = repository.publish_linear(
        project_id="project-2",
        session_id="session-1",
        expected_main_commit=project.head_commit,
        paths=[written.repository_path, manifest_path],
        message="Publish result",
        change_set_id="change-1",
        actor_id="owner",
    )
    status = repository.status("project-2")
    session = repository.session_status("project-2", "session-1", base_commit=published)
    assert status.head_commit == published
    assert session.head_commit == published
    assert session.clean is True
    assert (Path(status.path) / written.repository_path).read_text() == "# Result\n\nEvidence.\n"
    audit_ref = repository._git(
        Path(status.path),
        "show-ref",
        "--verify",
        f"refs/audit/sessions/session-1/{written.commit_sha}",
    )
    assert written.commit_sha in audit_ref
    assert (
        repository.publish_linear(
            project_id="project-2",
            session_id="session-1",
            expected_main_commit=project.head_commit,
            paths=[written.repository_path, manifest_path],
            message="Replay",
            change_set_id="change-1",
            actor_id="owner",
        )
        == published
    )


def test_node_revision_persists_runtime_contract_and_attestation(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    project = repository.initialize_project(
        project_id="project-evidence",
        name="Project",
        description=None,
        research_domain=None,
        owner_id="owner",
    )
    repository.ensure_session_workspace(
        project_id="project-evidence",
        session_id="session-evidence",
        base_commit=project.head_commit,
        title="Write",
        created_by="owner",
    )
    attestation_body = {
        "schema_version": 1,
        "artifact_type": "manuscript",
        "record_sha256": "a" * 64,
        "producer": {"node_type": "writing", "run_id": "run-1"},
    }
    attestation = {
        **attestation_body,
        "attestation_sha256": _payload_hash(attestation_body),
    }
    contract_body = {
        "schema_version": 1,
        "default": "deny",
        "types": {"manuscript": {"owners": ["writing"]}},
    }
    contract = {**contract_body, "contract_sha256": _payload_hash(contract_body)}
    content = "# Manuscript\n"

    written = repository.write_artifact_revision(
        project_id="project-evidence",
        session_id="session-evidence",
        artifact_id="artifact-1",
        artifact_type="manuscript",
        name="Paper",
        content=content,
        mime_type="text/markdown",
        checksum=repository.content_hash(content),
        version=1,
        actor_id="owner",
        change_set_id="change-1",
        expected_head_commit=repository.session_status(
            "project-evidence", "session-evidence"
        ).head_commit,
        owner_node="writing",
        source_attestation=attestation,
        artifact_contract=contract,
    )
    root = Path(repository.session_status("project-evidence", "session-evidence").path)
    assert (root / f".research/contracts/artifacts/{contract['contract_sha256']}.json").is_file()
    assert (root / ".research/attestations/artifact-1" / f"{'a' * 64}.json").is_file()
    manifest = yaml.safe_load(
        (root / Path(written.repository_path).with_name("manifest.yaml")).read_text()
    )
    assert manifest["owner_node"] == "writing"
    assert manifest["source_attestation_sha256"] == attestation["attestation_sha256"]
    assert manifest["artifact_contract_sha256"] == contract["contract_sha256"]


def test_project_documents_and_config_have_canonical_visible_paths(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.initialize_project(
        project_id="project-3",
        name="Project",
        description=None,
        research_domain=None,
        owner_id="owner",
    )
    repository.ensure_session_workspace(
        project_id="project-3",
        session_id="session-3",
        base_commit=None,
        title="Config",
        created_by="owner",
    )
    doc = repository.write_artifact_revision(
        project_id="project-3",
        session_id="session-3",
        artifact_id="doc-1",
        artifact_type="other",
        name="Protocol",
        content="# Protocol\n",
        mime_type="text/markdown",
        checksum=repository.content_hash("# Protocol\n"),
        version=1,
        actor_id="owner",
        change_set_id="change-3",
        expected_head_commit=repository.session_status("project-3", "session-3").head_commit,
        resource_type="project_doc",
        resource_key="project_doc/protocol.md",
    )
    config = repository.write_artifact_revision(
        project_id="project-3",
        session_id="session-3",
        artifact_id="config-1",
        artifact_type="other",
        name="Runtime",
        content="{}",
        mime_type="application/json",
        checksum=repository.content_hash("{}"),
        version=1,
        actor_id="owner",
        change_set_id="change-3",
        expected_head_commit=doc.commit_sha,
        resource_type="project_config",
        resource_key="project_config/runtime",
    )
    assert doc.repository_path == "documents/protocol.md"
    assert config.repository_path == "resources/project-settings/runtime.json"


def test_repository_rejects_path_traversal(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    with pytest.raises(ProjectRepositoryError):
        repository.project_path("../escape")
    with pytest.raises(ProjectRepositoryError):
        repository.candidate_paths(
            artifact_id="artifact",
            artifact_type="other",
            mime_type="text/plain",
            resource_type="project_doc",
            resource_key="project_doc/../../escape",
        )
    with pytest.raises(ProjectRepositoryError):
        repository._safe_relative_path("")


def test_receipt_write_rejects_payload_hash_mismatch(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    project = repository.initialize_project(
        project_id="project-4",
        name="Project",
        description=None,
        research_domain=None,
        owner_id="owner",
    )
    repository.ensure_session_workspace(
        project_id="project-4",
        session_id="session-4",
        base_commit=project.head_commit,
        title="Review",
        created_by="owner",
    )
    with pytest.raises(ProjectRepositoryError, match="does not match"):
        repository.write_receipt(
            project_id="project-4",
            session_id="session-4",
            artifact_id="artifact-4",
            revision_hash="a" * 64,
            receipt_type="review_approval",
            receipt_hash="b" * 64,
            payload={"decision": "approve"},
            actor_id="owner",
            expected_head_commit=repository.session_status("project-4", "session-4").head_commit,
        )


def test_live_diff_and_completed_node_checkpoint_include_small_scripts(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    project = repository.initialize_project(
        project_id="project-workspace",
        name="Workspace Project",
        description=None,
        research_domain=None,
        owner_id="owner",
    )
    session = repository.ensure_session_workspace(
        project_id="project-workspace",
        session_id="session-workspace",
        base_commit=project.head_commit,
        title="Run literature",
        created_by="owner",
    )
    root = Path(session.path)
    session_head = session.head_commit
    relative = "literature/scripts/collect.py"
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("print('collect')\n", encoding="utf-8")

    # The UI can render an uncommitted working-tree diff immediately.
    live = repository.diff(
        project_id="project-workspace",
        session_id="session-workspace",
    )
    assert live.files_changed == 1
    assert relative in live.patch
    assert "+print('collect')" in live.patch
    assert (
        repository.session_status("project-workspace", "session-workspace").head_commit
        == session_head
    )
    listing = repository.file_tree(
        "project-workspace",
        session_id="session-workspace",
        path=str(PurePosixPath(relative).parent),
    )
    live_entry = next(item for item in listing["entries"] if item["path"] == relative)
    assert live_entry == {
        "path": relative,
        "name": PurePosixPath(relative).name,
        "kind": "file",
        "sizeBytes": len("print('collect')\n"),
        "owner": "literature",
        # 记账与否由**这一层**判（`_is_bookkeeping`），不让界面拿路径形状去猜。
        "bookkeeping": False,
        "tracked": False,
        "status": "untracked",
    }

    checkpoint = repository.checkpoint_session_workspace(
        project_id="project-workspace",
        session_id="session-workspace",
        node_type="literature",
        run_id="run-1",
        run_status="completed",
        workspace_prefix="literature",
        paths=[relative],
        expected_head_commit=session_head,
    )

    assert checkpoint.commit_sha
    assert checkpoint.audit_ref is None
    assert repository.changed_paths("project-workspace", "session-workspace") == [relative]
    message = repository._git(root, "show", "-s", "--format=%B", checkpoint.commit_sha)
    assert "Node-Type: literature" in message
    assert "Run-ID: run-1" in message
    assert repository.session_status("project-workspace", "session-workspace").clean is True


def test_incomplete_node_checkpoint_remains_on_session_branch(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    project = repository.initialize_project(
        project_id="project-audit",
        name="Audit Project",
        description=None,
        research_domain=None,
        owner_id="owner",
    )
    session = repository.ensure_session_workspace(
        project_id="project-audit",
        session_id="session-audit",
        base_commit=project.head_commit,
        title="Attempt simulation",
        created_by="owner",
    )
    root = Path(session.path)
    session_head = session.head_commit
    relative = "experiments/simulate.py"
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("raise RuntimeError('failed')\n", encoding="utf-8")

    checkpoint = repository.checkpoint_session_workspace(
        project_id="project-audit",
        session_id="session-audit",
        node_type="experiment",
        run_id="run-failed",
        run_status="incomplete",
        workspace_prefix="experiments",
        paths=[relative],
        expected_head_commit=session_head,
    )

    assert checkpoint.commit_sha
    assert checkpoint.audit_ref is None
    assert repository._git(root, "rev-parse", "HEAD") == checkpoint.commit_sha
    assert "RuntimeError" in repository._git(root, "show", f"{checkpoint.commit_sha}:{relative}")
    assert target.exists()
    assert repository.session_status("project-audit", "session-audit").clean is True


def test_extension_node_is_isolated_by_node_and_run(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    project = repository.initialize_project(
        project_id="project-extension",
        name="Extension Project",
        description=None,
        research_domain=None,
        owner_id="owner",
    )
    session = repository.ensure_session_workspace(
        project_id="project-extension",
        session_id="session-extension",
        base_commit=project.head_commit,
        title="Run extension",
        created_by="owner",
    )
    root = Path(session.path)
    prefix = "runs/extensions/specialist/run-1"
    relative = f"{prefix}/result.md"
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("extension result\n", encoding="utf-8")

    checkpoint = repository.checkpoint_session_workspace(
        project_id="project-extension",
        session_id="session-extension",
        node_type="specialist",
        run_id="run-1",
        run_status="completed",
        workspace_prefix=prefix,
        paths=[relative],
        expected_head_commit=session.head_commit,
    )
    assert checkpoint.commit_sha

    escaped = root / "runs/extensions/specialist/run-2/escape.md"
    escaped.parent.mkdir(parents=True, exist_ok=True)
    escaped.write_text("escape\n", encoding="utf-8")
    with pytest.raises(ProjectRepositoryError, match="does not match"):
        repository.checkpoint_session_workspace(
            project_id="project-extension",
            session_id="session-extension",
            node_type="specialist",
            run_id="run-1",
            run_status="completed",
            workspace_prefix="runs/extensions/specialist/run-2",
            paths=["runs/extensions/specialist/run-2/escape.md"],
            expected_head_commit=checkpoint.commit_sha,
        )


def test_node_checkpoint_rejects_boundary_escape_large_files_and_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(tmp_path)
    project = repository.initialize_project(
        project_id="project-guard",
        name="Guard Project",
        description=None,
        research_domain=None,
        owner_id="owner",
    )
    session = repository.ensure_session_workspace(
        project_id="project-guard",
        session_id="session-guard",
        base_commit=project.head_commit,
        title="Guard",
        created_by="owner",
    )
    root = Path(session.path)
    prefix = "data"
    workspace = root / prefix
    workspace.mkdir(parents=True, exist_ok=True)

    outside = root / "plan/escape.md"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("escape\n", encoding="utf-8")
    with pytest.raises(ProjectRepositoryError, match="owned Project directory"):
        repository.checkpoint_session_workspace(
            project_id="project-guard",
            session_id="session-guard",
            node_type="data",
            run_id="run-guard",
            run_status="completed",
            workspace_prefix=prefix,
            paths=["plan/escape.md"],
            expected_head_commit=session.head_commit,
        )
    from app.services import project_repository as project_repository_module

    monkeypatch.setattr(project_repository_module.settings, "project_git_max_blob_bytes", 8)
    large = workspace / "large.bin"
    large.write_bytes(b"123456789")
    large_diff = repository.diff(
        project_id="project-guard",
        session_id="session-guard",
        paths=[f"{prefix}/large.bin"],
    )
    # 超过 blob 上限的文件在补丁里只是 git 眼里的二进制（`diff.bigFileThreshold`），
    # 正文不会被读出来；读侧不再另扫一遍内容。
    assert "123456789" not in large_diff.patch
    # ⚠️ 超限文件是**排除**，不是抛（2026-08-11 改）。
    #
    # 违规分两类，这里原本混成了一类：越界写 / 符号链接 / 像凭据是**契约
    # 违规**，必须硬拒；文件太大不是违规，是"这个文件不适合进 Git"。
    #
    # 抛出去的代价是整次 checkpoint 全挂：实测一个 150MB 的
    # `prod_T0.80_r1.lammpstrj` 让 experiment 的每一次 checkpoint 都失败 →
    # 9 份 LAMMPS 日志和全部实验产物一次都没进 Git → 恢复会话时全丢。
    # **全损严格劣于丢一个文件。**
    normal = workspace / "keep.md"
    normal.write_text("kept\n", encoding="utf-8")
    checkpoint = repository.checkpoint_session_workspace(
        project_id="project-guard",
        session_id="session-guard",
        node_type="data",
        run_id="run-guard",
        run_status="completed",
        workspace_prefix=prefix,
        paths=[f"{prefix}/large.bin", f"{prefix}/keep.md"],
        expected_head_commit=session.head_commit,
    )
    assert checkpoint.oversized_excluded == ((f"{prefix}/large.bin", 9),), (
        "排除了要显眼报回去 —— 节点得知道该给这个文件登记外部存储"
    )
    assert f"{prefix}/keep.md" in checkpoint.paths, "其余文件必须照常存下来"
    assert f"{prefix}/large.bin" not in checkpoint.paths
    session = repository.session_status("project-guard", "session-guard")

    monkeypatch.setattr(project_repository_module.settings, "project_git_max_blob_bytes", 10_000)
    secret = workspace / "private.pem"
    secret.write_text(
        "-----BEGIN PRIVATE KEY-----\nnot-real\n-----END PRIVATE KEY-----\n", encoding="utf-8"
    )
    secret_diff = repository.diff(
        project_id="project-guard",
        session_id="session-guard",
        paths=[f"{prefix}/private.pem"],
    )
    # 仓库层只管**写边界**（下面 checkpoint 必须拒）；人能看见什么由脱敏层判，
    # 读侧不再对每个改动文件读 1MB 样本扫一遍（那让一次读与工作区体量成正比）。
    from app.services.redaction import DEFAULT_REDACTION_POLICY

    shown = str(DEFAULT_REDACTION_POLICY.sanitize(secret_diff.patch).value)
    assert "not-real" not in shown and "BEGIN PRIVATE KEY" not in shown
    with pytest.raises(ProjectRepositoryError, match="private credential"):
        repository.checkpoint_session_workspace(
            project_id="project-guard",
            session_id="session-guard",
            node_type="data",
            run_id="run-guard",
            run_status="completed",
            workspace_prefix=prefix,
            paths=[f"{prefix}/private.pem"],
            expected_head_commit=session.head_commit,
        )


def test_node_checkpoint_rejects_a_commit_created_outside_platform_authority(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    project = repository.initialize_project(
        project_id="project-authority",
        name="Authority Project",
        description=None,
        research_domain=None,
        owner_id="owner",
    )
    session = repository.ensure_session_workspace(
        project_id="project-authority",
        session_id="session-authority",
        base_commit=project.head_commit,
        title="Authority",
        created_by="owner",
    )
    root = Path(session.path)
    prefix = "paper"
    relative = f"{prefix}/draft.md"
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("untrusted commit\n", encoding="utf-8")
    repository._git(root, "add", "--", relative)
    repository._git(root, "commit", "-m", "node attempted its own commit")

    with pytest.raises(ProjectRepositoryError, match="outside Platform checkpoint authority"):
        repository.checkpoint_session_workspace(
            project_id="project-authority",
            session_id="session-authority",
            node_type="writing",
            run_id="run-authority",
            run_status="completed",
            workspace_prefix=prefix,
            paths=[relative],
            expected_head_commit=session.head_commit,
        )
    with pytest.raises(ProjectRepositoryError, match="outside Platform revision authority"):
        repository.write_artifact_revision(
            project_id="project-authority",
            session_id="session-authority",
            artifact_id="artifact-authority",
            artifact_type="other",
            name="Untrusted",
            content="must not be committed\n",
            mime_type="text/plain",
            checksum=repository.content_hash("must not be committed\n"),
            version=1,
            actor_id="owner",
            change_set_id="change-authority",
            expected_head_commit=session.head_commit,
        )


def test_v1_repository_migrates_to_v2_without_losing_history(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    root = repository.project_path("legacy-project")
    root.mkdir(parents=True)
    repository._git(root, "init", "-b", "main")
    repository._configure_identity(root)
    (root / "PROJECT.yaml").write_text("schema_version: 1\ntitle: Legacy\n")
    (root / "memory/findings").mkdir(parents=True)
    (root / "memory/findings/finding.md").write_text("Stable result: 7\n")
    (root / "artifacts/experiments/run-1").mkdir(parents=True)
    (root / "artifacts/experiments/run-1/content.txt").write_text("legacy evidence\n")
    (root / ".research").mkdir()
    (root / ".research/schema-version").write_text("1\n")
    repository._git(root, "add", "--all")
    repository._git(root, "commit", "-m", "legacy project")

    migrated = repository.initialize_project(
        project_id="legacy-project",
        name="Legacy",
        description="Migrated",
        research_domain="testing",
        owner_id="owner",
    )

    assert yaml.safe_load((root / "project.yaml").read_text())["schema_version"] == 2
    assert "Stable result: 7" in (root / "MEMORY.md").read_text()
    legacy_artifact = root / ".research/legacy/v1/artifacts/experiments/run-1/content.txt"
    assert legacy_artifact.read_text() == "legacy evidence\n"
    assert (root / ".research/migrations/project-v2.json").is_file()
    assert repository._git(root, "rev-list", "--count", "HEAD") == "2"
    assert migrated.clean is True


# ── P6：账本上的冻结事实在 commit 咽喉执法 ─────────────────────────────────
#
# E2E v19 实测的洞：`frozen: true` 只是 metadata，执法散在各工具自查里 ——
# 绕开工具直接改文件、或把产物抄到别处再冻副本，闸就不存在。平台持有 Git
# commit 权，所以执法收到 checkpoint 这唯一咽喉：账本
# （`.research/ledger/records.jsonl`，freeze 时由 harness 追一行）钉死的路径，
# 内容一变就进不了库。记录正文是原生文件（`plan/pre_registration__X.md`），
# 冻结不碰文件 —— 钉的是 `path@sha256`。


def _frozen_project(tmp_path: Path, artifact_body: str = "# H1\n"):
    """建好 project + session，冻结一份记录（原生文件 + 账本 save/freeze 行 + 首次 checkpoint）。"""
    from app.services.harness_contract import ledger_module

    repository = _repository(tmp_path)
    project = repository.initialize_project(
        project_id="p-frozen", name="Frozen", description=None,
        research_domain=None, owner_id="owner",
    )
    session = repository.ensure_session_workspace(
        project_id="p-frozen", session_id="s-frozen",
        base_commit=project.head_commit, title="freeze", created_by="owner",
    )
    root = Path(session.path)
    ledger = ledger_module()
    record = ledger.write_record(
        root, artifact_type="pre_registration", name="X", content=artifact_body,
        directory="plan", produced_by_node_type="hypothesis", produced_by_run_id="r1",
        frozen=True,
    )
    rel = ledger.workspace_store(root).head(record["id"]).path
    assert rel == "plan/pre_registration__X.md"
    assert (root / rel).read_text(encoding="utf-8") == artifact_body
    first = repository.checkpoint_session_workspace(
        project_id="p-frozen", session_id="s-frozen", node_type="hypothesis",
        run_id="r1", run_status="completed", workspace_prefix="plan",
        paths=["plan"], expected_head_commit=session.head_commit,
    )
    assert first.commit_sha, "冻结内容自身的首次入库必须成功"
    assert first.frozen_violations == ()
    return repository, root, rel


def test_frozen_path_mutation_is_restored_at_checkpoint(tmp_path: Path) -> None:
    """v19 场景：冻结后改内容再 checkpoint —— 改动必须被恢复，不许入库。"""
    repository, root, rel = _frozen_project(tmp_path)
    frozen_bytes = (root / rel).read_bytes()

    (root / rel).write_text("# H1 改成了别的\n", encoding="utf-8")
    head = repository.session_status("p-frozen", "s-frozen").head_commit
    checkpoint = repository.checkpoint_session_workspace(
        project_id="p-frozen", session_id="s-frozen", node_type="hypothesis",
        run_id="r2", run_status="completed", workspace_prefix="plan",
        paths=["plan"], expected_head_commit=head,
    )

    assert checkpoint.frozen_violations == ((rel, "restored"),)
    assert (root / rel).read_bytes() == frozen_bytes, "工作区没有恢复成冻结内容"
    assert checkpoint.commit_sha is None, "除篡改外无别的改动，不该产生 commit"


def test_frozen_path_deletion_is_restored_at_checkpoint(tmp_path: Path) -> None:
    """删文件也是改动 —— 冻结的证据不许消失。"""
    repository, root, rel = _frozen_project(tmp_path)
    frozen_bytes = (root / rel).read_bytes()

    (root / rel).unlink()
    head = repository.session_status("p-frozen", "s-frozen").head_commit
    checkpoint = repository.checkpoint_session_workspace(
        project_id="p-frozen", session_id="s-frozen", node_type="hypothesis",
        run_id="r3", run_status="completed", workspace_prefix="plan",
        paths=["plan"], expected_head_commit=head,
    )

    assert checkpoint.frozen_violations == ((rel, "restored"),)
    assert (root / rel).read_bytes() == frozen_bytes


def test_legitimate_sibling_work_still_commits_alongside_a_violation(tmp_path: Path) -> None:
    """守卫只挡冻结路径，不许连坐 —— 同一 checkpoint 里的正常产出照常入库。"""
    from app.services.harness_contract import ledger_module

    repository, root, rel = _frozen_project(tmp_path)

    (root / rel).write_text("# tampered\n", encoding="utf-8")
    # 同一轮里正常存下的另一份记录：正文 plan/research_state__v1.md，没冻。
    ledger_module().write_record(
        root, artifact_type="research_state", name="v1", content="# v1\n",
        directory="plan", produced_by_node_type="hypothesis", produced_by_run_id="r4",
    )
    head = repository.session_status("p-frozen", "s-frozen").head_commit
    checkpoint = repository.checkpoint_session_workspace(
        project_id="p-frozen", session_id="s-frozen", node_type="hypothesis",
        run_id="r4", run_status="completed", workspace_prefix="plan",
        paths=["plan"], expected_head_commit=head,
    )

    assert checkpoint.frozen_violations == ((rel, "restored"),)
    assert checkpoint.commit_sha, "正常产出被连坐了"
    committed = repository._git(root, "show", "--name-only", "--format=", checkpoint.commit_sha)
    assert "research_state__v1.md" in committed
    assert rel not in committed


def test_unfrozen_paths_are_untouched_by_the_guard(tmp_path: Path) -> None:
    """账本没钉的路径随便改 —— 守卫不做任何账本之外的判断。"""
    repository, root, rel = _frozen_project(tmp_path)
    other = root / "plan" / "notes.md"
    other.write_text("草稿，改一百次都行\n", encoding="utf-8")
    head = repository.session_status("p-frozen", "s-frozen").head_commit
    checkpoint = repository.checkpoint_session_workspace(
        project_id="p-frozen", session_id="s-frozen", node_type="hypothesis",
        run_id="r5", run_status="completed", workspace_prefix="plan",
        paths=["plan"], expected_head_commit=head,
    )
    assert checkpoint.frozen_violations == ()
    assert checkpoint.commit_sha


# ── publish_linear 的两条不变量（E2E v19 实测缺失）─────────────────────────


def _publish_fixture(tmp_path: Path):
    repository = _repository(tmp_path)
    project = repository.initialize_project(
        project_id="p-pub", name="Publish", description=None,
        research_domain=None, owner_id="owner",
    )
    session = repository.ensure_session_workspace(
        project_id="p-pub", session_id="s-pub",
        base_commit=project.head_commit, title="work", created_by="owner",
    )
    root = Path(session.path)
    real = "experiments/result.json"
    (root / real).parent.mkdir(parents=True, exist_ok=True)
    (root / real).write_text('{"tg": 0.44}', encoding="utf-8")
    repository.checkpoint_session_workspace(
        project_id="p-pub", session_id="s-pub", node_type="experiment",
        run_id="r1", run_status="completed", workspace_prefix="experiments",
        paths=["experiments"], expected_head_commit=session.head_commit,
    )
    return repository, project, real


def test_publish_skips_paths_that_exist_nowhere(tmp_path: Path) -> None:
    """change set 里可能有既不在 Session 也不在 canonical 的路径。

    E2E v19 实测：模型误造的重复 experiment_log 被清掉后，那条路径两边都没有；
    旧实现照样把它塞进 `git add`，得到 `fatal: did not match any files`，
    **整个 publish 死掉**。它语义上是 no-op，本就不该进 add 列表。
    """
    repository, project, real = _publish_fixture(tmp_path)

    commit = repository.publish_linear(
        project_id="p-pub", session_id="s-pub",
        expected_main_commit=project.head_commit,
        paths=[real, "experiments/deleted_and_never_published.json"],
        message="publish", change_set_id="cs-1", actor_id="owner",
    )

    assert commit
    repo_root = repository.project_path("p-pub")
    assert (repo_root / real).is_file(), "真实产物没被发布"
    names = repository._git(repo_root, "show", "--name-only", "--format=", commit)
    assert "deleted_and_never_published.json" not in names


def test_a_failed_publish_leaves_canonical_clean(tmp_path: Path) -> None:
    """发布必须原子。

    旧实现失败时把已拷入的文件留在 canonical 里 → 此后**每一次** publish 都
    撞 "Canonical Project worktree is not clean"，项目永久发不出去（v19 实测）。
    """
    repository, project, real = _publish_fixture(tmp_path)
    repo_root = repository.project_path("p-pub")

    original_git = repository._git

    def _explode(cwd, *args, **kwargs):
        if args and args[0] == "commit":
            raise ProjectRepositoryError("simulated commit failure")
        return original_git(cwd, *args, **kwargs)

    repository._git = _explode
    with pytest.raises(ProjectRepositoryError):
        repository.publish_linear(
            project_id="p-pub", session_id="s-pub",
            expected_main_commit=project.head_commit,
            paths=[real], message="publish", change_set_id="cs-2", actor_id="owner",
        )
    repository._git = original_git

    assert repository._git(repo_root, "status", "--porcelain") == "", (
        "失败后 canonical 仓不干净 —— 后续 publish 会被永久堵死")

    # 而且清干净之后，重试必须能成功（证明回滚没把仓弄坏）
    commit = repository.publish_linear(
        project_id="p-pub", session_id="s-pub",
        expected_main_commit=project.head_commit,
        paths=[real], message="publish retry", change_set_id="cs-3", actor_id="owner",
    )
    assert commit
    assert (repo_root / real).is_file()


def _seed_session(tmp_path: Path, name: str):
    repository = _repository(tmp_path)
    project = repository.initialize_project(
        project_id=f"proj-{name}", name=name, description=None,
        research_domain=None, owner_id="owner",
    )
    session = repository.ensure_session_workspace(
        project_id=f"proj-{name}", session_id=f"sess-{name}",
        base_commit=project.head_commit, title=name, created_by="owner",
    )
    return repository, session


def _checkpoint(repository, name, expected, *, path="data/out.md", body="x\n"):
    root = Path(repository.session_status(f"proj-{name}", f"sess-{name}").path)
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return repository.checkpoint_session_workspace(
        project_id=f"proj-{name}", session_id=f"sess-{name}",
        node_type="data", run_id=f"run-{body.strip()}", run_status="completed",
        workspace_prefix="data", paths=[path], expected_head_commit=expected,
    )


def test_a_stale_expectation_self_heals_when_every_commit_is_platform_made(
    tmp_path: Path,
) -> None:
    """真实事故回放（2026-08-13 E2E v26）。

    平台 14:14 自己提交了一个 checkpoint，但把新 head 写进 DB 的那次事务没能
    提交（checkpoint 与 db.commit() 之间的 SSE 推送在客户端断开时会抛）。
    19:50 下一次 checkpoint 拿着**陈旧的** expected 一比，把平台自己那条提交
    判成外人 → fail-closed → 整个 turn 炸 → session failed，而且再也自己好不了。
    那时 8.9 小时的模拟还在跑，跑完时已经没有任何东西在看着它。

    判据现在只看 Git 里能重新读出来的事实：节点没有提交能力，所以中间每一条
    都是平台做的 → DB 只是落后了 → 采纳磁盘 HEAD 继续。
    """
    repository, session = _seed_session(tmp_path, "heal")

    first = _checkpoint(repository, "heal", session.head_commit, body="first\n")
    assert first.commit_sha

    # DB 没记住 first.commit_sha —— 调用方仍拿着上一个 head
    second = _checkpoint(
        repository, "heal", session.head_commit, path="data/out2.md", body="second\n"
    )
    assert second.commit_sha, "陈旧的期望值不该让平台拒绝自己的提交"
    assert second.commit_sha != first.commit_sha


def test_a_foreign_commit_is_still_refused_and_named(tmp_path: Path) -> None:
    """containment 没放松：不是平台做的提交，照旧拒绝 —— 且**指名是哪条**。"""
    repository, session = _seed_session(tmp_path, "foreign")
    root = Path(session.path)

    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "data/sneak.md").write_text("sneak\n", encoding="utf-8")
    repository._git(root, "add", "--all")
    repository._git(
        root, "-c", "user.name=Someone Else", "-c", "user.email=someone@elsewhere",
        "commit", "-m", "hand-rolled commit",
    )

    (root / "data/out.md").write_text("x\n", encoding="utf-8")
    with pytest.raises(ProjectRepositoryError, match="outside Platform checkpoint authority"):
        repository.checkpoint_session_workspace(
            project_id="proj-foreign", session_id="sess-foreign",
            node_type="data", run_id="run-1", run_status="completed",
            workspace_prefix="data", paths=["data/out.md"],
            expected_head_commit=session.head_commit,
        )


def test_a_rewritten_history_is_refused_even_if_the_committer_looks_right(
    tmp_path: Path,
) -> None:
    """最严重的形态：已提交的东西被人动过。这时**不看提交者是谁**，一律拒绝。"""
    repository, session = _seed_session(tmp_path, "rewrite")
    first = _checkpoint(repository, "rewrite", session.head_commit, body="first\n")
    root = Path(repository.session_status("proj-rewrite", "sess-rewrite").path)

    repository._git(root, "reset", "--hard", session.head_commit)
    (root / "data/other.md").write_text("other\n", encoding="utf-8")
    repository._git(root, "add", "--all")
    repository._git(root, "commit", "-m", "replacement\n\nSession-ID: sess-rewrite")

    (root / "data/out.md").write_text("x\n", encoding="utf-8")
    with pytest.raises(ProjectRepositoryError, match="outside Platform checkpoint authority"):
        repository.checkpoint_session_workspace(
            project_id="proj-rewrite", session_id="sess-rewrite",
            node_type="data", run_id="run-2", run_status="completed",
            workspace_prefix="data", paths=["data/out.md"],
            expected_head_commit=first.commit_sha,
        )


def test_another_sessions_platform_commit_is_not_mine_to_adopt(tmp_path: Path) -> None:
    """平台身份不够 —— 还得是**这个会话**的提交。否则跨会话的 head 漂移会被
    悄悄吞掉，而那正是两个驱动同时写一棵树的信号。"""
    repository, session = _seed_session(tmp_path, "xsession")
    root = Path(session.path)
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "data/other.md").write_text("other\n", encoding="utf-8")
    repository._git(root, "add", "--all")
    repository._git(root, "commit", "-m", "checkpoint\n\nSession-ID: some-other-session")

    (root / "data/out.md").write_text("x\n", encoding="utf-8")
    with pytest.raises(ProjectRepositoryError, match="outside Platform checkpoint authority"):
        repository.checkpoint_session_workspace(
            project_id="proj-xsession", session_id="sess-xsession",
            node_type="data", run_id="run-1", run_status="completed",
            workspace_prefix="data", paths=["data/out.md"],
            expected_head_commit=session.head_commit,
        )


def test_a_dependency_tree_sized_checkpoint_commits_instead_of_dying(tmp_path: Path) -> None:
    """路径数量不设上限 —— 2026-08-22 事故的类级回归。

    模型 `pip install --target` 把 5792 个依赖文件装进工作区，当时的
    `len(paths) > 2_000 → raise` 让整轮科研陪葬，且重试必然复发。判据：
    "文件太多"跟"文件太大"同类（产物形态问题，不是契约违规），如实入库 +
    见证，绝不全损。argv 上限由分批消化，这里的量要越过旧上限才有意义。
    """
    repository, session = _seed_session(tmp_path, "bigcp")
    root = Path(session.path)
    paths = []
    deps = root / "data" / "pylibs"
    for index in range(2_100):
        bucket = deps / f"pkg_{index % 40:02d}"
        bucket.mkdir(parents=True, exist_ok=True)
        target = bucket / f"module_{index:04d}.py"
        target.write_text(f"VALUE = {index}\n", encoding="utf-8")
        paths.append(str(target.relative_to(root)))

    checkpoint = repository.checkpoint_session_workspace(
        project_id="proj-bigcp", session_id="sess-bigcp",
        node_type="data", run_id="run-big", run_status="completed",
        workspace_prefix="data", paths=paths,
        expected_head_commit=session.head_commit,
    )

    assert checkpoint.commit_sha
    committed = repository._git(
        root, "diff-tree", "--no-commit-id", "--name-only", "-r", checkpoint.commit_sha
    ).splitlines()
    assert len([p for p in committed if p.startswith("data/pylibs/")]) == 2_100
    assert repository.session_status("proj-bigcp", "sess-bigcp").clean is True


def test_argv_chunker_preserves_every_path_in_order() -> None:
    """分批是实现细节：不丢、不重、不乱序，且每批都在字节预算内。"""
    from app.services.project_repository import _chunk_paths_for_argv

    paths = [f"data/deps/pkg_{index:05d}/module.py" for index in range(1_000)]
    chunks = list(_chunk_paths_for_argv(paths, max_bytes=1_000))
    assert len(chunks) > 1
    assert [path for chunk in chunks for path in chunk] == paths
    assert all(
        sum(len(path.encode("utf-8")) + 1 for path in chunk) <= 1_000 for chunk in chunks
    )
    # 单条超预算的路径独占一批，而不是被丢掉。
    oversized = ["x" * 2_000]
    assert list(_chunk_paths_for_argv(oversized, max_bytes=1_000)) == [oversized]


def test_a_directory_over_the_checkpoint_budget_is_excluded_whole_and_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """数据集的单位是目录：整目录超预算就整目录不入库，其余照常，且要报回去。

    2026-09-09 node20：observation 把日志 tar 解成 4492 个各自不大的文件，逐文件
    上限看不见它，checkpoint 照单全收 → 会话分支比 main 多 9048 个文件，此后每一次
    "这个会话改了什么"都跟它成正比。
    """
    from app.services import project_repository as project_repository_module

    repository = _repository(tmp_path)
    project = repository.initialize_project(
        project_id="project-bulk", name="Bulk", description=None,
        research_domain=None, owner_id="owner",
    )
    session = repository.ensure_session_workspace(
        project_id="project-bulk", session_id="session-bulk",
        base_commit=project.head_commit, title="Bulk", created_by="owner",
    )
    root = Path(session.path)
    prefix = "data"
    extracted = root / prefix / "extracted"
    extracted.mkdir(parents=True)
    for index in range(20):
        (extracted / f"log_{index}.txt").write_text("x" * 100, encoding="utf-8")
    (root / prefix / "analysis.py").write_text("print('ok')\n", encoding="utf-8")
    (root / prefix / "small").mkdir()
    (root / prefix / "small" / "note.md").write_text("fine\n", encoding="utf-8")

    monkeypatch.setattr(project_repository_module.settings, "project_git_max_tree_bytes", 1_000)
    paths = [f"{prefix}/extracted/log_{i}.txt" for i in range(20)] + [
        f"{prefix}/analysis.py", f"{prefix}/small/note.md",
    ]
    checkpoint = repository.checkpoint_session_workspace(
        project_id="project-bulk", session_id="session-bulk", node_type="data",
        run_id="run-bulk", run_status="completed", workspace_prefix=prefix,
        paths=paths, expected_head_commit=session.head_commit,
    )
    assert checkpoint.bulk_excluded == ((f"{prefix}/extracted", 2_000, 20),), (
        "超预算的目录要整个报回去：路径、字节、文件数"
    )
    assert f"{prefix}/analysis.py" in checkpoint.paths
    assert f"{prefix}/small/note.md" in checkpoint.paths, "没超预算的子目录照常入库"
    assert not any(p.startswith(f"{prefix}/extracted/") for p in checkpoint.paths)
    assert checkpoint.commit_sha, "其余文件必须照常提交，不许全损"
