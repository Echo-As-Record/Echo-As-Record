"""显式范围是选择条件，不是授权；None 才表示原来的列表扫描。"""

from collections.abc import Sequence
from unicodedata import category


class InvalidCaptureSelection(ValueError):
    """固定消息，不回显可能含私有数据的非法输入。"""

    def __init__(self) -> None:
        super().__init__("capture ID 必须是非空安全标识，显式选择不得为空")


def validate_capture_id(value: str) -> str:
    # 上游 opaque ID，不要求 UUID；URL query/fragment 字符由 HTTP 层百分号编码。
    if (
        not isinstance(value, str)
        or not value
        or value in (".", "..")
        or any(c in "/\\" or c.isspace() or category(c).startswith("C") for c in value)
    ):
        raise InvalidCaptureSelection()
    return value


def capture_selection(capture_ids: Sequence[str] | None) -> tuple[str, ...] | None:
    if capture_ids is None:
        return None
    if isinstance(capture_ids, str | bytes) or not isinstance(capture_ids, Sequence):
        raise InvalidCaptureSelection()
    selected = tuple(dict.fromkeys(validate_capture_id(value) for value in capture_ids))
    if not selected:
        raise InvalidCaptureSelection()
    return selected
