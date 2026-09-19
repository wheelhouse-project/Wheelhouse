# NVDA 2026.1.1 keyboard command list

Extracted from the local installation at `C:\Program Files\NVDA\documentation\en\keyCommands.html`.
200 keyboard commands. The braille display hardware key tables (about 650 more rows, one table per display model) are not included.

`NVDA` in a key combination means the NVDA modifier key, which is Insert or Caps Lock. The user chooses which.
Two key columns appear where NVDA defines both a desktop layout and a laptop layout. The desktop layout uses the numeric keypad. The laptop layout does not.


# NVDA 2026.1.1 Commands Quick Reference

## Using NVDA

### NVDA Touch Gestures

#### Touch Modes

### Basic NVDA commands
- **Starts or restarts NVDA** -- `Control+alt+n` / `Control+alt+n`
  Starts or restarts NVDA from the Desktop, if this Windows shortcut is enabled during NVDA's installation process. This is a Windows specific shortcut and therefore it cannot be reassigned in the input gestures dialog.
- **Stop speech** -- `Control` / `control` / `2-finger tap`
  Instantly stops speaking
- **Pause Speech** -- `shift` / `shift`
  Instantly pauses speech. Pressing it again will continue speaking where it left off (if pausing is supported by the current synthesizer)
- **NVDA Menu** -- `NVDA+n` / `NVDA+n` / `2-finger double-tap`
  Pops up the NVDA menu to allow you to access preferences, tools, help, etc.
- **Toggle Input Help Mode** -- `NVDA+1` / `NVDA+1`
  Pressing any key in this mode will report the key, and the description of any NVDA command associated with it
- **Quit NVDA** -- `NVDA+q` / `NVDA+q`
  Exits NVDA
- **Pass next key through** -- `NVDA+f2` / `NVDA+f2`
  Tells NVDA to pass the next key press straight through to the active application - even if it is normally treated as an NVDA key command
- **Toggle application sleep mode on and off** -- `NVDA+shift+s` / `NVDA+shift+z`
  sleep mode disables all NVDA commands and speech/braille output for the current application. This is most useful in applications that provide their own speech or screen reading features. Press this command again to disable sleep mode - note that NVDA will only retain the Sleep Mode setting until it is restarted.

### Reporting System Information
- **Report date/time** -- `NVDA+f12`
  Pressing once reports the current time, pressing twice reports the date
- **Report battery status** -- `NVDA+shift+b`
  Reports the battery status i.e. whether AC power is in use or the current charge percentage.
- **Report clipboard text** -- `NVDA+c`
  Reports the Text on the clipboard if there is any.

### Speech modes
- **Cycle Speech Mode** -- `NVDA+s`
  Cycles between speech modes.

## Navigating with NVDA

### Navigating with the System Focus
- **Report current focus** -- `NVDA+tab` / `NVDA+tab`
  announces the current object or control that has the System focus. Pressing twice will spell the information
- **Report title** -- `NVDA+t` / `NVDA+t`
  Reports the title of the currently active window. Pressing twice will spell the information. Pressing three times will copy it to the clipboard
- **Read active window** -- `NVDA+b` / `NVDA+b`
  reads all the controls in the currently active window (useful for dialogs)
- **Report Status Bar** -- `NVDA+end` / `NVDA+shift+end`
  Reports the Status Bar if NVDA finds one. Pressing twice will spell the information. Pressing three times will copy it to the clipboard
- **Report Shortcut Key** -- `shift+numpad2` / `NVDA+control+shift+.`
  Reports the shortcut (accelerator) key of the currently focused object

### Navigating with the System Caret
- **Say all** -- `NVDA+downArrow` / `NVDA+a`
  Starts reading from the current position of the system caret, moving it along as it goes
- **Read current line** -- `NVDA+upArrow` / `NVDA+l`
  Reads the line where the system caret is currently situated. Pressing twice spells the line. Pressing three times spells the line using character descriptions.
