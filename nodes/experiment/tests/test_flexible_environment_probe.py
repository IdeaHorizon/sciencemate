"""Selected shell environments stay run-local and execute only through Core."""
import asyncio
import hashlib
import os
from pathlib import Path
import shlex

import pytest

from core.state import State
from nodes.experiment.tools import env_provision as ep, repro_snapshot as rs
from nodes.experiment.tools import probe_toolchain as pt


@pytest.fixture(autouse=True)
def isolated_home(tmp_path,monkeypatch):
    monkeypatch.setenv('HARNESS_FRAMEWORK_HOME',str(tmp_path/'framework-home'))


@pytest.fixture
def native_backend():
    from core import sandbox
    available, reason = sandbox.availability()
    if not available:
        pytest.skip(f"native write-boundary backend unavailable: {reason}")


def state_and_script(tmp_path,content):
    state=State.new('experiment',tmp_path/'runs')
    path=ep.env_path_for_state(state)
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(content,encoding='utf-8',newline='\n')
    return state,path


def test_absent_environment_probe_does_not_create_or_register_script(tmp_path,monkeypatch):
    state=State.new('experiment',tmp_path/'runs')
    calls=[]
    monkeypatch.setattr(pt,'collect_platform_profile',lambda **kw: calls.append(kw) or {'toolchain':{}})
    result=asyncio.run(pt._probe_toolchain(state))
    assert result['status']=='success',result
    assert not ep.env_path_for_state(state).exists()
    assert not state.list_artifacts('build_env')
    assert calls==[{'env_path':None,'state':state}]
    assert result['env_path'] is None and result['build_env_sha256'] is None


def test_legacy_global_selector_cannot_execute_another_runs_script(tmp_path,monkeypatch):
    sentinel=tmp_path/'unexpected-write'
    script=tmp_path/'foreign-env.sh'
    script.write_text('printf bad > '+shlex.quote(sentinel.as_posix())+'\n',encoding='utf-8')
    monkeypatch.setenv('EXPERIMENT_ACTIVE_BUILD_ENV',str(script))
    assert rs._run("printf HOST")=='HOST'
    assert not sentinel.exists()


def test_selected_profile_without_state_does_not_fall_back_to_host(tmp_path,monkeypatch):
    script=tmp_path/'env.sh';script.write_text('export FC=selected\n')
    calls=[]
    def forbidden(*a,**kw):
        calls.append(a)
        raise AssertionError('selected env must not use a host subprocess')
    monkeypatch.setattr(rs.subprocess,'run',forbidden)
    with pytest.raises(RuntimeError,match='requires.*State'):
        rs.collect_platform_profile(env_path=str(script))
    assert not calls


def test_existing_script_metadata_identifies_its_actual_bytes_without_registration(tmp_path):
    state,script=state_and_script(tmp_path,'export FC=selected\n')
    raw=b'export FC=selected\r\n';script.write_bytes(raw)
    info=ep.existing_env_script(state)
    assert info['sha256']==hashlib.sha256(raw).hexdigest()
    assert info['size_bytes']==len(raw) and info['created'] is False
    assert not state.list_artifacts('build_env')


def test_failed_setup_cannot_run_the_wrapped_payload(tmp_path):
    from shared.lib.shell import bash_shell
    import subprocess
    script=tmp_path/'fail.sh';script.write_text('return 17\n')
    result=subprocess.run([bash_shell(),'-c',ep.wrap_command('printf PAYLOAD',str(script))],
                          capture_output=True,text=True,timeout=10)
    assert result.returncode==17,result
    assert 'PAYLOAD' not in result.stdout


def test_profile_context_does_not_mutate_the_process_environment(tmp_path,monkeypatch):
    state,script=state_and_script(tmp_path,'export FC=selected\n')
    monkeypatch.setenv('EXPERIMENT_ACTIVE_BUILD_ENV','foreign-selector')
    seen=[]
    def controlled(cmd,*,state,env_path,timeout):
        seen.append((state,env_path))
        if cmd.startswith("printf '%s\\0'"):
            return '\0'.join('selected' if k=='FC' else '' for k in rs._PROBE_ENV_VARS)+'\0'
        return ''
    monkeypatch.setattr(rs,'_run_under_env',controlled)
    profile=rs.collect_platform_profile(env_path=str(script),state=state)
    assert profile['env_vars']['FC']=='selected'
    assert seen and all(s is state and p==str(script) for s,p in seen)
    assert os.environ['EXPERIMENT_ACTIVE_BUILD_ENV']=='foreign-selector'
    assert rs._PROFILE_ENV.get() is None


