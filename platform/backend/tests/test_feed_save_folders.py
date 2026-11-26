"""Private save locations through the API, preserving the original save row."""

from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.feed import FeedEngagement, FeedItem, FeedSaveFolder
from tests import test_feed_endpoints_and_gates as endpoints

feed_client = endpoints.feed_client

ITEM = "aaaaaaaa-0000-4000-8000-000000000001"
SECOND = "aaaaaaaa-0000-4000-8000-000000000002"
BASE = "/api/v1/feed"


async def folder(client, name):
    response = await client.post(BASE + "/folders", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.asyncio
async def test_save_move_filter_and_default_preserve_original_save(feed_client):
    client, db, _current, users = feed_client
    assert (
        await client.post(f"{BASE}/items/{ITEM}/engagement", json={"action": "save"})
    ).status_code == 204
    original = await db.scalar(select(FeedEngagement).where(FeedEngagement.item_id == ITEM))
    original_id, saved_at = original.id, original.created_at
    assert (await client.get(BASE + "/folders")).json() == {"folders": [], "default_count": 1}
    reading, project = await folder(client, " 待读 "), await folder(client, "课题 A")
    assert reading["name"] == "待读"
    assert (
        await client.put(f"{BASE}/items/{ITEM}/save", json={"folder_id": reading["id"]})
    ).status_code == 204
    assert original.id == original_id and original.created_at == saved_at
    assert original.folder_id == reading["id"]
    # A replay from an older client cannot silently reset an explicitly chosen location.
    assert (
        await client.post(f"{BASE}/items/{ITEM}/engagement", json={"action": "save"})
    ).status_code == 204
    assert original.folder_id == reading["id"]
    detail = (await client.get(f"{BASE}/items/{ITEM}")).json()
    assert detail["saved"] and detail["saved_folder_id"] == reading["id"]
    assert (
        await client.get(BASE + "/items", params={"saved": "true", "folder_id": "default"})
    ).json() == []
    items = (
        await client.get(BASE + "/items", params={"saved": "true", "folder_id": reading["id"]})
    ).json()
    assert [item["id"] for item in items] == [ITEM] and items[0]["saved_folder_id"] == reading["id"]
    assert (
        await client.post(f"{BASE}/items/{SECOND}/engagement", json={"action": "save"})
    ).status_code == 204
    assert len((await client.get(BASE + "/items", params={"saved": "true"})).json()) == 2
    assert [
        item["id"]
        for item in (
            await client.get(BASE + "/items", params={"saved": "true", "folder_id": "default"})
        ).json()
    ] == [SECOND]
    counts = (await client.get(BASE + "/folders")).json()
    assert counts["default_count"] == 1
    assert {f["id"]: f["item_count"] for f in counts["folders"]} == {
        reading["id"]: 1,
        project["id"]: 0,
    }
    assert (
        await client.put(f"{BASE}/items/{ITEM}/save", json={"folder_id": project["id"]})
    ).status_code == 204
    assert original.id == original_id and original.created_at == saved_at
    assert (
        await client.put(f"{BASE}/items/{ITEM}/save", json={"folder_id": None})
    ).status_code == 204
    assert original.folder_id is None and original.created_at == saved_at
    await db.commit()
    from app.database import get_session_factory

    async with get_session_factory()() as fresh:
        persisted = await fresh.get(FeedEngagement, original_id)
        assert persisted.user_id == users["me"].id and persisted.folder_id is None
        assert persisted.created_at == saved_at
        assert (await fresh.get(FeedItem, ITEM)).extra == {}


@pytest.mark.asyncio
async def test_folder_ownership_is_private_even_on_the_same_article(feed_client):
    client, db, current, users = feed_client
    mine = await folder(client, "课题 A")
    assert (
        await client.post(
            f"{BASE}/items/{ITEM}/engagement", json={"action": "save", "folder_id": mine["id"]}
        )
    ).status_code == 204
    current["user"] = users["colleague"]
    assert (await client.get(BASE + "/folders")).json() == {"folders": [], "default_count": 0}
    assert (await client.get(f"{BASE}/items/{ITEM}")).json()["saved_folder_id"] is None
    assert (
        await client.post(
            f"{BASE}/items/{ITEM}/engagement", json={"action": "save", "folder_id": mine["id"]}
        )
    ).status_code == 404
    assert (
        await client.get(BASE + "/items", params={"saved": "true", "folder_id": mine["id"]})
    ).status_code == 404
    assert (
        await client.put(f"{BASE}/items/{ITEM}/save", json={"folder_id": mine["id"]})
    ).status_code == 404
    theirs = await folder(client, "课题 A")
    assert theirs["id"] != mine["id"]
    assert (
        await client.post(
            f"{BASE}/items/{ITEM}/engagement", json={"action": "save", "folder_id": theirs["id"]}
        )
    ).status_code == 204
    current["user"] = users["me"]
    assert (await client.get(f"{BASE}/items/{ITEM}")).json()["saved_folder_id"] == mine["id"]
    assert (
        await client.put(f"{BASE}/items/{ITEM}/save", json={"folder_id": theirs["id"]})
    ).status_code == 404
    assert (await db.get(FeedItem, ITEM)).extra == {}
    rows = (await db.scalars(select(FeedEngagement).where(FeedEngagement.item_id == ITEM))).all()
    assert len(rows) == 2 and {row.folder_id for row in rows} == {mine["id"], theirs["id"]}


@pytest.mark.asyncio
async def test_bad_locations_and_unsaved_moves_have_no_side_effect(feed_client):
    client, db, *_ = feed_client
    for name in ["", "  \n", "x" * 81]:
        assert (await client.post(BASE + "/folders", json={"name": name})).status_code == 422
    mine = await folder(client, "待读")
    assert (await client.post(BASE + "/folders", json={"name": " 待读 "})).status_code == 409
    assert len((await db.scalars(select(FeedSaveFolder))).all()) == 1
    assert (
        await client.put(f"{BASE}/items/{ITEM}/save", json={"folder_id": mine["id"]})
    ).status_code == 404
    assert (await client.get(f"{BASE}/items/{ITEM}")).json()["saved"] is False
    for identifier, expected in [("not-a-uuid", 422), (str(uuid4()), 404)]:
        assert (
            await client.post(
                f"{BASE}/items/{ITEM}/engagement", json={"action": "save", "folder_id": identifier}
            )
        ).status_code == expected
        assert (
            await client.get(BASE + "/items", params={"saved": "true", "folder_id": identifier})
        ).status_code == expected
    assert (await client.get(BASE + "/items", params={"folder_id": "default"})).status_code == 422
    assert (
        await client.post(
            f"{BASE}/items/{ITEM}/engagement", json={"action": "open", "folder_id": mine["id"]}
        )
    ).status_code == 422


@pytest.mark.asyncio
async def test_search_papers_save_in_the_selected_folder_without_global_private_data(feed_client):
    client, db, current, users = feed_client
    mine = await folder(client, "文献")
    data = {"title": "A search result", "doi": "10.1234/folder-paper", "folder_id": mine["id"]}
    response = await client.post(BASE + "/saved-papers", json=data)
    assert response.status_code == 201, response.text
    item = response.json()
    assert item["saved"] and item["saved_folder_id"] == mine["id"]
    row = await db.scalar(select(FeedEngagement).where(FeedEngagement.item_id == item["id"]))
    saved_id, saved_at = row.id, row.created_at
    default_data = {k: v for k, v in data.items() if k != "folder_id"}
    assert (await client.post(BASE + "/saved-papers", json=default_data)).json()[
        "saved_folder_id"
    ] == mine["id"]
    assert row.id == saved_id and row.created_at == saved_at
    current["user"] = users["colleague"]
    assert (
        await client.post(BASE + "/saved-papers", json={**data, "doi": "10.1234/rejected"})
    ).status_code == 404
    assert (
        await db.scalar(select(FeedItem.id).where(FeedItem.canonical_key == "doi:10.1234/rejected"))
        is None
    )
    assert (await client.post(BASE + "/saved-papers", json=default_data)).json()[
        "saved_folder_id"
    ] is None
    current["user"] = users["me"]
    assert (await client.delete(f"{BASE}/items/{item['id']}/engagement/save")).status_code == 204
    assert (
        await client.get(BASE + "/items", params={"saved": "true", "folder_id": mine["id"]})
    ).json() == []