- **Read current text selection** -- `NVDA+Shift+upArrow` / `NVDA+shift+s`
  Reads any currently selected text
- **Report text formatting** -- `NVDA+f` / `NVDA+f`
  Reports the formatting of the text where the caret is currently situated. Pressing twice shows the information in browse mode
- **Report language** -- *(no default key assigned)*
  Reports text language. Pressing twice shows the information in a window
- **Report link destination** -- `NVDA+k` / `NVDA+k`
  Pressing once speaks the destination URL of the link at the current caret or focus position. Pressing twice shows it in a window for more careful review
- **Report caret location** -- `NVDA+numpadDelete` / `NVDA+delete`
  Reports information about the location of the text or object at the position of system caret. For example, this might include the percentage through the document, the distance from the edge of the page or the exact screen position. Pressing twice may provide further detail.
- **Next sentence** -- `alt+downArrow` / `alt+downArrow`
  Moves the caret to the next sentence and announces it. (only supported in Microsoft Word and Outlook)
- **Previous sentence** -- `alt+upArrow` / `alt+upArrow`
  Moves the caret to the previous sentence and announces it. (only supported in Microsoft Word and Outlook)
- **Move to previous column** -- `control+alt+leftArrow`
  Moves the system caret to the previous column (staying in the same row)
- **Move to next column** -- `control+alt+rightArrow`
  Moves the system caret to the next column (staying in the same row)
- **Move to previous row** -- `control+alt+upArrow`
  Moves the system caret to the previous row (staying in the same column)
- **Move to next row** -- `control+alt+downArrow`
  Moves the system caret to the next row (staying in the same column)
- **Move to first column** -- `control+alt+home`
  Moves the system caret to the first column (staying in the same row)
- **Move to last column** -- `control+alt+end`
  Moves the system caret to the last column (staying in the same row)
- **Move to first row** -- `control+alt+pageUp`
  Moves the system caret to the first row (staying in the same column)
- **Move to last row** -- `control+alt+pageDown`
  Moves the system caret to the last row (staying in the same column)
- **Say all in column** -- `NVDA+control+alt+downArrow`
  Reads the column vertically from the current cell downwards to the last cell in the column.
- **Say all in row** -- `NVDA+control+alt+rightArrow`
  Reads the row horizontally from the current cell rightwards to the last cell in the row.
- **Read entire column** -- `NVDA+control+alt+upArrow`
  Reads the current column vertically from top to bottom without moving the system caret.
- **Read entire row** -- `NVDA+control+alt+leftArrow`
  Reads the current row horizontally from left to right without moving the system caret.

### Object Navigation
- **Report current object** -- `NVDA+numpad5` / `NVDA+shift+o`
  Reports the current navigator object. Pressing twice spells the information, and pressing 3 times copies this object's name and value to the clipboard.
- **Move to containing object** -- `NVDA+numpad8` / `NVDA+shift+upArrow` / `flick up (object mode)`
  Moves to the object containing the current navigator object
- **Move to previous object** -- `NVDA+numpad4` / `NVDA+shift+leftArrow`
  Moves to the object before the current navigator object
- **Move to previous object in flattened view** -- `NVDA+numpad9` / `NVDA+shift+[` / `flick left (object mode)`
  Moves to the previous object in a flattened view of the object navigation hierarchy
- **Move to next object** -- `NVDA+numpad6` / `NVDA+shift+rightArrow`
  Moves to the object after the current navigator object
- **Move to next object in flattened view** -- `NVDA+numpad3` / `NVDA+shift+]` / `flick right (object mode)`
  Moves to the next object in a flattened view of the object navigation hierarchy
- **Move to first contained object** -- `NVDA+numpad2` / `NVDA+shift+downArrow` / `flick down (object mode)`
  Moves to the first object contained by the current navigator object
- **Move to focus object** -- `NVDA+numpadMinus` / `NVDA+backspace`
  Moves to the object that currently has the system focus, and also places the review cursor at the position of the System caret, if it is showing
