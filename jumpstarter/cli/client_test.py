import pytest
from asyncclick.testing import CliRunner

from jumpstarter.config import ClientConfigV1Alpha1, UserConfigV1Alpha1

from .client import client


@pytest.fixture
def tmp_config_path(tmp_path, monkeypatch):
    monkeypatch.setattr(UserConfigV1Alpha1, "USER_CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(ClientConfigV1Alpha1, "CLIENT_CONFIGS_PATH", tmp_path / "clients")


@pytest.mark.anyio
async def test_client(tmp_config_path):
    runner = CliRunner()

    # create client non-interactively
    result = await runner.invoke(
        client, ["create", "test1", "--endpoint", "example.com:443", "--token", "dummy", "--allow", "jumpstarter.*"]
    )
    assert result.exit_code == 0

    # create duplicate client
    result = await runner.invoke(
        client, ["create", "test1", "--endpoint", "example.com:443", "--token", "dummy", "--allow", "jumpstarter.*"]
    )
    assert result.exit_code != 0

    # create client interactively
    result = await runner.invoke(
        client,
        ["create", "test2"],
        input="example.org:443\ndummytoken\njumpstarter.*,com.example.*\n",
    )
    assert result.exit_code == 0

    # list clients
    result = await runner.invoke(client, ["list"])
    assert result.exit_code == 0
    assert "*         test1   example.com:443" in result.output
    assert "          test2   example.org:443" in result.output

    # set default client
    result = await runner.invoke(client, ["use", "test2"])
    assert result.exit_code == 0

    # list clients
    result = await runner.invoke(client, ["list"])
    assert result.exit_code == 0
    assert "          test1   example.com:443" in result.output
    assert "*         test2   example.org:443" in result.output

    # delete default client
    result = await runner.invoke(client, ["delete", "test2"])
    assert result.exit_code == 0

    # list clients
    result = await runner.invoke(client, ["list"])
    assert result.exit_code == 0
    assert "*         test1   example.com:443" in result.output
    assert "*         test2   example.org:443" not in result.output


@pytest.fixture
def anyio_backend():
    return "asyncio"
