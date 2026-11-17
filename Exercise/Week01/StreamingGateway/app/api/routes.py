from fastapi import APIRouter

from app.store.memory import MemoryStore


class RunRouter(APIRouter):
    def __init__(self, store: MemoryStore) -> None:
        super().__init__()
        self.store = store


def create_router(store: MemoryStore) -> RunRouter:
    """创建运行与取消的路由。具体处理在接口步骤接入。"""
    return RunRouter(store)