- **Activate current navigator object** -- `NVDA+numpadEnter` / `NVDA+enter` / `double-tap`
  Activates the current navigator object (similar to clicking with the mouse or pressing space when it has the system focus)
- **Move System focus or caret to current review position** -- `NVDA+shift+numpadMinus` / `NVDA+shift+backspace`
  pressed once Moves the System focus to the current navigator object, pressed twice moves the system caret to the position of the review cursor
- **Report review cursor location** -- `NVDA+shift+numpadDelete` / `NVDA+shift+delete`
  Reports information about the location of the text or object at the review cursor. For example, this might include the percentage through the document, the distance from the edge of the page or the exact screen position. Pressing twice may provide further detail.
- **Move review cursor to status bar** -- *(no default key assigned)*
  Reports the Status Bar if NVDA finds one. It also moves the navigator object to this location.

### Reviewing Text
- **Move to top line in review** -- `shift+numpad7` / `NVDA+control+home`
  Moves the review cursor to the top line of the text
- **Move to previous line in review** -- `numpad7` / `NVDA+upArrow` / `flick up (text mode)`
  Moves the review cursor to the previous line of text
- **Report current line in review** -- `numpad8` / `NVDA+shift+.`
  Announces the current line of text where the review cursor is positioned. Pressing twice spells the line. Pressing three times spells the line using character descriptions.
- **Move to next line in review** -- `numpad9` / `NVDA+downArrow` / `flick down (text mode)`
  Move the review cursor to the next line of text
- **Move to bottom line in review** -- `shift+numpad9` / `NVDA+control+end`
  Moves the review cursor to the bottom line of text
- **Move to previous word in review** -- `numpad4` / `NVDA+control+leftArrow` / `2-finger flick left (text mode)`
  Moves the review cursor to the previous word in the text
- **Report current word in review** -- `numpad5` / `NVDA+control+.`
  Announces the current word in the text where the review cursor is positioned. Pressing twice spells the word. Pressing three times spells the word using character descriptions.
- **Move to next word in review** -- `numpad6` / `NVDA+control+rightArrow` / `2-finger flick right (text mode)`
  Move the review cursor to the next word in the text
- **Move to start of line in review** -- `shift+numpad1` / `NVDA+home`
  Moves the review cursor to the start of the current line in the text
- **Move to previous character in review** -- `numpad1` / `NVDA+leftArrow` / `flick left (text mode)`
  Moves the review cursor to the previous character on the current line in the text
- **Report current character in review** -- `numpad2` / `NVDA+.`
  Announces the current character on the line of text where the review cursor is positioned. Pressing twice reports a description or example of that character. Pressing three times reports the numeric value of the character in decimal and hexadecimal.
- **Move to next character in review** -- `numpad3` / `NVDA+rightArrow` / `flick right (text mode)`
  Move the review cursor to the next character on the current line of text
- **Move to end of line in review** -- `shift+numpad3` / `NVDA+end`
  Moves the review cursor to the end of the current line of text
- **Move to previous page in review** -- `NVDA+pageUp` / `NVDA+shift+pageUp`
  Moves the review cursor to the previous page of text if supported by the application
- **Move to next page in review** -- `NVDA+pageDown` / `NVDA+shift+pageDown`
  Moves the review cursor to the next page of text if supported by the application
- **Move to start of selection in review** -- `NVDA+alt+home` / `NVDA+alt+home`
  Moves the review cursor to the first character of the selected text
- **Move to end of selection in review** -- `NVDA+alt+end` / `NVDA+alt+end`
  Moves the review cursor to the last character of the selected text
- **Say all with review** -- `numpadPlus` / `NVDA+shift+a` / `3-finger flick down (text mode)`
  Reads from the current position of the review cursor, moving it as it goes
- **Select then Copy from review cursor** -- `NVDA+f9` / `NVDA+f9`
  Starts the select then copy process from the current position of the review cursor. The actual action is not performed until you tell NVDA where the end of the text range is
