# -*- coding: utf-8 -*-
"""SHP 读取层：基于 GDAL/OGR 的 ESRI Shapefile 驱动。

该模块提供与 :mod:`gdb2pg.gdb_reader` 相同的图层读取接口，方便调用方
在不改变 GDB 读取行为的前提下处理单个 ``.shp`` 文件或包含 Shapefile
数据的目录。

核心接口：

* :func:`open_shp`                 -> 打开 SHP 文件/目录数据源；
* :func:`list_layers`              -> 列出图层名；
* :func:`layer_meta`               -> 读取图层元数据；
* :func:`iter_features`            -> 逐要素产出属性、WKB 和 FID；
* :func:`observed_geometry_names`  -> 抽样探测实际几何类型；
* :func:`shp_layers_summary`       -> 生成图层摘要；
* :func:`pg_geom_type`、:func:`set_ewkb_srid` -> 与 GDB 读取层共用的纯函数。

SHP 本身没有 GDB 的 OBJECTID；OGR Shapefile 驱动提供的 FID（通常从 0
开始）仍按原接口返回，调用方不应把它当作 OBJECTID。
"""

from __future__ import annotations

import os
import struct
from pathlib import Path
from typing import Optional

from osgeo import ogr

# 这些函数只依赖 OGR 图层/WKB，直接复用已经稳定的通用实现。这样可以
# 保证 SHP 和 GDB 的字段日期规范化、EWKB 处理、几何类型映射完全一致，
# 同时不需要修改现有 gdb_reader.py 的行为。
from .gdb_reader import (  # noqa: F401  (re-exported compatibility API)
    iter_features as _gdb_iter_features,
    layer_meta as _gdb_layer_meta,
    list_layers,
    observed_geometry_names,
    pg_geom_type,
    set_ewkb_srid,
)


_SHP_DRIVER_NAMES = {"esri shapefile"}


def _dbf_field_names(lyr: ogr.Layer) -> Optional[set[str]]:
    """读取底层 DBF 字段名，用于识别驱动额外暴露的虚拟 FID。"""
    try:
        description = lyr.GetDataset().GetDescription()
        source = Path(description)
        if source.is_file() and source.suffix.lower() == ".shp":
            dbf = source.with_suffix(".dbf")
            if not dbf.is_file():
                dbf = next((p for p in source.parent.iterdir()
                            if p.is_file() and p.suffix.lower() == ".dbf"
                            and p.stem.lower() == source.stem.lower()), None)
        else:
            dbf = next((p for p in source.iterdir()
                        if p.is_file() and p.suffix.lower() == ".dbf"
                        and p.stem.lower() == lyr.GetName().lower()), None)
        if dbf is None or not dbf.is_file():
            return set()
        raw = dbf.read_bytes()
        if len(raw) < 32:
            return None
        header_len = struct.unpack_from("<H", raw, 8)[0]
        names: set[str] = set()
        for offset in range(32, min(header_len, len(raw)), 32):
            if raw[offset] == 0x0D:
                break
            field_raw = raw[offset:offset + 11].split(b"\0", 1)[0]
            if not field_raw:
                continue
            # 字段名通常是 ASCII；这里同时兼容 UTF-8/本地编码字段名。
            names.add(field_raw.decode("utf-8", errors="ignore").casefold())
            names.add(field_raw.decode("latin1", errors="ignore").casefold())
        return names
    except (OSError, StopIteration, struct.error):
        return None


def _synthetic_fid_names(lyr: ogr.Layer) -> set[str]:
    """识别 ESRI 驱动在无 DBF 用户字段时暴露的虚拟 FID 字段。

    该字段通常是 ``FID``/OFTInteger64/宽度 11。通过 DBF 字段描述符确认
    它不是物理字段后才过滤；真实 DBF 字段即使同名也必须原样保留。
    """
    physical = _dbf_field_names(lyr)
    defn = lyr.GetLayerDefn()
    out: set[str] = set()
    for i in range(defn.GetFieldCount()):
        field = defn.GetFieldDefn(i)
        if (field.GetName().upper() == "FID"
                and field.GetType() == ogr.OFTInteger64
                and field.GetWidth() == 11):
            if physical is None or field.GetName().casefold() not in physical:
                out.add(field.GetName())
    return out


def layer_meta(lyr: ogr.Layer) -> dict:
    """读取元数据，并过滤 ESRI 驱动的虚拟 FID 属性列。"""
    meta = _gdb_layer_meta(lyr)
    synthetic = _synthetic_fid_names(lyr)
    if synthetic:
        meta["fields"] = [f for f in meta["fields"] if f["name"] not in synthetic]
    return meta


