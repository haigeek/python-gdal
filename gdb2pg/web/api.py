# -*- coding: utf-8 -*-
"""Web API 路由（前缀 /api）。

统一响应：成功 {ok: True, data: ...}，失败 {ok: False, error: ...}（HTTP 4xx/5xx）。
handler 均为同步 def（FastAPI 线程池执行），psycopg 短连接天然线程安全。
"""

from __future__ import annotations

import os
import shutil
import time
import uuid
import zipfile
from typing import Optional

import psycopg
from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile

from .. import gdb_reader
from ..model import DatabaseConfig
from ..tasks import ALL_STATUS, EDITABLE, RUNNABLE, get_task_type, registered_types
from ..tasks.store import STATUS_QUEUED, STATUS_RUNNING, TaskStore
from .config import WebConfig
from .schemas import (DataSourceCreate, DataSourceUpdate, DatabaseTestRequest,
                      GdbLayersRequest, PreviewRequest, TaskCreate, TaskUpdate)
from ..gdb_reader import gdb_layers_summary

router = APIRouter(prefix="/api")


# ---------------------------------------------------------------- 依赖取值

def _cfg(request: Request) -> WebConfig:
    return request.app.state.web_config


def _store(request: Request) -> TaskStore:
    return request.app.state.store


def _manager(request: Request):
    return request.app.state.manager


def _ttype(request: Request, type_: str):
    t = get_task_type(type_)
    if t is None:
        raise HTTPException(404, f"未知任务类型: {type_}")
    return t


# ---------------------------------------------------------------- 任务类型

@router.get("/task-types")
def task_types():
    """已注册任务类型清单（含驱动前端表单的 form_schema）。"""
    return {"ok": True, "data": [
        {"type": t.type, "label": t.label, "form_schema": t.form_schema}
        for t in registered_types()
    ]}


# ---------------------------------------------------------------- 任务 CRUD

@router.post("/tasks", status_code=201)
def create_task(body: TaskCreate, request: Request):
    ttype = _ttype(request, body.type)
    config = _resolve_datasource(request, body.config)
    errs = ttype.validate(config)
    if errs:
        raise HTTPException(400, "；".join(errs))
    task_id = _store(request).create(body.type, body.name or body.type, config)
    return {"ok": True, "data": _detail(request, task_id)}


@router.get("/tasks")
def list_tasks(request: Request, status: Optional[str] = None,
               type_: Optional[str] = Query(None, alias="type"),
               q: Optional[str] = None, page: int = 1, page_size: int = 20,
               sort_by: str = "created_at", order: str = "desc"):
    if status and status not in ALL_STATUS:
        raise HTTPException(400, f"非法 status: {status}（可选 {ALL_STATUS}）")
    if type_ and get_task_type(type_) is None:
        raise HTTPException(400, f"未知任务类型: {type_}")
    if sort_by not in ("id", "created_at", "updated_at"):
        raise HTTPException(400, f"非法排序字段: {sort_by}（可选 id/created_at/updated_at）")
    if order not in ("asc", "desc"):
        raise HTTPException(400, f"非法排序方向: {order}（可选 asc/desc）")
    items, total = _store(request).list(
        status=status, type_=type_, q=q, page=page, page_size=page_size,
        sort_by=sort_by, order=order)
    return {"ok": True, "data": {
        "total": total,
        "items": [_summary(request, it) for it in items],
    }}


@router.get("/tasks/{task_id}")
def get_task(task_id: int, request: Request):
    return {"ok": True, "data": _detail(request, task_id)}


@router.put("/tasks/{task_id}")
def update_task(task_id: int, body: TaskUpdate, request: Request):
    store = _store(request)
    rec = store.get(task_id)
    if not rec:
        raise HTTPException(404, f"任务不存在: {task_id}")
    if rec["status"] not in EDITABLE:
        raise HTTPException(409, f"任务状态 {rec['status']} 不可编辑（仅 {EDITABLE}）")
    ttype = get_task_type(rec["type"])
    config = rec["config"] or {}
    if body.config is not None:
        config = _merge_password(config, body.config)
        config = _resolve_datasource(request, config)
        errs = ttype.validate(config)
        if errs:
            raise HTTPException(400, "；".join(errs))
    store.update(task_id, name=body.name, config=config)
    return {"ok": True, "data": _detail(request, task_id)}


