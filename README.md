# SQLite 复现库缩减 / 脚本依赖核查服务

入仓开发者排查 SQL 失败时，用它把精简后的**复现库**交给同事：上传一个结构受限、
外键有效的 SQLite 数据库和一段确定性 SQL 脚本，服务先确认原始错误，再在保持
表结构与剩余行值不变、外键始终有效的前提下删除数据行，直到得到 **1-最小**
复现库——删除任意一行（连同直接或间接引用它的全部行）后都无法再复现同一错误。

数仓值班工程师安排入仓 SQL 前，还可以使用**脚本依赖核查**：上传同样约束的
SQLite 库，按既定顺序提交 2～4 段 SQL，服务**只分析不运行**，识别每条语句
读取、写入的表（外键约束引起的隐含读取同样计入），按提交顺序建立读写/写写
冲突的先后关系，并给出依赖上可并行的批次。

## 复现判定（严格）

* 以 SQLite **扩展错误码 + 完整错误文本** 作为错误指纹，两者完全一致才算复现；
* 错误码不同、错误文本不同、脚本执行成功、试跑超时，都不算复现；
* 原脚本执行成功时直接返回“无需缩减”，不产出下载库。

## 输入约束

| 项目 | 限制 |
| --- | --- |
| 普通表数量 | ≤ 3（禁止视图、虚拟表、触发器、附加库） |
| 全部表总行数 | ≤ 60 |
| 主键 | 每张表有且仅有一个单列 `INTEGER PRIMARY KEY`（禁止 WITHOUT ROWID） |
| 外键 | 全部为单列 `INTEGER`，且引用目标表的单列整数主键；**允许循环引用** |
| 外键动作 | 仅允许 NO ACTION / RESTRICT（禁止 CASCADE/SET NULL 等） |
| 现有数据 | 上传库必须已通过 `PRAGMA foreign_key_check` |
| SQL 脚本 | UTF-8 文本、确定性、不访问外部文件 |

脚本安全由两层保证：

1. 静态检查拒绝 `ATTACH/DETACH`、除 `foreign_keys` 外的 `PRAGMA`、
   `load_extension/readfile/writefile`、`VACUUM INTO` 等；
2. 每个试跑连接安装 SQLite authorizer 作为运行期最终防线，
   并强制 `PRAGMA foreign_keys=ON`。

## 隔离保证（先 COMMIT 后失败也安全）

* 上传原件落盘后设为只读；
* 先复制出一份**规范原件**（`journal_mode=DELETE`、完整性检查通过）；
* **每一次试跑都从规范原件复制全新临时文件**，脚本在副本里执行，
  结束后删除副本及 `-wal/-shm/-journal` 侧车文件；
* 因此脚本即使先 `COMMIT` 再失败，已提交的修改也只落在临时副本上，
  不会污染原件、后续试跑或最终下载库；
* 下载库由规范原件按保留行集合重新生成，并再次外键校验、完整性检查和试跑确认。

## 缩减算法

行级 Delta Debugging，且每次删除都沿外键反向边做**闭包扩张**
（删除被引用行时连带删除所有直接/间接引用者），保证剩余数据外键有效：

1. **ddmin 阶段**：对当前保留行集合二分切片，尝试删除一个分块或其补集；
   复现同一错误就采纳并回到粗粒度，否则细化粒度直到单元素；
2. **逐行终检**：对每个保留行尝试删除其外键闭包，反复整轮扫描，
   直到一整轮没有任何可删集合——即满足 1-最小性；
3. 产出最终库后再跑一次脚本，确认错误码与完整错误文本与基线一致。

## 脚本依赖核查（只分析、不运行）

页面第二个标签页“脚本依赖核查”面向入仓前的依赖确认，与缩减入口共用同一套
上传库约束，原库与已有缩减结果都不会被修改。

### 允许的语句

| 类别 | 语句 |
| --- | --- |
| 查询 | `SELECT`、`VALUES`、`WITH ... SELECT`（支持 WITH 子查询/CTE） |
| 增删改现有表数据 | `INSERT`、`UPDATE`、`DELETE`、`REPLACE`（含 `ON CONFLICT` 与 `INSERT ... SELECT`） |
| 事务控制 | `BEGIN`（含 DEFERRED/IMMEDIATE/EXCLUSIVE）、`COMMIT`/`END`、`ROLLBACK`、`SAVEPOINT`、`RELEASE` |

DDL（CREATE/ALTER/DROP）、ATTACH/DETACH、非 `foreign_keys` 的 PRAGMA、扩展与
外部文件函数等一律拒绝；无法判定的语句（如引用不存在的表）会指出**语句序号与
行列位置**，并且不输出任何部分排程。

### 分析原理