- **Select then Copy to review cursor** -- `NVDA+f10` / `NVDA+f10`
  On the first press, text is selected from the position previously set as start marker up to and including the review cursor's current position. If the system caret can reach the text, it will be moved to the selected text. After pressing this key stroke a second time, the text will be copied to the Windows clipboard
- **Move to marked start for copy in review** -- `NVDA+shift+f9` / `NVDA+shift+f9`
  Moves the review cursor to the position previously set start marker for copy
- **Report text formatting** -- `NVDA+shift+f` / `NVDA+shift+f`
  Reports the formatting of the text where the review cursor is currently situated. Pressing twice shows the information in browse mode
- **Report current symbol replacement** -- *(no default key assigned)*
  Speaks the symbol where the review cursor is positioned. Pressed twice, shows the symbol and the text used to speak it in browse mode.

### Review Modes
- **Switch to next review mode** -- `NVDA+numpad7` / `NVDA+pageUp` / `2-finger flick up`
  switches to the next available review mode
- **Switch to previous review mode** -- `NVDA+numpad1` / `NVDA+pageDown` / `2-finger flick down`
  switches to the previous available review mode

### Navigating with the Mouse
- **Left mouse button click** -- `numpadDivide` / `NVDA+[`
  Clicks the left mouse button once. The common double click can be performed by pressing this key twice in quick succession
- **Left mouse button lock** -- `shift+numpadDivide` / `NVDA+control+[`
  Locks the left mouse button down. Press again to release it. To drag the mouse, press this key to lock the left button down and then move the mouse either physically or use one of the other mouse routing commands
- **Right mouse click** -- `numpadMultiply` / `NVDA+]` / `tap and hold`
  Clicks the right mouse button once, mostly used to open context menu at the location of the mouse.
- **Right mouse button lock** -- `shift+numpadMultiply` / `NVDA+control+]`
  Locks the right mouse button down. Press again to release it. To drag the mouse, press this key to lock the right button down and then move the mouse either physically or use one of the other mouse routing commands
- **Scroll up at the mouse position** -- *(no default key assigned)*
  Scrolls the mouse wheel up at the current mouse position
- **Scroll down at the mouse position** -- *(no default key assigned)*
  Scrolls the mouse wheel down at the current mouse position
- **Scroll left at the mouse position** -- *(no default key assigned)*
  Scrolls the mouse wheel left at the current mouse position
- **Scroll right at the mouse position** -- *(no default key assigned)*
  Scrolls the mouse wheel right at the current mouse position
- **Move mouse to current navigator object** -- `NVDA+numpadDivide` / `NVDA+shift+m`
  Moves the mouse to the location of the current navigator object and review cursor
- **Navigate to the object under the mouse** -- `NVDA+numpadMultiply` / `NVDA+shift+n`
  Set the navigator object to the object located at the position of the mouse
- **Toggle mouse audio coordinates** -- *(no default key assigned)*
  Toggles whether NVDA plays audio beeps that report the mouse position as it moves.

## Browse Mode
- **Toggle browse/focus modes** -- `NVDA+space`
  Toggles between focus mode and browse mode
- **Exit focus mode** -- `escape`
  Switches back to browse mode if focus mode was previously switched to automatically
- **Refresh browse mode document** -- `NVDA+f5`
  Reloads the current document content (useful if certain content seems to be missing from the document. Not available in Microsoft Word and Outlook.)
- **Find** -- `NVDA+control+f`
  Pops up a dialog in which you can type some text to find in the current document. See searching for text for more information.
- **Find next** -- `NVDA+f3`
  Finds the next occurrence of the text in the document that you previously searched for
- **Find previous** -- `NVDA+shift+f3`
  Finds the previous occurrence of the text in the document you previously searched for

### Single Letter Navigation

In browse mode, each letter below jumps to the next element of that type. Add shift to jump to the previous one.

