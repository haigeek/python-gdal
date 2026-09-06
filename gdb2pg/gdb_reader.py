# -*- coding: utf-8 -*-
"""GDB 读取层：基于 GDAL/OGR 的 OpenFileGDB 驱动（只读，免 ESRI 许可）。

核心产出：
- list_layers(ds)                    -> [图层名, ...]
- layer_meta(lyr)                    -> (spatial_ref, geom_type_name, field_defs, feature_count)
- iter_features(lyr, srs)            -> 逐要素产出 (属性dict, EWKB bytes 或 None)
"""

from __future__ import annotations

import datetime as _dt
from typing import Iterator, Optional

from osgeo import gdal, ogr

ogr.UseExceptions()
gdal.SetConfigOption("GDAL_FILENAME_IS_UTF8", "YES")

# OGR 几何类型 -> PG typmod 用的大写类型名（仅支持 PG 原生 typmod 的）
_PG_GEOM_TYPES = {
    ogr.wkbPoint: "POINT",
    ogr.wkbLineString: "LINESTRING",
    ogr.wkbPolygon: "POLYGON",
    ogr.wkbMultiPoint: "MULTIPOINT",
    ogr.wkbMultiLineString: "MULTILINESTRING",
    ogr.wkbMultiPolygon: "MULTIPOLYGON",
    ogr.wkbPoint25D: "POINTZ",
    ogr.wkbLineString25D: "LINESTRINGZ",
    ogr.wkbPolygon25D: "POLYGONZ",
    ogr.wkbMultiPoint25D: "MULTIPOINTZ",
    ogr.wkbMultiLineString25D: "MULTILINESTRINGZ",
    ogr.wkbMultiPolygon25D: "MULTIPOLYGONZ",
}


def open_gdb(path: str) -> ogr.DataSource:
    ds = ogr.Open(path, 0)
    if ds is None:
        raise RuntimeError(f"无法打开 GDB: {path}")
    return ds


def list_layers(ds: ogr.DataSource) -> list[str]:
    return [ds.GetLayerByIndex(i).GetName() for i in range(ds.GetLayerCount())]


def gdb_layers_summary(path: str) -> list[dict]:
    """读取 GDB 全部图层摘要（Web「图层选择」自动填充用）。

    每项：source（图层名）、feature_count（尽力统计）、
    geometry（PG 类型名或 None）、srid（投影坐标系 EPSG，取不到为 None）、
    fields（图层字段名列表，供主键列名等选择）。
    """
    ds = open_gdb(path)
    out: list[dict] = []
    for i in range(ds.GetLayerCount()):
        lyr = ds.GetLayerByIndex(i)
        srs = lyr.GetSpatialRef()
        srid = None
        if srs is not None:
            try:
                srid = int(srs.GetAuthorityCode(None) or srs.GetAuthorityCode("GEOGCS") or 0) or None
            except (TypeError, ValueError):
                srid = None
        field_names = [
            lyr.GetLayerDefn().GetFieldDefn(j).GetName()
            for j in range(lyr.GetLayerDefn().GetFieldCount())
        ]
        out.append({
            "source": lyr.GetName(),
            "feature_count": lyr.GetFeatureCount(True),
            "geometry": pg_geom_type(lyr.GetGeomType()),
            "srid": srid,
            "fields": field_names,
        })
    return out


def layer_meta(lyr: ogr.Layer) -> dict:
    """图层元数据：SRS、OGR 几何类型、字段定义、要素数。"""
    srs = lyr.GetSpatialRef()
    srs_wkt = srs.ExportToWkt() if srs else None
    geom_ogrid = lyr.GetGeomType()
    fields = [
        {
            "name": lyr.GetLayerDefn().GetFieldDefn(i).GetName(),
            "type": lyr.GetLayerDefn().GetFieldDefn(i).GetType(),
            "type_name": lyr.GetLayerDefn().GetFieldDefn(i).GetFieldTypeName(
                lyr.GetLayerDefn().GetFieldDefn(i).GetType()
            ),
            "width": lyr.GetLayerDefn().GetFieldDefn(i).GetWidth(),
            "precision": lyr.GetLayerDefn().GetFieldDefn(i).GetPrecision(),
        }
        for i in range(lyr.GetLayerDefn().GetFieldCount())
    ]
    return {
        "srs": srs,
        "srs_wkt": srs_wkt,
        "geom_ogrid": geom_ogrid,
        "geom_name": ogr.GeometryTypeToName(geom_ogrid),
        "fields": fields,
        "feature_count": lyr.GetFeatureCount(),
    }


def pg_geom_type(geom_ogrid: int) -> Optional[str]:
    """OGR 几何类型 -> PG typmod 类型；不支持/未知返回 None（用泛型 geometry）。"""
    return _PG_GEOM_TYPES.get(geom_ogrid)


