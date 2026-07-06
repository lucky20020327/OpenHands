"""Bridge to the public ``llm`` custom LiteLLM providers.

``llm`` ships company-specific LiteLLM *custom providers* (``ichat`` /
``taiji`` / ``tcloud``). Registering them with the process-global
``litellm`` instance lets OpenHands drive models such as ``tcloud/glm-5.1``,
``ichat/chat:gpt-5`` and ``taiji/<model>`` through the very same
``litellm.completion`` / ``litellm.acompletion`` calls the agent already uses —
no other OpenHands code needs to change.

The clarify package lives in a *separate* repository and is intentionally
optional. Every entry point in this module therefore degrades to a safe no-op
when clarify cannot be imported, so a default OpenHands install is completely
unaffected.

Resolution order for the clarify repository root:

1. ``CLARIFY_REPO_PATH`` environment variable (explicit opt-in).
2. Walking up the directory tree from this file (works when the OpenHands
   checkout lives under the clarify repo, e.g. ``<clarify>/external/OpenHands``).
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

# LiteLLM model-id prefixes owned by the clarify custom providers.
CLARIFY_PROVIDER_PREFIXES: tuple[str, ...] = ('ichat/', 'taiji/', 'tcloud/')
CLARIFY_ANTHROPIC_BASE_URL = 'http://ichat.woa.com/api/claude'
CLARIFY_OPENAI_CHAT_CAPABILITY_MODEL = 'openai/gpt-4.1'

_ANTHROPIC_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r'.*\bclaude-3-.*'),
    re.compile(r'.*\bclaude-.*-4.*'),
    re.compile(r'.*\bclaude\b.*'),
)

_OPENAI_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r'.*\bgpt-.*'),
    re.compile(r'.*\bo[1-9].*'),
)

# Static fallback so the model ids can still surface in discovery even when the
# clarify repo is not importable from this process.
_FALLBACK_CLARIFY_MODELS: list[str] = [
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

# Env var that tells a freshly spawned interpreter (e.g. the agent-server
# subprocess) to auto-register the clarify providers on startup.
AUTO_SETUP_ENV = 'CLARIFY_LLM_AUTO_SETUP'
REPO_PATH_ENV = 'CLARIFY_REPO_PATH'

# Module used to wrap the agent-server entrypoint so the subprocess registers
# the clarify providers before importing/running the real agent server.
_AGENT_LAUNCHER_MODULE = 'openhands.app_server.utils.clarify_agent_launcher'

_setup_lock = threading.Lock()
_setup_done = False
_setup_ok = False
_located_root: str | None = None


def _candidate_repo_roots() -> list[Path]:
    """Return possible clarify repo roots, most-specific first."""
    roots: list[Path] = []

    env_path = os.getenv('CLARIFY_REPO_PATH')
    if env_path:
        roots.append(Path(env_path).expanduser())

    # Walk up from this file: the OpenHands checkout may live inside the
    # clarify repo (e.g. ``<clarify>/external/OpenHands/...``).
    for parent in Path(__file__).resolve().parents:
        roots.append(parent)

    # De-duplicate while preserving order.
    seen: set[str] = set()
    unique: list[Path] = []
    for root in roots:
        key = str(root)
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def locate_clarify_repo() -> str | None:
    """Return the clarify repo root containing the public ``llm`` package.

    The result is cached after the first successful lookup.
    """
    global _located_root
    if _located_root is not None:
        return _located_root
    for root in _candidate_repo_roots():
        if (root / _RELATIVE_MARKER).is_file():
            _located_root = str(root)
            return _located_root
    return None


def _ensure_clarify_importable() -> bool:
    """Make the public ``llm`` package importable, adding the repo root to path."""
    try:
        import llm  # noqa: F401

        return True
    except Exception:  # noqa: BLE001 - probe import; fall back to path search
        pass

    root = locate_clarify_repo()
    if root is None:
        return False
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        import llm  # noqa: F401

        logger.info('[clarify-llm] located clarify repo at %s', root)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(
            '[clarify-llm] found clarify at %s but import failed: %s', root, e
        )
        return False


def setup_clarify_llm(debug: bool = False) -> bool:
    """Register the clarify custom LiteLLM providers (idempotent, fail-soft).

    Returns ``True`` when the providers are registered with ``litellm`` and
    ``False`` when clarify is unavailable (in which case OpenHands keeps using
    only its built-in providers).
    """
    global _setup_done, _setup_ok

    if _setup_done:
        return _setup_ok

    with _setup_lock:
        if _setup_done:
            return _setup_ok

        try:
            if not _ensure_clarify_importable():
                logger.info(
                    '[clarify-llm] clarify package not found; '
                    'set CLARIFY_REPO_PATH to enable ichat/taiji/tcloud models'
                )
                _setup_ok = False
            else:
                import llm as clarify_llm

                clarify_llm.litellm_setup(debug=debug)
                logger.info(
                    '[clarify-llm] registered custom LiteLLM providers: %s',
                    ', '.join(p.rstrip('/') for p in CLARIFY_PROVIDER_PREFIXES),
                )
                _setup_ok = True
        except Exception as e:  # noqa: BLE001 - never break OpenHands startup
            logger.warning('[clarify-llm] provider registration failed: %s', e)
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


def normalize_clarify_model_id(model: str | None) -> str | None:
    """Return the LiteLLM model id OpenHands should use for a Clarify model.

    ``custom_anthropic`` / ``custom_openai`` are ADK model wrappers, while
    OpenHands calls LiteLLM directly. This mirrors Clarify's ADK routing policy
    in LiteLLM terms without importing ADK-only classes.
    """
    if not model:
        return model
    model = model.strip()
    if not model:
        return model

    if _is_anthropic_model_id(model):
        return f'anthropic/{_normalize_anthropic_tail(model)}'
    if model.startswith(CLARIFY_PROVIDER_PREFIXES):
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
        # Catalogue access is best-effort; keep unknown ids unchanged.
        pass

    return model


def apply_clarify_llm_overrides(llm: Any, requested_model: str | None = None) -> Any:
    """Return an OpenHands SDK ``LLM`` configured for Clarify providers."""
    setup_clarify_llm()

    source_model = requested_model or getattr(llm, 'model', None)
    normalized_model = normalize_clarify_model_id(source_model)
    if not normalized_model:
        return llm

    update: dict[str, Any] = {}
    if normalized_model != getattr(llm, 'model', None):
        update['model'] = normalized_model

    if normalized_model.startswith('anthropic/') and _is_anthropic_model_id(
        source_model or ''
    ):
        update['base_url'] = os.getenv(
            'CLARIFY_ANTHROPIC_BASE_URL',
            CLARIFY_ANTHROPIC_BASE_URL,
        )
        if not getattr(llm, 'api_key', None) and os.getenv('ANTHROPIC_API_KEY'):
            update['api_key'] = SecretStr(os.getenv('ANTHROPIC_API_KEY') or '')
        update['model_canonical_name'] = normalized_model
    elif normalized_model.startswith('ichat/chat:'):
        # iChat chat models whose tail contains "gpt-5" must not be routed
        # through OpenHands' Responses API path.
        update['model_canonical_name'] = CLARIFY_OPENAI_CHAT_CAPABILITY_MODEL

    if not update:
        return llm

    logger.info(
        '[clarify-llm] mapped OpenHands model %s -> %s',
        source_model,
        normalized_model,
    )
    return llm.model_copy(update=update)


def is_clarify_model(model: str | None) -> bool:
    """Return ``True`` when *model* targets a clarify custom provider."""
    if not model:
        return False
    normalized = normalize_clarify_model_id(model)
    return bool(
        normalized
        and (
            normalized.startswith(CLARIFY_PROVIDER_PREFIXES)
            or (
                normalized.startswith('anthropic/')
                and _is_anthropic_model_id(model)
            )
        )
    )


def get_clarify_models() -> list[str]:
    """Return the ``provider/model`` ids served by the clarify providers.

    Best-effort registers the providers first so that simply listing the models
    also wires them into ``litellm``. Returns an empty list when the clarify
    repo is not importable, so a default OpenHands install is unaffected.
    """
    if not setup_clarify_llm():
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
        # iChat is routed through its ``chat`` API (``ichat/chat:<model>``).
        models += [f'ichat/chat:{name}' for name in ichat_models]
        return models
    except Exception as e:  # noqa: BLE001
        logger.warning('[clarify-llm] failed to read clarify model catalogue: %s', e)
        return list(_FALLBACK_CLARIFY_MODELS)


def prepare_agent_server_command(
    *,
    python_executable: str,
    agent_server_module: str,
    extra_args: list[str],
    env: dict[str, str],
) -> tuple[list[str], dict[str, str]]:
    """Build the agent-server launch command, wiring clarify when available.

    The agent server runs in a *separate* process and makes the real
    ``litellm.completion`` calls, so the clarify providers must be registered
    inside that subprocess too. When the clarify repo can be located, the
    command is wrapped with :mod:`clarify_agent_launcher`, which registers the
    providers and then runs the real agent-server module unchanged. The
    subprocess also receives ``CLARIFY_REPO_PATH`` / ``CLARIFY_LLM_AUTO_SETUP``
    so it can find and enable clarify.

    When clarify is unavailable this returns the original, unmodified command
    so default OpenHands behaviour is preserved.

    Returns the ``(cmd, env)`` pair to spawn.
    """
    base_cmd = [python_executable, '-m', agent_server_module, *extra_args]

    repo_root = locate_clarify_repo()
    if repo_root is None:
        return base_cmd, env

    env = dict(env)
    env[REPO_PATH_ENV] = repo_root
    env[AUTO_SETUP_ENV] = '1'
    # Make sure the subprocess can import clarify even with a sandbox cwd.
    existing_pythonpath = env.get('PYTHONPATH', '')
    parts = [repo_root] + (existing_pythonpath.split(os.pathsep) if existing_pythonpath else [])
    env['PYTHONPATH'] = os.pathsep.join(dict.fromkeys(p for p in parts if p))

    wrapped_cmd = [
        python_executable,
        '-m',
        _AGENT_LAUNCHER_MODULE,
        '--clarify-agent-server-module',
        agent_server_module,
        '--',
        *extra_args,
    ]
    logger.info(
        '[clarify-llm] wrapping agent-server launch with clarify provider setup'
    )
    return wrapped_cmd, env
