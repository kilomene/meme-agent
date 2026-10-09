"""Test package bootstrap.

Marks this process as a test run (FOREX_AGENT_TESTING=1) so production
side-effect sinks never touch the user's live files: the backtest
dataset recorder skips writes aimed at the live dataset path, and
similar guards key off the same marker. Tests that deliberately
exercise those writers pass an explicit tmp path instead of relying
on live config — explicit paths always write, even under test.
"""
import os

os.environ.setdefault("FOREX_AGENT_TESTING", "1")
