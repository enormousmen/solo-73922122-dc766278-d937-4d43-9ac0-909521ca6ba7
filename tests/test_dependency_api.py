"""脚本依赖核查接口的端到端测试。"""
from __future__ import annotations

import hashlib
import os

import pytest

os.environ.setdefault("DATA_DIR", "/tmp/reducer-test-data")

from fastapi.testclient import TestClient  # noqa: E402

from app.config import DATA_DIR  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def cyclic_bytes(client):
    return client.get("/static/sample_cyclic.db").content


def _post(client, db_bytes, statements=None, script_text=""):
    data = {}
    if statements is not None:
        for s in statements:
            data.setdefault("statements", []).append(s)
    return client.post(
        "/api/dependency-check",
        files={"db": ("cyclic.db", db_bytes, "application/x-sqlite3")},
        data=data or {"script_text": script_text},
    )


def test_dependency_check_happy_path(client, cyclic_bytes):
    r = _post(
        client,
        cyclic_bytes,
        statements=[
            "INSERT INTO dept (id, name) VALUES (9, 'x');",
            "SELECT * FROM dept;",
            "DELETE FROM project WHERE id = 100;",
            "SELECT * FROM employee;",
        ],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["statement_count"] == 4
    assert len(body["statements"]) == 4
    # 第一段插入 dept 无外键父表，读表为空；第二段读 dept 形成写读边
    assert body["statements"][0]["write_tables"] == ["dept"]
    conflicts = {(e["upstream"], e["downstream"], e["table"], e["conflict"])
                 for e in body["edges"]}
    assert (1, 2, "dept", "write-read") in conflicts
    # 批次覆盖全部语句且保持提交顺序
    flattened = [i for batch in body["batches"] for i in batch]
    assert sorted(flattened) == [1, 2, 3, 4]
    for batch in body["batches"]:
        assert batch == sorted(batch)


def test_dependency_check_implicit_fk_reads_in_response(client, cyclic_bytes):
    r = _post(
        client,
        cyclic_bytes,
        statements=[
            "INSERT INTO project (id, owner_id, dept_id) VALUES (200, 10, 1);",
            "SELECT 1;",
        ],
    )
    assert r.status_code == 200
    s1 = r.json()["statements"][0]
    assert "dept" in s1["implicit_tables"] and "employee" in s1["implicit_tables"]
    assert any(c["implicit"] and c["table"] == "dept" for c in s1["reads"])


def test_dependency_check_textarea_fallback(client, cyclic_bytes):
    r = _post(
        client,
        cyclic_bytes,
        script_text="SELECT * FROM dept; SELECT * FROM employee;",
    )
    assert r.status_code == 200
    body = r.json()
    assert body["statement_count"] == 2
    # 两个纯查询无冲突，同批并行
    assert body["edges"] == []
    assert body["batches"] == [[1, 2]]


def test_dependency_check_transaction_only(client, cyclic_bytes):
    r = _post(
        client,
        cyclic_bytes,
        statements=["BEGIN;", "COMMIT;"],
    )
    assert r.status_code == 200
    body = r.json()
    assert all(s["kind"] == "transaction" for s in body["statements"])
    assert body["batches"] == [[1, 2]]


def test_reject_wrong_statement_count(client, cyclic_bytes):
    r = _post(client, cyclic_bytes, statements=["SELECT * FROM dept;"])
    assert r.status_code == 400
    assert "2～4 段" in r.json()["detail"]

    r = _post(
        client,
        cyclic_bytes,
        statements=[f"SELECT {i};" for i in range(5)],
    )
    assert r.status_code == 400
    assert "2～4 段" in r.json()["detail"]


def test_reject_ddl_returns_position_and_no_schedule(client, cyclic_bytes):
    r = _post(
        client,
        cyclic_bytes,
        statements=["SELECT 1;", "DROP TABLE dept;"],
    )
    assert r.status_code == 400
    body = r.json()
    assert body["statement_index"] == 2
    assert body["line"] == 2 and body["column"] == 1
    assert "batches" not in body  # 无法判定时不输出部分排程


def test_reject_unknown_table(client, cyclic_bytes):
    r = _post(
        client,
        cyclic_bytes,
        statements=["SELECT * FROM dept;", "SELECT * FROM ghost;"],
    )
    assert r.status_code == 400
    body = r.json()
    assert body["statement_index"] == 2
    assert "无法判定" in body["detail"]


def test_reject_attach(client, cyclic_bytes):
    r = _post(
        client,
        cyclic_bytes,
        statements=["SELECT 1;", "ATTACH ':memory:' AS x;"],
    )
    assert r.status_code == 400
    assert "ATTACH" in r.json()["detail"]
    assert r.json()["statement_index"] == 2


def test_reject_non_sqlite_upload(client):
    r = client.post(
        "/api/dependency-check",
        files={"db": ("bad.db", b"not a database", "application/octet-stream")},
        data={"statements": ["SELECT 1;", "SELECT 2;"]},
    )
    assert r.status_code == 400
    assert "SQLite" in r.json()["detail"]


def test_reject_schema_violating_db(client, tmp_path):
    import sqlite3

    path = tmp_path / "four.db"
    conn = sqlite3.connect(str(path))
    for i in range(4):
        conn.execute(f"CREATE TABLE t{i} (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    r = client.post(
        "/api/dependency-check",
        files={"db": ("four.db", path.read_bytes(), "application/x-sqlite3")},
        data={"statements": ["SELECT 1;", "SELECT 2;"]},
    )
    assert r.status_code == 400
    assert "3 张" in r.json()["detail"]


def test_uploaded_db_never_modified(client, cyclic_bytes, tmp_path):
    db_path = tmp_path / "upload.db"
    db_path.write_bytes(cyclic_bytes)
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()

    r = _post(
        client,
        cyclic_bytes,
        statements=[
            "DELETE FROM dept WHERE id = 1;",
            "UPDATE employee SET dept_id = 2;",
            "INSERT INTO project (id, owner_id, dept_id) VALUES (9, 10, 1);",
            "SELECT * FROM dept;",
        ],
    )
    assert r.status_code == 200, r.text
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before
    # 临时工作目录已清理
    assert not list(DATA_DIR.glob("dep_*"))


def test_reducer_entry_still_works_alongside(client, cyclic_bytes):
    # 原有缩减入口保持可用
    r = client.post(
        "/api/jobs",
        files={"db": ("cyclic.db", cyclic_bytes, "application/x-sqlite3")},
        data={"script_text": "INSERT INTO dept (id, name) VALUES (1, 'x');"},
    )
    assert r.status_code == 202
