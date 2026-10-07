"""
app/clients/dry_run_command_service_client.py

A stand-in for ``CommandServiceClient`` used when ``DRY_RUN_MODE=true``.

The command proxy has its own client (pipelines and SSH commands are distinct
upstream domains), so it needs its own fake; the seam is the same one —
``_get_command_service()`` is the only construction point, and
``CommandService`` above it is untouched. Everything said in
``dry_run_deploy_service_client.py`` applies here too: not a subclass (so a
forgotten method cannot fall through to real HTTP), no token manager, errors
raised as ``DeployServiceError`` from a deploy-service-shaped body so the real
error adapter stays live, and process-global state cleared by
``reset_dry_run_command_service()``.

The whitelist mirrors the upstream ``cluster_proxy`` identity's restriction to
ansible commands, and holds one command of each shape a caller branches on:

- ``dry_run_ansible_ping`` — text output, killable, logged (so the live log
  viewer has something to stream).
- ``dry_run_ansible_facts`` — declares ``output_format: json`` (so
  ``?format=json`` is permitted and parsed), not killable, not logged (so the
  viewer's ``not_logged`` notice and kill's refusal are both reachable).

Lifecycle: an execution is ``running`` in the execute response and ``success``
from its first observation onward — a result poll *or* a trace poll, because
the HTML viewer only ever polls the trace and must see a terminal status to
stop. A kill while running takes it to ``killed``.

Identifiers are deliberately synthetic: command ids are prefixed ``dry-run-``,
the resolved address for non-IP hosts is TEST-NET-1 (RFC 5737), and the log
host uses the reserved ``.invalid`` TLD (RFC 2606).

Design shared with deploy-service; see that repo's docs/arch/dry-run-mode.md.
"""

from __future__ import annotations

import html
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.core.exceptions import DeployServiceError
from app.domain.command_models import (
    CommandArgumentConfig,
    CommandExecutionRequest,
    CommandExecutionResponse,
    CommandLogLine,
    CommandOutputFormat,
    CommandTraceResponse,
    CommandWhitelistConfig,
    HostType,
    OutputFormat,
    OutputJsonError,
    PipelineStep,
    UserCommandWhitelist,
)

_logger = logging.getLogger(__name__)

_RESOLVED_IP = "192.0.2.10"
_LOG_HOST = "dry-run-control-node.invalid"

DRY_RUN_PING = "dry_run_ansible_ping"
DRY_RUN_FACTS = "dry_run_ansible_facts"


def _whitelist() -> list[CommandWhitelistConfig]:
    return [
        CommandWhitelistConfig(
            command_name=DRY_RUN_PING,
            description="dry-run: ansible ping (never executed)",
            killable=True,
            logged=True,
            pipeline=[PipelineStep(command=["ansible", "{host}", "-m", "ping"])],
        ),
        CommandWhitelistConfig(
            command_name=DRY_RUN_FACTS,
            description="dry-run: ansible facts as JSON (never executed)",
            output_format=CommandOutputFormat.JSON,
            pipeline=[PipelineStep(command=["ansible", "{host}", "-m", "setup"])],
            arguments=[
                CommandArgumentConfig(
                    name="filter", type="str", required=False,
                    description="fact filter, echoed back in the output",
                ),
            ],
        ),
    ]


def _error(status: int, code: str, message: str) -> DeployServiceError:
    """Build the error deploy-service would have sent, via the real adapter."""
    return DeployServiceError(
        http_status=status,
        body={"error": {"code": code, "message": f"dry-run: {message}"}},
    )


@dataclass
class _Execution:
    config: CommandWhitelistConfig
    request: CommandExecutionRequest
    response: CommandExecutionResponse
    log_lines: list[str] = field(default_factory=list)


_EXECUTIONS: dict[str, _Execution] = {}


def reset_dry_run_command_service() -> None:
    """Drop all dry-run execution state (process-global; clear it per test)."""
    _EXECUTIONS.clear()


