from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ScopeInfo(BaseModel):
    scope: str
    description: str


class Me(BaseModel):
    type: str
    id: str
    name: str
    scopes: list[str]
    via: str
    ip: str | None = Field(default=None, description="This client's address, as Fetchbox sees it")
    connected_from: str | None = Field(
        default=None,
        description="The address that connected to Fetchbox: a reverse proxy's, if any",
    )


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class SetupIn(BaseModel):
    username: str | None = Field(default=None, min_length=1, max_length=64)
    password: str = Field(max_length=1024)


class SessionOut(BaseModel):
    csrf_token: str = Field(description="Send as X-CSRF-Token on cookie-authenticated writes")
    user: str


class ApiKeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    prefix: str
    scopes: list[str]
    created_by: str | None
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    last_used_ip: str | None
    revoked_at: datetime | None


class ApiKeyCreated(ApiKeyOut):
    key: str = Field(description="Full key. Shown once; only a hash is stored.")


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scopes: list[str] = Field(min_length=1)
    expires_at: datetime | None = None


class ApiKeyPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    scopes: list[str] | None = Field(default=None, min_length=1)
    expires_at: datetime | None = Field(
        default=None, description="Set a new expiry; send null with clear_expiry to remove"
    )
    clear_expiry: bool = False
