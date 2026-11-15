"""脚本依赖核查：只分析、不运行，识别每条语句的读/写表并构建可并行批次。

判定原理（全程不执行任何用户语句，原库只读打开）：

1. 用 ``sqlite3.complete_statement`` 按提交顺序切出 2~4 条完整语句；
2. 事务控制语句（BEGIN/COMMIT/ROLLBACK/SAVEPOINT/RELEASE）按关键字直接确认，
   不接触任何表；
3. 查询与增删改语句通过 ``EXPLAIN <stmt>`` 做**语句准备**而不执行，准备期
   安装的 authorizer 会上报 SQLITE_READ/INSERT/UPDATE/DELETE 事件；
4. 连接强制 ``PRAGMA foreign_keys=ON``，此时 authorizer 会原生上报**外键约束
   引起的隐含读取**（插入/更新时读父表主键、删除/更新父表时读子表外键列，
   自引用外键同样计入）；同时根据 schema 的外键定义独立推导一份隐含读取，
   两者合并，避免不同 SQLite 版本行为差异；
5. 按提交顺序，对读写同一表（write-read）或写写同一表（write-write）的
   两条语句建立先后边，再做拓扑分层得到“层内无冲突、可并行”的批次。

任何一条语句不在允许范围或无法判定，都抛出 :class:`DependencyRejected`
并给出语句序号与行列位置，此时不输出任何部分排程。
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from .errors import DependencyRejected
from .script_guard import _BANNED_PATTERNS
from .validation import Schema

# 允许的语句首关键字（查询 / 增删改现有表数据 / 事务控制）
_QUERY_VERBS = frozenset({"SELECT", "VALUES", "WITH"})
_WRITE_VERBS = frozenset({"INSERT", "UPDATE", "DELETE", "REPLACE"})
_TXN_VERBS = frozenset({"BEGIN", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE"})
_ALLOWED_VERBS = _QUERY_VERBS | _WRITE_VERBS | _TXN_VERBS

# 事务控制语句的完整形态（不经过 EXPLAIN，直接按形态确认，避免任何执行语义）
_TXN_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE | re.DOTALL)
    for p in (
        r"BEGIN(\s+(?:DEFERRED|IMMEDIATE|EXCLUSIVE))?(\s+TRANSACTION)?\s*;?\s*$",
        r"COMMIT(\s+TRANSACTION)?\s*;?\s*$",
        r"END(\s+TRANSACTION)?\s*;?\s*$",
        r"ROLLBACK(\s+TRANSACTION)?\s*;?\s*$",
        r"ROLLBACK\s+TO(\s+SAVEPOINT)?\s+[A-Za-z_][A-Za-z0-9_]*\s*;?\s*$",
        r"SAVEPOINT\s+[A-Za-z_][A-Za-z0-9_]*\s*;?\s*$",
        r"RELEASE(\s+SAVEPOINT)?\s+[A-Za-z_][A-Za-z0-9_]*\s*;?\s*$",
    )
)


@dataclass(frozen=True)
class ColumnAccess:
    table: str
    column: str | None = None
    implicit: bool = False  # True 表示由外键约束引起的隐含读取（仅读列使用）


@dataclass
class StatementAccess:
    index: int                      # 1 起始的提交序号
    sql: str
    kind: str                       # select / insert / update / delete / replace / transaction
    reads: list[ColumnAccess] = field(default_factory=list)
    writes: list[ColumnAccess] = field(default_factory=list)
    read_tables: list[str] = field(default_factory=list)
    write_tables: list[str] = field(default_factory=list)
    implicit_tables: list[str] = field(default_factory=list)
    line: int = 1
    column: int = 1

    def to_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "sql": self.sql.strip(),
            "kind": self.kind,
            "line": self.line,
            "column": self.column,
            "reads": [
                {"table": c.table, "column": c.column, "implicit": c.implicit}
                for c in self.reads
            ],
            "writes": [
                {"table": c.table, "column": c.column} for c in self.writes
            ],
            "read_tables": self.read_tables,
            "write_tables": self.write_tables,
            "implicit_tables": self.implicit_tables,
        }


@dataclass
class DependencyEdge:
    upstream: int       # 必须先执行的语句序号
    downstream: int     # 依赖前者的语句序号
    table: str
    conflict: str       # write-read / write-write

    def to_dict(self) -> dict[str, object]:
        return {
            "upstream": self.upstream,
            "downstream": self.downstream,
            "table": self.table,
            "conflict": self.conflict,
        }


@dataclass
class DependencyReport:
    statements: list[StatementAccess]
    edges: list[DependencyEdge]
    batches: list[list[int]]

    def to_dict(self) -> dict[str, object]:
        return {
            "statement_count": len(self.statements),
            "statements": [s.to_dict() for s in self.statements],
            "edges": [e.to_dict() for e in self.edges],
            "batches": self.batches,
        }


# --------------------------------------------------------------------------- #
# 语句切分与位置
# --------------------------------------------------------------------------- #

def _skip_whitespace_and_comments(script: str, pos: int) -> int:
    """跳过空白与 SQL 注释（行注释 --、块注释 / * * /），返回下一个有效位置。"""
    n = len(script)
    while pos < n:
        ch = script[pos]
        if ch.isspace():
            pos += 1
        elif ch == "-" and pos + 1 < n and script[pos + 1] == "-":
            nl = script.find("\n", pos)
            pos = n if nl == -1 else nl + 1
        elif ch == "/" and pos + 1 < n and script[pos + 1] == "*":
            close = script.find("*/", pos + 2)
            pos = n if close == -1 else close + 2
        else:
            break
    return pos


def split_statements(script: str) -> list[tuple[str, int, int]]:
    """按 SQLite 完整语句规则切分，返回 (语句文本, 起始行, 起始列)。

    纯注释/空白段跳过；位置基于原始脚本文本计算，用于错误定位。
    """
    pieces: list[tuple[str, int, int]] = []
    n = len(script)
    start = _skip_whitespace_and_comments(script, 0)
    while start < n:
        line = script.count("\n", 0, start) + 1
        last_nl = script.rfind("\n", 0, start)
        column = start - last_nl  # 上一个换行之后的 1 起始偏移
        end = start + 1
        while end <= n and not sqlite3.complete_statement(script[start:end]):
            end += 1
        if end > n:
            raise DependencyRejected(
                "语句缺少结束分号或引号未闭合，无法判定完整语句。",
                line=line,
                column=column,
            )
        piece = script[start:end]
        if piece.strip():
            pieces.append((piece, line, column))
        start = _skip_whitespace_and_comments(script, end)
    return pieces


def _first_verb(sql: str) -> tuple[str, str]:
    """返回 (首关键字大写, 去掉前导注释/空白后的语句文本)。"""
    text = sql.lstrip()
    # 跳过前导 SQL 行注释与块注释
    while True:
        if text.startswith("--"):
            nl = text.find("\n")
            text = "" if nl == -1 else text[nl + 1:].lstrip()
        elif text.startswith("/*"):
            close = text.find("*/")
            if close == -1:
                return "", text
            text = text[close + 2:].lstrip()
        else:
            break
    match = re.match(r"([A-Za-z]+)", text)
    if not match:
        return "", text
    return match.group(1).upper(), text


def _is_transaction(sql: str) -> bool:
    return any(p.fullmatch(sql.strip()) for p in _TXN_PATTERNS)


# --------------------------------------------------------------------------- #
# 外键隐含读取的 schema 推导
# --------------------------------------------------------------------------- #

def _build_fk_maps(schema: Schema) -> tuple[dict[str, list[tuple[str, str, str]]],
                                            dict[str, list[tuple[str, str, str]]]]:
    """返回 (表 -> 该表外键引用的父表列, 表 -> 引用该表的子表外键列)。

    元组形如 (对端表, 对端列, 本表外键列)；自引用外键同样保留。
    """
    parents: dict[str, list[tuple[str, str, str]]] = {t: [] for t in schema.tables}
    children: dict[str, list[tuple[str, str, str]]] = {t: [] for t in schema.tables}
    for src_table, src_col, dst_table, dst_col in schema.fk_edges:
        parents[src_table].append((dst_table, dst_col, src_col))
        children[dst_table].append((src_table, src_col, dst_col))
    return parents, children


# --------------------------------------------------------------------------- #
# 单语句分析
# --------------------------------------------------------------------------- #

class _Analyzer:
    def __init__(self, db_path: str, schema: Schema) -> None:
        self.schema = schema
        self.table_set = set(schema.tables)
        self.parents, self.children = _build_fk_maps(schema)
        # 只读 URI 连接：任何写操作在连接层即被拒绝，原库物理上不可能被修改
        self.conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        self.conn.set_authorizer(self._authorizer)
        # 与运行语义保持一致：开启外键后 authorizer 才会上报外键隐含读取
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._reset_events()

    def close(self) -> None:
        self.conn.close()

    def _reset_events(self) -> None:
        self.read_events: list[tuple[str, str]] = []
        self.write_events: list[tuple[str, int, str]] = []

    def _authorizer(self, action: int, arg1: str | None, arg2: str | None,
                    db_name: str | None, _trigger: str | None) -> int:
        # 运行期最终防线：静态检查漏网的危险操作在此拒绝
        if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_PRAGMA:
            return sqlite3.SQLITE_OK if str(arg1).lower() == "foreign_keys" \
                else sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION and str(arg1).lower() in (
            "load_extension",
            "readfile",
            "writefile",
        ):
            return sqlite3.SQLITE_DENY
        # 只接受 main 库中的用户表；CTE 内部读取 db_name 为 None、
        # CTE 自身读取的 trigger 参数携带 CTE 名，均不会落到这里
        if db_name != "main":
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            self.read_events.append((str(arg1), str(arg2)))
        elif action in (
            sqlite3.SQLITE_INSERT,
            sqlite3.SQLITE_UPDATE,
            sqlite3.SQLITE_DELETE,
        ):
            # 表级 INSERT/DELETE 事件 arg2 为 None；列级 UPDATE 事件为列名
            self.write_events.append((str(arg1), action, arg2))
        return sqlite3.SQLITE_OK

    def analyze(self, index: int, sql: str, line: int, column: int) -> StatementAccess:
        verb, stripped = _first_verb(sql)
        if verb not in _ALLOWED_VERBS:
            raise DependencyRejected(
                f"第 {index} 段语句以 {verb or '无法识别的标记'} 开头，"
                "仅允许查询（SELECT/VALUES/WITH）、对现有表的增删改"
                "（INSERT/UPDATE/DELETE/REPLACE）与事务控制"
                "（BEGIN/COMMIT/ROLLBACK/SAVEPOINT/RELEASE）。",
                statement_index=index,
                line=line,
                column=column,
            )
        if verb in _TXN_VERBS:
            if not _is_transaction(sql):
                raise DependencyRejected(
                    f"第 {index} 段事务控制语句形态无法判定，仅支持 "
                    "BEGIN/COMMIT/ROLLBACK/SAVEPOINT/RELEASE 及其标准形式。",
                    statement_index=index,
                    line=line,
                    column=column,
                )
            return StatementAccess(
                index=index, sql=sql, kind="transaction", line=line, column=column
            )

        # EXPLAIN 只触发语句准备（planning），不会执行语句、不触碰表数据
        self._reset_events()
        try:
            self.conn.execute("EXPLAIN " + stripped).fetchall()
        except sqlite3.Error as exc:
            raise DependencyRejected(
                f"第 {index} 段语句无法判定（SQLite 准备失败）：{exc}",
                statement_index=index,
                line=line,
                column=column,
            ) from exc

        kind = self._classify(verb, stripped)
        return self._collect(index, sql, kind, line, column)

    def _classify(self, verb: str, stripped: str) -> str:
        if verb in _WRITE_VERBS:
            return {"INSERT": "insert", "UPDATE": "update",
                    "DELETE": "delete", "REPLACE": "replace"}[verb]
        # WITH 子查询后可能接 SELECT，也可能接数据修改语句；以 authorizer
        # 实际上报的写事件为准，避免凭关键字误判
        if self.write_events:
            actions = {a for _, a, _ in self.write_events}
            if sqlite3.SQLITE_DELETE in actions:
                return "delete"
            if sqlite3.SQLITE_UPDATE in actions:
                return "update"
            return "insert"
        return "select"

    def _collect(self, index: int, sql: str, kind: str,
                 line: int, column: int) -> StatementAccess:
        # ---- 写集合：仅保留 schema 内用户表 ----
        write_cols: dict[str, set[str | None]] = {}
        write_actions: dict[str, set[int]] = {}
        for table, action, col in self.write_events:
            if table not in self.table_set:
                raise DependencyRejected(
                    f"第 {index} 段语句试图写库内受保护/不存在的表 {table}，"
                    "只允许增删改现有用户表的数据。",
                    statement_index=index,
                    line=line,
                    column=column,
                )
            write_cols.setdefault(table, set()).add(col)
            write_actions.setdefault(table, set()).add(action)
        write_targets = sorted(write_cols)

        # ---- 外键隐含读取（schema 独立推导）----
        # SQLite 只在键列被修改时做外键检查：
        # * 更新子表的外键列/主键 -> 读父表被引用主键；
        # * 更新父表的主键 -> 读所有子表外键列；
        # * 更新普通非键列（如 name）不产生任何隐含读取。
        implicit_cols: dict[str, set[str]] = {}
        for table in write_targets:
            actions = write_actions[table]
            updated = write_cols[table]
            key_changed = (
                sqlite3.SQLITE_UPDATE in actions
                and (
                    self.schema.pk_column[table] in updated
                    or any(src_col in updated
                           for _p, _pc, src_col in self.parents[table])
                )
            )
            if sqlite3.SQLITE_INSERT in actions or key_changed:
                for parent_table, parent_col, _src_col in self.parents[table]:
                    implicit_cols.setdefault(parent_table, set()).add(parent_col)
            if sqlite3.SQLITE_DELETE in actions or (
                key_changed and self.children[table]
            ):
                for child_table, child_col, _parent_col in self.children[table]:
                    implicit_cols.setdefault(child_table, set()).add(child_col)

        # ---- 读集合：authorizer 事件 + schema 推导合并 ----
        read_cols: dict[str, set[str]] = {}
        for table, col in self.read_events:
            if table not in self.table_set:
                # CTE/内部对象（db_name 非 main 已过滤），直接忽略
                continue
            read_cols.setdefault(table, set()).add(col)
        for table, cols in implicit_cols.items():
            read_cols.setdefault(table, set()).update(cols)

        reads = [
            ColumnAccess(
                table=t,
                column=c,
                # 仅当该列在 schema 推导出的外键隐含列集合中才标记；
                # 同一表上同时存在显式读取的列仍按显式处理
                implicit=(t in implicit_cols and c in implicit_cols[t]),
            )
            for t in sorted(read_cols)
            for c in sorted(read_cols[t])
        ]
        writes = [
            ColumnAccess(table=t, column=c)
            for t in write_targets
            for c in sorted(write_cols[t], key=lambda x: (x is not None, x or ""))
        ]
        implicit_tables = sorted(implicit_cols)
        return StatementAccess(
            index=index,
            sql=sql,
            kind=kind,
            reads=reads,
            writes=writes,
            read_tables=sorted(read_cols),
            write_tables=write_targets,
            implicit_tables=implicit_tables,
            line=line,
            column=column,
        )


# --------------------------------------------------------------------------- #
# 依赖边与并行批次
# --------------------------------------------------------------------------- #

def _build_edges(statements: list[StatementAccess]) -> list[DependencyEdge]:
    """按提交顺序为冲突语句建立先后边。

    两条语句在同一表上只要有一方是写即冲突：
    * 先写后读 / 先读后读写 -> ``write-read``（至少一方读、一方写）；
    * 双方都写 -> ``write-write``。
    纯读-读不冲突，不建边。边方向始终由提交顺序决定（earlier -> later）。
    """
    edges: list[DependencyEdge] = []
    seen: set[tuple[int, int, str, str]] = set()
    for earlier in statements:
        for later in statements:
            if later.index <= earlier.index:
                continue
            shared = (set(earlier.read_tables) | set(earlier.write_tables)) & (
                set(later.read_tables) | set(later.write_tables)
            )
            for table in sorted(shared):
                if table in earlier.write_tables and table in later.write_tables:
                    conflict = "write-write"
                elif table in earlier.write_tables or table in later.write_tables:
                    conflict = "write-read"
                else:
                    continue  # 纯读-读，可并行
                key = (earlier.index, later.index, table, conflict)
                if key not in seen:
                    seen.add(key)
                    edges.append(DependencyEdge(
                        earlier.index, later.index, table, conflict
                    ))
    edges.sort(key=lambda e: (e.upstream, e.downstream, e.table, e.conflict))
    return edges


def _build_batches(statements: list[StatementAccess],
                   edges: list[DependencyEdge]) -> list[list[int]]:
    """拓扑分层：同一层内任意两条语句都不共享写表、也不存在写-读表重叠。"""
    indegree = {s.index: 0 for s in statements}
    adjacency: dict[int, set[int]] = {s.index: set() for s in statements}
    for edge in edges:
        if edge.downstream not in adjacency[edge.upstream]:
            adjacency[edge.upstream].add(edge.downstream)
            indegree[edge.downstream] += 1

    batches: list[list[int]] = []
    remaining = set(indegree)
    while remaining:
        layer = sorted(idx for idx in remaining if indegree[idx] == 0)
        if not layer:  # 理论不可达：边只沿提交顺序建立，图必然无环
            raise DependencyRejected(
                "依赖图出现环，无法生成并行批次（内部错误）。"
            )
        batches.append(layer)
        for idx in layer:
            remaining.discard(idx)
            for nxt in adjacency[idx]:
                indegree[nxt] -= 1
    return batches


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def analyze_dependencies(db_path: str, schema: Schema,
                         raw_script: bytes | str) -> DependencyReport:
    """对 2~4 段 SQL 做静态依赖核查，返回读/写表、依赖边与并行批次。

    任何语句无法判定时抛出 :class:`DependencyRejected`，不返回部分排程。
    """
    if isinstance(raw_script, bytes):
        if b"\x00" in raw_script:
            raise DependencyRejected("脚本包含 NUL 字节。")
        try:
            script = raw_script.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DependencyRejected(f"脚本必须是 UTF-8 文本：{exc}") from exc
    else:
        script = raw_script
    if not script.strip():
        raise DependencyRejected("请提交 2～4 段 SQL 语句。")

    # 复用静态安全检查（ATTACH/PRAGMA/扩展/外部文件等），位置信息由
    # 切分阶段补全；这里先做整脚本级别的快速拦截。
    for pattern, message in _BANNED_PATTERNS:
        if pattern.search(script):
            pieces = split_statements(script)
            idx, line, column = _locate(script, pieces, pattern)
            raise DependencyRejected(
                message, statement_index=idx, line=line, column=column
            )

    pieces = split_statements(script)
    if not 2 <= len(pieces) <= 4:
        raise DependencyRejected(
            f"本次提交解析出 {len(pieces)} 段完整语句，"
            "要求按既定顺序提交 2～4 段 SQL。"
        )

    analyzer = _Analyzer(db_path, schema)
    try:
        statements = [
            analyzer.analyze(i, sql, line, column)
            for i, (sql, line, column) in enumerate(pieces, start=1)
        ]
    finally:
        analyzer.close()

    edges = _build_edges(statements)
    batches = _build_batches(statements, edges)
    return DependencyReport(statements=statements, edges=edges, batches=batches)


def _locate(script: str,
            pieces: list[tuple[str, int, int]],
            pattern: re.Pattern[str]) -> tuple[int | None, int | None, int | None]:
    match = pattern.search(script)
    if match is None or not pieces:
        return None, None, None
    offset = match.start()
    cursor = 0
    for i, (sql, line, column) in enumerate(pieces, start=1):
        end = cursor + len(sql)
        if cursor <= offset < end:
            return i, line, column
        cursor = end
    sql, line, column = pieces[-1]
    return len(pieces), line, column
