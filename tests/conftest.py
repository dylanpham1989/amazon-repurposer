"""Test dùng DB thật (Neon/Postgres local) trên schema tạm, rollback sau mỗi test."""
import contextlib

import pytest

import app.db as dbmod


@pytest.fixture
async def tmpdb():
    """Tạo schema trong 1 schema Postgres tạm rồi xoá sạch."""
    pool = await dbmod.connect()
    schema = "t_" + dbmod.new_id()[:12]
    async with pool.acquire() as c:
        await c.execute(f'CREATE SCHEMA "{schema}"')
        await c.execute(f'SET search_path TO "{schema}"')
        await c.execute(dbmod.SCHEMA)
    # ép mọi connection trong pool dùng schema tạm
    prev = dbmod._pool
    dbmod._pool = await dbmod.asyncpg.create_pool(
        dbmod.settings.pg_dsn, min_size=1, max_size=2, statement_cache_size=0,
        server_settings={"search_path": schema},
    )
    async with dbmod._pool.acquire() as c:
        await c.execute(dbmod.SCHEMA)
    yield dbmod._pool
    await dbmod._pool.close()
    dbmod._pool = prev
    async with pool.acquire() as c:
        await c.execute(f'DROP SCHEMA "{schema}" CASCADE')
    await dbmod.close()


def _db_reachable() -> bool:
    """settings đọc từ .env nên không dựa vào os.environ."""
    import socket
    from urllib.parse import urlparse

    u = urlparse(dbmod.settings.pg_dsn)
    if not u.hostname:
        return False
    with contextlib.suppress(OSError), socket.create_connection(
        (u.hostname, u.port or 5432), timeout=2
    ):
        return True
    return False


def pytest_collection_modifyitems(config, items):
    """Bỏ qua test cần DB nếu không kết nối được Postgres."""
    if _db_reachable():
        return
    skip = pytest.mark.skip(reason="không kết nối được Postgres (xem DATABASE_URL)")
    for item in items:
        if "tmpdb" in getattr(item, "fixturenames", ()):
            item.add_marker(skip)