@router.delete("/tasks/{task_id}", status_code=204)
def delete_task(task_id: int, request: Request):
    store = _store(request)
    rec = store.get(task_id)
    if not rec:
        raise HTTPException(404, f"任务不存在: {task_id}")
    if rec["status"] in (STATUS_QUEUED, STATUS_RUNNING):
        raise HTTPException(409, f"任务状态 {rec['status']} 运行中，请先取消或等待完成")
    store.delete(task_id)
    return None


# ---------------------------------------------------------------- 运行/取消

@router.post("/tasks/{task_id}/run")
def run_task(task_id: int, request: Request):
    store = _store(request)
    rec = store.get(task_id)
    if not rec:
        raise HTTPException(404, f"任务不存在: {task_id}")
    if rec["status"] not in RUNNABLE:
        raise HTTPException(409, f"任务状态 {rec['status']} 不允许运行（可选 {RUNNABLE}）")
    _manager(request).enqueue(task_id)
    return {"ok": True, "data": _detail(request, task_id)}


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: int, request: Request):
    try:
        _manager(request).cancel(task_id)
    except KeyError:
        raise HTTPException(404, f"任务不存在: {task_id}")
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True, "data": _detail(request, task_id)}


# ---------------------------------------------------------------- 日志

@router.get("/tasks/{task_id}/logs")
def task_logs(task_id: int, request: Request, after_id: int = 0, limit: int = 500):
    """增量日志（前端轮询）：返回 id > after_id 的记录。"""
    if not _store(request).get(task_id):
        raise HTTPException(404, f"任务不存在: {task_id}")
    rows = _store(request).logs(task_id, after_id=after_id, limit=limit)
    return {"ok": True, "data": {
        "last_id": rows[-1]["id"] if rows else after_id,
        "logs": [{"id": r["id"], "ts": r["ts"], "message": r["message"]} for r in rows],
    }}


# ---------------------------------------------------------------- 预览（类型分派）

@router.post("/tasks/preview")
def preview(body: PreviewRequest, request: Request):
    ttype = _ttype(request, body.type)
    # 数据源引用（datasource_id）先解析：补齐真实密码并移除引用，
    # 预览基于与保存一致的完整配置
    config = _resolve_datasource(request, body.config)
    return {"ok": True, "data": ttype.preview(config)}


# ---------------------------------------------------------------- GDB 目录浏览

@router.get("/gdb/browse")
def browse(request: Request, path: Optional[str] = None):
    """列服务器目录（限制在 uploads_dir ∪ allowed_base_dirs 内）。"""
    cfg = _cfg(request)
    resolved = _resolve_under(cfg, path)
    if resolved is None:
        raise HTTPException(403, "路径越界：仅允许浏览配置的目录范围")
    if not os.path.isdir(resolved):
        if path is None:
            # 默认根（uploads）不存在时自动创建，方便首次使用
            os.makedirs(resolved, exist_ok=True)
        else:
            raise HTTPException(404, f"目录不存在: {resolved}")
    return {"ok": True, "data": _list_dir(resolved)}


# ---------------------------------------------------------------- GDB 图层清单

@router.post("/gdb/layers")
def gdb_layers(body: GdbLayersRequest, request: Request):
    """读取 GDB 内全部图层（新建任务自动填充「图层选择」用）。

    路径须在白名单/上传目录内；打开失败返回 400（不致命）。
    """
    resolved = _resolve_under(_cfg(request), body.gdb)
    if resolved is None:
        raise HTTPException(403, "路径越界：仅允许配置目录范围内的 GDB")
    if not os.path.isdir(resolved):
        raise HTTPException(400, f"GDB 目录不存在或不可读: {resolved}")
    try:
        layers = gdb_layers_summary(resolved)
    except Exception as e:  # noqa: BLE001 打不开/无 GDAL 驱动等
        raise HTTPException(400, f"读取 GDB 图层失败: {e}")
    return {"ok": True, "data": {"gdb": resolved, "layers": layers}}


# ---------------------------------------------------------------- zip 上传