def test_core_refusal_is_not_reported_as_a_successful_profile(tmp_path,monkeypatch):
    from core import sandbox
    state,script=state_and_script(tmp_path,'export FC=selected\n')
    def refuse(*a,**kw):raise RuntimeError('fixture Core unavailable')
    monkeypatch.setattr(sandbox,'prepare_attempt_command',refuse)
    result=asyncio.run(pt._probe_toolchain(state))
    assert result['status']=='error' and 'Core unavailable' in result['error'],result
    assert not state.list_artifacts('platform_profile')


@pytest.mark.usefixtures("native_backend")
def test_real_core_probe_sources_selected_env_and_does_not_leak_to_host(tmp_path,monkeypatch):
    state,script=state_and_script(tmp_path,'export FC=selected-compiler\n')
    monkeypatch.setenv('FC','host-compiler')
    result=rs._run('printf "%s" "$FC"',env_path=str(script),state=state)
    assert result=='selected-compiler'
    assert rs._run('printf "%s" "$FC"')=='host-compiler'
    assert os.environ['FC']=='host-compiler'


@pytest.mark.usefixtures("native_backend")
def test_real_core_probe_refuses_setup_write_to_the_readonly_deliverable(tmp_path):
    state,script=state_and_script(tmp_path,'# replaced below\n')
    target=script.parent/'protected.dat';target.write_bytes(b'original')
    script.write_text('printf tampered > '+shlex.quote(target.as_posix())+'\n')
    with pytest.raises(RuntimeError,match='setup did not reach probe'):
        rs._run('printf PAYLOAD',env_path=str(script),state=state)
    assert target.read_bytes()==b'original'


def test_probe_rejects_unknown_input_before_environment_discovery(tmp_path,monkeypatch):
    state=State.new('experiment',tmp_path/'runs')
    result=asyncio.run(pt._probe_toolchain(state,command='printf unexpected'))
    assert result['status']=='error' and 'accepts no input' in result['error']
    assert not ep.env_path_for_state(state).exists()


@pytest.mark.usefixtures("native_backend")
def test_real_profile_observes_selected_tool_and_variables_under_the_same_script(tmp_path,monkeypatch):
    state,script=state_and_script(tmp_path,'# setup below\n')
    tools=script.parent/'toolchain';tools.mkdir()
    compiler=tools/'hf-env-cc'
    compiler.write_text('#!/usr/bin/env bash\nprintf "selected compiler 1.0\\n"\n',encoding='utf-8')
    compiler.chmod(0o755)
    script.write_text('printf "setup chatter\\n"\nexport FC=hf-env-cc\n'
                      'export PATH="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/toolchain:$PATH"\n',
                      encoding='utf-8')
    monkeypatch.setattr(rs,'_PROBE_TOOLS',{'compiler':['hf-env-cc']})
    profile=rs.collect_platform_profile(env_path=str(script),state=state)
    assert profile['toolchain']['hf-env-cc']['path'].endswith('/hf-env-cc')
    assert profile['toolchain']['hf-env-cc']['version']=='selected compiler 1.0'
    assert profile['env_vars']['FC']=='hf-env-cc'
    assert profile['env_provision']['env']['sha256']==hashlib.sha256(script.read_bytes()).hexdigest()
    assert profile['generated_by']=='framework_probe_under_env'


@pytest.mark.usefixtures("native_backend")
def test_real_core_missing_tool_is_unknown_not_a_setup_failure(tmp_path):
    state,script=state_and_script(tmp_path,'export FC=selected\n')
    assert rs._run('command -v hf_tool_absent_in_this_fixture_34239',env_path=str(script),state=state)==''


