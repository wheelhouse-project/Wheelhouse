; WheelHouse-Setup.iss -- graphical installer wizard for WheelHouse.
;
; This is a thin front end over scripts/release/public/install-wheelhouse.ps1
; (the engine). The wizard collects the user's choices with radio buttons and
; checkboxes, then runs the engine as a child process, passing the choices as
; parameters. The engine does the real work (download, verify, set up, write
; config, shortcuts). The wizard bundles only the engine script; the app archive
; and the speech model still download at install time, so the .exe stays small.
;
; Per-user, no administrator prompt: PrivilegesRequired=lowest and the engine
; installs entirely under the user profile (%LOCALAPPDATA%\WheelHouse).
;
; Design: docs/superpowers/specs/2026-07-13-graphical-installer-wizard-design.md
; Work:   wh-installer-inno-wizard (Phase 2) and its child tasks.
;
; The cloud AI key is handed to the engine ONLY through the
; WHEELHOUSE_AI_API_KEY_INPUT environment variable, never on the command line:
; a command line is readable by any local process via Win32_Process.CommandLine
; while the install runs (wh-ai-key-from-env).

; The build passes the release version with ISCC /DAppVer=<version> (the build
; workflow derives it from the release tag). The default keeps a plain compile
; (the tests, a local build) working with no define.
#ifndef AppVer
  #define AppVer "1.0.1"
#endif

