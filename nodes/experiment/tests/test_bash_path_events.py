"""结构化 Bash 路径事件与 cwd 作用域回归。"""
from __future__ import annotations

from nodes.experiment.tools.bash_semantics import analyze_bash

ROOT = "/workspace/root"
SAFE = "/workspace/safe"


def _commands(command: str, **kwargs):
    analysis = analyze_bash(command, initial_cwd=ROOT, **kwargs)
    return analysis, [
        event for event in analysis.static_path_events
        if event.kind == "command"
    ]


def _heads(command: str, head: str, **kwargs):
    analysis, events = _commands(command, **kwargs)
    return analysis, [event for event in events if event.head == head]


def test_pipeline_enumerates_tee_without_leaking_left_cd():
    _, tee = _heads("cd /workspace/safe | tee baseline/poison", "tee")
    assert len(tee) == 1
    assert tee[0].args == ("baseline/poison",)
    assert tee[0].cwd.values == (ROOT,)
    assert tee[0].context[-1] == "pipeline:1"


def test_subshell_cwd_does_not_escape():
    _, touches = _heads(
        "(cd /workspace/safe && touch inside); touch outside", "touch")
    assert touches[0].cwd.values == (SAFE,)
    assert touches[1].cwd.values == (ROOT,)


def test_literal_shell_c_cwd_does_not_escape():
    _, touches = _heads(
        "bash -c 'cd /workspace/safe && touch inside'; touch outside",
        "touch",
    )
    assert touches[0].cwd.values == (SAFE,)
    assert "shell-c:bash" in touches[0].context
    assert touches[1].cwd.values == (ROOT,)


def test_function_call_is_lazy_and_cwd_can_escape():
    _, inert = _heads("f(){ touch hidden; }; echo ok", "touch")
    assert inert == []

    _, invoked = _heads(
        "f(){ cd /workspace/safe; }; f && touch outside", "touch")
    assert len(invoked) == 1
    assert invoked[0].cwd.values == (SAFE,)
    assert "function:f" not in invoked[0].context


def test_redirect_binds_entry_cwd_before_cd():
    analysis = analyze_bash(
        "cd /workspace/safe > poison; touch after", initial_cwd=ROOT)
    redirects = [
        event for event in analysis.static_path_events
        if event.kind == "redirect"
    ]
    assert len(redirects) == 1
    assert redirects[0].target_values == ("poison",)
    assert redirects[0].cwd.values == (ROOT,)


def test_and_or_choose_success_and_failure_cwd():
    _, success = _heads("cd /workspace/safe && touch ok", "touch")
    assert success[0].cwd.values == (SAFE,)

    _, failure = _heads("cd /workspace/safe || touch fallback", "touch")
    assert failure[0].cwd.values == (ROOT,)


def test_exit_branch_does_not_flow_into_later_command():
    _, touches = _heads(
        "cd /workspace/safe || exit; touch after", "touch")
    assert touches[0].cwd.values == (SAFE,)


def test_plain_sequence_keeps_both_cd_outcomes():
    _, touches = _heads("cd /workspace/safe; touch after", "touch")
    assert set(touches[0].cwd.values) == {ROOT, SAFE}


def test_complex_loop_cwd_is_explicitly_unknown():
    analysis, touches = _heads(
        'for item in a b; do touch "$item"; done', "touch")
    assert analysis.path_unverifiable is False
    assert touches[0].cwd.unknown is True
    assert touches[0].dynamic_args is True


def test_external_binding_can_represent_scheduler_scratch():
    _, touches = _heads(
        'cd "$TMPDIR" && touch result',
        "touch",
        external_bindings={"TMPDIR": ("<scheduler-scratch>",)},
    )
    assert touches[0].cwd.values == ("<scheduler-scratch>",)


def test_fd_duplication_is_not_a_file_redirect():
    analysis = analyze_bash("printf x 2>&1", initial_cwd=ROOT)
    assert not [
        event for event in analysis.static_path_events
        if event.kind == "redirect"
    ]


def test_quoted_shell_text_is_data_not_an_executed_writer():
    _, touches = _heads("printf %s \"touch baseline/poison\"", "touch")
    assert touches == []

    analysis = analyze_bash(
        "python -c \"print(1 > 0)\"", initial_cwd=ROOT)
    assert not [
        event for event in analysis.static_path_events
        if event.kind == "redirect"
    ]


def test_common_cmake_pipeline_remains_statically_classified():
    analysis, commands = _commands(
        "cmake -S source -B build && "
        "cmake --build build 2>&1 | tee build/build.log"
    )
    assert analysis.dynamic_execution is False
    assert analysis.path_unverifiable is False
    assert [event.head for event in commands] == [
        "cmake", "cmake", "tee",
    ]


def test_mixed_known_and_unknown_redirect_variable_stays_unknown():
    analysis = analyze_bash(
        "srun ./a.out > $TMPDIR/$CASE/x",
        initial_cwd=ROOT,
        external_bindings={"TMPDIR": ("<scheduler-scratch>",)},
    )
    redirects = [
        event for event in analysis.static_path_events
        if event.kind == "redirect"
    ]
    assert len(redirects) == 1
    assert redirects[0].target_values == ()
    assert redirects[0].target_unknown is True


def test_dispatch_roles_expose_transparent_wrappers_and_real_commands():
    for command, wrapper, real in (
        ("bash -c 'touch /x'", "bash", "touch"),
        ("f(){ touch /x; }; f", "f", "touch"),
        ("builtin echo x", "builtin", "echo"),
        ("eval 'touch /x'", "eval", "touch"),
    ):
        _analysis, events = _commands(command)
        assert [(event.head, event.dispatch_role) for event in events] == [
            (wrapper, "transparent"),
            (real, "direct"),
        ]


