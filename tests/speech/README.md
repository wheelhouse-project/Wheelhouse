# Speech Pattern Test Suite

## Overview

Comprehensive test suite for WheelHouse speech pattern system covering all patterns in `patterns.toml`.

**Key Feature**: Automated parameter substitution validation catches bugs like the `{g1}` literal string issue automatically!

## Test Coverage

### Smoke Tests - `test_smoke_all_patterns.py`
✅ **Every pattern** in `patterns.toml` with automatic validation:
- Pattern matching works correctly
- Capture groups (g1, g2, g3) extract values properly
- **Parameter substitution** - captured values appear in action params (not literals)
- Action types match expectations

### Comprehensive Tests - `test_comprehensive_patterns.py`
✅ **Deep validation** of critical patterns:
- All bracket variations (`[]`, `{}`, `()`)
- All quote variations
- Boundary conditions (empty, single char, max length)
- Negative tests (inputs that shouldn't match)
- Multi-group captures (g1 AND g2)
- Delete with optional numbers
- Activate window patterns
- Multi-step actions

### Edge Case Tests - `test_edge_cases.py`
✅ **Error conditions** and special scenarios:
- Empty utterances
- Whitespace handling
- Null values
- Pattern conflicts
- Timeout scenarios  

## Running Tests

### Prerequisites
Run from `services/wheelhouse/`:
```bash
uv sync  # Install all dependencies including pytest into the service venv
```

### Run All Tests
```bash
# All tests
uv run pytest tests/speech/ -v

# Quick run (stop on first failure)
uv run pytest tests/speech/ -x

# With output
uv run pytest tests/speech/ -v -s
```

### Run Specific Test Files
```bash
# Smoke tests only
uv run pytest tests/speech/test_smoke_all_patterns.py -v

# Comprehensive tests only
uv run pytest tests/speech/test_comprehensive_patterns.py -v

# Edge cases only
uv run pytest tests/speech/test_edge_cases.py -v
```

### Run Specific Test Class
```bash
uv run pytest tests/speech/test_comprehensive_patterns.py::TestBracketQuotePatterns -v
```

### Run Specific Test
```bash
uv run pytest tests/speech/test_comprehensive_patterns.py::TestBracketQuotePatterns::test_all_bracket_variations -v
```

### Run with Coverage
```bash
uv run pytest tests/speech/ --cov=services.wheelhouse.speech --cov-report=html
```

## Key Test Cases Explained

### 🔴 CRITICAL: Parameter Substitution Validation
**Files**: All smoke tests in `test_smoke_all_patterns.py`  
**Why Critical**: Catches bugs where captured regex groups aren't properly substituted into action parameters  
**Example Bug**: `"activate chrome"` returned params with literal `"g1"` instead of `"chrome"`  
**How It Works**: 
```python
# Pattern: ^activate (\b\w+\b)$ with input "activate chrome"
# Expected: g1 captures "chrome"
# Test validates: "chrome" appears in actual params (not "g1")
assert "chrome" in str(actual_params)
```

### 🔴 CRITICAL: Multi-Group Captures  
**Test**: `TestMultiGroupCaptures` class  
**Why Critical**: Patterns with multiple capture groups (g1 AND g2) need both validated  
**Example**: `"backspace 5"` pattern `^(backspace|back space)\s*(\d+)?$`  
- g1 captures: "backspace"
- g2 captures: "5"  
- Both must appear in action params correctly  

### 🔴 CRITICAL: Bracket/Quote Variations
**Test**: `TestBracketQuotePatterns` class  
**Why Critical**: Complex patterns with optional markers need exhaustive testing  
**Examples**:
- `"open square bracket"` → `[`
- `"close curly braces"` → `}}`  
- `"open quotes hello close quotes"` → `"hello"`  

## Test Structure

### Test Files
- **test_smoke_all_patterns.py** - 84 auto-generated tests (one per pattern)
- **test_comprehensive_patterns.py** - 20 hand-written tests for critical scenarios
- **test_edge_cases.py** - 14 hand-written tests for error conditions
- **test_harness.py** - Mock infrastructure (MockApp, RealTextParser)
- **generate_smoke_tests.py** - Test generator script

### Test Classes (Comprehensive Tests)
- **TestBracketQuotePatterns** - All bracket/quote variations
- **TestBoundaryConditions** - Empty, single char, max length
- **TestNegativeTests** - Inputs that shouldn't match
- **TestMultiGroupCaptures** - Patterns using g1 AND g2
- **TestDeleteWithNumber** - Optional numeric parameters
- **TestActivatePattern** - Window activation
- **TestFindMultiAction** - Multi-step action sequences

## Fixtures

- `catalog` - Real PatternCatalog loaded from `patterns.toml`
- `parser_and_app` - Tuple of (RealTextParser, MockApp) for each test
- MockApp records actions without executing them
- RealTextParser wraps production TextParser with real pattern matching

## Regenerating Smoke Tests

When `patterns.toml` changes, regenerate smoke tests:

```bash
cd tests/speech
python generate_smoke_tests.py
```

The generator:
1. Loads all patterns from `patterns.toml`
2. Converts regex patterns to test inputs (e.g., `^delete\s*(\d+)?$` → `"delete 5"`)
3. Tracks capture groups (g1='5')
4. Generates assertions that validate captured values appear in params
5. Writes `test_smoke_all_patterns.py` with generated test functions

## What Gets Validated

Every smoke test validates:
1. ✅ Pattern matches the input
2. ✅ Action is executed (not None)
3. ✅ Action type matches expected (e.g., "hotkey_action")
4. ✅ **Captured values appear in parameters** (not literal "g1", "g2")

Example generated test:
```python
async def test_command_012_backspace_5(self, parser_and_app):
    """Pattern: ^(backspace|back space)\s*(\d+)?$"""
    parser, app = parser_and_app
    
    matched = await parser.parse_and_execute("backspace 5")
    
    assert matched, "Pattern should match: backspace 5"
    assert app.last_action is not None, "Should execute action"
    assert app.last_action.get("action") == "press_key_action"
    
    # Validate parameter substitution - THIS IS THE KEY!
    actual_params = app.last_action.get("params", {})
    assert "5" in str(actual_params) or 5 in actual_params
```

## Results

Run `pytest tests/speech/ -v --tb=short` for current test counts.

**Key Achievement**: Parameter substitution is validated across all patterns, automatically catching bugs like the `{g1}` literal string issue!

## Troubleshooting

### Import Errors
If you see import errors, ensure you're running from `services/wheelhouse/`:
```bash
cd services/wheelhouse
uv run pytest ../../tests/speech/ -v
```

### Async Warnings
All test functions should be marked with `@pytest.mark.asyncio` and defined as `async def`.

### Pattern Changes
After modifying `patterns.toml`, regenerate smoke tests:
```bash
python tests/speech/generate_smoke_tests.py
```
