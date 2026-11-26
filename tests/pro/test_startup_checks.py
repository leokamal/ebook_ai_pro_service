"""A wrong Supabase setting must be named in the log, not flood it."""
import logging
import socket

import httpx

from app.core.config import config_problems, get_settings
from app.workers.runner import ThreadJobRunner


def settings(monkeypatch, url, anon="sb_publishable_x"):
    monkeypatch.setenv("SUPABASE_URL", url)
    monkeypatch.setenv("SUPABASE_ANON_KEY", anon)
    return get_settings()


def test_values_are_stripped(monkeypatch):
    s = settings(monkeypatch, "  https://ref.supabase.co/ \n", " sb_publishable_x\n")
    assert (s.supabase_url, s.anon_key) == ("https://ref.supabase.co", "sb_publishable_x")


def test_problems_are_reported_without_leaking_keys(monkeypatch):
    def no_dns(*_):
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", lambda *_: [])
    assert config_problems(settings(monkeypatch, "https://ref.supabase.co")) == []
    assert "must look like" in config_problems(settings(monkeypatch, "ref.supabase.co"))[0]
    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    problems = config_problems(settings(monkeypatch, "https://gone.supabase.co", "sb_secret_abc123"))
    assert len(problems) == 2 and "gone.supabase.co" in problems[0] and "SECRET" in problems[1]
    assert "abc123" not in " ".join(problems)


def test_worker_backs_off_quietly_when_supabase_is_unreachable(caplog):
    waits = []

    class Queue:
        def claim(self, *_):
            raise httpx.ConnectError("[Errno -2] Name or service not known")

    runner = ThreadJobRunner(Queue(), handler=None, poll_seconds=5)

    def wait(seconds):
        waits.append(seconds)
        if len(waits) == 8:
            runner._stop.set()

    runner._stop.wait = wait
    with caplog.at_level(logging.ERROR, logger="uvicorn.error"):
        runner._loop()
    assert waits[:4] == [10, 20, 40, 80] and waits[-1] == 300
    assert all(r.exc_info is None and "cannot reach Supabase" in r.message for r in caplog.records)
