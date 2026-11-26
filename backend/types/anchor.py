"""Physical-pixel replay coordinate, independent of transient display/window identities."""
from dataclasses import dataclass

from ..storage.errors import StorageError


@dataclass(frozen=True)
class AnchoredPoint:
    version: int
    scope: str
    strategy: str
    reference_width: int
    reference_height: int
    x: int
    y: int

    @classmethod
    def parse(cls, value):
        if not isinstance(value, dict) or value.keys() != cls.__dataclass_fields__.keys():
            raise StorageError("INVALID_PARAMS", "定位表达必须具有全部且仅有已声明字段", field="anchor")
        a = cls(**value)
        if (type(a.version) is not int or a.version != 1 or a.scope not in ("window", "region", "display")
                or a.strategy not in ("relative", "offset")
                or any(type(n) is not int or not 1 <= n <= 2147483647 for n in (a.reference_width, a.reference_height))
                or any(type(n) is not int or not -2147483648 <= n <= 2147483647 for n in (a.x, a.y))):
            raise StorageError("INVALID_PARAMS", "定位版本、策略、范围或坐标类型无效", field="anchor")
        if not (0 <= a.x < a.reference_width and 0 <= a.y < a.reference_height):
            raise StorageError("REGION_LIMIT_VIOLATION", "源点越出录制范围", field="anchor")
        return a

    def resolve(self, rect):
        """Pure projection for previews and shared vectors. Dispatch resolves again in the host."""
        def axis(p, old, new, origin):
            if not 1 <= new <= 2147483647:
                raise StorageError("REGION_LIMIT_VIOLATION", "当前范围尺寸无效")
            if self.strategy == "relative":
                p = 0 if old == 1 else (p * (new - 1) + (old - 1) // 2) // (old - 1)
            if p >= new or not -2147483648 <= origin + p <= 2147483647:
                raise StorageError("REGION_LIMIT_VIOLATION", "局部点越出当前范围")
            return p + origin

        return (axis(self.x, self.reference_width, rect["width"], rect["x"]),
                axis(self.y, self.reference_height, rect["height"], rect["y"]))
