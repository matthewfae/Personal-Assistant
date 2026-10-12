"""Tests for telegram_handler.split_message."""

from telegram_handler import _utf16_len, split_message


def test_short_text_unchanged():
    assert split_message("hello") == ["hello"]


def test_empty_and_whitespace_give_no_chunks():
    assert split_message("") == []
    assert split_message("   \n ") == []


def test_prefers_paragraph_break():
    text = "a" * 60 + "\n\n" + "b" * 30
    assert split_message(text, limit=80) == ["a" * 60, "b" * 30]


def test_hard_cut_when_no_separator():
    chunks = split_message("x" * 250, limit=100)
    assert chunks == ["x" * 100, "x" * 100, "x" * 50]


def test_emoji_counted_as_two_units():
    # Each emoji is outside the BMP, so 60 of them are 120 UTF-16 units.
    chunks = split_message("\U0001F600" * 60, limit=100)
    assert all(_utf16_len(c) <= 100 for c in chunks)
    assert "".join(chunks) == "\U0001F600" * 60
