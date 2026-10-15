"""Sub-agents: named autonomous workers APEX can create, manage and delegate to.

A sub-agent is an operator-owned (or agent-created) record with its own model,
workspace directory and tool set. Delegating a task runs a normal agent turn in
the sub-agent's own conversation and workspace; the sub-agent's report is handed
back to APEX, which composes the chat/spoken answer for the operator.

See :mod:`subagents.manager` (CRUD + run orchestration), :mod:`subagents.runner`
(the turn itself), :mod:`subagents.routes` (the AGENTS tab REST surface) and
:mod:`tools.subagent_tools` (the model-facing tools).
"""
from .manager import SubAgentError, SubAgentManager, get_manager, subagent_context_block

__all__ = ["SubAgentManager", "SubAgentError", "get_manager", "subagent_context_block"]
