# -*- coding: utf-8 -*-
"""导入编排：配置 -> 图层计划 -> dry-run 报告 / 正式导入。

设计要点：
- importer.plan_all(config) 纯计算（不连库），可被 GUI 直接调用做预览；
- dry_run(config) 连库只看元数据（PostGIS 版本、表是否已存在）；
- run(config) 正式导入：每层一个事务，失败按 on_error 处理；
- run / dry_run 支持可选回调 log / progress / cancel，供 Web 任务框架
  上报进度、落库日志与协作式取消；不传时保持原有 print 行为（CLI 不变）。
"""

from __future__ import annotations

import fnmatch
import threading
import time
import weakref
from typing import Callable, Optional

from . import gdb_reader, shp_reader
from .model import ImportConfig, LayerPlan, LayerRule
from .pg_writer import PgWriter
from .schema_mapper import build_layer_plan


class ImportCancelled(Exception):
    """协作式取消信号：cancel 回调返回 True 时抛出，由上层决定状态。"""


def _source_reader(config: ImportConfig, source_kind: Optional[str] = None):
    """选择读取器。

    任务类型可显式传 source_kind，避免某个旧 GDB 配置中意外出现扩展
    字段后改变行为；未显式指定时仍支持按配置自动判断，方便 CLI 直接使用。
    """
    kind = source_kind or config.source_kind()
    if kind == "shp":
        path = config.shp or config.gdb
        if not path:
            raise ValueError("SHP 路径不能为空")
        return shp_reader, path, "SHP"
    if kind == "gdb":
        if not config.gdb:
            raise ValueError("GDB 路径不能为空")
        return gdb_reader, config.gdb, "GDB"
    raise ValueError(f"未知数据源类型: {kind}")


def _open_source(reader, path: str):
    if reader is shp_reader:
        return reader.open_shp(path)
    return reader.open_gdb(path)


# ---------------------------------------------------------------- 计划构建

def _match_selectors(name: str, selectors: dict) -> bool:
    inc = selectors.get("include", ["*"])
    exc = selectors.get("exclude", [])
    hit = any(fnmatch.fnmatch(name, pat) for pat in inc) or not inc
    if exc:
        hit = hit and not any(fnmatch.fnmatch(name, pat) for pat in exc)
    return hit


def plan_all(config: ImportConfig, *, source_kind: Optional[str] = None) -> list[LayerPlan]:
    """对配置数据源内所有图层生成导入计划（不连库）。

    图层选择语义：
    - 显式规则（config.layers）非空 → 只导入规则命中的图层
      （表格中删除的行不会因 selectors 兜底被加回）；
    - 无任何显式规则 → 用选择器（selectors，默认 include=*）兜底全部。
    """
    reader, source_path, _source_label = _source_reader(config, source_kind)
    ds = _open_source(reader, source_path)
    layer_names = reader.list_layers(ds)

    rules = config.layer_rules()
    if rules:
        # 显式规则模式：仅规则命中的图层（精确名或通配符）
        rule_by_src = {}
        for r in rules:
            for name in layer_names:
                if fnmatch.fnmatch(name, r.source):
                    rule_by_src[name] = r
        selected = [(name, rule_by_src[name]) for name in layer_names
                    if name in rule_by_src]
    else:
        # 选择器模式：无显式规则时的兜底（默认 include=* 全选）
        selected = [(name, None) for name in layer_names
                    if _match_selectors(name, config.selectors)]

    plans: list[LayerPlan] = []
    for name, rule in selected:
        if rule is None:
            rule = LayerRule(source=name)
        meta = reader.layer_meta(ds.GetLayerByName(name))
        plan = build_layer_plan(
            name, rule, meta, config.default, config.database.schema,
            source_kind=source_kind or config.source_kind(),
        )
        _downgrade_curve_geometry(ds, name, plan, reader=reader)
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
    "pointm", "linestringm", "polygonm",
    "multipointm", "multilinestringm", "multipolygonm",
    "pointzm", "linestringzm", "polygonzm",
    "multipointzm", "multilinestringzm", "multipolygonzm",
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