@router.post("/uploads")
def upload_zip(request: Request, file: UploadFile = File(...)):
    """上传 zip 并解压，定位 .gdb 目录返回其路径与图层清单。

    MD5 去重：相同内容的 zip 直接复用已有解压，避免重复占用空间
    （返回 cached=true 与已有 gdb_path）。
    """
    cfg = _cfg(request)
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(400, "仅支持 .zip 文件（GDB 目录打包）")
    dest = os.path.join(cfg.uploads_abs(), uuid.uuid4().hex)
    zpath = os.path.join(dest, "source.zip")
    os.makedirs(dest, exist_ok=True)
    success = False
    try:
        md5 = _save_upload(file, zpath, cfg.max_upload_mb * 1024 * 1024)
        store = _store(request)
        hit = store.upload_find(md5)
        if hit is not None and os.path.isdir(hit["gdb_path"]):
            # 去重命中：刷新使用时间，丢弃本次重复文件
            store.upload_touch(hit["id"])
            success = True
            shutil.rmtree(dest, ignore_errors=True)
            layers = gdb_reader.list_layers(gdb_reader.open_gdb(hit["gdb_path"]))
            return {"ok": True, "data": {
                "gdb_path": hit["gdb_path"], "layers": layers,
                "cached": True,
                "note": "相同内容的压缩包已上传过，直接复用已有解压",
            }}
        if hit is not None:
            # 记录存在但目录已被清理：删除记录后按新上传处理
            store.upload_delete(hit["id"])
        _safe_extract(zpath, dest)
        gdb_path = _find_gdb_dir(dest)
        if gdb_path is None:
            raise HTTPException(400, "压缩包内未找到 .gdb 目录（请打包 GDB 文件夹本身）")
        try:
            ds = gdb_reader.open_gdb(gdb_path)
            layers = gdb_reader.list_layers(ds)
        except Exception as e:  # noqa: BLE001 GDAL 打不开则报错
            raise HTTPException(400, f"GDB 无法被 GDAL 打开: {e}")
        store.upload_record(md5, gdb_path, os.path.getsize(zpath))
        success = True
        return {"ok": True, "data": {
            "gdb_path": gdb_path,
            "layers": layers,
            "cached": False,
            "note": "已解压到服务器，任务将使用此路径导入",
        }}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"上传/解压失败: {e}")
    finally:
        file.file.close()
        if not success:
            shutil.rmtree(dest, ignore_errors=True)


# ---------------------------------------------------------------- helpers

def _detail(request: Request, task_id: int) -> dict:
    rec = _store(request).get(task_id)
    if not rec:
        raise HTTPException(404, f"任务不存在: {task_id}")
    ttype = get_task_type(rec["type"])
    out = {
        "id": rec["id"], "type": rec["type"], "name": rec["name"],
        "status": rec["status"], "progress": rec["progress"],
        "result": rec["result"], "error": rec["error"],
        "created_at": rec["created_at"], "updated_at": rec["updated_at"],
        "started_at": rec["started_at"], "finished_at": rec["finished_at"],
    }
    out["config"] = ttype.sanitize(rec["config"] or {}) if ttype else (rec["config"] or {})
    return out


def _summary(request: Request, rec: dict) -> dict:
    """列表项：同详情结构（config 亦脱敏，便于编辑页直接预填）。"""
    ttype = get_task_type(rec["type"])
    out = {
        "id": rec["id"], "type": rec["type"], "name": rec["name"],
        "status": rec["status"], "progress": rec["progress"],
        "result": rec["result"], "error": rec["error"],
        "created_at": rec["created_at"], "updated_at": rec["updated_at"],
        "started_at": rec["started_at"], "finished_at": rec["finished_at"],
    }
    out["config"] = ttype.sanitize(rec["config"] or {}) if ttype else (rec["config"] or {})
    return out


def _merge_password(old_config: dict, new_config: dict) -> dict:
    """编辑时 password 为 "***" 哨兵 = 保留旧值（与 sanitize 输出一致）。"""
    import copy
    merged = copy.deepcopy(new_config)
    ndb = merged.get("database")
    if isinstance(ndb, dict) and ndb.get("password") == "***":
        old_db = (old_config or {}).get("database") or {}
        ndb["password"] = old_db.get("password")
    return merged


# ---------------------------------------------------------------- 数据源

def _ds_out(rec: dict) -> dict:
    """数据源输出（密码脱敏为 "***"）。"""
    cfg = dict(rec["config"] or {})
    if cfg.get("password"):
        cfg["password"] = "***"
    return {"id": rec["id"], "name": rec["name"], "config": cfg,
            "created_at": rec["created_at"], "updated_at": rec["updated_at"]}


def _merge_ds_password(old_config: dict, new_config: dict) -> dict:
    """数据源编辑：config.password 为 "***" 哨兵 = 保留旧值。"""
    import copy
    merged = copy.deepcopy(new_config)
    if merged.get("password") == "***":
        merged["password"] = (old_config or {}).get("password")
    return merged


