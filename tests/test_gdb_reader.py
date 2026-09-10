# -*- coding: utf-8 -*-
"""GDB 读取层自检（gdb_reader.py）。

覆盖：
- 纯函数（无需 GDB，总是执行）：set_ewkb_srid（WKB->EWKB / EWKB 改写 / XDR 字节序）、
  pg_geom_type（OGR 类型 -> PG typmod 类型）;
- 真实 GDB（有 GDB 才执行，否则跳过）：打开、图层枚举、按名取层、图层元数据、
  逐要素迭代（属性键/值类型、EWKB 合法性）、几何类型探测、读+SRID 注入直通。

前置：环境变量 G2P_TEST_GDB 指定 GDB 路径；缺省用 test_import_e2e 的默认路径。
运行：.conda/bin/python tests/test_gdb_reader.py
"""
import datetime as _dt
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from osgeo import gdal, ogr

from gdb2pg import gdb_reader

GDB = os.environ.get("G2P_TEST_GDB", "/Users/haigeek/dev/tys/gis/data/test1.gdb")
SAMPLE_LIMIT = 50       # 每层抽样要素上限
META_CHECK_LAYERS = 8   # 元数据检查的图层数上限
ITER_CHECK_LAYERS = 3   # 要素迭代检查的图层数上限


# ---------------------------------------------------------------- 纯函数

def test_set_ewkb_srid():
    """WKB -> EWKB 注入 SRID；已是 EWKB 则改写头部；XDR 大端字节序。"""
    # 普通 ISO WKB（NDR）：字节序(1) + 类型(4) + 16 字节几何体
    wkb = struct.pack("<BI", 1, ogr.wkbPoint) + b"\x00" * 16
    ewkb = gdb_reader.set_ewkb_srid(wkb, 4490)
    assert ewkb[0] == 1                              # NDR
    otype = struct.unpack("<I", ewkb[1:5])[0]
    assert otype & 0x20000000                        # EWKB 标志位
    assert struct.unpack("<I", ewkb[5:9])[0] == 4490  # SRID 落在 5..9
    assert ewkb[9:] == wkb[5:]                       # 几何体原样保留

    # 已是 EWKB：仅改写 SRID，几何体不动
    ewkb2 = gdb_reader.set_ewkb_srid(ewkb, 4326)
    assert struct.unpack("<I", ewkb2[5:9])[0] == 4326
    assert ewkb2[9:] == ewkb[9:]

    # XDR（大端）：字节序 0，类型与 SRID 均大端
    xdr = struct.pack(">BI", 0, ogr.wkbPoint) + b"\x00" * 16
    exdr = gdb_reader.set_ewkb_srid(xdr, 4490)
    assert exdr[0] == 0
    assert struct.unpack(">I", exdr[5:9])[0] == 4490

    # 边界：空输入原样返回；srid=None 不注入
    assert gdb_reader.set_ewkb_srid(b"", 4490) == b""
    assert gdb_reader.set_ewkb_srid(wkb, None) == wkb


def test_pg_geom_type():
    """OGR 几何类型码 -> PG typmod 类型名。"""
    assert gdb_reader.pg_geom_type(ogr.wkbPoint) == "POINT"
    assert gdb_reader.pg_geom_type(ogr.wkbMultiLineString) == "MULTILINESTRING"
    assert gdb_reader.pg_geom_type(ogr.wkbMultiPolygon) == "MULTIPOLYGON"
    assert gdb_reader.pg_geom_type(ogr.wkbMultiPolygon25D) == "MULTIPOLYGONZ"
    assert gdb_reader.pg_geom_type(ogr.wkbLineString25D) == "LINESTRINGZ"
    assert gdb_reader.pg_geom_type(ogr.wkbCurvePolygon) is None  # 曲线类 -> None（走泛型）
    assert gdb_reader.pg_geom_type(1347440720) is None           # 未知码 -> None


# ---------------------------------------------------------------- 真实 GDB

def test_open_and_layers(ds, layers):
    assert len(layers) > 0, "GDB 至少应有一个图层"
    for name in layers:
        lyr = ds.GetLayerByName(name)
        assert lyr is not None, f"无法按名取回图层: {name}"


