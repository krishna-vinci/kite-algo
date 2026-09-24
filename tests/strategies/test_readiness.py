"""First-run readiness: what the static check can and cannot promise.

The contract is deliberately narrow: the helper parses with ``ast``, never
imports or executes the source, names the entrypoint/syntax/known-dependency
problems it can prove, and reports dynamic or guarded-optional imports as
``unknown`` instead of as a pass.
"""

from __future__ import annotations

import pytest

from backend.strategies import readiness


def _checks(result):
    return {check["id"]: check for check in result["checks"]}


def test_ready_source_with_a_compatible_entrypoint():
    result = readiness.assess_source_readiness(
        "import pandas as pd\n\n\ndef main(ctx):\n    return pd.__name__\n"
    )
    assert result["status"] == "ready"
    assert result["entrypoint"]["found"] is True
    assert result["entrypoint"]["compatible"] is True
    assert result["imports"]["available"] == ["pandas"]
    assert result["imports"]["missing"] == []
    assert result["imports"]["providers"] == {"pandas": "pandas"}


def test_default_argument_entrypoint_is_compatible():
    result = readiness.assess_source_readiness("def main(ctx, note=None):\n    return 0\n")
    assert result["status"] == "ready"
    assert result["entrypoint"]["compatible"] is True


def test_optional_keyword_only_argument_is_compatible():
    result = readiness.assess_source_readiness("def main(ctx, *, note=None):\n    return 0\n")
    assert result["status"] == "ready"
    assert result["entrypoint"]["compatible"] is True


def test_required_keyword_only_argument_is_blocked():
    """``main(ctx, *, required)`` cannot be called by the bootstrap."""
    result = readiness.assess_source_readiness("def main(ctx, *, required):\n    return 0\n")
    assert result["status"] == "blocked"
    check = _checks(result)["entrypoint"]
    assert check["status"] == "blocked"
    assert "keyword-only" in check["detail"]
    assert "required" in check["detail"]
    assert result["entrypoint"]["compatible"] is False


def test_main_redefinition_reports_unknown_and_never_ready():
    source = (
        "def main(ctx):\n"
        "    return 1\n"
        "\n"
        "\n"
        "main = None\n"
    )
    result = readiness.assess_source_readiness(source)
    assert result["status"] == "blocked"
    check = _checks(result)["entrypoint"]
    assert check["status"] == "unknown"
    assert "assigned at module level" in check["detail"]


def test_two_module_level_mains_report_unknown_and_never_ready():
    source = "def main(ctx):\n    return 1\n\n\ndef main(ctx):\n    return 2\n"
    result = readiness.assess_source_readiness(source)
    assert result["status"] == "blocked"
    check = _checks(result)["entrypoint"]
    assert check["status"] == "unknown"
    assert "defined 2 times" in check["detail"]


def test_import_bound_and_deleted_main_report_unknown():
    imported = readiness.assess_source_readiness("import os as main\n")
    assert imported["status"] == "blocked"
    assert _checks(imported)["entrypoint"]["status"] == "unknown"
    assert "import" in _checks(imported)["entrypoint"]["detail"]

    deleted = readiness.assess_source_readiness("def main(ctx):\n    return 1\n\n\ndel main\n")
    assert deleted["status"] == "blocked"
    assert _checks(deleted)["entrypoint"]["status"] == "unknown"


def test_syntax_error_is_reported_not_raised():
    result = readiness.assess_source_readiness("def main(ctx)\n    return 1\n")
    assert result["status"] == "blocked"
    assert _checks(result)["syntax"]["status"] == "blocked"
    assert "syntax error" in _checks(result)["syntax"]["detail"]
    assert _checks(result)["entrypoint"]["status"] == "unknown"
    assert _checks(result)["imports"]["status"] == "unknown"


def test_missing_entrypoint_is_blocked_and_named():
    result = readiness.assess_source_readiness("value = 1\n")
    assert result["status"] == "blocked"
    check = _checks(result)["entrypoint"]
    assert check["status"] == "blocked"
    assert "no module-level 'main'" in check["detail"]


