"""Gateway-life evidence exempts unknown worker deaths, never affirmative exits."""
from datetime import datetime, timezone
import json
import time
from pathlib import Path

import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER

G = 65001

@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_KANBAN_CRASH_GRACE_SECONDS', '0')
    monkeypatch.setattr(kb, '_pid_alive', lambda pid: False)
    kbd._recent_worker_exits.clear()
    kb.init_db()
    return home


def ledger(home, *records):
    path = home / 'logs' / 'gateway-exit-diag.log'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(dict(r, ts=datetime.fromtimestamp(r['ts'], timezone.utc).isoformat())) + '\n' for r in records))


def life(home, anchor, tag='asyncio.run.SystemExit'):
    ledger(home, dict(tag='gateway.start', pid=G, ts=anchor-60),
           dict(tag=tag, pid=G if tag != 'gateway.previous_unclean_exit' else G+1,
                prior_pid=G, ts=anchor+60))


def worker(conn, tid=None, pid=70001, review=False):
    tid = tid or kb.create_task(conn, title='test', assignee='a')
    assert kb.claim_task(conn, tid, claimer=f'{kb._host_prefix()}{G}') is not None
    if review:
        row = conn.execute("SELECT id, payload FROM task_events WHERE task_id=? AND kind='claimed' ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
        payload = kb._json_dict(row['payload'])
        payload['source_status'] = 'review'
        conn.execute('UPDATE task_events SET payload=? WHERE id=?', (json.dumps(payload), row['id']))
        conn.commit()
    anchor = int(time.time()) - 120
    conn.execute('UPDATE tasks SET worker_pid=?, worker_started_at=NULL, started_at=?, last_heartbeat_at=? WHERE id=?', (pid, anchor, anchor, tid))
    conn.commit()
    return tid, anchor


def events(conn, tid, kind):
    return [kb._json_dict(r['payload']) for r in conn.execute('SELECT payload FROM task_events WHERE task_id=? AND kind=?', (tid, kind))]


def exempt(conn, tid, kind='crashed', status='ready'):
    task = kb.get_task(conn, tid)
    assert task.status == status
    assert task.consecutive_failures == 0
    assert not events(conn, tid, 'gave_up')
    assert not events(conn, tid, 'blocked')
    payloads = events(conn, tid, kind)
    assert len(payloads) == 1
    assert payloads[0]['infrastructure'] == 'gateway_restart'
    assert payloads[0]['gateway_pid'] == G
    assert 'gateway restart' in task.last_failure_error
    run = conn.execute('SELECT metadata FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1', (tid,)).fetchone()
    metadata = kb._json_dict(run['metadata'])
    for key in ('infrastructure', 'gateway_pid', 'gateway_start_ts', 'gateway_exit_ts', 'terminal_tag', 'anchor_ts'):
        assert metadata[key] == payloads[0][key]


def test_t1_infra_crash_exempt(home):
    with kbc.connect() as conn:
        tid, anchor = worker(conn)
        life(home, anchor)
        kbd.detect_crashed_workers(conn)
        exempt(conn, tid)


def test_t2_unclean_exit(home):
    with kbc.connect() as conn:
        tid, anchor = worker(conn)
        life(home, anchor, 'gateway.previous_unclean_exit')
        kbd.detect_crashed_workers(conn)
        exempt(conn, tid)


def charged(conn, tid):
    assert kb.get_task(conn, tid).consecutive_failures == 1
    assert 'infrastructure' not in events(conn, tid, 'crashed')[-1]


def test_t3_no_gateway_start(home):
    with kbc.connect() as conn:
        tid, anchor = worker(conn)
        ledger(home, dict(tag='asyncio.run.SystemExit', pid=G, ts=anchor+60))
        kbd.detect_crashed_workers(conn)
        charged(conn, tid)


def test_t4_live_gateway(home):
    with kbc.connect() as conn:
        tid, anchor = worker(conn)
        ledger(home, dict(tag='gateway.start', pid=G, ts=anchor-60))
        kbd.detect_crashed_workers(conn)
        charged(conn, tid)


def test_t5_exit_before_anchor(home):
    with kbc.connect() as conn:
        tid, anchor = worker(conn)
        ledger(home, dict(tag='gateway.start', pid=G, ts=anchor-60),
               dict(tag='asyncio.run.SystemExit', pid=G, ts=anchor-1))
        kbd.detect_crashed_workers(conn)
        charged(conn, tid)


def test_t6_trailer_wins(home):
    with kbc.connect() as conn:
        tid, anchor = worker(conn)
        life(home, anchor)
        log = kb.worker_log_path(tid)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(f'{KANBAN_WORKER_EXIT_TRAILER}1\n')
        kbd.detect_crashed_workers(conn)
        charged(conn, tid)
        assert events(conn, tid, 'crashed')[-1]['exit_kind'] == 'nonzero_exit'


def test_t7_breaker_intact(home):
    with kbc.connect() as conn:
        tid, _ = worker(conn)
        kbd.detect_crashed_workers(conn)
        charged(conn, tid)
        worker(conn, tid, pid=70002)
        kbd.detect_crashed_workers(conn)
        task = kb.get_task(conn, tid)
        assert task.status == 'blocked'
        assert task.block_kind == 'breaker'
        assert task.consecutive_failures == 2
        assert len(events(conn, tid, 'gave_up')) == 1
        assert events(conn, tid, 'blocked')[-1]['kind'] == 'breaker'


def test_t8_stale_claim(home):
    with kbc.connect() as conn:
        tid, anchor = worker(conn)
        life(home, anchor)
        conn.execute('UPDATE tasks SET claim_expires=? WHERE id=?', (anchor, tid))
        conn.commit()
        assert kb.release_stale_claims(conn) == 1
        exempt(conn, tid, kind='reclaimed')


def test_t9_cooldown(home, monkeypatch):
    with kbc.connect() as conn:
        tid, anchor = worker(conn)
        life(home, anchor)
        kbd.detect_crashed_workers(conn)
        ended = conn.execute('SELECT ended_at FROM task_runs WHERE task_id=?', (tid,)).fetchone()[0]
        monkeypatch.setattr(kb, '_resolve_rate_limit_cooldown_seconds', lambda: 300)
        monkeypatch.setattr(kbd.time, 'time', lambda: ended + 299)
        assert kbd.check_respawn_guard(conn, tid) == 'infrastructure_cooldown'
        monkeypatch.setattr(kbd.time, 'time', lambda: ended + 300)
        assert kbd.check_respawn_guard(conn, tid) is None


def test_t10_review_lane(home):
    with kbc.connect() as conn:
        tid, anchor = worker(conn, review=True)
        life(home, anchor)
        kbd.detect_crashed_workers(conn)
        exempt(conn, tid, status='review')


def test_t11_systemic_trap(home, monkeypatch):
    with kbc.connect() as conn:
        workers = [worker(conn, pid=70001+i) for i in range(3)]
        life(home, workers[0][1])
        # Force identical fingerprints: exemption must not rely on error wording.
        monkeypatch.setattr(kbd, '_error_fingerprint', lambda error: 'identical')
        kbd.detect_crashed_workers(conn)
        for tid, _ in workers:
            exempt(conn, tid)
            assert not kb._has_sticky_block(conn, tid)


def test_existing_budget_and_long_booking_lag_are_preserved(home):
    with kbc.connect() as conn:
        tid, anchor = worker(conn)
        anchor -= 3600
        conn.execute('UPDATE tasks SET last_heartbeat_at=?, consecutive_failures=1, max_retries=1 WHERE id=?', (anchor, tid))
        conn.commit()
        life(home, anchor)
        kbd.detect_crashed_workers(conn)
        task = kb.get_task(conn, tid)
        assert task.status == 'ready'
        assert task.consecutive_failures == 1
        assert not events(conn, tid, 'gave_up')
        assert events(conn, tid, 'crashed')[-1]['anchor_ts'] == anchor


def test_malformed_lines_do_not_hide_valid_evidence(home):
    anchor = int(time.time())-120
    life(home, anchor)
    path = home / 'logs' / 'gateway-exit-diag.log'
    path.write_bytes(b'not json\n[]\nnull\n\xff\n' + path.read_bytes() + b'{"tag": []}\n')
    kbd._gateway_life_records.cache_clear()
    assert kbd._gateway_life_evidence(f'{kb._host_prefix()}{G}', anchor, time.time())['infrastructure'] == 'gateway_restart'


def test_ledger_home_a_b_a(home, tmp_path, monkeypatch):
    anchor = int(time.time())-120
    life(home, anchor)
    other = tmp_path / 'other-home'
    other.mkdir()
    ledger(other, dict(tag='gateway.start', pid=G, ts=anchor-60))
    kbd._gateway_life_records.cache_clear()
    for target, expected in ((home, True), (other, False), (home, True)):
        monkeypatch.setenv('HERMES_HOME', str(target))
        evidence = kbd._gateway_life_evidence(f'{kb._host_prefix()}{G}', anchor, time.time())
        assert bool(evidence) == expected


@pytest.mark.parametrize('case', ['missing', 'empty', 'malformed', 'truncated', 'pid_reuse', 'future_exit', 'cross_host', 'bad_claimer', 'no_anchor'])
def test_fail_closed_evidence(home, case):
    now = int(time.time())
    anchor = now - 120
    life(home, anchor)
    path = home / 'logs' / 'gateway-exit-diag.log'
    claimer = f'{kb._host_prefix()}{G}'
    if case == 'missing':
        path.unlink()
    elif case == 'empty':
        path.write_text('')
    elif case == 'malformed':
        path.write_text('not json\n[]\nnull\n{"tag": [], "ts": "bad"}\n{"tag":"gateway.start","pid":65001,"ts":"bad"}\n')
    elif case == 'truncated':
        # Original start falls outside the bounded tail; terminal alone cannot exempt.
        path.write_text(path.read_text().splitlines()[0] + '\n' + 'x' * (1024*1024) + '\n' + path.read_text().splitlines()[1] + '\n')
    elif case == 'pid_reuse':
        ledger(home, dict(tag='gateway.start', pid=G, ts=anchor-300),
               dict(tag='asyncio.run.SystemExit', pid=G, ts=anchor-200),
               dict(tag='gateway.start', pid=G, ts=anchor-60))
    elif case == 'future_exit':
        ledger(home, dict(tag='gateway.start', pid=G, ts=anchor-60),
               dict(tag='asyncio.run.SystemExit', pid=G, ts=now+1))
    elif case == 'cross_host':
        claimer = f'other-host:{G}'
    elif case == 'bad_claimer':
        claimer = f'{kb._host_prefix()}w{G}'
    elif case == 'no_anchor':
        anchor = None
    kbd._gateway_life_records.cache_clear()
    assert kbd._gateway_life_evidence(claimer, anchor, now) is None


@pytest.mark.parametrize('rc, kind', [(0, 'clean_exit'), (1, 'nonzero_exit'), (-15, 'signaled'), (75, 'rate_limited'), (78, 'terminal_provider')])
def test_positive_exit_bypasses_ledger(home, monkeypatch, rc, kind):
    monkeypatch.setattr(kbd, '_classify_worker_exit', lambda pid: (kind, rc))
    def forbidden(*args, **kwargs):
        pytest.fail('positive exit must not consult the ledger')
    monkeypatch.setattr(kbd, '_gateway_life_evidence', forbidden)
    dead = kbd._classify_dead_worker_exit(70001, f'{kb._host_prefix()}{G}', anchor_ts=int(time.time())-120)
    assert dead.kind == kind
    assert dead.infra_evidence is None


def test_stale_claim_without_worker_stays_charged(home):
    with kbc.connect() as conn:
        tid, anchor = worker(conn)
        life(home, anchor)
        conn.execute('UPDATE tasks SET worker_pid=NULL, claim_expires=? WHERE id=?', (anchor, tid))
        conn.commit()
        assert kb.release_stale_claims(conn) == 1
        assert kb.get_task(conn, tid).consecutive_failures == 1
        assert 'infrastructure' not in events(conn, tid, 'reclaimed')[-1]


def test_sweep_reads_ledger_once_and_refreshes_next_tick(home, monkeypatch):
    with kbc.connect() as conn:
        workers = [worker(conn, pid=70100+i) for i in range(3)]
        life(home, workers[0][1])
        opened = []
        original = Path.open
        def tracked(path, *args, **kwargs):
            if path.name == 'gateway-exit-diag.log':
                opened.append(path)
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, 'open', tracked)
        kbd.detect_crashed_workers(conn)
        assert len(opened) == 1
        tid, anchor = worker(conn, pid=70104)
        ledger(home, dict(tag='gateway.start', pid=G, ts=anchor-60))
        opened.clear()
        kbd.detect_crashed_workers(conn)
        assert len(opened) == 1
        charged(conn, tid)