def test_layer_meta(ds, layers):
    seen_geom = False
    for name in layers[:META_CHECK_LAYERS]:
        meta = gdb_reader.layer_meta(ds.GetLayerByName(name))
        assert set(meta) >= {"srs", "fields", "feature_count", "geom_ogrid", "geom_name"}
        assert meta["feature_count"] >= 0, f"{name}: 要素数非法"
        for f in meta["fields"]:
            assert f["name"], f"{name}: 存在空字段名"
            assert f["type_name"], f"{name}: 存在空类型描述"
        if meta["geom_ogrid"] not in (ogr.wkbNone, ogr.wkbUnknown):
            seen_geom = True
    print(f"        （抽查 {min(len(layers), META_CHECK_LAYERS)} 层；"
          f"几何类图层已探测: {seen_geom}）")
    # 不强断 seen_geom：允许全部为纯属性表的 GDB


def test_iter_features(ds, layers):
    for name in layers[:ITER_CHECK_LAYERS]:
        lyr = ds.GetLayerByName(name)
        meta = gdb_reader.layer_meta(lyr)
        ftypes = [f["type"] for f in meta["fields"]]
        fnames = [f["name"] for f in meta["fields"]]

        n = 0
        for attrs, ewkb, _fid in gdb_reader.iter_features(lyr, meta["srs"]):
            # 契约：属性键 == 字段名集合
            assert set(attrs) == set(fnames), f"{name}: 属性键与字段结构不一致"
            # EWKB 合法：None=空几何；否则 NDR/BE 字节序 + 至少 5 字节
            if ewkb is not None:
                assert ewkb[0] in (0, 1), f"{name}: 非法 WKB 字节序"
                assert len(ewkb) >= 5, f"{name}: WKB 过短"
            # 日期/时间字段值类型抽查（脏数据容许 None）
            for i, ft in enumerate(ftypes):
                if ft in (ogr.OFTDate, ogr.OFTDateTime, ogr.OFTTime):
                    v = attrs[fnames[i]]
                    assert v is None or isinstance(
                        v, (_dt.date, _dt.datetime, _dt.time)), \
                        f"{name}.{fnames[i]}: 日期字段值类型异常: {v!r}"
            n += 1
            if n >= SAMPLE_LIMIT:
                break
        print(f"        {name}: 抽样 {n} 条要素（源计数 {meta['feature_count']}）")


def test_observed_geometry_names(ds, layers):
    for name in layers[:ITER_CHECK_LAYERS]:
        lyr = ds.GetLayerByName(name)
        meta = gdb_reader.layer_meta(lyr)
        if meta["geom_ogrid"] in (ogr.wkbNone, ogr.wkbUnknown):
            continue
        names = gdb_reader.observed_geometry_names(lyr, limit=100)
        assert isinstance(names, set)
        for gname in names:
            assert gname == gname.lower(), f"{name}: 几何类型名应为小写: {gname}"
        print(f"        {name}: 实测几何类型 {sorted(names) or '(空/无几何)'}")


def test_read_srid_chain(ds, layers):
    """读取 + SRID 注入直通（模拟 importer 的 _features_with_srid）。"""
    for name in layers[:ITER_CHECK_LAYERS]:
        lyr = ds.GetLayerByName(name)
        meta = gdb_reader.layer_meta(lyr)
        if meta["geom_ogrid"] in (ogr.wkbNone, ogr.wkbUnknown):
            continue
        injected = False
        for _, ewkb, _fid in gdb_reader.iter_features(lyr, meta["srs"]):
            if ewkb is None:
                continue
            out = gdb_reader.set_ewkb_srid(ewkb, 4490)
            assert struct.unpack("<I", out[5:9])[0] == 4490
            injected = True
            break
        assert injected, f"{name}: 声明为几何图层但未读到任何非空几何"
        print(f"        {name}: SRID 注入直通 OK")


def test_layers_summary(ds, layers):
    """图层摘要（Web 新建任务「图层选择」自动填充用）。"""
    summary = gdb_reader.gdb_layers_summary(ds.GetDescription())
    got = [s["source"] for s in summary]
    assert sorted(got) == sorted(layers), f"{got} != {layers}"
    for s in summary:
        assert isinstance(s["feature_count"], int)
        assert s["geometry"] is None or isinstance(s["geometry"], str)
        assert s["srid"] is None or isinstance(s["srid"], int)
    print(f"        图层摘要 {len(summary)} 个 OK")


# ---------------------------------------------------------------- 主流程

