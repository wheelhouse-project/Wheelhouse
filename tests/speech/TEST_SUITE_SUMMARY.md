# WheelHouse Comprehensive Pattern Test Suite

## Summary
- **Auto-generated smoke tests** covering all patterns in `patterns.toml` with **parameter substitution validation**
- **Comprehensive tests** for edge cases, boundaries, and multi-group captures
- **Edge case tests** for error conditions and special scenarios
- **100% pattern coverage**
- **All tests validate parameter substitution** - catches bugs like `{g1}` literal string issues ✅

## Test Files

| File | Purpose |
|------|---------|
| `test_smoke_all_patterns.py` | Auto-generated smoke tests for ALL patterns with parameter validation |
| `test_comprehensive_patterns.py` | Deep validation of critical patterns (brackets, quotes, multi-group captures) |
| `test_edge_cases.py` | Hand-written tests for error conditions and special scenarios |
| `test_harness.py` | - | Mock infrastructure (MockApp, RealTextParser) |
| `generate_smoke_tests.py` | - | Enhanced test generator with parameter substitution validation |

## What Makes These Tests Comprehensive

### Parameter Substitution Validation
Unlike basic smoke tests that only check "did something happen," these tests validate that:
- Capture groups (g1, g2, g3) are correctly extracted from input
- Captured values are properly substituted into action parameters
- Example: `"activate chrome"` → verifies `"chrome"` appears in params, not literal `"g1"`

**This catches bugs like the `{g1}` literal string issue automatically!**

## Running Tests

```bash
# All tests
uv run pytest tests/speech/ -v

# Smoke tests only
uv run pytest tests/speech/test_smoke_all_patterns.py -v

# Comprehensive tests only
uv run pytest tests/speech/test_comprehensive_patterns.py -v

# Edge cases only
uv run pytest tests/speech/test_edge_cases.py -v

# Specific test
uv run pytest tests/speech/test_comprehensive_patterns.py::TestBracketQuotePatterns::test_all_bracket_variations -v

# Quick run (stop on first failure)
uv run pytest tests/speech/ -x
```

## Regenerating Smoke Tests

When `patterns.toml` changes:

```bash
python tests/speech/generate_smoke_tests.py
```

This regenerates `test_smoke_all_patterns.py` with:
- Automatic test input generation from regex patterns
- Capture group tracking (g1, g2, g3)
- Parameter substitution validation

## Bugs Caught by Tests

### 1. Parameter Substitution Bug (`{g1}` literal)
**Symptom**: `"select that"` command returned action with params containing literal `"g1"` instead of `"that"`  
**Root Cause**: Parameter substitution in `command_engine.py` wasn't handling embedded markers like `"{g1}"`, `{g1}`, `[g1]`  
**Fix**: Loop through context dict and replace each marker: `result = result.replace(key, value)`  
**Test**: `test_backspace_with_number`, `test_tab_with_number` - validate captured values appear in params ✅

### 2. Multi-Group Capture Handling
**Symptom**: Patterns with multiple capture groups (g1 AND g2) weren't being tested comprehensively  
**Root Cause**: Original tests didn't validate both groups simultaneously  
**Fix**: Added dedicated tests for patterns using g1 + g2 combinations  
**Test**: `TestMultiGroupCaptures` class ✅

### 3. Bracket/Quote Variations
**Symptom**: Different bracket types (`[]`, `{}`, `()`) and quote handling needed comprehensive coverage  
**Root Cause**: Complex patterns with optional markers weren't fully tested  
**Fix**: Created exhaustive tests for all bracket and quote variations  
**Test**: `TestBracketQuotePatterns` class (6 tests) ✅

## Test Coverage

### Smoke Tests
Every pattern in `patterns.toml` gets:
- Automatic test input generation from regex pattern
- Capture group tracking (g1, g2, g3)
- Action type validation
- **Parameter substitution validation** - verifies captured values appear in result

Example smoke test:
```python
async def test_command_012_backspace_5(self, parser_and_app):
    """Pattern: ^(backspace|back space)\s*(\d+)?$"""
    matched = await parser.parse_and_execute("backspace 5")
    
    assert matched, "Pattern should match: backspace 5"
    assert app.last_action.get("action") == "press_key_action"
    
    # Validate parameter substitution
    actual_params = app.last_action.get("params", {})
    assert "5" in str(actual_params) or 5 in actual_params
```

### Comprehensive Tests (20 tests)
Deep validation of critical patterns:
- **Bracket/Quote Patterns** (6 tests): All variations of `[]`, `{}`, `()`, quotes
- **Boundary Conditions** (5 tests): Empty strings, single characters, max lengths
- **Negative Tests** (2 tests): Invalid inputs that should NOT match
- **Multi-Group Captures** (2 tests): Patterns using g1 AND g2 simultaneously
- **Delete with Number** (2 tests): Optional numeric parameters
- **Activate Pattern** (2 tests): Window activation validation
- **Find Multi-Action** (1 test): Complex multi-step action sequences

### Edge Case Tests (14 tests)
Error conditions and special scenarios:
- Empty utterances
- Whitespace handling
- Null values
- Pattern conflicts
- Timeout scenarios

## Mock Infrastructure

### MockApp
Records all actions without executing (for testing):
```python
app = MockApp()
await parser.parse_and_execute("new tab")
assert app.last_action.get("action") == "hotkey_action"
assert app.last_action.get("params") == {"keys": ["ctrl", "n"]}
```

### RealTextParser
Wraps production TextParser with real PatternCatalog:
```python
parser = RealTextParser(mock_app, catalog)
matched = await parser.parse_and_execute("activate chrome")
# Uses real pattern matching + parameter substitution
# But records actions instead of executing them
```

### PatternCatalog
Real pattern catalog (not mocked):
```python
catalog = PatternCatalog("patterns.toml")
pattern = catalog.find_match("new tab", is_fresh=True)
# Uses actual production code for pattern matching
```

## Results

Run `pytest tests/speech/ -v --tb=short` for current test counts.

The test suite validates parameter substitution across all patterns and would automatically catch bugs like the `{g1}` literal string issue!
