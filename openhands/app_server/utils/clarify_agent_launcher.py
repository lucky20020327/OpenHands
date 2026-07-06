"""Agent-server entrypoint wrapper that enables the clarify LLM providers.

The OpenHands agent server runs in its own process and is what actually issues
``litellm.completion`` calls. To let it drive the clarify custom providers
(``ichat`` / ``taiji`` / ``tcloud``), this thin wrapper registers them *before*
handing control to the real agent-server module, which then runs completely
unchanged.

Usage (constructed by
:func:`openhands.app_server.utils.clarify_llm.prepare_agent_server_command`)::

    python -m openhands.app_server.utils.clarify_agent_launcher \
        --clarify-agent-server-module openhands.agent_server -- --port 8000

Everything after ``--`` is forwarded verbatim to the wrapped module as its
``sys.argv`` tail, so the agent server sees exactly the arguments it would have
received from a direct ``python -m openhands.agent_server`` invocation.

Registration is fully fail-soft: if clarify cannot be imported the wrapper just
runs the agent server as usual.
"""

from __future__ import annotations

import runpy
import sys


def _split_args(argv: list[str]) -> tuple[str, list[str]]:
    """Return ``(agent_server_module, forwarded_args)`` from *argv*."""
    module = 'openhands.agent_server'
    forwarded: list[str] = []

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == '--clarify-agent-server-module':
            if i + 1 < len(argv):
                module = argv[i + 1]
                i += 2
                continue
            i += 1
            continue
        if arg == '--':
            forwarded = argv[i + 1 :]
            break
        i += 1

    return module, forwarded


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    module, forwarded = _split_args(argv)

    try:
        from openhands.app_server.utils.clarify_llm import setup_clarify_llm

        setup_clarify_llm()
    except Exception:  # noqa: BLE001 - never block the agent server from starting
        pass
    try:
        from openhands.clarify.tools import register_clarify_all

        register_clarify_all()
    except Exception:  # noqa: BLE001 - never block the agent server from starting
        pass

    # Reconstruct argv so the wrapped module behaves exactly like ``-m module``.
    sys.argv = [module, *forwarded]
    runpy.run_module(module, run_name='__main__', alter_sys=True)


if __name__ == '__main__':
    main()
