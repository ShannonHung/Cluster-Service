"""
tests/unit/test_dry_run_deploy_clients.py

T9: the dry-run deploy-service clients, tested directly.

Two kinds of assertion live here. The interface-parity tests are the contract:
the fakes are deliberately *not* subclasses of the real clients (a forgotten
override would silently fall through to real HTTP), so nothing but these tests
stops the two surfaces drifting apart. The behaviour tests pin down the error
paths, because those are what keep ``DeployServiceError``'s code / status
mapping reachable in dry-run rather than dead code.

Design shared with deploy-service; see that repo's docs/arch/dry-run-mode.md.
"""

from __future__ import annotations

import inspect

import pytest

from app.clients.command_service_client import CommandServiceClient
from app.clients.deploy_service_client import DeployServiceClient
from app.clients.dry_run_command_service_client import (
    DRY_RUN_FACTS,
    DRY_RUN_PING,
    DryRunCommandServiceClient,
    reset_dry_run_command_service,
)
from app.clients.dry_run_deploy_service_client import (
    DryRunDeployServiceClient,
    reset_dry_run_deploy_service,
)
from app.core.exceptions import DeployServiceError, ErrorCode
from app.domain.command_models import (
    CommandExecutionRequest,
    HostType,
    OutputFormat,
    OutputJsonError,
)
from app.domain.pipeline_models import PipelineVariable


@pytest.fixture(autouse=True)
def _clean_state():
    reset_dry_run_deploy_service()
    reset_dry_run_command_service()
    yield
    reset_dry_run_deploy_service()
    reset_dry_run_command_service()


def _public_methods(cls) -> dict[str, inspect.Signature]:
    return {
        name: inspect.signature(fn)
        for name, fn in inspect.getmembers(cls, inspect.isfunction)
        if not name.startswith("_")
    }


def _code(exc: DeployServiceError) -> ErrorCode:
    return exc.to_response()["error_code"]


# ── interface parity ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "real, fake",
    [
        (DeployServiceClient, DryRunDeployServiceClient),
        (CommandServiceClient, DryRunCommandServiceClient),
    ],
)
def test_fake_exposes_exactly_the_real_public_surface(real, fake):
    """Same method names, same parameters, same defaults, same return types.

    Compared as signature objects, so a renamed keyword or a changed default
    fails here rather than as a TypeError in a dry-run e2e job. A method added
    to the real client without a fake counterpart fails too — which is the
    point: the new method must be stubbed before dry-run can claim to cover it.
    """
    assert _public_methods(fake) == _public_methods(real)


@pytest.mark.parametrize(
    "real, fake",
    [
        (DeployServiceClient, DryRunDeployServiceClient),
        (CommandServiceClient, DryRunCommandServiceClient),
    ],
)
def test_fake_is_not_a_subclass_of_the_real_client(real, fake):
    """Inheritance would make any un-overridden method a live HTTP call."""
    assert not issubclass(fake, real)


@pytest.mark.parametrize("fake", [DryRunDeployServiceClient, DryRunCommandServiceClient])
def test_public_methods_are_coroutines(fake):
    """The services ``await`` every call; a sync method would return the
    model itself and the await would raise."""
    for name in _public_methods(fake):
        assert inspect.iscoroutinefunction(getattr(fake, name)), name


# ── pipelines ─────────────────────────────────────────────────────────────────


async def test_trigger_then_poll_reaches_a_terminal_state():
    client = DryRunDeployServiceClient()
    created = await client.trigger_pipeline("deploy", "main", [])
    assert created.status == "running"

    polled = await client.get_pipeline(created.id)
    assert polled.status == "success"
    assert polled.finished_at is not None


async def test_identifiers_are_obviously_synthetic():
    created = await DryRunDeployServiceClient().trigger_pipeline("deploy", "main", [])
    assert created.id > 9_000_000_000
    assert ".invalid" in created.web_url
    assert "dry-run" in created.tag_list


async def test_action_is_forwarded_as_the_execution_variable():
    created = await DryRunDeployServiceClient().trigger_pipeline(
        "deploy", "main", [PipelineVariable(key="A", value="1")]
    )
    assert {(v.key, v.value) for v in created.variables} == {
        ("EXECUTION", "deploy"), ("A", "1"),
    }