def test_delegated_runtime_command_is_not_route_identity():
    _analysis, events = _commands("printf x | xargs touch")
    assert [(event.head, event.dispatch_role) for event in events] == [
        ("printf", "direct"),
        ("xargs", "direct"),
        ("touch", "delegated"),
    ]
    assert events[-1].runtime_args is True


def test_head_path_extraction_matrix():
    """路径形式 argv[0] 保留原始 token；裸名（走 PATH）为 None。

    ``head`` 的 basename 语义逐字不变——route 匹配与既有消费者共用该字段，
    E-13 只加 ``head_path``，不改任何既有值。
    """
    for command, expected_head, expected_head_path in (
        ("/opt/tools/solver --steps 5", "solver", "/opt/tools/solver"),
        ("bin/solver --steps 5", "solver", "bin/solver"),
        ("./solver input.dat", "solver", "./solver"),
        ("../outside/solver x", "solver", "../outside/solver"),
        ("solver --steps 5", "solver", None),
        ("env OMP_NUM_THREADS=4 /opt/tools/solver run", "solver",
         "/opt/tools/solver"),
        ("command /opt/tools/solver run", "solver", "/opt/tools/solver"),
        ("exec /opt/tools/solver run", "solver", "/opt/tools/solver"),
        ("env A=1 solver run", "solver", None),
        ('c=/usr/bin; "$c/gcc" --version', "gcc", "/usr/bin/gcc"),
    ):
        _, events = _heads(command, expected_head)
        assert len(events) == 1, command
        assert events[0].head == expected_head, command
        assert events[0].head_path == expected_head_path, command


def test_dynamic_head_has_no_head_path():
    analysis, events = _commands('"$runner" job.sh')
    assert analysis.dynamic_execution is True
    assert all(event.head_path is None for event in events)


def test_delegated_targets_carry_head_path():
    _, find_events = _heads(
        "find . -name '*.sh' -exec /opt/tools/tool {} ';'", "tool")
    assert len(find_events) == 1
    assert find_events[0].dispatch_role == "delegated"
    assert find_events[0].head_path == "/opt/tools/tool"

    _, xargs_events = _heads("printf x | xargs ./tool", "tool")
    assert len(xargs_events) == 1
    assert xargs_events[0].dispatch_role == "delegated"
    assert xargs_events[0].head_path == "./tool"

    _, bare = _heads("printf x | xargs touch", "touch")
    assert bare[0].head_path is None


def test_head_identity_agrees_with_exec_preflight_engine():
    """双引擎一致性 corpus（E-13 × E-14）。

    同一批命令样本上，AST 路径投影（head/head_path）与提交链的
    ``collect_exec_path_targets``（本地存在性预检）对"命令头是谁"的结论
    必须一致——各自在自己的维度断言：前者回答远端可见性投影用哪个 token，
    后者回答哪些路径形态目标要做本地 exec 预检。
    """
    from nodes.experiment.tools import safe_bash as sb

    for command, expected_pairs, expected_exec_tokens in (
        # 绝对路径头：两个引擎都认定它是路径形态可执行目标。
        ("/opt/tools/solver --steps 5",
         [("solver", "/opt/tools/solver")], ["/opt/tools/solver"]),
        # 相对含斜杠：同上。
        ("bin/solver --steps 5", [("solver", "bin/solver")], ["bin/solver"]),
        ("./solver input.dat", [("solver", "./solver")], ["./solver"]),
        # env 包裹：两侧都剥掉 wrapper 后看真实入口。
        ("env OMP_NUM_THREADS=4 ./solver run",
         [("solver", "./solver")], ["./solver"]),
        # mpirun 委托：launcher 自己是裸名（PATH），被启动程序 ./solver 是
        # E-14 维度的路径目标；AST 层按 operand/委托语义处理，不冒充命令头。
        ("mpirun -np 4 ./solver", [("mpirun", None)], ["./solver"]),
        # find -exec 委托：AST 层产出 delegated head_path；词法引擎不解析
        # find 语义，不得把选项值误认成命令头。
        ("find . -name '*.sh' -exec ./tool {} ';'",
         [("find", None), ("tool", "./tool")], []),
        ("printf x | xargs ./tool",
         [("printf", None), ("xargs", None), ("tool", "./tool")], []),
        # 裸名：两侧都不产生路径形态命令头。
        ("solver --steps 5", [("solver", None)], []),
        # 动态头：两侧都提取不出可证明的路径头。
        ('"$runner" job.sh', [], []),
    ):
        _, events = _commands(command)
        pairs = [
            (event.head, event.head_path) for event in events
            if event.head is not None and event.head != "__hf_dynamic__"
        ]
        assert pairs == expected_pairs, command
        exec_tokens = [
            entry["token"]
            for entry in sb.collect_exec_path_targets(command, cwd=ROOT)
        ]
        assert exec_tokens == expected_exec_tokens, command


def test_runtime_shell_payload_and_lastpipe_fail_closed():
    trap = analyze_bash("trap \"touch poison\" EXIT", initial_cwd=ROOT)
    assert trap.dynamic_execution is False
    assert trap.path_unverifiable is True

    lastpipe = analyze_bash(
        "shopt -s lastpipe; cd /workspace/safe | cat; touch after",
        initial_cwd=ROOT,
    )
    assert lastpipe.path_unverifiable is True
