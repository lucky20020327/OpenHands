"""Bridge to CodeGraph's public ``llm`` custom LiteLLM providers.

``llm`` ships company-specific LiteLLM *custom providers* (``ichat`` /
``taiji`` / ``tcloud``). Registering them with the process-global
``litellm`` instance lets OpenHands drive models such as ``tcloud/glm-5.1``,
``ichat/chat:gpt-5`` and ``taiji/<model>`` through the same
``litellm.completion`` / ``litellm.acompletion`` calls the agent already uses.

The ``llm`` package lives outside the OpenHands tree and is optional. Every
entry point here degrades to a safe no-op when it cannot be imported.

Resolution order for the repo root that contains ``llm/``:

1. ``CUSTOM_LLM_REPO_PATH`` / ``CODEGRAPH_REPO_PATH`` / ``CLARIFY_REPO_PATH``
2. Walking up from this file (works when OpenHands lives under
   ``<repo>/external/OpenHands``).
"""

from __future__ import annotations

import os
import re
import sys
import threading
from pathlib import Path
from typing import Any

from openhands.app_server.utils.logger import openhands_logger as logger
from pydantic import SecretStr

CUSTOM_PROVIDER_PREFIXES: tuple[str, ...] = ('ichat/', 'taiji/', 'tcloud/')
CUSTOM_ANTHROPIC_BASE_URL = 'http://ichat.woa.com/api/claude'
OPENAI_CHAT_CAPABILITY_MODEL = 'openai/gpt-4.1'

_ANTHROPIC_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r'.*\bclaude-3-.*'),
    re.compile(r'.*\bclaude-.*-4.*'),
    re.compile(r'.*\bclaude\b.*'),
)

_OPENAI_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r'.*\bgpt-.*'),
    re.compile(r'.*\bo[1-9].*'),
)

_FALLBACK_CUSTOM_MODELS: list[str] = [
    'bedrocknd-claude-opus-4-6',
    'claude-opus-4-6',
    'tcloud/glm-5.1',
    'tcloud/minimax-m2.7',
    'tcloud/kimi-k2.6',
    'ichat/chat:gpt-5',
    'ichat/chat:gpt-5.1',
    'ichat/chat:gemini-2.5-pro',
]

_RELATIVE_MARKER = Path('llm') / '__init__.py'

AUTO_SETUP_ENV = 'CUSTOM_LLM_AUTO_SETUP'
REPO_PATH_ENVS = (
    'CUSTOM_LLM_REPO_PATH',
    'CODEGRAPH_REPO_PATH',
    'CLARIFY_REPO_PATH',
)

_AGENT_LAUNCHER_MODULE = 'openhands.app_server.utils.custom_llm_agent_launcher'

_setup_lock = threading.Lock()
_setup_done = False
_setup_ok = False
_located_root: str | None = None


def _candidate_repo_roots() -> list[Path]:
    roots: list[Path] = []
    for env_name in REPO_PATH_ENVS:
        env_path = os.getenv(env_name)
        if env_path:
            roots.append(Path(env_path).expanduser())

    for parent in Path(__file__).resolve().parents:
        roots.append(parent)

    seen: set[str] = set()
    unique: list[Path] = []
    for root in roots:
        key = str(root)
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def locate_custom_llm_repo() -> str | None:
    """Return the repo root containing the public ``llm`` package."""
    global _located_root
    if _located_root is not None:
        return _located_root
    for root in _candidate_repo_roots():
        if (root / _RELATIVE_MARKER).is_file():
            _located_root = str(root)
            return _located_root
    return None


def _ensure_custom_llm_importable() -> bool:
    try:
        import llm  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        pass

    root = locate_custom_llm_repo()
    if root is None:
        return False
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        import llm  # noqa: F401

        logger.info('[custom-llm] located llm package at %s', root)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning('[custom-llm] found repo at %s but import failed: %s', root, e)
        return False


def setup_custom_llm(debug: bool = False) -> bool:
    """Register custom LiteLLM providers (idempotent, fail-soft)."""
    global _setup_done, _setup_ok

    if _setup_done:
        return _setup_ok

    with _setup_lock:
        if _setup_done:
            return _setup_ok

        try:
            if not _ensure_custom_llm_importable():
                logger.info(
                    '[custom-llm] llm package not found; set CUSTOM_LLM_REPO_PATH '
                    'to enable ichat/taiji/tcloud models'
                )
                _setup_ok = False
            else:
                import llm as custom_llm

                custom_llm.litellm_setup(debug=debug)
                logger.info(
                    '[custom-llm] registered providers: %s',
                    ', '.join(p.rstrip('/') for p in CUSTOM_PROVIDER_PREFIXES),
                )
                _setup_ok = True
        except Exception as e:  # noqa: BLE001
            logger.warning('[custom-llm] provider registration failed: %s', e)
            _setup_ok = False
        finally:
            _setup_done = True

    return _setup_ok


