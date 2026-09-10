# -*- coding: utf-8 -*-
"""gdb_import 任务类型：把 File Geodatabase 导入 PostgreSQL/PostGIS。

- validate：纯配置校验（必填、枚举、路径存在），不连库、不打开 GDB；
- preview：importer.plan_all（纯计算）+ 连目标库只读检查 PostGIS 与表冲突；
- run：接入 importer.run，把 log / progress / cancel 桥接到 TaskContext；
- sanitize：database.password 脱敏为 "***"（前端编辑时的哨兵值）；
- form_schema：驱动前端动态表单（GDB 本地路径 + 浏览 + zip 上传 + 目标库与图层规则）。
"""

from __future__ import annotations

import copy
import os

from ..importer import ImportCancelled, plan_all, run as import_run
from ..model import ImportConfig
from ..pg_writer import PgWriter
from .base import TaskCancelled, TaskContext, TaskType, register

_FORM_SCHEMA = {
    "groups": [
        {
            "key": "default",
            "label": "默认导入规则",
            "fields": [
                {"key": "srid", "label": "兜底 SRID", "type": "int", "default": 4490,
                 "help": "图层无 SRS 时使用；优先级: 图层规则 > 图层自带 SRS > 此值"},
                {"key": "mode", "label": "导入模式", "type": "enum",
                 "options": ["create", "overwrite", "append"], "default": "create"},
                {"key": "geometries", "label": "导入几何", "type": "bool", "default": True},
                {"key": "create_spatial_index", "label": "创建 GIST 索引", "type": "bool", "default": True},
                {"key": "launder_columns", "label": "列名转小写", "type": "bool", "default": True,
                 "help": "源列名转小写，空格等特殊字符转下划线"},
                {"key": "launder_tables", "label": "表名转小写", "type": "bool", "default": True,
                 "help": "目标表名转小写，空格等特殊字符转下划线"},
                {"key": "fid_pk", "label": "使用 GDB 主键", "type": "bool", "default": True,
                 "help": "目标表主键使用 GDB 原生 OBJECTID（OGR FID，稳定唯一）；关闭则回退自增 bigserial"},
                {"key": "pk_column", "label": "主键列名", "type": "text", "default": "objectid",
                 "help": "使用 GDB 主键时主键列名（默认 objectid）"},
                {"key": "geom_column", "label": "几何列名", "type": "text", "default": "shape",
                 "help": "几何字段列名（缺省值，可被图层规则逐层覆盖）"},
                {"key": "on_error", "label": "单层失败处理", "type": "enum",
                 "options": ["abort", "skip"], "default": "abort"},
            ],
        },
        {
            "key": "gdb",
            "label": "数据源（GDB）",
            "single": True,  # 直接映射 config["gdb"]
            "fields": [
                {
                    "key": "gdb",
                    "label": "GDB 路径",
                    "type": "gdb_path",
                    "help": "服务器本地路径；可用「浏览」选取目录或「上传 zip」解压后自动填入",
                },
            ],
        },
        {
            "key": "layers",
            "label": "图层选择与导入模式（填写 GDB 路径后自动读取，逐行设置）",
            "actions": [
                {"key": "read_gdb", "label": "读取图层"},
            ],
            "fields": [
                {
                    "key": "layers",
                    "label": "导入图层",
                    "type": "table",
                    "columns": [
                        {"key": "source", "label": "源图层（支持 * ? 通配）", "type": "text"},
                        {"key": "table", "label": "目标表名（缺省=源名）", "type": "text"},
                        {"key": "srid", "label": "强制 SRID", "type": "int"},
                        {"key": "mode", "label": "导入模式", "type": "enum",
                         "options": ["create", "overwrite", "append"]},
                        {"key": "geom_column", "label": "几何列名（缺省继承默认）", "type": "text"},
                    ],
                },
            ],
        },
        {
            "key": "database",
            "label": "目标数据库",
            "test": True,  # 组标题栏显示「检测」：连接/PostGIS/schema/延迟
            "actions": [{"key": "from_datasource", "label": "从数据源选择"}],
            "fields": [
                {"key": "host", "label": "主机", "type": "text", "default": "127.0.0.1"},
                {"key": "port", "label": "端口", "type": "int", "default": 5432},
                {"key": "dbname", "label": "数据库名", "type": "text", "default": "postgres"},
                {"key": "user", "label": "用户", "type": "text", "default": "postgres"},
                {"key": "password", "label": "密码", "type": "password",
                 "help": "可留空（连接不需要密码时）"},
                {"key": "schema", "label": "目标 schema", "type": "text", "default": "public"},
                {"key": "ssl", "label": "SSL 模式", "type": "enum",
                 "options": ["prefer", "require", "disable", "allow"], "default": "prefer"},
                {"key": "ensure_postgis", "label": "启用 PostGIS", "type": "bool", "default": False},
            ],
        },
    ],
}

_MODE_ENUM = ("create", "overwrite", "append")
_ON_ERROR_ENUM = ("abort", "skip")
_SSL_ENUM = ("prefer", "require", "disable", "allow")


