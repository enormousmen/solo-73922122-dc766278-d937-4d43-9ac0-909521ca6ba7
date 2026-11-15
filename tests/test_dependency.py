"""脚本依赖核查分析器的单元测试。"""
from __future__ import annotations

import pytest

from app.dependency import analyze_dependencies, split_statements
from app.errors import DependencyRejected
from app.validation import validate_database


@pytest.fixture()
def cyclic_schema(cyclic_db):
    return cyclic_db, validate_database(str(cyclic_db))


def test_split_statements_and_positions():
    pieces = split_statements(
        "SELECT 1;\n-- 注释\n  SELECT 2; /* x */ SELECT ';' AS s;"
    )
    assert len(pieces) == 3
    assert pieces[0][1:] == (1, 1)
    assert pieces[1][1] == 3          # 第二段在第 3 行
    assert pieces[1][2] == 3          # 行首两个空格之后
    assert pieces[2][1] == 3          # 块注释与第二段同行
    assert pieces[2][2] == 21         # 块注释 "/* x */ " 之后


def test_unterminated_statement_rejected():
    with pytest.raises(DependencyRejected, match="结束分号") as exc:
        split_statements("SELECT 1; SELECT ('unterminated'")
    assert exc.value.line == 1
    assert exc.value.column == 11


def test_select_reads_tables_and_columns(cyclic_schema):
    db, schema = cyclic_schema
    report = analyze_dependencies(
        str(db),
        schema,
        "SELECT * FROM dept; SELECT id FROM employee WHERE dept_id = 1;",
    )
    s1, s2 = report.statements
    assert s1.kind == "select" and s1.write_tables == []
    assert s1.read_tables == ["dept"]
    assert s2.read_tables == ["employee"]
    assert {c.column for c in s2.reads} == {"dept_id", "id"}
    assert all(not c.implicit for c in s2.reads)
    # 两个纯查询之间不存在冲突边，可同批并行
    assert report.edges == []
    assert report.batches == [[1, 2]]


def test_with_subquery_supported(cyclic_schema):
    db, schema = cyclic_schema
    report = analyze_dependencies(
        str(db),
        schema,
        "WITH recent AS (SELECT id, dept_id FROM employee) "
        "SELECT * FROM recent JOIN dept ON dept.id = recent.dept_id; "
        "SELECT 1;",
    )
    s1 = report.statements[0]
    assert s1.kind == "select"
    # CTE 名 recent 不得出现在表清单里，真实表 dept/employee 都被读取
    assert s1.read_tables == ["dept", "employee"]
    assert "recent" not in s1.read_tables


def test_insert_implicit_parent_reads(cyclic_schema):
    db, schema = cyclic_schema
    report = analyze_dependencies(
        str(db),
        schema,
        "INSERT INTO project (id, owner_id, dept_id) VALUES (200, 10, 1); "
        "SELECT * FROM dept;",
    )
    s1 = report.statements[0]
    assert s1.kind == "insert"
    assert s1.write_tables == ["project"]
    # 外键约束：插入 project 时隐含读取 employee(id) 与 dept(id)
    assert sorted(s1.implicit_tables) == ["dept", "employee"]
    implicit_pairs = {(c.table, c.column) for c in s1.reads if c.implicit}
    assert ("dept", "id") in implicit_pairs
    assert ("employee", "id") in implicit_pairs


def test_self_reference_implicit_read(self_ref_db):
    schema = validate_database(str(self_ref_db))
    report = analyze_dependencies(
        str(self_ref_db),
        schema,
        "INSERT INTO node (id, parent_id, label) VALUES (9, 1, 'x'); "
        "SELECT id FROM node;",
    )
    s1 = report.statements[0]
    # 自引用外键 node.parent_id -> node(id)：node 同时是写表与隐含读表
    assert s1.write_tables == ["node"]
    assert ("node", "id") in {(c.table, c.column) for c in s1.reads if c.implicit}
    # 同表写-读：第二段查询必须排在第一段之后
    edge = next(e for e in report.edges if e.table == "node")
    assert (edge.upstream, edge.downstream, edge.conflict) == (1, 2, "write-read")