def _acceptable_for(geom_pg: str, *, strict_dimension: bool = False) -> set:
    """返回目标 typmod 可接受的要素类型集合。

    GDB 的旧观察器只返回基础名称，为保持既有兼容矩阵默认放行 Z 变体；
    SHP 观察器能识别 Z/M/ZM，因此启用严格维度检查避免 COPY 时才失败。
    """
    value = geom_pg.lower()
    dimension = ""
    for suffix in ("zm", "z", "m"):
        if value.endswith(suffix):
            dimension = suffix
            value = value[:-len(suffix)]
            break
    fam = _FAMILY.get(value)
    if not fam:
        return set()
    if strict_dimension:
        return {f + dimension for f in fam}
    return fam | {f + suffix for f in fam for suffix in ("z", "m", "zm")}


def _downgrade_curve_geometry(ds, layer_name: str, plan: LayerPlan, *, reader=gdb_reader):
    """探测实际要素几何：出现曲线/曲面，或类型超出列类型兼容族时降级泛型。"""
    if plan.geometry_pg is None or plan.geometry_pg.startswith("GENERIC"):
        return
    try:
        observer = getattr(reader, "observed_geometry_dimensions", None)
        names = (observer(ds.GetLayerByName(layer_name)) if observer is not None
                 else reader.observed_geometry_names(ds.GetLayerByName(layer_name)))
    except Exception:
        return
    if not names:
        return
    strict_dimension = getattr(reader, "observed_geometry_dimensions", None) is not None
    if (not names.issubset(_BASIC_GEOMS)
            or not names.issubset(_acceptable_for(
                plan.geometry_pg, strict_dimension=strict_dimension))):
        declared = plan.geometry_pg
        plan.geometry_pg = "GENERIC"
        plan.issues.append(
            f"实测几何类型 {sorted(names)} 与图层声明 {declared} 不兼容，"
            f"降级为泛型 geometry 列"
        )


# ---------------------------------------------------------------- 报告

def format_plan(plan: LayerPlan, indent: str = "  ", source_label: str = "GDB") -> list[str]:
    lines = [
        f"{indent}源图层 : {plan.source}",
        f"{indent}目标表 : {plan.schema}.{plan.table}  (mode={plan.mode})",
        f"{indent}要素数 : {plan.feature_count}",
    ]
    pk_source = getattr(plan, "pk_source", None) or ("fid" if plan.fid_pk else "auto")
    if pk_source == "field":
        pk_text = f"{plan.pk_column}（SHP 源字段 {plan.pk_field}）"
    elif pk_source == "fid":
        pk_text = f"{plan.pk_column}（{source_label} 原生 FID/OBJECTID）"
    else:
        pk_text = f"{plan.pk_column} 自增 bigserial"
    lines.append(f"{indent}主键   : {pk_text}")
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


# ---------------------------------------------------------------- 执行

def _features_with_srid(lyr, srs, srid, *, reader=gdb_reader):
    """迭代要素，并把 EWKB 头部 SRID 修正为计划值。产出 (attrs, ewkb, fid)。"""
    for attrs, ewkb, fid in reader.iter_features(lyr, srs):
        if ewkb is not None and srid:
            ewkb = reader.set_ewkb_srid(ewkb, srid)
        yield attrs, ewkb, fid


def dry_run(config: ImportConfig, *, log: Callable[[str], None] = print,
             source_kind: Optional[str] = None) -> list[LayerPlan]:
    """只读检查：连接目标库（取元数据），输出全部计划与现存表冲突。"""
    plans = plan_all(config, source_kind=source_kind)
    _reader, source_path, source_label = _source_reader(config, source_kind)
    log("=" * 70)
    log(f"{source_label}      : {source_path}")
    log(f"目标库   : {config.database.redacted()}")
    log(f"图层计划 : {len(plans)} 个")
    log("=" * 70)
    for i, p in enumerate(plans, 1):
        log(f"[{i}/{len(plans)}]")
        for line in format_plan(p, source_label=source_label):
            log(line)
        log("")

    with PgWriter(config.database) as pg:
        pgv = pg.postgis_version()
        if pgv is None:
            log("[注意] 目标库未检测到 PostGIS（无 PostGIS_Version() 函数）")
            if not config.database.ensure_postgis:
                log("       可配置 database.ensure_postgis=true 尝试自动启用")
        else:
            log(f"PostGIS : {pgv.strip()}")
        for p in plans:
            exists = pg.table_exists(p.schema, p.table)
            if exists:
                hint = "将重建" if p.mode == "overwrite" else ("将追加" if p.mode == "append" else "已存在且 mode=create 会失败")
                log(f"[检查] 表 {p.schema}.{p.table} 已存在 -> {hint}")
    return plans


