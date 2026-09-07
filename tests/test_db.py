import app.db as dbmod


async def test_placeholder_conversion():
    """Codebase viết `?` kiểu SQLite, Postgres cần $n."""
    assert dbmod._convert("SELECT * FROM t WHERE a=? AND b=?") == \
        "SELECT * FROM t WHERE a=$1 AND b=$2"
    assert dbmod._convert("SELECT 1") == "SELECT 1"


async def test_schema_tables_exist(tmpdb):
    rows = await dbmod.fetch_all(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = current_schema()"
    )
    names = {r["table_name"] for r in rows}
    assert {
        "batches", "products", "images", "html_cache", "templates", "assets", "quota"
    } <= names


async def test_schema_is_idempotent(tmpdb):
    """Chạy app lần 2 không được lỗi schema — mọi lệnh đều IF NOT EXISTS."""
    async with tmpdb.acquire() as conn:
        await conn.execute(dbmod.SCHEMA)
    rows = await dbmod.fetch_all(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = current_schema()"
    )
    assert len({r["table_name"] for r in rows}) >= 7


async def test_cascade_delete(tmpdb):
    b, p = dbmod.new_id(), dbmod.new_id()
    await dbmod.execute("INSERT INTO batches (id) VALUES (?)", (b,))
    await dbmod.execute(
        "INSERT INTO products (id, batch_id, source_url) VALUES (?,?,?)", (p, b, "u")
    )
    await dbmod.execute(
        "INSERT INTO images (id, product_id, position, source_url) VALUES (?,?,?,?)",
        (dbmod.new_id(), p, 0, "i"),
    )
    await dbmod.execute("DELETE FROM batches WHERE id=?", (b,))
    assert await dbmod.fetch_one("SELECT 1 FROM products WHERE id=?", (p,)) is None
    assert await dbmod.fetch_one("SELECT 1 FROM images WHERE product_id=?", (p,)) is None


async def test_image_position_unique(tmpdb):
    import asyncpg
    import pytest

    b, p = dbmod.new_id(), dbmod.new_id()
    await dbmod.execute("INSERT INTO batches (id) VALUES (?)", (b,))
    await dbmod.execute(
        "INSERT INTO products (id, batch_id, source_url) VALUES (?,?,?)", (p, b, "u")
    )
    await dbmod.execute(
        "INSERT INTO images (id, product_id, position, source_url) VALUES (?,?,?,?)",
        (dbmod.new_id(), p, 0, "a"),
    )
    with pytest.raises(asyncpg.UniqueViolationError):
        await dbmod.execute(
            "INSERT INTO images (id, product_id, position, source_url) VALUES (?,?,?,?)",
            (dbmod.new_id(), p, 0, "b"),
        )


async def test_recover_stuck_jobs(tmpdb):
    """App restart giữa batch -> job phải thành failed, không kẹt 'running' mãi."""
    b, p = dbmod.new_id(), dbmod.new_id()
    await dbmod.execute("INSERT INTO batches (id, status) VALUES (?, 'running')", (b,))
    await dbmod.execute(
        "INSERT INTO products (id, batch_id, source_url, status) VALUES (?,?,?, 'running')",
        (p, b, "u"),
    )
    n = await dbmod.recover_stuck_jobs()
    assert n == 1
    row = await dbmod.fetch_one("SELECT status, error FROM products WHERE id=?", (p,))
    assert row["status"] == "failed"
    assert "khởi động lại" in row["error"]
    brow = await dbmod.fetch_one("SELECT status FROM batches WHERE id=?", (b,))
    assert brow["status"] == "failed"


async def test_done_products_untouched_by_recover(tmpdb):
    b, p = dbmod.new_id(), dbmod.new_id()
    await dbmod.execute("INSERT INTO batches (id, status) VALUES (?, 'done')", (b,))
    await dbmod.execute(
        "INSERT INTO products (id, batch_id, source_url, status) VALUES (?,?,?, 'done')",
        (p, b, "u"),
    )
    await dbmod.recover_stuck_jobs()
    prow = await dbmod.fetch_one("SELECT status FROM products WHERE id=?", (p,))
    assert prow["status"] == "done"
