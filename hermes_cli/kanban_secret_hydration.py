"""Bounded, credential-scoped recovery for failed Kanban worker secret hydration.

A blocked card is probed without starting a worker. Only the reference that failed
is read, and its value is discarded; neither credentials nor values enter the DB.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

# A generic worker mention of 1Password is not evidence of a startup failure.
_HYDRATION_ERROR = re.compile(
    r"(?:1password:\s*op read failed|op read (?:failed|timed out|exited)|"
    r"could not read secret)", re.I,
)
_REFERENCE = re.compile(r"op://[^\s'\"<>]+")
INITIAL_DELAY = 300
MAX_DELAY = 8 * 3600
MAX_AGE = 24 * 3600


def failed_reference(output: str) -> str | None:
    """Return a failed op reference, empty when the error omits it, or None for no evidence."""
    match = _HYDRATION_ERROR.search(output)
    if not match:
        return None
    # The CLI's diagnostic puts the reference immediately after the error.
    ref = _REFERENCE.search(output[match.end():match.end() + 300])
    return ref.group(0).rstrip(".,:;") if ref else ""


def _profile_config(assignee: str):
    from hermes_cli.profiles import normalize_profile_name, resolve_profile_env
    from hermes_cli.env_loader import _load_secrets_config

    home = Path(resolve_profile_env(normalize_profile_name(assignee)))
    cfg = _load_secrets_config(home)
    op = cfg.get("onepassword") or {}
    return home, op if isinstance(op, dict) else {}


def configured_secret_name(assignee: str, reference: str) -> str | None:
    """Persist an env-var name, not a vault path or a secret value."""
    try:
        _, cfg = _profile_config(assignee)
        refs = cfg.get("env") or {}
        if not isinstance(refs, dict):
            return None
        if not reference and len(refs) == 1:
            return next(iter(refs))
        return next((name for name, ref in refs.items() if ref == reference), None)
    except (OSError, ValueError, TypeError):
        return None


def probe(assignee: str, secret_name: str) -> bool:
    """True only after the SAME configured read succeeds (or source is removed).

    Use the profile's own bootstrap files, never the dispatcher's inherited token.
    One op read per backoff interval; the returned plaintext is not retained.
    """
    from agent.secret_scope import load_env_file
    from agent.secret_sources.base import set_source_environment, reset_source_environment
    from agent.secret_sources.onepassword import _run_op_read, find_op

    try:
        home, cfg = _profile_config(assignee)
        if not cfg.get("enabled") or secret_name not in (cfg.get("env") or {}):
            return True  # configuration changed; let the normal worker validate it
        reference = cfg["env"][secret_name]
        env = {key: os.environ[key] for key in ("PATH", "HOME", "USERPROFILE", "APPDATA",
               "LOCALAPPDATA", "SystemRoot", "TMPDIR", "TMP", "TEMP") if key in os.environ}
        for file in (home / ".op.env", home / ".env"):
            if file.exists():
                env.update(load_env_file(file))
        token_name = cfg.get("service_account_token_env") or "OP_SERVICE_ACCOUNT_TOKEN"
        binary = find_op(str(cfg.get("binary_path") or ""))
        if binary is None:
            return False
        token = set_source_environment(env)
        try:
            _run_op_read(binary, reference, account=str(cfg.get("account") or ""),
                         token_value=env.get(token_name, ""))
        finally:
            reset_source_environment(token)
        return True
    except (OSError, RuntimeError, ValueError, TypeError):
        return False


def next_delay(attempt: int) -> int:
    return min(MAX_DELAY, INITIAL_DELAY * 2 ** min(attempt, 8))
