"""
app/domain/command_models.py

Pydantic models for the command-execution proxy. Mirrored from deploy-service's
app/domain/command.py but trimmed to the request/response/whitelist shapes
cluster-service needs — no runtime dataclasses, no asyncssh. cluster-service
only forwards JSON to deploy-service; it never executes SSH itself.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class CommandStatus(str, Enum):
    RUNNING = "running"
    KILLING = "killing"
    KILLED = "killed"
    SUCCESS = "success"
    FAILED = "failed"


class HostType(str, Enum):
    IP = "ip"
    BASTION = "bastion"
    HOSTNAME = "hostname"


# ── Whitelist configuration ──────────────────────────────────────────────────

class CommandArgumentConfig(BaseModel):
    name: str
    type: str
    validation_regex: str = ""
    required: bool = True
    description: str = ""


class PipelineStep(BaseModel):
    command: List[str]


class CommandWhitelistConfig(BaseModel):
    command_name: str
    description: str = ""
    disconnects_ssh: bool = False
    killable: bool = False
    logged: bool = False
    pipeline: List[PipelineStep]
    arguments: List[CommandArgumentConfig] = []


class UserCommandWhitelist(BaseModel):
    name: str = "admin"
    allow_hosts: List[str] = [".*"]
    deny_hosts: List[str] = []
    allow_commands: List[CommandWhitelistConfig]


# ── Request / Response ───────────────────────────────────────────────────────

class CommandOption(BaseModel):
    timeout_seconds: int = 30
    bastion_type: Optional[str] = None
    ip_label: Optional[str] = None


class CommandExecutionRequest(BaseModel):
    command_name: str
    host: str
    host_type: HostType = HostType.IP
    port: int = 22
    username: str
    ssh_config: str = "default"
    option: Optional[CommandOption] = Field(default_factory=CommandOption)
    arguments: Dict[str, Any] = Field(default_factory=dict)


class CommandExecutionResponse(BaseModel):
    command_id: Optional[str] = None
    status: str
    message: str = ""
    exit_status: Optional[int] = None
    output: Optional[str] = None
    exec_command: Optional[str] = None
    host_type: Optional[HostType] = None
    resolved_ip: Optional[str] = None
    pgids: List[int] = Field(default_factory=list)


class CommandLogLine(BaseModel):
    num: int
    content_html: str


class CommandTraceResponse(BaseModel):
    command_id: str
    status: str
    next_byte_offset: int
    next_line_num: int
    lines: List[CommandLogLine]
    total_size: int = 0
    size_warning: bool = False
    too_large: bool = False
    log_host: Optional[str] = None
    log_port: Optional[int] = None
    log_user: Optional[str] = None
    log_file_path: Optional[str] = None
