# -*- coding: utf-8 -*-
"""端到端自检：跑真实 GDB 导入本地/远程 PostGIS 并校验。

前置：本地验证库（scripts/dev_db.sh）已就绪，或设置 GDB2PG_PGURL 指向可写库。
运行：.conda/bin/python tests/test_import_e2e.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gdb2pg.model import DatabaseConfig, Defaults, ImportConfig
from gdb2pg.importer import dry_run, run

GDB = "/Users/haigeek/dev/tys/gis/data/test1.gdb"
EXPECT = {"bzxxm_ln": 1021}


def build_cfg() -> ImportConfig:
    url = os.environ.get("GDB2PG_PGURL")
    if url:
        # postgresql://user:pass@host:port/dbname?sslmode=...
        import urllib.parse
        from dataclasses import replace
        p = urllib.parse.urlparse(url)
        db = DatabaseConfig(
            host=p.hostname or "127.0.0.1",
            port=p.port or 5432,
            dbname=p.path.lstrip("/") or "postgres",
            user=p.username or "postgres",
            password=p.password,
            schema="public",
        )
    else:
        db = DatabaseConfig(host="127.0.0.1", port=5433, dbname="gdb2pg_test",
                            user="postgres", schema="public")
    return ImportConfig(
        gdb=GDB,
        database=db,
        default=Defaults(srid=4490, mode="overwrite", create_spatial_index=True),
        layers=[{"source": "bzxxm_ln"}],
    )


def main() -> int:
    cfg = build_cfg()
    print("== dry-run ==")
    dry_run(cfg)
    print("\n== 正式导入 ==")
    stats = run(cfg)
    ok = [s for s, _ in stats["ok"]]
    assert ok == list(EXPECT), f"导入图层不符: {ok}"

    import psycopg
    with psycopg.connect(cfg.database.dsn(), autocommit=True) as conn:
        for tbl, n in EXPECT.items():
            with conn.cursor() as cur:
                cur.execute("SELECT count(*), Find_SRID(%s::text, %s::text, 'geom'::text) FROM %s" % (
                    "'public'", "'" + tbl + "'", f'public."{tbl}"'))
                cnt, srid = cur.fetchone()
                assert cnt == n, f"{tbl}: {cnt} != {n}"
                assert srid == 4490, f"{tbl}: srid {srid}"
                cur.execute(
                    "SELECT indexname FROM pg_indexes WHERE tablename=%s AND indexdef LIKE '%%USING gist%%'",
                    (tbl,))
                assert cur.fetchone(), f"{tbl}: 缺 GIST 索引"
                cur.execute(f'SELECT count(*) FROM public."{tbl}" WHERE xzqmc IS NOT NULL')
                assert cur.fetchone()[0] > 0, f"{tbl}: 中文属性缺失"
        print("\n[OK] 端到端校验通过：计数/SRID/GIST索引/中文属性 全部符合预期")
    return 0


if __name__ == "__main__":
    sys.exit(main())