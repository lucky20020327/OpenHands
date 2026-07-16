"""Agent-server entrypoint wrapper that enables custom LiteLLM providers.

The OpenHands agent server runs in its own process and issues the real
``litellm.completion`` calls. This wrapper registers ichat/taiji/tcloud
providers before handing control to the real agent-server module.
"""

from __future__ import annotations

import runpy
import sys


def _split_args(argv: list[str]) -> tuple[str, list[str]]:
    module = 'openhands.agent_server'
    forwarded: list[str] = []

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == '--custom-llm-agent-server-module':
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
        from openhands.app_server.utils.custom_llm import setup_custom_llm

        setup_custom_llm()
    except Exception:  # noqa: BLE001
        pass

    sys.argv = [module, *forwarded]
    runpy.run_module(module, run_name='__main__', alter_sys=True)


if __name__ == '__main__':
    main()