[Setup]
AppId={{A7D3F1E2-4B6C-4E8A-9F1D-2C3B4A5D6E7F}
AppName=WheelHouse
AppVersion={#AppVer}
AppPublisher=WheelHouse Project
AppPublisherURL=https://wheelhouse-project.github.io/WheelHouse/
; The engine owns the real install location. This directory holds only the
; engine script and the uninstaller, and is deliberately SEPARATE from the
; engine's own %LOCALAPPDATA%\WheelHouse tree so a full uninstall (which removes
; that tree) does not delete this uninstaller out from under itself.
DefaultDirName={localappdata}\WheelHouseSetup
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableWelcomePage=no
PrivilegesRequired=lowest
OutputDir=build
OutputBaseFilename=WheelHouse-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=WheelHouse
VersionInfoVersion={#AppVer}
VersionInfoProductName=WheelHouse
; Always write a setup log (%TEMP%\Setup Log YYYY-MM-DD #NNN.txt). The engine's
; console output -- including any shortcut-creation error -- is captured via
; EngineLog into this log; without it a field failure leaves no evidence
; (wh-startmenu-shortcut-check: first physical install lost the Start-menu
; shortcut and nothing recorded why).
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
WelcomeLabel2=This will install WheelHouse, voice control for your PC.%n%nThis takes about 10 to 20 minutes and needs an internet connection. Click Next to begin.

[Files]
; Bundled into the .exe and copied to {app} so the uninstaller can call it.
; Also extracted to a temporary folder at install time to run the install.
; The engine sits beside this script: in the public pool they are siblings, and
; the build workflow copies the engine next to this .iss before compiling. Paths
; resolve against the .iss directory (SourceDir), so a bare filename is correct.
Source: "install-wheelhouse.ps1"; DestDir: "{app}"; Flags: ignoreversion

[Code]
const
  ENGINE = 'install-wheelhouse.ps1';

var
  SpeechPage: TInputOptionWizardPage;
  AiPage: TWizardPage;
  AiSkipRadio, AiCloudRadio: TNewRadioButton;
  AiKeyLabel, AiKeyLink: TNewStaticText;
  AiKeyEdit: TNewEdit;
  OptionsPage: TInputOptionWizardPage;
  MicPage: TWizardPage;
  MicStatusLabel, MicHelpLabel: TNewStaticText;
  MicOpenButton, MicRecheckButton: TNewButton;

function SetEnvironmentVariable(lpName: string; lpValue: string): Boolean;
  external 'SetEnvironmentVariableW@kernel32.dll stdcall';

// ---------- existing-install detection (re-run / repair) ----------

function InstalledConfigPath: string;
begin
  // The engine installs under %LOCALAPPDATA%\WheelHouse\app; its config.toml
  // records the user's prior choices. Mirrors $AppDir in install-wheelhouse.ps1.
  Result := ExpandConstant('{localappdata}\WheelHouse\app\services\wheelhouse\config.toml');
end;

function ExistingInstall: Boolean;
begin
  Result := FileExists(InstalledConfigPath);
end;

function CurrentProvider: string;
var
  lines: TArrayOfString;
  i, eq, p: Integer;
  s, key, val, quote: string;
begin
  // Read last_provider from the installed config so a re-run defaults the speech
  // choice to the engine already installed instead of silently forcing Parakeet.
  // Mirrors Get-CurrentProvider in the engine, including both TOML quote styles.
  Result := '';
  if not LoadStringsFromFile(InstalledConfigPath, lines) then
    exit;
  for i := 0 to GetArrayLength(lines) - 1 do begin
    s := Trim(lines[i]);
    eq := Pos('=', s);
    if eq > 0 then begin
      // Compare the key exactly, not by prefix: a bare "starts with last_provider"
      // test would also match a future key like last_provider_url.
      key := Trim(Copy(s, 1, eq - 1));
      if key = 'last_provider' then begin
        val := Trim(Copy(s, eq + 1, Length(s)));
        // Extract the text between the opening and matching closing quote, so
        // any trailing content (e.g. a hand-added comment) is ignored.
        if (Length(val) >= 2) and ((val[1] = '"') or (val[1] = #39)) then begin
          quote := Copy(val, 1, 1);
          val := Copy(val, 2, Length(val) - 1);
          p := Pos(quote, val);
          if p > 0 then
            val := Copy(val, 1, p - 1);
        end;
        Result := val;
        exit;
      end;
    end;
  end;
end;

// ---------- microphone permission ----------

function MicrophoneAllowed: Boolean;
var
  v, base: string;
begin
  // Default to "allowed" when the setting cannot be read, so we never block a
  // machine whose Windows build stores this differently.
  Result := True;
  base := 'Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone';
  if RegQueryStringValue(HKEY_CURRENT_USER, base + '\NonPackaged', 'Value', v) then
    Result := (CompareText(v, 'Allow') = 0)
  else if RegQueryStringValue(HKEY_CURRENT_USER, base, 'Value', v) then
    Result := (CompareText(v, 'Allow') = 0);
end;

procedure UpdateMicStatus;
begin
  if MicrophoneAllowed then
    MicStatusLabel.Caption := 'Microphone access for desktop apps is ON. You are all set.'
  else
    MicStatusLabel.Caption := 'Microphone access for desktop apps is OFF right now. WheelHouse will hear nothing until you turn it on.';
end;

procedure RecheckMicClick(Sender: TObject);
begin
  UpdateMicStatus;
end;

procedure OpenMicSettingsClick(Sender: TObject);
var
  ResultCode: Integer;
begin
  ShellExec('open', 'ms-settings:privacy-microphone', '', '', SW_SHOW, ewNoWait, ResultCode);
end;

// ---------- AI page key field enable/disable ----------

procedure UpdateAiKeyState;
var
  enabled: Boolean;
begin
  enabled := AiCloudRadio.Checked;
  AiKeyLabel.Enabled := enabled;
  AiKeyEdit.Enabled := enabled;
  AiKeyLink.Enabled := enabled;
end;

procedure AiRadioClick(Sender: TObject);
begin
  UpdateAiKeyState;
end;

// ---------- choice -> engine argument helpers ----------

function SpeechProviderArg: string;
begin
  case SpeechPage.SelectedValueIndex of
    1: Result := 'google_stt';
    2: Result := 'distil_medium_en';
  else
    Result := 'parakeet_tdt';
  end;
end;

function YesNo(b: Boolean): string;
begin
  if b then Result := 'yes' else Result := 'no';
end;

// ---------- live progress from the engine's tagged output ----------

procedure EngineLog(const S: String; const Error, FirstLine: Boolean);
var
  line, rest, msg: string;
  sp, pct: Integer;
begin
  Log('engine: ' + S);
  line := Trim(S);
  if Copy(line, 1, 9) = 'PROGRESS ' then begin
    rest := Trim(Copy(line, 10, Length(line)));
    sp := Pos(' ', rest);
    if sp > 0 then begin
      pct := StrToIntDef(Copy(rest, 1, sp - 1), -1);
      msg := Trim(Copy(rest, sp + 1, Length(rest)));
    end else begin
      pct := StrToIntDef(rest, -1);
      msg := '';
    end;
    if pct >= 0 then begin
      if pct > 100 then pct := 100;
      WizardForm.ProgressGauge.Style := npbstNormal;
      WizardForm.ProgressGauge.Position := pct;
    end;
    if msg <> '' then
      WizardForm.StatusLabel.Caption := msg;
  end else if Copy(line, 1, 10) = 'HEARTBEAT ' then begin
    msg := Trim(Copy(line, 11, Length(line)));
    WizardForm.ProgressGauge.Style := npbstMarquee;
    if msg <> '' then
      WizardForm.StatusLabel.Caption := msg + '  (still working, please wait...)';
  end;
  WizardForm.Refresh;
end;

// ---------- run the engine ----------

procedure RunEngine;
var
  Params: string;
  ResultCode: Integer;
  ok, cloud: Boolean;
begin
  ExtractTemporaryFile(ENGINE);
  cloud := AiCloudRadio.Checked;

  Params :=
    '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{tmp}\' + ENGINE) + '"' +
    ' -SttProvider ' + SpeechProviderArg +
    ' -AutoStart ' + YesNo(OptionsPage.Values[0]) +
    ' -StartNow ' + YesNo(OptionsPage.Values[1]);

  if cloud then
    Params := Params + ' -AiMode cloud'
  else if ExistingInstall then
    // Re-run with the default "leave AI unchanged": preserve the existing [ai]
    // config and any persisted key. -AiMode off would make the engine delete the
    // stored cloud credential on what the user thinks is a harmless repair.
    Params := Params + ' -AiMode keep'
  else
    Params := Params + ' -AiMode off';

  // The cloud key reaches the child only through the environment. Setting it on
  // this installer process means the child inherits it at launch; we clear it
  // immediately afterward so it does not linger in the installer environment.
  if cloud then
    SetEnvironmentVariable('WHEELHOUSE_AI_API_KEY_INPUT', Trim(AiKeyEdit.Text));

  WizardForm.ProgressGauge.Style := npbstMarquee;
  WizardForm.StatusLabel.Caption := 'Setting up WheelHouse. This can take 10 to 20 minutes...';
  WizardForm.Refresh;

  try
    ok := ExecAndLogOutput(
      ExpandConstant('{sysnative}\WindowsPowerShell\v1.0\powershell.exe'),
      Params, '', SW_HIDE, ewWaitUntilTerminated, ResultCode, @EngineLog);
  finally
    if cloud then
      SetEnvironmentVariable('WHEELHOUSE_AI_API_KEY_INPUT', '');
  end;

  if (not ok) or (ResultCode <> 0) then
    RaiseException(
      'WheelHouse setup could not finish. Details are in the setup log. Please try '
      + 'again; if it keeps failing, visit the WheelHouse issues page for help.');
end;

// ---------- wizard events ----------

procedure InitializeWizard;
begin
  { Speech engine (radio buttons) }
  SpeechPage := CreateInputOptionPage(wpWelcome,
    'Speech engine',
    'How should WheelHouse turn what you say into text?',
    'Choose one. Parakeet works offline and is recommended for almost everyone.',
    True, False);
  SpeechPage.Add('Parakeet - works offline, recommended for almost everyone');
  SpeechPage.Add('Google Cloud - more accurate, advanced, needs a Google account');
  SpeechPage.Add('Distil-Whisper - needs an NVIDIA graphics card (advanced)');
  { On a re-run, default to the engine already installed so clicking through the
    defaults does not silently switch it. The wizard must still pass an explicit
    -SttProvider (the engine prompts interactively when it is omitted, which would
    hang the hidden install shell), so it defaults the radio to last_provider. }
  if CurrentProvider = 'google_stt' then
    SpeechPage.SelectedValueIndex := 1
  else if CurrentProvider = 'distil_medium_en' then
    SpeechPage.SelectedValueIndex := 2
  else
    SpeechPage.SelectedValueIndex := 0;

  { AI helper (custom page: two radios + a key field revealed for the cloud choice) }
  AiPage := CreateCustomPage(SpeechPage.ID,
    'AI helper (optional)',
    'The AI cleans up dictated text and answers questions in the help window. It is optional: speech, voice commands, dictation, and clicking all work without it.');

  AiSkipRadio := TNewRadioButton.Create(WizardForm);
  AiSkipRadio.Parent := AiPage.Surface;
  AiSkipRadio.Left := 0;
  AiSkipRadio.Top := ScaleY(8);
  AiSkipRadio.Width := AiPage.SurfaceWidth;
  if ExistingInstall then
    AiSkipRadio.Caption := 'Leave my AI helper setting unchanged.'
  else
    AiSkipRadio.Caption := 'Skip for now (recommended). You can set this up later.';
  AiSkipRadio.Checked := True;
  AiSkipRadio.OnClick := @AiRadioClick;

  AiCloudRadio := TNewRadioButton.Create(WizardForm);
  AiCloudRadio.Parent := AiPage.Surface;
  AiCloudRadio.Left := 0;
  AiCloudRadio.Top := AiSkipRadio.Top + ScaleY(26);
  AiCloudRadio.Width := AiPage.SurfaceWidth;
  AiCloudRadio.Caption := 'Use a cloud model (Google Gemini). This sends dictated text to Google.';
  AiCloudRadio.OnClick := @AiRadioClick;

  AiKeyLabel := TNewStaticText.Create(WizardForm);
  AiKeyLabel.Parent := AiPage.Surface;
  AiKeyLabel.Left := ScaleX(18);
  AiKeyLabel.Top := AiCloudRadio.Top + ScaleY(30);
  AiKeyLabel.Caption := 'Paste your Google API key:';

  AiKeyEdit := TNewEdit.Create(WizardForm);
  AiKeyEdit.Parent := AiPage.Surface;
  AiKeyEdit.Left := ScaleX(18);
  AiKeyEdit.Top := AiKeyLabel.Top + ScaleY(16);
  AiKeyEdit.Width := AiPage.SurfaceWidth - ScaleX(36);

  AiKeyLink := TNewStaticText.Create(WizardForm);
  AiKeyLink.Parent := AiPage.Surface;
  AiKeyLink.Left := ScaleX(18);
  AiKeyLink.Top := AiKeyEdit.Top + ScaleY(26);
  AiKeyLink.Width := AiPage.SurfaceWidth - ScaleX(36);
  AiKeyLink.Caption := 'Get a free key at https://aistudio.google.com/apikey (a Google account is required).';

  UpdateAiKeyState;

  { Options (two checkboxes, both on by default) }
  OptionsPage := CreateInputOptionPage(AiPage.ID,
    'Options',
    'Two last choices. Both are recommended.',
    '',
    False, False);
  OptionsPage.Add('Start WheelHouse automatically when I log in');
  OptionsPage.Add('Start WheelHouse now, when setup finishes');
  OptionsPage.Values[0] := True;
  OptionsPage.Values[1] := True;

  { Microphone permission }
  MicPage := CreateCustomPage(OptionsPage.ID,
    'Microphone',
    'WheelHouse must be allowed to use your microphone, or it will hear nothing.');

  MicStatusLabel := TNewStaticText.Create(WizardForm);
  MicStatusLabel.Parent := MicPage.Surface;
  MicStatusLabel.Left := 0;
  MicStatusLabel.Top := ScaleY(8);
  MicStatusLabel.Width := MicPage.SurfaceWidth;
  MicStatusLabel.AutoSize := False;
  MicStatusLabel.WordWrap := True;
  MicStatusLabel.Height := ScaleY(40);

  MicHelpLabel := TNewStaticText.Create(WizardForm);
  MicHelpLabel.Parent := MicPage.Surface;
  MicHelpLabel.Left := 0;
  MicHelpLabel.Top := ScaleY(56);
  MicHelpLabel.Width := MicPage.SurfaceWidth;
  MicHelpLabel.AutoSize := False;
  MicHelpLabel.WordWrap := True;
  MicHelpLabel.Height := ScaleY(48);
  MicHelpLabel.Caption := 'If it is off: click the button to open Windows microphone settings, turn on "Let desktop apps access your microphone," then click Check again.';

  MicOpenButton := TNewButton.Create(WizardForm);
  MicOpenButton.Parent := MicPage.Surface;
  MicOpenButton.Left := 0;
  MicOpenButton.Top := ScaleY(112);
  MicOpenButton.Width := ScaleX(190);
  MicOpenButton.Height := ScaleY(26);
  MicOpenButton.Caption := 'Open microphone settings';
  MicOpenButton.OnClick := @OpenMicSettingsClick;

  MicRecheckButton := TNewButton.Create(WizardForm);
  MicRecheckButton.Parent := MicPage.Surface;
  MicRecheckButton.Left := ScaleX(200);
  MicRecheckButton.Top := ScaleY(112);
  MicRecheckButton.Width := ScaleX(120);
  MicRecheckButton.Height := ScaleY(26);
  MicRecheckButton.Caption := 'Check again';
  MicRecheckButton.OnClick := @RecheckMicClick;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if (MicPage <> nil) and (CurPageID = MicPage.ID) then
    UpdateMicStatus;
  if CurPageID = wpFinished then
    WizardForm.FinishedLabel.Caption :=
      'You''re all set. To see everything you can say and how it works:' + #13#10 + #13#10 +
      '  - Say "x-ray pattern manager," then click the "? Help" button, or' + #13#10 +
      '  - Right-click the WheelHouse icon near the clock (it shows as a red dot when' + #13#10 +
      '    listening; if you don''t see it, click the small up-arrow), and click' + #13#10 +
      '    "Pattern Manager."' + #13#10 + #13#10 +
      'Say each command as its own short phrase, not inside a longer sentence. Most ' +
      'commands work on their own; only a few need the word "x-ray" first.';
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (AiPage <> nil) and (CurPageID = AiPage.ID) then begin
    if AiCloudRadio.Checked and (Trim(AiKeyEdit.Text) = '') then begin
      MsgBox('Please paste your Google API key, or choose "Skip for now."', mbError, MB_OK);
      Result := False;
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
    RunEngine;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Params, enginePath: string;
  ResultCode, answer: Integer;
  execOk: Boolean;
begin
  if CurUninstallStep = usUninstall then begin
    enginePath := ExpandConstant('{app}\' + ENGINE);
    if not FileExists(enginePath) then begin
      // The removal helper is gone (a user hand-deleted it, or an antivirus
      // removed it). Inno will still delete {app} and the Add/Remove Programs
      // entry after this returns, but the engine's tree under
      // %LOCALAPPDATA%\WheelHouse (model, config, downloads) would be left
      // behind with no uninstaller. We cannot run the engine's clean removal,
      // so at least tell the user which folder to delete by hand.
      MsgBox(
        'The WheelHouse removal helper is missing, so the WheelHouse files in' + #13#10 +
        ExpandConstant('{localappdata}\WheelHouse') + #13#10 +
        'cannot be removed automatically. After this finishes, delete that folder' + #13#10 +
        'by hand to fully remove WheelHouse.',
        mbInformation, MB_OK);
    end else begin
      answer := MsgBox(
        'Keep your personal WheelHouse settings and voice patterns?' + #13#10 + #13#10 +
        'Yes = keep them (choose this if you might reinstall).' + #13#10 +
        'No = remove everything WheelHouse created.',
        mbConfirmation, MB_YESNO);
      // -Force skips the engine's interactive confirmation; the wizard cannot
      // answer it. -KeepData preserves personal data when the user asked.
      Params := '-NoProfile -ExecutionPolicy Bypass -File "' + enginePath + '" -Uninstall -Force';
      if answer = IDYES then
        Params := Params + ' -KeepData';
      execOk := Exec(ExpandConstant('{sysnative}\WindowsPowerShell\v1.0\powershell.exe'),
        Params, '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
      // If the engine could not finish (commonly: WheelHouse is still running, so
      // its verified-stopped check throws and it exits non-zero), abort the
      // uninstall. usUninstall runs BEFORE Inno deletes the files and the Add/Remove
      // Programs entry, so raising here preserves the uninstaller for a retry
      // instead of orphaning the app tree with no way left to remove it.
      if (not execOk) or (ResultCode <> 0) then begin
        MsgBox(
          'WheelHouse could not be fully removed. This usually means it is still' + #13#10 +
          'running. Please close it first -- right-click the WheelHouse icon near' + #13#10 +
          'the clock and choose Quit -- then run the uninstall again.',
          mbError, MB_OK);
        RaiseException('WheelHouse could not be removed (it may still be running). '
          + 'Close WheelHouse and try the uninstall again.');
      end;
    end;
  end;
end;
