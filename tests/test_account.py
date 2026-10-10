"""Tests for opt-in multi-account support."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from sub_agy.config import AccountConfig, Config, load_config
from sub_agy.jobs import job_dir, new_meta, read_meta, write_meta
from sub_agy.plan import assemble_prompt, parse_plan
from sub_agy.quota import extract_gemini_headroom, select_account
from sub_agy.schema import RESULT_SCHEMA
from sub_agy.supervise import supervise_round


def _make_fake_account_agy(
    bin_path: Path,
    five_h_pct: float = 90.0,
    weekly_pct: float = 90.0,
    fail_quota: bool = False,
    low_quota: bool = False,
    invocation_log: Path | None = None,
) -> None:
    bin_path.parent.mkdir(parents=True, exist_ok=True)
    if low_quota:
        five_h_pct = 10.0
        weekly_pct = 10.0

    quota_envelope = {
        "status": "SUCCESS",
        "command": {
            "data": {
                "groups": [
                    {
                        "name": "Gemini Models",
                        "buckets": [
                            {
                                "id": "gemini-5h",
                                "name": "Five Hour Limit Remaining",
                                "window": "5h",
                                "remaining_fraction": five_h_pct / 100.0,
                                "reset_time": "2026-10-20T00:00:00Z",
                                "description": "refresh in 1 hour",
                            },
                            {
                                "id": "gemini-weekly",
                                "name": "Weekly Limit Remaining",
                                "window": "weekly",
                                "remaining_fraction": weekly_pct / 100.0,
                                "reset_time": "2026-10-25T00:00:00Z",
                                "description": "refresh in 5 days",
                            },
                        ],
                    }
                ]
            }
        },
    }

    script = f"""#!/usr/bin/env python3
import json
import os
import sys

log_file = {str(invocation_log)!r} if {invocation_log is not None} else None
if log_file:
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps({{"argv": sys.argv, "bin": sys.argv[0]}}) + "\\n")

if "-p" in sys.argv and "/usage" in sys.argv:
    if {fail_quota}:
        print("Usage query failed", file=sys.stderr)
        sys.exit(1)
    print(json.dumps({quota_envelope!r}))
    sys.exit(0)

# Normal execution
status = os.environ.get("FAKE_AGY_STATUS", "SUCCESS")
response = os.environ.get("FAKE_AGY_RESPONSE", "executed by " + sys.argv[0])
structured_raw = os.environ.get("FAKE_AGY_STRUCTURED")
conversation_id = os.environ.get("FAKE_AGY_CONVERSATION", "conv-multi-123")

if not os.environ.get("FAKE_AGY_NO_RESULT"):
    event = {{
        "type": "result",
        "status": status,
        "response": response,
        "conversation_id": conversation_id,
        "duration_seconds": 1.0,
        "num_turns": 1,
    }}
    if structured_raw:
        event["structured_output"] = json.loads(structured_raw)
    print(json.dumps(event))

