"""Shared test harness helpers; no application fallback is permitted."""
from __future__ import annotations

import os
import unittest

from test_environment import (
    TEST_DATABASE_URL_ENV,
    TEST_OBJECT_ROOT_ENV,
    GuardedTestEnvironment,
    guarded_test_environment,
    test_mode_enabled,
)


def guarded_environment_or_skip() -> GuardedTestEnvironment:
    """Skip only when the entire optional integration environment is absent.

    Partial or unsafe configuration is an error, not a skip: this makes CI and local
    invocation fail closed while allowing the offline suite to run without credentials.
    """
    has_url = bool(os.environ.get(TEST_DATABASE_URL_ENV))
    has_root = bool(os.environ.get(TEST_OBJECT_ROOT_ENV))
    if not has_url and not has_root and not test_mode_enabled():
        raise unittest.SkipTest(
            "guarded integration environment not configured; set TEST_DATABASE_URL "
            "and TEST_OBJECT_ROOT"
        )
    environment = guarded_test_environment()
    environment.activate()
    return environment
