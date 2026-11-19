"""品类域出入参模型（Pydantic v2）。

`code` 只允许小写字母、数字与连字符：它会出现在 URL（/shop?category=fruit-citrus）
和接口参数里，限制字符集可以避免编码问题，也让「改中文名不改链接」成为可能。
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

CODE_PATTERN = r"^[a-z0-9][a-z0-9-]{1,31}$"


class CategoryCreate(BaseModel):
    code: str = Field(..., min_length=2, max_length=32, pattern=CODE_PATTERN)
    name: str = Field(..., min_length=1, max_length=32)
    parent_id: str | None = Field(default=None, max_length=32)
    icon: str = Field(default="", max_length=8)
    image: str = Field(default="", max_length=200)
    sort_no: int = Field(default=0, ge=0, le=9999)
    description: str = Field(default="", max_length=200)

    @field_validator("code", "name", "icon", "image", "description", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class CategoryUpdate(BaseModel):
    """只传要改的字段；code 允许修改但必须保持全局唯一。"""

    code: str | None = Field(default=None, min_length=2, max_length=32, pattern=CODE_PATTERN)
    name: str | None = Field(default=None, min_length=1, max_length=32)
    parent_id: str | None = Field(default=None, max_length=32)
    icon: str | None = Field(default=None, max_length=8)
    image: str | None = Field(default=None, max_length=200)
    sort_no: int | None = Field(default=None, ge=0, le=9999)
    description: str | None = Field(default=None, max_length=200)
    enabled: bool | None = None

    @field_validator("code", "name", "icon", "image", "description", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class CategoryOut(BaseModel):
    category_id: str
    code: str
    name: str
    parent_id: str | None = None
    parent_name: str = ""
    level: int = 1
    icon: str = ""
    image: str = ""
    sort_no: int = 0
    enabled: bool = True
    description: str = ""
    product_count: int = 0
    children: list[CategoryOut] = Field(default_factory=list)


CategoryOut.model_rebuild()