```
h: heading | l: list | i: list item | t: table | k: link | n: nonLinked text
f: form field | u: unvisited link | v: visited link | e: edit field | b: button
x: checkbox | c: combo box | r: radio button | q: block quote | s: separator
m: frame | g: graphic | d: landmark | o: embedded object | a: annotation
p: text paragraph | w: spelling error | 1 to 9: headings at levels 1 to 9
```

Press `NVDA+shift+space` to turn single letter navigation off for the current document.

- **Move to start of container** -- `shift+comma`
  Moves to the start of the container (list, table, etc.) where the caret is positioned
- **Move past end of container** -- `comma`
  Moves past the end of the container (list, table, etc.) where the caret is positioned

### The Elements List
- **Browse mode elements list** -- `NVDA+f7`
  Lists various types of elements in the current document

### Searching for text
- **Find text** -- `NVDA+control+f`
  Opens the search dialog
- **Find next** -- `NVDA+f3`
  searches the next occurrence of the current search term
- **Find previous** -- `NVDA+shift+f3`
  searches the previous occurrence of the current search term

### Embedded Objects
- **Move to containing browse mode document** -- `NVDA+control+space`
  Moves the focus out of the current embedded object and into the document that contains it

### Native Selection Mode
- **Toggle Native Selection Mode on and off** -- `NVDA+shift+f10`
  Toggles native selection mode on and off

## Reading Mathematical Content

### Interactive Navigation
- **Interact with math content** -- `NVDA+alt+m`
  Begins interaction with math content.

#### Navigation Modes
- **Move to previous** -- `leftArrow`
- **Move to previous cell in a table, or previous digit if in columnar math** -- `control+leftArrow or control+alt+leftArrow`
- **Read previous** -- `shift+leftArrow`
- **Describe previous** -- `control+shift+leftArrow`
- **Move to next** -- `rightArrow`
- **Move to next cell in a table, or next digit if in columnar math** -- `control+rightArrow or control+alt+rightArrow`
- **Read next** -- `shift+rightArrow`
- **Describe next** -- `control+shift+rightArrow`
- **Zoom out** -- `upArrow`
- **Move to cell above in a table, or digit above in columnar math** -- `control+upArrow or control+alt+upArrow`
- **Change Navigation Mode (Enhanced/Simple/Character) to larger** -- `shift+upArrow`
- **Zoom out all the way** -- `control+shift+upArrow`
- **Zoom in** -- `downArrow`
- **Move to cell below in a table, or digit below in columnar math** -- `control+downArrow or control+alt+downArrow`
- **Change Navigation Mode (Enhanced/Simple/Character) to smaller** -- `shift+downArrow`
- **Zoom in all the way** -- `control+shift+downArrow`
- **Where am I** -- `enter`
- **Global Where am I** -- `control+enter`
- **Jump to placemarker** -- `1 through 0 ( 0 is 10)`
- **Set placemarker** -- `control+1 through control+0`
- **Read placemarker** -- `shift+1 through shift+0`
- **Describe placemarker** -- `control+shift+1 through control+shift+0`
- **Read current** -- `space`
- **Read current cell** -- `control+space`
- **Toggle "speech mode" to Read or Describe** -- `shift+space`
- **Describe current** -- `control+shift+space`
- **Move to start of expression** -- `home`
- **Move to start of line** -- `control+home`
- **Move to start of column in table, or move to digit at top in columnar math** -- `shift+home`
- **Move to end of expression** -- `end`
- **Move to end of line** -- `control+end`
- **Move to end of column in table, or move to digit at bottom in columnar math** -- `shift+end`
- **Move back to last position** -- `backspace`

## Braille

### Braille Input

## Vision

### Screen Curtain
- **Toggles the state of the screen curtain** -- `NVDA+control+escape`
  Enable to make the screen black or disable to show the contents of the screen. Pressed once, screen curtain is enabled until you restart NVDA. Pressed twice, screen curtain is enabled until you disable it.