1. 用 `sqlite3.complete_statement` 按提交顺序切出 2～4 条完整语句（跳过注释）；
2. 库以 **只读 URI** 打开，每条数据语句仅执行 `EXPLAIN <stmt>` 做语句准备，
   **准备不等于执行**，不触碰任何表数据；
3. 准备期 authorizer 上报 `SQLITE_READ/INSERT/UPDATE/DELETE` 事件，得到显式
   读/写的表与列；CTE 内部对象通过库名/触发器名参数过滤，不会误判为表；
4. 连接强制 `PRAGMA foreign_keys=ON`，authorizer 会原生上报**外键隐含读取**，
   同时按 schema 外键定义独立推导一份并合并：
   * 插入子表、更新子表外键/主键 → 隐含读父表被引用主键列；
   * 删除行、更新父表主键 → 隐含读所有子表外键列；自引用外键同样计入；
   * 仅更新普通非键列不产生隐含读取；
5. 按提交顺序，两条语句在同一表上只要有一方是写即建立先后边
   （写-读 `write-read` / 写-写 `write-write`，纯读-读不建边），再做拓扑分层，
   得到层内无冲突、**依赖上可并行**的批次。

### 结果内容

* 每条语句的类型、原文、读取表（标注外键隐含表与隐含列）、写入表与列明细；
* 冲突边清单：先执行语句 → 后执行语句、冲突表、冲突类型；
* 并行批次：同一批次内的语句无共享写表、无写-读表重叠，可并行；批次间串行。

## 快速开始（Docker Compose）

```bash
docker compose up --build
# 浏览器打开 http://localhost:8000
```

页面支持：上传库与脚本（文件或粘贴文本）、运行、轮询查看删减结果与日志、
下载复现库 `repro.db`，并内置两个可一键试跑的样例。

停止与清理：

```bash
docker compose down          # 停止容器（保留数据卷）
docker compose down -v       # 同时删除任务数据卷
```

## 本地开发

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
DATA_DIR=./data .venv/bin/uvicorn app.main:app --reload --port 8000
.venv/bin/pytest             # 80 个测试
```

## 内置样例

| 样例 | 说明 |
| --- | --- |
| `cyclic` | 三表循环外键（dept ↔ employee ↔ project），脚本触发主键唯一约束错误 |
| `commit_then_fail` | 自引用循环外键，脚本先 `COMMIT` 一个 DELETE，再触发 NOT NULL 错误，验证副本隔离 |

样例文件由 `app/samples.py` 在启动时生成于 `app/static/`，可单独下载。

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MAX_DB_BYTES` | 20971520 | 上传库大小上限（字节） |
| `MAX_SCRIPT_BYTES` | 262144 | 脚本大小上限（字节） |
| `MAX_TRIALS` | 800 | 单次任务试跑次数上限 |
| `TRIAL_TIMEOUT_SECONDS` | 15 | 单次试跑超时 |
| `JOB_TTL_SECONDS` | 3600 | 任务结果保留时长 |
| `MAX_CONCURRENT_JOBS` | 2 | 后台并发任务数 |
| `DATA_DIR` | `/data` | 任务工作目录（容器内挂载为数据卷） |

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 单页前端（含“复现库缩减”“脚本依赖核查”两个标签页） |
| GET | `/api/samples` | 内置样例清单与下载地址 |
| POST | `/api/jobs` | 表单字段 `db`（必填）、`sql`（文件）或 `script_text`（文本），返回 202 与任务 id |
| GET | `/api/jobs/{id}` | 查询任务状态、错误指纹、行数变化、保留行与缩减日志 |
| GET | `/api/jobs/{id}/download` | 下载复现库 `repro.db`（仅缩减成功时可用） |
| POST | `/api/dependency-check` | 脚本依赖核查：字段 `db`、重复字段 `statements`（2～4 段）或 `script_text`（整段文本按分号切分）；同步返回每条语句读写表、冲突边与并行批次；无法判定时 400 并带语句序号/行列位置，不返回部分排程 |

## 目录结构

```
app/
  config.py        # 约束与运行配置
  errors.py        # 业务异常（含 DependencyRejected 位置信息）
  validation.py    # 库结构/行/外键图校验
  script_guard.py  # 脚本静态检查
  dependency.py    # 脚本依赖核查：语句切分、EXPLAIN+authorizer 读写分析、外键隐含读取、冲突边与并行批次
  runner.py        # 规范副本、authorizer、独立副本试跑、确定性双跑
  reducer.py       # ddmin + 逐行闭包终检
  jobs.py          # 后台任务与并发/TTL 管理
  service.py       # 上传->校验->确认->缩减 流水线
  samples.py       # 内置样例生成
  main.py          # FastAPI 路由（缩减任务 + /api/dependency-check）
  static/index.html
tests/             # 单元/集成/端到端测试（80 个）
Dockerfile
docker-compose.yml
```
