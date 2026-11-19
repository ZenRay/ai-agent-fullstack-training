from app.tools.registry import Tool, ToolRegistry

_SCHEMA = {
    "type": "object",
    "properties": {"city": {"type": "string", "description": "城市名，例如北京"}},
    "required": ["city"],
    "additionalProperties": False,
}


async def get_weather(city: str) -> str:
    """返回固定晴天描述，用来走完一次发问。不访问外部天气服务。"""
    return f"{city}今天晴，31度。"


WEATHER = Tool(
    name="get_weather",
    description="查询城市当前天气。只读，不会修改数据。调用前需要城市名。",
    input_schema=_SCHEMA,
    kind="read",
    handler=get_weather,
)


def register_weather(registry: ToolRegistry) -> None:
    registry.register(WEATHER)
