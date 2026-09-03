"""Tests for the paths that break when the locale codepage is not UTF-8.

agy speaks UTF-8 on every platform, while Windows resolves pipes, stdio and
PATH lookups through the console codepage -- cp936 on a Chinese install.  These
assertions pin the explicit encodings and the PATHEXT-aware lookup that make
the two agree; on a UTF-8 platform they still pin the same contract.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from sub_agy import supervise
from sub_agy.config import Config, agy_bin_path
from sub_agy.jobs import job_dir, read_meta, result_path, write_meta
from sub_agy.plan import assemble_prompt, parse_plan
from sub_agy.procs import child_group_kwargs, configure_stdio
from sub_agy.schema import RESULT_SCHEMA
from sub_agy.supervise import supervise_round
from sub_agy.worktree import git_head


_CJK_RESPONSE = "已完成：改了 3 个文件 • 无报错"
_CJK_SUMMARY = "中文摘要"
_CJK_TOOL = "写入文件"


def _event_line(event: dict) -> bytes:
    return json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n"


def _write_stream_stub(script: Path, chunks: list[bytes]) -> Path:
    """Write a stand-in agy that copies fixed bytes to stdout.

    The output is bytes rather than printed text because agy emits UTF-8
    whatever the console codepage is, which a ``print`` from a Python child
    would not reproduce -- it would encode to the locale codepage instead.
    """
    script.write_text(
        "import sys\n"
        f"for chunk in {chunks!r}:\n"
        "    sys.stdout.buffer.write(chunk)\n"
        "sys.stdout.buffer.flush()\n",
        encoding="utf-8",
    )
    return script


def _seed_job(
    project: Path, job_id: str, agy_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Create a queued job and a config that runs ``agy_bin`` for it."""
    config_path = project / "test-config.toml"
    config_path.write_text(
        f'agy_bin = "{agy_bin.as_posix()}"\ndefault_timeout = "5m"\n', encoding="utf-8"
    )
    monkeypatch.setenv("SUB_AGY_CONFIG", str(config_path))

    jdir = job_dir(project, job_id)
    jdir.mkdir(parents=True)
    prompt = assemble_prompt(parse_plan("encoding check"), str(project))
    (jdir / "prompt.txt").write_text(prompt, encoding="utf-8")
    (jdir / "schema.json").write_text(json.dumps(RESULT_SCHEMA), encoding="utf-8")
    write_meta(
        project,
        job_id,
        {
            "id": job_id,
            "state": "queued",
            "project": str(project),
            "worktree": None,
            "branch": None,
            "base_sha": git_head(project),
            "model": "gemini-3.7-flash",
            "effort": "low",
            "timeout": "5m",
            "conversation_id": None,
            "agy_status": None,
            "exit_code": None,
            "error": None,
            "attempts": 0,
            "recovered_from_transcript": False,
            "round": 1,
        },
    )
    return jdir


def test_configure_stdio_skips_streams_it_cannot_reconfigure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Unreconfigurable:
        def reconfigure(self, **kwargs: object) -> None:
            raise io.UnsupportedOperation("stream is not seekable")

    plain = io.StringIO()
    assert not hasattr(plain, "reconfigure")
    monkeypatch.setattr(sys, "stdout", plain)
    monkeypatch.setattr(sys, "stderr", _Unreconfigurable())

    configure_stdio()


def test_configure_stdio_pins_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = io.TextIOWrapper(io.BytesIO(), encoding="ascii", errors="strict")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    with pytest.raises(UnicodeEncodeError):
        stream.write("•")

    configure_stdio()

    assert stream.encoding == "utf-8"
    stream.write("•")
    stream.flush()
    assert stream.buffer.getvalue() == "•".encode("utf-8")