def _resolve_datasource(request: Request, config: dict) -> dict:
    """任务 config.database 引用数据源（datasource_id）时：

    - 密码为空/"***"→ 用数据源真实密码补齐；
    - 移除 datasource_id：任务保存的是独立快照，之后改数据源不影响本任务。
    """
    import copy
    cfg = copy.deepcopy(config)
    db = cfg.get("database")
    if not isinstance(db, dict) or not db.get("datasource_id"):
        return cfg
    rec = _store(request).datasource_get(db["datasource_id"])
    if not rec:
        raise HTTPException(400, f"引用的数据源不存在: {db['datasource_id']}")
    src = rec["config"] or {}
    if db.get("password") in (None, "", "***"):
        db["password"] = src.get("password")
    db.pop("datasource_id", None)
    return cfg


def _resolve_under(cfg: WebConfig, path: Optional[str]) -> Optional[str]:
    """把请求路径解析为白名单内绝对路径；不存在或越界返回 None。"""
    roots = cfg.allowed_dirs_abs()
    if not roots:
        return None
    if not path:
        return roots[0]
    p = path if os.path.isabs(path) else os.path.join(roots[0], path)
    p = os.path.realpath(p)
    for root in roots:
        if p == root or p.startswith(root + os.sep):
            return p
    return None


def _list_dir(path: str) -> dict:
    parent = os.path.dirname(path) if os.path.dirname(path) != path else None
    entries = []
    try:
        names = sorted(os.listdir(path))
    except OSError as e:
        raise HTTPException(400, f"无法读取目录: {e}")
    for name in names:
        if name.startswith("."):
            continue
        full = os.path.join(path, name)
        if os.path.isdir(full):
            entries.append({"name": name, "is_dir": True,
                            "is_gdb": name.lower().endswith(".gdb")})
        else:
            try:
                size = os.path.getsize(full)
            except OSError:
                size = 0
            entries.append({"name": name, "is_dir": False, "is_gdb": False, "size": size})
    return {"path": path, "parent": parent, "entries": entries}


def _save_upload(file: UploadFile, zpath: str, max_bytes: int) -> str:
    """流式保存上传文件并计算 MD5，返回 md5 hex；超过 max_bytes 抛 400。"""
    import hashlib
    h = hashlib.md5()
    written = 0
    with open(zpath, "wb") as out:
        while True:
            chunk = file.file.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > max_bytes:
                raise HTTPException(
                    400, f"文件超过大小限制 {max_bytes // (1024 * 1024)}MB")
            h.update(chunk)
            out.write(chunk)
    return h.hexdigest()


def _decode_zip_filename(info: zipfile.ZipInfo) -> str:
    """修正 zip 成员文件名编码。

    Windows 压缩的中文 zip 常不带 UTF-8 标记（EFS flag）：zipfile 会按
    cp437 解码成乱码。经典修复：乱码字符串按 cp437 还原字节，再用 GBK
    解码回中文。无标记且非 GBK 时保持原样。
    """
    name = info.filename
    if info.flag_bits & 0x800:  # 有 UTF-8 标记：zipfile 已正确解码
        return name
    try:
        return name.encode("cp437").decode("gbk")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return name  # 纯 ASCII 或非 GBK 编码，保持原样


def _safe_extract(zpath: str, dest: str):
    """解压 zip，拒绝路径穿越（zip-slip）。成员名先做编码修复。

    注意不能用 zf.extractall()：它按修改后的文件名去 NameToInfo 索引
    查找原始成员会 KeyError。这里直接基于已修复的 ZipInfo 逐成员提取。
    """
    with zipfile.ZipFile(zpath) as zf:
        for info in zf.infolist():
            info.filename = _decode_zip_filename(info)
            name = info.filename
            if not name or name.startswith("/") or "\\" in name:
                raise ValueError(f"非法 zip 成员路径: {name!r}")
            parts = name.split("/")
            if ".." in parts:
                raise ValueError(f"zip 成员包含越界路径: {name!r}")
        for info in zf.infolist():
            target = os.path.join(dest, *info.filename.split("/"))
            if info.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)


def _find_gdb_dir(root: str, depth: int = 0) -> Optional[str]:
    """在解压目录中定位 .gdb 文件夹（递归深度 <= 3）。"""
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return None
    for name in names:
        full = os.path.join(root, name)
        if not os.path.isdir(full):
            continue
        if name.lower().endswith(".gdb"):
            return full
        if depth < 3:
            found = _find_gdb_dir(full, depth + 1)
            if found:
                return found
    return None


# ---------------------------------------------------------------- 数据库检测

