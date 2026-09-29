"""Single internal Bash semantic analyzer backed by tree-sitter-bash.

The result is deliberately about executable command positions, not words that
happen to occur in shell text. Callers must reject dynamic execution rather
than falling back to keyword matching.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal

try:
    import tree_sitter_bash
    from tree_sitter import Language, Node, Parser
except ImportError as exc:
    Language = Node = Parser = None  # type: ignore[assignment,misc]
    tree_sitter_bash = None  # type: ignore[assignment]
    _ANALYZER_UNAVAILABLE = f"{type(exc).__name__}: {exc}"
    _PARSER = None
else:
    _ANALYZER_UNAVAILABLE = None
    _LANGUAGE = Language(tree_sitter_bash.language())
    _PARSER = Parser(_LANGUAGE)

_SCHEDULER_WORDS = frozenset({"sbatch", "qsub", "bsub", "salloc", "srun"})
_SHELLS = frozenset({"bash", "sh", "dash", "zsh", "ksh"})
_DETACHERS = frozenset({"nohup", "setsid", "disown", "coproc"})

EXTERNAL_CONTROL_NONE = "not_control_plane"
EXTERNAL_CONTROL_QUERY = "read_only_query"
EXTERNAL_CONTROL_EFFECT = "external_effect"
ExternalControlMode = Literal[
    "not_control_plane", "read_only_query", "external_effect",
]

_EXTERNAL_CONTROL_HEADS = frozenset({
    "kubectl", "docker", "podman", "systemd-run", "tmux", "screen",
    "at", "batch", "apptainer", "singularity", "ssh", "pdsh",
})
_MAKE_HEADS = frozenset({"make", "gmake"})
_MAKE_FLAG_ENV = frozenset({"MAKEFLAGS", "MFLAGS"})
_PERSISTENT_ASSIGNMENT_BUILTINS = frozenset({
    "declare", "export", "local", "readonly", "typeset",
})
_MAKE_SHORT_OPTIONS_WITH_VALUE = frozenset({
    "C", "f", "I", "j", "l", "O", "o", "W",
})
_HELP_VERSION_ARGS = frozenset({
    "-h", "--help", "-V", "--version", "help", "version",
})
_FIND_EXEC_ACTIONS = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
_XARGS_VALUE_OPTIONS = frozenset({
    "-a", "--arg-file", "-d", "--delimiter", "-E", "--eof", "-I", "--replace",
    "-L", "--max-lines", "-n", "--max-args", "-P", "--max-procs",
    "-s", "--max-chars",
})
_XARGS_FLAG_OPTIONS = frozenset({
    "-0", "--null", "-o", "--open-tty", "-p", "--interactive",
    "-r", "--no-run-if-empty", "-t", "--verbose", "-x", "--exit",
})
_DYNAMIC_TOKEN = "__hf_dynamic__"
_MAX_NESTING = 8
_MAX_STATIC_VALUES = 32
_MAX_FLOW_ITERATIONS = _MAX_STATIC_VALUES + 2
_MAX_STATIC_COMMANDS = 512
_UNQUOTED_DYNAMIC_CHARS = frozenset("*?[ \t\r\n")

StaticValues = tuple[str, ...]
Bindings = dict[str, StaticValues | None]
FunctionBody = tuple[Node, bytes]


@dataclass(frozen=True)
class FunctionBinding:
    bodies: tuple[FunctionBody, ...]
    may_be_undefined: bool = False


Functions = dict[str, FunctionBinding]


@dataclass(frozen=True)
class SchedulerInvocation:
    head: str
    args: tuple[str, ...]


@dataclass(frozen=True)
class CwdDomain:
    """A bounded set of possible working directories at one AST event.

    ``unknown`` is independent from ``values``: a branch may have several
    statically known cwd values and another unresolved one. Path consumers
    must project both parts; unknown must never degrade to no target.
    """

    values: tuple[str, ...] = ()
    unknown: bool = False


@dataclass(frozen=True)
class StaticPathEvent:
    """One executable command or output redirect with its shell cwd scope."""

    kind: str
    cwd: CwdDomain
    raw: str
    context: tuple[str, ...] = ()
    dispatch_role: str = "direct"
    head: str | None = None
    # 原始 argv[0] token（含目录信息），仅当它含 "/" 时才填——POSIX 下这必然
    # 是文件系统路径而非 PATH 查找。``head`` 保持 basename 语义不变，route
    # 匹配与既有消费者不受影响；远端可见性投影用本字段找回目录信息。
    head_path: str | None = None
    args: tuple[str, ...] = ()
    dynamic_args: bool = False
    runtime_args: bool = False
    redirect_operator: str | None = None
    target_values: tuple[str, ...] = ()
    target_unknown: bool = False


@dataclass
class BashAnalysis:
    scheduler_invocations: list[SchedulerInvocation] = field(default_factory=list)
    # tree-sitter 已确认处于执行位置的命令或输出重定向片段；供路径层
    # 按真实 AST 顺序复用，覆盖 pipeline、subshell、函数体与字面 shell -c。
    static_path_nodes: list[str] = field(default_factory=list)
    static_path_events: list[StaticPathEvent] = field(default_factory=list)
    path_unverifiable: bool = False
    backgrounded: bool = False
    detached_launch: bool = False
    ignore_errors_build: bool = False
    external_control: ExternalControlMode = EXTERNAL_CONTROL_NONE
    dynamic_execution: bool = False
    parse_error: bool = False
    analyzer_unavailable: str | None = None

    @property
    def unsafe_dynamic(self) -> bool:
        return self.dynamic_execution or self.parse_error or self.analyzer_unavailable is not None


def _source(node: Node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _contains_expansion(node: Node) -> bool:
    return (
        node.type in {"command_substitution", "process_substitution"}
        or node.type.endswith("_expansion")
        or any(_contains_expansion(child) for child in node.children)
    )


def _literal(node: Node, source: bytes) -> str | None:
    """Return the shell value only when the AST proves it is static."""
    if _contains_expansion(node):
        return None
    text = _source(node, source)
    if node.type in {"string", "raw_string"} and len(text) >= 2:
        quote = text[:1]
        if quote in {"'", '"'} and text[-1:] == quote:
            return text[1:-1]
    return text


def _dedupe_values(values: list[str]) -> StaticValues | None:
    result = tuple(dict.fromkeys(values))
    return result if len(result) <= _MAX_STATIC_VALUES else None


def _combine_values(parts: list[StaticValues]) -> StaticValues | None:
    values = [""]
    for part in parts:
        combined = [prefix + suffix for prefix in values for suffix in part]
        deduped = _dedupe_values(combined)
        if deduped is None:
            return None
        values = list(deduped)
    return tuple(values)


def _variable_name(node: Node, source: bytes) -> str | None:
    named = list(node.named_children)
    if node.type == "simple_expansion":
        if len(named) == 1 and named[0].type == "variable_name":
            return _source(named[0], source)
        return None
    if node.type == "expansion":
        if len(named) == 1 and named[0].type == "variable_name":
            return _source(named[0], source)
        return None
    return None


def _resolved_values(node: Node, source: bytes, bindings: Bindings) -> StaticValues | None:
    """Evaluate only a bounded, side-effect-free subset of shell words."""
    if node.type in {"command_substitution", "process_substitution"}:
        return None
    variable = _variable_name(node, source)
    if variable is not None:
        return bindings.get(variable)
    # tree-sitter-bash can expose an ambiguous unbraced mixed expansion as
    # a concatenation ending in a bare dollar token.  A known prefix is not
    # sufficient path authority when an unknown suffix can still escape it.
    if node.type == "concatenation" and any(
            not child.is_named and child.type == "$"
            for child in node.children):
        return None
    if not _contains_expansion(node):
        literal = _literal(node, source)
        return (literal,) if literal is not None else None
    if node.type not in {"command_name", "string", "concatenation"}:
        return None
    parts: list[StaticValues] = []
    for child in node.named_children:
        values = _resolved_values(child, source, bindings)
        if values is None:
            return None
        parts.append(values)
    return _combine_values(parts) if parts else ("",)


HeadPair = tuple[str, str | None]


def _resolved_heads(
    node: Node, source: bytes, bindings: Bindings,
) -> tuple[HeadPair, ...] | None:
    values = _resolved_values(node, source, bindings)
    if values is None:
        return None
    text = _source(node, source).lstrip()
    quoted = text.startswith(("'", '"'))
    if not quoted and _contains_expansion(node):
        # Unquoted expansion performs field splitting and globbing. Its command
        # count can depend on IFS or the filesystem, so it is not a static head.
        if any(any(char in value for char in _UNQUOTED_DYNAMIC_CHARS)
               for value in values):
            return None
    pairs = tuple(dict.fromkeys(
        (normalized, _head_path_token(value)) for value in values
        if (normalized := _normal_head(value)) is not None
    ))
    return pairs if len(pairs) <= _MAX_STATIC_VALUES else None


def _normal_head(value: str | None) -> str | None:
    return value.rsplit("/", 1)[-1] if value else None


def _head_path_token(value: str | None) -> str | None:
    """保留路径形式的 argv[0] token；裸名（走 PATH）为 None。"""
    return value if value and "/" in value and value != _DYNAMIC_TOKEN else None


def _only_help_or_version(args: list[str]) -> bool:
    return bool(args) and all(
        item in _HELP_VERSION_ARGS for item in args
    )


def _first_static_operand(
    args: list[str],
    *,
    value_options: frozenset[str] = frozenset(),
    flag_options: frozenset[str] = frozenset(),
    short_value_options: frozenset[str] = frozenset(),
) -> tuple[str | None, int]:
    """Return the first non-option token, or the dynamic sentinel.

    Only explicitly modelled global options are skipped.  An unknown option in
    front of a control-plane verb is not guessed to be read-only because it may
    consume the following token.
    """
    index = 0
    while index < len(args):
        token = args[index]
        if token == _DYNAMIC_TOKEN:
            return _DYNAMIC_TOKEN, index
        if token == "--":
            index += 1
            return (
                (args[index].lower(), index)
                if index < len(args) else (None, index)
            )
        if token in value_options:
            if index + 1 >= len(args) or args[index + 1] == _DYNAMIC_TOKEN:
                return _DYNAMIC_TOKEN, index
            index += 2
            continue
        if any(token.startswith(option + "=") for option in value_options):
            index += 1
            continue
        if any(
            token.startswith(option) and token != option
            for option in short_value_options
        ):
            index += 1
            continue
        if token in flag_options:
            index += 1
            continue
        if token.startswith("-"):
            return _DYNAMIC_TOKEN, index
        return token.lower(), index
    return None, index


def _docker_read_only(args: list[str]) -> bool:
    verb, index = _first_static_operand(
        args,
        value_options=frozenset({
            "--config", "--context", "-c", "--host", "-H", "--log-level",
            "--connection", "--url", "--identity", "--root", "--runroot",
            "--storage-driver", "--events-backend", "--runtime",
        }),
        flag_options=frozenset({
            "--debug", "-D", "--tls", "--tlsverify", "--remote",
        }),
        short_value_options=frozenset({"-c", "-H"}),
    )
    if verb is None:
        return True
    if verb in {_DYNAMIC_TOKEN, ""}:
        return False
    if verb in {
        "version", "info", "ps", "images", "inspect", "logs", "stats",
        "top", "events", "port", "wait", "history", "search", "help",
    }:
        return True
    nested_read_only = {
        "image": {"ls", "list", "inspect", "history"},
        "container": {
            "ls", "list", "inspect", "logs", "stats", "top", "port", "wait",
        },
        "network": {"ls", "list", "inspect"},
        "volume": {"ls", "list", "inspect"},
        "context": {"ls", "list", "show", "inspect"},
        "system": {"df", "events", "info"},
        "builder": {"ls", "inspect"},
        "manifest": {"inspect"},
        "machine": {"ls", "list", "inspect", "info"},
    }
    if verb == "compose":
        nested, _ = _first_static_operand(
            args[index + 1:],
            value_options=frozenset({
                "-f", "--file", "--profile", "--project-name",
                "--project-directory", "--env-file", "--ansi",
                "--progress", "--parallel",
            }),
            flag_options=frozenset({"--compatibility", "--dry-run"}),
            short_value_options=frozenset({"-f"}),
        )
        return nested in {
            None, "config", "events", "images", "ls", "logs", "ps", "top",
            "version",
        }
    if verb in nested_read_only:
        nested, _ = _first_static_operand(args[index + 1:])
        return nested in nested_read_only[verb]
    return False


def classify_external_control_command(
    head: str | None,
    args: list[str] | tuple[str, ...],
) -> ExternalControlMode:
    """对一个静态执行头给出唯一 external-control 三态结论。

    not_control_plane 表示应继续走普通命令语义；read_only_query 表示只做
    能力/状态读取；external_effect 表示本地客户端不能证明远端生命周期已
    收敛，必须由受管外部作业契约接管。
    """
    normalized_head = _normal_head(head)
    normalized_args = list(args)
    if normalized_head not in _EXTERNAL_CONTROL_HEADS:
        return EXTERNAL_CONTROL_NONE
    # 远端 shell 的本地进程退出不代表远端工作结束；只有精确的客户端
    # 版本探查不建立远端会话。其他 ssh/pdsh 形式全部交给受管平台契约。
    if normalized_head in {"ssh", "pdsh"}:
        return (
            EXTERNAL_CONTROL_QUERY
            if normalized_args == ["-V"]
            else EXTERNAL_CONTROL_EFFECT
        )
    if _only_help_or_version(normalized_args):
        return EXTERNAL_CONTROL_QUERY
    if normalized_head == "kubectl":
        # Cobra 的 help flag 在动作解析前退出，不触发 API 变更或远端 exec。
        if any(item in {"-h", "--help"} for item in normalized_args):
            return EXTERNAL_CONTROL_QUERY
        verb, verb_index = _first_static_operand(
            normalized_args,
            value_options=frozenset({
                "--as", "--as-group", "--cache-dir", "--certificate-authority",
                "--client-certificate", "--client-key", "--cluster",
                "--context", "--kubeconfig", "--namespace", "-n",
                "--request-timeout", "--server", "-s", "--token", "--user",
            }),
            flag_options=frozenset({
                "--disable-compression", "--insecure-skip-tls-verify",
                "--match-server-version", "--warnings-as-errors",
            }),
            short_value_options=frozenset({"-n", "-s"}),
        )
        if verb in {
            None, "get", "version", "describe", "logs", "top",
            "api-resources", "api-versions", "explain", "cluster-info",
        }:
            return EXTERNAL_CONTROL_QUERY
        nested, _ = _first_static_operand(normalized_args[verb_index + 1:])
        if verb == "auth" and nested == "can-i":
            return EXTERNAL_CONTROL_QUERY
        if (
            verb == "config"
            and nested in {"view", "current-context", "get-contexts"}
        ):
            return EXTERNAL_CONTROL_QUERY
        return EXTERNAL_CONTROL_EFFECT
    if normalized_head in {"docker", "podman"}:
        return (
            EXTERNAL_CONTROL_QUERY
            if _docker_read_only(normalized_args)
            else EXTERNAL_CONTROL_EFFECT
        )
    if normalized_head == "systemd-run":
        return (
            EXTERNAL_CONTROL_EFFECT
            if normalized_args else EXTERNAL_CONTROL_QUERY
        )
    if normalized_head == "tmux":
        verb, _ = _first_static_operand(
            normalized_args,
            value_options=frozenset({"-c", "-f", "-L", "-S", "-T"}),
            flag_options=frozenset({
                "-2", "-C", "-CC", "-D", "-N", "-u", "-v", "-V",
            }),
        )
        read_only = verb in {
            "-v", "display-message", "info", "list-clients",
            "list-commands", "list-keys", "list-panes", "list-sessions",
            "list-windows", "ls", "show-environment", "show-options",
        }
        return (
            EXTERNAL_CONTROL_QUERY
            if read_only else EXTERNAL_CONTROL_EFFECT
        )
    if normalized_head == "screen":
        read_only = bool(
            normalized_args
            and normalized_args[0] in {
                "-ls", "-list", "-version", "--version",
            }
        )
        return (
            EXTERNAL_CONTROL_QUERY
            if read_only else EXTERNAL_CONTROL_EFFECT
        )
    if normalized_head == "at":
        read_only = any(
            item in {"-l", "--list", "-c"} for item in normalized_args
        )
        return (
            EXTERNAL_CONTROL_QUERY
            if read_only else EXTERNAL_CONTROL_EFFECT
        )
    if normalized_head == "batch":
        return EXTERNAL_CONTROL_EFFECT
    if normalized_head in {"apptainer", "singularity"}:
        verb, index = _first_static_operand(
            normalized_args,
            flag_options=frozenset({
                "--debug", "--quiet", "--silent", "--verbose",
            }),
        )
        if verb in {"instance.start", "instance.run", "instance.stop"}:
            return EXTERNAL_CONTROL_EFFECT
        if verb in {"instance.list", "instance.stats"}:
            return EXTERNAL_CONTROL_QUERY
        if verb != "instance":
            return EXTERNAL_CONTROL_NONE
        action, _ = _first_static_operand(normalized_args[index + 1:])
        if action in {"start", "run", "stop"}:
            return EXTERNAL_CONTROL_EFFECT
        if action in {None, "list", "stats"}:
            return EXTERNAL_CONTROL_QUERY
        return EXTERNAL_CONTROL_EFFECT
    return EXTERNAL_CONTROL_NONE


def _record_external_control(
    result: BashAnalysis,
    classification: ExternalControlMode,
) -> None:
    if classification == EXTERNAL_CONTROL_EFFECT:
        result.external_control = EXTERNAL_CONTROL_EFFECT
    elif (
        classification == EXTERNAL_CONTROL_QUERY
        and result.external_control == EXTERNAL_CONTROL_NONE
    ):
        result.external_control = EXTERNAL_CONTROL_QUERY


def _short_make_flags_ignore_errors(token: str) -> bool:
    if not token.startswith("-") or token.startswith("--"):
        return False
    for option in token[1:]:
        if option == "i":
            return True
        if option in _MAKE_SHORT_OPTIONS_WITH_VALUE:
            break
    return False


def _make_flag_value_ignores_errors(value: str) -> bool:
    for token in value.split():
        if token == "--ignore-errors":
            return True
        if _short_make_flags_ignore_errors(token):
            return True
        # GNU make serializes boolean MAKEFLAGS as a leading bare bundle.
        if token.isalpha() and "i" in token:
            return True
    return False


def _unwrap_static_wrappers(
    head: str,
    args: list[str],
) -> tuple[str | None, list[str], list[str]]:
    """Unwrap command/env/exec while retaining command-local make flags."""
    current_head: str | None = head
    current_args = list(args)
    assignments: list[str] = []
    for _ in range(4):
        if current_head == "command":
            index = 0
            while index < len(current_args) and current_args[index].startswith("-"):
                option = current_args[index]
                if (
                    option in {"-v", "-V", "--version"}
                    or (
                        option.startswith("-")
                        and not option.startswith("--")
                        and any(flag in option[1:] for flag in ("v", "V"))
                    )
                ):
                    return None, [], assignments
                index += 1
            if index >= len(current_args):
                return None, [], assignments
            current_head = _normal_head(current_args[index])
            current_args = current_args[index + 1:]
            continue
        if current_head == "env":
            index = 0
            while index < len(current_args):
                token = current_args[index]
                if token == "--":
                    index += 1
                    break
                if "=" in token and token.split("=", 1)[0].isidentifier():
                    assignments.append(token)
                    index += 1
                    continue
                if token in {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}:
                    index += 2
                    continue
                if token.startswith(("--unset=", "--chdir=", "--split-string=")):
                    index += 1
                    continue
                if token.startswith("-"):
                    index += 1
                    continue
                break
            if index >= len(current_args):
                return None, [], assignments
            current_head = _normal_head(current_args[index])
            current_args = current_args[index + 1:]
            continue
        if current_head == "exec":
            index = 0
            while index < len(current_args) and current_args[index].startswith("-"):
                index += 2 if current_args[index] == "-a" else 1
            if index >= len(current_args):
                return None, [], assignments
            current_head = _normal_head(current_args[index])
            current_args = current_args[index + 1:]
            continue
        break
    return current_head, current_args, assignments


def _make_invocation_ignores_errors(
    head: str,
    args: list[str],
    bindings: Bindings,
) -> bool:
    make_head, make_args, wrapper_assignments = _unwrap_static_wrappers(
        head, args)
    if make_head not in _MAKE_HEADS:
        return False
    flag_values: list[str] = []
    for token in [*wrapper_assignments, *make_args]:
        if "=" not in token:
            continue
        name, value = token.split("=", 1)
        if name in _MAKE_FLAG_ENV:
            flag_values.append(value)
    for name in _MAKE_FLAG_ENV:
        if name not in bindings:
            continue
        values = bindings[name]
        if values is None:
            return True
        flag_values.extend(values)
    if any(_make_flag_value_ignores_errors(value) for value in flag_values):
        return True
    for token in make_args:
        if token == "--":
            break
        if token == "--ignore-errors" or _short_make_flags_ignore_errors(token):
            return True
    return False


def _command_tokens(
    node: Node, source: bytes, bindings: Bindings,
) -> tuple[tuple[HeadPair, ...] | None, list[str], bool]:
    name = node.child_by_field_name("name")
    if name is None:
        return None, [], False
    heads = _resolved_heads(name, source, bindings)
    args: list[str] = []
    dynamic_args = False
    for child in node.children:
        if child == name or child.type in {"command_name", "comment"}:
            continue
        if not child.is_named:
            continue
        values = _resolved_values(child, source, bindings)
        if values is None or len(values) != 1:
            # Expanded arguments are ordinary data unless a command-specific
            # handler uses that position as executable source.
            args.append(_DYNAMIC_TOKEN)
            dynamic_args = True
        else:
            args.append(values[0])
    return heads, args, dynamic_args


def _unwrap(
    head: str, args: list[str], head_token: str | None = None,
) -> tuple[str | None, list[str], bool, str | None]:
    """展开 command/env/exec 包装并透传原始 head token（第四个返回值）。

    第四项仅供路径层填 ``StaticPathEvent.head_path``；route 层忽略它，
    basename 语义（第一项）保持不变。
    """
    index = 0
    while index < len(args) and "=" in args[index] and args[index].split("=", 1)[0].isidentifier():
        index += 1
    if head == "command":
        if index < len(args) and args[index] in {"-v", "-V", "--version"}:
            return "__hf_noexec__", [], False, None
        while index < len(args) and args[index].startswith("-"):
            index += 1
        return (
            (_normal_head(args[index]), args[index + 1:], False,
             _head_path_token(args[index]))
            if index < len(args) else (None, [], False, None)
        )
    if head == "env":
        while index < len(args):
            token = args[index]
            if token == "--":
                index += 1
                break
            if "=" in token and token.split("=", 1)[0].isidentifier():
                index += 1
                continue
            if token.startswith("-"):
                index += 2 if token in {"-u", "--unset"} else 1
                continue
            break
        return (
            (_normal_head(args[index]), args[index + 1:], False,
             _head_path_token(args[index]))
            if index < len(args) else (None, [], False, None)
        )
    if head == "exec":
        while index < len(args) and args[index].startswith("-"):
            if args[index] == "-a":
                index += 2
            else:
                index += 1
        return (
            (_normal_head(args[index]), args[index + 1:], False,
             _head_path_token(args[index]))
            if index < len(args) else ("__hf_noexec__", [], False, None)
        )
    return head, args, True, head_token


def _shell_c_payload(args: list[str]) -> tuple[bool, str | None]:
    for index, arg in enumerate(args):
        if arg == "--":
            break
        is_short_c = (
            arg.startswith("-")
            and not arg.startswith("--")
            and "c" in arg[1:]
        )
        if arg == "-c" or is_short_c:
            return True, args[index + 1] if index + 1 < len(args) else None
    return False, None


def _shell_is_nonexecuting(args: list[str]) -> bool:
    for arg in args:
        if arg == "--":
            break
        if arg in {"--help", "--version", "--noexec"}:
            return True
        if arg.startswith("-") and not arg.startswith("--") and "n" in arg[1:]:
            return True
        if not arg.startswith("-"):
            break
    return False


def _xargs_target(
    args: list[str],
) -> tuple[str | None, list[str], bool, str | None]:
    index = 0
    replacement: str | None = None
    while index < len(args):
        token = args[index]
        if token == _DYNAMIC_TOKEN:
            return None, [], True, None
        if token == "--":
            index += 1
            break
        if token in _XARGS_FLAG_OPTIONS:
            index += 1
            continue
        if token in _XARGS_VALUE_OPTIONS:
            if index + 1 >= len(args) or args[index + 1] == _DYNAMIC_TOKEN:
                return None, [], True, None
            if token in {"-I", "--replace"}:
                replacement = args[index + 1]
            index += 2
            continue
        if token.startswith("--") and "=" in token:
            option, value = token.split("=", 1)
            if option in _XARGS_VALUE_OPTIONS:
                if option == "--replace":
                    replacement = value
                index += 1
                continue
        if token.startswith("-") and len(token) > 2:
            short = token[:2]
            if short in _XARGS_VALUE_OPTIONS:
                if short == "-I":
                    replacement = token[2:]
                index += 1
                continue
        if token.startswith("-"):
            return None, [], True, None
        break
    if index >= len(args):
        return "echo", [], False, None
    target = args[index]
    if replacement and replacement in target:
        return None, [], True, None
    return _normal_head(target), args[index + 1:], False, _head_path_token(target)

def _find_exec_targets(
    args: list[str],
) -> tuple[list[tuple[str, str | None, list[str]]], bool]:
    targets: list[tuple[str, str | None, list[str]]] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token not in _FIND_EXEC_ACTIONS:
            index += 1
            continue
        if index + 1 >= len(args):
            return [], True
        target = args[index + 1]
        if target == _DYNAMIC_TOKEN or "{}" in target:
            return [], True
        end = index + 2
        while end < len(args) and args[end] not in {";", "+"}:
            end += 1
        if end >= len(args):
            return [], True
        target_args = args[index + 2:end]
        targets.append(
            (_normal_head(target) or "", _head_path_token(target), target_args))
        index = end + 1
    return targets, False


def _record_nested_payload(
    payload: str | None,
    result: BashAnalysis,
    depth: int,
    *,
    functions: Functions | None = None,
    call_stack: frozenset[str] = frozenset(),
) -> None:
    if payload is None or payload == _DYNAMIC_TOKEN or depth >= _MAX_NESTING:
        result.dynamic_execution = True
        return
    source = payload.encode("utf-8")
    tree = _PARSER.parse(source)
    result.parse_error = result.parse_error or tree.root_node.has_error
    _walk(
        tree.root_node, source, result, depth + 1,
        functions if functions is not None else {}, call_stack, {},
    )


def _classify_command(
    head: str | None,
    args: list[str],
    dynamic_args: bool,
    allow_functions: bool,
    result: BashAnalysis,
    depth: int,
    functions: Functions,
    call_stack: frozenset[str],
    bindings: Bindings,
) -> None:
    if head is None or head == "__hf_dynamic__":
        result.dynamic_execution = True
        return
    if head == "__hf_noexec__":
        return

    if allow_functions and head in functions:
        if head in call_stack or depth >= _MAX_NESTING:
            result.dynamic_execution = True
            return
        function_binding = functions[head]
        if function_binding.may_be_undefined:
            result.dynamic_execution = True
        binding_states: list[Bindings] = []
        function_states: list[Functions] = []
        for body, body_source in function_binding.bodies:
            call_bindings = dict(bindings)
            call_functions = dict(functions)
            saved_positionals = {
                key: value for key, value in call_bindings.items()
                if key.isdigit()
            }
            for key in [key for key in call_bindings if key.isdigit()]:
                call_bindings.pop(key, None)
            for index, arg in enumerate(args, start=1):
                call_bindings[str(index)] = (
                    None if arg == _DYNAMIC_TOKEN else (arg,)
                )
            _walk(
                body, body_source, result, depth + 1, call_functions,
                call_stack | {head}, call_bindings,
            )
            for key in [key for key in call_bindings if key.isdigit()]:
                call_bindings.pop(key, None)
            call_bindings.update(saved_positionals)
            binding_states.append(call_bindings)
            function_states.append(call_functions)
        if binding_states:
            _merge_bindings(bindings, binding_states)
            _merge_functions(functions, function_states)
        return

    if _make_invocation_ignores_errors(head, args, bindings):
        result.ignore_errors_build = True
    external_control = classify_external_control_command(head, args)
    _record_external_control(result, external_control)
    if external_control == EXTERNAL_CONTROL_EFFECT:
        result.detached_launch = True
        return
    if head in _SCHEDULER_WORDS:
        invocation = SchedulerInvocation(head, tuple(args))
        if invocation not in result.scheduler_invocations:
            result.scheduler_invocations.append(invocation)
        return
    if head in _DETACHERS:
        result.detached_launch = True
        return
    if head == "find":
        targets, unknown = _find_exec_targets(args)
        if unknown:
            result.dynamic_execution = True
        else:
            for target, _target_token, target_args in targets:
                _classify_command(
                    target, target_args, _DYNAMIC_TOKEN in target_args, False,
                    result, depth, functions, call_stack, bindings,
                )
        return
    if head == "xargs":
        target, target_args, unknown, _target_token = _xargs_target(args)
        if unknown or target is None:
            result.dynamic_execution = True
        else:
            if target == "env":
                target, target_args, _, _ = _unwrap(target, target_args)
            _classify_command(
                target, target_args, _DYNAMIC_TOKEN in target_args, False,
                result, depth, functions, call_stack, bindings,
            )
        return
    if head == "eval":
        if dynamic_args or _DYNAMIC_TOKEN in args:
            result.dynamic_execution = True
        elif args:
            _record_nested_payload(
                " ".join(args), result, depth,
                functions=functions, call_stack=call_stack,
            )
        bindings.clear()
        return
    if head in {"source", "."}:
        bindings.clear()
        result.dynamic_execution = True
        return
    if head == "read":
        names = [arg for arg in args if arg.isidentifier()]
        for name in names or ["REPLY"]:
            bindings[name] = None
        return
    if head in {"mapfile", "readarray"}:
        names = [arg for arg in args if arg.isidentifier()]
        bindings[(names or ["MAPFILE"])[-1]] = None
        return
    if head == "unset":
        for name in args:
            if name.isidentifier():
                bindings.pop(name, None)
        return
    if head == "printf" and "-v" in args:
        index = args.index("-v")
        if index + 1 < len(args) and args[index + 1].isidentifier():
            bindings[args[index + 1]] = None
        return

    if head in _SHELLS:
        if _shell_is_nonexecuting(args):
            return
        has_c, payload = _shell_c_payload(args)
        if has_c:
            _record_nested_payload(payload, result, depth)
        else:
            # A script path, stdin, pipe, or heredoc is executable source whose
            # contents are outside this command string. Never guess it is safe.
            result.dynamic_execution = True


def _merge_bindings(bindings: Bindings, states: list[Bindings]) -> None:
    keys = set().union(*(state.keys() for state in states)) if states else set()
    bindings.clear()
    for key in keys:
        combined: list[str] = []
        unknown = False
        for state in states:
            values = state.get(key)
            if values is None:
                unknown = True
                break
            combined.extend(values)
        if unknown:
            bindings[key] = None
            continue
        bindings[key] = _dedupe_values(combined)


def _merge_functions(functions: Functions, states: list[Functions]) -> None:
    keys = set().union(*(state.keys() for state in states)) if states else set()
    functions.clear()
    for key in keys:
        bodies: list[FunctionBody] = []
        seen: set[tuple[bytes, int, int]] = set()
        may_be_undefined = False
        for state in states:
            binding = state.get(key)
            if binding is None:
                may_be_undefined = True
                continue
            may_be_undefined = may_be_undefined or binding.may_be_undefined
            for body, body_source in binding.bodies:
                identity = (body_source, body.start_byte, body.end_byte)
                if identity not in seen:
                    seen.add(identity)
                    bodies.append((body, body_source))
        if len(bodies) > _MAX_STATIC_VALUES:
            bodies = []
            may_be_undefined = True
        functions[key] = FunctionBinding(tuple(bodies), may_be_undefined)


def _walk(
    node: Node,
    source: bytes,
    result: BashAnalysis,
    depth: int,
    functions: Functions,
    call_stack: frozenset[str],
    bindings: Bindings,
) -> None:
    actual_source = source
    transient_command_assignments = False
    if node.type == "ERROR":
        result.parse_error = True

    if any(child.type == "&" for child in node.children):
        result.backgrounded = True

    if node.type in {"command_substitution", "process_substitution", "subshell"}:
        local_bindings = dict(bindings)
        local_functions = dict(functions)
        for child in node.children:
            _walk(
                child, actual_source, result, depth, local_functions,
                call_stack, local_bindings,
            )
        return

    if node.type == "pipeline":
        states = [dict(bindings)]
        function_states = [dict(functions)]
        for child in node.named_children:
            component_bindings = dict(bindings)
            component_functions = dict(functions)
            _walk(
                child, actual_source, result, depth, component_functions,
                call_stack, component_bindings,
            )
            states.append(component_bindings)
            function_states.append(component_functions)
        _merge_bindings(bindings, states)
        _merge_functions(functions, function_states)
        return

    if node.type == "list":
        flow_bindings = dict(bindings)
        flow_functions = dict(functions)
        states: list[Bindings] = []
        function_states: list[Functions] = []
        for child in node.named_children:
            _walk(
                child, actual_source, result, depth, flow_functions,
                call_stack, flow_bindings,
            )
            states.append(dict(flow_bindings))
            function_states.append(dict(flow_functions))
        _merge_bindings(bindings, states or [flow_bindings])
        _merge_functions(functions, function_states or [flow_functions])
        return

    if node.type == "variable_assignment":
        name_node = node.child_by_field_name("name")
        value_node = node.child_by_field_name("value")
        if name_node is None:
            result.dynamic_execution = True
            return
        if value_node is not None:
            for child in value_node.children:
                _walk(child, actual_source, result, depth, functions, call_stack, bindings)
        name = _source(name_node, actual_source)
        if value_node is None:
            bindings[name] = ("",)
        else:
            bindings[name] = _resolved_values(value_node, actual_source, bindings)
        return

    if node.type == "for_statement":
        variable = node.child_by_field_name("variable")
        body = node.child_by_field_name("body")
        value_nodes = node.children_by_field_name("value")
        values: list[str] = []
        known = bool(value_nodes)
        for value_node in value_nodes:
            resolved = _resolved_values(value_node, actual_source, bindings)
            if resolved is None:
                known = False
                break
            values.extend(resolved)
        if variable is None:
            result.dynamic_execution = True
            return
        entry_bindings = dict(bindings)
        entry_functions = dict(functions)
        loop_bindings = dict(bindings)
        loop_functions = dict(functions)
        loop_bindings[_source(variable, actual_source)] = (
            _dedupe_values(values) if known else None)
        if body is not None:
            for _ in range(_MAX_FLOW_ITERATIONS):
                iteration_bindings = dict(loop_bindings)
                iteration_functions = dict(loop_functions)
                _walk(
                    body, actual_source, result, depth, iteration_functions,
                    call_stack, iteration_bindings,
                )
                next_bindings: Bindings = {}
                next_functions: Functions = {}
                _merge_bindings(
                    next_bindings, [loop_bindings, iteration_bindings])
                _merge_functions(
                    next_functions, [loop_functions, iteration_functions])
                if (next_bindings == loop_bindings
                        and next_functions == loop_functions):
                    break
                loop_bindings = next_bindings
                loop_functions = next_functions
            else:
                result.dynamic_execution = True
        _merge_bindings(bindings, [entry_bindings, loop_bindings])
        _merge_functions(functions, [entry_functions, loop_functions])
        return

    if node.type in {"while_statement", "until_statement"}:
        condition = node.child_by_field_name("condition")
        body = node.child_by_field_name("body")
        flow_bindings = dict(bindings)
        flow_functions = dict(functions)
        exit_states: list[Bindings] = []
        exit_function_states: list[Functions] = []
        for _ in range(_MAX_FLOW_ITERATIONS):
            condition_bindings = dict(flow_bindings)
            condition_functions = dict(flow_functions)
            if condition is not None:
                _walk(
                    condition, actual_source, result, depth,
                    condition_functions, call_stack, condition_bindings,
                )
            body_bindings = dict(condition_bindings)
            body_functions = dict(condition_functions)
            if body is not None:
                _walk(
                    body, actual_source, result, depth, body_functions,
                    call_stack, body_bindings,
                )
            exit_states.extend((condition_bindings, body_bindings))
            exit_function_states.extend(
                (condition_functions, body_functions))
            next_bindings: Bindings = {}
            next_functions: Functions = {}
            _merge_bindings(
                next_bindings,
                [flow_bindings, condition_bindings, body_bindings],
            )
            _merge_functions(
                next_functions,
                [flow_functions, condition_functions, body_functions],
            )
            if (next_bindings == flow_bindings
                    and next_functions == flow_functions):
                break
            flow_bindings = next_bindings
            flow_functions = next_functions
        else:
            result.dynamic_execution = True
        _merge_bindings(bindings, exit_states or [flow_bindings])
        _merge_functions(
            functions, exit_function_states or [flow_functions])
        return

    if node.type == "case_statement":
        branch_base = dict(bindings)
        function_base = dict(functions)
        value = node.child_by_field_name("value")
        if value is not None:
            _walk(
                value, actual_source, result, depth, function_base,
                call_stack, branch_base,
            )
        states = [branch_base]
        function_states = [function_base]
        for child in node.named_children:
            if child.type != "case_item":
                continue
            branch_bindings = dict(branch_base)
            branch_functions = dict(function_base)
            _walk(
                child, actual_source, result, depth, branch_functions,
                call_stack, branch_bindings,
            )
            states.append(branch_bindings)
            function_states.append(branch_functions)
        _merge_bindings(bindings, states)
        _merge_functions(functions, function_states)
        return

    if node.type in {"if_statement", "elif_clause"}:
        condition_bindings = dict(bindings)
        condition_functions = dict(functions)
        consequence: list[Node] = []
        alternative: Node | None = None
        for index, child in enumerate(node.children):
            if not child.is_named:
                continue
            field = node.field_name_for_child(index)
            if field == "condition":
                _walk(
                    child, actual_source, result, depth,
                    condition_functions, call_stack, condition_bindings,
                )
            elif child.type == "else_clause":
                alternative = child
            else:
                consequence.append(child)
        then_bindings = dict(condition_bindings)
        then_functions = dict(condition_functions)
        for child in consequence:
            _walk(
                child, actual_source, result, depth, then_functions,
                call_stack, then_bindings,
            )
        states = [then_bindings]
        function_states = [then_functions]
        if alternative is not None:
            else_bindings = dict(condition_bindings)
            else_functions = dict(condition_functions)
            _walk(
                alternative, actual_source, result, depth, else_functions,
                call_stack, else_bindings,
            )
            states.append(else_bindings)
            function_states.append(else_functions)
        else:
            states.append(condition_bindings)
            function_states.append(condition_functions)
        _merge_bindings(bindings, states)
        _merge_functions(functions, function_states)
        return

    if node.type == "function_definition":
        name_node = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        name = _normal_head(_literal(name_node, actual_source)) if name_node is not None else None
        if name is None or body is None:
            result.dynamic_execution = True
        else:
            functions[name] = FunctionBinding(((body, actual_source),))
        return

    if node.type == "command":
        heads, args, dynamic_args = _command_tokens(node, actual_source, bindings)
        transient_command_assignments = (
            heads is None
            or any(
                candidate not in _PERSISTENT_ASSIGNMENT_BUILTINS
                for candidate, _raw_head in heads
            )
        )
        if heads is None:
            result.dynamic_execution = True
        else:
            states: list[Bindings] = []
            function_states: list[Functions] = []
            for candidate, _raw_head in heads:
                command_bindings = dict(bindings)
                command_functions = dict(functions)
                if candidate in {"command", "env", "exec"}:
                    if _make_invocation_ignores_errors(
                            candidate, list(args), command_bindings):
                        result.ignore_errors_build = True
                    external_head, external_args, _ = _unwrap_static_wrappers(
                        candidate, list(args))
                    if external_head is not None:
                        external_control = classify_external_control_command(
                            external_head, external_args)
                        _record_external_control(result, external_control)
                        if external_control == EXTERNAL_CONTROL_EFFECT:
                            result.detached_launch = True
                head, unwrapped_args, allow_functions, _head_token = _unwrap(
                    candidate, list(args))
                _classify_command(
                    head, unwrapped_args, dynamic_args, allow_functions,
                    result, depth, command_functions, call_stack,
                    command_bindings,
                )
                states.append(command_bindings)
                function_states.append(command_functions)
            _merge_bindings(bindings, states)
            _merge_functions(functions, function_states)

    for child in node.children:
        if transient_command_assignments and child.type == "variable_assignment":
            _walk(
                child, actual_source, result, depth, dict(functions),
                call_stack, dict(bindings),
            )
            continue
        _walk(child, actual_source, result, depth, functions, call_stack, bindings)



@dataclass(frozen=True)
class _CwdOutcome:
    success: CwdDomain
    failure: CwdDomain
    terminated: CwdDomain = CwdDomain()


def _cwd_domain(values: tuple[str, ...] | list[str] = (), *,
                unknown: bool = False) -> CwdDomain:
    unique = tuple(dict.fromkeys(str(value) for value in values if value))
    if len(unique) > _MAX_STATIC_VALUES:
        return CwdDomain(unknown=True)
    return CwdDomain(unique, unknown)


def _cwd_union(*domains: CwdDomain) -> CwdDomain:
    values: list[str] = []
    unknown = False
    for domain in domains:
        values.extend(domain.values)
        unknown = unknown or domain.unknown
    return _cwd_domain(values, unknown=unknown)


def _cwd_unchanged(incoming: CwdDomain) -> _CwdOutcome:
    return _CwdOutcome(incoming, incoming)


def _cwd_unknown(incoming: CwdDomain) -> CwdDomain:
    return _cwd_domain(list(incoming.values), unknown=True)


def _cwd_success_after_cd(incoming: CwdDomain,
                          args: list[str]) -> CwdDomain:
    operands: list[str] = []
    after_separator = False
    for arg in args:
        if arg == _DYNAMIC_TOKEN:
            return CwdDomain(unknown=True)
        if arg == "--":
            after_separator = True
            continue
        if not after_separator and arg in {"-L", "-P", "-e", "-@"}:
            continue
        if not after_separator and arg.startswith("-"):
            return CwdDomain(unknown=True)
        operands.append(arg)
    if len(operands) != 1:
        return CwdDomain(unknown=True)
    operand = operands[0]
    if not operand or "$" in operand or operand.startswith("~"):
        return CwdDomain(unknown=True)
    if operand.startswith("<"):
        return _cwd_domain((operand,))
    if os.path.isabs(operand):
        return _cwd_domain((os.path.abspath(operand),))
    values = [
        base if base.startswith("<")
        else os.path.abspath(os.path.join(base, operand))
        for base in incoming.values
    ]
    return _cwd_domain(values, unknown=incoming.unknown)


def _append_path_event(result: BashAnalysis, event: StaticPathEvent) -> None:
    if len(result.static_path_events) >= _MAX_STATIC_COMMANDS:
        result.path_unverifiable = True
        return
    result.static_path_events.append(event)
    result.static_path_nodes.append(event.raw)


def _record_path_redirect(node: Node, source: bytes, result: BashAnalysis,
                          incoming: CwdDomain, bindings: Bindings,
                          context: tuple[str, ...]) -> None:
    operator = next(
        (child.type for child in node.children if not child.is_named), None)
    if operator not in {">", ">>", ">|", "&>", "&>>", "<>", ">&"}:
        return
    destination = node.child_by_field_name("destination")
    if destination is None or destination.type in {"number", "file_descriptor"}:
        return
    values = _resolved_values(destination, source, bindings)
    _append_path_event(result, StaticPathEvent(
        kind="redirect", cwd=incoming,
        raw=_source(node, source).strip(), context=context,
        redirect_operator=operator, target_values=values or (),
        target_unknown=values is None,
    ))


def _normalized_external_bindings(
    external_bindings: Bindings | None,
) -> Bindings:
    if not external_bindings:
        return {}
    normalized: Bindings = {}
    for name, values in external_bindings.items():
        if values is None:
            normalized[str(name)] = None
        elif isinstance(values, str):
            normalized[str(name)] = (values,)
        else:
            normalized[str(name)] = tuple(str(value) for value in values)
    return normalized



def _path_substitutions(
    node: Node, source: bytes, result: BashAnalysis,
    incoming: CwdDomain, functions: Functions, bindings: Bindings,
    external_bindings: Bindings, depth: int,
    call_stack: frozenset[str], context: tuple[str, ...],
) -> None:
    for child in node.named_children:
        if child.type in {"command_substitution", "process_substitution"}:
            _walk_path(
                child, source, result, incoming, dict(functions),
                dict(bindings), external_bindings, depth, call_stack,
                context,
            )
        else:
            _path_substitutions(
                child, source, result, incoming, functions, bindings,
                external_bindings, depth, call_stack, context,
            )


def _path_sequence(
    nodes: list[Node], source: bytes, result: BashAnalysis,
    incoming: CwdDomain, functions: Functions, bindings: Bindings,
    external_bindings: Bindings, depth: int,
    call_stack: frozenset[str], context: tuple[str, ...],
) -> _CwdOutcome:
    if not nodes:
        return _cwd_unchanged(incoming)
    active = incoming
    terminated = CwdDomain()
    last = _cwd_unchanged(incoming)
    for index, child in enumerate(nodes):
        if not active.values and not active.unknown:
            break
        last = _walk_path(
            child, source, result, active, functions, bindings,
            external_bindings, depth, call_stack, context,
        )
        terminated = _cwd_union(terminated, last.terminated)
        if index < len(nodes) - 1:
            active = _cwd_union(last.success, last.failure)
    return _CwdOutcome(last.success, last.failure, terminated)


def _path_nested_payload(
    payload: str | None, result: BashAnalysis, incoming: CwdDomain,
    functions: Functions, bindings: Bindings,
    external_bindings: Bindings, depth: int,
    call_stack: frozenset[str], context: tuple[str, ...],
) -> _CwdOutcome:
    if payload is None or payload == _DYNAMIC_TOKEN or depth >= _MAX_NESTING:
        result.path_unverifiable = True
        unknown = _cwd_unknown(incoming)
        return _CwdOutcome(unknown, unknown)
    nested_source = payload.encode("utf-8")
    tree = _PARSER.parse(nested_source)
    result.parse_error = result.parse_error or tree.root_node.has_error
    return _walk_path(
        tree.root_node, nested_source, result, incoming, functions, bindings,
        external_bindings, depth + 1, call_stack, context,
    )


def _path_synthetic_command(
    head: str | None, args: list[str], result: BashAnalysis,
    incoming: CwdDomain, context: tuple[str, ...], raw: str, *,
    runtime_args: bool, head_token: str | None = None,
) -> None:
    _append_path_event(result, StaticPathEvent(
        kind="command", cwd=incoming, raw=raw, context=context,
        dispatch_role="delegated", head=head, head_path=head_token,
        args=tuple(args),
        dynamic_args=_DYNAMIC_TOKEN in args, runtime_args=runtime_args,
    ))



def _path_classify_command(
    head: str | None, args: list[str], dynamic_args: bool,
    allow_functions: bool, raw: str, result: BashAnalysis,
    incoming: CwdDomain, functions: Functions, bindings: Bindings,
    external_bindings: Bindings, depth: int,
    call_stack: frozenset[str], context: tuple[str, ...],
    head_token: str | None = None,
) -> _CwdOutcome:
    shell_c = _shell_c_payload(args) if head in _SHELLS else (False, None)
    transparent_dispatch = (
        (allow_functions and head in functions)
        or (head == "builtin" and bool(args) and args[0] != _DYNAMIC_TOKEN)
        or (head == "eval" and bool(args) and not dynamic_args)
        or (
            head in _SHELLS
            and shell_c[0]
            and shell_c[1] not in {None, _DYNAMIC_TOKEN}
            and depth < _MAX_NESTING
        )
    )
    _append_path_event(result, StaticPathEvent(
        kind="command", cwd=incoming, raw=raw, context=context,
        dispatch_role="transparent" if transparent_dispatch else "direct",
        head=head, head_path=head_token, args=tuple(args),
        dynamic_args=dynamic_args,
    ))
    if head is None or head == _DYNAMIC_TOKEN:
        result.path_unverifiable = True
        unknown = _cwd_unknown(incoming)
        return _CwdOutcome(unknown, unknown)
    if head == "__hf_noexec__":
        return _cwd_unchanged(incoming)

    if allow_functions and head in functions:
        if head in call_stack or depth >= _MAX_NESTING:
            result.path_unverifiable = True
            unknown = _cwd_unknown(incoming)
            return _CwdOutcome(unknown, unknown)
        function_binding = functions[head]
        if function_binding.may_be_undefined:
            result.path_unverifiable = True
        outcomes: list[_CwdOutcome] = []
        binding_states: list[Bindings] = []
        function_states: list[Functions] = []
        for body, body_source in function_binding.bodies:
            call_bindings = dict(bindings)
            call_functions = dict(functions)
            for key in [key for key in call_bindings if key.isdigit()]:
                call_bindings.pop(key, None)
            for index, arg in enumerate(args, start=1):
                call_bindings[str(index)] = (
                    None if arg == _DYNAMIC_TOKEN else (arg,)
                )
            outcomes.append(_walk_path(
                body, body_source, result, incoming, call_functions,
                call_bindings, external_bindings, depth + 1,
                call_stack | {head}, context + (f"function:{head}",),
            ))
            binding_states.append(call_bindings)
            function_states.append(call_functions)
        if binding_states:
            _merge_bindings(bindings, binding_states)
            _merge_functions(functions, function_states)
        if not outcomes:
            result.path_unverifiable = True
            unknown = _cwd_unknown(incoming)
            return _CwdOutcome(unknown, unknown)
        return _CwdOutcome(
            _cwd_union(*(item.success for item in outcomes)),
            _cwd_union(*(item.failure for item in outcomes)),
            _cwd_union(*(item.terminated for item in outcomes)),
        )

    if head == "builtin":
        if not args or args[0] == _DYNAMIC_TOKEN:
            result.path_unverifiable = True
            unknown = _cwd_unknown(incoming)
            return _CwdOutcome(unknown, unknown)
        return _path_classify_command(
            _normal_head(args[0]), args[1:], _DYNAMIC_TOKEN in args[1:],
            False, raw, result, incoming, functions, bindings,
            external_bindings, depth, call_stack,
            context + ("builtin",),
            head_token=_head_path_token(args[0]),
        )
    if head == "cd":
        return _CwdOutcome(_cwd_success_after_cd(incoming, args), incoming)
    if head in {"pushd", "popd"}:
        result.path_unverifiable = True
        unknown = _cwd_unknown(incoming)
        return _CwdOutcome(unknown, unknown)
    if head == "shopt" and "lastpipe" in args:
        result.path_unverifiable = True
        unknown = _cwd_unknown(incoming)
        return _CwdOutcome(unknown, unknown)
    external_control = classify_external_control_command(head, args)
    if external_control != EXTERNAL_CONTROL_NONE:
        # 控制面命令不把远端路径伪装成本地写目标：query 只读放行；
        # external effect 会在更早的 static validity 与 route owner 契约拒绝。
        return _cwd_unchanged(incoming)
    if head in {"trap", "parallel"}:
        # These commands execute shell text later or outside this syntax tree.
        # Path authorization must fail closed instead of claiming no target.
        result.path_unverifiable = True
        return _cwd_unchanged(incoming)
    if head in {"true", ":"}:
        return _CwdOutcome(incoming, CwdDomain())
    if head == "false":
        return _CwdOutcome(CwdDomain(), incoming)
    if head == "exit":
        return _CwdOutcome(CwdDomain(), CwdDomain(), incoming)

    if head == "eval":
        if dynamic_args or _DYNAMIC_TOKEN in args:
            result.path_unverifiable = True
            unknown = _cwd_unknown(incoming)
            return _CwdOutcome(unknown, unknown)
        return _path_nested_payload(
            " ".join(args) if args else None, result, incoming, functions,
            bindings, external_bindings, depth, call_stack,
            context + ("eval",),
        )
    if head in {"source", "."}:
        result.path_unverifiable = True
        unknown = _cwd_unknown(incoming)
        return _CwdOutcome(unknown, unknown)

    if head == "xargs":
        target, target_args, unknown_target, target_token = _xargs_target(args)
        if target is None or unknown_target:
            result.path_unverifiable = True
        else:
            _path_synthetic_command(
                target, target_args, result, incoming,
                context + ("xargs",), raw, runtime_args=True,
                head_token=target_token,
            )
        return _cwd_unchanged(incoming)
    if head == "find":
        targets, unknown_target = _find_exec_targets(args)
        if unknown_target:
            _path_synthetic_command(
                None, [_DYNAMIC_TOKEN], result, incoming,
                context + ("find-exec",), raw, runtime_args=True,
            )
        for target, target_token, target_args in targets:
            _path_synthetic_command(
                target, target_args, result, incoming,
                context + ("find-exec",), raw,
                runtime_args=any("{}" in item for item in target_args),
                head_token=target_token,
            )
        return _cwd_unchanged(incoming)

    if head in _SHELLS:
        if _shell_is_nonexecuting(args):
            return _cwd_unchanged(incoming)
        has_c, payload = shell_c
        if has_c:
            _path_nested_payload(
                payload, result, incoming, {}, dict(external_bindings),
                external_bindings, depth, frozenset(),
                context + (f"shell-c:{head}",),
            )
        else:
            result.path_unverifiable = True
        return _cwd_unchanged(incoming)
    return _cwd_unchanged(incoming)


def _walk_path_command(
    node: Node, source: bytes, result: BashAnalysis,
    incoming: CwdDomain, functions: Functions, bindings: Bindings,
    external_bindings: Bindings, depth: int,
    call_stack: frozenset[str], context: tuple[str, ...],
) -> _CwdOutcome:
    _path_substitutions(
        node, source, result, incoming, functions, bindings,
        external_bindings, depth, call_stack,
        context + ("substitution",),
    )
    raw = _source(node, source).strip()
    heads, args, dynamic_args = _command_tokens(node, source, bindings)
    if heads is None:
        return _path_classify_command(
            None, args, True, False, raw, result, incoming, functions,
            bindings, external_bindings, depth, call_stack, context,
        )
    outcomes: list[_CwdOutcome] = []
    binding_states: list[Bindings] = []
    function_states: list[Functions] = []
    for candidate, raw_head in heads:
        command_bindings = dict(bindings)
        command_functions = dict(functions)
        head, unwrapped_args, allow_functions, head_token = _unwrap(
            candidate, list(args), raw_head)
        outcomes.append(_path_classify_command(
            head, unwrapped_args, dynamic_args, allow_functions, raw, result,
            incoming, command_functions, command_bindings,
            external_bindings, depth, call_stack, context,
            head_token=head_token,
        ))
        binding_states.append(command_bindings)
        function_states.append(command_functions)
    _merge_bindings(bindings, binding_states)
    _merge_functions(functions, function_states)
    return _CwdOutcome(
        _cwd_union(*(item.success for item in outcomes)),
        _cwd_union(*(item.failure for item in outcomes)),
        _cwd_union(*(item.terminated for item in outcomes)),
    )



def _walk_path(
    node: Node, source: bytes, result: BashAnalysis,
    incoming: CwdDomain, functions: Functions, bindings: Bindings,
    external_bindings: Bindings, depth: int,
    call_stack: frozenset[str], context: tuple[str, ...],
) -> _CwdOutcome:
    if node.type == "ERROR":
        result.parse_error = True
        unknown = _cwd_unknown(incoming)
        return _CwdOutcome(unknown, unknown)

    if node.type == "redirected_statement":
        for child in node.named_children:
            if child.type == "file_redirect":
                _record_path_redirect(
                    child, source, result, incoming, bindings, context)
                _path_substitutions(
                    child, source, result, incoming, functions, bindings,
                    external_bindings, depth, call_stack,
                    context + ("redirect-substitution",),
                )
        body = node.child_by_field_name("body")
        if body is None:
            return _cwd_unchanged(incoming)
        return _walk_path(
            body, source, result, incoming, functions, bindings,
            external_bindings, depth, call_stack, context,
        )

    if node.type == "file_redirect":
        _record_path_redirect(node, source, result, incoming, bindings, context)
        return _cwd_unchanged(incoming)

    if node.type in {"command_substitution", "process_substitution", "subshell"}:
        _path_sequence(
            list(node.named_children), source, result, incoming,
            dict(functions), dict(bindings), external_bindings, depth,
            call_stack, context + (node.type,),
        )
        return _cwd_unchanged(incoming)

    if node.type == "pipeline":
        for index, child in enumerate(node.named_children):
            _walk_path(
                child, source, result, incoming, dict(functions),
                dict(bindings), external_bindings, depth, call_stack,
                context + (f"pipeline:{index}",),
            )
        return _cwd_unchanged(incoming)

    if node.type == "list":
        children = list(node.named_children)
        if len(children) != 2:
            unknown = _cwd_unknown(incoming)
            _path_sequence(
                children, source, result, unknown, functions, bindings,
                external_bindings, depth, call_stack,
                context + ("conditional-list",),
            )
            return _CwdOutcome(unknown, unknown)
        operator = next(
            (child.type for child in node.children
             if not child.is_named and child.type in {"&&", "||"}), None)
        operator_label = operator or "unknown"
        left_bindings = dict(bindings)
        left_functions = dict(functions)
        left = _walk_path(
            children[0], source, result, incoming, left_functions,
            left_bindings, external_bindings, depth, call_stack,
            context + (f"list-left:{operator_label}",),
        )
        if operator == "&&":
            right_incoming = left.success
        elif operator == "||":
            right_incoming = left.failure
        else:
            right_incoming = _cwd_unknown(
                _cwd_union(left.success, left.failure))
        right_bindings = dict(left_bindings)
        right_functions = dict(left_functions)
        right = _walk_path(
            children[1], source, result, right_incoming, right_functions,
            right_bindings, external_bindings, depth, call_stack,
            context + (f"list-right:{operator_label}",),
        )
        _merge_bindings(bindings, [left_bindings, right_bindings])
        _merge_functions(functions, [left_functions, right_functions])
        if operator == "&&":
            return _CwdOutcome(
                right.success,
                _cwd_union(left.failure, right.failure),
                _cwd_union(left.terminated, right.terminated),
            )
        if operator == "||":
            return _CwdOutcome(
                _cwd_union(left.success, right.success), right.failure,
                _cwd_union(left.terminated, right.terminated),
            )
        unknown = _cwd_unknown(_cwd_union(
            left.success, left.failure, right.success, right.failure))
        return _CwdOutcome(
            unknown, unknown, _cwd_union(left.terminated, right.terminated))



    if node.type == "variable_assignment":
        _path_substitutions(
            node, source, result, incoming, functions, bindings,
            external_bindings, depth, call_stack,
            context + ("assignment-substitution",),
        )
        name_node = node.child_by_field_name("name")
        value_node = node.child_by_field_name("value")
        if name_node is None:
            result.path_unverifiable = True
        else:
            name = _source(name_node, source)
            bindings[name] = (
                ("",) if value_node is None
                else _resolved_values(value_node, source, bindings)
            )
        return _cwd_unchanged(incoming)

    if node.type == "function_definition":
        name_node = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        name = (
            _normal_head(_literal(name_node, source))
            if name_node is not None else None
        )
        if name is None or body is None:
            result.path_unverifiable = True
        else:
            functions[name] = FunctionBinding(((body, source),))
        return _CwdOutcome(incoming, CwdDomain())

    if node.type in {
        "for_statement", "while_statement", "until_statement",
        "if_statement", "elif_clause", "case_statement",
    }:
        uncertain = _cwd_unknown(incoming)
        _path_sequence(
            list(node.named_children), source, result, uncertain,
            dict(functions), dict(bindings), external_bindings, depth,
            call_stack, context + (f"uncertain-flow:{node.type}",),
        )
        return _CwdOutcome(uncertain, uncertain)

    if node.type == "command":
        return _walk_path_command(
            node, source, result, incoming, functions, bindings,
            external_bindings, depth, call_stack, context,
        )

    return _path_sequence(
        [child for child in node.named_children if child.type != "comment"],
        source, result, incoming, functions, bindings, external_bindings,
        depth, call_stack, context,
    )


def analyzer_unavailable_reason() -> str | None:
    return _ANALYZER_UNAVAILABLE


def analyze_bash(
    command: str, *, _depth: int = 0,
    initial_cwd: str | None = None,
    external_bindings: Bindings | None = None,
) -> BashAnalysis:
    if _PARSER is None:
        return BashAnalysis(
            analyzer_unavailable=_ANALYZER_UNAVAILABLE or "parser_unavailable")
    source = (command or "").encode("utf-8")
    tree = _PARSER.parse(source)
    result = BashAnalysis(parse_error=tree.root_node.has_error)
    _walk(tree.root_node, source, result, _depth, {}, frozenset(), {})
    inherited = _normalized_external_bindings(external_bindings)
    incoming = (
        CwdDomain((os.path.abspath(initial_cwd),))
        if initial_cwd is not None else CwdDomain(unknown=True)
    )
    _walk_path(
        tree.root_node, source, result, incoming, {}, dict(inherited),
        inherited, _depth, frozenset(), ("root",),
    )
    return result