sys.exit(int(os.environ.get("FAKE_AGY_EXIT", "0")))
"""
    bin_path.write_text(script, encoding="utf-8")
    bin_path.chmod(0o755)


def _run_cli(cwd: Path, *args: str, config_path: Path | None = None, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ}
    if config_path:
        env["SUB_AGY_CONFIG"] = str(config_path)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "sub_agy.cli", "--cwd", str(cwd), *args],
        cwd=str(cwd),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
    )


def test_select_account_quota_headroom(tmp_path: Path) -> None:
    """Account with better bottleneck headroom is selected."""
    bin1 = tmp_path / "bin" / "agy-acc1.py"
    bin2 = tmp_path / "bin" / "agy-acc2.py"
    # acc1: 90% 5h, 40% weekly -> bottleneck 40%
    _make_fake_account_agy(bin1, five_h_pct=90.0, weekly_pct=40.0)
    # acc2: 70% 5h, 70% weekly -> bottleneck 70%
    _make_fake_account_agy(bin2, five_h_pct=70.0, weekly_pct=70.0)

    config = Config(
        accounts=(
            AccountConfig(name="acc1", agy_bin=str(bin1)),
            AccountConfig(name="acc2", agy_bin=str(bin2)),
        ),
        low_quota_threshold=20.0,
    )

    chosen, meta = select_account(config)
    assert chosen.name == "acc2"
    assert meta["reason"] == "quota_selected"


def test_select_account_low_quota_handling(tmp_path: Path) -> None:
    """When every account is below low-quota threshold, fail deterministically without picking a wrong profile."""
    bin1 = tmp_path / "bin" / "agy-acc1.py"
    bin2 = tmp_path / "bin" / "agy-acc2.py"
    _make_fake_account_agy(bin1, five_h_pct=15.0, weekly_pct=50.0)  # 5h is below 20%
    _make_fake_account_agy(bin2, five_h_pct=50.0, weekly_pct=10.0)  # weekly is below 20%

    config = Config(
        accounts=(
            AccountConfig(name="acc1", agy_bin=str(bin1)),
            AccountConfig(name="acc2", agy_bin=str(bin2)),
        ),
        low_quota_threshold=20.0,
    )

    with pytest.raises(RuntimeError) as exc_info:
        select_account(config)
    err = str(exc_info.value)
    assert "no configured account has sufficient Gemini quota" in err
    assert "acc1" in err
    assert "acc2" in err
    assert "run --account NAME" in err


def test_select_account_failed_quota_handling(tmp_path: Path) -> None:
    """If one account fails quota query, choose the eligible one; if all fail, raise clear error."""
    bin1 = tmp_path / "bin" / "agy-acc1.py"
    bin2 = tmp_path / "bin" / "agy-acc2.py"
    _make_fake_account_agy(bin1, fail_quota=True)
    _make_fake_account_agy(bin2, five_h_pct=80.0, weekly_pct=80.0)

    config = Config(
        accounts=(
            AccountConfig(name="acc1", agy_bin=str(bin1)),
            AccountConfig(name="acc2", agy_bin=str(bin2)),
        ),
    )

    # acc1 fails, but acc2 succeeds -> acc2 selected
    chosen, _ = select_account(config)
    assert chosen.name == "acc2"

    # Both fail -> deterministic error
    bin2_fail = tmp_path / "bin" / "agy-acc2-fail.py"
    _make_fake_account_agy(bin2_fail, fail_quota=True)
    config_all_fail = Config(
        accounts=(
            AccountConfig(name="acc1", agy_bin=str(bin1)),
            AccountConfig(name="acc2", agy_bin=str(bin2_fail)),
        ),
    )
    with pytest.raises(RuntimeError) as exc_info:
        select_account(config_all_fail)
    err = str(exc_info.value)
    assert "quota query failed" in err


def test_select_account_explicit_and_unknown(tmp_path: Path) -> None:
    """Explicit account selection uses the named account, unknown name raises error."""
    bin1 = tmp_path / "bin" / "agy-acc1.py"
    bin2 = tmp_path / "bin" / "agy-acc2.py"
    # acc2 is low quota, but explicitly selected
    _make_fake_account_agy(bin1, five_h_pct=90.0, weekly_pct=90.0)
    _make_fake_account_agy(bin2, five_h_pct=5.0, weekly_pct=5.0)

    config = Config(
        accounts=(
            AccountConfig(name="acc1", agy_bin=str(bin1)),
            AccountConfig(name="acc2", agy_bin=str(bin2)),
        ),
    )

    chosen, meta = select_account(config, requested_name="acc2")
    assert chosen.name == "acc2"
    assert meta["reason"] == "explicit"

    with pytest.raises(ValueError) as exc_info:
        select_account(config, requested_name="nonexistent")
    assert "unknown account 'nonexistent'" in str(exc_info.value)


def test_run_persists_account_metadata_and_supervisor_command(
    git_repo: Path, tmp_path: Path
) -> None:
    """New job persists chosen account, agy_bin, and brain_dir in meta.json, and supervisor uses matching agy_bin."""
    invocations_log = tmp_path / "invocations.log"
    bin1 = tmp_path / "bin" / "agy-acc1.py"
    bin2 = tmp_path / "bin" / "agy-acc2.py"
    brain1 = tmp_path / "brains" / "acc1-brain"
    brain2 = tmp_path / "brains" / "acc2-brain"
    brain1.mkdir(parents=True)
    brain2.mkdir(parents=True)

    _make_fake_account_agy(bin1, five_h_pct=30.0, weekly_pct=30.0, invocation_log=invocations_log)
    _make_fake_account_agy(bin2, five_h_pct=95.0, weekly_pct=95.0, invocation_log=invocations_log)

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[[accounts]]
name = "acc1"
agy_bin = "{bin1.as_posix()}"
brain_dir = "{brain1.as_posix()}"

[[accounts]]
name = "acc2"
agy_bin = "{bin2.as_posix()}"
brain_dir = "{brain2.as_posix()}"
""",
        encoding="utf-8",
    )

    plan = git_repo / "plan.md"
    plan.write_text("test plan\n", encoding="utf-8")

    res = _run_cli(git_repo, "run", "--plan", str(plan), config_path=config_path)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    job_id = out["job_id"]
    # Output must show account
    assert out["account"] == "acc2"

    meta = read_meta(git_repo, job_id)
    assert meta["account"] == "acc2"
    assert meta["agy_bin"] == str(bin2)
    assert meta["brain_dir"] == str(brain2.resolve())

    # Wait for completion
    for _ in range(50):
        status_res = _run_cli(git_repo, "status", job_id, config_path=config_path)
        status_data = json.loads(status_res.stdout)
        if status_data["state"] in ("done", "error"):
            break
        time.sleep(0.1)

    assert status_data["state"] == "done"
    assert status_data["account"] == "acc2"

    # Verify the supervisor executed bin2 (matching wrapper)
    assert invocations_log.exists()
    lines = invocations_log.read_text(encoding="utf-8").splitlines()
    normal_runs = [json.loads(line) for line in lines if "-p" in json.loads(line)["argv"] and "/usage" not in json.loads(line)["argv"]]
    assert len(normal_runs) >= 1
    assert any(str(bin2) in r["bin"] for r in normal_runs)


