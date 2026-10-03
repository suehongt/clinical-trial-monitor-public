from datetime import datetime, timezone
from core.monitor_scheduler import run_due_monitors

def test_due_occurrence_is_durably_claimed_once(test_db):
    from db.connection import get_connection
    conn=get_connection()
    mid=conn.execute("INSERT INTO monitors (name,enabled,schedule_enabled,schedule_frequency,next_run_at) VALUES ('x',1,1,'hourly','2026-01-01 10:00:00')").lastrowid
    conn.execute("INSERT INTO monitor_rules (monitor_id,rules_json) VALUES (?, '{}')",(mid,));conn.commit()
    now=datetime(2026,1,1,10,0,tzinfo=timezone.utc)
    assert run_due_monitors(conn,now=now)["claimed"]==1
    assert run_due_monitors(conn,now=now)["claimed"]==0
    assert conn.execute("SELECT COUNT(*) FROM monitor_runs WHERE monitor_id=? AND trigger='scheduled'",(mid,)).fetchone()[0]==1

def test_manual_run_does_not_consume_scheduled_occurrence(test_db):
    from db.connection import get_connection
    from core.monitors import run_monitor
    conn=get_connection(); mid=conn.execute("INSERT INTO monitors (name,enabled,schedule_enabled,schedule_frequency,next_run_at) VALUES ('x',1,1,'hourly','2026-01-01 10:00:00')").lastrowid
    conn.execute("INSERT INTO monitor_rules (monitor_id,rules_json) VALUES (?, '{}')",(mid,));conn.commit()
    run_monitor(conn,mid)
    assert conn.execute("SELECT trigger FROM monitor_runs WHERE monitor_id=?",(mid,)).fetchone()[0]=='manual'
    run_due_monitors(conn,now=datetime(2026,1,1,10,0,tzinfo=timezone.utc))
    assert [r[0] for r in conn.execute("SELECT trigger FROM monitor_runs WHERE monitor_id=? ORDER BY id",(mid,))]==['manual','scheduled']

def test_paused_and_overdue_schedule_policy(test_db):
    from db.connection import get_connection
    conn=get_connection(); mid=conn.execute("INSERT INTO monitors (name,enabled,schedule_enabled,schedule_frequency,next_run_at) VALUES ('x',0,1,'hourly','2026-01-01 01:00:00')").lastrowid
    conn.execute("INSERT INTO monitor_rules (monitor_id,rules_json) VALUES (?, '{}')",(mid,));conn.commit()
    assert run_due_monitors(conn,now=datetime(2026,1,1,1,0,tzinfo=timezone.utc))["claimed"]==0
    conn.execute("UPDATE monitors SET enabled=1 WHERE id=?",(mid,));conn.commit()
    result=run_due_monitors(conn,now=datetime(2026,1,1,5,20,tzinfo=timezone.utc))
    assert result["claimed"]==1
    # anchor rolls forward through missed slots: 01:00 -> ... -> 06:00, not tick+1h
    assert conn.execute("SELECT next_run_at FROM monitors WHERE id=?",(mid,)).fetchone()[0]=='2026-01-01 06:00:00'

def test_on_time_tick_keeps_wall_clock_anchor(test_db):
    from db.connection import get_connection
    conn=get_connection(); mid=conn.execute("INSERT INTO monitors (name,enabled,schedule_enabled,schedule_frequency,next_run_at) VALUES ('x',1,1,'daily','2026-01-01 14:19:00')").lastrowid
    conn.execute("INSERT INTO monitor_rules (monitor_id,rules_json) VALUES (?, '{}')",(mid,));conn.commit()
    # tick lands 5 seconds late; the daily 14:19 anchor must not drift
    run_due_monitors(conn,now=datetime(2026,1,1,14,19,5,tzinfo=timezone.utc))
    assert conn.execute("SELECT next_run_at FROM monitors WHERE id=?",(mid,)).fetchone()[0]=='2026-01-02 14:19:00'

def test_advance_occurrence_skips_missed_slots_without_backlog(test_db):
    from db.connection import get_connection
    conn=get_connection(); mid=conn.execute("INSERT INTO monitors (name,enabled,schedule_enabled,schedule_frequency,next_run_at) VALUES ('x',1,1,'daily','2026-01-01 14:19:00')").lastrowid
    conn.execute("INSERT INTO monitor_rules (monitor_id,rules_json) VALUES (?, '{}')",(mid,));conn.commit()
    # outage of two days: exactly one catch-up run, then the original 14:19 phase
    run_due_monitors(conn,now=datetime(2026,1,3,9,0,tzinfo=timezone.utc))
    assert conn.execute("SELECT COUNT(*) FROM monitor_runs WHERE monitor_id=? AND trigger='scheduled'",(mid,)).fetchone()[0]==1
    assert conn.execute("SELECT next_run_at FROM monitors WHERE id=?",(mid,)).fetchone()[0]=='2026-01-03 14:19:00'
