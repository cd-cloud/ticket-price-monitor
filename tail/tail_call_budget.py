"""Counts attempted provider calls, including initial/enrichment calls and errors."""
class ProviderCallBudgetExceeded(RuntimeError):
    pass


class TailCallBudget:
    def __init__(self, limit=0):
        self.limit = limit
        self.used = 0
        self.by_kind = {}

    @property
    def exhausted(self):
        return bool(self.limit and self.used >= self.limit)

    def consume(self, kind):
        if self.exhausted:
            raise ProviderCallBudgetExceeded("已达到平台调用次数预算")
        self.used += 1
        self.by_kind[kind] = self.by_kind.get(kind, 0) + 1

    def snapshot(self):
        return {"limit": self.limit, "used": self.used, "by_kind": dict(self.by_kind)}