## Content Recognition

### Windows OCR

## Application Specific Features

### Microsoft Word

#### Automatic Column and Row Header Reading
- **Set column headers** -- `NVDA+shift+c`
  Pressing this once tells NVDA this is the first header cell in the row that contains column headers, which should be automatically announced when moving between columns below this row. Pressing twice will clear the setting.
- **Set row headers** -- `NVDA+shift+r`
  Pressing this once tells NVDA this is the first header cell in the column that contains row headers, which should be automatically announced when moving between rows after this column. Pressing twice will clear the setting.

#### Browse Mode in Microsoft Word

##### The Elements List

#### Reporting Comments

### Microsoft Excel

#### Automatic Column and Row Header Reading
- **Set column headers** -- `NVDA+shift+c`
  Pressing this once tells NVDA this is the first header cell in the row that contains column headers, which should be automatically announced when moving between columns below this row. Pressing twice will clear the setting.
- **Set row headers** -- `NVDA+shift+r`
  Pressing this once tells NVDA this is the first header cell in the column that contains row headers, which should be automatically announced when moving between rows after this column. Pressing twice will clear the setting.

#### The Elements List

#### Reporting Notes

#### Reading Protected Cells

### Microsoft PowerPoint
- **Toggle speaker notes reading** -- `control+shift+s`
  When in a running slide show, this command will toggle between the speaker notes for the slide and the content for the slide. This only affects what NVDA reads, not what is displayed on screen.

### foobar2000
- **Report remaining time** -- `control+shift+r`
  Reports the remaining time of the currently playing track, if any.
- **Report elapsed time** -- `control+shift+e`
  Reports the elapsed time of the currently playing track, if any.
- **Report track length** -- `control+shift+t`
  Reports the length of the currently playing track, if any.

### Miranda IM
- **Report recent message** -- `NVDA+control+1-4`
  Reports one of the recent messages, depending on the number pressed; e.g. NVDA+control+2 reads the second most recent message.

### Poedit
- **Report notes for translators** -- `control+shift+a`
  Reports any notes for translators. If pressed twice, presents the notes in browse mode
- **Report Comment** -- `control+shift+c`
  Reports any comment in the comments window. If pressed twice, presents the comment in browse mode
- **Report Old Source Text** -- `control+shift+o`
  Reports the old source text, if any. If pressed twice, presents the text in browse mode
- **Report Translation Warning** -- `control+shift+w`
  Reports a translation warning, if any. If pressed twice, presents the warning in browse mode

### Kindle for PC

#### Text Selection

### Azardi
- **Enter** -- `enter`
  Opens the selected book.
- **Context menu** -- `applications`
  Opens the context menu for the selected book.

### Windows Console
- **Scroll up** -- `control+upArrow`
  Scrolls the console window up, so earlier text can be read.
- **Scroll down** -- `control+downArrow`
  Scrolls the console window down, so later text can be read.
- **Scroll to start** -- `control+home`
  Scrolls the console window to the beginning of the buffer.
- **Scroll to end** -- `control+end`
  Scrolls the console window to the end of the buffer.

## Configuring NVDA

### NVDA Settings

#### General
- **Open General settings** -- `NVDA+control+g` / `NVDA+control+g`
  The General category of the NVDA Settings dialog sets NVDA's overall behaviour such as interface language and whether or not it should check for updates.

#### Speech Settings
- **Open Speech settings** -- `NVDA+control+v` / `NVDA+control+v`
  The Speech category in the NVDA Settings dialog contains options that lets you change the speech synthesizer as well as voice characteristics for the chosen synthesizer.
- **Punctuation/Symbol Level** -- `NVDA+p` / `NVDA+p`
  This allows you to choose the amount of punctuation and other symbols that should be spoken as words.

#### Select Synthesizer
- **Open Select Synthesizer dialog** -- `NVDA+control+s` / `NVDA+control+s`
  The Synthesizer dialog, which can be opened by activating the Change... button in the speech category of the NVDA settings dialog, allows you to select which Synthesizer NVDA should use to speak with.

