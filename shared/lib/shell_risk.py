"""Project executable shell positions for risk matching; unknown syntax stays conservative.

This projection grants no filesystem or process capability. It only separates
literal report data from executed commands; the original payload still reaches
the independent scope checker and execution boundary unchanged.
"""

from __future__ import annotations

import ast
import shlex
from functools import lru_cache
from pathlib import PurePosixPath


@lru_cache(maxsize=1)
def _language():
    import tree_sitter_bash
    from tree_sitter import Language

    return Language(tree_sitter_bash.language())


UNPARSED = object()


def python_report_data(source: str, *, syntax_tree=UNPARSED) -> bool:
    try:
        tree = ast.parse(source) if syntax_tree is UNPARSED else syntax_tree
    except (SyntaxError, ValueError, RecursionError):
        return False
    if not isinstance(tree, ast.AST):
        return False
    forms = (
        ast.Module,
        ast.Expr,
        ast.Assign,
        ast.Name,
        ast.Load,
        ast.Store,
        ast.Import,
        ast.alias,
        ast.Dict,
        ast.List,
        ast.Tuple,
        ast.Constant,
        ast.BinOp,
        ast.Add,
        ast.Call,
        ast.Attribute,
        ast.keyword,
    )
    for node in ast.walk(tree):
        if not isinstance(node, forms):
            return False
        if isinstance(node, ast.Import) and any(a.name != "json" or a.asname for a in node.names):
            return False
        if isinstance(node, ast.Assign) and any(
            not isinstance(t, ast.Name) or t.id in {"open", "print", "repr", "json"}
            for t in node.targets
        ):
            return False
        if isinstance(node, ast.Attribute) and not (
            isinstance(node.value, ast.Name)
            and node.value.id == "json"
            and node.attr in {"dump", "dumps"}
        ):
            return False
        if isinstance(node, ast.Call) and not (
            isinstance(node.func, ast.Name)
            and node.func.id in {"open", "print", "repr"}
            or isinstance(node.func, ast.Attribute)
        ):
            return False
    return True


def execution_fragments(text: str) -> tuple[str, ...]:
    return _project(text, 0)[0]


def _project(text: str, depth: int) -> tuple[tuple[str, ...], bool]:
    if depth >= 8:
        return (text,), False
    try:
        from tree_sitter import Parser

        source = text.encode("utf-8")
        tree = Parser(_language()).parse(source)
    except (ImportError, ValueError, TypeError, RuntimeError, RecursionError):
        return (text,), False
    if tree.root_node.has_error:
        return (text,), False
    fragments: list[str] = []
    complete = True
    shells = {"sh", "bash", "dash", "zsh", "ksh"}
    data_commands = {
        "echo",
        "printf",
        "cat",
        "head",
        "tail",
        "tee",
        "wc",
        "sha256sum",
        "sha512sum",
        "md5sum",
    }

    def raw(node):
        return source[node.start_byte : node.end_byte].decode("utf-8")

    def words(node):
        try:
            return shlex.split(raw(node), comments=True)
        except ValueError:
            return []

    def known_command(tokens):
        if not tokens:
            return ""
        executable = tokens[0]
        if "/" not in executable:
            return executable
        path = PurePosixPath(executable)
        return path.name if str(path.parent) in {"/bin", "/usr/bin"} else ""

    def dynamic(node):
        return node.type in {
            "expansion",
            "simple_expansion",
            "command_substitution",
            "process_substitution",
            "arithmetic_expansion",
        } or any(dynamic(child) for child in node.named_children)

    def walk(node):
        nonlocal complete
        if node.type == "function_definition":
            complete = False
            return
        if node.type == "pipeline":
            for child in node.named_children:
                tokens = words(child)
                if known_command(tokens) not in data_commands:
                    complete = False
                    fragments.append(" ".join(words(node)))
                    return
        if node.type == "redirected_statement":
            command = next(
                (child for child in node.named_children if child.type == "command"), None
            )
            tokens = words(command) if command is not None else []
            here = next(
                (
                    child
                    for child in node.named_children
                    if child.type in {"heredoc_redirect", "herestring_redirect"}
                ),
                None,
            )
            if here is not None and tokens and known_command(tokens) not in data_commands:
                body = next(
                    (raw(child) for child in here.named_children if child.type == "heredoc_body"),
                    "",
                )
                if not (
                    known_command(tokens) in {"python", "python3", "python2"}
                    and body
                    and python_report_data(body)
                ):
                    complete = False
                    return
        if node.type == "command":
            tokens = words(node)
            head = known_command(tokens)
            if head in data_commands:
                pass  # argv is data; substitutions below still execute
            elif head in shells and not dynamic(node):
                flag = next(
                    (
                        i
                        for i, word in enumerate(tokens[1:], 1)
                        if word.startswith("-") and "c" in word
                    ),
                    None,
                )
                if flag is not None and flag + 2 == len(tokens):
                    nested, understood = _project(tokens[flag + 1], depth + 1)
                    fragments.extend(nested)
                    complete = complete and understood
                else:
                    complete = False
            elif (
                head in {"python", "python3", "python2"}
                and len(tokens) == 3
                and tokens[1] == "-c"
                and not dynamic(node)
                and python_report_data(tokens[2])
            ):
                pass
            elif (
                head in {"python", "python3", "python2"}
                and tokens[1:] in ([], ["-"])
                and node.parent.type == "redirected_statement"
                and any(child.type == "heredoc_redirect" for child in node.parent.named_children)
            ):
                pass  # the enclosing here-document was checked above
            else:
                complete = False
                fragments.append(" ".join(tokens) if tokens and not dynamic(node) else raw(node))
        elif node.type == "file_redirect":
            fragments.append(raw(node))
            if dynamic(node):
                complete = False
        for child in node.named_children:
            walk(child)

    try:
        walk(tree.root_node)
    except RecursionError:
        return (text,), False
    if not complete:
        fragments.append(text)
    return tuple(fragments), complete