async def test_duplicate_running_pipeline_maps_to_conflict():
    """Goes through the real adapter's _DEPLOY_CODE_MAP."""
    client = DryRunDeployServiceClient()
    first = await client.trigger_pipeline("deploy", "main", [])

    with pytest.raises(DeployServiceError) as info:
        await client.trigger_pipeline("deploy", "main", [])

    assert _code(info.value) == ErrorCode.PIPELINE_CONFLICT
    assert info.value.upstream_status == 409
    assert info.value.to_response()["details"] == {"pipeline_ids": [first.id]}


async def test_different_variables_are_not_a_duplicate():
    client = DryRunDeployServiceClient()
    await client.trigger_pipeline("deploy", "main", [])
    await client.trigger_pipeline("deploy", "main", [PipelineVariable(key="A", value="1")])


async def test_check_running_reports_matches_only():
    client = DryRunDeployServiceClient()
    await client.trigger_pipeline("deploy", "main", [])

    hit = await client.check_running("deploy", "main", [])
    miss = await client.check_running("deploy", "other-branch", [])

    assert (hit.has_running, hit.count) == (True, 1)
    assert (miss.has_running, miss.count) == (False, 0)


async def test_unknown_pipeline_maps_to_not_found():
    with pytest.raises(DeployServiceError) as info:
        await DryRunDeployServiceClient().get_pipeline(1)
    assert _code(info.value) == ErrorCode.PIPELINE_NOT_FOUND


async def test_cancel_then_retry():
    client = DryRunDeployServiceClient()
    created = await client.trigger_pipeline("deploy", "main", [])

    cancelled = await client.cancel_pipeline(created.id)
    assert cancelled.status == "canceled"
    assert (await client.get_pipeline(created.id)).status == "canceled"

    retried = await client.retry_pipeline(created.id)
    assert retried.status == "running"
    assert retried.finished_at is None


async def test_returned_models_are_copies():
    """A caller mutating a response must not rewrite the stored pipeline."""
    client = DryRunDeployServiceClient()
    created = await client.trigger_pipeline("deploy", "main", [])
    created.status = "tampered"
    assert (await client.cancel_pipeline(created.id)).status == "canceled"


async def test_reset_clears_pipelines():
    client = DryRunDeployServiceClient()
    created = await client.trigger_pipeline("deploy", "main", [])
    reset_dry_run_deploy_service()
    with pytest.raises(DeployServiceError):
        await client.get_pipeline(created.id)


# ── inventory ─────────────────────────────────────────────────────────────────


async def test_inventory_answers_for_the_kube_fake_node_names():
    from app.services.dry_run_kube_client import DRY_RUN_NODES

    for name in DRY_RUN_NODES:
        info = await DryRunDeployServiceClient().get_node(name)
        assert info.node.name == name


async def test_inventory_unknown_node_is_an_upstream_404():
    """InventoryProxyService turns exactly this into a 404 for the caller."""
    with pytest.raises(DeployServiceError) as info:
        await DryRunDeployServiceClient().get_node("no-such-node")
    assert info.value.upstream_status == 404


async def test_node_bastion_source_reflects_the_override():
    client = DryRunDeployServiceClient()
    default = await client.resolve_node_bastion("dry-run-node-ready")
    explicit = await client.resolve_node_bastion("dry-run-node-ready", bastion_type="dry-run")
    assert default.bastion_type_source == "config"
    assert explicit.bastion_type_source == "query_param"


# ── commands ──────────────────────────────────────────────────────────────────


def _request(command: str = DRY_RUN_PING, **kwargs) -> CommandExecutionRequest:
    return CommandExecutionRequest(command_name=command, host="10.0.0.1", username="u", **kwargs)


async def test_whitelist_is_the_cluster_proxy_identity():
    whitelist = await DryRunCommandServiceClient().get_all_commands_info()
    assert whitelist.name == "cluster_proxy"
    assert {c.command_name for c in whitelist.allow_commands} == {DRY_RUN_PING, DRY_RUN_FACTS}


async def test_unknown_command_info_is_not_found():
    with pytest.raises(DeployServiceError) as info:
        await DryRunCommandServiceClient().get_command_info("rm_rf")
    assert info.value.upstream_status == 404


