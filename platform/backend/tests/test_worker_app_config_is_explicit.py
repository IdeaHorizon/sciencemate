"""A worker must not load its checkout's .env as App Server configuration."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
HARNESS_DOTENV = (
    "LLM_API_KEY=diagnostic-placeholder\n"
    "LLM_BASE_URL=http://127.0.0.1:1/v1\n"
    "LLM_MODEL=diagnostic-model\n"
    "STATE_DIR=./output\n"
)


def run_python(tmp_path, code, **overrides):
    env = {
        key: value for key, value in os.environ.items()
        if key in (
            "PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "WINDIR", "TEMP", "TMP",
            "TMPDIR", "LANG", "LC_ALL", "SSL_CERT_FILE",
        )
    }
    env.update(
        PYTHONPATH=os.pathsep.join((str(REPO), str(REPO / "platform/backend"))),
        PLATFORM_DATA_ROOT=str(tmp_path / "data"),
        **overrides,
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.mark.parametrize("profile", ["personal", "org"])
@pytest.mark.parametrize("dotenv", [HARNESS_DOTENV, "DEBUG=true\n"], ids=["harness", "app-fields"])
def test_worker_uses_delivered_config_even_with_a_checkout_dotenv(tmp_path, profile, dotenv):
    (tmp_path / ".env").write_text(dotenv, encoding="utf-8")
    # Even an inherited explicit App dotenv must not enter the worker.
    inherited = tmp_path / "server.env"
    inherited.write_text("DEBUG=true\n", encoding="utf-8")
    context = dict(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'delivered.db').as_posix()}",
        platform_data_root=str(tmp_path / "delivered-data"),
        runtime_tenant_id="delivered-tenant", profile=profile,
        secret_key="delivered-placeholder",
    )
    assert run_python(
        tmp_path,
        """
import json, os
from types import SimpleNamespace
from core import remote_resources
from platform_runtime import _apply_request_scope
context = json.loads(os.environ[remote_resources.DELIVERY_ENV])
before = dict(os.environ)
session = SimpleNamespace(state=SimpleNamespace(project_id='project'))
request = {'app_binding': {'user_id': 'user'}}
_apply_request_scope(session, request)
from app.config import settings
assert all(getattr(settings, key) == value for key, value in context.items())
assert settings.debug is False
assert remote_resources._handler is not None
assert dict(os.environ) == {
    key: value for key, value in before.items() if key != remote_resources.DELIVERY_ENV
}
# Subsequent submissions reuse the consumed delivery without reloading .env.
_apply_request_scope(session, request)
assert settings.debug is False
print('bound')
""",
        HARNESS_RESOURCE_RUNTIME=json.dumps(context),
        HARNESS_CONTROL_SOCKET=str(tmp_path / "worker.sock"),
        PLATFORM_ENV_FILE=str(inherited),
    ) == "bound"


def test_worker_restores_environment_after_a_failed_bootstrap_and_can_retry(tmp_path):
    (tmp_path / ".env").write_text(HARNESS_DOTENV, encoding="utf-8")
    context = dict(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'delivered.db').as_posix()}",
        platform_data_root=str(tmp_path / "delivered-data"),
        runtime_tenant_id="delivered-tenant", profile="invalid-profile",
        secret_key="delivered-placeholder",
    )
    assert run_python(
        tmp_path,
        """
import json, os
from pydantic import ValidationError
from core import remote_resources
before = dict(os.environ)
try:
    remote_resources.bind_worker(user_id='user', project_id='project')
except ValidationError as exc:
    assert [error['loc'] for error in exc.errors()] == [('PLATFORM_PROFILE',)]
else:
    raise AssertionError('invalid delivered config must still fail validation')
assert dict(os.environ) == {
    key: value for key, value in before.items() if key != remote_resources.DELIVERY_ENV
}
context = json.loads(before[remote_resources.DELIVERY_ENV])
context['profile'] = 'personal'
os.environ[remote_resources.DELIVERY_ENV] = json.dumps(context)
remote_resources.bind_worker(user_id='user', project_id='project')
assert remote_resources._handler is not None
assert 'PLATFORM_ENV_FILE' not in os.environ
print('retried')
""",
        HARNESS_RESOURCE_RUNTIME=json.dumps(context),
        HARNESS_CONTROL_SOCKET=str(tmp_path / "worker.sock"),
        DATABASE_URL="outer-process-placeholder",
    ) == "retried"


@pytest.mark.parametrize("explicit_file", [False, True])
def test_app_server_still_loads_and_validates_its_dotenv(tmp_path, explicit_file):
    dotenv = tmp_path / ("server.env" if explicit_file else ".env")
    dotenv.write_text("APP_NAME=Configured server\nDEBUG=true\n", encoding="utf-8")
    overrides = {"PLATFORM_ENV_FILE": str(dotenv)} if explicit_file else {}
    if explicit_file:
        (tmp_path / ".env").write_text(HARNESS_DOTENV, encoding="utf-8")
    assert run_python(
        tmp_path,
        """
from app.config import settings
assert settings.app_name == 'Configured server'
assert settings.debug is True
print('configured')
""",
        **overrides,
    ) == "configured"
    with dotenv.open("a", encoding="utf-8") as stream:
        stream.write("LLM_MODEL=unexpected-backend-setting\n")
    assert run_python(
        tmp_path,
        """
from pydantic import ValidationError
try:
    from app.config import settings
except ValidationError as exc:
    assert [(error['loc'], error['type']) for error in exc.errors()] == [
        (('llm_model',), 'extra_forbidden')
    ]
else:
    raise AssertionError('unknown App settings must still be rejected')
print('rejected')
""",
        **overrides,
    ) == "rejected"
