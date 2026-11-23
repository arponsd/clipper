"""G6: `autoclipper schedule add|list|cancel|edit` (no network: scheduling never sends anything)."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from publish_fakes import connect_all, make_job

from autoclipper import cli
from autoclipper.publish import records, schedules

JOB = "20261123-120000-pubtst"


def run_cli(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def when(hours=3):
    tz = timezone(timedelta(hours=6))
    return datetime.fromtimestamp(int(time.time() + hours * 3600), tz=tz).strftime("%Y-%m-%d %H:%M")


@pytest.fixture
def job():
    return make_job(JOB)


def test_parser():
    a = cli.build_parser().parse_args(["schedule", "add", "J", "--clip", "1", "--lang", "hi", "--platform", "all",
                                       "--at", "2026-11-24 18:30", "--tz", "Asia/Dhaka"])
    assert a.func.__name__ == "cmd_add" and a.platform == ["all"] and a.tz == "Asia/Dhaka" and not a.yes
    assert cli.build_parser().parse_args(["schedule"]).func.__name__ == "cmd_list"


def test_add_dry_run_prints_plan_with_time_and_stores_nothing(job, capsys):
    connect_all(dry_run=True)
    code, out, err = run_cli(capsys, "schedule", "add", JOB, "--clip", "1", "--lang", "hi", "--platform", "all",
                             "--at", when(), "--tz", "Asia/Dhaka")
    assert code == 0, err
    assert "Dry run: nothing was scheduled" in out and "schedule: app" in out and "(UTC+06:00)" in out
    assert not schedules.store_path().exists()
    assert records.read_plan(JOB, 1, "hi", "instagram")["schedule"]["timezone"] == "Asia/Dhaka"


def test_add_live_needs_yes(job, capsys):
    connect_all()
    code, out, err = run_cli(capsys, "schedule", "add", JOB, "--clip", "1", "--lang", "hi", "--platform",
                             "instagram", "--at", when(), "--tz", "Asia/Dhaka")
    assert code == 2 and "add --yes" in err
    assert not schedules.store_path().exists()


def test_add_list_edit_cancel(job, capsys):
    connect_all()
    code, out, err = run_cli(capsys, "schedule", "add", JOB, "--clip", "1", "--lang", "hi", "--platform",
                             "instagram", "--platform", "facebook", "--at", when(), "--tz", "Asia/Dhaka", "--yes")
    assert code == 0, err
    rows = schedules.read_all()
    assert len(rows) == 1 and "keep the server running" in out
    sid = rows[0]["id"]
    code, out, _ = run_cli(capsys, "schedule", "list", "--json")
    assert code == 0 and json.loads(out)[0]["id"] == sid
    code, out, _ = run_cli(capsys, "schedule", "list")
    assert sid in out and "Instagram" in out
    code, out, err = run_cli(capsys, "schedule", "edit", sid, "--at", when(5), "--tz", "Asia/Dhaka")
    assert code == 0, err
    code, out, err = run_cli(capsys, "schedule", "cancel", sid, "--platform", "instagram")
    assert code == 0 and schedules.get(sid)["items"]["instagram"]["state"] == "cancelled"
    code, out, err = run_cli(capsys, "schedule", "cancel", sid)
    assert code == 0 and schedules.get(sid)["state"] == "cancelled"


def test_errors_exit_2(job, capsys):
    connect_all()
    code, _, err = run_cli(capsys, "schedule", "add", JOB, "--clip", "1", "--lang", "hi", "--platform", "youtube",
                           "--at", "soon", "--yes")
    assert code == 2 and "publish_at" in err and "Traceback" not in err
    code, _, err = run_cli(capsys, "schedule", "cancel", "s_000000000000")
    assert code == 2 and "not found" in err
