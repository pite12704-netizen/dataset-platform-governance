"""【学习标注：统一配置】所有 DP_ 环境变量在这里声明，并集中校验生产环境安全要求。"""
from functools import lru_cache
import os
import json
from pathlib import Path
from typing import Literal
from sqlalchemy.engine import make_url

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DP_", env_file=".env", extra="ignore", hide_input_in_errors=True)
    environment: Literal["development", "test", "production"] = "development"
    deployment: Literal["public", "private"] = "private"
    registration_mode: Literal['deployment', 'closed', 'open'] = 'deployment'
    secret_key: str = Field(min_length=32,repr=False)
    session_signing_key: str = Field(default='',repr=False)
    session_signing_key_file: Path | None = None
    mfa_encryption_keys: list[str] = Field(default_factory=list,repr=False)
    mfa_encryption_keys_file: Path | None = None
    mfa_allow_legacy_key: bool = True
    database_url: str = "sqlite:///./var/platform.db"
    database_pool_size: int = Field(default=10, ge=1, le=100)
    database_max_overflow: int = Field(default=10, ge=0, le=100)
    database_pool_recycle: int = Field(default=1800, ge=60)
    database_pool_timeout: float = Field(default=5, ge=.1, le=30)
    database_connect_timeout: int = Field(default=10, ge=1, le=30)
    database_read_timeout: int = Field(default=30, ge=1, le=300)
    database_write_timeout: int = Field(default=30, ge=1, le=300)
    api_thread_tokens: int = Field(default=40, ge=4, le=128)
    # Agent 纯 SQL 阶段使用原生异步连接；独立预算，不能遗漏在 MySQL 总连接数中。
    database_async_pool_size: int = Field(default=5, ge=1, le=50)
    database_async_max_overflow: int = Field(default=0, ge=0, le=50)
    request_body_timeout_seconds: float = Field(default=30, ge=.1, le=120)
    upload_receive_timeout_seconds: float = Field(default=120, ge=.1, le=300)
    upload_max_inflight: int = Field(default=8, ge=1, le=32)
    download_max_inflight: int = Field(default=8, ge=1, le=64)
    media_max_inflight: int = Field(default=4, ge=1, le=16)
    agent_status_max_inflight: int = Field(default=8,ge=1,le=64)
    agent_status_stream_seconds: float = Field(default=45,ge=5,le=120)
    agent_status_poll_seconds: float = Field(default=1,ge=.2,le=10)
    mcp_max_inflight: int = Field(default=4, ge=1, le=32)
    public_url: str = "http://localhost:8000"
    frontend_url: str = "http://localhost:5173"
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    storage_backend: Literal["local", "s3"] = "local"
    storage_root: Path = Path("var/objects")
    s3_endpoint: str = "http://localhost:9000"
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_bucket: str = "datasets"
    s3_region: str = "us-east-1"
    s3_max_pool_connections: int = Field(default=40, ge=1, le=128)
    redis_url: str = "redis://localhost:6379/0"
    task_mode: Literal["celery", "inline", "manual"] = "celery"
    worker_role: Literal["worker", "vision", "mail"] = "worker"
    session_hours: int = 12
    cookie_secure: bool = False
    require_admin_mfa: bool = True
    require_verification: bool = True
    default_quota_bytes: int = Field(default=10 * 1024**3,ge=1)
    account_max_organizations: int = Field(default=3, ge=0, le=1000)
    account_quota_budget_bytes: int = Field(default=40 * 1024**3, ge=1)
    platform_storage_budget_bytes: int = Field(default=100 * 1024**3, ge=1)
    dispatch_lease_seconds: int = Field(default=60, ge=30, le=900)
    dispatch_max_backoff_seconds: int = Field(default=900, ge=60, le=3600)
    backup_metadata_max_bytes: int = Field(default=128*1024**2,ge=1024,le=1024**3)
    max_file_bytes: int = 5 * 1024**3
    part_size_bytes: int = 8 * 1024**2
    max_conversion_bytes: int = 64 * 1024**2
    upload_expiry_hours: int = 24
    scan_mode: Literal["clamav", "development"] = "development"
    clamav_host: str = "localhost"
    clamav_port: int = 3310
    malware_max_bytes: int = Field(default=64 * 1024**2, ge=1)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    smtp_ssl: bool = False
    smtp_ca_file: Path | None = None
    mail_from: str = "datasets@localhost"
    mail_spool: Path = Path("var/mail")
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    mcp_issuer: str = ""
    mcp_audience: str = ""
    mcp_jwks_url: str = ""
    mcp_jwks_timeout_seconds: float = Field(default=3, ge=.1, le=10)
    mcp_jwks_cache_seconds: int = Field(default=300, ge=30, le=3600)
    model_url: str = ""
    model_name: str = ""
    model_api_key: str = ""
    agent_tools_enabled: bool = False
    agent_model_revision: str = Field(default='',max_length=200)
    agent_release_evidence_file: Path | None = None
    agent_release_evidence_sha256: str = Field(default='',pattern=r'^(?:[a-f0-9]{64})?$')
    # 同一模型服务的 API 与视觉 Worker 共用并发额度，生产环境用 Redis 原子协调。
    model_max_inflight: int = Field(default=2, ge=1, le=32)
    model_resource_group: str = Field(default="", max_length=128)
    model_queue_seconds: float = Field(default=2, ge=0, le=10)
    model_retry_attempts: int = Field(default=2, ge=1, le=3)
    model_retry_base_seconds: float = Field(default=.25, ge=.01, le=2)
    model_breaker_failures: int = Field(default=3, ge=1, le=20)
    model_breaker_seconds: float = Field(default=30, ge=.1, le=300)
    vision_timeout_seconds: int = Field(default=120, ge=5, le=300)
    vision_attribute_mode: Literal['one-pass','crop-pass']='one-pass'
    vision_image_budget_seconds: int = Field(default=180,ge=5,le=600)
    agent_max_rounds: int = Field(default=4, ge=1, le=8)
    agent_max_tool_calls: int = Field(default=6, ge=1, le=12)
    agent_timeout_seconds: int = Field(default=45, ge=5, le=120)
    agent_terminal_timeout_seconds: float = Field(default=5, ge=.1, le=15)
    agent_context_bytes: int = Field(default=32768, ge=4096, le=131072)
    agent_model_response_bytes: int = Field(default=65536, ge=4096, le=262144)
    agent_max_output_tokens: int = Field(default=1200, ge=128, le=4096)
    agent_total_tokens: int = Field(default=12000, ge=1024, le=65536)
    agent_history_turns: int = Field(default=6, ge=0, le=12)
    agent_history_bytes: int = Field(default=8192, ge=512, le=32768)
    vision_enabled: bool = False
    vision_max_images: int = Field(default=100, ge=1, le=100)
    vision_plan_active_shards: int = Field(default=4,ge=1,le=20)
    space_max_vision_plans: int = Field(default=2,ge=1,le=20)
    space_max_active_vision_shards: int = Field(default=16,ge=1,le=100)
    vision_model_revision: str = Field(default="", max_length=200)
    vision_model_url: str = ""
    vision_model_name: str = ""
    vision_model_api_key: str = ""
    vision_max_response_bytes: int = Field(default=262144, ge=4096, le=1048576)
    governance_max_images: int = Field(default=10000, ge=1, le=100000)
    dedup_max_candidates: int = Field(default=20000, ge=1, le=100000)
    dedup_max_comparisons: int = Field(default=100000000, ge=1, le=1000000000)
    delivery_max_images: int = Field(default=10000,ge=1,le=100000)
    delivery_max_annotations: int = Field(default=100000,ge=1,le=1000000)
    preview_cache_bytes: int = Field(default=32*1024**2,ge=0,le=512*1024**2)
    preview_cache_ttl_seconds: int = Field(default=600,ge=1,le=3600)
    thumbnail_persistence_enabled: bool = True
    agent_content_retention_days: int = Field(default=30,ge=1,le=3650)
    agent_memory_pins: int = Field(default=8,ge=1,le=32)
    agent_topic_history_scan: int = Field(default=64,ge=0,le=256)
    acceptance_namespace: str = Field(default='',max_length=64)
    quality_min_truth_images: int = Field(default=100, ge=1, le=10000)
    quality_min_positives: int = Field(default=20, ge=1, le=10000)
    quality_min_negatives: int = Field(default=20, ge=0, le=10000)
    quality_min_attribute_coverage: float = Field(default=.95, ge=0, le=1)
    quality_min_precision: float = Field(default=.9, ge=0, le=1)
    quality_min_recall: float = Field(default=.8, ge=0, le=1)
    review_lease_seconds: int = Field(default=300, ge=60, le=1800)
    secret_key_file: Path | None = None
    database_url_file: Path | None = None
    s3_access_key_file: Path | None = None
    s3_secret_key_file: Path | None = None
    redis_url_file: Path | None = None
    smtp_password_file: Path | None = None
    rate_limit_per_minute: int = 120
    transfer_limit_per_minute: int = Field(default=1200, ge=1, le=60000)
    login_limit_per_minute: int = 10

    @property
    def registration_enabled(self) -> bool:
        return self.registration_mode == 'open' or self.registration_mode == 'deployment' and self.deployment == 'public'

    @property
    def registration_email_required(self) -> bool:
        return self.deployment == 'public' or self.require_verification

    @property
    def mail_delivery_mode(self) -> str:
        return 'smtp' if self.smtp_host else 'spool' if self.environment != 'production' else 'disabled'

    @property
    def max_upload_bytes(self) -> int:
        """接收上限与扫描容量一致，避免已知无法检查的文件占用存储和上传带宽。"""
        return min(self.max_file_bytes, self.malware_max_bytes) if self.scan_mode == "clamav" else self.max_file_bytes

    @model_validator(mode="before")
    @classmethod
    def read_secret_files(cls, data):
        # Docker secrets 保存在挂载文件，密钥不进入镜像或源码。
        data = dict(data)
        for name in ("secret_key", "session_signing_key", "mfa_encryption_keys", "database_url", "s3_access_key", "s3_secret_key", "redis_url", 'smtp_password'):
            path = data.get(name + "_file")
            if path:
                data[name] = Path(path).read_text(encoding="utf-8").strip()
                if name=='mfa_encryption_keys':data[name]=json.loads(data[name])
        return data

    @property
    def jwt_signing_key(self):
        return self.session_signing_key or self.secret_key

    @model_validator(mode='after')
    def validate_security_keys(self):
        if self.session_signing_key and len(self.session_signing_key)<32:
            raise ValueError('Dedicated session signing key must contain at least 32 characters')
        from cryptography.fernet import Fernet
        try:
            for key in self.mfa_encryption_keys:Fernet(key.encode())
        except (ValueError,TypeError):
            raise ValueError('Invalid MFA encryption key configuration') from None
        if not self.mfa_allow_legacy_key and not self.mfa_encryption_keys:
            raise ValueError('A dedicated MFA key is required before retiring the legacy key')
        return self

    @model_validator(mode="after")
    def production_checks(self):
        if self.environment == "production":
            if self.acceptance_namespace:raise ValueError('验收命名空间仅允许隔离测试环境')
            url = make_url(self.database_url)
            if url.drivername != "mysql+pymysql" or not url.database or not url.username or not url.password:
                raise ValueError("生产环境必须配置 MySQL 与专用数据库账号，不能使用 SQLite")
            if url.username.lower() == "root":
                raise ValueError("业务服务不能使用 MySQL root 账号")
            if self.storage_backend != "s3" or self.task_mode != "celery":
                raise ValueError("生产环境必须使用 S3 存储与 Celery Worker")
            if self.scan_mode != "clamav":
                raise ValueError("生产环境必须配置 ClamAV 扫描，不能降级跳过")
            if not self.cookie_secure or not self.public_url.startswith("https://"):
                raise ValueError("生产环境必须使用 HTTPS 和 Secure Cookie")
            if not self.require_admin_mfa:
                raise ValueError("生产环境必须启用管理员 MFA")
            if not self.s3_access_key or not self.s3_secret_key or not self.s3_bucket:
                raise ValueError("生产环境必须配置私有对象桶与应用专用存储凭据")
            if self.part_size_bytes < 5 * 1024**2:
                raise ValueError("S3 分片大小不能小于 5MiB")
            if self.deployment == "public" and self.registration_enabled and (not self.smtp_host or not self.require_verification):
                raise ValueError("公网生产环境必须配置账号验证与邮件服务")
            if self.registration_enabled and self.require_verification and not self.smtp_host:
                raise ValueError('开放邮箱验证注册必须配置 SMTP 邮件服务')
            if self.smtp_host and not (self.smtp_ssl or self.smtp_starttls):
                raise ValueError('生产邮件服务必须启用 SSL 或 STARTTLS')
            if self.smtp_host and ('@' not in self.mail_from or self.mail_from.endswith('@localhost')):
                raise ValueError('邮件服务须配置真实发件地址 DP_MAIL_FROM')
            if self.agent_tools_enabled:
                from .agent_release import validate
                validate(self)
        return self


@lru_cache
def get_settings():
    return Settings(_env_file=os.environ.get('DP_SETTINGS_FILE', '.env'))
