"""Compatibility entrypoint for the data-node custom loop.

The framework currently discovers custom loops only at
``nodes/<node_type>/agent_loop.py``.  Keep the implementation in
``data_agent_loop.py`` while preserving that discovery contract.
"""
from __future__ import annotations

from nodes.data.data_agent_loop import run_loop

__all__ = ["run_loop"]