#### Synth settings ring
- **Move to next synth setting** -- `NVDA+control+rightArrow` / `NVDA+shift+control+rightArrow`
  Moves to the next available speech setting after the current, wrapping around to the first setting again after the last
- **Move to previous synth setting** -- `NVDA+control+leftArrow` / `NVDA+shift+control+leftArrow`
  Moves to the next available speech setting before the current, wrapping around to the last setting after the first
- **Increment current synth setting** -- `NVDA+control+upArrow` / `NVDA+shift+control+upArrow`
  increases the current speech setting you are on. E.g. increases the rate, chooses the next voice, increases the volume
- **Increment the current synth setting in a larger step** -- `NVDA+control+pageUp` / `NVDA+shift+control+pageUp`
  Increases the value of the current speech setting you're on in larger steps. e.g. when you're on a voice setting, it will jump forward every 20 voices; when you're on slider settings (rate, pitch, etc) it will jump forward the value up to 20%
- **Decrement current synth setting** -- `NVDA+control+downArrow` / `NVDA+shift+control+downArrow`
  decreases the current speech setting you are on. E.g. decreases the rate, chooses the previous voice, decreases the volume
- **Decrement the current synth setting in a larger step** -- `NVDA+control+pageDown` / `NVDA+shift+control+pageDown`
  Decreases the value of the current speech setting you're on in larger steps. e.g. when you're on a voice setting, it will jump backward every 20 voices; when you're on a slider setting, it will jump backward the value up to 20%
- **Set the first value of the current synth setting** -- *(no default key assigned)*
  Select the first value of the current speech setting, e.g. set rate to 0 or select the first available voice
- **Set the last value of the current synth setting** -- *(no default key assigned)*
  Select the last value of the current speech setting, e.g. set rate to 100 or select the last available voice

#### Braille
- **Braille mode** -- `NVDA+alt+t` / `NVDA+alt+t`
  This option allows you to select between the available braille modes.
- **Tether Braille** -- `NVDA+control+t` / `NVDA+control+t`
  This option allows you to choose whether the braille display will follow the system focus / caret, the navigator object / review cursor, or both.

#### Select Braille Display
- **Open Select Braille Display dialog** -- `NVDA+control+a` / `NVDA+control+a`
  The Select Braille Display dialog, which can be opened by activating the Change... button in the Braille category of the NVDA settings dialog, allows you to select which Braille display NVDA should use for braille output.

#### Audio
- **Open Audio settings** -- `NVDA+control+u` / `NVDA+control+u`
  The Audio category in the NVDA Settings dialog contains options that let you change several aspects of audio output.
- **Audio Ducking Mode** -- `NVDA+shift+d` / `NVDA+shift+d`
  This option allows you to choose if NVDA should lower the volume of other applications while NVDA is speaking, or all the time while NVDA is running.

##### Sound split
- **Cycle Sound Split Mode** -- `NVDA+alt+s`
  Cycles between sound split modes.

#### Keyboard
- **Open Keyboard settings** -- `NVDA+control+k` / `NVDA+control+k`
  The Keyboard category in the NVDA Settings dialog contains options that set how NVDA behaves as you use and type on your keyboard.
- **Speak Typed Characters** -- `NVDA+2` / `NVDA+2`
  This option controls when NVDA announces characters you type on the keyboard.
- **Speak Typed Words** -- `NVDA+3` / `NVDA+3`
  This option controls when NVDA announces words you type on the keyboard.
- **Speak Command Keys** -- `NVDA+4` / `NVDA+4`
  When enabled, NVDA will announce all non-character keys you type on the keyboard. This includes key combinations such as control plus another letter.

#### Mouse
- **Open Mouse settings** -- `NVDA+control+m` / `NVDA+control+m`
  The Mouse category in the NVDA Settings dialog allows NVDA to track the mouse, play mouse coordinate beeps and sets other mouse usage options.
