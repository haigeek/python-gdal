# -*- coding: utf-8 -*-
"""业务库（任务元数据）存取层自检：CRUD / 状态流转 / 日志 / 级联删除 / 孤儿恢复。

前置：本地验证库（scripts/dev_db.sh，127.0.0.1:5433）已就绪，
或设置 G2P_TEST_PGURL 指向可写业务库（纯 Postgres 即可，无需 PostGIS）。
运行：.conda/bin/python tests/test_task_store.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gdb2pg.tasks.store import (STATUS_CANCELLED, STATUS_DRAFT, STATUS_FAILED,
                                STATUS_QUEUED, STATUS_RUNNING, STATUS_SUCCEEDED,
                                TaskStore)
from gdb2pg.web.config import ManagementConfig
from gdb2pg.web.db import ensure_meta_schema

SCHEMA = "g2p_test"  # 独立测试 schema，避免污染开发库


def mgmt_db() -> ManagementConfig:
    url = os.environ.get("G2P_TEST_PGURL")
    if url:
        import urllib.parse
        p = urllib.parse.urlparse(url)
        return ManagementConfig(host=p.hostname or "127.0.0.1", port=p.port or 5432,
                                dbname=p.path.lstrip("/") or "postgres",
                                user=p.username or "postgres", password=p.password)
    return ManagementConfig(host="127.0.0.1", port=5433, dbname="g2p_meta",
                          user="postgres")


def main() -> int:
    db = mgmt_db()
    ensure_meta_schema(db, SCHEMA)
    store = TaskStore(db, schema=SCHEMA)

    # -- CRUD --
    tid = store.create("gdb_import", "测试任务", {"gdb": "/tmp/x.gdb", "database": {"host": "h"}})
    rec = store.get(tid)
    assert rec and rec["status"] == STATUS_DRAFT, rec
    assert rec["config"]["gdb"] == "/tmp/x.gdb"
    assert rec["name"] == "测试任务"

    items, total = store.list(q="测试", page=1, page_size=10)
    assert total >= 1 and any(i["id"] == tid for i in items)
    items, total = store.list(status=STATUS_DRAFT)
    assert any(i["id"] == tid for i in items)

    store.update(tid, name="改名", config={"gdb": "/tmp/y.gdb"})
    rec = store.get(tid)
    assert rec["name"] == "改名" and rec["config"]["gdb"] == "/tmp/y.gdb"

    # -- 状态流转 --
    store.update_state(tid, status=STATUS_QUEUED)
    store.update_state(tid, status=STATUS_RUNNING, progress={"layer_index": 1, "rows": 10})
    store.update_state(tid, status=STATUS_SUCCEEDED, result={"ok": [{"source": "a", "rows": 1}]},
                       finished_at=True)
    rec = store.get(tid)
    assert rec["status"] == STATUS_SUCCEEDED
    assert rec["progress"]["rows"] == 10
    assert rec["result"]["ok"][0]["source"] == "a"
    assert rec["finished_at"] is not None

    # -- 失败 --
    tid2 = store.create("gdb_import", "失败任务", {})
    store.update_state(tid2, status=STATUS_FAILED, error="boom", finished_at=True)
    rec = store.get(tid2)
    assert rec["status"] == STATUS_FAILED and rec["error"] == "boom"

    # -- 取消 --  -- 日志 --
    for m in ("line1", "line2", "line3"):
        store.append_log(tid, m)
    logs = store.logs(tid)
    assert [l["message"] for l in logs] == ["line1", "line2", "line3"]
    inc = store.logs(tid, after_id=logs[0]["id"])
    assert [l["message"] for l in inc] == ["line2", "line3"]

    # -- 孤立任务恢复（模拟服务重启） --
    store.update_state(tid2, status=STATUS_RUNNING)
    orphaned = store.orphan_recover("服务重启中断")
    assert orphaned >= 1
    rec = store.get(tid2)
    assert rec["status"] == STATUS_FAILED and "重启" in (rec["error"] or "")

    # -- 级联删除：删任务后日志随之删除 --
    store.delete(tid)
    assert store.get(tid) is None
    assert store.logs(tid) == []
    store.delete(tid2)

    print("[OK] 业务库存取层校验通过：CRUD/状态/日志/级联/孤儿恢复")
    return 0


if __name__ == "__main__":
    sys.exit(main())