def _has_provider_prefix(model: str) -> bool:
    return '/' in model


def _is_anthropic_model_id(model: str) -> bool:
    return any(pattern.match(model) for pattern in _ANTHROPIC_PATTERNS)


def _is_openai_model_id(model: str) -> bool:
    return any(pattern.match(model) for pattern in _OPENAI_PATTERNS)


def _normalize_anthropic_tail(model: str) -> str:
    return model.rsplit('/', 1)[-1].rsplit(':', 1)[-1].strip() or model


def normalize_custom_model_id(model: str | None) -> str | None:
    """Return the LiteLLM model id OpenHands should use."""
    if not model:
        return model
    model = model.strip()
    if not model:
        return model

    if _is_anthropic_model_id(model):
        return f'anthropic/{_normalize_anthropic_tail(model)}'
    if model.startswith(CUSTOM_PROVIDER_PREFIXES):
        return model
    if _is_openai_model_id(model) and not _has_provider_prefix(model):
        return f'ichat/chat:{model}'

    try:
        from llm.api_config import (
            ichat_models,
            taiji_models,
            tcloud_models,
        )

        if model in tcloud_models:
            return f'tcloud/{model}'
        if model in taiji_models:
            return f'taiji/{model}'
        if model in ichat_models:
            return f'ichat/chat:{model}'
    except Exception:
        pass

    return model


def apply_custom_llm_overrides(llm: Any, requested_model: str | None = None) -> Any:
    """Return an OpenHands SDK ``LLM`` configured for custom providers."""
    setup_custom_llm()

    source_model = requested_model or getattr(llm, 'model', None)
    normalized_model = normalize_custom_model_id(source_model)
    if not normalized_model:
        return llm

    update: dict[str, Any] = {}
    if normalized_model != getattr(llm, 'model', None):
        update['model'] = normalized_model

    if normalized_model.startswith('anthropic/') and _is_anthropic_model_id(
        source_model or ''
    ):
        update['base_url'] = os.getenv(
            'CUSTOM_ANTHROPIC_BASE_URL',
            os.getenv('CLARIFY_ANTHROPIC_BASE_URL', CUSTOM_ANTHROPIC_BASE_URL),
        )
        if not getattr(llm, 'api_key', None) and os.getenv('ANTHROPIC_API_KEY'):
            update['api_key'] = SecretStr(os.getenv('ANTHROPIC_API_KEY') or '')
        update['model_canonical_name'] = normalized_model
    elif normalized_model.startswith('ichat/chat:'):
        # Avoid OpenHands Responses-API routing for gpt-5-style iChat models.
        update['model_canonical_name'] = OPENAI_CHAT_CAPABILITY_MODEL

    if not update:
        return llm

    logger.info(
        '[custom-llm] mapped OpenHands model %s -> %s',
        source_model,
        normalized_model,
    )
    return llm.model_copy(update=update)


def get_custom_llm_models() -> list[str]:
    """Return ``provider/model`` ids served by the custom providers."""
    if not setup_custom_llm():
        return []

    try:
        from llm.api_config import (
            ichat_models,
            taiji_models,
            tcloud_models,
        )

        models = ['bedrocknd-claude-opus-4-6', 'claude-opus-4-6']
        models += [f'tcloud/{name}' for name in tcloud_models]
        models += [f'taiji/{name}' for name in taiji_models]
        models += [f'ichat/chat:{name}' for name in ichat_models]
        return models
    except Exception as e:  # noqa: BLE001
        logger.warning('[custom-llm] failed to read model catalogue: %s', e)
        return list(_FALLBACK_CUSTOM_MODELS)


def prepare_agent_server_command(
    *,
    python_executable: str,
    agent_server_module: str,
    extra_args: list[str],
    env: dict[str, str],
) -> tuple[list[str], dict[str, str]]:
    """Build the agent-server launch command, wiring custom LLM when available."""
    base_cmd = [python_executable, '-m', agent_server_module, *extra_args]

    repo_root = locate_custom_llm_repo()
    if repo_root is None:
        return base_cmd, env

    env = dict(env)
    env['CUSTOM_LLM_REPO_PATH'] = repo_root
    env[AUTO_SETUP_ENV] = '1'
    existing_pythonpath = env.get('PYTHONPATH', '')
    parts = [repo_root] + (
        existing_pythonpath.split(os.pathsep) if existing_pythonpath else []
    )
    env['PYTHONPATH'] = os.pathsep.join(dict.fromkeys(p for p in parts if p))

    wrapped_cmd = [
        python_executable,
        '-m',
        _AGENT_LAUNCHER_MODULE,
        '--custom-llm-agent-server-module',
        agent_server_module,
        '--',
        *extra_args,
    ]
    logger.info('[custom-llm] wrapping agent-server launch with provider setup')
    return wrapped_cmd, env
