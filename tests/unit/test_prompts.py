"""Tests for user confirmation prompts.

Prompts are the safety gate before destructive operations. The framework
needs to handle three cases:
1. Interactive terminal: show prompt, wait for input
2. Non-TTY (CI/piped): auto-deny (safe default) unless --yes
3. --yes flag: auto-confirm everything (for scripted use)
"""
from unittest.mock import patch

import pytest


class TestConfirm:
    """confirm() asks the user a yes/no question."""

    def test_yes_flag_skips_prompt(self):
        """When --yes is active, confirm() returns True without asking."""
        from qbrd_tools.prompts import confirm

        assert confirm("Delete everything?", yes=True) is True

    def test_non_tty_auto_denies(self):
        """When stdin is not a TTY (CI, piped), confirm() returns False."""
        from qbrd_tools.prompts import confirm

        with patch("qbrd_tools.prompts._is_tty", return_value=False):
            assert confirm("Delete everything?", yes=False) is False

    def test_user_types_y(self):
        from qbrd_tools.prompts import confirm

        with patch("qbrd_tools.prompts._is_tty", return_value=True), \
             patch("builtins.input", return_value="y"):
            assert confirm("Continue?", yes=False) is True

    def test_user_types_n(self):
        from qbrd_tools.prompts import confirm

        with patch("qbrd_tools.prompts._is_tty", return_value=True), \
             patch("builtins.input", return_value="n"):
            assert confirm("Continue?", yes=False) is False

    def test_empty_input_defaults_to_no(self):
        from qbrd_tools.prompts import confirm

        with patch("qbrd_tools.prompts._is_tty", return_value=True), \
             patch("builtins.input", return_value=""):
            assert confirm("Continue?", yes=False) is False


class TestConfirmDestructive:
    """confirm_destructive() requires typing 'yes' for dangerous operations."""

    def test_yes_flag_skips_prompt(self):
        from qbrd_tools.prompts import confirm_destructive

        assert confirm_destructive("Drop database?", yes=True) is True

    def test_requires_full_yes(self):
        from qbrd_tools.prompts import confirm_destructive

        with patch("qbrd_tools.prompts._is_tty", return_value=True), \
             patch("builtins.input", return_value="yes"):
            assert confirm_destructive("Drop database?", yes=False) is True

    def test_y_is_not_enough(self):
        """'y' doesn't count for destructive -- must type full 'yes'."""
        from qbrd_tools.prompts import confirm_destructive

        with patch("qbrd_tools.prompts._is_tty", return_value=True), \
             patch("builtins.input", return_value="y"):
            assert confirm_destructive("Drop database?", yes=False) is False