@register
class GdbImportTask(TaskType):
    type = "gdb_import"
    label = "GDB → PostGIS 导入"
    form_schema = _FORM_SCHEMA
    import_source_kind = "gdb"
    source_key = "gdb"
    source_label = "GDB"

    def prepare_config(self, config: dict) -> dict:
        """给具体数据源任务做配置归一；GDB 保持原样。"""
        return config

    # ------------------------------------------------------------ 校验

    def validate(self, config: dict) -> list[str]:
        errs: list[str] = []
        gdb = (config or {}).get("gdb")
        if not gdb:
            errs.append("GDB 路径不能为空")
        elif not os.path.isdir(gdb):
            errs.append(f"GDB 路径不是目录或不存在: {gdb}")
        if (config or {}).get("shp"):
            errs.append("GDB 任务不能同时配置 shp")

        db = (config or {}).get("database") or {}
        for k in ("host", "dbname", "user"):
            if not db.get(k):
                errs.append(f"database.{k} 不能为空")
        port = db.get("port")
        if port is not None and not (isinstance(port, int) and 0 < port < 65536):
            errs.append(f"database.port 必须为 1-65535 的整数: {port!r}")
        if db.get("ssl") and db["ssl"] not in _SSL_ENUM:
            errs.append(f"database.ssl 非法: {db['ssl']}（可选 {_SSL_ENUM}）")

        default = (config or {}).get("default") or {}
        srid = default.get("srid")
        if srid is not None and not (isinstance(srid, int) and srid > 0):
            errs.append(f"default.srid 必须为正整数: {srid!r}")
        if default.get("mode") and default["mode"] not in _MODE_ENUM:
            errs.append(f"default.mode 非法: {default['mode']}（可选 {_MODE_ENUM}）")
        if default.get("on_error") and default["on_error"] not in _ON_ERROR_ENUM:
            errs.append(f"default.on_error 非法: {default['on_error']}（可选 {_ON_ERROR_ENUM}）")

        layers = (config or {}).get("layers") or []
        for i, r in enumerate(layers):
            if not isinstance(r, dict):
                errs.append(f"图层规则[{i}] 必须是对象")
                continue
            if not r.get("source"):
                errs.append(f"图层规则[{i}].source 不能为空")
            if r.get("mode") and r["mode"] not in _MODE_ENUM:
                errs.append(f"图层规则[{i}].mode 非法: {r['mode']}")
            if r.get("srid") is not None and not (isinstance(r["srid"], int) and r["srid"] > 0):
                errs.append(f"图层规则[{i}].srid 必须为正整数: {r['srid']!r}")
        return errs

    # ------------------------------------------------------------ 预览

    def preview(self, config: dict) -> dict:
        cfg = ImportConfig.from_dict(self.prepare_config(config))
        out = {
            self.source_key: cfg.source_path(),
            "layers": [],
            "postgis": None,
            "db_checks": [],
            "error": None,
            "db_error": None,
        }
        try:
            plans = plan_all(cfg, source_kind=self.import_source_kind)
        except Exception as e:  # noqa: BLE001 GDB 打不开等，预览不致命
            out["error"] = f"读取 {self.source_label} 失败: {e}"
            return out
        for p in plans:
            out["layers"].append({
                "source": p.source,
                "table": p.table,
                "schema": p.schema,
                "mode": p.mode,
                "feature_count": p.feature_count,
                "geometry": p.geometry_pg,
                "srid": p.srid,
                "columns": [{"src": s, "dst": d, "pg": t} for s, d, t in p.columns],
                "pk_source": p.pk_source,
                "pk_field": p.pk_field,
                "pk_column": p.pk_column,
                "issues": p.issues,
                "errors": p.errors,
            })
        # 目标库只读检查：失败不阻断预览，单列 db_error
        try:
            with PgWriter(cfg.database) as pg:
                out["postgis"] = (pg.postgis_version() or "").strip() or None
                for p in plans:
                    if pg.table_exists(p.schema, p.table):
                        hint = ("将重建" if p.mode == "overwrite"
                                else ("将追加" if p.mode == "append"
                                      else "已存在且 mode=create 会失败"))
                        out["db_checks"].append(f"表 {p.schema}.{p.table} 已存在 -> {hint}")
        except Exception as e:  # noqa: BLE001
            out["db_error"] = f"目标库检查失败: {e}"
        return out

    # ------------------------------------------------------------ 执行

    def run(self, ctx: TaskContext, config: dict) -> dict:
        cfg = ImportConfig.from_dict(self.prepare_config(config))

        def _log(msg: str):
            # 任务日志入库（前端面板展示）并镜像到服务端控制台，便于调试
            ctx.log(msg)
            print(f"[任务{ctx.task_id}] {msg}", flush=True)

        def _progress(i: int, total: int, source: str, rows: int):
            ctx.set_progress({
                "layer_index": i, "layer_total": total,
                "source": source, "rows": rows,
            })

        try:
            stats = import_run(cfg, log=_log, progress=_progress, cancel=ctx.cancel,
                               source_kind=self.import_source_kind)
        except ImportCancelled:
            raise TaskCancelled() from None
        except Exception:
            if ctx.cancel():  # 失败且用户在取消 -> 归为取消
                raise TaskCancelled() from None
            raise
        return {
            "ok": [{"source": s, "rows": n} for s, n in stats["ok"]],
            "skipped": [{"source": s, "message": m} for s, m in stats["skipped"]],
            "failed": [{"message": m} for m in stats["failed"]],
        }

    # ------------------------------------------------------------ 脱敏

    def sanitize(self, config: dict) -> dict:
        c = copy.deepcopy(config or {})
        db = c.get("database")
        if isinstance(db, dict) and db.get("password") is not None:
            db["password"] = "***"
        return c
