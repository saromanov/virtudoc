from virtudoc.tokens import estimate_tokens, truncate_to_tokens


def test_latin_tokens():
    assert estimate_tokens("aaaa") == 1
    assert estimate_tokens("hello world") == 3


def test_cyrillic_tokens():
    assert estimate_tokens("абв") == 1
    assert estimate_tokens("абвгд") == 2


def test_mixed_tokens():
    assert estimate_tokens("abcабв") == 2


def test_truncate_within_budget():
    text = "x" * 400
    truncated = truncate_to_tokens(text, 10)
    assert estimate_tokens(truncated) <= 11
    assert truncated.endswith("…")


def test_truncate_short_text_unchanged():
    assert truncate_to_tokens("короткий текст", 100) == "короткий текст"
