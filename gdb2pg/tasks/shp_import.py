# -*- coding: utf-8 -*-
"""shp_import 任务类型：把 ESRI Shapefile 导入 PostgreSQL/PostGIS。

SHP 任务复用 GDB 任务的动态表单、计划、事务和任务状态桥接；唯一的
数据源差异是 ``shp`` 为单个 .shp 主文件（sidecar 文件放在同目录）或
包含多个 Shapefile 的目录；默认保留 DBF 字段，并使用目标表自增主键。
"""

from __future__ import annotations

import copy
import os

from .base import register
from .gdb_import import (
    GdbImportTask,
    _FORM_SCHEMA,
    _MODE_ENUM,
    _ON_ERROR_ENUM,
    _SSL_ENUM,
)


def _shp_form_schema() -> dict:
    schema = copy.deepcopy(_FORM_SCHEMA)
    for group in schema["groups"]:
        if group["key"] == "gdb":
            group["key"] = "shp"
            group["label"] = "数据源（SHP）"
            field = group["fields"][0]
            field.update({
                "key": "shp",
                "label": "SHP 路径",
                "type": "shp_path",
                "help": "服务器本地 .shp 文件；同目录的 .shx/.dbf/.prj/.cpg 会自动读取",
            })
        elif group["key"] == "layers":
            group["label"] = "图层选择与导入模式（填写 SHP 路径后自动读取，逐行设置）"
            for action in group.get("actions", []):
                if action["key"] == "read_gdb":
                    action.update({"key": "read_shp", "label": "读取图层"})
            table = next((field for field in group["fields"]
                          if field.get("type") == "table"), None)
            if table is not None:
                table["columns"].append({
                    "key": "pk_field",
                    "label": "主键字段（可选）",
                    "type": "field",
                })
        elif group["key"] == "default":
            # SHP 不暴露 GDB 专用的 fid_pk/pk_column 开关；默认规则提供
            # pk_field，作为各图层主键字段的继承值，图层规则可覆盖。
            group["fields"] = [field for field in group["fields"]
                                if field["key"] not in ("fid_pk", "pk_column")]
            group["fields"].append({
                "key": "pk_field",
                "label": "默认主键字段（可选）",
                "type": "text",
                "default": "",
                "help": "填写源 DBF 字段名；各图层可在图层规则中单独覆盖，留空则使用自增主键",
            })
            for field in group["fields"]:
                if field["key"] == "geom_column":
                    field["help"] = "默认 shape；若与源字段同名会自动调整几何列名，保留源字段"
    return schema


@register
class ShpImportTask(GdbImportTask):
    type = "shp_import"
    label = "SHP → PostGIS 导入"
    form_schema = _shp_form_schema()
    import_source_kind = "shp"
    source_key = "shp"
    source_label = "SHP"

    def prepare_config(self, config: dict) -> dict:
        """SHP 默认保留源字段并使用自增主键；兼容显式旧 fid_pk 配置。"""
        out = copy.deepcopy(config or {})
        defaults = out.setdefault("default", {})
        if isinstance(defaults, dict):
            defaults.setdefault("fid_pk", False)
            defaults.setdefault("launder_columns", False)
            defaults.setdefault("pk_field", "")
        return out

    def validate(self, config: dict) -> list[str]:
        errs: list[str] = []
        shp = (config or {}).get("shp")
        if not shp:
            errs.append("SHP 路径不能为空")
        elif os.path.isfile(shp):
            if not shp.lower().endswith(".shp"):
                errs.append(f"SHP 路径必须是 .shp 文件: {shp}")
        elif os.path.isdir(shp):
            try:
                has_shp = any(name.lower().endswith(".shp")
                              for name in os.listdir(shp))
            except OSError:
                has_shp = False
            if not has_shp:
                errs.append(f"SHP 目录中未找到 .shp 文件: {shp}")
        else:
            errs.append(f"SHP 路径不是文件/目录或不存在: {shp}")
        if (config or {}).get("gdb"):
            errs.append("SHP 任务不能同时配置 gdb")

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
        if default.get("pk_field") is not None and not isinstance(default["pk_field"], str):
            errs.append("default.pk_field 必须为字符串")
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
            if r.get("pk_field") is not None and not isinstance(r["pk_field"], str):
                errs.append(f"图层规则[{i}].pk_field 必须为字符串")
            if r.get("mode") and r["mode"] not in _MODE_ENUM:
                errs.append(f"图层规则[{i}].mode 非法: {r['mode']}")
            if r.get("srid") is not None and not (isinstance(r["srid"], int) and r["srid"] > 0):
                errs.append(f"图层规则[{i}].srid 必须为正整数: {r['srid']!r}")
        return errs


__all__ = ["ShpImportTask"]
