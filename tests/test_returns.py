import json
import os
import threading

import pytest

from abq import returns


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ABQ_HOME", str(tmp_path))


def test_channel_is_one_shot_and_result_is_resumable():
    token = returns.open_channel(ttl=60)
    message_id = returns.send(token, "готово")

    first = returns.wait(token, timeout=0)
    second = returns.wait(token, timeout=0)

    assert first == second
    assert first["id"] == message_id
    assert first["text"] == "готово"
    with pytest.raises(returns.ReturnChannelError, match="already"):
        returns.send(token, "duplicate")


def test_wait_can_time_out_without_consuming_the_channel():
    token = returns.open_channel(ttl=60)

    with pytest.raises(returns.ReturnPending):
        returns.wait(token, timeout=0)

    returns.send(token, "later")
    assert returns.wait(token, timeout=0)["text"] == "later"


def test_only_one_concurrent_sender_can_publish():
    token = returns.open_channel(ttl=60)
    outcomes = []

    def publish(text):
        try:
            returns.send(token, text)
            outcomes.append((text, "sent"))
        except returns.ReturnChannelError:
            outcomes.append((text, "rejected"))

    threads = [threading.Thread(target=publish, args=(text,)) for text in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(status for _, status in outcomes) == ["rejected", "sent"]
    assert returns.read(token)["text"] in {"a", "b"}


def test_payload_is_bounded():
    token = returns.open_channel(ttl=60)

    with pytest.raises(returns.ReturnChannelError, match="exceeds"):
        returns.send(token, "x" * (returns.MAX_PAYLOAD_BYTES + 1))


def test_expired_channels_are_removed(monkeypatch):
    current = [1000.0]
    monkeypatch.setattr(returns.time, "time", lambda: current[0])
    token = returns.open_channel(ttl=5)
    channel = os.path.join(returns.returns_path(), token)

    current[0] = 1006.0
    with pytest.raises(returns.ReturnChannelError, match="expired"):
        returns.read(token)
    assert not os.path.exists(channel)


def test_published_payload_gets_a_fresh_retention_window(monkeypatch):
    current = [1000.0]
    monkeypatch.setattr(returns.time, "time", lambda: current[0])
    token = returns.open_channel(ttl=5)
    channel = os.path.join(returns.returns_path(), token)

    current[0] = 1004.0
    returns.send(token, "late but valid")

    current[0] = 1006.0
    assert returns.read(token)["text"] == "late but valid"
    assert returns.cleanup_expired(now=current[0]) == 0

    current[0] = 1010.0
    assert returns.cleanup_expired(now=current[0]) == 1
    assert not os.path.exists(channel)


def test_publish_filesystem_race_is_a_transport_error(monkeypatch):
    token = returns.open_channel(ttl=60)
    real_open = returns.os.open

    def raced_open(path, flags, mode=0o777):
        if "result.tmp-" in os.fspath(path):
            raise FileNotFoundError(os.fspath(path))
        return real_open(path, flags, mode)

    monkeypatch.setattr(returns.os, "open", raced_open)

    with pytest.raises(returns.ReturnChannelError, match="cannot publish"):
        returns.send(token, "result")


def test_lock_timeout_is_a_return_channel_error(monkeypatch):
    token = returns.open_channel(ttl=60)

    class BusyLock:
        def __init__(self, target, timeout):
            pass

        def __enter__(self):
            raise TimeoutError("busy")

        def __exit__(self, *exc):
            pass

    monkeypatch.setattr(returns, "FileLock", BusyLock)

    with pytest.raises(returns.ReturnChannelError, match="busy"):
        returns.read(token)


def test_result_written_by_previous_format_keeps_publish_retention(monkeypatch):
    current = [1000.0]
    monkeypatch.setattr(returns.time, "time", lambda: current[0])
    token = returns.open_channel(ttl=5)
    returns.send(token, "compatible")
    result_path = os.path.join(returns.returns_path(), token, "result.json")
    with open(result_path, encoding="utf-8") as fh:
        record = json.load(fh)
    record.pop("expires_at")
    with open(result_path, "w", encoding="utf-8") as fh:
        json.dump(record, fh)

    current[0] = 1004.0
    assert returns.read(token)["text"] == "compatible"


def test_close_removes_the_address():
    token = returns.open_channel(ttl=60)
    returns.close(token)

    with pytest.raises(returns.ReturnChannelError, match="does not exist|corrupt"):
        returns.read(token)


def test_invalid_token_never_escapes_the_spool():
    with pytest.raises(returns.ReturnChannelError, match="invalid"):
        returns.read("../registry.json")
