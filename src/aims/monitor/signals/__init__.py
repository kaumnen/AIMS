"""Signals: deterministic detectors over the event log. Built-in ones and the declarative rules
ship with AIMS; plugins register under the `aims.signals` entry point group."""

from aims.monitor.signals.base import Hit, Signal, SignalError, SignalSetup
from aims.monitor.signals.engine import SignalEngine, check_signals, signal_versions
from aims.monitor.signals.rules import validate_rules

__all__ = [
    "Hit",
    "Signal",
    "SignalEngine",
    "SignalError",
    "SignalSetup",
    "check_signals",
    "signal_versions",
    "validate_rules",
]
