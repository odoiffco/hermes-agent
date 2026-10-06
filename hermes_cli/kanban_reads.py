"""Bounded reporting reads shared by the Kanban CLI and model tools."""
from __future__ import annotations

import json

COMMENT_CHUNK_CHARS = 4000
BLOCK_REASON_CHARS = 500


def block_fields(conn, task):
    """Append-only row metadata; historical block events are labelled as such."""
    row = conn.execute(
        "SELECT id, kind, payload, created_at FROM task_events "
        "WHERE task_id = ? AND kind IN ('blocked', 'dependency_wait', 'block_loop_detected') "
        "ORDER BY id DESC LIMIT 1",
        (task.id,),
    ).fetchone()
    latest = None
    if row:
        payload = json.loads(row['payload']) if row['payload'] else {}
        reason = payload.get('reason') or ''
        latest = {
            'id': row['id'], 'created_at': row['created_at'], 'event_kind': row['kind'],
            'kind': payload.get('kind'), 'requested_kind': payload.get('requested_kind'),
            'reason': reason[:BLOCK_REASON_CHARS],
            'reason_chars': len(reason), 'reason_truncated': len(reason) > BLOCK_REASON_CHARS,
            'remainder_hint': ('Full reason is in this event (identified by event_kind/created_at) in full show.'
                               if len(reason) > BLOCK_REASON_CHARS else None),
        }
    return {'block_kind': task.block_kind, 'block_recurrences': task.block_recurrences,
            'latest_block': latest}


def comment_read(conn, task, *, comment_id=None, comment_offset=0):
    """One comment, newest by default, with stable id and character cursors.

    Never build worker_context or load bodies/runs/events for this path. SQL
    substr keeps even a single enormous comment bounded before serialization.
    """
    for name, value in (('comment_id', comment_id), ('comment_offset', comment_offset)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise ValueError(f'{name} must be an integer')
    if comment_id is not None and comment_id < 1:
        raise ValueError('comment_id must be >= 1')
    if comment_offset < 0:
        raise ValueError('comment_offset must be >= 0')
    total = conn.execute('SELECT COUNT(*) FROM task_comments WHERE task_id = ?', (task.id,)).fetchone()[0]
    where = 'task_id = ?'
    params = [task.id]
    if comment_id is not None:
        where += ' AND id = ?'
        params.append(comment_id)
    row = conn.execute(
        'SELECT id, created_at, substr(author, 1, 200) AS author, length(author) AS author_chars, '
        'substr(body, ?, ?) AS body, length(body) AS body_chars FROM task_comments '
        f'WHERE {where} ORDER BY id DESC LIMIT 1',
        [comment_offset + 1, COMMENT_CHUNK_CHARS, *params],
    ).fetchone()
    if comment_id is not None and row is None:
        raise ValueError(f'no comment {comment_id} on task {task.id}')
    if comment_offset and (row is None or comment_offset >= row['body_chars']):
        raise ValueError('comment_offset is past the end of the comment')
    comment = None
    previous = None
    following = None
    next_offset = None
    if row:
        comment = dict(row)
        comment['body_offset'] = comment_offset
        comment['body_truncated'] = comment_offset > 0 or len(row['body']) < row['body_chars']
        comment['author_truncated'] = row['author_chars'] > 200
        end = comment_offset + len(row['body'])
        next_offset = end if end < row['body_chars'] else None
        previous, following = conn.execute(
            'SELECT MAX(CASE WHEN id < ? THEN id END), MIN(CASE WHEN id > ? THEN id END) '
            'FROM task_comments WHERE task_id = ?', (row['id'], row['id'], task.id),
        ).fetchone()
    return {
        'mode': 'comments',
        'task': {'id': task.id, 'status': task.status, 'block_kind': task.block_kind,
                 'block_recurrences': task.block_recurrences},
        'comment': comment, 'comment_count': total,
        'omitted_comments': max(0, total - (1 if row else 0)),
        'previous_comment_id': previous, 'next_comment_id': following,
        'next_comment_offset': next_offset, 'chunk_chars': COMMENT_CHUNK_CHARS,
        'omitted_sections': ['other task fields', 'parents', 'children', 'events', 'runs', 'worker_context'],
        'truncated': True,
        'remainder_hint': (
            'Bounded comments-only read, not full task state. Call kanban_show(mode="comments", '
            'task_id=<task id>, comment_id=<previous_comment_id or next_comment_id>) for another comment. '
            'For the rest of this body, use its comment_id and next_comment_offset as comment_offset. '
            'CLI: hermes kanban show <task id> --mode comments --comment-id <id> --comment-offset <offset>. '
            'Other sections: full show (may exceed inline cap). Author display is capped at 200 characters; '
            'full author is available in full show.'),
    }