def build_sample_gdb() -> tuple:
    """GDAL 3.10+ 的 OpenFileGDB 驱动支持创建：生成小样例 GDB。

    返回 (path, cleanup)，cleanup 在测试结束后删除临时目录。
    仅用于实现 `--sample` 自检模式，真实数据请用 G2P_TEST_GDB。
    """
    drv = ogr.GetDriverByName("OpenFileGDB")
    if drv is None or drv.GetMetadataItem("DCAP_CREATE") != "YES":
        raise SystemExit("[跳过] OpenFileGDB 不支持创建，且未提供 G2P_TEST_GDB")
    import shutil
    import tempfile
    base = tempfile.mkdtemp(prefix="g2p_sample_")
    path = os.path.join(base, "sample.gdb")
    ds = drv.CreateDataSource(path)

    lyr = ds.CreateLayer("roads", geom_type=ogr.wkbLineString)
    lyr.CreateField(ogr.FieldDefn("name", ogr.OFTString))
    lyr.CreateField(ogr.FieldDefn("count", ogr.OFTInteger))
    lyr.CreateField(ogr.FieldDefn("created", ogr.OFTDateTime))
    for i in range(20):
        f = ogr.Feature(lyr.GetLayerDefn())
        f["name"] = f"道路-{i}"
        f["count"] = i * 10
        f["created"] = f"2024-01-0{i % 9 + 1}T08:30:00"
        g = ogr.Geometry(ogr.wkbLineString)
        g.AddPoint(116.0 + i, 39.0)
        g.AddPoint(116.5 + i, 39.5)
        f.SetGeometry(g)
        lyr.CreateFeature(f)

    tab = ds.CreateLayer("attribs", geom_type=ogr.wkbNone)
    tab.CreateField(ogr.FieldDefn("code", ogr.OFTString))
    f = ogr.Feature(tab.GetLayerDefn())
    f["code"] = "A-1"
    tab.CreateFeature(f)
    ds = None
    return path, (lambda: shutil.rmtree(base, ignore_errors=True))


TESTS = [
    ("EWKB SRID 注入（纯函数）", lambda: test_set_ewkb_srid()),
    ("PG 几何类型映射（纯函数）", lambda: test_pg_geom_type()),
]


def main() -> int:
    global GDB
    cleanup = None
    gdal.SetConfigOption("GDAL_FILENAME_IS_UTF8", "YES")

    if os.path.isdir(GDB):
        print(f"GDB 路径: {GDB}  （存在: True）")
    elif "--sample" in sys.argv:
        GDB, cleanup = build_sample_gdb()
        print(f"[模式] 未提供 G2P_TEST_GDB，已自动生成样例 GDB: {GDB}")
    else:
        print(f"GDB 路径: {GDB}  （存在: False）")
        print("[提示] 未找到 GDB，真实数据用例将跳过；"
              "可用环境变量 G2P_TEST_GDB 指定路径，或用 --sample 自建样例")

    runnable = list(TESTS)
    has_gdb = os.path.isdir(GDB)
    if has_gdb:
        ds = gdb_reader.open_gdb(GDB)
        layers = gdb_reader.list_layers(ds)
        shown = ", ".join(layers[:8]) + ("…" if len(layers) > 8 else "")
        print(f"图层数量: {len(layers)}  [{shown}]")
        runnable += [
            ("打开与按名取层", lambda: test_open_and_layers(ds, layers)),
            ("图层元数据", lambda: test_layer_meta(ds, layers)),
            ("逐要素迭代（属性/EWKB/日期类型）", lambda: test_iter_features(ds, layers)),
            ("几何类型探测", lambda: test_observed_geometry_names(ds, layers)),
            ("读取 + SRID 注入直通", lambda: test_read_srid_chain(ds, layers)),
            ("图层摘要（Web 图层选择）", lambda: test_layers_summary(ds, layers)),
        ]

    failed = 0
    for name, fn in runnable:
        try:
            fn()
            print(f"[PASS] {name}")
        except AssertionError as e:
            failed += 1
            print(f"[FAIL] {name}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"[FAIL] {name}: ({type(e).__name__}) {e}")

    if cleanup:
        cleanup()
    print("=" * 60)
    if failed:
        print(f"失败 {failed}/{len(runnable)} 项")
        return 1
    print(f"[OK] 读取层校验全部通过（{len(runnable)} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())