# 单层事务内语句超时（毫秒）：优先级最低的纯兜底，防止语句永不返回
# （如服务端异常挂起）。注意不能设太短：332 万行级大图层的 COPY 语句
# 本身可能跑 40+ 分钟，误杀会把已写入的数据整体回滚。精准的"卡死"
# 判定由任务层的无进展看护负责（基于 progress 心跳，见 manager）。
STMT_TIMEOUT_MS = 6 * 60 * 60 * 1000  # 6 小时
# 单层事务内锁等待超时（毫秒）：撞上旧事务锁时自动失败，不再无限等待
LOCK_TIMEOUT_MS = 60 * 1000  # 60 秒

# 当前线程持有的目标库连接（weakref，避免持有 writer 实例）
_active_writers: dict[int, weakref.ref] = {}


def _register_writer(pg: PgWriter):
    _active_writers[threading.get_ident()] = weakref.ref(pg)


def _unregister_writer():
    _active_writers.pop(threading.get_ident(), None)


def close_writer_for_thread(tid: int):
    """尽力关闭指定线程持有的数据库连接。

    取消兜底用：任务线程卡死（如 GDB 读取阻塞）无法自行退出时，从外部
    断开其连接 -> 服务端会话终止 -> 事务回滚 -> 锁立即释放。
    """
    ref = _active_writers.get(tid)
    if ref is None:
        return
    w = ref()
    if w is not None:
        try:
            w.close()
        except Exception:  # noqa: BLE001 连接可能正被占用中关闭，尽力而为
            pass


def run(config: ImportConfig, *,
        log: Callable[[str], None] = print,
        progress: Optional[Callable[[int, int, str, int], None]] = None,
        cancel: Optional[Callable[[], bool]] = None,
        source_kind: Optional[str] = None) -> dict:
    """正式导入。返回汇总统计。

    - log(line)：替代 print 输出日志（默认打印到 stdout）；
    - progress(i, total, source, rows)：第 i/total 层、source 图层，已写 rows 行；
    - cancel()：返回 True 时中止（COPY 循环内协作检查），抛 ImportCancelled。
    """
    if cancel is not None and cancel():
        raise ImportCancelled("任务已请求取消")
    plans = plan_all(config, source_kind=source_kind)
    with PgWriter(config.database) as pg:
        _register_writer(pg)
        try:
            if config.database.ensure_postgis:
                pg.ensure_postgis()
            pg.ensure_schema(config.database.schema)
            pgv = pg.postgis_version()
            if pgv is None:
                raise RuntimeError(
                    "目标库没有 PostGIS；可设置 database.ensure_postgis=true 或先在库里 CREATE EXTENSION postgis")

            stats = {"ok": [], "skipped": [], "failed": []}
            for i, plan in enumerate(plans, 1):
                if cancel is not None and cancel():
                    raise ImportCancelled("任务已请求取消")
                if plan.errors:
                    stats["skipped"].append((plan.source, "; ".join(plan.errors)))
                    log(f"[{i}/{len(plans)}] [跳过] {plan.source}: {plan.errors[0]}")
                    continue
                try:
                    _import_one(pg, config, plan, i, len(plans), log, progress, cancel,
                                source_kind=source_kind)
                    stats["ok"].append((plan.source, plan.feature_count))
                except ImportCancelled:
                    raise
                except Exception as e:
                    if config.default.on_error == "skip":
                        stats["failed"].append(f"{plan.source}: {e}")
                        log(f"[{i}/{len(plans)}] [失败-跳过] {plan.source}: {e}")
                        continue
                    raise

            log("=" * 70)
            log(f"完成：成功 {len(stats['ok'])}，跳过 {len(stats['skipped'])}，失败 {len(stats['failed'])}")
            for s, msg in stats["skipped"]:
                log(f"  [跳过] {s}: {msg}")
            for m in stats["failed"]:
                log(f"  [失败] {m}")
            return stats
        finally:
            _unregister_writer()


