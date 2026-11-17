from app.model.protocol import StreamingModel
from app.store.memory import MemoryStore
from app.tools.registry import ToolRegistry


async def execute_run(
    store: MemoryStore,
    model: StreamingModel,
    tools: ToolRegistry,
    run_id: str,
) -> None:
    """把一次运行推进到终止事件。控制过程在 Agent Loop 步骤实现。"""
    raise NotImplementedError("Agent Loop 尚未实现")
