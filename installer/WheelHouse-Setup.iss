; Wheelhouse-Setup.iss -- graphical installer wizard for Wheelhouse.
;
; This is a thin front end over scripts/release/public/install-wheelhouse.ps1
; (the engine). The wizard collects the user's choices with radio buttons and
; checkboxes, then runs the engine as a child process, passing the choices as
; parameters. The engine does the real work (download, verify, set up, write
; config, shortcuts). The wizard bundles only the engine script; the app archive
; and the speech model still download at install time, so the .exe stays small.
;
; Per-user, no administrator prompt: PrivilegesRequired=lowest and the engine
; installs entirely under the user profile (%LOCALAPPDATA%\Wheelhouse).
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
AppName=Wheelhouse
AppVersion={#AppVer}
AppPublisher=Wheelhouse Project
AppPublisherURL=https://wheelhouse-project.org/
; The engine owns the real install location. This directory holds only the
; engine script and the uninstaller, and is deliberately SEPARATE from the
; engine's own %LOCALAPPDATA%\Wheelhouse tree so a full uninstall (which removes
; that tree) does not delete this uninstaller out from under itself.
DefaultDirName={localappdata}\WheelhouseSetup
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableWelcomePage=no
PrivilegesRequired=lowest
OutputDir=build
OutputBaseFilename=Wheelhouse-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=Wheelhouse
VersionInfoVersion={#AppVer}
VersionInfoProductName=Wheelhouse
; Always write a setup log (%TEMP%\Setup Log YYYY-MM-DD #NNN.txt). The engine's
; console output -- including any shortcut-creation error -- is captured via
; EngineLog into this log; without it a field failure leaves no evidence
; (wh-startmenu-shortcut-check: first physical install lost the Start-menu
; shortcut and nothing recorded why).
SetupLogging=yes
; Inno 6.7.0+ turns on the Windows Redirection Guard mitigation by default,
; and children of Setup inherit it (verified empirically on Windows 10.0.26200;
; the Inno docs' claim that children do not inherit is wrong there). Under that
; mitigation the engine's uv.exe cannot traverse uv's own managed-Python
; minor-version junction (%APPDATA%\uv\python\cpython-X.Y-...), because a
; non-elevated user created it: `uv sync` fails with "untrusted mount point"
; (os error 448) and the install dies. Field failure: Setup Log 2026-07-21 #003.
; The guard exists to protect ELEVATED installers from unprivileged users'
; junctions; this installer is per-user and never elevated, so it loses no
; protection that applies to it.
RedirectionGuard=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
WelcomeLabel2=This will install Wheelhouse, voice control for your PC.%n%nThis takes about 10 to 20 minutes and needs an internet connection. Click Next to begin.
; Inno shows this box after our own failure dialog. The stock wording ("Please
; correct the problem and run Setup again") tells the user to do the one thing
; they do not know how to do, so it names the help address instead.
SetupAborted=Wheelhouse was not installed.%n%nYou can run Setup again at any time. If it keeps failing, email help@wheelhouse-project.org. Attach the setup log if the previous message showed you where to find it.

[Files]
; Bundled into the .exe and copied to {app} so the uninstaller can call it.
; Also extracted to a temporary folder at install time to run the install.
; The engine sits beside this script: in the public pool they are siblings, and
; the build workflow copies the engine next to this .iss before compiling. Paths
; resolve against the .iss directory (SourceDir), so a bare filename is correct.
Source: "install-wheelhouse.ps1"; DestDir: "{app}"; Flags: ignoreversion

[Code]
#include "tagparse.isi"
#include "failuremsg.isi"
#include "notices.isi"
#include "micconsent.isi"

const
  ENGINE = 'install-wheelhouse.ps1';
  HELP_EMAIL = 'help@wheelhouse-project.org';
  ISSUES_URL = 'https://github.com/wheelhouse-project/Wheelhouse/issues';

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
  { Why the engine stopped, and what it told the user to try, taken from its
    tagged FAILURE / FAILHINT output. Empty until the engine reports one.
    EngineFailSeen is what makes advice belong to a failure; the reason text
    cannot serve, because a FAILURE line whose text is empty is still a failure.
    EngineOutputError holds what Inno said if it could not go on reading the
    engine's output -- every tag after that point is lost, so it is the best
    remaining answer to what went wrong.
    EngineStarted answers a different question from EngineFailSeen: the engine
    can be running for minutes before it reports anything, so what the wizard
    says about an exception depends on both. EngineFinished answers a third one:
    the engine is waited for, so after the launch call returns the process is
    gone and anything that goes wrong belongs to clearing up.
    KeyLeftInEnvironment records that the cloud key could not be taken back out
    of this process's environment, which decides whether the failure dialog may
    start a child process (wh-wizard-failure-guidance.2.8). }
  EngineFailReason, EngineFailAdvice, EngineOutputError: string;
  EngineFailSeen, EngineStarted, EngineFinished, KeyLeftInEnvironment: Boolean;
  { What the engine had to say that did not stop the install: the speech engine
    it substituted, a requirement this machine does not meet, a step it could
    not finish. Collected from its tagged NOTICE output and shown on the finish
    page. EngineNoticesDropped counts the ones that did not fit, so the user is
    told they exist rather than left with a list that quietly stops
    (wh-wizard-distil-fallback). }
  EngineNotices: string;
  EngineNoticesDropped: Integer;

function SetEnvironmentVariable(lpName: string; lpValue: string): Boolean;
  external 'SetEnvironmentVariableW@kernel32.dll stdcall';

// ---------- existing-install detection (re-run / repair) ----------

function InstalledConfigPath: string;
begin
  // The engine installs under %LOCALAPPDATA%\Wheelhouse\app; its config.toml
  // records the user's prior choices. Mirrors $AppDir in install-wheelhouse.ps1.
  Result := ExpandConstant('{localappdata}\Wheelhouse\app\services\wheelhouse\config.toml');
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
  base, perApp, umbrella: string;
begin
  { Read both values, then let ResolveMicrophoneConsent in micconsent.isi decide.
    A read that fails and a value that is present and empty are both handed on as
    an empty string, because Windows reports success on a value that exists and
    is empty and the two cases mean the same thing here: this key did not answer
    the question. The engine's Resolve-MicrophoneConsent has the same shape and
    the two are run over one table of value pairs in
    scripts/release/tests/test_installer.py, because one decides whether a page
    is shown and the other decides whether a sentence is printed. }
  base := 'Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone';
  if not RegQueryStringValue(HKEY_CURRENT_USER, base + '\NonPackaged', 'Value', perApp) then
    perApp := '';
  if not RegQueryStringValue(HKEY_CURRENT_USER, base, 'Value', umbrella) then
    umbrella := '';
  Result := ResolveMicrophoneConsent(perApp, umbrella);
end;

procedure UpdateMicStatus;
begin
  if MicrophoneAllowed then
    MicStatusLabel.Caption := 'Microphone access for desktop apps is ON. You are all set.'
  else
    MicStatusLabel.Caption := 'Microphone access for desktop apps is OFF right now. Wheelhouse will hear nothing until you turn it on.';
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
  { Any callback at all means Inno created the process and is reading from it,
    including the one below that reports a broken output channel and returns
    straight away. Recorded first so no branch can leave it unrecorded. }
  EngineStarted := True;
  Log('engine: ' + S);
  if Error then begin
    { Inno reports a problem reading the engine's output through this same
      callback, with the description in S instead of a line of output. Nothing
      more will arrive, so keep it: without this the run ends with no reason at
      all and the dialog can only say the engine stopped without saying why
      (wh-wizard-failure-guidance.2.1). }
    if EngineOutputError = '' then
      EngineOutputError := Trim(S);
    Exit;
  end;
  line := Trim(S);
  if TagPayload(line, 'PROGRESS', rest) then begin
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
  end else if TagPayload(line, 'HEARTBEAT', msg) then begin
    WizardForm.ProgressGauge.Style := npbstMarquee;
    if msg <> '' then
      WizardForm.StatusLabel.Caption := msg + '  (still working, please wait...)';
  end else if TagPayload(line, 'NOTICE', msg) then begin
    { Kept for the finish page rather than shown here: the status label is
      overwritten by the next PROGRESS line, so a note put there would be gone
      before the user could read it. Which notes fit is decided in notices.isi,
      where the harness can run it. }
    AddNotice(msg, EngineNotices, EngineNoticesDropped);
  end else
    { Which failure line each piece of text belongs to is decided in
      failuremsg.isi, where the harness can run it. }
    ApplyFailureTag(line, EngineFailReason, EngineFailAdvice, EngineFailSeen);
  WizardForm.Refresh;
end;

// ---------- failure reporting ----------

function SetupLogPath: string;
begin
  // SetupLogging=yes guarantees a log file, and the log constant expands to its
  // full path. The guard is for the case where that ever stops holding: an
  // exception raised here would replace whatever message was being built with an
  // internal error, at the worst possible moment to lose it. Both callers treat
  // an empty result as "say nothing about a log", because naming a path the
  // wizard could not produce sends the user after a file they cannot find.
  // (A brace comment cannot be used around the line below -- the closing brace
  // of the constant would end the comment early.)
  try
    Result := ExpandConstant('{log}');
  except
    Result := '';
  end;
end;

procedure ReportEngineFailure;
var
  msg, logPath: string;
  ResultCode: Integer;
begin
  { The engine already worked out why it stopped and what the user should try.
    It runs hidden, so without this its explanation reached only the setup log
    and the user was left with a dialog that named no cause and no next step
    (wh-wizard-failure-guidance). }
  logPath := SetupLogPath;

  msg := FailureDialogText(EngineFailReason, EngineFailAdvice, logPath,
                           HELP_EMAIL, ISSUES_URL);

  { Naming the file is not the same as handing it over: it sits in %TEMP% under
    a name containing spaces and a '#'. Offer to open it only when it is really
    there, so the question is never asked about a file the user cannot find --
    and only when the child process that would open it cannot inherit the cloud
    key from this one. }
  if MayOfferTheLog((logPath <> '') and FileExists(logPath), KeyLeftInEnvironment) then begin
    if MsgBox(msg + #13#10 + #13#10 + 'Open the setup log now?', mbError, MB_YESNO) = IDYES then begin
      if not ShellExec('open', logPath, '', '', SW_SHOW, ewNoWait, ResultCode) then
        MsgBox('The setup log could not be opened. Its full path is:' + #13#10
             + logPath, mbInformation, MB_OK);
    end;
  end else
    MsgBox(msg, mbError, MB_OK);

  { Inno needs an exception to abort the install. }
  RaiseException(FailureAbortText(logPath, HELP_EMAIL));
end;

// ---------- run the engine ----------

procedure RunEngine;
var
  Params: string;
  ResultCode: Integer;
  ok, cloud: Boolean;
begin
  { Everything from here to the end of the engine run is inside one handler.
    Unpacking the engine, expanding a path and setting up the output redirection
    can all raise, and an exception raised here leaves this procedure without
    ever reaching ReportEngineFailure: Inno then shows its own internal error
    text, which names no cause and no next step -- the exact dialog this whole
    change exists to replace (wh-wizard-failure-guidance.2.1). }
  try
    ExtractTemporaryFile(ENGINE);
    cloud := AiCloudRadio.Checked;

    Params :=
      '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{tmp}\' + ENGINE) + '"' +
      ' -SttProvider ' + SpeechProviderArg +
      ' -AutoStart ' + YesNo(OptionsPage.Values[0]) +
      ' -StartNow ' + YesNo(OptionsPage.Values[1]) +
      { Asks the engine to write its notices to stdout as well as to the console
        nobody is watching. Passed present-only, never as -TaggedOutput:$false,
        for the same reason the yes/no answers are strings: under `powershell
        -File` a switch given a value is a parameter-binding error. }
      ' -TaggedOutput';

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
    // this installer process means the child inherits it at launch. Everything
    // after the key is set is inside the region that clears it: the wizard-form
    // calls below can raise, and this procedure goes on to show a dialog and can
    // start a child process to open the setup log, either of which would then be
    // running with the key still in the environment
    // (wh-wizard-failure-guidance.2.7).
    try
      if cloud then begin
        if not SetEnvironmentVariable('WHEELHOUSE_AI_API_KEY_INPUT', Trim(AiKeyEdit.Text)) then begin
          { The key reaches the engine only this way. Launching anyway installs
            with the AI features off while the wizard reports success, so what
            the user chose is discarded with nothing on screen to say so
            (wh-wizard-failure-guidance.2.8). Raising rather than reporting from
            here leaves the clearing below and the dialog to the handlers that
            already run in the right order. }
          EngineFailReason := 'Setup could not hand the AI key you entered to the '
                            + 'installation step, so it stopped rather than installing '
                            + 'with the AI features switched off.';
          { Naming the other choice by its caption would be wrong half the time:
            it reads "Skip for now" on a first install and "Leave my AI helper
            setting unchanged" on a repair, where it keeps an existing cloud
            setup rather than skipping anything (wh-wizard-failure-guidance.2.11). }
          EngineFailAdvice := 'Run Setup again and select the other choice on the AI '
                            + 'helper page; Setup does not need the key for that choice. '
                            + 'Speech, voice commands, dictation and clicking all work '
                            + 'without the AI helper.';
          RaiseException('the cloud key could not be placed in the environment');
        end;
      end;

      WizardForm.ProgressGauge.Style := npbstMarquee;
      WizardForm.StatusLabel.Caption := 'Setting up Wheelhouse. This can take 10 to 20 minutes...';
      WizardForm.Refresh;

      ok := ExecAndLogOutput(
        ExpandConstant('{sysnative}\WindowsPowerShell\v1.0\powershell.exe'),
        Params, '', SW_HIDE, ewWaitUntilTerminated, ResultCode, @EngineLog);
      { The call waits for the engine, so a launch that returned is a process
        that existed and has now ended. Only ever set, never cleared: what the
        engine's own output already proved cannot be taken back by a later
        answer. }
      if ok then begin
        EngineStarted := True;
        EngineFinished := True;
      end;
    finally
      if cloud then begin
        if not SetEnvironmentVariable('WHEELHOUSE_AI_API_KEY_INPUT', '') then begin
          KeyLeftInEnvironment := True;
          Log('engine: the cloud key could not be cleared from the environment; '
            + 'setup will not start any further programs from this process');
        end;
      end;
    end;
  except
    { The engine never got far enough to say anything, so the wizard says what
      it knows instead. Anything it already reported is better and is kept --
      including a failure whose text was empty, which is why the function is
      told whether one was seen and not only whether the reason is blank. }
    if EngineFailReason = '' then
      EngineFailReason := EngineRaisedReason(EngineStarted, EngineFinished, EngineFailSeen,
                                             GetExceptionMessage);
    ReportEngineFailure;
  end;

  if (not ok) or (ResultCode <> 0) then begin
    if EngineFailReason = '' then
      EngineFailReason := EngineStopReason(EngineFailSeen, ok, ResultCode, EngineOutputError);
    ReportEngineFailure;
  end;
end;

// ---------- wizard events ----------

procedure InitializeWizard;
begin
  { Speech engine (radio buttons) }
  SpeechPage := CreateInputOptionPage(wpWelcome,
    'Speech engine',
    'How should Wheelhouse turn what you say into text?',
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
  OptionsPage.Add('Start Wheelhouse automatically when I log in');
  OptionsPage.Add('Start Wheelhouse now, when setup finishes');
  OptionsPage.Values[0] := True;
  OptionsPage.Values[1] := True;

  { Microphone permission }
  MicPage := CreateCustomPage(OptionsPage.ID,
    'Microphone',
    'Wheelhouse must be allowed to use your microphone, or it will hear nothing.');

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

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  { The microphone page exists to fix a permission that is off. Shown to a user
    whose permission is already on, it opens by saying Wheelhouse will hear
    nothing without it, then says it is on, then explains what to do "if it is
    off", above two buttons that do nothing for them -- three sentences about a
    problem they do not have (wh-wizard-mic-permission-noise). The check reads
    the same two registry values the engine reads at the end of the install, and
    both treat an unreadable setting as allowed, so a Windows build that stores
    it elsewhere is never sent after a setting that is not the problem. }
  Result := (MicPage <> nil) and (PageID = MicPage.ID) and MicrophoneAllowed;
end;

procedure CurPageChanged(CurPageID: Integer);
var
  notes, kept, standing: string;
  dropped, room, i: Integer;
  texts: TArrayOfString;
  heights: TArrayOfInteger;
begin
  if (MicPage <> nil) and (CurPageID = MicPage.ID) then
    UpdateMicStatus;
  if CurPageID = wpFinished then begin
    standing :=
      'You''re all set. To see everything you can say and how it works:' + #13#10 + #13#10 +
      '  - Say "x-ray pattern manager," then click the "? Help" button, or' + #13#10 +
      '  - Right-click the Wheelhouse icon near the clock (it shows as a red dot when' + #13#10 +
      '    listening; if you don''t see it, click the small up-arrow), and click' + #13#10 +
      '    "Pattern Manager."' + #13#10 + #13#10 +
      'Say each command as its own short phrase, not inside a longer sentence. Most ' +
      'commands work on their own; only a few need the word "x-ray" first.';

    { Build one candidate caption per number of notes kept, from every note down
      to none. The notes go above the standing text rather than below it: a note
      saying the speech engine was substituted is the one thing on this page the
      user did not already choose (wh-wizard-distil-fallback). MAX_NOTICE_TEXT
      bounds the characters, which says nothing about how many lines they wrap
      to, so the page may still have more than it can draw. }
    kept := EngineNotices;
    dropped := EngineNoticesDropped;
    SetArrayLength(texts, 0);
    while True do begin
      notes := NoticeBlockText(kept, dropped, SetupLogPath);
      if notes <> '' then
        notes := notes + #13#10;
      SetArrayLength(texts, GetArrayLength(texts) + 1);
      texts[GetArrayLength(texts) - 1] := notes + standing;
      if not DropLastNotice(kept, dropped) then
        Break;
    end;

    { The label Inno lays out here is a fixed rectangle sized for its own
      one-sentence finish message, and it does not grow when the caption is
      replaced: measured on this machine it stands at 193 pixels while the
      standing text alone needs 529, so eight of its eleven lines were never
      drawn. Letting it size itself is what makes the text visible; the space
      below its top is what it has to fit inside, and that measured 1035. }
    WizardForm.FinishedLabel.AutoSize := True;
    room := WizardForm.FinishedLabel.Parent.ClientHeight
            - WizardForm.FinishedLabel.Top;

    { Nothing here can work out how tall a caption will be, so each candidate is
      assigned and measured. Choosing between them is BestNoticeCandidate's job,
      because taking a note off does not reliably shorten the page and simply
      walking to the end can lose a note's text while leaving the page taller. }
    SetArrayLength(heights, GetArrayLength(texts));
    for i := 0 to GetArrayLength(texts) - 1 do begin
      WizardForm.FinishedLabel.Caption := texts[i];
      heights[i] := WizardForm.FinishedLabel.Height;
    end;
    WizardForm.FinishedLabel.Caption := texts[BestNoticeCandidate(heights, room)];
  end;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (AiPage <> nil) and (CurPageID = AiPage.ID) then begin
    if AiCloudRadio.Checked and (Trim(AiKeyEdit.Text) = '') then begin
      { Same reason as the advice in RunEngine: the other choice is captioned
        differently on a repair, so it is pointed at rather than quoted. }
      MsgBox('Please paste your Google API key, or select the other choice on this page.',
             mbError, MB_OK);
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
      // %LOCALAPPDATA%\Wheelhouse (model, config, downloads) would be left
      // behind with no uninstaller. We cannot run the engine's clean removal,
      // so at least tell the user which folder to delete by hand.
      MsgBox(
        'The Wheelhouse removal helper is missing, so the Wheelhouse files in' + #13#10 +
        ExpandConstant('{localappdata}\Wheelhouse') + #13#10 +
        'cannot be removed automatically. After this finishes, delete that folder' + #13#10 +
        'by hand to fully remove Wheelhouse.',
        mbInformation, MB_OK);
    end else begin
      answer := MsgBox(
        'Keep your personal Wheelhouse settings and voice patterns?' + #13#10 + #13#10 +
        'Yes = keep them (choose this if you might reinstall).' + #13#10 +
        'No = remove everything Wheelhouse created.',
        mbConfirmation, MB_YESNO);
      // -Force skips the engine's interactive confirmation; the wizard cannot
      // answer it. -KeepData preserves personal data when the user asked.
      Params := '-NoProfile -ExecutionPolicy Bypass -File "' + enginePath + '" -Uninstall -Force';
      if answer = IDYES then
        Params := Params + ' -KeepData';
      execOk := Exec(ExpandConstant('{sysnative}\WindowsPowerShell\v1.0\powershell.exe'),
        Params, '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
      // If the engine could not finish (commonly: Wheelhouse is still running, so
      // its verified-stopped check throws and it exits non-zero), abort the
      // uninstall. usUninstall runs BEFORE Inno deletes the files and the Add/Remove
      // Programs entry, so raising here preserves the uninstaller for a retry
      // instead of orphaning the app tree with no way left to remove it.
      if (not execOk) or (ResultCode <> 0) then begin
        MsgBox(
          'Wheelhouse could not be fully removed. This usually means it is still' + #13#10 +
          'running. Please close it first -- right-click the Wheelhouse icon near' + #13#10 +
          'the clock and choose Quit -- then run the uninstall again.',
          mbError, MB_OK);
        RaiseException('Wheelhouse could not be removed (it may still be running). '
          + 'Close Wheelhouse and try the uninstall again.');
      end;
    end;
  end;
end;