- **Enable mouse tracking** -- `NVDA+m` / `NVDA+m`
  When enabled, NVDA will announce the text currently under the mouse pointer, as you move it around the screen. This allows you to find things on the screen, by physically moving the mouse, rather than trying to find them through object navigation.

#### Review Cursor
- **Follow System Focus** -- `NVDA+7` / `NVDA+7`
  When enabled, The review cursor will always be placed in the same object as the current system focus whenever the focus changes.
- **Follow System Caret** -- `NVDA+6` / `NVDA+6`
  When enabled, the review cursor will automatically be moved to the position of the System caret each time it moves.

#### Object Presentation
- **Open Object Presentation settings** -- `NVDA+control+o` / `NVDA+control+o`
  The Object Presentation category in the NVDA Settings dialog is used to set how much information NVDA will present about controls such as description, position information and so on.
- **Progress bar output** -- `NVDA+u` / `NVDA+u`
  This option controls how NVDA reports progress bar updates to you.
- **Report dynamic content changes** -- `NVDA+5` / `NVDA+5`
  Toggles the announcement of new content in particular objects such as terminals and the history control in chat programs.

#### Browse Mode
- **Open Browse Mode settings** -- `NVDA+control+b` / `NVDA+control+b`
  The Browse Mode category in the NVDA Settings dialog is used to configure NVDA's behaviour when you read and navigate complex documents such as web pages.
- **Use screen layout** -- `NVDA+v` / `NVDA+v`
  This option allows you to specify whether browse mode should place clickable content (links, buttons and fields) on its own line, or if it should keep it in the flow of text as it is visually shown.

#### Document Formatting
- **Open Document Formatting settings** -- `NVDA+control+d` / `NVDA+control+d`
  Most of the options in this category are for configuring what type of formatting you wish to have reported as you move the cursor around documents.

#### Advanced Settings

##### Annotations

### Saving and Reloading the configuration
- **Save configuration** -- `NVDA+control+c` / `NVDA+control+c`
  Saves your current configuration so that it is not lost when you exit NVDA
- **Revert configuration** -- `NVDA+control+r` / `NVDA+control+r`
  Pressing once resets your configuration to when you last saved it. Pressing three times will reset it back to factory defaults.

### Configuration Profiles

#### Basic Management

## Remote Access

### Remote Access Key Commands Summary
- **Toggle Remote connection** -- `NVDA+alt+r`
  Starts a new Remote Access session or, if a session is already in progress, disconnects from it.
- **Toggle Control** -- `NVDA+alt+tab`
  Switches between controlling the remote and local computer.
- **Connect** -- *(no default key assigned)*
  Starts a new Remote Access session. Unavailable in secure mode .
- **Copy link** -- *(no default key assigned)*
  Copies a link to the remote session to the clipboard.
- **Disconnect** -- *(no default key assigned)*
  Ends an existing Remote Access session.
- **Mute remote** -- *(no default key assigned)*
  Mutes or unmutes the speech coming from the remote computer.
- **Send clipboard** -- *(no default key assigned)*
  Sends the contents of the clipboard to the remote computer.
- **Send control+alt+delete** -- *(no default key assigned)*
  Sends control+alt+delete to the controlled computer.

## Extra Tools

### Log Viewer
- **Open log viewer** -- `NVDA+f1`
  Opens the log viewer and displays developer information about the current navigator object.
- **Copy a fragment of the log to the clipboard** -- `NVDA+control+shift+f1`
  When this command is pressed once, it sets a starting point for the log content that should be captured. When pressed a second time, it copies the log content since the start point to your clipboard.

### Reload plugins
- **Reload plugins** -- `NVDA+control+f3`
  Reloads NVDA's global plugins and app modules.
- **Report loaded app module and executable** -- `NVDA+control+f1`
  Report the name of the app module, if any, and the name of the executable associated with the application which has the keyboard focus.