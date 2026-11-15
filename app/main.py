"""FastAPI 入口：上传、轮询任务、查看结果、下载复现库、脚本依赖核查。"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .dependency import analyze_dependencies
from .errors import DependencyRejected, JobError, ScriptRejected, ValidationError
from .jobs import registry
from .samples import ensure_samples
from .service import run_pipeline
from .validation import validate_database

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    ensure_samples(STATIC_DIR)
    registry.cleanup_expired()

    async def _janitor() -> None:
        while True:
            await asyncio.sleep(60)
            registry.cleanup_expired()

    janitor = asyncio.create_task(_janitor())
    try:
        yield
    finally:
        janitor.cancel()


app = FastAPI(title="SQLite 复现库缩减服务", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/samples")
def list_samples() -> JSONResponse:
    return JSONResponse(ensure_samples(STATIC_DIR))


@app.post("/api/jobs")
async def create_job(
    db: UploadFile = File(..., description="满足约束的 SQLite 数据库"),
    sql: UploadFile | None = File(None, description="确定性 SQL 脚本文件"),
    script_text: str = Form("", description="或直接粘贴 SQL 脚本"),
) -> JSONResponse:
    raw_db = await db.read()
    _check_uploaded_db(raw_db)

    if sql is not None:
        raw_script = await sql.read()
    else:
        raw_script = script_text.encode("utf-8")
    if not raw_script.strip():
        raise ValidationError("请上传或粘贴 SQL 脚本。")

    job = registry.create()
    upload_path = job.dir / "upload.db"
    upload_path.write_bytes(raw_db)
    # 原件落盘后只读保存，任何试跑都只接触副本
    Path(upload_path).chmod(0o444)

    registry.run_in_background(job, run_pipeline, str(upload_path), raw_script)
    return JSONResponse({"id": job.id, "status": job.status}, status_code=202)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> JSONResponse:
    job = registry.get(job_id)
    return JSONResponse(job.public_dict())


@app.get("/api/jobs/{job_id}/download")
def download_reduced(job_id: str) -> FileResponse:
    job = registry.get(job_id)
    if job.status != "done" or not job.result.get("download_available"):
        raise JobError("缩减结果尚不可下载。")
    reduced = job.dir / "reduced.db"
    if not reduced.exists():
        raise JobError("缩减库文件已被清理。")
    return FileResponse(
        reduced,
        media_type="application/x-sqlite3",
        filename="repro.db",
    )


@app.exception_handler(ValidationError)
@app.exception_handler(ScriptRejected)
@app.exception_handler(JobError)
def _handle_bad_request(_request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(DependencyRejected)
def _handle_dependency_rejected(_request: Request,
                                exc: DependencyRejected) -> JSONResponse:
    # 无法判定时只指出位置与原因，不输出任何部分排程
    return JSONResponse(status_code=400, content=exc.to_dict())


def _check_uploaded_db(raw_db: bytes) -> None:
    if len(raw_db) == 0:
        raise ValidationError("数据库文件为空。")
    if len(raw_db) > config.MAX_DB_BYTES:
        raise ValidationError(
            f"数据库大小 {len(raw_db)} 字节，超过上限 {config.MAX_DB_BYTES} 字节。"
        )
    if not raw_db.startswith(b"SQLite format 3\x00"):
        raise ValidationError("文件不是合法的 SQLite 数据库（魔数校验失败）。")


@app.post("/api/dependency-check")
async def dependency_check(
    db: UploadFile = File(..., description="满足原有约束的 SQLite 数据库"),
    statements: list[str] = Form(
        default_factory=list, description="按既定顺序提交的 2～4 段 SQL"
    ),
    script_text: str = Form("", description="或粘贴整段脚本（按分号切分）"),
) -> JSONResponse:
    """脚本依赖核查：只静态分析、不运行，返回读写表、依赖边与并行批次。"""
    raw_db = await db.read()
    _check_uploaded_db(raw_db)

    # 优先使用分段字段；为空时回退到整段文本，由分析器按分号切分。
    # 每段可能自带结尾分号，统一去掉后再连接，避免产生空语句。
    segments = [s.strip().rstrip(";").strip()
                for s in statements if s and s.strip()]
    raw_script = (";\n".join(segments) + ";" if segments else script_text)
    if not raw_script.strip():
        raise DependencyRejected("请提交 2～4 段 SQL 语句。")
    if len(raw_script.encode("utf-8")) > config.MAX_SCRIPT_BYTES:
        raise DependencyRejected(
            f"脚本大小超过上限 {config.MAX_SCRIPT_BYTES} 字节。"
        )

    # 与缩减流水线完全相同的结构/外键约束校验；原件落盘后只读打开
    work_dir = Path(tempfile.mkdtemp(prefix="dep_", dir=str(config.DATA_DIR)))
    upload_path = work_dir / "upload.db"
    try:
        upload_path.write_bytes(raw_db)
        upload_path.chmod(0o444)
        schema = validate_database(str(upload_path))
        report = analyze_dependencies(str(upload_path), schema, raw_script)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    return JSONResponse(report.to_dict())
