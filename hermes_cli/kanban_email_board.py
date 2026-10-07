"""Approved D3 connection adapter and pure path-to-board resolution.

Based on t_c3770fe1 attachment 361 (resolver_reference_v2.py). Only
board_snapshot reads deployment state; resolve_board_slug uses its arguments.
"""
import re
from pathlib import PurePath
from typing import Optional

_BOARD_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def resolve_board_slug(main_file: Optional[str], root: str) -> Optional[str]:
    """Approved R1–R5: one connection file maps to one slug or None."""
    if main_file is None:
        return None
    p = PurePath(main_file)
    r = PurePath(root)
    if p.name != "kanban.db":
        return None
    boards = r / "kanban" / "boards"
    if p.parent.parent == boards:
        slug = p.parent.name
        return slug if _BOARD_SLUG_RE.match(slug) else None
    if p.parent == r:
        return "default"
    return None


def board_snapshot(conn) -> tuple[Optional[str], str]:
    """Normalize one home snapshot and the opened connection's actual main file."""
    from pathlib import Path
    from hermes_cli.kanban_db import kanban_home
    from hermes_cli.kanban_db_connect import _main_db_file
    root = str(Path(kanban_home()).resolve())
    mf = _main_db_file(conn)
    return (str(Path(mf).resolve()) if mf else None), root
