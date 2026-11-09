"""Fail-closed naming rules for optional local benchmark fixtures."""

import re


def valid_a14_name(name: str) -> str:
    if not re.fullmatch(r"bench_a14_[a-z0-9_]{1,40}", name):
        raise ValueError("Le benchmark exige un nom bench_a14_* fictif.")
    return name


def valid_reporting_name(name: str) -> str:
    if not re.fullmatch(r"bench_perf_m10_[a-z0-9_]{1,40}", name):
        raise ValueError("Le benchmark exige un nom bench_perf_m10_* fictif.")
    return name
