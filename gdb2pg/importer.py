# -*- coding: utf-8 -*-
"""导入编排：配置 -> 图层计划 -> dry-run 报告 / 正式导入。

设计要点：
- importer.plan_all(config) 纯计算（不连库），可被 GUI 直接调用做预览；
- dry_run(config) 连库只看元数据（PostGIS 版本、表是否已存在）；
- run(config) 正式导入：每层一个事务，失败按 on_error 处理。
"""

from __future__ import annotations

import fnmatch
import time
from typing import Optional

from . import gdb_reader
from .model import ImportConfig, LayerPlan, LayerRule
from .pg_writer import PgWriter
from .schema_mapper import build_layer_plan


# ---------------------------------------------------------------- 计划构建

def _match_selectors(name: str, selectors: dict) -> bool:
    inc = selectors.get("include", ["*"])
    exc = selectors.get("exclude", [])
    hit = any(fnmatch.fnmatch(name, pat) for pat in inc) or not inc
    if exc:
        hit = hit and not any(fnmatch.fnmatch(name, pat) for pat in exc)
    return hit


def plan_all(config: ImportConfig) -> list[LayerPlan]:
    """对 GDB 内所有图层生成导入计划（不连库）。"""
    ds = gdb_reader.open_gdb(config.gdb)
    layer_names = gdb_reader.list_layers(ds)

    rules = config.layer_rules()
    # 显式规则：精确名或通配符匹配
    rule_by_src = {}
    for r in rules:
        for name in layer_names:
            if fnmatch.fnmatch(name, r.source):
                rule_by_src[name] = r

    plans: list[LayerPlan] = []
    for name in layer_names:
        rule = rule_by_src.get(name)
        if rule is None and not _match_selectors(name, config.selectors):
            continue  # 既无显式规则也不被选择器选中
        if rule is None:
            rule = LayerRule(source=name)
        meta = gdb_reader.layer_meta(ds.GetLayerByName(name))
        plan = build_layer_plan(name, rule, meta, config.default, config.database.schema)
        _downgrade_curve_geometry(ds, name, plan)
        plans.append(plan)

    # 表名碰撞检测
    seen = {}
    for p in plans:
        key = (p.schema, p.table)
        if key in seen:
            msg = f"目标表 {p.schema}.{p.table} 与图层 [{seen[key]}] 冲突"
            p.errors.append(msg)
        else:
            seen[key] = p.source
    return plans


# 基础几何类型（含 3D 变体）；曲线/表面/集合类不在其中
_BASIC_GEOMS = {
    "point", "linestring", "polygon",
    "multipoint", "multilinestring", "multipolygon",
    "pointz", "linestringz", "polygonz",
    "multipointz", "multilinestringz", "multipolygonz",
}
# 各列类型可接受的要素类型家族（PostGIS typmod 兼容矩阵）
_FAMILY = {
    "point": {"point"},
    "multipoint": {"point", "multipoint"},
    "linestring": {"linestring"},
    "multilinestring": {"linestring", "multilinestring"},
    "polygon": {"polygon"},
    "multipolygon": {"polygon", "multipolygon"},
}


def _acceptable_for(geom_pg: str) -> set:
    fam = _FAMILY.get(geom_pg.lower())
    if not fam:
        return set()
    return fam | {f + "z" for f in fam}


def _downgrade_curve_geometry(ds, layer_name: str, plan: LayerPlan):
    """探测实际要素几何：出现曲线/曲面，或类型超出列类型兼容族时降级泛型。"""
    if plan.geometry_pg is None or plan.geometry_pg == "GENERIC":
        return
    try:
        names = gdb_reader.observed_geometry_names(ds.GetLayerByName(layer_name))
    except Exception:
        return
    if not names:
        return
    if not names.issubset(_BASIC_GEOMS) or not names.issubset(_acceptable_for(plan.geometry_pg)):
        declared = plan.geometry_pg
        plan.geometry_pg = "GENERIC"
        plan.issues.append(
            f"实测几何类型 {sorted(names)} 与图层声明 {declared} 不兼容，"
            f"降级为泛型 geometry 列"
        )


# ---------------------------------------------------------------- 报告

def format_plan(plan: LayerPlan, indent: str = "  ") -> list[str]:
    lines = [
        f"{indent}源图层 : {plan.source}",
        f"{indent}目标表 : {plan.schema}.{plan.table}  (mode={plan.mode})",
        f"{indent}要素数 : {plan.feature_count}",
    ]
    if plan.geometry_pg is not None:
        srid = plan.srid if plan.srid is not None else "?"
        lines.append(f"{indent}几何   : {plan.geom_column} geometry({plan.geometry_pg}, {srid})")
    else:
        lines.append(f"{indent}几何   : 无（纯属性表）")
    if plan.columns:
        lines.append(f"{indent}字段   : " + ", ".join(f"{s}->{d}:{t}" for s, d, t in plan.columns))
    for w in plan.issues:
        lines.append(f"{indent}[警告] {w}")
    for e in plan.errors:
        lines.append(f"{indent}[错误] {e}")
    return lines