def test_child_group_kwargs_is_platform_correct() -> None:
    kwargs = child_group_kwargs()
    if os.name == "nt":
        # start_new_session is accepted and then ignored here, so a kwargs dict
        # carrying it would leave the child in the caller's group.
        assert kwargs == {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        assert kwargs == {"start_new_session": True}


def test_agy_pipe_decodes_utf8_regardless_of_locale(tmp_path: Path) -> None:
    event = {"type": "result", "status": "SUCCESS", "response": _CJK_RESPONSE}
    stub = _write_stream_stub(tmp_path / "stream_stub.py", [_event_line(event)])

    proc = subprocess.Popen(
        [sys.executable, str(stub)],
        stdout=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        **child_group_kwargs(),
    )
    out, _ = proc.communicate(timeout=60)

    assert proc.returncode == 0
    assert json.loads(out.strip())["response"] == _CJK_RESPONSE


def test_run_agy_once_pipes_declare_utf8(
    git_repo: Path, fake_agy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = "j-utf8-kwargs"
    _seed_job(git_repo, job_id, fake_agy, monkeypatch)

    captured: list[dict] = []
    real_popen = subprocess.Popen

    def _spy(*args: object, **kwargs: object) -> subprocess.Popen:
        if "--output-format" in args[0]:
            captured.append(kwargs)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _spy)
    supervise_round(job_id, 1, git_repo)

    assert read_meta(git_repo, job_id)["state"] == "done"
    assert len(captured) == 1
    kwargs = captured[0]
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "replace"
    assert "text" not in kwargs
    assert "universal_newlines" not in kwargs
    if os.name == "nt":
        assert kwargs["creationflags"] == subprocess.CREATE_NEW_PROCESS_GROUP
        assert "start_new_session" not in kwargs
    else:
        assert kwargs["start_new_session"] is True


def test_agy_bin_path_finds_bare_name_via_pathext(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if os.name == "nt":
        stub = bin_dir / "agy-stub.cmd"
        stub.write_text("@echo off\r\n", encoding="ascii")
    else:
        stub = bin_dir / "agy-stub"
        stub.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
        stub.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))

    resolved = agy_bin_path(Config(agy_bin="agy-stub"))

    assert resolved != "agy-stub"
    assert Path(resolved) == stub


def test_agy_bin_path_keeps_unresolvable_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    monkeypatch.setenv("PATH", str(empty_dir))

    assert agy_bin_path(Config(agy_bin="agy-stub")) == "agy-stub"


def test_supervise_keeps_result_through_undecodable_output(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result_event = {
        "type": "result",
        "status": "SUCCESS",
        "response": _CJK_RESPONSE,
        "conversation_id": "conv-utf8",
        "duration_seconds": 1.0,
        "num_turns": 1,
        "structured_output": {
            "summary": _CJK_SUMMARY,
            "files_changed": [],
            "tests_passed": True,
        },
    }
    stub = _write_stream_stub(
        tmp_path / "cjk_agy.py",
        [
            _event_line({"type": "tool_use", "name": _CJK_TOOL}),
            b"\xff\xfe not valid utf-8\n",
            b"warmup line, not json\n",
            _event_line(result_event),
        ],
    )
    job_id = "j-cjk"
    jdir = _seed_job(git_repo, job_id, stub, monkeypatch)

    supervise_round(job_id, 1, git_repo)

    meta = read_meta(git_repo, job_id)
    assert meta["state"] == "done", meta.get("error")
    assert meta["agy_status"] == "SUCCESS"
    assert meta["conversation_id"] == "conv-utf8"

    result = json.loads(result_path(git_repo, job_id).read_text(encoding="utf-8"))
    assert result["response_text"] == _CJK_RESPONSE
    assert result["summary"] == _CJK_SUMMARY

    events = (jdir / "events.ndjson").read_text(encoding="utf-8")
    assert _CJK_TOOL in events

    stderr_log = (jdir / "stderr.log").read_text(encoding="utf-8")
    assert "[parse error]" in stderr_log
    assert "[reader error]" not in stderr_log


def test_supervise_reports_reader_failure_in_stderr_log(
    git_repo: Path, fake_agy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = "j-reader-error"
    jdir = _seed_job(git_repo, job_id, fake_agy, monkeypatch)
    real_write_event = supervise._write_event

    def _fail_on_result(project: Path, job_id: str, event: dict) -> None:
        if event.get("type") == "result":
            raise RuntimeError("events.ndjson unwritable")
        real_write_event(project, job_id, event)

    monkeypatch.setattr(supervise, "_write_event", _fail_on_result)
    supervise_round(job_id, 1, git_repo)

    stderr_log = (jdir / "stderr.log").read_text(encoding="utf-8")
    assert "[reader error]" in stderr_log
    assert "events.ndjson unwritable" in stderr_log
    # The event reached `events` before the write failed, so the round still lands.
    assert read_meta(git_repo, job_id)["state"] == "done"
