# -*- coding: utf-8 -*-
"""SHP -> PostGIS 导入端到端自检。

默认连接工程自带的 5433 验证库；也可通过 G2P_TEST_PGURL 指定目标库。
没有可用数据库时，脚本模式打印跳过，避免影响只安装 GDAL 的读取层校验。
"""

from __future__ import annotations

import os
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from osgeo import ogr, osr

from gdb2pg.importer import run
from gdb2pg.model import DatabaseConfig, ImportConfig


def _target_db() -> DatabaseConfig:
    url = os.environ.get("G2P_TEST_PGURL")
    if url:
        import urllib.parse
        parsed = urllib.parse.urlparse(url)
        return DatabaseConfig(
            host=parsed.hostname or "127.0.0.1",
            port=parsed.port or 5432,
            dbname=parsed.path.lstrip("/") or "postgres",
            user=parsed.username or "postgres",
            password=parsed.password,
        )
    return DatabaseConfig(
        host="127.0.0.1", port=5433, dbname="gdb2pg_test", user="postgres",
    )


def _create_shp(path: Path, name: str, *, with_fields: bool) -> Path:
    driver = ogr.GetDriverByName("ESRI Shapefile")
    assert driver is not None
    shp = path / f"{name}.shp"
    ds = driver.CreateDataSource(str(shp))
    assert ds is not None
    srs = osr.SpatialReference()
    assert srs.ImportFromEPSG(4326) == 0
    layer = ds.CreateLayer(name, srs=srs, geom_type=ogr.wkbPoint)
    if with_fields:
        layer.CreateField(ogr.FieldDefn("Code", ogr.OFTString))
        layer.CreateField(ogr.FieldDefn("name", ogr.OFTString))
        layer.CreateField(ogr.FieldDefn("created", ogr.OFTDate))
    for i in range(3):
        feature = ogr.Feature(layer.GetLayerDefn())
        if with_fields:
            feature.SetField("Code", f"R-{i}")
            feature.SetField("name", f"road-{i}")
            feature.SetField("created", f"2024-01-{i + 1:02d}")
        geom = ogr.Geometry(ogr.wkbPoint)
        geom.AddPoint(116 + i, 39 + i)
        feature.SetGeometry(geom)
        assert layer.CreateFeature(feature) == 0
    ds = None
    return shp


def _can_connect(db: DatabaseConfig) -> bool:
    try:
        import psycopg
        with psycopg.connect(db.dsn(), connect_timeout=3):
            return True
    except Exception as exc:  # noqa: BLE001 端到端自检允许无库时跳过
        print(f"[跳过] 目标 PostgreSQL 不可用: {exc}")
        return False


def test_shp_import_to_postgis():
    db = _target_db()
    if not _can_connect(db):
        return

    suffix = uuid.uuid4().hex[:10]
    roads_table = f"shp_e2e_roads_{suffix}"
    empty_table = f"shp_e2e_empty_{suffix}"
    with tempfile.TemporaryDirectory(prefix="g2p_shp_e2e_") as tmp:
        root = Path(tmp)
        roads = _create_shp(root, "roads", with_fields=True)
        empty_attrs = _create_shp(root, "empty_attrs", with_fields=False)
        try:
            cfg = ImportConfig.from_dict({
                "shp": str(roads),
                "database": {**db.__dict__, "schema": "public"},
                "default": {
                    "mode": "create",
                    "create_spatial_index": True,
                    "launder_tables": False,
                    "pk_field": "Code",
                },
                "layers": [{"source": "roads", "table": roads_table}],
            })
            stats = run(cfg, log=lambda message: print(f"LOG {message}"))
            assert stats["ok"] == [("roads", 3)]

            import psycopg
            with psycopg.connect(db.dsn(), autocommit=True) as conn:
                assert conn.execute(
                    f'SELECT count(*) FROM public."{roads_table}"',
                ).fetchone()[0] == 3
                assert conn.execute(
                    f'SELECT count(*) FROM public."{roads_table}" '
                    "WHERE ST_SRID(shape) = 4326 AND name = 'road-1'",
                ).fetchone()[0] == 1
                assert conn.execute(
                    f'SELECT "Code" FROM public."{roads_table}" ORDER BY "Code"',
                ).fetchall() == [("R-0",), ("R-1",), ("R-2",)]
                assert conn.execute(
                    "SELECT count(*) FROM information_schema.table_constraints "
                    "WHERE table_schema = 'public' AND table_name = %s "
                    "AND constraint_type = 'PRIMARY KEY'",
                    (roads_table,),
                ).fetchone()[0] == 1
                assert conn.execute(
                    "SELECT count(*) FROM pg_indexes "
                    "WHERE schemaname = 'public' AND tablename = %s "
                    "AND indexname = %s",
                    (roads_table, roads_table + "_geom_idx"),
                ).fetchone()[0] == 1

            # 边界场景：关闭几何导入且 DBF 无属性字段，验证
            # create_table/COPY 不会生成空列列表 SQL。
            empty_cfg = ImportConfig.from_dict({
                "shp": str(empty_attrs),
                "database": {**db.__dict__, "schema": "public"},
                "default": {
                    "mode": "create",
                    "geometries": False,
                    "launder_tables": False,
                },
                "layers": [{"source": "empty_attrs", "table": empty_table}],
            })
            empty_stats = run(empty_cfg, log=lambda message: print(f"LOG {message}"))
            assert empty_stats["ok"] == [("empty_attrs", 3)]
            with psycopg.connect(db.dsn(), autocommit=True) as conn:
                assert conn.execute(
                    f'SELECT count(*) FROM public."{empty_table}"',
                ).fetchone()[0] == 3
                assert conn.execute(
                    f'SELECT fid_2 FROM public."{empty_table}" ORDER BY fid_2',
                ).fetchall() == [(1,), (2,), (3,)]
        finally:
            # 仅清理本测试随机生成的表名，不触碰任何既有表。
            import psycopg
            with psycopg.connect(db.dsn(), autocommit=True) as conn:
                conn.execute(f'DROP TABLE IF EXISTS public."{roads_table}"')
                conn.execute(f'DROP TABLE IF EXISTS public."{empty_table}"')


def main() -> int:
    test_shp_import_to_postgis()
    print("SHP -> PostGIS 端到端自检完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