def _print_plans(plans: list[LayerPlan]):
    for i, p in enumerate(plans, 1):
        print(f"[{i}/{len(plans)}]")
        for line in format_plan(p):
            print(line)
        print()


# ---------------------------------------------------------------- 执行

def _features_with_srid(lyr, srs, srid):
    """迭代要素，并把 EWKB 头部 SRID 修正为计划值。"""
    for attrs, ewkb in gdb_reader.iter_features(lyr, srs):
        if ewkb is not None and srid:
            ewkb = gdb_reader.set_ewkb_srid(ewkb, srid)
        yield attrs, ewkb


def dry_run(config: ImportConfig) -> list[LayerPlan]:
    """只读检查：连接目标库（取元数据），输出全部计划与现存表冲突。"""
    plans = plan_all(config)
    print("=" * 70)
    print(f"GDB      : {config.gdb}")
    print(f"目标库   : {config.database.redacted()}")
    print(f"图层计划 : {len(plans)} 个")
    print("=" * 70)
    _print_plans(plans)

    with PgWriter(config.database) as pg:
        pgv = pg.postgis_version()
        if pgv is None:
            print("[注意] 目标库未检测到 PostGIS（无 PostGIS_Version() 函数）")
            if not config.database.ensure_postgis:
                print("       可配置 database.ensure_postgis=true 尝试自动启用")
        else:
            print(f"PostGIS : {pgv.strip()}")
        for p in plans:
            exists = pg.table_exists(p.schema, p.table)
            if exists:
                hint = "将重建" if p.mode == "overwrite" else ("将追加" if p.mode == "append" else "已存在且 mode=create 会失败")
                print(f"[检查] 表 {p.schema}.{p.table} 已存在 -> {hint}")
    return plans


def run(config: ImportConfig) -> dict:
    """正式导入。返回汇总统计。"""
    plans = plan_all(config)
    with PgWriter(config.database) as pg:
        if config.database.ensure_postgis:
            pg.ensure_postgis()
        pg.ensure_schema(config.database.schema)
        pgv = pg.postgis_version()
        if pgv is None:
            raise RuntimeError(
                "目标库没有 PostGIS；可设置 database.ensure_postgis=true 或先在库里 CREATE EXTENSION postgis")

        stats = {"ok": [], "skipped": [], "failed": []}
        for i, plan in enumerate(plans, 1):
            if plan.errors:
                stats["skipped"].append((plan.source, "; ".join(plan.errors)))
                print(f"[{i}/{len(plans)}] [跳过] {plan.source}: {plan.errors[0]}")
                continue
            try:
                _import_one(pg, config, plan, i, len(plans))
                stats["ok"].append((plan.source, plan.feature_count))
            except Exception as e:
                if config.default.on_error == "skip":
                    stats["failed"].append(f"{plan.source}: {e}")
                    print(f"[{i}/{len(plans)}] [失败-跳过] {plan.source}: {e}")
                    continue
                raise

        print("=" * 70)
        print(f"完成：成功 {len(stats['ok'])}，跳过 {len(stats['skipped'])}，失败 {len(stats['failed'])}")
        for s, msg in stats["skipped"]:
            print(f"  [跳过] {s}: {msg}")
        for m in stats["failed"]:
            print(f"  [失败] {m}")
        return stats


def _import_one(pg: PgWriter, config: ImportConfig, plan: LayerPlan, i: int, total: int):
    """导入单个图层（单事务，失败整体回滚）。"""
    ds = gdb_reader.open_gdb(config.gdb)
    try:
        lyr = ds.GetLayerByName(plan.source)
        meta = gdb_reader.layer_meta(lyr)
        srs = meta["srs"]

        t0 = time.time()
        # 单个图层一个事务：DDL + COPY + 索引要么全成要么全退（失败不留残表）
        pg.conn.autocommit = False
        try:
            if plan.mode == "append":
                if not pg.table_exists(plan.schema, plan.table):
                    raise RuntimeError(f"append 但表 {plan.schema}.{plan.table} 不存在")
            else:  # create | overwrite
                if plan.mode == "overwrite" and pg.table_exists(plan.schema, plan.table):
                    pg.drop_table(plan.schema, plan.table)
                pg.create_table(plan)

            n = pg.copy_features(
                plan,
                _features_with_srid(lyr, srs, plan.srid),
                progress=lambda n: print(f"      ...{n} 条", end="\r"),
            )
            if plan.geometry_pg is not None and config.default.create_spatial_index:
                pg.create_spatial_index(plan)
            pg.conn.commit()
        except Exception:
            pg.conn.rollback()
            raise
        finally:
            pg.conn.autocommit = True

        # 对账
        actual = pg.count_rows(plan.schema, plan.table)
        if actual != plan.feature_count:
            print(f"      [警告] 导入 {actual} 行，源计数 {plan.feature_count}（OpenFileGDB 计数误差或数据变化）")
        dt = time.time() - t0
        print(f"[{i}/{total}] {plan.source} -> {plan.schema}.{plan.table}  共 {n} 行  ({dt:.1f}s)")
    finally:
        ds = None