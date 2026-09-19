"""The gate must distinguish actual assertions from unrelated exceptions."""
import xml.etree.ElementTree as ET

import pytest

from mutation_gate_provider_env_check import is_assertion_failure


@pytest.mark.parametrize("message, expected", [
    pytest.param("assert False\n + where False = all(generator)", True, id="rewritten-assert"),
    pytest.param("AssertionError: missing remedy", True, id="named-assertion"),
    pytest.param("RuntimeError: assert False", False, id="runtime-error"),
    pytest.param("ValueError: AssertionError in data", False, id="exception-mentions-assertion"),
])
def test_gate_accepts_only_assertion_failures(message, expected):
    failure = ET.Element("failure", message=message)
    assert is_assertion_failure(failure) is expected
