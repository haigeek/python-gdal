# -*- coding: utf-8 -*-
"""任务类型抽象：TaskContext / TaskType / 注册表 / 协作式取消异常。

未来新增任务类型时，实现一个 TaskType 子类（type / label / form_schema +
validate / preview / run / sanitize），用 @register 装饰即可：
- 后端自动出现在 /api/task-types，前端据此动态渲染表单；
- 任务框架统一负责状态机、日志、进度、取消、后台执行，类型只关心"怎么做"。
"""

from __future__ import annotations

from abc import ABC
from typing import Callable, Optional


class TaskCancelled(Exception):
    """协作式取消：任务内部确认取消请求后抛出，manager 据此标记 cancelled。"""


class TaskContext:
    """manager 在执行任务时注入的运行上下文，类型实现通过它上报运行状态。"""

    def __init__(
        self,
        task_id: int,
        log: Callable[[str], None],
        set_progress: Callable[[dict], None],
        cancel: Callable[[], bool],
    ):
        self.task_id = task_id
        self.log = log            # 追加一行日志（落业务库 task_log）
        self.set_progress = set_progress  # 更新轻量进度 {current,total,source,rows}
        self.cancel = cancel      # 返回 True 表示用户请求取消


class TaskType(ABC):
    """任务类型基类。子类必须覆盖 type / label / run；其余有默认实现。"""

    type: str = ""          # 唯一标识，如 'gdb_import'
    label: str = ""         # 界面显示名

    # 驱动前端动态表单的结构化描述（见 gdb_import.py 内示例）：
    #   {"groups": [{"key","label","fields":[
    #       {"key","label","type":"text|int|bool|password|enum|table|gdb_path",
    #        "default","options"(enum), "help"}],
    #     "table"(type=table 时): {"columns":[{"key","label","type"}], "label"}}]}
    form_schema: dict = {}

    def validate(self, config: dict) -> list[str]:
        """纯配置校验：返回错误列表，空列表 = 通过（创建/更新前调用）。"""
        return []

    def preview(self, config: dict) -> dict:
        """dry-run 预览：返回可 JSON 化的 dict（前端"预览"按钮调用）。"""
        return {}

    def run(self, ctx: TaskContext, config: dict) -> dict:
        """阻塞执行，返回可 JSON 化的 result dict。

        检测到 ctx.cancel() 为 True 时应尽快停止并抛 TaskCancelled。
        """
        raise NotImplementedError

    def sanitize(self, config: dict) -> dict:
        """API 输出前的脱敏（如密码打码）；输入输出均为新 dict。"""
        return config


# ---------------------------------------------------------------- 注册表

TASK_REGISTRY: dict[str, TaskType] = {}


def register(cls):
    """类装饰器：把 TaskType 子类登记进全局注册表。"""
    inst = cls()
    t = inst.type
    if not t:
        raise ValueError(f"{cls.__name__} 未定义 type")
    if t in TASK_REGISTRY:
        raise ValueError(f"任务类型已注册: {t}")
    TASK_REGISTRY[t] = inst
    return cls


def get_task_type(t: str) -> Optional[TaskType]:
    """按类型标识取实例；未注册返回 None。"""
    return TASK_REGISTRY.get(t)


def registered_types() -> list[TaskType]:
    """已注册的全部任务类型实例。"""
    return list(TASK_REGISTRY.values())