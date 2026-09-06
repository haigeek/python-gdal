# -*- coding: utf-8 -*-
"""任务执行管理器：FIFO 队列 + worker 线程 + 协作式取消。

- start() 启动 workers 个后台线程；enqueue() 把任务置 queued 并入队；
- worker 取任务 -> running -> 构造 TaskContext 注入 store 的日志/进度 ->
  type.run()；成功 -> succeeded + result；TaskCancelled -> cancelled；
  其他异常 -> failed + error（并落一条 traceback 日志）；
- cancel() 对 queued 直接标 cancelled（worker 弹出后跳过），
  对 running 置内存标志，由任务内部 ctx.cancel() 协作响应；
- 单进程部署假设：取消标志与队列都在本进程内。
"""

from __future__ import annotations

import queue
import threading
import time
import traceback
from typing import Optional

from .base import TaskCancelled, TaskContext, get_task_type
from .store import (STATUS_CANCELLED, STATUS_FAILED, STATUS_QUEUED,
                    STATUS_RUNNING, STATUS_SUCCEEDED, TaskStore)


class TaskManager:
    def __init__(self, store: TaskStore, workers: int = 1,
                 force_wait: float = 30.0,
                 no_progress_timeout: float = 20 * 60.0):
        self.store = store
        self.workers = max(1, workers)
        self._force_wait = force_wait  # 取消后强制标记 cancelled 的等待秒数
        # 无进展看护：任务心跳（progress 写入会刷新 updated_at）停更超过该
        # 秒数判定卡死（如 GDB 读取挂起）并自动中止。慢但正常推进的任务
        # 每次 progress 都会续命，不会被误杀。
        self._no_progress_timeout = no_progress_timeout
        self._q: "queue.Queue[Optional[int]]" = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._cancel: set[int] = set()
        self._lock = threading.Lock()
        self._started = False

    # ------------------------------------------------------------ 生命周期

    def start(self):
        """启动 worker 线程（幂等）。"""
        if self._started:
            return
        self._started = True
        for i in range(self.workers):
            t = threading.Thread(
                target=self._worker_loop, name=f"task-worker-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def shutdown(self):
        """停止 worker（发哨兵唤醒；进行中的任务让其自然结束）。"""
        for _ in self._threads:
            self._q.put(None)
        self._threads = []

    # ------------------------------------------------------------ 对外操作

    def enqueue(self, task_id: int):
        """提交任务运行：状态置 queued 并入队。任务需处于可运行状态。"""
        rec = self.store.get(task_id)
        if not rec:
            raise KeyError(f"任务不存在: {task_id}")
        st = rec["status"]
        if st in (STATUS_QUEUED, STATUS_RUNNING):
            raise RuntimeError(f"任务 {task_id} 正在运行中（状态 {st}）")
        self.store.update_state(
            task_id, status=STATUS_QUEUED,
            started_at=None, finished_at=None, error=None,
            progress=None, result=None)  # 清空上次运行残留的进度/结果
        self._q.put(task_id)

    def cancel(self, task_id: int):
        """请求取消：queued 直接标记；running 置协作式标志。"""
        rec = self.store.get(task_id)
        if not rec:
            raise KeyError(f"任务不存在: {task_id}")
        st = rec["status"]
        if st == STATUS_QUEUED:
            with self._lock:
                self._cancel.add(task_id)
            self.store.update_state(task_id, status=STATUS_CANCELLED,
                                    finished_at=True)
        elif st == STATUS_RUNNING:
            with self._lock:
                self._cancel.add(task_id)
        else:
            raise RuntimeError(f"任务不在运行状态（当前 {st}）")

    # ------------------------------------------------------------ worker

    def _worker_loop(self):
        while True:
            task_id = self._q.get()
            if task_id is None:
                break
            self._execute(task_id)

    def _execute(self, task_id: int):
        rec = self.store.get(task_id)
        if not rec:
            return
        # 入队后被取消：跳过执行
        if self._is_cancelled(task_id):
            self.store.update_state(task_id, status=STATUS_CANCELLED,
                                    finished_at=True)
            return
        self.store.update_state(
            task_id, status=STATUS_RUNNING, started_at=True, error=None)

        ttype = get_task_type(rec["type"])
        ctx = TaskContext(
            task_id=task_id,
            log=lambda m: self.store.append_log(task_id, m),
            set_progress=lambda p: self.store.update_state(task_id, progress=p),
            cancel=lambda: self._is_cancelled(task_id),
        )

        # 任务体跑在子线程：取消请求后无论卡在哪里（COPY、GDB 读取阻塞等），
        # 最多等待 FORCE_WAIT 秒即可强制标记 cancelled，保证状态可收敛。
        holder: dict = {}

        def _work():
            try:
                holder["result"] = ttype.run(ctx, rec["config"] or {})
            except TaskCancelled:
                holder["cancelled"] = True
            except Exception as e:  # noqa: BLE001 任务失败统一落库
                holder["error"] = e
                holder["tb"] = traceback.format_exc()

        runner = threading.Thread(
            target=_work, name=f"task-runner-{task_id}", daemon=True)
        runner.start()

        FORCE_WAIT = self._force_wait
        aborted_by_watchdog = False
        next_beat = time.monotonic()
        while runner.is_alive():
            if self._is_cancelled(task_id):
                runner.join(FORCE_WAIT)
                if runner.is_alive():
                    # 卡死（如 OGR 读取阻塞）无法终止：强制标记，线程为
                    # daemon，随进程收尾且其后续结果不再写库
                    try:
                        # 主动断开任务持有的数据库连接 -> 服务端会话终止 ->
                        # 事务回滚 -> 锁立即释放（不留锁残链）
                        from ..importer import close_writer_for_thread
                        close_writer_for_thread(runner.ident)
                        self.store.append_log(
                            task_id,
                            "[取消] 任务线程未在 30s 内退出，已强制标记为已取消"
                            "（已断开其数据库连接释放锁；后台线程将自行结束）",
                        )
                    except Exception:  # noqa: BLE001 日志失败不影响主流程
                        pass
                    self.store.update_state(
                        task_id, status=STATUS_CANCELLED, finished_at=True)
                break
            runner.join(0.5)
            # 无进展看护：每 30s 检查一次任务心跳（updated_at 由 progress
            # 写入刷新）；停更超过阈值判定卡死，自动中止（状态归 failed）
            if time.monotonic() >= next_beat:
                next_beat = time.monotonic() + 30.0
                rec = self.store.get(task_id)
                if rec:
                    beat = rec.get("updated_at") or rec.get("started_at")
                    if beat is not None:
                        idle = time.time() - beat.timestamp()
                        if idle > self._no_progress_timeout:
                            aborted_by_watchdog = True
                            with self._lock:
                                self._cancel.add(task_id)
                            msg = (f"任务 {idle/60:.0f} 分钟无任何写入进度，"
                                   "判定卡死，已自动中止")
                            try:
                                self.store.append_log(
                                    task_id, "[卡死看护] " + msg)
                            except Exception:  # noqa: BLE001
                                pass
                            runner.join(FORCE_WAIT)
                            if runner.is_alive():
                                try:
                                    from ..importer import close_writer_for_thread
                                    close_writer_for_thread(runner.ident)
                                except Exception:  # noqa: BLE001
                                    pass
                            self.store.update_state(
                                task_id, status=STATUS_FAILED,
                                error=msg + "（已释放数据库锁）", finished_at=True)
                            break

        # 任务线程已结束：先按取消判定（含线程结束前后到达的取消请求）
        if not aborted_by_watchdog and self._is_cancelled(task_id):
            self.store.update_state(
                task_id, status=STATUS_CANCELLED, finished_at=True)
        elif not aborted_by_watchdog and "result" in holder:
            self.store.update_state(task_id, status=STATUS_SUCCEEDED,
                                    result=holder["result"], finished_at=True)
        elif not aborted_by_watchdog and "error" in holder:
            self.store.update_state(task_id, status=STATUS_FAILED,
                                    error=str(holder["error"]), finished_at=True)
            try:
                self.store.append_log(task_id, holder["tb"])
            except Exception:  # noqa: BLE001 日志失败不掩盖主错误
                pass
        with self._lock:
            self._cancel.discard(task_id)

    def _is_cancelled(self, task_id: int) -> bool:
        with self._lock:
            return task_id in self._cancel