def _norm_datetime(feat: ogr.Feature, idx: int, ftype: int):
    """把日期时间字段规范成 PG 可直接适配的 Python 对象。"""
    y, m, d, hh, mm, ss, tz = feat.GetFieldAsDateTime(idx)
    # 亚秒：GDAL 可能以 float 秒返回（如 45.5）
    if isinstance(ss, float):
        us = int(round((ss - int(ss)) * 1_000_000))
        ss = int(ss)
    else:
        us = 0
    if ftype == ogr.OFTDate:
        if not y:
            return None
        try:
            return _dt.date(y, m, d)
        except ValueError:
            return None
    if ftype in (ogr.OFTTime, ogr.OFTDateTime):
        if ftype == ogr.OFTTime:
            try:
                return _dt.time(hh, mm, ss, us) if (hh or mm or ss) else None
            except ValueError:
                return None
        if not y:
            return None
        try:
            val = _dt.datetime(y, m, d, hh, mm, ss, us)
        except ValueError:  # 脏数据（如 m=0），容错为 NULL
            return None
        if tz == 100:  # GMT
            return val.replace(tzinfo=_dt.timezone.utc)
        return val
    return feat.GetField(idx)


def iter_features(lyr: ogr.Layer, srs=None) -> Iterator[tuple[dict, Optional[bytes], int]]:
    """逐要素产出 (属性dict, EWKB bytes, FID)。

    - 属性 dict 以【源字段名】为键，值为 str/int/float/None/date/time/datetime；
    - EWKB 为 NDR 扩展格式（含 SRID），几何为 NULL/缺失时返回 None；
    - FID：OpenFileGDB 即 GDB 内部 OBJECTID（稳定、唯一），可作目标表主键；
    - 调用方负责 patch SRID（见 set_ewkb_srid）。
    """
    defn = lyr.GetLayerDefn()
    n = defn.GetFieldCount()
    names = [defn.GetFieldDefn(i).GetName() for i in range(n)]
    ftypes = [defn.GetFieldDefn(i).GetType() for i in range(n)]
    lyr.ResetReading()
    for feat in lyr:
        attrs = {
            names[i]: _norm_datetime(feat, i, ftypes[i]) if ftypes[i] in (
                ogr.OFTDate, ogr.OFTTime, ogr.OFTDateTime
            ) else feat.GetField(i)
            for i in range(n)
        }
        geom = feat.GetGeometryRef()
        ewkb = None
        if geom is not None and not geom.IsEmpty():
            geom = geom.Clone()
            if srs is not None:
                geom.AssignSpatialReference(srs)
            # 新版绑定 ExportToWkb 产出的 WKB 不含 SRID；SRID 由
            # set_ewkb_srid 在导入侧统一注入（输入若已是 EWKB 则改写头部）
            ewkb = geom.ExportToWkb(ogr.wkbNDR)
        yield attrs, ewkb, feat.GetFID()


def observed_geometry_names(lyr: ogr.Layer, limit: int = 200) -> set:
    """采样前 limit 个要素，返回实际出现的几何类型名集合（小写）。

    GDB 里常见"图层类型是 Multi Line String、个别要素实为曲线"的脏数据，
    PostGIS typmod 会拒绝曲线写入 LINESTRING/MULTILINESTRING 列，
    因此导入前先探测，必要时降级为泛型 geometry 列。
    """
    names: set = set()
    lyr.ResetReading()
    for i, feat in enumerate(lyr):
        if i >= limit:
            break
        g = feat.GetGeometryRef()
        if g is not None and not g.IsEmpty():
            names.add(g.GetGeometryName().lower())
    lyr.ResetReading()
    return names


def set_ewkb_srid(ewkb: bytes, srid: int) -> bytes:
    """返回带指定 SRID 的 NDR EWKB。

    - 输入已是 EWKB（类型位含 0x20000000）→ 直接改写头部 SRID；
    - 输入是普通 ISO WKB → 拼接标准 EWKB 头（类型位加 EWKB 标志 + SRID）。
    """
    if not ewkb or srid is None:
        return ewkb
    is_xdr = ewkb[0] != 1
    otype = int.from_bytes(ewkb[1:5], "little" if not is_xdr else "big")
    srid_bytes = int(srid).to_bytes(4, "little" if not is_xdr else "big")
    if otype & 0x20000000:  # 已是 EWKB：类型|EWKB标志位，第 5..9 字节为 SRID
        head = bytearray(ewkb[:9])
        head[5:9] = srid_bytes
        return bytes(head) + ewkb[9:]
    new_type = otype | 0x20000000
    return (
        bytes([ewkb[0]])
        + new_type.to_bytes(4, "little" if not is_xdr else "big")
        + srid_bytes
        + ewkb[5:]
    )