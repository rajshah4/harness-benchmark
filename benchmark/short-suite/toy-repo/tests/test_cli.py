from toyapp.cli import format_greeting


def test_format_greeting_current_behavior() -> None:
    assert format_greeting("Ada") == "Hello, Ada."