@pytest.mark.usefixtures("native_backend")
def test_real_core_setup_early_exit_zero_does_not_claim_verified_environment(tmp_path):
    state,script=state_and_script(tmp_path,'exit 0\n')
    with pytest.raises(RuntimeError,match='setup did not reach probe'):
        rs._run('printf PAYLOAD',env_path=str(script),state=state)


def test_missing_selected_script_does_not_silently_use_host_environment(tmp_path):
    state,script=state_and_script(tmp_path,'export FC=selected\n')
    script.unlink()
    with pytest.raises(RuntimeError,match='probe unavailable'):
        rs._run('printf HOST',env_path=str(script),state=state)


def test_selected_environment_directory_is_an_error_not_absence(tmp_path):
    state=State.new('experiment',tmp_path/'runs')
    script=ep.env_path_for_state(state);script.mkdir(parents=True)
    result=asyncio.run(pt._probe_toolchain(state))
    assert result['status']=='error' and 'not a regular file' in result['error'],result
    assert not state.list_artifacts('platform_profile')


@pytest.mark.parametrize('operation',['stat','read_bytes'])
def test_unreadable_selected_environment_is_an_error_not_absence(tmp_path,monkeypatch,operation):
    state,script=state_and_script(tmp_path,'export FC=selected\n')
    original=getattr(Path,operation)
    def denied(path,*a,**kw):
        if path==script:raise PermissionError('fixture selected environment denied')
        return original(path,*a,**kw)
    monkeypatch.setattr(Path,operation,denied)
    result=asyncio.run(pt._probe_toolchain(state))
    assert result['status']=='error' and 'PermissionError' in result['error'],result
    assert not state.list_artifacts('platform_profile')



def test_script_path_mention_and_an_unrelated_source_do_not_bypass_selected_environment(tmp_path,monkeypatch):
    from shared.lib.shell import bash_shell
    import subprocess
    state,script=state_and_script(tmp_path,'export FC=selected-compiler\n')
    unrelated=script.parent/'other.sh';unrelated.write_text('# intentionally empty\n')
    monkeypatch.setenv('FC','host-compiler')
    command=('printf "%s\\n" '+shlex.quote(str(script))+'; . '+
             shlex.quote(unrelated.as_posix())+'; printf "%s" "$FC"')
    done=subprocess.run([bash_shell(),'-c',ep.wrap_command(command,str(script))],
                        capture_output=True,text=True,timeout=10)
    assert done.returncode==0,done
    assert done.stdout.splitlines()[-1]=='selected-compiler'


def test_disappearing_selected_script_is_an_error_not_an_empty_environment(tmp_path,monkeypatch):
    state,script=state_and_script(tmp_path,'export FC=selected\n')
    original=ep.env_info
    def remove_before_read(path):
        script.unlink()
        return original(path)
    monkeypatch.setattr(ep,'env_info',remove_before_read)
    with pytest.raises(FileNotFoundError):
        ep.existing_env_script(state)



@pytest.mark.usefixtures("native_backend")
def test_excessive_setup_output_is_rejected_without_a_profile(tmp_path,monkeypatch):
    import subprocess
    from types import SimpleNamespace
    calls=[]
    def file_capture(*args,**kwargs):
        assert not kwargs.get("capture_output"), "untrusted setup must not be buffered in memory"
        assert hasattr(kwargs.get("stdout"), "fileno")
        assert hasattr(kwargs.get("stderr"), "fileno")
        calls.append(True)
        return subprocess.run(*args,**kwargs)
    # Replace only this module's reference. Core and the actual subprocess run
    # stay real, while an accidental switch back to unbounded capture fails.
    monkeypatch.setattr(rs,"subprocess",SimpleNamespace(run=file_capture,
        DEVNULL=subprocess.DEVNULL,TimeoutExpired=subprocess.TimeoutExpired))
    state,script=state_and_script(tmp_path,
        'for ((i=0;i<2050;i++)); do printf "%01024d" 0; done\nprintf "\\n"\nexport FC=selected\n')
    with pytest.raises(RuntimeError,match='output.*limit'):
        rs._run('printf PAYLOAD',env_path=str(script),state=state)
    assert not state.list_artifacts('platform_profile')
    assert calls
