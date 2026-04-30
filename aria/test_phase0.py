"""Phase 0 verification test — run with: python test_phase0.py"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


async def test() -> None:
    from core.database import init_all_databases, state_db, memory_db, journal_db

    data_dir = Path("data")
    data_dir.mkdir(exist_ok=True)

    await init_all_databases(
        data_dir / "state.db",
        data_dir / "memory.db",
        data_dir / "journal.db",
    )

    print("=== Phase 0 Verification ===\n")

    # 1. WAL mode on all DBs
    print("[1] WAL mode check")
    for name, db in [("state", state_db), ("memory", memory_db), ("journal", journal_db)]:
        row = await db.fetchone("PRAGMA journal_mode")
        mode = row[0]
        status = "PASS" if mode == "wal" else "FAIL"
        print(f"  {name}.db journal_mode = {mode!r} [{status}]")
        assert mode == "wal", f"WAL not set on {name}.db!"

    # 2. schema_versions present and populated
    print("\n[2] Schema versions")
    for name, db in [("state", state_db), ("memory", memory_db), ("journal", journal_db)]:
        rows = await db.fetchall("SELECT version, description FROM schema_versions ORDER BY version")
        for row in rows:
            print(f"  {name}.db v{row['version']}: {row['description']}")
        assert len(rows) >= 1, f"No migrations found in {name}.db!"
    print("  [PASS]")

    # 3. FTS5 round-trip test
    print("\n[3] FTS5 insert + search round-trip")
    await memory_db.execute(
        "DELETE FROM facts_fts WHERE rowid = (SELECT rowid FROM facts WHERE key = ?)",
        ("__fts_test__",),
    )
    await memory_db.execute(
        "INSERT OR REPLACE INTO facts(key, value, source) VALUES (?, ?, ?)",
        ("__fts_test__", "hello fts5 world testing", "test"),
    )
    await memory_db.execute(
        "INSERT INTO facts_fts(rowid, key, value) VALUES "
        "((SELECT rowid FROM facts WHERE key = ?), ?, ?)",
        ("__fts_test__", "__fts_test__", "hello fts5 world testing"),
    )
    await memory_db.commit()

    result = await memory_db.fetchall(
        "SELECT key, value FROM facts_fts WHERE facts_fts MATCH ?",
        ("fts5",),
    )
    assert len(result) > 0, "FTS5 search returned no results!"
    print(f"  Search for 'fts5' returned {len(result)} row(s): key={result[0]['key']!r}")
    print("  [PASS]")

    # Cleanup
    await memory_db.execute(
        "DELETE FROM facts_fts WHERE rowid = (SELECT rowid FROM facts WHERE key = ?)",
        ("__fts_test__",),
    )
    await memory_db.execute("DELETE FROM facts WHERE key = ?", ("__fts_test__",))
    await memory_db.commit()

    # 4. Idempotency — run init again, count schema_versions rows
    print("\n[4] Idempotency (re-running init_all_databases)")
    await init_all_databases(
        data_dir / "state.db",
        data_dir / "memory.db",
        data_dir / "journal.db",
    )
    rows = await state_db.fetchall("SELECT version FROM schema_versions")
    print(f"  state.db schema_versions rows after 2nd run: {len(rows)}")
    assert len(rows) == 1, f"Expected 1 migration row, got {len(rows)} — migrations ran twice!"
    print("  [PASS]")

    # 5. Interrupted task recovery: insert a 'running' task, re-init, check status
    print("\n[5] Interrupted task recovery")
    await state_db.execute(
        "INSERT OR REPLACE INTO tasks(id, title, status) VALUES (?, ?, ?)",
        ("t_test_interrupt", "Test interruption recovery", "running"),
    )
    await state_db.commit()
    await init_all_databases(
        data_dir / "state.db",
        data_dir / "memory.db",
        data_dir / "journal.db",
    )
    row = await state_db.fetchone("SELECT status FROM tasks WHERE id = ?", ("t_test_interrupt",))
    assert row is not None and row["status"] == "interrupted", (
        f"Expected 'interrupted', got {row['status'] if row else None!r}"
    )
    print(f"  Task status after recovery: {row['status']!r} [PASS]")
    await state_db.execute("DELETE FROM tasks WHERE id = ?", ("t_test_interrupt",))
    await state_db.commit()

    # Close
    await state_db.close()
    await memory_db.close()
    await journal_db.close()

    print("\n" + "=" * 40)
    print("ALL PHASE 0 CHECKS PASSED")
    print("=" * 40)


if __name__ == "__main__":
    asyncio.run(test())
