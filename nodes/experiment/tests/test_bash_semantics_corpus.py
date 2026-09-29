"""Broad, labelled corpus for experiment's Bash execution semantics."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from nodes.experiment.tools import timeout_escalation as te
from nodes.experiment.tools.bash_semantics import (
    EXTERNAL_CONTROL_EFFECT,
    EXTERNAL_CONTROL_NONE,
    EXTERNAL_CONTROL_QUERY,
    analyze_bash,
)


@dataclass(frozen=True)
class Case:
    family: str
    command: str
    background: bool = False
    srun: bool = False
    dynamic: bool = False
    parse_error: bool = False


CASES: list[Case] = []


def _add(family: str, commands: tuple[str, ...], **expected: bool) -> None:
    CASES.extend(Case(family, command, **expected) for command in commands)


_add("plain_text", (
    "echo sbatch", "echo 'sbatch job.sh'", 'printf "%s\\n" "qsub job.pbs"',
    "printf '%s' bsub", "echo salloc srun", "logger 'sbatch failed'",
    "cat README-sbatch.md", "head -n 2 sbatch.log", "tail -f qsub.output",
    "wc -l bsub.txt", "basename /tmp/srun", "dirname /tmp/salloc/job",
))
_add("search_patterns", (
    "grep -n sbatch file", "grep -E 'sbatch|qsub' file", "rg -n 'srun' .",
    "awk '/sbatch/{print}' file", "sed -n '/qsub/p' file",
    "perl -ne 'print if /bsub/' file", "find /tmp -name sbatch",
    "find . -type f -name '*qsub*' -print", "locate sbatch", "which sbatch",
    "whereis qsub", "type -a srun", "command -v sbatch", "command -V qsub",
    "compgen -c | grep sbatch",
))
_add("comments_assignments", (
    "# sbatch job.sh", "echo ok # qsub job.pbs", "name=sbatch",
    "cmd='sbatch job.sh'", "export SCHEDULER=slurm", "SBATCH_ARGS='--time=1'",
    "arr=(sbatch qsub bsub)", "readonly probe=srun", "declare -x label=sbatch",
    "unset sbatch",
))
_add("shell_data", (
    "[[ sbatch == sbatch ]]", "[[ 'qsub' =~ qsub ]]",
    "case x in sbatch) echo no;; esac",
    'for c in sbatch qsub bsub srun; do command -v "$c"; done',
    'for c in sbatch qsub; do echo "$c"; done',
    "select c in sbatch qsub; do break; done", "echo $((1 + 2)) # srun",
    "test sbatch = sbatch", "[ qsub = qsub ]", "true || echo sbatch",
))
_add("redirections_paths", (
    "echo ok > sbatch.log", "cat < qsub.txt", "echo ok 2> bsub.err",
    "echo ok > /tmp/srun", "exec 3>sbatch.out", "read x < salloc.input",
    "echo probe > /dev/null 2>&1", "printf x >> qsub.log", "touch sbatch",
    "mkdir -p /tmp/qsub/srun", "cp sbatch.txt qsub.txt", "mv bsub.txt salloc.txt",
))
_add("quoted_text", (
    r"echo s\batch", 'echo "sba""tch"', """echo 'q'"sub" """,
    "printf '%q' 'sbatch job.sh'", "echo '$(sbatch job.sh)'",
    "echo '$runner'", r"echo \$srun", "echo -- --wrap='sbatch job.sh'",
    "printf '%s\\n' '#!/bin/bash' 'sbatch job.sh'",
))
_add("heredoc_text", (
    "cat <<'EOF'\nsbatch job.sh\nEOF", "cat <<EOF\nqsub job.pbs\nEOF",
    "read -r x <<'TXT'\nbsub job.lsf\nTXT",
    "python - <<'PY'\nprint('srun app')\nPY",
    "cat <<< 'salloc --nodes=1'", "grep sbatch <<<'sbatch text'",
))
_add("query_modes", (
    "sbatch --help", "sbatch --version", "qsub --help", "bsub -h",
    "salloc --usage", "srun --help", "srun --version", "srun -h",
    "env sbatch --version", "command qsub --help", "/usr/bin/sbatch --version",
    "bash -n job.sh", "sh -n job.sh", "bash --help", "bash --version",
))
_add("foreground", (
    "pwd", "ls -la", "cd /tmp && touch marker && echo OK", "make -j4",
    "cmake -S . -B build", "python solver.py", "mpirun -np 4 ./solver",
    "curl -I https://example.invalid", "git status --short", "tar -tf source.tar",
    "sleep 0.1", "echo one | sed 's/one/two/'", "false || true",
    "true && echo done", "bash -c 'echo ok'", "eval 'echo harmless'",
    "printf '%s\\n' a b | xargs -n1 echo", "printf x | xargs echo",
))
_add("inert_functions", (
    "submit() { sbatch job.sh; }", "function launch { qsub job.pbs; }",
    "f() { srun hostname; }", "function g() { bsub < job.lsf; }",
))

_add("case_sensitive_and_wrapper_context", (
    "SBATCH job.sh", "SRUN hostname",
    "f() { sbatch job.sh; }; command f",
    "f() { qsub job.pbs; }; env f",
    "f() { srun hostname; }; exec f",
    "printf x | xargs command sbatch job.sh",
))

_add("scheduler_submit", (
    "sbatch job.sh", "/usr/bin/sbatch job.sh", "command sbatch job.sh",
    "env sbatch job.sh", "env A=1 sbatch --time=10 job.sh", "qsub job.pbs",
    "command qsub -q short job.pbs", "bsub < job.lsf",
    "bsub -q normal ./run.sh", "salloc --nodes=1",
    "env SLURM_HINT=nomultithread sbatch job.sh", "bash -c 'sbatch job.sh'",
    "bash -lc 'qsub job.pbs'", "sh -c 'bsub < job.lsf'",
    "submit() { sbatch job.sh; }; submit",
    "launch() { qsub job.pbs; }; launch",
), background=True)
_add("srun_execution", (
    "srun hostname", "/usr/bin/srun -n 4 ./solver",
    "command srun --mpi=pmix app", "env OMP_NUM_THREADS=2 srun -n 2 app",
    "bash -c 'srun hostname'", "launch() { srun hostname; }; launch",
), srun=True)
_add("background_operator", (
    "sleep 30 &", "echo work & wait", "(sleep 1) &", "{ sleep 1; } &",
    "cmd1 & cmd2", "python solver.py >run.log 2>&1 &", "make -j4 &",
    "sbatch --help &", "coproc sleep 1", "coproc WORKER { sleep 1; }",
), background=True)
_add("detached", (
    "nohup sleep 30", "nohup python solver.py >run.log 2>&1",
    "setsid ./solver", "setsid -f ./solver", "disown", "disown -h %1",
    "command nohup sleep 1", "env nohup sleep 1", "nohup sbatch job.sh",
    "setsid srun app",
), background=True)
_add("nested_execution", (
    "echo $(sbatch job.sh)", "x=$(qsub job.pbs)", "cat <(bsub < job.lsf)",
    "diff <(echo a) <(salloc --nodes=1)",
    "bash -c 'echo before; sbatch job.sh'",
    "command bash -c 'qsub job.pbs'", "env bash -lc 'bsub < job.lsf'",
    "eval 'sbatch job.sh'",
), background=True)
_add("xargs_scheduler", (
    "printf x | xargs srun hostname",
    "printf x | xargs -n1 srun hostname",
    "printf x | xargs env srun hostname",
), srun=True)
_add("find_execution", (
    "find . -name '*.sh' -exec sbatch {} ';'",
    "find . -name '*.pbs' -execdir qsub {} ';'",
), background=True)
_add("find_safe_execution", (
    "find . -type f -ok echo {} ';'",
    "find . -type f -exec printf '%s\\n' {} ';'",
))
_add("dynamic_source", (
    'eval "$payload"', "source setup.sh", ". ./env.sh", "$cmd --version",
    '"$runner" job.sh', 'bash -c "$payload"', 'sh -c "$cmd"',
), dynamic=True)
_add("external_shell_source", (
    "bash job.sh", "sh ./job.sh", "dash script", "env bash job.sh",
    "command sh ./job.sh", "printf 'sbatch job.sh\\n' | bash",
    "bash < job.sh", "sh -s < job.sh",
    "bash <<'EOF'\nsbatch job.sh\nEOF", "zsh ./submit.zsh",
), dynamic=True)
_add("syntax_error", (
    "if then", "for x in; do", "echo $(", "case x in", "{ echo ok;",
    "foo | | bar", "echo 'unterminated", "[[ x ==", "while; do echo; done",
    "function { echo x; }",
), parse_error=True)

_add("static_dataflow_safe", (
    'c=gcc; "$c" --version',
    'c=/usr/bin; "$c/gcc" --version',
    'for c in gcc gfortran clang; do "$c" --version; done',
    'for c in gcc gfortran srun; do command -v "$c"; done',
    'for c in gcc gfortran srun; do "$c" --version; done',
    'payload="echo ok"; bash -c "$payload"',
    'probe() { "$1" --version; }; probe gcc',
))
_add("static_dataflow_scheduler", (
    'c=sbatch; "$c" job.sh',
    'for c in gcc sbatch; do "$c" job.sh; done',
    'payload="sbatch job.sh"; bash -c "$payload"',
), background=True)
_add("static_dataflow_srun", (
    'c=srun; "$c" hostname',
    'for c in gcc srun; do "$c" hostname; done',
    'launch() { "$1" hostname; }; launch srun',
), srun=True)
_add("runtime_dataflow_unknown", (
    'for c in "$@"; do "$c" --version; done',
    'for c in $(cat tools.txt); do "$c" --version; done',
    'c=gcc; read c; "$c" --version',
    'c=gcc; c=$(cat tool.txt); "$c" --version',
    'payload=$(cat payload.sh); bash -c "$payload"',
    'launch() { "$@"; }; launch gcc --version',
), dynamic=True)
_add("control_flow_scheduler", (
    'c=sbatch; false && c=gcc; "$c" job.sh',
    'c=sbatch; while false; do c=gcc; done; "$c" job.sh',
    'c=sbatch; until true; do c=gcc; done; "$c" job.sh',
    'c=sbatch; case "$mode" in x) c=gcc;; y) c=clang;; esac; "$c" job.sh',
    'c=sbatch; printf x | c=gcc; "$c" job.sh',
), background=True)
_add("control_flow_srun", (
    'c=srun; true || c=gcc; "$c" hostname',
    'x=gcc; for c in a b; do "$x" hostname; x=srun; done',
    'x=gcc; n=0; while [ "$n" -lt 2 ]; do "$x" hostname; x=srun; n=$((n+1)); done',
    'x=gcc; case "$mode" in x) x=srun;; y) x=clang;; esac; "$x" hostname',
    'x=srun; printf x | x=gcc; "$x" hostname',
), srun=True)
_add("control_flow_function_scheduler", (
    'f() { sbatch job.sh; }; false && f() { echo safe; }; f',
    'if test -n "$mode"; then f() { sbatch job.sh; }; else f() { echo safe; }; fi; f',
    'f() { sbatch job.sh; }; while false; do f() { echo safe; }; done; f',
    'f() { sbatch job.sh; }; printf x | f() { echo safe; }; f',
), background=True)
_add("control_flow_function_srun", (
    'case "$mode" in x) f() { srun hostname; };; y) f() { echo safe; };; esac; f',
), srun=True, dynamic=True)

assert len(CASES) >= 150


@pytest.mark.parametrize(
    "case",
    CASES,
    ids=lambda case: f"{case.family}:{case.command[:48]}",
)
def test_bash_execution_semantics_corpus(case: Case):
    analysis = analyze_bash(case.command)
    assert te.looks_backgrounded(case.command) is case.background
    assert te.classify_bash_execution(case.command).known_srun_launch is case.srun
    assert analysis.dynamic_execution is case.dynamic
    assert analysis.parse_error is case.parse_error


@pytest.mark.parametrize("command", (
    "kubectl run probe --image=busybox",
    "kubectl apply -f deployment.yaml",
    "kubectl delete pod probe",
    "kubectl exec pod -- solver",
    "kubectl attach pod -c solver",
    "kubectl port-forward pod/solver 8080:80",
    "kubectl proxy",
    "kubectl auth reconcile -f role.yaml",
    "kubectl config set-context science --namespace=science",
    "kubectl config use-context science",
    "docker run --rm image",
    "docker build .",
    "docker exec container solver",
    "podman create image",
    "systemd-run --user solver",
    "tmux new-session -d -s solver ./solver",
    "screen -dmS solver ./solver",
    "at now + 1 minute",
    "batch",
    "apptainer instance start image.sif solver",
    "singularity instance.stop solver",
    "command env kubectl create job solver --image=image",
    "ssh login-node hostname",
    "ssh -f login-node ./solver",
    "env ssh login-node sbatch job.slurm",
    "pdsh -w node01,node02 hostname",
    "ssh --help",
    "pdsh --version",
))
def test_external_control_plane_launches_use_managed_background_semantics(command):
    decision = te.classify_bash_execution(command)
    assert decision.known_background_launch, command
    assert not decision.unverifiable_execution, command


@pytest.mark.parametrize("command", (
    "kubectl get pods",
    "kubectl -n science get pod solver",
    "kubectl --context cluster version --client",
    "kubectl describe pod solver",
    "kubectl logs pod/solver",
    "kubectl top pods",
    "kubectl api-resources",
    "kubectl api-versions",
    "kubectl explain pods.spec",
    "kubectl cluster-info",
    "kubectl auth can-i create pods",
    "kubectl config view",
    "kubectl config current-context",
    "kubectl config get-contexts",
    "ssh -V",
    "pdsh -V",
    "docker ps",
    "docker --context remote image inspect solver",
    "podman info",
    "podman container logs solver",
    "systemd-run --help",
    "tmux -V",
    "tmux list-sessions",
    "screen -ls",
    "at -l",
    "at -c 42",
    "batch --help",
    "apptainer instance list",
    "singularity instance stats solver",
    "apptainer exec image.sif true",
    "command -v docker podman kubectl",
    "command -v ssh pdsh",
))
def test_external_control_plane_read_only_queries_remain_foreground(command):
    decision = te.classify_bash_execution(command)
    assert not decision.known_background_launch, command
    assert not decision.unverifiable_execution, command


@pytest.mark.parametrize("command", (
    "make -i",
    "make -ki all",
    "gmake --ignore-errors",
    "MAKEFLAGS=-i make all",
    "MFLAGS=ik gmake all",
    "env MAKEFLAGS=--ignore-errors make",
    "command env MFLAGS=-i gmake",
    "export MAKEFLAGS=-i; make",
))
def test_make_ignore_errors_is_mechanically_marked(command):
    analysis = analyze_bash(command)
    decision = te.classify_bash_execution(command)

    assert analysis.ignore_errors_build, command
    assert decision.unverifiable_execution, command
    assert decision.uncertainty_kind == "ignore_errors_build", command
    assert not decision.known_background_launch, command


@pytest.mark.parametrize("command", (
    "make -k",
    "make -j4",
    "make -f buildi.mk",
    "MAKEFLAGS=-k make",
    "MAKEFLAGS=-i true; make -k",
    "MAKEFLAGS=-i ./configure; make -k",
    "./compile em_real -j 2",
    "echo 'make -i'",
    "printf '%s\\n' 'MAKEFLAGS=-i make'",
))
def test_normal_build_and_make_text_do_not_trigger_ignore_errors(command):
    analysis = analyze_bash(command)
    decision = te.classify_bash_execution(command)

    assert not analysis.ignore_errors_build, command
    assert decision.uncertainty_kind != "ignore_errors_build", command


@pytest.mark.parametrize(("command", "expected"), (
    ("echo kubectl get pods", EXTERNAL_CONTROL_NONE),
    ("apptainer exec image.sif true", EXTERNAL_CONTROL_NONE),
    ("kubectl get pods", EXTERNAL_CONTROL_QUERY),
    ("kubectl auth can-i create pods", EXTERNAL_CONTROL_QUERY),
    ("kubectl config current-context", EXTERNAL_CONTROL_QUERY),
    ("ssh -V", EXTERNAL_CONTROL_QUERY),
    ("command env kubectl get pods", EXTERNAL_CONTROL_QUERY),
    ("kubectl exec pod -- true", EXTERNAL_CONTROL_EFFECT),
    ("kubectl proxy", EXTERNAL_CONTROL_EFFECT),
    ("ssh login-node true", EXTERNAL_CONTROL_EFFECT),
))
def test_external_control_tri_state_is_recorded_once(command, expected):
    analysis = analyze_bash(command)

    assert analysis.external_control == expected