@router.post("/database/test")
def test_database(request: Request, body: DatabaseTestRequest):
    """目标数据库连接检测：连通性 / PostGIS / schema 是否存在 / 延迟。

    密码 "***"（编辑页脱敏值）时需传 task_id，从任务已保存的 config 取真实密码；
    否则要求明文密码（新建页）。
    """
    db = dict(body.database)
    if db.get("password") == "***":
        # 引用数据源且未自行填密码：用数据源密码检测（新建未保存场景）
        if db.get("datasource_id"):
            ds_rec = _store(request).datasource_get(db["datasource_id"])
            if not ds_rec:
                raise HTTPException(404, f"数据源不存在: {db['datasource_id']}")
            db["password"] = (ds_rec["config"] or {}).get("password")
        elif body.task_id is None:
            raise HTTPException(
                400, "密码已脱敏：请重新填写密码，或传入任务 ID 用已保存的密码检测")
        else:
            rec = _store(request).get(body.task_id)
            if not rec:
                raise HTTPException(404, f"任务不存在: {body.task_id}")
            old_db = ((rec.get("config") or {}).get("database")) or {}
            if not old_db.get("password"):
                raise HTTPException(400, "任务未保存数据库密码，无法用旧密码检测")
            db["password"] = old_db["password"]

    known = {k: db[k] for k in (
        "host", "port", "dbname", "user", "password",
        "password_env", "schema", "ssl", "ensure_postgis") if k in db}
    dcfg = DatabaseConfig(**known)

    t0 = time.time()
    try:
        with psycopg.connect(dcfg.dsn(), connect_timeout=5, autocommit=True) as conn:
            server_version = conn.execute("SHOW server_version").fetchone()[0]
            try:
                postgis = conn.execute("SELECT postgis_version()").fetchone()[0]
            except Exception:
                postgis = None
            schema = known.get("schema") or dcfg.schema
            row = conn.execute(
                "SELECT to_regnamespace(%s)", (schema,)).fetchone()
    except psycopg.OperationalError as e:
        raise HTTPException(400, f"连接失败: {e}")
    except Exception as e:
        raise HTTPException(400, f"检测失败: {e}")
    return {"ok": True, "data": {
        "connected": True,
        "server_version": server_version,
        "postgis_version": postgis,
        "schema": schema,
        "schema_exists": bool(row and row[0]),
        "latency_ms": int((time.time() - t0) * 1000),
    }}


# ---------------------------------------------------------------- 数据源管理

@router.get("/datasources")
def list_datasources(request: Request):
    """数据源清单（config 密码脱敏为 "***"）。"""
    return {"ok": True, "data": [_ds_out(r) for r in _store(request).datasource_list()]}


@router.post("/datasources", status_code=201)
def create_datasource(body: DataSourceCreate, request: Request):
    try:
        ds_id = _store(request).datasource_create(body.name.strip(), body.config or {})
    except psycopg.errors.UniqueViolation:
        raise HTTPException(400, f"数据源名称已存在: {body.name}")
    rec = _store(request).datasource_get(ds_id)
    return {"ok": True, "data": _ds_out(rec)}


@router.get("/datasources/{ds_id}")
def get_datasource(ds_id: int, request: Request):
    rec = _store(request).datasource_get(ds_id)
    if not rec:
        raise HTTPException(404, f"数据源不存在: {ds_id}")
    return {"ok": True, "data": _ds_out(rec)}


@router.put("/datasources/{ds_id}")
def update_datasource(ds_id: int, body: DataSourceUpdate, request: Request):
    store = _store(request)
    rec = store.datasource_get(ds_id)
    if not rec:
        raise HTTPException(404, f"数据源不存在: {ds_id}")
    cfg = None
    if body.config is not None:
        cfg = _merge_ds_password(rec["config"] or {}, body.config)
    try:
        ok = store.datasource_update(
            ds_id,
            name=body.name.strip() if body.name else None,
            config=cfg)
    except psycopg.errors.UniqueViolation:
        raise HTTPException(400, f"数据源名称已存在: {body.name}")
    if not ok:
        raise HTTPException(404, f"数据源不存在: {ds_id}")
    return {"ok": True, "data": _ds_out(store.datasource_get(ds_id))}


@router.delete("/datasources/{ds_id}", status_code=204)
def delete_datasource(ds_id: int, request: Request):
    if not _store(request).datasource_delete(ds_id):
        raise HTTPException(404, f"数据源不存在: {ds_id}")
    return None