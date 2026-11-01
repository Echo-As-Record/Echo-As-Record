"""① 的异常。

- 冲突（版本已变、去重键已存在等）不是异常：write 返回带 conflict 的 WriteResult，
  调用方重新读取后重算即可。需要异常时调用 WriteResult.check()，抛出 ConflictError。
- 请求本身有错（格式、未登记的类型、权限）抛异常，整批不生效。重试也不会成功。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ear_storage_client.results import Conflict


class StorageError(Exception):
    """① 的所有异常的基类。"""


class InvalidRequest(StorageError, ValueError):
    """请求格式错误、数据类型未登记、操作不适用于这类数据等。"""


class PermissionDenied(StorageError, PermissionError):
    """调用方模块不负责写这类数据，或不能发布这类事件。"""


class ConflictError(StorageError):
    """WriteResult.check() 遇到冲突时抛出。"""

    def __init__(self, conflict: Conflict) -> None:
        super().__init__(f"{conflict.reason}（第 {conflict.index} 条操作）：{conflict.message}")
        self.conflict = conflict
