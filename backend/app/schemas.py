"""【学习标注：接口输入】Pydantic 在业务函数之前完成类型、长度、枚举和格式校验。"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Register(Input):
    email: str | None = Field(default=None, min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    username: str | None = Field(default=None, min_length=3, max_length=32, pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]*$')
    name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=12, max_length=128)

    @field_validator("email", 'username')
    @classmethod
    def lowercase(cls, value):
        return value.lower() if value else value

    @model_validator(mode='after')
    def identifier_required(self):
        if not self.email and not self.username:
            raise ValueError('请填写账号或邮箱')
        return self


class Login(Input):
    email: str | None = Field(default=None, max_length=254)
    account: str | None = Field(default=None, min_length=1, max_length=254)
    password: str = Field(max_length=128)
    code: str | None = Field(default=None, pattern=r"^\d{6}$")
    recovery_code: str | None = Field(default=None,pattern=r'^[a-fA-F0-9-]{24,26}$')

    @model_validator(mode='after')
    def one_identifier(self):
        if bool(self.email) == bool(self.account):
            raise ValueError('请提供一个登录账号')
        if self.code and self.recovery_code:raise ValueError('认证器验证码与恢复码只填写一种')
        return self


class SensitiveAccount(Input):
    current_password: str = Field(min_length=1,max_length=128)
    code: str | None = Field(default=None,pattern=r'^\d{6}$')


class EmailChange(SensitiveAccount):
    email: str = Field(min_length=3,max_length=254,pattern=r'^[^\s@]+@[^\s@]+\.[^\s@]+$')


class EmailConfirm(SensitiveAccount):
    token: str = Field(min_length=20,max_length=200)


class TokenInput(Input):
    token: str = Field(min_length=20, max_length=200)


class Reset(TokenInput):
    password: str = Field(min_length=12, max_length=128)


class EmailInput(Input):
    email: str = Field(min_length=3, max_length=254, pattern=r'^[^\s@]+@[^\s@]+\.[^\s@]+$')


class ChangePassword(Input):
    current_password: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=12, max_length=128)


class UsernameInput(Input):
    username: str = Field(min_length=3, max_length=32, pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]*$')
    current_password: str = Field(min_length=1, max_length=128)


class CodeInput(Input):
    code: str = Field(pattern=r"^\d{6}$")


class NameInput(Input):
    name: str = Field(min_length=1, max_length=150)


class MemberInput(Input):
    email: str = Field(max_length=254)
    role: Literal["admin", "reviewer", "contributor", "member",'evaluator'] = "member"
    active: bool = True


class ProjectMemberInput(Input):
    user_id: str
    can_apply: bool = False
    active: bool = True


class ModalityInput(Input):
    modality: Literal["text", "image", "audio", "video", "tabular", "multimodal"]


class DatasetInput(Input):
    space_id: str
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=20000)
    modality: Literal["text", "image", "audio", "video", "tabular", "multimodal"] = "text"
    category: str = Field(default="通用", max_length=80)
    tags: list[str] = Field(default_factory=list, max_length=20)
    visibility: Literal["public", "organization", "selected", "private"] = "private"
    access_mode: Literal["open", "application"] = "application"
    audience_users: list[str] = Field(default_factory=list, max_length=100)
    audience_projects: list[str] = Field(default_factory=list, max_length=100)


class VersionInput(Input):
    license: str = Field(min_length=1, max_length=150)
    source: str = Field(min_length=1, max_length=10000)
    card: dict = Field(default_factory=dict)
    changelog: str = Field(default="", max_length=5000)
    quality_tier: Literal['raw', 'curated', 'certified'] = 'raw'

    @field_validator('card')
    @classmethod
    def server_owned_fields(cls, value):
        if any(str(key).startswith('_') or key in {'quality_tier', 'certified'} for key in value):
            raise ValueError('不能设置服务端质量认证或交付状态')
        return value


class UploadInput(Input):
    filename: str = Field(min_length=1, max_length=240)
    size: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    idempotency_key: str = Field(min_length=8, max_length=100)
    preview_allowed: bool = False

    @field_validator("filename")
    @classmethod
    def safe_filename(cls, value):
        if any(c in value for c in '/\\\x00\r\n') or value in {".", ".."}:
            raise ValueError("文件名不能包含路径或控制字符")
        return value


class Decision(Input):
    approve: bool
    comment: str = Field(default="", max_length=5000)


class ApplicationInput(Input):
    version_id: str
    project_id: str | None = None
    purpose: str = Field(min_length=5, max_length=5000)
    duration_days: int = Field(ge=1, le=365, default=30)
    idempotency_key: str = Field(min_length=8, max_length=100)


class AttributeFilter(Input):
    hat: Literal['hat','no_hat','unclear'] | None = None
    sleeve: Literal['short_sleeves','long_sleeves','sleeveless','unclear'] | None = None
    upper_color: Literal['white','black','red','blue','green','yellow','other_or_unclear'] | None = None
    has_person: bool | None = None

    @model_validator(mode='after')
    def negative_attributes(self):
        if self.has_person is False and any((self.hat,self.sleeve,self.upper_color)):
            raise ValueError('无人样本不能同时筛选人物属性')
        return self


class JobInput(Input):
    version_id: str
    kind: Literal["quality", "convert", "package", "vision", "dedup", "split", "evaluate", "curate", "release"]
    target_format: Literal["json", "csv", "png", "jpg", "webp"] | None = None
    idempotency_key: str = Field(min_length=8, max_length=100)
    asset_ids: list[str] = Field(default_factory=list, max_length=100)
    truth_asset_id: str | None = Field(default=None, max_length=32)
    batch_id: str | None = Field(default=None, max_length=32)
    dedup_report_id: str | None = Field(default=None, max_length=32)
    groups: dict[str, str] = Field(default_factory=dict, max_length=10000)
    seed: int = Field(default=20261007, ge=0, le=2147483647)
    near_distance: int = Field(default=6, ge=0, le=16)
    evaluation_target: Literal['model_candidate','approved_annotations']='model_candidate'
    evaluation_set_id: str | None = Field(default=None,min_length=32,max_length=32)
    delivery_format: Literal['coco', 'reviewed-json','yolo','vlm-jsonl'] = 'coco'
    selection: AttributeFilter = Field(default_factory=AttributeFilter)


class ReportInput(Input):
    reason: str = Field(min_length=5, max_length=5000)


class StatusInput(Input):
    status: Literal["active", "unlisted", "blocked"]
    reason: str = Field(min_length=3, max_length=1000)


class CredentialInput(Input):
    name: str = Field(min_length=1, max_length=100)
    scopes: list[Literal["catalog:read", "content:read", "jobs:read", "agent:read",'metrics:read']]
    duration_days: int = Field(ge=1, le=90, default=30)
