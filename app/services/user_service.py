from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User


async def get_or_create_user(db: AsyncSession, telegram_id: str) -> User:
    result = await db.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(telegram_id=telegram_id)
        db.add(user)
        await db.commit()
        await db.refresh(user)
    return user
