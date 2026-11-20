from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class VariableDecl(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1)
    required: bool = False


class ToolContract(BaseModel):
    """模型看见的工具说明。能不能写、超时和幂等仍由注册表决定。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1)
    description: str
    input_schema: dict[str, Any]


class ModelSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    alias: str = Field(min_length=1)
    temperature: float | None = None
    tool_choice: str | None = None


class OutputRule(BaseModel):
    """schema 表达不了的业务规则。目前只检查数字是否来自工具原文。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: Literal["number_from_source"]
    field: str = Field(min_length=1)


class OutputContract(BaseModel):
    """最终答案必须被程序解析时才挂上。空着表示仍是自由文本。"""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)
    json_schema: dict[str, Any] = Field(alias="schema")
    rules: list[OutputRule] = Field(default_factory=list)


class PromptVersion(BaseModel):
    """写入后不再修改。content_hash 只覆盖会改变行为的字段。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1)
    version: int = Field(ge=1)
    hypothesis: str = Field(min_length=1)
    template: str
    variables: list[VariableDecl]
    tools: list[ToolContract]
    assembly: Literal["system_then_thread"]
    model: ModelSettings
    output: OutputContract | None = None
    content_hash: str


class Expectation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    tool: str | None = None
    arguments: dict[str, Any] | None = None
    then_stop: bool = False
    no_tool: bool = False

    @model_validator(mode="after")
    def _shape(self) -> "Expectation":
        if self.no_tool == (self.tool is not None):
            raise ValueError("用例要么禁止调用工具，要么指定一个工具")
        if self.tool is not None and self.arguments is None:
            raise ValueError("调用工具的用例需要期望参数")
        if self.no_tool and self.arguments is not None:
            raise ValueError("不调用工具的用例没有参数")
        return self


class ToolResultFixture(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    ok: bool
    output: str


class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    input: str = Field(min_length=1)
    expect: Expectation
    tool_result: ToolResultFixture | None = None
    variables: dict[str, str] = Field(default_factory=dict)


class EvalSet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1)
    version: int = Field(ge=1)
    cases: list[EvalCase]
    content_hash: str
