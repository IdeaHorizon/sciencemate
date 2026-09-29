"""公开树 = 内部树 − 专业版 − 内部件 − 数据，由 `scripts/package/export_public_tree.py` 机械生成。

这里守三件事：排除表指的位置都真的存在（表烂了要红）、按表算出来的公开文件里没有一个
import 专业版（核心指着专业版，或者表漏了一处）、真导出一次之后树里没有专业版的目录、
接缝换成了空桩。导出树里三套测试能不能过，由 CI 的 `public-tree` job 真跑。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]


def _the_export_module():
    path = REPO / "scripts" / "package" / "export_public_tree.py"
    spec = importlib.util.spec_from_file_location("export_public_tree", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _this_is_the_internal_tree() -> bool:
    """公开树里专业版目录整个不在 —— 那是导出的结果，不是排除表烂了。"""
    return (REPO / "platform" / "backend" / "app" / "pro").is_dir()


def test_every_excluded_location_exists() -> None:
    """排除表指的每一处都在：搬走了就更新表，别让表烂着。"""
    if not _this_is_the_internal_tree():
        pytest.skip("公开树：排除表指的位置本来就不在")
    x = _the_export_module()
    missing = [rule for rule in x.EXCLUDED if not (REPO / rule.rstrip("/")).exists()]
    assert not missing, f"排除表里这些位置不存在了：{missing}"


def test_no_public_file_imports_the_pro_edition() -> None:
    x = _the_export_module()
    hits = x.what_still_imports_the_pro_edition(REPO, x.public_files())
    assert not hits, "这些公开文件还 import 专业版：\n  " + "\n  ".join(hits)


def test_the_pro_edition_is_wholly_excluded() -> None:
    """每一个专业版目录下的每一个跟踪文件都被排除；一个都没漏。"""
    if not _this_is_the_internal_tree():
        pytest.skip("公开树：没有专业版可排除")
    x = _the_export_module()
    public = set(x.public_files())
    for rule in x.PROFESSIONAL:
        inside = [p for p in x.tracked_files() if p == rule or (rule.endswith("/") and p.startswith(rule))]
        assert inside, f"专业版的位置 {rule} 下一个跟踪文件都没有 —— 表烂了？"
        assert not (set(inside) & public), f"{rule} 下有文件进了公开树"


def test_an_export_has_no_pro_edition_and_a_stub_seam(tmp_path: Path) -> None:
    x = _the_export_module()
    into = tmp_path / "public"
    files = x.export(into)
    for gone in ("platform/backend/app/pro", "platform/backend/tests/pro", "platform/frontend/src/pro",
                 "platform/frontend/src/app/login", "platform/frontend/src/app/register",
                 "platform/frontend/src/app/(workspace)/organisation", "deploy/org", ".gitea",
                 "docs/RFC_ORGANISATION_PAGE_20260923.md", "docs/EXEC_PLAN_TWO_EDITIONS_20260916.md", "docs-html"):
        assert not (into / gone).exists(), f"导出树里还有 {gone}"
    # docs/ 整体不进，但代码和闸读的登记表要在（KEPT_INSIDE_EXCLUDED）。
    assert (into / "docs/verdict_demolition").is_dir()
    assert (into / "docs/compression-fate-table.md").is_file()
    assert (into / x.SEAM).read_text(encoding="utf-8") == x.SEAM_STUB
    assert (into / "platform/backend/app/assembly.py").exists()
    assert (into / "platform/frontend/src/features/auth/slots.ts").exists()
    assert not x.what_still_imports_the_pro_edition(into, files)
    assert len(files) == len(x.public_files())


def test_git_init_makes_a_repository(tmp_path: Path) -> None:
    x = _the_export_module()
    into = tmp_path / "public"
    x.export(into)
    sha = x.make_it_a_repository(into)
    assert len(sha) >= 7
    assert (into / ".git").is_dir()
