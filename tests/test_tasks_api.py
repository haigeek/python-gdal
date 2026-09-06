# -*- coding: utf-8 -*-
"""Web API 自检：任务 CRUD / 运行 / 取消 / 预览 / 密码脱敏哨兵 / 状态冲突。

用 TestClient 走真实 FastAPI 应用；注册一个假任务类型驱动全流程，
gdb_import 只做纯配置校验（不打开真实 GDB）。
前置：本地业务库（dev_db.sh，5433）或 G2P_TEST_PGURL。
运行：.conda/bin/python tests/test_tasks_api.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from gdb2pg.tasks.base import TaskContext, TaskType, register
from gdb2pg.tasks.store import STATUS_RUNNING
from gdb2pg.web.app import create_app
from gdb2pg.web.config import ManagementConfig, WebConfig

SCHEMA = "g2p_api_test"


@register
class FakeBlink(TaskType):
    """同步立即完成的假类型：跑通 创建->运行->成功 链路。"""
    type = "fake_blink"
    label = "假任务（测试）"
    form_schema = {"groups": [
        {"key": "param", "label": "参数", "fields": [
            {"key": "value", "label": "值", "type": "text", "default": "x"}]},
    ]}

    def validate(self, config):
        if not (config or {}).get("param", {}).get("value"):
            return ["param.value 不能为空"]
        return []

    def run(self, ctx: TaskContext, config: dict):
        ctx.log(f"执行中 value={config['param']['value']}")
        ctx.set_progress({"layer_index": 1, "layer_total": 1, "rows": 3})
        return {"ok": [{"source": "fake", "rows": 3}]}

    def sanitize(self, config):
        return config


def mgmt_db() -> ManagementConfig:
    url = os.environ.get("G2P_TEST_PGURL")
    if url:
        import urllib.parse
        p = urllib.parse.urlparse(url)
        return ManagementConfig(host=p.hostname or "127.0.0.1", port=p.port or 5432,
                                dbname=p.path.lstrip("/") or "postgres",
                                user=p.username or "postgres", password=p.password)
    return ManagementConfig(host="127.0.0.1", port=5433, dbname="g2p_meta", user="postgres")


def main() -> int:
    cfg = WebConfig.load()
    cfg.management = mgmt_db()
    cfg.management.schema = SCHEMA
    app = create_app(config=cfg)

    with TestClient(app) as c:
        # -- 任务类型清单 --
        r = c.get("/api/task-types")
        assert r.status_code == 200, r.text
        types = r.json()["data"]
        names = {t["type"] for t in types}
        assert "gdb_import" in names and "fake_blink" in names
        fake_schema = next(t for t in types if t["type"] == "fake_blink")
        assert fake_schema["form_schema"]["groups"]

        # -- 创建 --
        r = c.post("/api/tasks", json={"type": "fake_blink", "name": "链路任务",
                                       "config": {"param": {"value": "abc"}}})
        assert r.status_code == 201, r.text
        tid = r.json()["data"]["id"]

        # -- gdb_import 校验错误 --
        r = c.post("/api/tasks", json={"type": "gdb_import", "name": "坏",
                                       "config": {"gdb": "/no/such/path"}})
        assert r.status_code == 400 and "GDB" in r.json()["error"], r.text

        # -- 未知类型 --
        r = c.post("/api/tasks", json={"type": "nope", "name": "x", "config": {}})
        assert r.status_code == 404

        # -- 列表 / 详情 --
        r = c.get(f"/api/tasks/{tid}")
        assert r.status_code == 200 and r.json()["data"]["status"] == "draft"

        # -- 运行 -> 轮询至成功 --
        r = c.post(f"/api/tasks/{tid}/run")
        assert r.status_code == 200, r.text
        for _ in range(50):
            st = c.get(f"/api/tasks/{tid}").json()["data"]["status"]
            if st in ("succeeded", "failed", "cancelled"):
                break
            time.sleep(0.1)
        assert st == "succeeded", f"状态异常: {st}"
        logs = c.get(f"/api/tasks/{tid}/logs").json()["data"]["logs"]
        assert any("执行中" in l["message"] for l in logs)
        detail = c.get(f"/api/tasks/{tid}").json()["data"]
        assert detail["result"]["ok"][0]["rows"] == 3

        # -- 密码脱敏 + 更新哨兵保留 --
        r = c.post("/api/tasks", json={"type": "gdb_import", "name": "带密",
                                       "config": {"gdb": "/tmp/x.gdb",
                                                  "database": {"host": "h", "port": 5432,
                                                               "dbname": "d", "user": "u",
                                                               "password": "secret"}}})
        assert r.status_code == 201, r.text
        tid2 = r.json()["data"]["id"]
        got = c.get(f"/api/tasks/{tid2}").json()["data"]
        assert got["config"]["database"]["password"] == "***"
        # 用 *** 哨兵更新：应保留旧密码
        r = c.put(f"/api/tasks/{tid2}", json={"config": {
            "gdb": "/tmp/y.gdb",
            "database": {"host": "h", "port": 5432, "dbname": "d", "user": "u",
                         "password": "***"}}})
        assert r.status_code == 200, r.text
        got = c.get(f"/api/tasks/{tid2}").json()["data"]
        assert got["config"]["database"]["password"] == "***"
        # 对照：直接查库确认密码仍是 secret
        import psycopg
        with psycopg.connect(mgmt_db().dsn(), autocommit=True) as conn:
            cur = conn.execute(f"SELECT config->'database'->>'password' FROM {SCHEMA}.task WHERE id={tid2}")
            assert cur.fetchone()[0] == "secret"

        # -- 状态冲突：running 中不可编辑/删除/二次运行 --
        c.post(f"/api/tasks/{tid}/run")
        store = app.state.store
        store.update_state(tid, status=STATUS_RUNNING)
        r = c.put(f"/api/tasks/{tid}", json={"name": "hack"})
        assert r.status_code == 409, r.text
        r = c.delete(f"/api/tasks/{tid}")
        assert r.status_code == 409, r.text
        r = c.post(f"/api/tasks/{tid}/run")
        assert r.status_code == 409, r.text
        store.update_state(tid, status="failed")
        r = c.put(f"/api/tasks/{tid}", json={"name": "ok"})
        assert r.status_code == 200, r.text

        # -- 非法 status 过滤 --
        r = c.get("/api/tasks?status=ghost")
        assert r.status_code == 400

        # -- 预览错误容忍（GDB 不存在 -> error 字段，而非 500） --
        r = c.post("/api/tasks/preview", json={"type": "gdb_import",
                                               "config": {"gdb": "/no/such",
                                                          "database": {"host": "h"}}})
        assert r.status_code == 200, r.text
        assert r.json()["data"]["error"] is not None

        # -- 目录浏览（默认根自动创建） --
        r = c.get("/api/gdb/browse")
        assert r.status_code == 200, r.text
        assert r.json()["data"]["path"] in app.state.web_config.allowed_dirs_abs()
        # 越界的绝对路径 -> 403
        r = c.get("/api/gdb/browse", params={"path": "/etc"})
        assert r.status_code == 403, r.text

        # -- 目标数据库检测 --
        d = {"host": "127.0.0.1", "port": 5433, "dbname": "g2p_meta",
             "user": "postgres", "schema": "g2p"}
        r = c.post("/api/database/test", json={"database": d})
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["connected"] is True and data["server_version"]
        assert data["schema"] == "g2p" and data["schema_exists"] is True

        # 连不上 -> 400（ok:false + 连接失败）
        r = c.post("/api/database/test",
                   json={"database": {"host": "127.0.0.1", "port": 1,
                                      "dbname": "x", "user": "u"}})
        assert r.status_code == 400 and r.json()["ok"] is False
        assert "连接失败" in r.json()["error"]

        # 密码脱敏但未传 task_id -> 400 提示
        r = c.post("/api/database/test",
                   json={"database": {**d, "password": "***"}})
        assert r.status_code == 400 and "脱敏" in r.json()["error"]

        # 脱敏 + task_id：从任务取已保存密码参与检测（trust 认证下任意密码可连）
        tid3 = store.create("fake_blink", "密码检测任务", {"database": {
            "host": "127.0.0.1", "port": 5433, "dbname": "g2p_meta",
            "user": "postgres", "password": "whatever", "schema": "g2p"}})
        r = c.post("/api/database/test",
                   json={"database": {**d, "password": "***"}, "task_id": tid3})
        assert r.status_code in (200, 400), r.text
        if r.status_code == 400:
            assert "脱敏" not in r.json()["error"]  # 已用旧密码而非直接报脱敏
        r = c.delete(f"/api/tasks/{tid3}")
        assert r.status_code == 204, r.text

        # -- 数据源管理 --
        r = c.post("/api/datasources", json={
            "name": "测试源A",
            "config": {"host": "127.0.0.1", "port": 5433, "dbname": "g2p_meta",
                       "user": "postgres", "password": "secret", "schema": "public"}})
        assert r.status_code == 201, r.text
        ds = r.json()["data"]
        assert ds["config"]["password"] == "***"
        ds_id = ds["id"]

        # 重名 -> 400
        r = c.post("/api/datasources", json={"name": "测试源A", "config": {}})
        assert r.status_code == 400 and "已存在" in r.json()["error"], r.text

        # 列表脱敏
        r = c.get("/api/datasources")
        assert r.status_code == 200
        assert any(x["id"] == ds_id and x["config"]["password"] == "***"
                   for x in r.json()["data"])

        # 编辑：密码 "***" 哨兵保留旧值
        r = c.put(f"/api/datasources/{ds_id}", json={"config": {
            "host": "127.0.0.1", "port": 5433, "dbname": "g2p_meta",
            "user": "postgres", "password": "***", "schema": "public"}})
        assert r.status_code == 200, r.text
        assert r.json()["data"]["config"]["password"] == "***"
        import psycopg
        with psycopg.connect(mgmt_db().dsn(), autocommit=True) as conn:
            cur = conn.execute(
                f"SELECT config->>'password' FROM {SCHEMA}.datasource WHERE id={ds_id}")
            assert cur.fetchone()[0] == "secret"

        # 任务引用数据源：password "***" + datasource_id -> 保存时解析真实密码并移除引用
        r = c.post("/api/tasks", json={"type": "fake_blink", "name": "引用数据源任务",
                                       "config": {"param": {"value": "x"},
                                                  "database": {"datasource_id": ds_id,
                                                               "password": "***"}}})
        assert r.status_code == 201, r.text
        tid4 = r.json()["data"]["id"]
        with psycopg.connect(mgmt_db().dsn(), autocommit=True) as conn:
            row = conn.execute(
                f"SELECT config FROM {SCHEMA}.task WHERE id={tid4}").fetchone()
        cfg4 = row[0]
        assert cfg4["database"]["password"] == "secret"
        assert "datasource_id" not in cfg4["database"]
        r = c.delete(f"/api/tasks/{tid4}")
        assert r.status_code == 204

        # 引用不存在的数据源 -> 400
        r = c.post("/api/tasks", json={"type": "fake_blink", "name": "坏引用",
                                       "config": {"param": {"value": "x"},
                                                  "database": {"datasource_id": 99999}}})
        assert r.status_code == 400 and "数据源不存在" in r.json()["error"], r.text

        # 删除数据源
        r = c.delete(f"/api/datasources/{ds_id}")
        assert r.status_code == 204, r.text
        r = c.get(f"/api/datasources/{ds_id}")
        assert r.status_code == 404

        # -- zip 上传 MD5 去重 --
        from test_gdb_reader import build_sample_gdb
        import io, zipfile, os, shutil  # noqa: E402
        gdb_dir, cleanup = build_sample_gdb()
        try:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                for root, _, files in os.walk(gdb_dir):
                    for fn in files:
                        full = os.path.join(root, fn)
                        zf.write(full, os.path.relpath(full, os.path.dirname(gdb_dir)))
            data = buf.getvalue()
            r1 = c.post("/api/uploads", files={"file": ("sample.gdb.zip", data, "application/zip")})
            assert r1.status_code == 200, r1.text
            d1 = r1.json()["data"]
            assert d1["cached"] is False and os.path.isdir(d1["gdb_path"])
            r2 = c.post("/api/uploads", files={"file": ("sample.gdb.zip", data, "application/zip")})
            assert r2.status_code == 200, r2.text
            d2 = r2.json()["data"]
            assert d2["cached"] is True, r2.text
            assert d2["gdb_path"] == d1["gdb_path"], r2.text
            with psycopg.connect(mgmt_db().dsn(), autocommit=True) as conn:
                assert conn.execute(
                    f"SELECT count(*) FROM {SCHEMA}.upload").fetchone()[0] == 1
        finally:
            cleanup()
            # 清理测试产生的上传目录与记录（保持测试卫生）
            try:
                with psycopg.connect(mgmt_db().dsn(), autocommit=True) as conn:
                    rows = conn.execute(
                        f"SELECT gdb_path FROM {SCHEMA}.upload").fetchall()
                    conn.execute(f"DELETE FROM {SCHEMA}.upload")
            except Exception:  # noqa: BLE001
                rows = []
            for (gpath,) in rows:
                shutil.rmtree(os.path.dirname(gpath), ignore_errors=True)

        # -- 删除 --
        r = c.delete(f"/api/tasks/{tid2}")
        assert r.status_code == 204, r.text
        r = c.delete(f"/api/tasks/{tid}")
        assert r.status_code == 204, r.text
        r = c.get(f"/api/tasks/{tid}")
        assert r.status_code == 404

    print("[OK] Web API 链路校验通过：CRUD/运行/取消/脱敏哨兵/状态冲突/预览")
    return 0


if __name__ == "__main__":
    sys.exit(main())