def _import_one(pg: PgWriter, config: ImportConfig, plan: LayerPlan, i: int, total: int,
                log: Callable[[str], None],
                progress: Optional[Callable[[int, int, str, int], None]],
                cancel: Optional[Callable[[], bool]],
                source_kind: Optional[str] = None):
    """导入单个图层（单事务，失败整体回滚）。"""
    reader, source_path, source_label = _source_reader(config, source_kind)
    ds = _open_source(reader, source_path)
    try:
        lyr = ds.GetLayerByName(plan.source)
        meta = reader.layer_meta(lyr)
        srs = meta["srs"]

        t0 = time.time()
        # 单个图层一个事务：DDL + COPY + 索引要么全成要么全退（失败不留残表）
        pg.conn.autocommit = False
        # 事务内语句超时兜底（默认 30 分钟）：COPY/DDL 若因 GDB 读取卡死
        # 挂起（服务端空等数据），超时自动失败 -> 回滚 -> 释放锁，避免
        # 长期占锁导致后续重跑建表被阻塞
        cur_tmp = pg.conn.cursor()
        # SET 不支持 bind 参数；值均为整数字面量，直接拼接（无注入面）
        cur_tmp.execute(f"SET LOCAL statement_timeout = {STMT_TIMEOUT_MS}")
        cur_tmp.execute(f"SET LOCAL lock_timeout = {LOCK_TIMEOUT_MS}")
        cur_tmp.close()
        try:
            if plan.mode == "append":
                if not pg.table_exists(plan.schema, plan.table):
                    raise RuntimeError(f"append 但表 {plan.schema}.{plan.table} 不存在")
                log(f"[{i}/{total}] 追加写入 {plan.schema}.{plan.table}")
            else:  # create | overwrite
                if plan.mode == "overwrite" and pg.table_exists(plan.schema, plan.table):
                    # 覆盖模式：先删旧表（表结构/数据随之清空，事务内，失败整体回滚）
                    log(f"[{i}/{total}] 覆盖模式：删除并重建 {plan.schema}.{plan.table}"
                        f"（清空原有数据）")
                    pg.drop_table(plan.schema, plan.table)
                elif plan.mode == "overwrite":
                    log(f"[{i}/{total}] 覆盖模式：{plan.schema}.{plan.table} 不存在，直接建表")
                else:
                    pk_source = getattr(plan, "pk_source", None) or (
                        "fid" if plan.fid_pk else "auto")
                    if pk_source == "field":
                        pk_text = f"{plan.pk_column}/SHP 源字段 {plan.pk_field}"
                    elif pk_source == "fid":
                        pk_text = f"{plan.pk_column}/{source_label} OBJECTID"
                    else:
                        pk_text = f"{plan.pk_column}/自增 bigserial"
                    log(f"[{i}/{total}] 动态建表 {plan.schema}.{plan.table}"
                        f"（{plan.feature_count} 行源数据，主键={pk_text}）")
                pg.create_table(plan)

            def _prog(n: int):
                if progress is not None:
                    progress(i, total, plan.source, n)
                # COPY 阶段每 5000 行在日志刷一条，记录读取/写入推进
                if n > 0 and n % 5000 == 0:
                    log(f"[{i}/{total}] 正在写入 {plan.source}… 已 {n} 行")

            log(f"[{i}/{total}] 开始读取要素并写入 {plan.source}…（OGR 逐要素迭代）")
            # 首要素打点：区分「卡在打开/首要素读取」与「读取中但慢」
            def _feat_gen():
                first = True
                t_first = time.time()
                for attrs, ewkb, _fid in _features_with_srid(
                        lyr, srs, plan.srid, reader=reader):
                    if first:
                        log(f"[{i}/{total}] 已读取第一个要素（耗时 {time.time() - t_first:.1f}s），开始逐批写入…")
                        first = False
                    yield attrs, ewkb, _fid

            n = pg.copy_features(
                plan,
                _feat_gen(),
                progress=_prog,
                cancel=cancel,
            )
            if plan.geometry_pg is not None and config.default.create_spatial_index:
                log(f"[{i}/{total}] 创建 GIST 索引 {plan.schema}.{plan.table}_geom")
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
            counter_name = "OpenFileGDB" if source_label == "GDB" else "SHP"
            log(f"      [警告] 导入 {actual} 行，源计数 {plan.feature_count}"
                f"（{counter_name} 计数误差或数据变化）")
        dt = time.time() - t0
        log(f"[{i}/{total}] {plan.source} -> {plan.schema}.{plan.table}  共 {n} 行  ({dt:.1f}s)")
    finally:
        ds = None
