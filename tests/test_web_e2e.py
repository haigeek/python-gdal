# -*- coding: utf-8 -*-
"""Web 端到端自检：真实 GDB 经 API 建任务 -> 运行 -> 轮询至成功 -> 校验目标库。

前置：
- 本地业务库（scripts/dev_db.sh，5433）或 G2P_TEST_PGURL；
- 目标库可写（GDB2PG_PGURL 或本地 5433/gdb2pg_test）+ 真实 GDB；
- GDB 路径：环境变量 G2P_TEST_GDB，缺省 /Users/haigeek/dev/tys/gis/data/test1.gdb。
运行：.conda/bin/python tests/test_web_e2e.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from gdb2pg.model import DatabaseConfig
from gdb2pg.web.app import create_app
from gdb2pg.web.config import ManagementConfig, WebConfig

SCHEMA = "g2p_e2e_test"
GDB = os.environ.get("G2P_TEST_GDB", "/Users/haigeek/dev/tys/gis/data/test1.gdb")
EXPECT = {"bzxxm_ln": 1021}


def mgmt_db() -> ManagementConfig:
    url = os.environ.get("G2P_TEST_PGURL")
    if url:
        import urllib.parse
        p = urllib.parse.urlparse(url)
        return ManagementConfig(host=p.hostname or "127.0.0.1", port=p.port or 5432,
                                dbname=p.path.lstrip("/") or "postgres",
                                user=p.username or "postgres", password=p.password)
    return ManagementConfig(host="127.0.0.1", port=5433, dbname="g2p_meta", user="postgres")


def target_db() -> DatabaseConfig:
    url = os.environ.get("GDB2PG_PGURL")
    if url:
        import urllib.parse
        p = urllib.parse.urlparse(url)
        return DatabaseConfig(host=p.hostname or "127.0.0.1", port=p.port or 5432,
                              dbname=p.path.lstrip("/") or "postgres",
                              user=p.username or "postgres", password=p.password)
    return DatabaseConfig(host="127.0.0.1", port=5433, dbname="gdb2pg_test", user="postgres")


def main() -> int:
    if not os.path.isdir(GDB):
        print(f"[跳过] 未找到测试 GDB: {GDB}（可用环境变量 G2P_TEST_GDB 指定）")
        return 0

    cfg = WebConfig.load()
    cfg.management = mgmt_db()
    cfg.management.schema = SCHEMA
    store = None
    with TestClient(create_app(config=cfg)) as c:
        store = c.app.state.store

        r = c.post("/api/tasks", json={
            "type": "gdb_import",
            "name": "e2e-导入",
            "config": {
                "gdb": GDB,
                "database": {
                    "host": target_db().host, "port": target_db().port,
                    "dbname": target_db().dbname, "user": target_db().user,
                    "password": target_db().password, "schema": "public",
                    "ssl": "prefer",
                },
                "default": {"srid": 4490, "mode": "overwrite",
                            "geometries": True, "create_spatial_index": True},
                "layers": [{"source": "bzxxm_ln"}],
            }})
        assert r.status_code == 201, f"创建失败: {r.text}"
        tid = r.json()["data"]["id"]

        # 预览（真实 GDB -> 分层计划）
        r = c.post("/api/tasks/preview", json={
            "type": "gdb_import",
            "config": {"gdb": GDB, "database": {"host": "127.0.0.1"}, "layers": []}})
        assert r.status_code == 200, r.text
        pv = r.json()["data"]
        assert pv["error"] is None and pv["layers"], f"预览异常: {pv}"

        # 预览：显式规则存在时只导规则命中的图层（删除行不因 selectors 兜底被加回）
        r = c.post("/api/tasks/preview", json={
            "type": "gdb_import",
            "config": {"gdb": GDB, "database": {"host": "127.0.0.1"},
                       "layers": [{"source": "bzxxm_ln"}]}})
        assert r.status_code == 200, r.text
        pv2 = r.json()["data"]
        assert pv2["error"] is None and pv2["layers"] == ["bzxxm_ln"], f"显式规则失效: {pv2}"

        # 运行 -> 轮询
        r = c.post(f"/api/tasks/{tid}/run")
        assert r.status_code == 200, r.text
        deadline = time.time() + 300
        st = "draft"
        detail = None
        while time.time() < deadline:
            detail = c.get(f"/api/tasks/{tid}").json()["data"]
            st = detail["status"]
            if st in ("succeeded", "failed", "cancelled"):
                break
            time.sleep(2)
        assert st == "succeeded", f"任务未成功: status={st} error={detail and detail['error']}"

        # 目标库校验
        import psycopg
        with psycopg.connect(target_db().dsn(), autocommit=True) as conn:
            for tbl, n in EXPECT.items():
                cnt = conn.execute(f'SELECT count(*) FROM public."{tbl}"').fetchone()[0]
                assert cnt == n, f"{tbl}: {cnt} != {n}"

        logs = c.get(f"/api/tasks/{tid}/logs").json()["data"]["logs"]
        assert any("成功" in l["message"] for l in logs)

        # 清理任务记录（目标库表保留）
        r = c.delete(f"/api/tasks/{tid}")
        assert r.status_code == 204
        print("[OK] Web 端到端校验通过：真实 GDB 建任务->预览->运行->成功->目标库断言")
        return 0


if __name__ == "__main__":
    sys.exit(main())