def test_latest_start_selects_its_own_terminal(home):
    anchor = int(time.time())-120
    ledger(home, dict(tag='gateway.start', pid=G, ts=anchor-400),
           dict(tag='asyncio.run.SystemExit', pid=G, ts=anchor-300),
           dict(tag='gateway.start', pid=G, ts=anchor-60),
           dict(tag='gateway.exit_clean', pid=G, ts=anchor+30),
           dict(tag='atexit.hook', pid=G, ts=anchor+31))
    kbd._gateway_life_records.cache_clear()
    evidence = kbd._gateway_life_evidence(f'{kb._host_prefix()}{G}', anchor, time.time())
    assert evidence['terminal_tag'] == 'gateway.exit_clean'
    assert datetime.fromisoformat(evidence['gateway_start_ts']).timestamp() == anchor-60


@pytest.mark.parametrize('source', ['task', 'run', 'worker', 'none'])
def test_anchor_precedence(home, source):
    with kbc.connect() as conn:
        tid, anchor = worker(conn)
        life(home, anchor)
        conn.execute('UPDATE tasks SET last_heartbeat_at=NULL, started_at=?, worker_started_at=? WHERE id=?',
                     (anchor if source == 'task' else None, anchor if source == 'worker' else None, tid))
        if source in ('worker', 'none'):
            conn.execute('UPDATE tasks SET current_run_id=NULL WHERE id=?', (tid,))
        else:
            conn.execute('UPDATE task_runs SET started_at=? WHERE task_id=?', (anchor if source == 'run' else anchor-300, tid))
        conn.commit()
        kbd.detect_crashed_workers(conn)
        if source == 'none':
            charged(conn, tid)
        elif source == 'worker':
            assert kb.get_task(conn, tid).status == 'ready'
            assert kb.get_task(conn, tid).consecutive_failures == 0
            assert events(conn, tid, 'crashed')[-1]['anchor_ts'] == anchor
        else:
            exempt(conn, tid)
