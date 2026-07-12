"""
Generate smoke tests for all patterns in patterns.toml.

Smoke tests validate:
1. Pattern matches expected input
2. Code executes without crashing
3. Returns an action payload
4. Action type matches expectation from function name

Does NOT validate:
- Correctness of behavior (that's for targeted tests)
- Edge cases (that's for comprehensive tests)
- User intent (that requires documentation)
"""
import sys
from pathlib import Path
import tomllib
import re

project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

# The e2e suite is hand-maintained: greedy patterns signal end-of-utterance
# explicitly, some tests carry strict xfail markers, and unicode-routing
# tweaks were applied in place. This generator only drafts a scaffold for
# new patterns and must never overwrite the maintained file
# (wh-smoke-generator-crash).
E2E_MAINTAINED_FILE = project_root / "services" / "wheelhouse" / "tests" / "e2e" / "test_e2e_all_patterns.py"
E2E_SCAFFOLD_FILE = project_root / "services" / "wheelhouse" / "tests" / "e2e" / "e2e_all_patterns_scaffold.py"


def parse_pattern_groups(pattern: str) -> tuple[str, dict[str, str]]:
    r"""Convert regex pattern to test input and track capture group values.

    Processes capture groups in left-to-right order to correctly assign g1, g2, g3, etc.

    Returns:
        (test_input, group_values) where group_values maps 'g1', 'g2', etc. to captured text

    Examples:
        '^new tab$' -> ('new tab', {})
        r'^delete\s*(\d+)?$' -> ('delete 5', {'g1': '5'})
        r'^(backspace|back space)\s*(\d+)?$' -> ('backspace 5', {'g1': 'backspace', 'g2': '5'})
        r'^activate (\b\w+\b)$' -> ('activate chrome', {'g1': 'chrome'})
    """
    # Remove anchors
    text = pattern.replace('^', '').replace('$', '')

    # Remove lookbehind/lookahead assertions entirely - they don't consume input
    # (?<=...) positive lookbehind, (?<!...) negative lookbehind
    # (?=...) positive lookahead, (?!...) negative lookahead
    text = re.sub(r'\(\?<[=!][^)]+\)', '', text)
    text = re.sub(r'\(\?[=!][^)]+\)', '', text)

    # Track capture groups and their test values
    group_values = {}
    group_counter = 1

    # Replace \s patterns with space
    text = re.sub(r'\\s[*+]?', ' ', text)

    # Replace \b with nothing
    text = text.replace('\\b', '')

    # Handle non-capturing groups with alternations first: (?:foo|bar) -> foo
    def replace_non_capturing_alternation(match):
        content = match.group(1)
        if '|' in content:
            # Take first option from alternation
            first_option = content.split('|')[0].strip()
            return first_option
        return content

    text = re.sub(r'\(\?:([^)]+)\)', replace_non_capturing_alternation, text)

    # Process all capturing groups in left-to-right order
    # This regex finds any capturing group: (...)
    def replace_capture_group(match):
        nonlocal group_counter
        content = match.group(1)

        # Determine what kind of capture this is and generate test value
        if '|' in content:
            # Alternation - take first option
            first_option = content.split('|')[0].strip()
            test_value = first_option.replace('\\b', '').replace('\\w', 'w')
            group_values[f'g{group_counter}'] = test_value
            group_counter += 1
            return test_value
        elif '\\d+' in content:
            # Number capture
            group_values[f'g{group_counter}'] = '5'
            group_counter += 1
            return ' 5'
        elif '.+' in content:
            # Greedy plus
            group_values[f'g{group_counter}'] = 'hello world'
            group_counter += 1
            return 'hello world'
        elif '.*' in content:
            # Greedy star
            group_values[f'g{group_counter}'] = 'test'
            group_counter += 1
            return 'test'
        elif '\\w+' in content or 'w+' in content:
            # Word capture
            group_values[f'g{group_counter}'] = 'chrome'
            group_counter += 1
            return 'chrome'
        else:
            # Unknown pattern - use generic value
            group_values[f'g{group_counter}'] = 'value'
            group_counter += 1
            return 'value'

    # Replace all capturing groups - matches (...) but not (?:...)
    # This pattern: \( - literal open paren
    #              (?!\?:) - negative lookahead for ?: (non-capturing)
    #              (?!\?) - also exclude other special groups like (?=
    #              ([^)]+) - capture the contents
    #              \) - literal close paren
    text = re.sub(r'\((?!\?)([^)]+)\)', replace_capture_group, text)

    # Clean up any remaining regex artifacts
    text = text.replace('(?:', '').replace(')', '')
    text = text.replace('\\', '')
    text = re.sub(r'\s+', ' ', text).strip()

    # Handle optional markers
    text = text.replace('?', '')

    return text, group_values