async def test_non_whitelisted_execution_maps_to_forbidden():
    with pytest.raises(DeployServiceError) as info:
        await DryRunCommandServiceClient().execute_command(_request("rm_rf"))
    assert _code(info.value) == ErrorCode.DEPLOY_SERVICE_FORBIDDEN


async def test_execute_then_poll_reaches_success():
    client = DryRunCommandServiceClient()
    started = await client.execute_command(_request())
    assert started.status == "running"
    assert started.command_id.startswith("dry-run-")

    done = await client.get_command_result(started.command_id)
    assert (done.status, done.exit_status) == ("success", 0)
    assert done.output


async def test_non_ip_host_resolves_to_test_net():
    started = await DryRunCommandServiceClient().execute_command(
        _request(host_type=HostType.HOSTNAME)
    )
    assert started.resolved_ip.startswith("192.0.2.")


async def test_json_format_parses_a_json_command():
    client = DryRunCommandServiceClient()
    started = await client.execute_command(_request(DRY_RUN_FACTS, arguments={"filter": "x"}))
    done = await client.get_command_result(started.command_id, OutputFormat.JSON)
    assert done.output_json == {"dry_run": True, "host": "10.0.0.1", "arguments": {"filter": "x"}}
    assert isinstance(done.output, str), "output keeps its raw string value"


async def test_json_format_is_refused_for_a_text_command():
    client = DryRunCommandServiceClient()
    started = await client.execute_command(_request())
    with pytest.raises(DeployServiceError) as info:
        await client.get_command_result(started.command_id, OutputFormat.JSON)
    assert info.value.upstream_status == 400


async def test_json_format_on_a_killed_command_explains_the_null():
    """Kill is refused for the json command, so reach a non-success state via
    the store: the not_applicable branch must still be reachable."""
    client = DryRunCommandServiceClient()
    started = await client.execute_command(_request(DRY_RUN_FACTS))
    from app.clients import dry_run_command_service_client as mod

    mod._EXECUTIONS[started.command_id].response.status = "killed"
    done = await client.get_command_result(started.command_id, OutputFormat.JSON)
    assert done.output_json is None
    assert done.output_json_error == OutputJsonError.NOT_APPLICABLE


async def test_kill_running_command():
    client = DryRunCommandServiceClient()
    started = await client.execute_command(_request())
    killed = await client.kill_command(started.command_id, force=True)
    assert killed.status == "killed"
    assert (await client.get_command_result(started.command_id)).status == "killed"


async def test_kill_refused_for_non_killable_command():
    client = DryRunCommandServiceClient()
    started = await client.execute_command(_request(DRY_RUN_FACTS))
    with pytest.raises(DeployServiceError):
        await client.kill_command(started.command_id)


async def test_unknown_execution_is_not_found():
    for call in (
        DryRunCommandServiceClient().get_command_result("dry-run-nope"),
        DryRunCommandServiceClient().kill_command("dry-run-nope"),
        DryRunCommandServiceClient().get_command_trace("dry-run-nope"),
    ):
        with pytest.raises(DeployServiceError) as info:
            await call
        assert info.value.upstream_status == 404


async def test_trace_is_incremental():
    """Polling with the returned cursor must never repeat a line — that is how
    the HTML viewer consumes it."""
    client = DryRunCommandServiceClient()
    started = await client.execute_command(_request())

    first = await client.get_command_trace(started.command_id)
    assert first.lines and first.status == "success"
    assert [line.num for line in first.lines] == list(range(1, len(first.lines) + 1))

    second = await client.get_command_trace(
        started.command_id, first.next_byte_offset, first.next_line_num
    )
    assert second.lines == []
    assert second.next_byte_offset == first.next_byte_offset


async def test_trace_escapes_html():
    client = DryRunCommandServiceClient()
    started = await client.execute_command(
        CommandExecutionRequest(command_name=DRY_RUN_PING, host="<b>x</b>", username="u")
    )
    trace = await client.get_command_trace(started.command_id)
    assert all("<b>" not in line.content_html for line in trace.lines)


async def test_trace_for_unlogged_command_says_so():
    client = DryRunCommandServiceClient()
    started = await client.execute_command(_request(DRY_RUN_FACTS))
    trace = await client.get_command_trace(started.command_id)
    assert trace.not_logged is True
    assert trace.status == "success", "the viewer must still see a terminal status"
