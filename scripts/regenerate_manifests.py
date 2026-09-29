"""CLI 包装：重新生成 ORG_MANIFEST.md + 所有 project 的 PROJECT_MANIFEST.md。

通常 curator dreaming 跑完后自动 trigger 这个；user 也可以手动跑。

用法：
    python scripts/regenerate_manifests.py                  # 全部
    python scripts/regenerate_manifests.py --project <id>   # 仅一个项目
    python scripts/regenerate_manifests.py --org-only       # 仅 org manifest
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.manifest import regenerate_all, write_org_manifest, write_project_manifest


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", type=str, help="只重新生成该 project 的 manifest")
    ap.add_argument("--org-only", action="store_true", help="只生成 ORG_MANIFEST.md")
    args = ap.parse_args()

    if args.org_only:
        p = write_org_manifest()
        print(f"✅ wrote {p}")
        return 0
    if args.project:
        p = write_project_manifest(args.project)
        print(f"✅ wrote {p}")
        return 0

    result = regenerate_all()
    print(f"✅ org: {result['org_manifest']}")
    print(f"✅ {len(result['project_manifests'])} project manifests:")
    for p in result["project_manifests"]:
        print(f"  - {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
