"""API contracts for logical Project resources."""

import json
import re
from datetime import datetime
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ResourceType = Literal["storage", "dataset", "database", "compute"]
_SENSITIVE_KEYS = {
    "password",
    "secret",
    "token",
    "api_key",
    "credential",
    "private_key",
    "access_key",
}
_HOST_PATH_KEYS = {"host_path", "local_path", "workspace_root", "project_root"}
_CAPABILITY_KEYS = {"sandbox_mount"}
_SECRET_REF_PATTERN = r"^(?:env|vault|keychain|secret)://[A-Za-z0-9][A-Za-z0-9._/@:-]{0,476}$"


def _canonical_key(key: str) -> str:
    expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
    return expanded.lower().replace("-", "_")


def _validate_public_config(value: Any, *, path: str = "config") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} keys must be strings")
            canonical = _canonical_key(key)
            if canonical in _SENSITIVE_KEYS or any(
                canonical.endswith(f"_{item_key}") for item_key in _SENSITIVE_KEYS
            ):
                raise ValueError(
                    f"{path}.{key} is credential-like; use the write-only secret_ref field"
                )
            if canonical in _HOST_PATH_KEYS:
                raise ValueError(
                    f"{path}.{key} is a host path; use a logical workspace_binding"
                )
            if canonical in _CAPABILITY_KEYS:
                raise ValueError(
                    f"{path}.{key} cannot grant a sandbox capability; use a deployment-owned "
                    "workspace_binding"
                )
            _validate_public_config(item, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _validate_public_config(item, path=f"{path}[{index}]")
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise ValueError(f"{path} contains an unsupported JSON value")


def _validate_endpoint(value: str | None) -> str | None:
    if value is None:
        return None
    endpoint = value.strip()
    if not endpoint:
        return None
    if endpoint.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", endpoint):
        raise ValueError("endpoint cannot be an absolute host path; use workspace_binding")
    parsed = urlsplit(endpoint)
    if parsed.scheme == "file":
        raise ValueError("file:// host paths are not Project resource identifiers")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("endpoint must not contain embedded credentials")
    for key, _item in parse_qsl(parsed.query, keep_blank_values=True):
        canonical = _canonical_key(key)
        if canonical in _SENSITIVE_KEYS or any(
            canonical.endswith(f"_{item_key}") for item_key in _SENSITIVE_KEYS
        ):
            raise ValueError("endpoint query must not contain credentials")
    return endpoint


class ProjectResourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource_type: ResourceType
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2_000)
    provider: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
    )
    endpoint: str | None = Field(default=None, max_length=1_000)
    workspace_binding: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$",
    )
    config: dict[str, Any] = Field(default_factory=dict)
    secret_ref: str | None = Field(default=None, max_length=500, pattern=_SECRET_REF_PATTERN)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("name cannot be blank")
        return cleaned

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str | None) -> str | None:
        return _validate_endpoint(value)

    @field_validator("config")
    @classmethod
    def validate_config(cls, value: dict[str, Any]) -> dict[str, Any]:
        _validate_public_config(value)
        try:
            encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ValueError("config must contain only JSON values") from exc
        if len(encoded.encode("utf-8")) > 16_384:
            raise ValueError("config exceeds the 16 KiB limit")
        return value


class ProjectResourceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource_type: ResourceType | None = None
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2_000)
    provider: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
    )
    endpoint: str | None = Field(default=None, max_length=1_000)
    workspace_binding: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$",
    )
    config: dict[str, Any] | None = None
    secret_ref: str | None = Field(default=None, max_length=500, pattern=_SECRET_REF_PATTERN)
    is_enabled: bool | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("name cannot be blank")
        return cleaned

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str | None) -> str | None:
        return _validate_endpoint(value)

    @field_validator("config")
    @classmethod
    def validate_config(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            raise ValueError("config cannot be null")
        return ProjectResourceCreate.validate_config(value)

    @model_validator(mode="after")
    def require_update(self) -> "ProjectResourceUpdate":
        if not self.model_fields_set:
            raise ValueError("at least one resource field must be supplied")
        for field in ("resource_type", "name", "provider", "is_enabled"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class ProjectResourceOut(BaseModel):
    id: str
    project_id: str
    resource_type: ResourceType
    name: str
    description: str | None
    provider: str
    endpoint: str | None
    workspace_binding: str | None
    config: dict[str, Any]
    is_enabled: bool
    health_status: Literal["unknown"] = "unknown"
    has_secret_reference: bool
    created_by_user_id: str | None
    updated_by_user_id: str | None
    created_at: datetime
    updated_at: datetime
    disabled_at: datetime | None
