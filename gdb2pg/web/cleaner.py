# -*- coding: utf-8 -*-
"""uploads 目录定时清理。

策略：删除「超过 TTL 且未被任何任务引用」的上传（zip MD5 去重表 upload）；
被任务 config.gdb 引用的目录始终保留。TTL <= 0 时禁用。
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
from datetime import datetime, timedelta, timezone

log = logging.getLogger("gdb2pg.cleaner")


def clean_uploads_once(store, uploads_abs: str, ttl_days: int) -> int:
    """执行一次清理，返回删除的上传记录数。"""
    if ttl_days <= 0:
        return 0
    before = datetime.now(timezone.utc) - timedelta(days=ttl_days)
    stale = store.upload_stale(before)
    if not stale:
        return 0
    referenced = store.task_gdb_paths()
    removed = 0
    for rec in stale:
        gdb = rec["gdb_path"]
        if gdb in referenced:
            continue
        # 只清理 uploads 根内的目录，防误删其它路径
        if gdb.startswith(uploads_abs + os.sep):
            shutil.rmtree(os.path.dirname(gdb), ignore_errors=True)
            shutil.rmtree(gdb, ignore_errors=True)
        store.upload_delete(rec["id"])
        removed += 1
        log.info("清理过期上传: %s (md5=%s)", gdb, rec["md5"])
    return removed


class UploadCleaner:
    """后台守护线程，默认每 24h 清理一次。"""

    def __init__(self, store, uploads_abs: str, ttl_days: int,
                 interval_hours: float = 24.0):
        self._store = store
        self._uploads_abs = uploads_abs
        self._ttl_days = ttl_days
        self._interval_hours = interval_hours
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, name="upload-cleaner", daemon=True)

    @property
    def enabled(self) -> bool:
        return self._ttl_days > 0

    def _loop(self) -> None:
        while not self._stop.wait(self._interval_hours * 3600):
            try:
                n = clean_uploads_once(self._store, self._uploads_abs, self._ttl_days)
                if n:
                    log.info("uploads 清理完成：删除 %d 条", n)
            except Exception:  # noqa: BLE001 清理失败不影响服务
                log.exception("uploads 清理异常")

    def start(self) -> None:
        if self.enabled:
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()