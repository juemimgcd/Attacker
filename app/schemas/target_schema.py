from enum import Enum

from pydantic import BaseModel, Field, HttpUrl


# 定义目标 Agent 调用支持的 HTTP 方法。
class HTTPMethod(str, Enum):
    post = "POST"


# 定义目标 Agent 的鉴权方式和鉴权头配置。
class TargetAuth(BaseModel):
    type: str = "none"
    token: str | None = None
    header_name: str = "Authorization"
    token_prefix: str = "Bearer"


# 定义发送给目标 Agent 的请求体模板。
class TargetRequestTemplate(BaseModel):
    body_template: dict = Field(
        default_factory=lambda: {
            "messages": [
                {
                    "role": "user",
                    "content": "{prompt}",
                }
            ]
        }
    )


# 定义目标 Agent 的完整 HTTP 接入配置。
class TargetConfig(BaseModel):
    name: str
    endpoint: HttpUrl
    method: HTTPMethod = HTTPMethod.post
    headers: dict[str, str] = Field(default_factory=dict)
    auth: TargetAuth = Field(default_factory=TargetAuth)
    timeout_seconds: float = 30.0
    request_template: TargetRequestTemplate = Field(default_factory=TargetRequestTemplate)
