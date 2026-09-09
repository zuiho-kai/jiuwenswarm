"""Validated operator configuration for the optional desktop worker."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .capabilities import resolve_cua_capabilities


class CuaConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    enabled: bool = False
    command: str = "cua-driver"
    capabilities: list[str] = Field(default_factory=list)
    delivery_mode: Literal["background", "foreground"] = "background"
    screenshot_multimodal: bool = False
    snapshot_keep_last_k: int = Field(default=3, ge=1, le=10)
    max_iterations: int = Field(default=25, ge=1, le=100)
    timeout_s: int = Field(default=300, ge=1, le=1800)
    tool_timeout_s: int = Field(default=60, ge=1, le=300)
    lock_wait_s: int = Field(default=30, ge=0, le=300)

    @field_validator("capabilities")
    @classmethod
    def validate_capabilities(cls, value: list[str]) -> list[str]:
        rejected = resolve_cua_capabilities(value).rejected_names
        if rejected:
            raise ValueError(f"Unknown CUA capabilities: {', '.join(rejected)}")
        return value

    @field_validator("command")
    @classmethod
    def validate_command(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("CUA command cannot be empty")
        return value.strip()

    def resolve_command(self) -> str:
        resolved = shutil.which(self.command)
        if resolved:
            return resolved
        if self.command == "cua-driver" and os.environ.get("LOCALAPPDATA"):
            candidate = (
                Path(os.environ["LOCALAPPDATA"])
                / "Programs/Cua/cua-driver/bin/cua-driver.exe"
            )
            if candidate.is_file():
                return str(candidate)
        raise FileNotFoundError(
            f"CUA driver not found: {self.command}. Install cua-driver==0.10.0 and start "
            "`cua-driver serve` in the desktop user's interactive session."
        )

    @property
    def allowed_tools(self) -> tuple[str, ...]:
        # Lifecycle belongs to the host, never the model.
        return tuple(
            name
            for name in resolve_cua_capabilities(self.capabilities).allowed_tool_names
            if name not in {"start_session", "end_session"}
        )
