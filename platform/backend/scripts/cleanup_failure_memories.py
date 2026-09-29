"""One-shot cleanup: archive memory_entries that are execution-failure events.

These should never have been persisted as long-term memory — they belong
in ops logs. This script finds them by pattern match on content and sets
status=archived (not deletion — preserves audit trail).

Usage:
    PYTHONPATH=. python scripts/cleanup_failure_memories.py --dry-run
    PYTHONPATH=. python scripts/cleanup_failure_memories.py --apply
    PYTHONPATH=. python scripts/cleanup_failure_memories.py --apply --project-id <UUID>
"""
import argparse
import asyncio
import logging
import re
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("cleanup_failure_memories")

FAILURE_PATTERNS = (
    re.compile(r"Node\s+\w+\s+execution\s+failed", re.IGNORECASE),
    re.compile(r"Budget\s+exceeded:", re.IGNORECASE),
    re.compile(r"LLM\s+call\s+failed", re.IGNORECASE),
    re.compile(r"Tool\s+call\s+failed:", re.IGNORECASE),
    re.compile(r"timeout\s+exceeded", re.IGNORECASE),
)


def is_failure_event(content: str) -> bool:
    return any(p.search(content or "") for p in FAILURE_PATTERNS)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="Actually archive (default: dry-run)")
    parser.add_argument("--dry-run", action="store_true", help="Default mode")
    parser.add_argument("--project-id", default=None,
                        help="Limit to a single project (default: all)")
    args = parser.parse_args()
    apply = args.apply

    from sqlalchemy import select
    from app.database import get_session_factory
    from app.models.memory import MemoryEntry

    factory = get_session_factory()
    async with factory() as db:
        stmt = select(MemoryEntry).where(MemoryEntry.status == "active")
        if args.project_id:
            stmt = stmt.where(MemoryEntry.project_id == args.project_id)
        result = await db.execute(stmt)
        all_entries = list(result.scalars().all())

        matched = [e for e in all_entries if is_failure_event(e.content or "")]
        logger.info(
            "Scanned %d active memories; %d match failure patterns.",
            len(all_entries), len(matched),
        )

        # Group by project for reporting
        from collections import Counter
        per_project: Counter = Counter()
        for e in matched:
            per_project[str(e.project_id) if e.project_id else "<no_project>"] += 1
        for pid, n in per_project.most_common(10):
            logger.info("  project=%s : %d failure memories", pid[:8] if pid != "<no_project>" else pid, n)

        # Show samples
        for e in matched[:5]:
            logger.info("  sample: [%s] %s", e.layer, str(e.content)[:120])

        if not apply:
            logger.info("(dry-run) — pass --apply to archive these memories.")
            return

        # Apply: set status=archived
        for e in matched:
            e.status = "archived"
        await db.commit()
        logger.info("Archived %d failure-event memories.", len(matched))


if __name__ == "__main__":
    asyncio.run(main())
