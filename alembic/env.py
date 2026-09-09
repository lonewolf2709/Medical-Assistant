import asyncio
import os
from logging.config import fileConfig

from alembic import context
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import create_async_engine

from app.database import Base
from app.models import *  # noqa: F401,F403 — ensure all models are registered

load_dotenv()

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = os.environ["DATABASE_URL"]
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = create_async_engine(os.environ["DATABASE_URL"])
    async with connectable.connect() as connection:
        await connection.run_sync(
            lambda conn: context.configure(connection=conn, target_metadata=target_metadata)
        )
        async with connection.begin():
            await connection.run_sync(lambda _: context.run_migrations())


def run() -> None:
    if context.is_offline_mode():
        run_migrations_offline()
    else:
        asyncio.run(run_migrations_online())


run()