def infer_action_type(function_name: str) -> str:
    """Map function names to expected action types in payloads."""
    mapping = {
        'hk': 'hotkey_action',
        'hotkey': 'hotkey_action',
        'press_keys': 'hotkey_action',
        'press': 'press_key_action',
        'type_text': 'type_text',
        'insert_text': 'intelligent_insert_text',
        'text': 'intelligent_insert_text',  # text() calls insert_text()
        'wrap_or_insert': 'wrap_or_insert',  # has its own action type
        'number_point': 'intelligent_insert_text',  # returns insert_text payload
        'activate': 'activate_window',
        'transform_selection': 'transform_selection',
        'skip_clipboard_restore': 'skip_clipboard_restore',
    }
    return mapping.get(function_name, function_name)


# Functions that don't produce action payloads (not testable via action recording)
# These either return None (async side effects) or return data for subsequent actions
UNTESTABLE_FUNCTIONS = {'run', 'gs', 'sleep', 'add_hint_to_stt', 'capture_clipboard', 'cursor_navigate'}


def generate_smoke_tests():
    """Generate smoke test file for all patterns."""
    
    patterns_file = project_root / "services" / "wheelhouse" / "speech" / "config" / "patterns.toml"
    
    with open(patterns_file, 'rb') as f:
        data = tomllib.load(f)
    
    patterns = data['pattern']
    
    # Generate test file
    output = []
    output.append('"""')
    output.append('AUTO-GENERATED SMOKE TESTS FOR ALL PATTERNS')
    output.append('')
    output.append('These tests validate that:')
    output.append('1. Each pattern matches its expected input')
    output.append('2. Code executes without crashing')
    output.append('3. Returns an action payload')
    output.append('4. Action type is reasonable')
    output.append('')
    output.append('Generated from patterns.toml.')
    output.append('To regenerate: python tests/speech/generate_smoke_tests.py')
    output.append('"""')
    output.append('')
    output.append('import pytest')
    output.append('from pathlib import Path')
    output.append('import sys')
    output.append('')
    output.append('project_root = Path(__file__).parent.parent.parent')
    output.append('sys.path.insert(0, str(project_root))')
    output.append('')
    output.append('from tests.speech.test_harness import MockApp, RealTextParser')
    output.append('from services.wheelhouse.speech.pattern_catalog import PatternCatalog')
    output.append('')
    output.append('')
    output.append('@pytest.fixture')
    output.append('def catalog():')
    output.append('    """Real pattern catalog."""')
    output.append('    patterns_file = str(project_root / "services" / "wheelhouse" / "speech" / "config" / "patterns.toml")')
    output.append('    return PatternCatalog(patterns_file)')
    output.append('')
    output.append('')
    output.append('@pytest.fixture')
    output.append('def parser_and_app(catalog):')
    output.append('    """Create parser and app for each test."""')
    output.append('    app = MockApp()')
    output.append('    parser = RealTextParser(app, catalog)')
    output.append('    return parser, app')
    output.append('')
    output.append('')
    
    # Group patterns by type
    command_patterns = []
    replacement_patterns = []
    
    for i, p in enumerate(patterns):
        pattern_str = p.get('pattern', '')
        if pattern_str.startswith('^'):
            command_patterns.append((i, p))
        else:
            replacement_patterns.append((i, p))
    
    # Generate command pattern tests
    output.append('class TestCommandPatterns:')
    output.append('    """Smoke tests for command patterns (^ anchor)."""')
    output.append('')
    
    for idx, pattern_data in command_patterns:
        pattern_str = pattern_data.get('pattern', '')
        actions = pattern_data.get('actions', [])

        if not actions:
            continue

        # Generate test input and track captured groups
        test_input, group_values = parse_pattern_groups(pattern_str)

        # Check if pattern has testable actions (ones that produce action payloads)
        testable_actions = [a for a in actions if a.get('function', '') not in UNTESTABLE_FUNCTIONS]
        has_testable_actions = len(testable_actions) > 0

        if has_testable_actions:
            # Get expected action (use LAST TESTABLE action since tests check app.last_action)
            last_action = testable_actions[-1]
            function_name = last_action.get('function', '')
            expected_action = infer_action_type(function_name)
            action_params = last_action.get('params', [])

            # `press_keys` consumes natural-language key phrases and normalizes them
            # into key lists (e.g., "control c" -> ["ctrl", "c"]). Use a valid
            # key phrase here instead of generic free text.
            if function_name == "press_keys" and pattern_str == r"^press (.+)$":
                test_input = "press control c"
                group_values["g1"] = "control c"
        else:
            # Pattern only has side-effect functions - we'll just test matching
            expected_action = None
            action_params = []
        
        # Build expected params with substitutions
        expected_params = []
        for param in action_params:
            if isinstance(param, str) and param in group_values:
                expected_params.append(group_values[param])
            else:
                expected_params.append(param)
        
        # Create test name
        test_name = test_input.replace(' ', '_').replace('"', '').replace("'", '')
        test_name = re.sub(r'[^a-zA-Z0-9_]', '', test_name)
        test_name = f"test_command_{idx:03d}_{test_name[:30]}"
        
        output.append(f'    @pytest.mark.asyncio')
        output.append(f'    async def {test_name}(self, parser_and_app):')
        # Escape backslashes in pattern for docstring
        escaped_pattern = pattern_str.replace('\\', '\\\\')
        output.append(f'        """Pattern: {escaped_pattern}"""')
        output.append(f'        parser, app = parser_and_app')
        output.append(f'        ')
        output.append(f'        matched = await parser.parse_and_execute("{test_input}")')
        output.append(f'        ')
        output.append(f'        assert matched, "Pattern should match: {test_input}"')

        if has_testable_actions:
            output.append(f'        assert app.last_action is not None, "Should execute action"')
            output.append(f'        assert app.last_action.get("action") == "{expected_action}", "Action type mismatch"')
        else:
            # Pattern only has side-effect functions (run, gs, etc.) - just verify it matched
            output.append(f'        # Pattern uses side-effect functions only (mocked in tests)')

        # Validate parameters if we have capture groups
        if has_testable_actions and group_values and action_params:
            if function_name == "press_keys":
                # press_keys transforms spoken words into normalized key tokens.
                # Direct string substitution checks on params are not meaningful.
                output.append(f'        # press_keys normalizes spoken key names; skip raw gN param assertion')
                output.append(f'    ')
                continue

            # Check which groups are actually used in params
            used_groups = []
            for param in action_params:
                if isinstance(param, str) and param in group_values:
                    used_groups.append((param, group_values[param]))
            
            if used_groups:
                output.append(f'        ')
                output.append(f'        # Validate parameter substitution')
                output.append(f'        actual_params = app.last_action.get("params", {{}})')
                for group_name, group_value in used_groups:
                    output.append(f'        # Expected {group_name}="{group_value}" to be substituted in params')
                    # Generate assertion that checks if the captured value appears in actual params
                    if group_value.isdigit():
                        # For numeric values, check both string and int representation
                        output.append(f'        assert "{group_value}" in str(actual_params) or {group_value} in (actual_params.values() if isinstance(actual_params, dict) else actual_params), \\')
                        output.append(f'            "Captured value {group_name}={group_value} should appear in params"')
                    else:
                        # For string values
                        output.append(f'        assert "{group_value}" in str(actual_params), \\')
                        output.append(f'            "Captured value {group_name}={group_value} should appear in params"')
        
        output.append(f'    ')
    
    # Generate replacement pattern tests
    output.append('')
    output.append('class TestReplacementPatterns:')
    output.append('    """Smoke tests for replacement patterns (no ^ anchor)."""')
    output.append('')
    
    for idx, pattern_data in replacement_patterns:
        pattern_str = pattern_data.get('pattern', '')
        actions = pattern_data.get('actions', [])

        if not actions:
            continue

        # Generate test input and track captured groups
        test_input, group_values = parse_pattern_groups(pattern_str)

        # Check if pattern has testable actions (ones that produce action payloads)
        testable_actions = [a for a in actions if a.get('function', '') not in UNTESTABLE_FUNCTIONS]
        has_testable_actions = len(testable_actions) > 0

        if has_testable_actions:
            # Get expected action (use LAST TESTABLE action since tests check app.last_action)
            last_action = testable_actions[-1]
            function_name = last_action.get('function', '')
            expected_action = infer_action_type(function_name)
        else:
            expected_action = None

        # Create test name
        test_name = test_input.replace(' ', '_').replace('"', '').replace("'", '')
        test_name = re.sub(r'[^a-zA-Z0-9_]', '', test_name)
        test_name = f"test_replacement_{idx:03d}_{test_name[:30]}"

        output.append(f'    @pytest.mark.asyncio')
        output.append(f'    async def {test_name}(self, parser_and_app):')
        # Escape backslashes in pattern for docstring
        escaped_pattern = pattern_str.replace('\\', '\\\\')
        output.append(f'        """Pattern: {escaped_pattern}"""')
        output.append(f'        parser, app = parser_and_app')
        output.append(f'        ')
        output.append(f'        matched = await parser.parse_and_execute("{test_input}")')
        output.append(f'        ')
        output.append(f'        assert matched, "Pattern should match: {test_input}"')

        if has_testable_actions:
            output.append(f'        assert app.last_action is not None, "Should execute action"')
            output.append(f'        assert app.last_action.get("action") == "{expected_action}", "Action type mismatch"')
        else:
            # Pattern only has side-effect functions (run, gs, etc.) - just verify it matched
            output.append(f'        # Pattern uses side-effect functions only (mocked in tests)')

        output.append(f'    ')
    
    # Write file
    output_file = project_root / "tests" / "speech" / "test_smoke_all_patterns.py"
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write('\n'.join(output))
    
    print(f"Generated {len(command_patterns) + len(replacement_patterns)} smoke tests")
    print(f"  - Command patterns: {len(command_patterns)}")
    print(f"  - Replacement patterns: {len(replacement_patterns)}")
    print(f"  - Output: {output_file}")