def test_delete_parent_implicit_child_reads(cyclic_schema):
    db, schema = cyclic_schema
    report = analyze_dependencies(
        str(db),
        schema,
        "DELETE FROM dept WHERE id = 2; SELECT * FROM employee;",
    )
    s1 = report.statements[0]
    assert s1.kind == "delete"
    assert s1.write_tables == ["dept"]
    # 子表 employee/project 的外键列被隐含读取以做 NO ACTION/RESTRICT 检查
    pairs = {(c.table, c.column) for c in s1.reads if c.implicit}
    assert ("employee", "dept_id") in pairs
    assert ("project", "dept_id") in pairs


def test_update_non_fk_column_has_no_implicit_reads(cyclic_schema):
    db, schema = cyclic_schema
    # 更新普通非键列 name 不触发任何外键检查（WHERE 中的 id 是显式读取）
    report = analyze_dependencies(
        str(db),
        schema,
        "UPDATE dept SET name = 'x' WHERE id = 1; SELECT * FROM dept;",
    )
    s1 = report.statements[0]
    assert s1.kind == "update"
    assert s1.write_tables == ["dept"]
    assert s1.implicit_tables == []
    assert all(not c.implicit for c in s1.reads)


def test_update_key_column_triggers_implicit_reads(cyclic_schema):
    db, schema = cyclic_schema
    report = analyze_dependencies(
        str(db),
        schema,
        "UPDATE employee SET dept_id = 2 WHERE id = 10; SELECT * FROM dept;",
    )
    s1 = report.statements[0]
    # 更新外键列 -> 隐含读父表 dept(id)；employee 有子表 project 与自引用
    assert "dept" in s1.implicit_tables
    assert ("dept", "id") in {(c.table, c.column) for c in s1.reads if c.implicit}


def test_write_read_and_write_write_edges_in_submission_order(cyclic_schema):
    db, schema = cyclic_schema
    report = analyze_dependencies(
        str(db),
        schema,
        "INSERT INTO dept (id, name) VALUES (9, 'x'); "
        "SELECT * FROM dept; "
        "UPDATE dept SET name = 'y' WHERE id = 9;",
    )
    by_table = [e for e in report.edges if e.table == "dept"]
    # 同表：1->2 写读、1->3 写写（同表双方写不再重复计写读）、2->3 写读
    assert {(e.upstream, e.downstream, e.conflict) for e in by_table} == {
        (1, 2, "write-read"),
        (1, 3, "write-write"),
        (2, 3, "write-read"),
    }
    # 严格串行：每批只有一条
    assert report.batches == [[1], [2], [3]]


def test_parallel_batches_independent_statements(cyclic_schema):
    db, schema = cyclic_schema
    report = analyze_dependencies(
        str(db),
        schema,
        "INSERT INTO dept (id, name) VALUES (9, 'x'); "
        "SELECT * FROM project; "
        "SELECT * FROM employee;",
    )
    # 第 2、3 段只读，且与第 1 段写表 dept 无重叠 -> 三条全部可并行
    assert report.edges == []
    assert report.batches == [[1, 2, 3]]


def test_transaction_statements_have_no_table_access(cyclic_schema):
    db, schema = cyclic_schema
    report = analyze_dependencies(
        str(db),
        schema,
        "BEGIN; SELECT * FROM dept; COMMIT;",
    )
    kinds = [s.kind for s in report.statements]
    assert kinds == ["transaction", "select", "transaction"]
    txn = report.statements[0]
    assert txn.read_tables == [] and txn.write_tables == []
    assert report.statements[2].sql.strip().upper().startswith("COMMIT")
    # 事务语句不产生任何边
    assert all(e.table == "dept" for e in report.edges)


def test_savepoint_forms_accepted(cyclic_schema):
    db, schema = cyclic_schema
    report = analyze_dependencies(
        str(db),
        schema,
        "SAVEPOINT sp1; ROLLBACK TO SAVEPOINT sp1; RELEASE sp1;",
    )
    assert all(s.kind == "transaction" for s in report.statements)
    assert report.edges == []
    assert report.batches == [[1, 2, 3]]


def test_ddl_rejected_with_position(cyclic_schema):
    db, schema = cyclic_schema
    with pytest.raises(DependencyRejected) as exc:
        analyze_dependencies(
            str(db),
            schema,
            "SELECT 1; CREATE TABLE x (id INTEGER PRIMARY KEY);",
        )
    assert exc.value.statement_index == 2
    assert exc.value.line == 1


def test_unknown_table_rejected_no_partial_schedule(cyclic_schema):
    db, schema = cyclic_schema
    with pytest.raises(DependencyRejected, match="无法判定") as exc:
        analyze_dependencies(
            str(db),
            schema,
            "SELECT * FROM dept; SELECT * FROM ghost;",
        )
    assert exc.value.statement_index == 2


