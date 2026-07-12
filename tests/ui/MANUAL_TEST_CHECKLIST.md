# UI Actions Manual Test Checklist

**Purpose**: Verify UIActionHandler refactoring maintains all functionality.
**When to use**: After refactoring, before deleting old backup file.
**Tester**: Run through systematically, check each item.

---

## Prerequisites

- [ ] WheelHouse is running (all processes started)
- [ ] Microphone is configured and working
- [ ] Have Notepad, Windows Terminal, and a browser available

---

## 1. Basic Text Insertion (Notepad)

**Setup**: Open fresh Notepad window

- [ ] **First word capitalized**: Dictate "hello" → Should appear as "Hello"
- [ ] **Normal spacing**: Continue "world" → Should see "Hello world"
- [ ] **Punctuation no space**: Say "comma" → Should be "Hello world,"
- [ ] **After period capitalized**: Say "period next sentence" → "Hello world. Next sentence"
- [ ] **Multiple words flow**: Dictate a full sentence → Spacing and caps correct

**Expected**: Text appears with correct spacing/capitalization, no glitches

---

## 2. Windows Terminal (Popup Editor)

**Setup**: Open Windows Terminal (PowerShell or Command Prompt)

- [ ] **Popup appears**: Dictate "test" → Popup editor should appear (NOT direct paste)
- [ ] **Text visible in popup**: "test" shows in popup window
- [ ] **Continue dictation**: Say "more text" → Appends to popup
- [ ] **Accept button**: Click Accept → Text pastes to terminal
- [ ] **Multiple rounds**: Dictate again → New popup appears

**Expected**: Terminal gets popup editor, text pastes correctly on accept

---

## 3. Selection Transformations

**Setup**: Open Notepad, type "hello world test"

### Case Conversions
- [ ] **Select text, say "snake case"** → "hello_world_test"
- [ ] Undo (Ctrl+Z), select again, say **"camel case"** → "helloWorldTest"
- [ ] Undo, select again, say **"pascal case"** → "HelloWorldTest"
- [ ] Undo, select again, say **"kebab case"** → "hello-world-test"
- [ ] Undo, select again, say **"uppercase"** → "HELLO WORLD TEST"

### Wrapping
- [ ] Type "test", select it, say **"quote"** → "test"
- [ ] Undo, select again, say **"bracket"** → [test]
- [ ] Undo, select again, say **"parenthesis"** → (test)

**Expected**: All transformations work, text changes correctly

---

## 4. Clipboard Preservation

**Setup**: Open Notepad

- [ ] Copy some text (Ctrl+C) to clipboard - remember what you copied
- [ ] Dictate a sentence using voice
- [ ] Paste (Ctrl+V) → Should paste your original copied text, NOT dictation

**Expected**: Clipboard preserved across dictation operations

---

## 5. Context Gathering (UIA Fallback)

**Setup**: Open an app that might not support UIA well (try Paint, older apps)

- [ ] **First word**: Dictate "hello" → Should still insert correctly
- [ ] **Continue**: Say "world" → Should have space before it
- [ ] **Punctuation**: Say "comma" → Should have no extra space

**Expected**: Clipboard fallback works, text formatted correctly even in non-UIA apps

---

## 6. Utterance Boundaries

**Setup**: Open Notepad

- [ ] **Single utterance**: Dictate "hello world" continuously → Appears correctly
- [ ] **Pause between words**: Say "hello" ... [2 sec pause] ... "world" → Both appear
- [ ] **Fast speech**: Rapid dictation → All words captured

**Expected**: Utterance start/end handled correctly, clipboard restored per utterance

---

## 7. Edge Cases

**Setup**: Various scenarios

### Empty Document
- [ ] Open new Notepad
- [ ] Dictate immediately → First word capitalized

### After Punctuation
- [ ] Type "Test." in Notepad
- [ ] Dictate "new" → Should see "Test. New" (capitalized after period)

### Punctuation Only
- [ ] Empty Notepad
- [ ] Dictate "comma" → Just "," appears (not "Comma")

### Existing Selection
- [ ] Type "old text" and select it all
- [ ] Dictate "new text" → Should replace selection, no extra space

**Expected**: All edge cases handled gracefully

---

## 8. Hotkeys and Key Presses

**Setup**: Notepad with some text

- [ ] Say "press enter" → New line created
- [ ] Say "backspace three" → Deletes 3 characters
- [ ] Say "select all" → Text selected
- [ ] Say "copy" (Ctrl+C) → Text copied

**Expected**: Hotkey commands work as before

---

## 9. Multiple Applications

**Setup**: Have 3-4 apps open (Notepad, Browser, Terminal, etc.)

- [ ] Dictate in Notepad → Works
- [ ] Switch to browser address bar, dictate → Works
- [ ] Switch to Terminal → Popup appears
- [ ] Switch back to Notepad, dictate → Works again

**Expected**: Switching between apps doesn't break dictation

---

## 10. Performance and Stability

**Run for 5-10 minutes of active dictation**

- [ ] No crashes or hangs
- [ ] No memory leaks (Task Manager: memory stays reasonable)
- [ ] Response time feels the same as before
- [ ] No error popups or console spam

**Expected**: Stable, performant, no degradation

---

## 11. Automated Unit Tests

**Setup**: Command line in project root

```bash
cd /home/user/WheelHouse
pytest tests/ui/test_text_perfector.py -v
pytest tests/ui/test_selection_transformer.py -v
```

- [ ] All TextPerfector tests pass
- [ ] All SelectionTransformer tests pass
- [ ] No unexpected failures

**Expected**: 100% test pass rate

---

## Summary Checklist

After completing all sections above:

- [ ] All basic text insertion tests passed
- [ ] Terminal popup editor works
- [ ] All selection transformations work
- [ ] Clipboard preservation works
- [ ] Context gathering fallback works
- [ ] Utterance boundaries handled correctly
- [ ] Edge cases handled gracefully
- [ ] Hotkeys and key presses work
- [ ] Multiple applications work
- [ ] Performance and stability acceptable
- [ ] Unit tests all pass

**If ALL boxes checked**: Refactoring is successful! ✅
**If ANY box unchecked**: Investigate failure, compare with old backup file.

---

## Rollback Procedure (If Needed)

If tests fail and you need to revert:

```bash
cd /home/user/WheelHouse/services/wheelhouse/ui

# Delete new files
rm text_perfector.py selection_transformer.py utterance_clipboard_manager.py
rm window_focus_manager.py clipboard_operations.py insertion_strategies.py
rm ui_action_handler.py ui_actions.py

# Restore old file
mv ui_actions_OLD_BACKUP.py ui_actions.py

# Restart WheelHouse
```

---

## Notes Section

Use this space to record any issues found:

```
Issue 1: [Description]
Fix: [How you resolved it]

Issue 2: [Description]
Fix: [How you resolved it]
```

---

**Tester Signature**: _______________  **Date**: _______________

**Status**: [ ] PASS - All tests passed, refactoring successful
**Status**: [ ] FAIL - Issues found, see notes above