def generate_e2e_tests(patterns_file=None, output_file=None):
    """Generate an E2E test scaffold for all patterns.

    E2E tests send WordEvents through the full pipeline (SpeechProcessor ->
    TextParser -> UIActionHandler) and check OS-level recordings (keystrokes,
    clipboard writes) instead of action dicts.

    Writes E2E_SCAFFOLD_FILE by default, never E2E_MAINTAINED_FILE: the real
    suite is hand-maintained, so new-pattern tests are drafted here and then
    copied over and adjusted by hand.
    """
    if patterns_file is None:
        patterns_file = project_root / "services" / "wheelhouse" / "speech" / "config" / "patterns.toml"
    if output_file is None:
        output_file = E2E_SCAFFOLD_FILE

    with open(patterns_file, 'rb') as f:
        data = tomllib.load(f)

    patterns = data['pattern']

    output = []
    output.append('"""')
    output.append('E2E TEST SCAFFOLD - DRAFTS ONLY (not collected by pytest, gitignored)')
    output.append('')
    output.append('Generated from patterns.toml as a starting point for new-pattern tests.')
    output.append('The real suite is test_e2e_all_patterns.py, which is hand-maintained:')
    output.append('greedy patterns need explicit end-of-utterance markers, and some tests')
    output.append('carry strict xfail markers. Copy the tests you need from here into that')
    output.append('file and adjust by hand. Never overwrite that file with this one.')
    output.append('')
    output.append('These tests feed WordEvents through the full pipeline:')
    output.append('  WordEvent -> SpeechProcessor -> TextParser -> UIActionHandler -> Recording')
    output.append('')
    output.append('To regenerate this scaffold: python tests/speech/generate_smoke_tests.py --e2e')
    output.append('"""')
    output.append('')
    output.append('import asyncio')
    output.append('import pytest')
    output.append('from services.wheelhouse.tests.e2e.e2e_harness import E2EPipelineHarness')
    output.append('')
    output.append('')
    output.append('@pytest.fixture')
    output.append('async def harness(pattern_catalog):')
    output.append('    """Create and start an E2E pipeline harness."""')
    output.append('    h = E2EPipelineHarness(catalog=pattern_catalog)')
    output.append('    await h.start()')
    output.append('    yield h')
    output.append('    await h.stop()')
    output.append('')
    output.append('')

    # Group patterns by type
    command_patterns = []
    replacement_patterns = []

    for i, p in enumerate(patterns):
        pattern_str = p.get('pattern', '')
        if pattern_str.startswith('^'):
            command_patterns.append((i, p))
        else:
            replacement_patterns.append((i, p))

    # Generate command pattern tests
    output.append('class TestE2ECommandPatterns:')
    output.append('    """E2E tests for command patterns (^ anchor)."""')
    output.append('')

    for idx, pattern_data in command_patterns:
        pattern_str = pattern_data.get('pattern', '')
        actions = pattern_data.get('actions', [])

        if not actions:
            continue

        test_input, group_values = parse_pattern_groups(pattern_str)
        words = test_input.split()

        if not words:
            continue

        # Determine what the command should produce
        testable_actions = [a for a in actions if a.get('function', '') not in UNTESTABLE_FUNCTIONS]
        if not testable_actions:
            expected_type = 'side_effect'
        else:
            last_action = testable_actions[-1]
            function_name = last_action.get('function', '')
            expected_type = infer_action_type(function_name)

        # Create test name
        test_name = test_input.replace(' ', '_').replace('"', '').replace("'", '')
        test_name = re.sub(r'[^a-zA-Z0-9_]', '', test_name)
        test_name = f"test_e2e_cmd_{idx:03d}_{test_name[:30]}"

        requires_hotword = pattern_data.get('requires_hotword', False)
        escaped_pattern = pattern_str.replace('\\', '\\\\')

        output.append(f'    @pytest.mark.asyncio')
        output.append(f'    async def {test_name}(self, harness):')
        output.append(f'        """Pattern: {escaped_pattern}"""')

        # Send hotword first if pattern requires it
        if requires_hotword:
            output.append('        await harness.send_word(harness.hotword, start_of_utterance=True)')
            # Send pattern words after hotword
            for word in words:
                output.append(f'        await harness.send_word("{word}", delay_before_ms=50)')
        else:
            # Send each word as a WordEvent
            for i, word in enumerate(words):
                if i == 0:
                    output.append(f'        await harness.send_word("{word}", start_of_utterance=True)')
                else:
                    output.append(f'        await harness.send_word("{word}", delay_before_ms=50)')

        # Wait for command timeout
        output.append(f'        await harness.wait_for_timeout(1100)')

        # Assertion based on expected type and action details
        # Count total keystroke-producing actions (press + hk/hotkey)
        keystroke_actions = [a for a in actions if a.get('function') in ('press', 'hk', 'hotkey')]
        is_multi_action = len(keystroke_actions) > 1

        if expected_type == 'press_key_action':
            # Extract key name and repeat count from params
            last_press = [a for a in actions if a.get('function') == 'press'][-1]
            press_params = last_press.get('params', [])
            key_name = press_params[0] if press_params else 'unknown'
            # Repeat: second param, could be "g1" (resolved from group_values) or int
            repeat = 1
            if len(press_params) > 1:
                rp = press_params[1]
                if isinstance(rp, int):
                    repeat = rp
                elif isinstance(rp, str) and rp in group_values:
                    try:
                        repeat = int(group_values[rp])
                    except (ValueError, TypeError):
                        repeat = 1
            output.append('        keys = harness.recording.get_keystroke_keys()')
            if is_multi_action:
                # Multi-action pattern: just check last press key is present
                output.append(f'        assert ("{key_name}",) in keys, \\')
                output.append(f'            f"Expected {key_name} in keystrokes, got {{keys}}"')
            else:
                output.append(f'        assert keys == [("{key_name}",)] * {repeat}, \\')
                output.append(f'            f"Expected {repeat}x {key_name}, got {{keys}}"')
        elif expected_type == 'hotkey_action':
            # Extract key tuple from the last hotkey action. press_keys also
            # maps to hotkey_action but resolves its keys from the spoken
            # words at runtime, so a press_keys-only pattern has no hk/hotkey
            # action here and no static key tuple to assert on.
            hk_actions = [a for a in actions if a.get('function') in ('hk', 'hotkey')]
            key_parts = []
            if hk_actions:
                hk_params = hk_actions[-1].get('params', [])
                # Filter out repeat count (last param if int or group ref)
                for p in hk_params:
                    if isinstance(p, str) and p not in group_values and not p.isdigit():
                        key_parts.append(p)
                    elif isinstance(p, int):
                        break  # repeat count
                    elif isinstance(p, str) and (p in group_values or p.isdigit()):
                        break  # repeat count from capture group
            if not hk_actions:
                # press_keys-only pattern: smoke check, no keystroke assertion
                output.append(f'        # Action type: {expected_type} (press_keys) - verify no crash')
                output.append('        pass')
            elif key_parts:
                key_tuple_code = ', '.join(f'"{k}"' for k in key_parts)
                key_tuple_desc = '+'.join(key_parts)
                output.append('        keys = harness.recording.get_keystroke_keys()')
                output.append(f'        assert ({key_tuple_code},) in keys, \\')
                output.append(f'            f"Expected {key_tuple_desc} in keystrokes, got {{keys}}"')
            else:
                output.append('        keys = harness.recording.get_keystroke_keys()')
                output.append(f'        assert len(keys) > 0, "Expected keystrokes from command: {test_input}"')
        elif expected_type == 'side_effect':
            # Check if pattern uses 'run' -- verify run_programs recording
            has_run = any(a.get('function') == 'run' for a in actions)
            if has_run:
                output.append('        assert len(harness.recording.run_programs) > 0, \\')
                output.append(f'            "Expected run_programs recording from: {test_input}"')
            else:
                output.append('        # Side-effect only pattern - just verify no crash')
                output.append('        pass')
        else:
            output.append(f'        # Action type: {expected_type} - verify no crash')
            output.append('        pass')

        output.append(f'')

    # Generate replacement pattern tests
    output.append('')
    output.append('class TestE2EReplacementPatterns:')
    output.append('    """E2E tests for replacement patterns (no ^ anchor).')
    output.append('')
    output.append('    Replacements trigger mid-dictation. We send them as part of a')
    output.append('    dictation utterance and verify no crash through the full pipeline.')
    output.append('    """')
    output.append('')

    for idx, pattern_data in replacement_patterns:
        pattern_str = pattern_data.get('pattern', '')
        actions = pattern_data.get('actions', [])

        if not actions:
            continue

        test_input, group_values = parse_pattern_groups(pattern_str)
        words = test_input.split()

        if not words:
            continue

        test_name = test_input.replace(' ', '_').replace('"', '').replace("'", '')
        test_name = re.sub(r'[^a-zA-Z0-9_]', '', test_name)
        test_name = f"test_e2e_repl_{idx:03d}_{test_name[:30]}"

        escaped_pattern = pattern_str.replace('\\', '\\\\')

        output.append(f'    @pytest.mark.asyncio')
        output.append(f'    async def {test_name}(self, harness):')
        output.append(f'        """Pattern: {escaped_pattern}"""')

        # Send words as dictation utterance
        word_list = ', '.join(f'"{w}"' for w in words)
        output.append(f'        await harness.send_utterance([{word_list}])')
        output.append(f'        await asyncio.sleep(0.1)')
        output.append(f'        # Replacement pattern - verify no crash through full pipeline')

        output.append(f'')

    # Write scaffold file (never the hand-maintained suite)
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write('\n'.join(output))

    print(f"Generated {len(command_patterns) + len(replacement_patterns)} E2E scaffold tests")
    print(f"  - Command patterns: {len(command_patterns)}")
    print(f"  - Replacement patterns: {len(replacement_patterns)}")
    print(f"  - Output: {output_file}")
    print(f"  - Copy new tests by hand into: {E2E_MAINTAINED_FILE}")


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--e2e', action='store_true', help='Generate E2E tests')
    parser.add_argument('--smoke', action='store_true', help='Generate smoke tests')
    args = parser.parse_args()

    if args.e2e:
        generate_e2e_tests()
    elif args.smoke:
        generate_smoke_tests()
    else:
        # Default: generate both
        generate_smoke_tests()
        generate_e2e_tests()