class DryRunCommandServiceClient:
    """In-memory stand-in for ``CommandServiceClient``. Opens no connection."""

    async def get_all_commands_info(self) -> UserCommandWhitelist:
        _logger.warning("DRY-RUN | op=command.get_all_commands_info")
        return UserCommandWhitelist(name="cluster_proxy", allow_commands=_whitelist())

    async def get_command_info(self, command_name: str) -> CommandWhitelistConfig:
        _logger.warning("DRY-RUN | op=command.get_command_info | command=%s", command_name)
        config = self._config(command_name)
        if config is None:
            raise _error(404, "NOT_FOUND", f"command '{command_name}' not found")
        return config

    async def execute_command(
        self, body: CommandExecutionRequest
    ) -> CommandExecutionResponse:
        config = self._config(body.command_name)
        if config is None:
            # The upstream whitelist denies, rather than 404s, a command the
            # identity is not allowed to run.
            raise _error(
                403, "FORBIDDEN",
                f"command '{body.command_name}' is not in the cluster_proxy whitelist",
            )

        command_id = f"dry-run-{uuid.uuid4().hex}"
        exec_command = " && ".join(
            " ".join(step.command).replace("{host}", body.host) for step in config.pipeline
        )
        response = CommandExecutionResponse(
            command_id=command_id,
            status="running",
            message="dry-run: command was not executed",
            exec_command=exec_command,
            host_type=body.host_type,
            resolved_ip=body.host if body.host_type == HostType.IP else _RESOLVED_IP,
        )
        _EXECUTIONS[command_id] = _Execution(
            config=config,
            request=body,
            response=response,
            log_lines=[
                f"dry-run: {config.command_name} on {body.host} ({body.host_type.value})",
                f"dry-run: would run: {exec_command}",
                "dry-run: nothing was executed; no SSH connection was opened",
            ] if config.logged else [],
        )
        _logger.warning(
            "DRY-RUN | op=command.execute_command | command=%s | host=%s | id=%s | "
            "nothing was executed",
            body.command_name, body.host, command_id,
        )
        return response.model_copy(deep=True)

    async def get_command_result(
        self, command_id: str, output_format: OutputFormat = OutputFormat.RAW
    ) -> CommandExecutionResponse:
        execution = self._observe(command_id)
        _logger.warning(
            "DRY-RUN | op=command.get_command_result | id=%s | format=%s",
            command_id, output_format.value,
        )
        wants_json = output_format == OutputFormat.JSON
        if wants_json and execution.config.output_format != CommandOutputFormat.JSON:
            raise _error(
                400, "BAD_REQUEST",
                f"command '{execution.config.command_name}' does not declare "
                "output_format json",
            )

        result = execution.response.model_copy(deep=True)
        if wants_json:
            if result.status == "success":
                result.output_json = json.loads(result.output or "null")
            else:
                result.output_json_error = OutputJsonError.NOT_APPLICABLE
        return result

    async def kill_command(
        self, command_id: str, force: bool = False
    ) -> CommandExecutionResponse:
        execution = self._get(command_id)
        if not execution.config.killable:
            raise _error(
                400, "BAD_REQUEST",
                f"command '{execution.config.command_name}' is not killable",
            )
        if execution.response.status == "running":
            execution.response.status = "killed"
            execution.response.message = "dry-run: kill accepted; nothing was running"
            execution.log_lines.append(f"dry-run: killed (force={force})")
        _logger.warning(
            "DRY-RUN | op=command.kill_command | id=%s | force=%s | no process was killed",
            command_id, force,
        )
        return execution.response.model_copy(deep=True)

    async def get_command_trace(
        self, command_id: str, byte_offset: int = 0, line_num: int = 1
    ) -> CommandTraceResponse:
        execution = self._observe(command_id)
        status = execution.response.status
        if not execution.config.logged:
            return CommandTraceResponse(
                command_id=command_id, status=status,
                next_byte_offset=byte_offset, next_line_num=line_num,
                lines=[], not_logged=True,
            )

        # Serve the slice past byte_offset, as the real incremental endpoint does,
        # so a viewer that polls with the returned cursor never sees a line twice.
        log = "".join(f"{line}\n" for line in execution.log_lines).encode()
        tail = log[byte_offset:].decode(errors="replace").splitlines()
        lines = [
            CommandLogLine(num=line_num + i, content_html=html.escape(text))
            for i, text in enumerate(tail)
        ]
        return CommandTraceResponse(
            command_id=command_id,
            status=status,
            next_byte_offset=len(log),
            next_line_num=line_num + len(lines),
            lines=lines,
            total_size=len(log),
            log_host=_LOG_HOST,
            log_port=22,
            log_user="dry-run",
            log_file_path=f"/dry-run/logs/{command_id}.log",
        )

    # ── internals ─────────────────────────────────────────────────────────────

    @staticmethod
    def _config(command_name: str) -> CommandWhitelistConfig | None:
        return next((c for c in _whitelist() if c.command_name == command_name), None)

    @staticmethod
    def _get(command_id: str) -> _Execution:
        execution = _EXECUTIONS.get(command_id)
        if execution is None:
            raise _error(404, "NOT_FOUND", f"command {command_id} not found")
        return execution

    def _observe(self, command_id: str) -> _Execution:
        """Fetch an execution, completing it on first observation."""
        execution = self._get(command_id)
        response = execution.response
        if response.status == "running":
            response.status = "success"
            response.exit_status = 0
            response.message = "dry-run: completed; nothing was executed"
            response.output = self._output(execution)
            if execution.config.logged:
                execution.log_lines.append("dry-run: finished with exit status 0")
        return execution

    @staticmethod
    def _output(execution: _Execution) -> str:
        if execution.config.output_format == CommandOutputFormat.JSON:
            payload: dict[str, Any] = {
                "dry_run": True,
                "host": execution.request.host,
                "arguments": execution.request.arguments,
            }
            return json.dumps(payload)
        return f"dry-run: {execution.request.host} | SUCCESS => pong (not executed)\n"