def test_nested_main_is_named_as_not_module_level():
    result = readiness.assess_source_readiness("def helper():\n    def main(ctx):\n        return 1\n")
    assert result["status"] == "blocked"
    assert "not defined at module level" in _checks(result)["entrypoint"]["detail"]


def test_incompatible_signatures_are_named():
    no_argument = readiness.assess_source_readiness("def main():\n    return 1\n")
    assert no_argument["status"] == "blocked"
    assert "no positional argument" in _checks(no_argument)["entrypoint"]["detail"]

    two_required = readiness.assess_source_readiness("def main(ctx, config):\n    return 1\n")
    assert two_required["status"] == "blocked"
    assert "requires 2 positional" in _checks(two_required)["entrypoint"]["detail"]

    asynchronous = readiness.assess_source_readiness("async def main(ctx):\n    return 1\n")
    assert asynchronous["status"] == "blocked"
    assert "async def" in _checks(asynchronous)["entrypoint"]["detail"]


def test_known_missing_dependency_blocks_with_the_profile_named():
    result = readiness.assess_source_readiness("import scipy\n\n\ndef main(ctx):\n    return 0\n")
    assert result["status"] == "blocked"
    check = _checks(result)["imports"]
    assert check["status"] == "blocked"
    assert "scipy" in check["detail"]
    assert readiness.RUNTIME_PROFILE["id"] in check["remediation"]
    assert "pandas" in check["remediation"]


def test_stdlib_and_sdk_imports_are_available():
    result = readiness.assess_source_readiness(
        "import json\nimport os.path\nfrom kite_algo_worker import indicators\n\n\ndef main(ctx):\n    return 0\n"
    )
    assert result["status"] == "ready"
    assert set(result["imports"]["available"]) >= {"json", "os", "kite_algo_worker"}
    assert result["imports"]["missing"] == []


def test_dynamic_imports_are_unknown_not_a_pass():
    result = readiness.assess_source_readiness(
        "import importlib\n\n\ndef main(ctx):\n    return importlib.import_module('scipy')\n"
    )
    assert result["status"] == "ready"
    assert result["imports"]["dynamic"] is True
    assert _checks(result)["imports"]["status"] == "unknown"
    assert any("dynamic" in message for message in result["messages"])


def test_guarded_supported_import_is_available():
    source = (
        "try:\n"
        "    import numba\n"
        "except ImportError:\n"
        "    numba = None\n"
        "\n"
        "\n"
        "def main(ctx):\n"
        "    return numba\n"
    )
    result = readiness.assess_source_readiness(source)
    assert result["status"] == "ready"
    assert result["imports"]["optional_missing"] == []
    assert result["imports"]["optional_available"] == ["numba"]


def test_guarded_unsupported_import_is_reported_as_unknown():
    source = (
        "try:\n"
        "    import scipy\n"
        "except ImportError:\n"
        "    scipy = None\n"
        "\n"
        "\n"
        "def main(ctx):\n"
        "    return scipy\n"
    )
    result = readiness.assess_source_readiness(source)
    assert result["status"] == "ready"
    assert result["imports"]["optional_missing"] == ["scipy"]
    assert _checks(result)["optional_imports"]["status"] == "unknown"


def test_empty_or_oversized_source_is_rejected():
    with pytest.raises(ValueError):
        readiness.assess_source_readiness("")
    with pytest.raises(ValueError):
        readiness.assess_source_readiness("x = 1\n" * 100000)


def test_profile_payload_is_the_documented_runner_profile():
    profile = readiness.profile_payload()
    assert profile["id"] == "hosted-python-dataframe-indicators"
    assert profile["python"] == "3.14"
    assert profile["runtime_pip_install"] is False
    assert profile["server_side_indicators"] is True
    import_names = {entry["import_name"] for entry in profile["packages"]}
    assert {"pandas", "numpy", "numba"} <= import_names
