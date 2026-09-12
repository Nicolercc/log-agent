import os
import subprocess


def _fake_python(tmp_path, *, classify_exit=0, review_stale_lines=("review health OK",)):
    review_stale_out = "\\n".join(review_stale_lines)
    script = tmp_path / "fake-python"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        "case \"$*\" in\n"
        f"  *classify.py*) exit {classify_exit} ;;\n"
        "  *'review stale'*) printf '%b\\n' \"" + review_stale_out + "\"; exit 0 ;;\n"
        "  *'jt.py where'*) exit 0 ;;\n"
        "  *'jt.py stale'*) echo '  (nothing)'; exit 0 ;;\n"
        "  *py_compile*) exit 0 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _fake_osascript(tmp_path, log_path):
    script = tmp_path / "fake-osascript"
    script.write_text(
        "#!/usr/bin/env bash\n"
        f'{{ echo "CALLED args=$*"; cat; }} >> "{log_path}"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _run_daily_stale(tmp_path, fake_python, fake_osascript):
    env = os.environ.copy()
    env["JT_PYTHON"] = str(fake_python)
    env["JT_LOG_DIR"] = str(tmp_path / "logs")
    env["JT_OSASCRIPT"] = str(fake_osascript)
    return subprocess.run(
        ["scripts/jt-daily-stale.sh"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )


def test_daily_stale_notifies_and_fails_loud_on_permanent_classify_failure(tmp_path):
    # The exit criterion in plain terms: a classify run that keeps failing
    # (Anthropic down, bad credentials, whatever) must produce both a
    # non-zero process exit (for launchd/log-based monitoring) AND an
    # actual notification call (for a human who isn't watching logs) --
    # not one or the other.
    fake_python = _fake_python(tmp_path, classify_exit=1)
    notify_log = tmp_path / "notify.log"
    fake_osascript = _fake_osascript(tmp_path, notify_log)

    result = _run_daily_stale(tmp_path, fake_python, fake_osascript)

    assert result.returncode != 0
    assert notify_log.exists()
    body = notify_log.read_text(encoding="utf-8")
    assert "CALLED" in body
    assert "jt automation failed" in body
    assert "exited 1" in body


def test_daily_stale_notifies_on_review_queue_alert_even_when_automation_succeeds(tmp_path):
    # A growing/aging review queue is a *different* failure mode from a
    # hard crash -- classify itself exits 0 here -- and must still surface
    # a notification, with wording that names what's actually wrong
    # (item count), not a generic "something's wrong".
    fake_python = _fake_python(
        tmp_path,
        classify_exit=0,
        review_stale_lines=(
            "review health: total=2 oldest_age_days=1 oldest_created_at=2026-09-01",
            "classify health: consecutive_failures=0 classifier_exhaustion=no log=x",
            "ALERT total_review_queue_size=2 threshold=1",
        ),
    )
    notify_log = tmp_path / "notify.log"
    fake_osascript = _fake_osascript(tmp_path, notify_log)

    result = _run_daily_stale(tmp_path, fake_python, fake_osascript)

    assert result.returncode == 0
    assert notify_log.exists()
    body = notify_log.read_text(encoding="utf-8")
    assert "2 item(s) awaiting review" in body
    # Not the failure subtitle -- this run didn't crash, it's a quality
    # signal, and the two must stay visually distinguishable.
    assert "jt needs attention" in body
    assert "jt automation failed" not in body


def test_daily_stale_stays_quiet_when_healthy(tmp_path):
    # The other half of "not too noisy": a clean run with an empty review
    # queue and no failure history must not page at all.
    fake_python = _fake_python(tmp_path, classify_exit=0)
    notify_log = tmp_path / "notify.log"
    fake_osascript = _fake_osascript(tmp_path, notify_log)

    result = _run_daily_stale(tmp_path, fake_python, fake_osascript)

    assert result.returncode == 0
    assert not notify_log.exists() or notify_log.read_text(encoding="utf-8") == ""


def test_jt_automation_preserves_classify_exit_status(tmp_path):
    fake_python = tmp_path / "fake-python"
    fake_python.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        "case \"$*\" in\n"
        "  *classify.py*) exit 42 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    env = os.environ.copy()
    env["JT_PYTHON"] = str(fake_python)
    env["JT_LOG_DIR"] = str(tmp_path / "logs")

    result = subprocess.run(
        ["scripts/jt-automation.sh"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )

    assert result.returncode == 42


def test_jt_automation_keeps_review_alerts_nonfatal(tmp_path):
    fake_python = tmp_path / "fake-python"
    fake_python.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        "case \"$*\" in\n"
        "  *'review stale --no-fail'*) echo 'ALERT total_review_queue_size=1 threshold=1'; exit 0 ;;\n"
        "  *'review stale'*) echo 'ALERT total_review_queue_size=1 threshold=1'; exit 7 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    env = os.environ.copy()
    env["JT_PYTHON"] = str(fake_python)
    env["JT_LOG_DIR"] = str(tmp_path / "logs")

    result = subprocess.run(
        ["scripts/jt-automation.sh", "--dry-run"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )

    assert result.returncode == 0


def test_jt_automation_loads_local_env_file_for_launchd(tmp_path):
    env_file = tmp_path / "jt.env"
    env_file.write_text("ANTHROPIC_API_KEY=from-jt-env\n", encoding="utf-8")

    fake_python = tmp_path / "fake-python"
    fake_python.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        "case \"$*\" in\n"
        "  *classify.py*) test \"${ANTHROPIC_API_KEY:-}\" = from-jt-env ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    env = os.environ.copy()
    env["JT_ENV_FILE"] = str(env_file)
    env["JT_PYTHON"] = str(fake_python)
    env["JT_LOG_DIR"] = str(tmp_path / "logs")
    env.pop("ANTHROPIC_API_KEY", None)

    result = subprocess.run(
        ["scripts/jt-automation.sh", "--dry-run"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )

    assert result.returncode == 0
