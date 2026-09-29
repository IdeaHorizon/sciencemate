"""Export the latest paper draft from the database to a file.

Usage: PYTHONPATH=. python scripts/export_paper.py
"""
import asyncio

from sqlalchemy import text
from app.database import get_session_factory

PROJECT_ID = "acf20d0c-0c58-45f8-80fb-338b4295cd94"
OUTPUT_PATH = "/Users/wddddds/Desktop/Work/Projects/Agent_research_platform/backend/output/mlip_paper_draft_v2.md"

async def main():
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            text("""
                SELECT name, extra_data->>'_content' as content, created_at
                FROM artifacts
                WHERE project_id = :pid AND type = 'paper_draft'
                ORDER BY created_at DESC
                LIMIT 1
            """),
            {"pid": PROJECT_ID},
        )
        row = result.fetchone()
        if not row:
            print("No paper_draft artifact found.")
            return

        print(f"Found: {row.name} (created {row.created_at})")
        print(f"Content length: {len(row.content)} chars")

        import os
        os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
        with open(OUTPUT_PATH, "w") as f:
            f.write(row.content)

        print(f"Exported to: {OUTPUT_PATH}")

asyncio.run(main())