def test_transcript_recovery_reads_matching_brain_dir(
    git_repo: Path, tmp_path: Path
) -> None:
    """Transcript recovery isolates to the job's configured brain directory without cross-account confusion."""
    bin_acc = tmp_path / "bin" / "agy-trans.py"
    brain_acc = tmp_path / "brains" / "acc-brain"
    brain_other = tmp_path / "brains" / "other-brain"

    _make_fake_account_agy(bin_acc)

    # Put a transcript only in other-brain
    other_conv = brain_other / "uuid-other" / ".system_generated" / "logs"
    other_conv.mkdir(parents=True)
    (other_conv / "transcript.jsonl").write_text(
        '{"role":"assistant","content":"from other brain"}\n',
        encoding="utf-8",
    )

    # Set up job on acc-brain
    job_id = "j-trans-test"
    jdir = job_dir(git_repo, job_id)
    jdir.mkdir(parents=True)
    plan = parse_plan("trans")
    prompt = assemble_prompt(plan, str(git_repo))
    (jdir / "prompt.txt").write_text(prompt, encoding="utf-8")
    (jdir / "schema.json").write_text(json.dumps(RESULT_SCHEMA), encoding="utf-8")

    meta = new_meta(
        job_id,
        git_repo,
        None,
        None,
        "base",
        "gemini-3.7-flash",
        "low",
        "5m",
        account="acc1",
        agy_bin=str(bin_acc),
        brain_dir=str(brain_acc),
    )
    write_meta(git_repo, job_id, meta)

    # agy produces no result event
    os.environ["FAKE_AGY_NO_RESULT"] = "1"
    try:
        supervise_round(job_id, 1, git_repo)
    finally:
        os.environ.pop("FAKE_AGY_NO_RESULT", None)

    # Because acc-brain was empty, it should NOT recover from other-brain!
    res_meta = read_meta(git_repo, job_id)
    assert res_meta["state"] == "error"
    assert res_meta["recovered_from_transcript"] is False

    # Now put a transcript in acc-brain
    time.sleep(0.1)
    acc_conv = brain_acc / "uuid-acc" / ".system_generated" / "logs"
    acc_conv.mkdir(parents=True)
    (acc_conv / "transcript.jsonl").write_text(
        '{"role":"assistant","content":"from matching brain"}\n',
        encoding="utf-8",
    )

    job_id2 = "j-trans-test-2"
    jdir2 = job_dir(git_repo, job_id2)
    jdir2.mkdir(parents=True)
    (jdir2 / "prompt.txt").write_text(prompt, encoding="utf-8")
    (jdir2 / "schema.json").write_text(json.dumps(RESULT_SCHEMA), encoding="utf-8")

    meta2 = new_meta(
        job_id2,
        git_repo,
        None,
        None,
        "base",
        "gemini-3.7-flash",
        "low",
        "5m",
        account="acc1",
        agy_bin=str(bin_acc),
        brain_dir=str(brain_acc),
    )
    write_meta(git_repo, job_id2, meta2)

    os.environ["FAKE_AGY_NO_RESULT"] = "1"
    try:
        supervise_round(job_id2, 1, git_repo)
    finally:
        os.environ.pop("FAKE_AGY_NO_RESULT", None)

    res_meta2 = read_meta(git_repo, job_id2)
    assert res_meta2["recovered_from_transcript"] is True
    res_json = json.loads((jdir2 / "result.json").read_text(encoding="utf-8"))
    assert res_json["response_text"] == "from matching brain"


