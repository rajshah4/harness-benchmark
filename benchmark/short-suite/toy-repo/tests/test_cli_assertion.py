from toyapp.cli import format_greeting


def test_format_greeting() -> None:
    assert format_greeting("Ada") == "Hello, Grace."