def test_attach_and_pragma_rejected(cyclic_schema):
    db, schema = cyclic_schema
    with pytest.raises(DependencyRejected, match="ATTACH"):
        analyze_dependencies(str(db), schema, "SELECT 1; ATTACH ':memory:' AS x;")
    with pytest.raises(DependencyRejected, match="PRAGMA"):
        analyze_dependencies(str(db), schema, "SELECT 1; PRAGMA cache_size=100;")


def test_statement_count_limits(cyclic_schema):
    db, schema = cyclic_schema
    with pytest.raises(DependencyRejected, match="2～4 段"):
        analyze_dependencies(str(db), schema, "SELECT 1;")
    with pytest.raises(DependencyRejected, match="2～4 段"):
        analyze_dependencies(
            str(db),
            schema,
            " ".join(f"SELECT {i};" for i in range(5)),
        )


def test_replace_and_upsert_classified_as_writes(cyclic_schema):
    db, schema = cyclic_schema
    report = analyze_dependencies(
        str(db),
        schema,
        "REPLACE INTO dept (id, name) VALUES (1, 'z'); "
        "INSERT INTO dept (id, name) VALUES (8, 'q') "
        "ON CONFLICT(id) DO UPDATE SET name = 'q2';",
    )
    assert report.statements[0].kind == "replace"
    assert report.statements[0].write_tables == ["dept"]
    assert report.statements[1].write_tables == ["dept"]
    # 写写冲突，必须串行
    assert any(e.conflict == "write-write" for e in report.edges)


def test_insert_select_reads_source_and_writes_target(cyclic_schema):
    db, schema = cyclic_schema
    report = analyze_dependencies(
        str(db),
        schema,
        "INSERT INTO employee (id, dept_id, mentor_id) "
        "SELECT id + 50, dept_id, mentor_id FROM employee; "
        "SELECT * FROM employee;",
    )
    s1 = report.statements[0]
    assert s1.kind == "insert"
    assert s1.write_tables == ["employee"]
    assert "employee" in s1.read_tables  # SELECT 源表
    assert "dept" in s1.implicit_tables  # 外键隐含读父表
    # 同表写后读
    assert any(
        e.upstream == 1 and e.downstream == 2 and e.conflict == "write-read"
        for e in report.edges
    )


def test_analysis_does_not_modify_database(cyclic_schema):
    import hashlib

    db, schema = cyclic_schema
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    analyze_dependencies(
        str(db),
        schema,
        "DELETE FROM dept WHERE id = 1; UPDATE employee SET dept_id = 2; "
        "INSERT INTO project (id, owner_id, dept_id) VALUES (9, 10, 1); "
        "SELECT * FROM dept;",
    )
    after = hashlib.sha256(db.read_bytes()).hexdigest()
    assert before == after


def test_multi_fk_insert_reports_every_parent(cyclic_schema):
    db, schema = cyclic_schema
    report = analyze_dependencies(
        str(db),
        schema,
        "INSERT INTO project (id, owner_id, dept_id) VALUES (300, 10, 1); "
        "SELECT 1;",
    )
    s1 = report.statements[0]
    # project 有两条外键：owner_id -> employee、dept_id -> dept，均须隐含读取
    assert ("dept", "id") in {(c.table, c.column) for c in s1.reads if c.implicit}
    assert ("employee", "id") in {(c.table, c.column) for c in s1.reads if c.implicit}


def test_readonly_connection_rejects_physical_write(cyclic_schema):
    # 即使绕过 EXPLAIN 直接尝试写，只读 URI 连接也会在连接层拒绝
    import sqlite3

    db, _schema = cyclic_schema
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.execute("PRAGMA foreign_keys=ON")
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("DELETE FROM dept")
    conn.close()


def test_foreign_key_check_failure_still_rejected(tmp_path):
    # 依赖核查沿用原有库约束：外键违规库同样被拒绝
    import sqlite3

    from app.errors import ValidationError

    path = tmp_path / "badfk.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE a (id INTEGER PRIMARY KEY);
        CREATE TABLE b (id INTEGER PRIMARY KEY, a_id INTEGER REFERENCES a(id));
        INSERT INTO a VALUES (1);
        INSERT INTO b VALUES (1, 99);
        """
    )
    conn.commit()
    conn.close()
    with pytest.raises(ValidationError, match="外键违规"):
        validate_database(str(path))