def test_feedback_stickiness(git_repo: Path, tmp_path: Path) -> None:
    """Feedback rounds preserve the job's original account even if config changes."""
    bin1 = tmp_path / "bin" / "agy-fb1.py"
    bin2 = tmp_path / "bin" / "agy-fb2.py"
    invocations_log = tmp_path / "fb_invocations.log"
    _make_fake_account_agy(bin1, five_h_pct=90.0, weekly_pct=90.0, invocation_log=invocations_log)
    _make_fake_account_agy(bin2, five_h_pct=95.0, weekly_pct=95.0, invocation_log=invocations_log)

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[[accounts]]
name = "acc1"
agy_bin = "{bin1.as_posix()}"

[[accounts]]
name = "acc2"
agy_bin = "{bin2.as_posix()}"
""",
        encoding="utf-8",
    )

    plan = git_repo / "plan.md"
    plan.write_text("plan\n", encoding="utf-8")

    # Start explicitly on acc1
    res = _run_cli(git_repo, "run", "--plan", str(plan), "--account", "acc1", config_path=config_path)
    assert res.returncode == 0
    job_id = json.loads(res.stdout)["job_id"]

    # Wait for round 1 done
    for _ in range(50):
        status_res = _run_cli(git_repo, "status", job_id, config_path=config_path)
        status_data = json.loads(status_res.stdout)
        if status_data["state"] in ("done", "error"):
            break
        time.sleep(0.1)

    assert status_data["state"] == "done"
    assert status_data["account"] == "acc1"

    # Now change config so acc1 no longer exists or acc2 is the only account
    config_path.write_text(
        f"""