def iter_features(lyr: ogr.Layer, srs=None):
    """逐要素产出 (属性, WKB, FID)，不重复产出虚拟 FID 属性。"""
    synthetic = _synthetic_fid_names(lyr)
    for attrs, ewkb, fid in _gdb_iter_features(lyr, srs):
        if synthetic:
            attrs = {name: value for name, value in attrs.items()
                     if name not in synthetic}
        yield attrs, ewkb, fid


def observed_geometry_dimensions(lyr: ogr.Layer, limit: int = 200) -> set[str]:
    """采样实际几何并保留 Z/M 维度后缀（point、pointz、pointm、pointzm）。"""
    names: set[str] = set()
    lyr.ResetReading()
    for i, feat in enumerate(lyr):
        if i >= limit:
            break
        geom = feat.GetGeometryRef()
        if geom is None or geom.IsEmpty():
            continue
        name = geom.GetGeometryName().lower()
        suffix = ("z" if geom.Is3D() else "") + ("m" if geom.IsMeasured() else "")
        names.add(name + suffix)
    lyr.ResetReading()
    return names


def open_shp(path: os.PathLike[str] | str) -> ogr.DataSource:
    """以只读方式打开单个 ``.shp`` 或 Shapefile 目录数据源。

    GDAL 的 ESRI Shapefile 驱动既支持将 ``roads.shp`` 作为数据源打开，
    也支持传入目录并自动枚举目录内的 ``*.shp`` 文件。目录中如有多个
    Shapefile，返回的数据源会包含多个图层。

    ``ogr.Open`` 对不存在、损坏或非 Shapefile 数据源有时返回 ``None``，
    有时在启用异常模式时直接抛异常；这里统一转换成易于调用方处理的
    ``RuntimeError``，与 ``open_gdb`` 的错误契约一致。
    """
    source = os.fspath(path)
    try:
        ds = ogr.Open(source, 0)
    except Exception as exc:  # noqa: BLE001 - OGR exception types vary by GDAL
        raise RuntimeError(f"无法打开 SHP: {source}") from exc
    if ds is None:
        raise RuntimeError(f"无法打开 SHP: {source}")

    # 防止把 .gdb/GPKG 等其它 OGR 数据源误当成 SHP 读入。普通文件和
    # 目录数据源均由同一个 ESRI Shapefile 驱动返回该名称。
    try:
        driver = ds.GetDriver()
        driver_name = ""
        if driver is not None:
            # ShortName 是现代绑定的属性；旧版绑定可仅暴露 GetName。
            driver_name = getattr(driver, "ShortName", "") or driver.GetName()
    except Exception:  # noqa: BLE001 - a partially opened datasource
        driver_name = ""
    if driver_name.lower() not in _SHP_DRIVER_NAMES:
        ds = None
        raise RuntimeError(f"不是 ESRI Shapefile 数据源: {source}")
    return ds


def _authority_srid(srs) -> Optional[int]:
    """从 OGR SpatialReference 尽力解析 EPSG authority code。"""
    if srs is None:
        return None
    for node in (None, "GEOGCS", "PROJCS"):
        try:
            code = srs.GetAuthorityCode(node)
            if code:
                return int(code)
        except (TypeError, ValueError):
            continue
    return None


def shp_layers_summary(path: os.PathLike[str] | str) -> list[dict]:
    """读取 SHP 文件/目录内全部图层摘要。

    每项字段与 ``gdb_layers_summary`` 保持一致：
    ``source``、``feature_count``、``geometry``、``srid`` 和 ``fields``。
    """
    ds = open_shp(path)
    out: list[dict] = []
    for i in range(ds.GetLayerCount()):
        lyr = ds.GetLayerByIndex(i)
        meta = layer_meta(lyr)
        out.append({
            "source": lyr.GetName(),
            "feature_count": int(lyr.GetFeatureCount(True)),
            "geometry": pg_geom_type(meta["geom_ogrid"]),
            "srid": _authority_srid(meta["srs"]),
            "fields": [field["name"] for field in meta["fields"]],
        })
    return out


__all__ = [
    "open_shp",
    "list_layers",
    "layer_meta",
    "iter_features",
    "observed_geometry_names",
    "observed_geometry_dimensions",
    "pg_geom_type",
    "set_ewkb_srid",
    "shp_layers_summary",
]
