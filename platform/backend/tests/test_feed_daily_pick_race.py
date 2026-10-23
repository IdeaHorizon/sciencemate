from datetime import UTC, datetime
import pytest
from sqlalchemy import select,func
from app.models.user import User
from app.models.feed import FeedDailyPick
from app.services.feed import ranking


@pytest.mark.asyncio
async def test_concurrent_insert_winner_is_reused_without_duplicate_key(db_session,monkeypatch):
    user=User(email='picks-race@test.local',display_name='Test',hashed_password='x')
    db_session.add(user)
    await db_session.flush()
    today=datetime.now(UTC).date()
    winner=[{'item_id':'winner','reason':'Already chosen','project_id':None}]
    db_session.add(FeedDailyPick(user_id=user.id,pick_date=today,picks=winner))
    await db_session.flush()
    original_get=db_session.get
    async def stale_get(model,ident,**kwargs):
        if model is FeedDailyPick:
            return None  # The other request inserted after this request's read.
        return await original_get(model,ident,**kwargs)
    monkeypatch.setattr(db_session,'get',stale_get)
    assert await ranking.daily_picks(db_session,user_id=user.id,scored=[])==winner
    assert await db_session.scalar(select(func.count()).select_from(FeedDailyPick))==1


@pytest.mark.asyncio
async def test_refresh_replaces_picks_without_deleting_the_row(db_session):
    user=User(email='picks-refresh@test.local',display_name='Test',hashed_password='x')
    db_session.add(user)
    await db_session.flush()
    today=datetime.now(UTC).date()
    db_session.add(FeedDailyPick(user_id=user.id,pick_date=today,picks=[{'item_id':'old'}]))
    await db_session.flush()
    assert await ranking.daily_picks(db_session,user_id=user.id,scored=[],refresh=True)==[]
    assert await db_session.scalar(select(func.count()).select_from(FeedDailyPick))==1