[[accounts]]
name = "acc2"
agy_bin = "{bin2.as_posix()}"
""",
        encoding="utf-8",
    )

    # Run feedback
    fb_res = _run_cli(git_repo, "feedback", job_id, "please revise", config_path=config_path)
    assert fb_res.returncode == 0, fb_res.stderr

    # Meta must still retain acc1!
    meta_round2 = read_meta(git_repo, job_id)
    assert meta_round2["account"] == "acc1"
    assert meta_round2["agy_bin"] == str(bin1)

    # Wait for round 2 done
    for _ in range(50):
        status_res = _run_cli(git_repo, "status", job_id, config_path=config_path)
        status_data = json.loads(status_res.stdout)
        if status_data["state"] in ("done", "error") and status_data["round"] == 2:
            break
        time.sleep(0.1)

    assert status_data["state"] == "done"
    assert status_data["account"] == "acc1"


def test_cli_outputs_show_account(git_repo: Path, tmp_path: Path) -> None:
    """run, status, result, and watch outputs all show account."""
    bin1 = tmp_path / "bin" / "agy-show.py"
    _make_fake_account_agy(bin1)

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[[accounts]]
name = "myaccount"
agy_bin = "{bin1.as_posix()}"
""",
        encoding="utf-8",
    )

    plan = git_repo / "plan.md"
    plan.write_text("plan\n", encoding="utf-8")

    # 1. run output
    run_res = _run_cli(git_repo, "run", "--plan", str(plan), config_path=config_path)
    assert run_res.returncode == 0
    run_out = json.loads(run_res.stdout)
    assert run_out["account"] == "myaccount"
    job_id = run_out["job_id"]

    # 2. status output (single and --all)
    status_single = _run_cli(git_repo, "status", job_id, config_path=config_path)
    assert json.loads(status_single.stdout)["account"] == "myaccount"

    status_all = _run_cli(git_repo, "status", "--all", config_path=config_path)
    all_jobs = json.loads(status_all.stdout)
    assert any(j["id"] == job_id and j["account"] == "myaccount" for j in all_jobs)

    status_pretty = _run_cli(git_repo, "status", "--all", "--pretty", config_path=config_path)
    assert "account" in status_pretty.stdout.lower()
    assert "myaccount" in status_pretty.stdout

    # Wait for terminal
    for _ in range(50):
        s = json.loads(_run_cli(git_repo, "status", job_id, config_path=config_path).stdout)
        if s["state"] in ("done", "error"):
            break
        time.sleep(0.1)

    # 3. result output
    result_res = _run_cli(git_repo, "result", job_id, config_path=config_path)
    assert json.loads(result_res.stdout)["account"] == "myaccount"

    # 4. watch output
    watch_res = _run_cli(git_repo, "watch", job_id, config_path=config_path)
    assert json.loads(watch_res.stdout)[0]["account"] == "myaccount"

    watch_pretty = _run_cli(git_repo, "watch", job_id, "--pretty", config_path=config_path)
    assert "account" in watch_pretty.stdout.lower()
    assert "myaccount" in watch_pretty.stdout


def test_quota_command_multi_account(tmp_path: Path) -> None:
    """sub-agy quota reports all accounts, individual account, oneline, and pretty."""
    bin1 = tmp_path / "bin" / "agy-q1.py"
    bin2 = tmp_path / "bin" / "agy-q2.py"
    _make_fake_account_agy(bin1, five_h_pct=95.0, weekly_pct=95.0)
    _make_fake_account_agy(bin2, five_h_pct=80.0, weekly_pct=80.0)

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[[accounts]]
name = "acc1"
agy_bin = "{bin1.as_posix()}"

[[accounts]]
name = "acc2"
agy_bin = "{bin2.as_posix()}"
""",
        encoding="utf-8",
    )

    # Multi-account default JSON
    res = _run_cli(tmp_path, "quota", config_path=config_path)
    assert res.returncode == 0
    data = json.loads(res.stdout)
    assert data["ok"] is True
    assert "accounts" in data
    assert "acc1" in data["accounts"]
    assert "acc2" in data["accounts"]

    # Specific account
    res_acc1 = _run_cli(tmp_path, "quota", "--account", "acc1", config_path=config_path)
    assert res_acc1.returncode == 0
    data_acc1 = json.loads(res_acc1.stdout)
    assert data_acc1["ok"] is True
    assert "groups" in data_acc1

    # Oneline multi-account
    res_oneline = _run_cli(tmp_path, "quota", "--oneline", config_path=config_path)
    assert res_oneline.returncode == 0
    assert "[acc1]" in res_oneline.stdout
    assert "[acc2]" in res_oneline.stdout

    # Pretty multi-account
    res_pretty = _run_cli(tmp_path, "quota", "--pretty", config_path=config_path)
    assert res_pretty.returncode == 0
    assert "=== Account: acc1" in res_pretty.stdout
    assert "=== Account: acc2" in res_pretty.stdout


def test_legacy_job_uses_default_account(tmp_path: Path, git_repo: Path) -> None:
    """Jobs created without account metadata default to 'default' account."""
    meta = new_meta(
        "j-legacy",
        git_repo,
        None,
        None,
        "base",
        "gemini-3.7-flash",
        "low",
        "5m",
    )
    # Simulate an old job with no account keys
    meta.pop("account", None)
    meta.pop("agy_bin", None)
    meta.pop("brain_dir", None)
    meta["state"] = "done"
    write_meta(git_repo, "j-legacy", meta)

    # status shows default
    status_res = _run_cli(git_repo, "status", "j-legacy")
    assert status_res.returncode == 0
    assert json.loads(status_res.stdout)["account"] == "default"
