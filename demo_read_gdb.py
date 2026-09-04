#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
demo_read_gdb.py —— 用 Python 版 GDAL/OGR 读取 File Geodatabase (.gdb) 的示例

功能：
  1. 打印 GDAL 版本及 OpenFileGDB / FileGDB 驱动是否可用
  2. 打开 .gdb，列出全部图层（要素类/表）
  3. 逐层打印：要素数量、几何类型、范围(extent)、字段结构
  4. 逐层抽样打印前 N 条要素的属性与 WKT 几何

用法：
  .venv/bin/python demo_read_gdb.py [gdb路径] [--limit 5]

示例：
  .venv/bin/python demo_read_gdb.py test1.gdb
  .venv/bin/python demo_read_gdb.py data/test1.gdb --layers "地块,道路" --limit 10
"""

import argparse
import os
import sys

from osgeo import gdal, ogr

# GDAL 4.0 起默认抛异常；显式启用，便于定位错误
ogr.UseExceptions()

# 让 GDAL 能处理中文路径（macOS 下多为 UTF-8，此项为保险）
gdal.SetConfigOption("GDAL_FILENAME_IS_UTF8", "YES")


def layer_extent(layer):
    """返回图层范围；GDB 缓存的范围可能损坏（现实中常见），
    此时退化为逐要素计算真实范围。"""
    ext = list(layer.GetExtent())
    if not (ext[0] < ext[2] and ext[1] < ext[3]):  # 缓存范围不合法
        print("  - 提示: GDB 缓存的范围无效，改为逐要素计算")
        mnx = mny = mxx = mxy = None
        layer.ResetReading()
        for feat in layer:
            geom = feat.GetGeometryRef()
            if geom is None or geom.IsEmpty():
                continue
            e = geom.GetEnvelope()
            mnx = e[0] if mnx is None else min(mnx, e[0])
            mny = e[1] if mny is None else min(mny, e[1])
            mxx = e[2] if mxx is None else max(mxx, e[2])
            mxy = e[3] if mxy is None else max(mxy, e[3])
        ext = [mnx, mny, mxx, mxy]
    if not (ext[0] < ext[2] and ext[1] < ext[3]):
        # 极端情形：部分 ArcGIS 导出的 MultiPolygon 其包络本身是反的，
        # 逐要素计算后仍无法得到合法范围
        return None
    return ext


def print_driver_info():
    """打印 GDAL 版本与 GDB 相关驱动。"""
    print("=" * 72)
    print("GDAL 版本   :", gdal.VersionInfo())
    print("GDAL 说明   :", gdal.VersionInfo("RELEASE_NAME"))
    for drv_name in ("OpenFileGDB", "FileGDB"):
        drv = ogr.GetDriverByName(drv_name)
        if drv:
            print(f"驱动 {drv_name:<12}: 可用  (读写能力: {drv.GetMetadataItem('DCAP_OPEN')})")
        else:
            print(f"驱动 {drv_name:<12}: 不可用")
    print("=" * 72)


def fmt_geom_type(gt):
    """把几何类型枚举值转换为可读名称。"""
    name = ogr.GeometryTypeToName(gt)
    return f"{name} (type={gt})"


def describe_layer(layer, limit):
    """描述单个图层：数量、几何、范围、字段，并抽样打印要素。"""
    lyr_name = layer.GetName()
    print(f"\n【图层】{lyr_name}")
    print(f"  - 几何类型 : {fmt_geom_type(layer.GetGeomType())}")
    print(f"  - 要素数量 : {layer.GetFeatureCount()}")

    extent = layer_extent(layer)
    if extent:
        print(f"  - 范围     : minX={extent[0]:.6f} minY={extent[1]:.6f} "
              f"maxX={extent[2]:.6f} maxY={extent[3]:.6f}")
    else:
        print("  - 范围     : （数据异常，无法计算有效范围）")

    # 字段结构
    lyr_defn = layer.GetLayerDefn()
    print(f"  - 字段({lyr_defn.GetFieldCount()}):")
    for i in range(lyr_defn.GetFieldCount()):
        fld = lyr_defn.GetFieldDefn(i)
        print(f"      {fld.GetName():<32} {fld.GetFieldTypeName(fld.GetType())} "
              f"宽={fld.GetWidth()} 精度={fld.GetPrecision()}")

    # 抽样打印要素
    print(f"  - 要素抽样(最多 {limit} 条):")
    count = 0
    layer.ResetReading()
    for feat in layer:
        if count >= limit:
            break
        attrs = [feat.GetField(i) for i in range(lyr_defn.GetFieldCount())]
        field_names = [lyr_defn.GetFieldDefn(i).GetName() for i in range(lyr_defn.GetFieldCount())]
        attr_str = ", ".join(f"{n}={v!r}" for n, v in zip(field_names, attrs))
        geom = feat.GetGeometryRef()
        wkt = geom.ExportToWkt() if geom else "NULL"
        print(f"    [{count + 1}] 属性: {{{attr_str}}}")
        print(f"         几何: {wkt[:200]}{'...' if len(wkt) > 200 else ''}")
        count += 1
    if count == 0:
        print("      （无要素/空图层）")


def main():
    parser = argparse.ArgumentParser(description="用 Python GDAL/OGR 读取 File Geodatabase")
    parser.add_argument("gdb", nargs="?", default="data/test1.gdb",
                        help="File Geodatabase (.gdb) 路径")
    parser.add_argument("--layers", default=None,
                        help="只读取指定图层名(逗号分隔)；默认全部")
    parser.add_argument("--limit", type=int, default=5,
                        help="每图层抽样打印的要素条数，默认 5")
    parser.add_argument("--list-only", action="store_true",
                        help="只列出图层名，不做详情")
    args = parser.parse_args()

    gdb_path = args.gdb
    if not os.path.isdir(gdb_path):
        print(f"[错误] 找不到 .gdb：{gdb_path}")
        print("用法示例: python demo_read_gdb.py /path/to/xxx.gdb --limit 10")
        sys.exit(1)

    print_driver_info()
    print(f"\n打开: {gdb_path}")

    ds = ogr.Open(gdb_path, 0)  # 0 = 只读；OpenFileGDB 驱动仅支持只读
    if ds is None:
        print("[错误] 打开失败（可能是空库、损坏，或超出 OpenFileGDB 支持范围）")
        sys.exit(1)

    n_layers = ds.GetLayerCount()
    print(f"图层数量: {n_layers}")

    # 首次扫描：列图层名（GDB 内图层名可能带路径，如 'fld/xxx'）
    all_names = [ds.GetLayerByIndex(i).GetName() for i in range(n_layers)]
    for i, name in enumerate(all_names):
        print(f"  [{i}] {name}")

    if args.list_only:
        return

    if args.layers:
        targets = [s.strip() for s in args.layers.split(",")]
        missing = [t for t in targets if t not in all_names]
        if missing:
            print(f"[警告] 以下图层名未找到: {missing}")
        indices = [i for i, n in enumerate(all_names) if n in targets]
    else:
        indices = list(range(n_layers))

    for idx in indices:
        layer = ds.GetLayer(idx)
        describe_layer(layer, args.limit)

    ds = None
    print("\n完成。")


if __name__ == "__main__":
    main()