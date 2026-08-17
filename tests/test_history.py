from abq import history


def test_append_and_read(tmp_path):
    path = str(tmp_path / "history.jsonl")
    history.append({"from": "api", "to": "web", "text": "hello"}, path=path)
    history.append({"from": "web", "to": "api", "text": "hi"}, path=path)

    records = history.read(path=path)
    assert [r["text"] for r in records] == ["hello", "hi"]
    assert records[0]["ts"].endswith("Z")


def test_read_limit_returns_the_tail(tmp_path):
    path = str(tmp_path / "history.jsonl")
    for i in range(5):
        history.append({"from": "a", "to": "b", "text": str(i)}, path=path)

    assert [r["text"] for r in history.read(limit=2, path=path)] == ["3", "4"]


def test_read_missing_file(tmp_path):
    assert history.read(path=str(tmp_path / "none.jsonl")) == []


def test_format_line_is_single_line_and_plain(tmp_path):
    line = history.format_line(
        {"ts": "2026-08-17T01:02:03.000Z", "from": "a", "to": "b", "text": "x\ny"},
        color=False,
    )
    assert line == "01:02:03 a -> b  x y"


def test_unicode_survives_a_round_trip(tmp_path):
    path = str(tmp_path / "history.jsonl")
    history.append({"from": "a", "to": "b", "text": "привет 👋"}, path=path)

    assert history.read(path=path)[0]["text"] == "привет 👋"
