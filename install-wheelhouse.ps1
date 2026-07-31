# install-wheelhouse.ps1 -- one-command Wheelhouse installer for end users.
#
# Primary invocation (from the README):
#   irm https://github.com/wheelhouse-project/Wheelhouse/releases/latest/download/install-wheelhouse.ps1 | iex
#
# Secondary invocation (downloaded file):
#   powershell -ExecutionPolicy Bypass -File install-wheelhouse.ps1
#
# Uninstall (requires the downloaded file -- iex cannot pass parameters):
#   powershell -ExecutionPolicy Bypass -File install-wheelhouse.ps1 -Uninstall
#
# What it does, in order: preflight checks, uv install (winget, then the
# official install script), release archive download + SHA256 verify,
# per-service uv sync, one STT question, Parakeet model download + verify,
# config writing, shortcuts, optional start-at-login, optional start-now.
# Re-running repairs or updates an existing install; user settings and
# voice patterns are preserved across updates.
#
# PowerShell 5.1 compatible. Design: release plan section 5
# (docs/superpowers/specs/2026-07-09-open-source-release-v1-plan.md in the
# development repository).

[CmdletBinding()]
param(
    [switch]$Uninstall,
    # Override the app archive source (used by the clean-VM validation gate
    # to serve the export over local HTTP). Env-var equivalents exist so the
    # irm|iex path can be overridden too: WHEELHOUSE_ARCHIVE_URL and
    # WHEELHOUSE_ARCHIVE_SHA256.
    [string]$ArchiveUrl = "",
    [string]$ArchiveSha256 = "",
    # Speech engine, supplied by the graphical installer so its choice skips
    # the interactive question. Empty means "ask" (the one-liner path). The
    # ValidateSet rejects an unknown value at the command line; an unset
    # default is exempt from validation.
    [ValidateSet("parakeet_tdt", "google_stt", "distil_medium_en")]
    [string]$SttProvider = "",
    # Install-flow answers the graphical installer supplies so its yes/no
    # questions are not asked. A yes/no string sentinel, NOT a [switch]: the
    # installer launches this script with `powershell -File`, and under -File a
    # switch passed as "-Switch:$false" is a hard parameter-binding error (Inno
    # Setup passes literal args), so a switch cannot carry a definite "no". A
    # string binds cleanly -- pass "-AutoStart no" / "-AutoStart yes". Empty
    # (the default) means "ask", which is the interactive one-liner path.
    [ValidateSet("yes", "no")]
    [string]$AutoStart = "",
    [ValidateSet("yes", "no")]
    [string]$StartNow = "",
    # AI helper choice, supplied by the graphical installer. "keep" (the default,
    # meaning -AiMode was omitted) preserves an existing install's AI config on a
    # re-run and defaults a FRESH install to off -- so a re-run without -AiMode
    # never clobbers a working cloud setup, and a first install still does not
    # ship AI on and pointed at an Ollama the installer never sets up (codex 2.1).
    # "off" is explicit: it writes an empty base_url -- the documented "AI off"
    # switch -- AND clears the persisted cloud key so no stale secret lingers in
    # the user environment (codex 2.3). "cloud" writes Google's Gemini Flash Lite
    # OpenAI-compatible endpoint + model + kind=cloud. -AiApiKey is the cloud key;
    # it is routed to the environment, never config.toml (git-tracked --
    # wh-ai-key-from-env). The graphical installer must NOT pass the key with
    # -AiApiKey (a command line is readable via Win32_Process.CommandLine while
    # the install runs); it sets WHEELHOUSE_AI_API_KEY_INPUT on the child process
    # instead, which Resolve-AiApiKey reads. -AiApiKey stays for the developer /
    # one-liner path. -AiBaseUrl / -AiModel are optional cloud overrides that
    # default to the pinned Gemini values above.
    # "local" downloads a llama.cpp build and a model file and configures
    # Wheelhouse to start its own server. It needs no account and no key, which
    # is why it exists: the cloud option requires a Google key, and a user who
    # cannot or will not get one had no way to use the fix and rewrite commands
    # at all. It runs the hardware check first and, on a machine that cannot
    # run the model, reports why and writes the off state rather than
    # installing 5 GB that will not start (wh-ai-installer-local-mode).
    [ValidateSet("keep", "off", "cloud", "local")]
    [string]$AiMode = "keep",
    [string]$AiApiKey = "",
    [string]$AiBaseUrl = "",
    [string]$AiModel = "",
    # Uninstall-flow answers. -Force skips the "are you sure" confirmation;
    # -KeepData preserves personal data without asking. -Force without
    # -KeepData removes personal data without asking. These stay bare switches
    # (safe under -File): the wizard only ever passes them present or omits
    # them, never "-Force:$false". The graphical installer always passes -Force
    # for an uninstall (it cannot answer an interactive prompt); the one-liner
    # -Uninstall path passes neither and is asked both questions.
    [switch]$Force,
    [switch]$KeepData,
    # Asks for the machine-readable copy of every notice (see Write-InstallNotice).
    # The graphical installer passes it; a person running this script in a console
    # does not, and reads the [!] lines instead. Without the switch both would be
    # printed and every notice would appear twice on that console. A bare switch
    # is safe under `powershell -File` as long as the caller passes it present or
    # omits it, never "-TaggedOutput:$false", which is what the wizard does.
    [switch]$TaggedOutput
)

$ErrorActionPreference = "Stop"

# Empty when the script text is piped through Invoke-Expression (the primary
# irm|iex path); set when run as a downloaded file. Failure handling must not
# call `exit` in the iex case -- that closes the user's console window.
$script:RunningFromFile = [bool]$PSCommandPath

# --- Pinned versions and sources -------------------------------------------
# The archive URL and hash are stamped on publish day: build the release
# archive, hash it, stamp both values here, upload archive + this script.

$AppVersion = "1.0.7"
$DefaultArchiveUrl = "https://github.com/wheelhouse-project/Wheelhouse/releases/download/v$AppVersion/wheelhouse-$AppVersion.zip"
$DefaultArchiveSha256 = "<ARCHIVE-SHA256>"

# Parakeet TDT 0.6b v3 int8 -- the default offline STT model. URL + SHA256
# verified against the upstream GitHub release asset digest (2026-07-11).
$ModelUrl = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8.tar.bz2"
$ModelSha256 = "5793d0fd397c5778d2cf2126994d58e9d56b1be7c04d13c7a15bb1b4eafb16bf"
$ModelDirName = "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"

# Hardware floor for the default Parakeet CPU tier. The RAM floor is a hard
# stop (the model plus the app cannot run usefully below it); the CPU floor
# only warns, because it is extrapolated from a fast development machine and
# has never been validated on a genuinely slow core -- hard-stopping on an
# unvalidated heuristic would be the worse failure for this audience.
$RamFloorBytes = 75 * 100MB      # nominal 8 GB, measured physical is less
$RamRecommendedBytes = 15 * 1GB  # nominal 16 GB
$CpuWarnCores = 4
$DiskFloorBytes = 10GB           # app + venvs + model archive + extraction

# NVIDIA PCI vendor id, for the CUDA provider offer.
$NvidiaVendorId = 4318
$CudaMinVramBytes = 4GB

# Cloud AI defaults. The graphical installer pre-fills Google's Gemini Flash
# Lite (OpenAI-compatible endpoint) so the user supplies only a key; the engine
# writes these into [ai.server] for -AiMode cloud unless -AiBaseUrl / -AiModel
# override them. The key is NEVER written to config.toml (git-tracked) -- it
# goes to the WHEELHOUSE_AI_API_KEY environment variable (wh-ai-key-from-env).
# This endpoint answers the app's readiness probe: Google documents a supported
# GET /v1beta/openai/models under this root (returns 200 with a valid key), so
# the app's is_available() check (GET base_url/models) correctly reports the
# cloud AI as reachable rather than a false "AI not available" (deepseek 1.3).
$DefaultAiBaseUrl = "https://generativelanguage.googleapis.com/v1beta/openai/"
$DefaultAiModel = "gemini-2.5-flash-lite"

# Local AI runtime (-AiMode local). The same two addresses and hashes are
# pinned in services/installer/components/registry.py; this script is
# downloaded and run on its own and cannot import Python, so the duplication is
# guarded by a test that reads both files rather than prevented
# (test_installer_local_ai.py test_the_pins_match_the_component_registry).
#
# The llama.cpp build is the VULKAN one, not CUDA: Vulkan covers NVIDIA, AMD,
# and Intel alike, which is what lets the hardware check accept any vendor's
# card instead of stranding every AMD machine on the processor.
#
# We do NOT redistribute the weights. The model address is Google's own
# repository on Hugging Face, pinned by the hash its publisher records.
$LlamaServerUrl = "https://github.com/ggml-org/llama.cpp/releases/download/b10107/llama-b10107-bin-win-vulkan-x64.zip"
$LlamaServerSha256 = "c5b3a5ee8319b1eccbb748a54390aa806bbf7d1aceeea452e4c57921d113e53e"
$LocalAiModelUrl = "https://huggingface.co/google/gemma-4-E4B-it-qat-q4_0-gguf/resolve/main/gemma-4-E4B_q4_0-it.gguf"
$LocalAiModelSha256 = "676c35070db6dbe52f93e9c864ee0fba4eddea94b9c875d9cb10daff453fbaee"
$LocalAiModelFileName = "gemma-4-E4B_q4_0-it.gguf"
# The name shown in the model menu. llama-server serves one model and ignores
# the name in a request, so this is for the user to recognize, not for routing.
$LocalAiModelName = "gemma-4-e4b"
# The address Wheelhouse talks to AND the port it starts the server on --
# [ai.runtime] deliberately has no port key, so these cannot drift apart
# (ai/runtime_config.py). It must be loopback with an explicit port or the
# runtime refuses to start.
$LocalAiBaseUrl = "http://127.0.0.1:8781/v1"

# --- Paths ------------------------------------------------------------------

$LocalRoot = Join-Path $env:LOCALAPPDATA "Wheelhouse"
$AppDir = Join-Path $LocalRoot "app"
$ModelsDir = Join-Path $LocalRoot "models"
# Deliberately NOT $ModelsDir: that directory holds the extracted speech model,
# and Test-ModelComplete judges whether the speech model is installed by which
# files are sitting in it. A 5 GB GGUF dropped in there invites exactly that
# confusion.
$AiModelsDir = Join-Path $LocalRoot "ai-models"
$LlamaDir = Join-Path $LocalRoot "llama"
$DownloadsDir = Join-Path $LocalRoot "downloads"
$RoamingRoot = Join-Path $env:APPDATA "Wheelhouse"
$OverrideFile = Join-Path $LocalRoot "stt_model_overrides.toml"

# Files preserved across updates (relative to the app directory). This is a
# PER-FILE list on purpose: data/ mixes user state with shipped read-only
# starter files, and preserving the whole directory would freeze the shipped
# files forever. User files are restored AFTER extraction; shipped files
# always come from the new archive.
$PreservePaths = @(
    "services\wheelhouse\config.toml",
    "services\wheelhouse\data\user_patterns.toml",
    "services\wheelhouse\data\soft_allow_tuples.toml",
    "services\wheelhouse\data\soft_allow_declined_tuples.toml",
    "services\wheelhouse\data\soft_allow_pending_counters.toml",
    "services\stt_providers\shared\hints.txt"
)

$ShortcutName = "Wheelhouse.lnk"
$IssuesUrl = "https://github.com/wheelhouse-project/Wheelhouse/issues"

# --- Output helpers ----------------------------------------------------------

function Hide-UserProfilePath {
    param([string]$Text)
    # The help document promises that installer failure messages contain no
    # personal data and can be included in a help request -- and the profile
    # path carries the Windows account name. Failure text can quote Python
    # exceptions and native tool output, which name absolute paths under the
    # profile; Python's repr form doubles the backslashes. Each separator in
    # the profile path therefore matches one or more of either slash, so
    # C:\Users\name, C:\\Users\\name, and C:/Users/name all become the
    # placeholder. LOCALAPPDATA and APPDATA get their own placeholders so a
    # launcher that unset USERPROFILE but left the others cannot reopen the
    # leak; when USERPROFILE is set it is replaced first and already covers
    # both.
    foreach ($pair in @(
        @{ Value = $env:USERPROFILE; Placeholder = '%USERPROFILE%' },
        @{ Value = $env:LOCALAPPDATA; Placeholder = '%LOCALAPPDATA%' },
        @{ Value = $env:APPDATA; Placeholder = '%APPDATA%' }
    )) {
        if (-not $pair.Value) { continue }
        $pattern = [regex]::Escape($pair.Value) -replace '\\\\', '[\\/]+'
        $Text = $Text -replace $pattern, $pair.Placeholder
    }
    return $Text
}

# Write-Warn and Write-Fail sanitize because they are the writers every
# failure and warning surface goes through -- including the top-level
# unexpected-error handler, which interpolates raw exception text and does
# not pass through Stop-Install. Write-Status stays unsanitized: success
# messages that name a path (a created shortcut, the install folder) name
# one the installer built itself and the user may need verbatim.
function Write-Status { param([string]$Message) Write-Host "[+] $Message" -ForegroundColor Green }
function Write-Warn { param([string]$Message) Write-Host "[!] $(Hide-UserProfilePath -Text $Message)" -ForegroundColor Yellow }
function Write-Fail { param([string]$Message) Write-Host "[x] $(Hide-UserProfilePath -Text $Message)" -ForegroundColor Red }

# The single choke point for every yes/no question the installer asks, so the
# graphical installer can answer each one non-interactively. When -Specified
# is true the caller-supplied -Value is returned and nothing is asked;
# otherwise the user is prompted and only the exact word "yes" is a yes.
function Resolve-YesNoChoice {
    param([bool]$Specified, [bool]$Value, [string]$Prompt)
    if ($Specified) { return $Value }
    return ((Read-Host $Prompt) -eq "yes")
}

# Machine-readable progress for a wizard reading this script's stdout as a child
# process (Write-Progress is invisible across that boundary). A PROGRESS line
# advances a real bar; a heartbeat is emitted immediately before each silent
# step (uv sync, archive/model extraction) so the wizard shows an honest "still
# working" state, and the next PROGRESS milestone is the after-the-step signal
# that it did not freeze (design 7). [Console]::Out.WriteLine targets the real
# stdout stream directly, past any PowerShell host-redirection quirks.
function Write-InstallProgress {
    param([int]$Pct, [string]$Label)
    [Console]::Out.WriteLine("PROGRESS $Pct $Label")
}

function Write-InstallHeartbeat {
    param([string]$Label)
    [Console]::Out.WriteLine("HEARTBEAT $Label")
}

# The reason this install is stopping, in the same machine-readable form as
# PROGRESS and HEARTBEAT and for the same reason: the graphical installer runs
# this script hidden, and the person at the screen must be told what went wrong
# and what to do about it. Write-Host output goes to the PowerShell host, whose
# redirection across a hidden child process is exactly what these tagged lines
# avoid, so the wizard's failure dialog is built from these two lines rather
# than from the [x] and What-to-try lines a console user reads.
# Each tag is one line: the wizard reads stdout a line at a time and cannot
# tell a wrapped message from a new one, so any newline inside the text is
# folded to a space before it is written (wh-wizard-failure-guidance).
function Write-InstallFailure {
    param([string]$Reason, [string]$WhatToTry = "")
    # Sanitized here as well as in Write-Fail: the wizard's failure dialog is
    # built from these tagged lines, and the unexpected-error handler feeds
    # them raw exception text directly.
    $Reason = Hide-UserProfilePath -Text $Reason
    [Console]::Out.WriteLine("FAILURE " + ($Reason -replace '\s+', ' '))
    if ($WhatToTry) {
        $WhatToTry = Hide-UserProfilePath -Text $WhatToTry
        [Console]::Out.WriteLine("FAILHINT " + ($WhatToTry -replace '\s+', ' '))
    }
}

# Something the user should still know about once setup has finished: the speech
# engine that was substituted for the one they picked, a requirement this machine
# does not meet, a step that could not be completed. Write-Warn alone reaches the
# PowerShell host, and the graphical installer runs this script hidden, so those
# messages reached the setup log and nothing else -- a user whose Distil-Whisper
# choice was swapped for Parakeet was told nothing at all
# (wh-wizard-distil-fallback). The tagged copy goes to the real stdout stream the
# wizard reads, on the same one-line-per-message contract as FAILURE and FAILHINT:
# the wizard cannot tell a wrapped message from a new one, so any newline in the
# text is folded to a space before it is written.
#
# Warnings about something the installer went on to recover from -- a download
# retried, a stale partial file discarded -- stay on Write-Warn: they are true
# while they are printed and not afterwards, and the finish page is read at the
# end.
function Write-InstallNotice {
    param([string]$Message)
    # Sanitized before the tagged copy too: notices carry exception text
    # (shortcut and AI-helper failures) and reach the wizard's finish page.
    $Message = Hide-UserProfilePath -Text $Message
    Write-Warn $Message
    if ($TaggedOutput) {
        [Console]::Out.WriteLine("NOTICE " + ($Message -replace '\s+', ' '))
    }
}

# Whether Windows currently lets desktop applications use the microphone. The
# wizard reads these same two values before it decides whether to show its
# microphone page (MicrophoneAllowed in Wheelhouse-Setup.iss); two checks of one
# setting that disagreed would be silent in both directions, so both read the
# per-application value first, fall back to the umbrella one, and treat a setting
# they cannot read as allowed rather than telling the user to go and fix a
# setting that is not the problem (wh-wizard-mic-permission-noise).
function Resolve-MicrophoneConsent {
    param([string]$PerApp, [string]$Umbrella)
    # $PerApp is the NonPackaged value, $Umbrella the one on the key above it.
    # Either arrives empty when the key is absent, when the read failed, or when
    # the value is present and empty -- Windows reports success on a value that
    # exists and is empty, and the three cases mean the same thing here: this key
    # did not answer the question.
    #
    # An empty first value is not an answer, so the umbrella value is asked next.
    # When neither answers, the result is "allowed": a Windows build that stores
    # this somewhere else must not make the installer warn about a microphone
    # that works.
    #
    # This is the half of the check the wizard has to agree with. Its Pascal
    # counterpart is ResolveMicrophoneConsent in micconsent.isi, and
    # scripts/release/tests/test_installer.py runs both over one table of value
    # pairs, because this decides whether a sentence is printed and that one
    # decides whether a whole page is shown.
    if ($PerApp) { return ($PerApp -eq "Allow") }
    if ($Umbrella) { return ($Umbrella -eq "Allow") }
    return $true
}

function Test-MicrophonePermission {
    $base = "HKCU:\Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
    $values = @("", "")
    $keys = @("$base\NonPackaged", $base)
    for ($i = 0; $i -lt $keys.Count; $i++) {
        try {
            $raw = (Get-ItemProperty -LiteralPath $keys[$i] -Name Value -ErrorAction Stop).Value
            # Only a string answers. Windows writes this setting as a string, and
            # the wizard's RegQueryStringValue refuses anything else and moves on
            # to the next key -- converting a number to text here instead would
            # make the two disagree on a machine that had one.
            if ($raw -is [string]) { $values[$i] = $raw }
        } catch {
            # This key is absent on this Windows build; it answers nothing.
        }
    }
    return (Resolve-MicrophoneConsent -PerApp $values[0] -Umbrella $values[1])
}

function Write-TomlFile {
    param([string]$Path, [string[]]$Lines, [string]$NewLine = "`r`n")
    # The app reads its TOML files in binary mode with tomllib, which
    # rejects a UTF-8 BOM -- and PowerShell 5.1's Set-Content -Encoding UTF8
    # always writes one. Write through .NET with an explicit BOM-less
    # encoding instead. Join with an explicit terminator (WriteAllText, not
    # WriteAllLines) so a caller rewriting an existing file can preserve its
    # line-ending style; WriteAllLines hard-codes Environment.NewLine (CRLF
    # on Windows) and would silently flip an LF-only shipped config to CRLF.
    # Default CRLF matches the previous WriteAllLines behavior for callers
    # authoring a fresh file.
    $encoding = New-Object System.Text.UTF8Encoding($false)
    if ($Lines.Count -eq 0) {
        $text = ""
    } else {
        $text = ($Lines -join $NewLine) + $NewLine
    }
    [System.IO.File]::WriteAllText($Path, $text, $encoding)
}

function ConvertTo-TomlBasicString {
    param([string]$Value)
    # Encode a TOML basic (double-quoted) string: escape backslash first, then
    # double-quote. Enough for the values the installer writes (endpoint, model,
    # kind) and any wizard override of -AiBaseUrl / -AiModel; control characters
    # do not occur in these. Without this, an override containing a quote or
    # backslash would produce malformed TOML and the app's tomllib load would
    # reject the whole config.
    '"' + ($Value -replace '\\', '\\' -replace '"', '\"') + '"'
}

function Stop-Install {
    param([string]$Message, [string]$WhatToTry = "")
    # Write-Fail and Write-InstallFailure sanitize their own text; the
    # what-to-try console line below goes through Write-Host directly, so it
    # is sanitized here.
    if ($WhatToTry) { $WhatToTry = Hide-UserProfilePath -Text $WhatToTry }
    Write-Fail $Message
    if ($WhatToTry) { Write-Host "    What to try: $WhatToTry" }
    Write-Host "    If you are stuck, please file an issue: $IssuesUrl"
    Write-Host "    or email help@wheelhouse-project.org"
    Write-InstallFailure -Reason $Message -WhatToTry $WhatToTry
    # Unwinds to the top-level handler instead of calling `exit`: under the
    # irm|iex path, `exit` terminates the user's whole console session.
    throw "WHEELHOUSE-INSTALL-STOP"
}

function Invoke-Native {
    param([string]$Exe, [string[]]$Arguments)
    # Runs a native command with stderr merged into the captured output.
    # This CANNOT be done inline under $ErrorActionPreference = 'Stop':
    # PowerShell 5.1 wraps redirected stderr lines in error records, and
    # Stop promotes the first one to a terminating error even when the
    # command exits 0 -- and uv writes all its progress to stderr. Relax
    # the preference around the call and rely on the exit code.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & $Exe @Arguments 2>&1
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    @{ Output = @($output); ExitCode = $code }
}

# --- Preflight checks (step a) ------------------------------------------------

function Test-Preflights {
    Write-Status "Checking this computer meets the requirements..."

    if (-not [Environment]::Is64BitOperatingSystem) {
        Stop-Install "Wheelhouse needs 64-bit Windows; this system is 32-bit." `
            "There is nothing to fix on this computer. Wheelhouse needs 64-bit Windows 10 or 11, and cannot run here."
    }
    $os = Get-CimInstance Win32_OperatingSystem
    $osVersion = [Version]$os.Version
    if ($osVersion.Major -lt 10) {
        Stop-Install "Wheelhouse needs Windows 10 or 11; this system reports Windows version $($os.Version)." `
            "Update this computer to Windows 10 or 11, then run Setup again."
    }

    # Disk space on the drive that will hold the install.
    $driveName = (Get-Item -LiteralPath $env:LOCALAPPDATA).PSDrive.Name
    $free = (Get-PSDrive -Name $driveName).Free
    if ($free -lt $DiskFloorBytes) {
        $freeGb = [math]::Round($free / 1GB, 1)
        Stop-Install "Not enough free disk space on drive ${driveName}: ($freeGb GB free, 10 GB needed)." `
            "Free up disk space, then run the installer again."
    }

    # RAM floor: hard stop BEFORE downloading anything. Below this the
    # default speech model loads too slowly to be usable. Routed through
    # Stop-Install like every other hard stop, so this failure also gets
    # the standard What-to-try line and the issues URL.
    $ram = (Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory
    if ($ram -lt $RamFloorBytes) {
        $ramGb = [math]::Round($ram / 1GB, 1)
        Stop-Install "This computer has $ramGb GB of memory. The built-in offline speech engine needs about 8 GB to run well." `
            "Use the Google Cloud speech engine instead -- it runs in the cloud and needs far less memory, but requires a Google Cloud account (see INSTALL.md in the Wheelhouse repository). Or add memory to this computer."
    }
    if ($ram -lt $RamRecommendedBytes) {
        Write-InstallNotice "This computer has $([math]::Round($ram / 1GB, 1)) GB of memory. Wheelhouse will run, but 16 GB is recommended."
    }

    # CPU floor: warn and continue (see the note on the constant above).
    $cores = (Get-CimInstance Win32_Processor | Measure-Object -Property NumberOfCores -Sum).Sum
    if ($cores -lt $CpuWarnCores) {
        Write-InstallNotice "This computer has $cores CPU cores. Speech recognition may respond slowly; 4 or more cores are recommended."
    }

    # Microphone presence: warn only -- the user may plug one in later.
    $micCount = 0
    try {
        $captureKey = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture"
        if (Test-Path $captureKey) {
            foreach ($endpoint in Get-ChildItem $captureKey) {
                $state = (Get-ItemProperty -LiteralPath $endpoint.PSPath -Name DeviceState -ErrorAction SilentlyContinue).DeviceState
                if ($state -eq 1) { $micCount++ }
            }
        }
    } catch { $micCount = -1 }
    if ($micCount -eq 0) {
        Write-InstallNotice "No active microphone was found. Wheelhouse needs one to hear you -- plug one in before the first run."
    }

    Write-Status "Requirement checks passed."
}

# --- Running-app refusal (stop before wipe) ------------------------------------

function Test-CommandLineInAppDir {
    param([string]$CommandLine)
    # Path-boundary match: the app directory must be followed by a path
    # separator, a closing quote, whitespace, or the end of the command
    # line. A bare substring test would also match sibling directories
    # that merely start with the same text (e.g. Wheelhouse\app_backup)
    # and falsely refuse an install.
    if (-not $CommandLine) { return $false }
    $pattern = [regex]::Escape($AppDir.ToLower()) + '($|[\\/"''\s])'
    return [bool]($CommandLine.ToLower() -match $pattern)
}

function Test-RunningWheelHouse {
    param([switch]$RequireVerified)
    # All four Wheelhouse processes are python.exe inside venvs, so detection
    # matches each process's command line against the install directory.
    # Name-based matching would either miss them or hit innocent Python
    # processes.
    #
    # -RequireVerified is for the re-checks immediately before a
    # destructive step: if the check itself fails there (WMI broken on
    # hardened images), proceeding could wipe a RUNNING install, which
    # fails half-way on locked files. The early informational checks stay
    # warn-and-continue so a broken WMI does not block a plain install.
    $running = @()
    try {
        $procs = Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe' OR Name = 'uv.exe'"
        foreach ($p in $procs) {
            if (Test-CommandLineInAppDir -CommandLine $p.CommandLine) {
                $running += $p
            }
        }
    } catch {
        if ($RequireVerified) {
            Stop-Install "Could not check whether Wheelhouse is currently running ($($_.Exception.Message))." `
                "Close Wheelhouse if it is running (right-click the Wheelhouse tray icon and choose Exit), or restart this computer, then run the installer again."
        }
        Write-Warn "Could not check for a running Wheelhouse: $($_.Exception.Message)"
        return
    }
    if ($running.Count -gt 0) {
        Stop-Install "Wheelhouse appears to be running ($($running.Count) process(es) found)." `
            "Close Wheelhouse first: right-click the Wheelhouse tray icon and choose Exit, or say the exit voice command. Then run this installer again."
    }
}

# --- uv installation (step b) ---------------------------------------------------

function Find-UvExe {
    $cmd = Get-Command uv.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links\uv.exe"),
        (Join-Path $env:USERPROFILE ".local\bin\uv.exe"),
        (Join-Path $env:USERPROFILE ".cargo\bin\uv.exe")
    )
    foreach ($c in $candidates) {
        if (Test-Path -LiteralPath $c) { return $c }
    }
    return $null
}

function Test-DirOnPersistedPath {
    param([string]$Directory)
    # The app runs bare `uv` at every speech-provider start, so uv's
    # directory must be on the PERSISTED PATH -- not just this process's
    # environment. A child-process probe would false-pass (it inherits this
    # session's in-memory PATH), so read the registry values directly.
    $target = $Directory.TrimEnd('\')
    $values = @()
    try {
        $userPath = (Get-ItemProperty -LiteralPath "HKCU:\Environment" -Name Path -ErrorAction SilentlyContinue).Path
        if ($userPath) { $values += $userPath }
    } catch {}
    try {
        $machinePath = (Get-ItemProperty -LiteralPath "HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" -Name Path -ErrorAction SilentlyContinue).Path
        if ($machinePath) { $values += $machinePath }
    } catch {}
    foreach ($value in $values) {
        foreach ($entry in $value -split ';') {
            $expanded = [Environment]::ExpandEnvironmentVariables($entry).TrimEnd('\')
            if ($expanded -ieq $target) { return $true }
        }
    }
    return $false
}

function Send-EnvironmentChangeBroadcast {
    # $Advice is what the user is told when the broadcast fails. It comes from
    # the caller because this runs for more than one variable: once for the
    # PATH that carries uv, and again for the AI key. One fixed sentence for
    # both sent the AI-key user looking for a uv problem they did not have.
    param([string]$Advice)
    # A registry PATH write is invisible to already-running processes --
    # including Explorer, which every shortcut launch inherits its
    # environment from -- until a WM_SETTINGCHANGE "Environment" broadcast.
    # Without it, the app's speech-provider starts (bare `uv`) fail on a
    # machine where this installer first installed uv, until the user
    # signs out and back in. Best-effort: a failed broadcast must never
    # fail the install.
    try {
        if (-not ("WheelHouseInstaller.NativeMethods" -as [type])) {
            Add-Type -Namespace WheelHouseInstaller -Name NativeMethods -MemberDefinition @'
[DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Auto)]
public static extern IntPtr SendMessageTimeout(
    IntPtr hWnd, uint Msg, UIntPtr wParam, string lParam,
    uint fuFlags, uint uTimeout, out UIntPtr lpdwResult);
'@
        }
        $result = [UIntPtr]::Zero
        # HWND_BROADCAST (0xffff), WM_SETTINGCHANGE (0x1A),
        # SMTO_ABORTIFHUNG (2), 5-second per-window timeout.
        $sent = [WheelHouseInstaller.NativeMethods]::SendMessageTimeout(
            [IntPtr]0xffff, 0x1A, [UIntPtr]::Zero, "Environment",
            2, 5000, [ref]$result)
        # Windows reports a refused or timed-out broadcast by returning zero,
        # and a zero from a native call raises nothing, so discarding this
        # return value made every broadcast look like a success. The registry
        # write has already happened at this point: what did not happen is
        # telling the running programs, which is exactly what the caller's
        # advice is about. Raise so both ways of failing leave by one path.
        if ($sent.ToInt64() -eq 0) {
            throw "Windows did not deliver the environment-change broadcast."
        }
    } catch {
        # A caller that says nothing gets nothing shown, rather than a bullet
        # with no text after it; test_every_caller_of_the_broadcast_says_what_
        # it_was_announcing is what keeps every caller supplying one.
        if ($Advice) { Write-InstallNotice $Advice }
    }
}

function Add-DirToUserPath {
    param([string]$Directory)
    # Reads and writes the RAW registry value: [Environment]::
    # GetEnvironmentVariable returns REG_EXPAND_SZ values pre-expanded and
    # SetEnvironmentVariable writes REG_SZ, so the round trip would
    # permanently flatten every %VARIABLE% a user's PATH carries.
    $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey("Environment", $true)
    try {
        $raw = $key.GetValue(
            "Path", $null,
            [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        if ($null -eq $raw) {
            $raw = ""
            $kind = [Microsoft.Win32.RegistryValueKind]::ExpandString
        } else {
            $kind = $key.GetValueKind("Path")
        }
        $newValue = ($raw.TrimEnd(';') + ";" + $Directory).TrimStart(';')
        $key.SetValue("Path", $newValue, $kind)
    } finally {
        $key.Close()
    }
    Send-EnvironmentChangeBroadcast -Advice ("Could not tell running programs about the PATH change. " +
        "If Wheelhouse cannot find uv when it starts, sign out and back in once.")
}

function Set-UserEnvVar {
    param([string]$Name, [string]$Value)
    # Persist a User-scope environment variable (HKCU\Environment) and tell
    # running programs, so a shortcut launched from the current Explorer sees
    # it without a sign-out. Wrapped in its own function so the key-routing
    # logic (Set-AiApiKeyEnv) can be tested without mutating the real user
    # environment. A plain secret carries no %VARIABLE% to preserve, so the
    # REG_SZ that SetEnvironmentVariable writes is correct here (unlike PATH,
    # which must keep its REG_EXPAND_SZ kind).
    [Environment]::SetEnvironmentVariable($Name, $Value, "User")
    Send-EnvironmentChangeBroadcast -Advice ("Could not tell running programs that $Name changed. " +
        "If Wheelhouse does not see it, sign out and back in once.")
}

function Set-AiApiKeyEnv {
    param([string]$Key)
    # Route the cloud AI key to the environment, never config.toml (git-tracked
    # -- wh-ai-key-from-env). Persist it User-scope for future launches AND set
    # it in this process so the optional "start now" child launched at the end
    # of the install inherits it (a freshly-persisted User var is not yet in
    # this process's own environment block).
    Set-UserEnvVar -Name "WHEELHOUSE_AI_API_KEY" -Value $Key
    $env:WHEELHOUSE_AI_API_KEY = $Key
}

function Clear-AiApiKeyEnv {
    # Explicitly turning AI off removes the persisted cloud key (codex 2.3).
    # Leaving a stale secret in the user environment is a hygiene problem and a
    # latent cost/leak risk: a later accidental AI re-enable would silently reach
    # a paid endpoint with the old key. Passing "" to Set-UserEnvVar removes the
    # User-scope variable (.NET deletes a User/Machine var set to empty), reusing
    # the same wrapper Set-AiApiKeyEnv persists through so it is stub-testable.
    # Then clear this process's copy so an optional "start now" child does not
    # inherit it. Only EXPLICIT "off" clears the key; "keep" never touches it.
    Set-UserEnvVar -Name "WHEELHOUSE_AI_API_KEY" -Value ""
    Remove-Item Env:\WHEELHOUSE_AI_API_KEY -ErrorAction SilentlyContinue
}

function Resolve-AiApiKey {
    param([string]$AiApiKey)
    # Resolve the cloud key without exposing it on the installer's command line.
    # The graphical installer sets WHEELHOUSE_AI_API_KEY_INPUT on this child
    # process instead of passing -AiApiKey, because a command line is readable
    # via Win32_Process.CommandLine by any local process for the duration of the
    # install (a transient exposure the git-tracked-file rule does not cover).
    # An explicit -AiApiKey still wins for the developer / one-liner path. This
    # input var is distinct from the persisted WHEELHOUSE_AI_API_KEY that the
    # app reads at runtime.
    if ($AiApiKey) { return $AiApiKey }
    if ($env:WHEELHOUSE_AI_API_KEY_INPUT) { return $env:WHEELHOUSE_AI_API_KEY_INPUT }
    return ""
}

function Install-Uv {
    $uv = Find-UvExe
    if (-not $uv) {
        Write-Status "Installing uv (the Python environment manager Wheelhouse uses)..."
        $wingetOk = $false
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            $winget = Invoke-Native -Exe "winget" -Arguments @(
                "install", "--id=astral-sh.uv", "-e", "--silent",
                "--accept-source-agreements", "--accept-package-agreements")
            $wingetOk = ($winget.ExitCode -eq 0)
        }
        if (-not $wingetOk) {
            # Real path, not an edge case: winget is absent on Windows 10
            # LTSC, pre-21H2 Windows 10, and Store-disabled Enterprise images.
            # The official install script runs in a CHILD powershell: it ends
            # in `exit 1` on failure, which would terminate this process (and
            # under irm|iex the user's console) if run in-process via iex.
            Write-Status "winget was not available; using the official uv install script..."
            $fallback = Invoke-Native -Exe "powershell" -Arguments @(
                "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                "irm https://astral.sh/uv/install.ps1 | iex")
            if ($fallback.ExitCode -ne 0) {
                Stop-Install "Could not install uv (no winget, and the uv install script failed)." `
                    "Install uv manually from https://docs.astral.sh/uv/getting-started/installation/ then run this installer again."
            }
        }
        $uv = Find-UvExe
        if (-not $uv) {
            Stop-Install "uv was installed but its program could not be found." `
                "Install uv manually from https://docs.astral.sh/uv/getting-started/installation/ then run this installer again."
        }
    }
    Write-Status "uv found: $uv"

    # Persist uv's directory on the user PATH if the installer above did not.
    $uvDir = Split-Path $uv -Parent
    if (-not (Test-DirOnPersistedPath $uvDir)) {
        Write-Status "Adding uv to your PATH so Wheelhouse can start its speech engines..."
        Add-DirToUserPath -Directory $uvDir
        if (-not (Test-DirOnPersistedPath $uvDir)) {
            Stop-Install "Could not add uv's directory to the saved PATH ($uvDir)." `
                "Add it to your user PATH manually (Settings > System > About > Advanced system settings > Environment Variables), then run this installer again."
        }
    }
    # Make uv resolvable for the rest of THIS process too.
    if (-not ($env:Path -split ';' | Where-Object { $_.TrimEnd('\') -ieq $uvDir.TrimEnd('\') })) {
        $env:Path = "$env:Path;$uvDir"
    }
    return $uv
}

# --- Download with resume + SHA256 (steps c and e) ------------------------------

function Get-WebResponseStatusCode {
    param($ErrorRecord)
    # Returns the HTTP status code carried by a failed web request, or 0
    # when the failure has no response (timeouts, DNS, connection resets).
    # PowerShell wraps .NET method exceptions, so the WebException is often
    # one or two InnerExceptions deep -- walk the chain.
    $ex = $ErrorRecord.Exception
    while ($null -ne $ex) {
        if ($ex -is [System.Net.WebException] -and $null -ne $ex.Response) {
            $code = 0
            try { $code = [int]$ex.Response.StatusCode } catch { $code = 0 }
            return $code
        }
        $ex = $ex.InnerException
    }
    return 0
}

function Test-ResidueOwnerAlive {
    param([string]$Name)
    # Working files and directories carry the process id of the installer
    # run that created them (<name>-<pid>, plus an optional .meta suffix).
    # A LIVE owner means the item is a concurrent run's work in progress
    # -- possibly the machine's only complete copy mid-move -- so it must
    # never be treated as residue. A dead owner's leavings are removable.
    # This run's own id counts as removable: by the time a sweep runs,
    # this run is finished with its own working files. Process id reuse
    # can make a dead run's file survive one sweep (an unrelated live
    # process holds the number); a later sweep removes it.
    if ($Name -match '-(\d+)(\.meta)?$') {
        $owner = 0
        if ([int]::TryParse($Matches[1], [ref]$owner) -and $owner -ne $PID) {
            return ($null -ne (Get-Process -Id $owner -ErrorAction SilentlyContinue))
        }
    }
    return $false
}

function Remove-DownloadResidue {
    param([string]$Destination)
    # Best-effort sweep of one destination's download working files: dead
    # runs' per-run partials and their resume side files, the bare
    # .partial/.partial.meta names an older installer version used, and
    # moved-aside invalid copies. Files named with a live process id are
    # another run's download in progress and are skipped. A failure to
    # delete must not turn a finished download into a false failure: warn
    # and continue.
    $dir = Split-Path -Parent $Destination
    $leaf = Split-Path -Leaf $Destination
    # The .NET probe, not Test-Path: it returns false instead of throwing,
    # so an unreadable directory skips the sweep rather than turning a
    # finished download into a terminating error under the script-wide
    # ErrorActionPreference Stop.
    if (-not [System.IO.Directory]::Exists($dir)) { return }
    foreach ($pattern in @("$leaf.partial*", "$leaf.invalid-*")) {
        foreach ($item in @(Get-ChildItem -LiteralPath $dir -Filter $pattern -ErrorAction SilentlyContinue)) {
            if (Test-ResidueOwnerAlive -Name $item.Name) { continue }
            try {
                # -Recurse: a moved-aside occupier can be a folder.
                Remove-Item -LiteralPath $item.FullName -Recurse -Force -ErrorAction Stop
            } catch {
                Write-Warn "Could not remove a leftover download file ($($item.Name)); it will be removed on the next run."
            }
        }
    }
}

function Get-FileSha256IfReadable {
    param([string]$Path)
    # A hash probe that cannot abort the install: overlapping runs move
    # and replace these files, so the file can vanish or be locked between
    # a Test-Path and the hash read. $null means "could not be hashed",
    # and every caller treats that as "not verified" rather than a crash.
    try {
        return (Get-FileHash -LiteralPath $Path -Algorithm SHA256 -ErrorAction Stop).Hash
    } catch {
        return $null
    }
}

function Complete-VerifiedDownload {
    param(
        [string]$Partial,
        [string]$Meta,
        [string]$Destination,
        [string]$ExpectedSha256,
        [string]$Description
    )
    # Promotion of a verified per-run partial to the shared destination.
    # An exclusive rename, not Move-Item -Force: overlapping installer
    # runs share the destination, and a blind overwrite would race the
    # other run's own promotion. On a conflict, a destination that
    # verifies against the expected checksum is another run's finished
    # download -- accept it and discard this run's identical copy. An
    # occupier that does NOT verify (the invalid cached file left in
    # place by the entry check, a corrupt copy, or a FOLDER left at the
    # cache filename by filesystem damage or a manual repair) is moved
    # aside -- never deleted in place from a hash snapshot -- and the
    # promotion is retried once. The occupier is classified with the
    # .NET Exists probes (they return false instead of throwing, so an
    # unreadable path cannot abort the install here) and a folder is
    # moved with the directory move; the file-only move cannot move a
    # folder, and swallowing that failure made a folder occupier a
    # permanent trap: every rerun downloaded the asset and stopped again.
    $accepted = $false
    try {
        [System.IO.File]::Move($Partial, $Destination)
    } catch {
        $destHash = Get-FileSha256IfReadable -Path $Destination
        if ($destHash -and $destHash -ieq $ExpectedSha256) {
            $accepted = $true
        } elseif ([System.IO.File]::Exists($Destination) -or [System.IO.Directory]::Exists($Destination)) {
            $aside = "$Destination.invalid-$PID"
            Remove-Item -LiteralPath $aside -Recurse -Force -ErrorAction SilentlyContinue
            try {
                if ([System.IO.Directory]::Exists($Destination)) {
                    [System.IO.Directory]::Move($Destination, $aside)
                } else {
                    [System.IO.File]::Move($Destination, $aside)
                }
            } catch { }
            try {
                [System.IO.File]::Move($Partial, $Destination)
            } catch {
                $retryHash = Get-FileSha256IfReadable -Path $Destination
                if ($retryHash -and $retryHash -ieq $ExpectedSha256) {
                    $accepted = $true
                } else {
                    Stop-Install "Placing the downloaded $Description failed: $($_.Exception.Message)" `
                        "Run the installer again."
                }
            }
        } else {
            # The destination is free yet the rename failed (this run's
            # partial is locked or gone). Nothing to accept; stop with
            # the cause.
            Stop-Install "Placing the downloaded $Description failed: $($_.Exception.Message)" `
                "Run the installer again."
        }
    }
    if ($accepted) {
        Remove-Item -LiteralPath $Partial -Force -ErrorAction SilentlyContinue
        Write-Status "$Description was downloaded by another setup running at the same time."
    } else {
        Write-Status "$Description downloaded and verified."
    }
    # No existence probe before the removal: the download already
    # succeeded, and under the script-wide ErrorActionPreference Stop a
    # bare Test-Path that cannot read the path would be a terminating
    # error -- failing a finished download over its cleanup. Remove-Item
    # with SilentlyContinue is a no-op on a missing file.
    Remove-Item -LiteralPath $Meta -Force -ErrorAction SilentlyContinue
    Remove-DownloadResidue -Destination $Destination
}

function Invoke-VerifiedDownload {
    param(
        [string]$Url,
        [string]$Destination,
        [string]$ExpectedSha256,
        [string]$Description
    )
    # One mechanism on purpose: a .NET HttpWebRequest stream copy with a
    # Range header for resume, verified by Get-FileHash. Not BITS (disabled
    # by policy on hardened images; persistent jobs break on GitHub's
    # expiring redirect URLs) and not bare Invoke-WebRequest (PowerShell 5.1
    # buffers the whole response in memory).
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

    if (Test-Path -LiteralPath $Destination) {
        $existing = Get-FileSha256IfReadable -Path $Destination
        if ($existing -and $existing -ieq $ExpectedSha256) {
            Write-Status "$Description is already downloaded and verified."
            Remove-DownloadResidue -Destination $Destination
            return
        }
        # An invalid cached copy is NOT deleted here: deleting from this
        # hash snapshot would race a concurrent run promoting a verified
        # copy to the same path (the same stale-snapshot class as the
        # model-tree cleanup). Promotion replaces it with an exclusive
        # move once this run's own copy verifies.
        Write-Warn "The cached $Description does not match the expected checksum; downloading it again."
    }

    # Per-run working files: the process id in the names keeps overlapping
    # installer runs (the script takes no lock) from writing or deleting
    # each other's download in progress.
    $partial = "$Destination.partial-$PID"
    $meta = "$Destination.partial-$PID.meta"

    if (-not (Test-Path -LiteralPath $partial)) {
        # Adopt a partial left by a run that is no longer alive, so "run
        # the installer again -- it resumes where it left off" stays true
        # across runs. The exclusive rename is the claim: two live runs
        # cannot both adopt the same file, and the loser of the rename
        # simply starts fresh. The bare .partial name an older installer
        # version used has no process id and is treated as dead.
        $downloadDir = Split-Path -Parent $Destination
        $downloadLeaf = Split-Path -Leaf $Destination
        $orphans = @()
        if (Test-Path -LiteralPath $downloadDir) {
            $orphans = @(Get-ChildItem -LiteralPath $downloadDir -Filter "$downloadLeaf.partial*" -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -notlike "*.meta" -and -not (Test-ResidueOwnerAlive -Name $_.Name) })
        }
        foreach ($orphan in $orphans) {
            try { [System.IO.File]::Move($orphan.FullName, $partial) } catch { continue }
            $orphanMeta = "$($orphan.FullName).meta"
            if (Test-Path -LiteralPath $meta) {
                Remove-Item -LiteralPath $meta -Force -ErrorAction SilentlyContinue
            }
            if (Test-Path -LiteralPath $orphanMeta) {
                # Best-effort: without its meta the resume validators are
                # gone and Get-ResumeState starts the download fresh.
                try { [System.IO.File]::Move($orphanMeta, $meta) } catch { }
            }
            break
        }
    }

    $attempt = 0
    while ($true) {
        $attempt++
        try {
            Invoke-DownloadOnce -Url $Url -Partial $partial -Meta $meta -Description $Description
        } catch {
            # The partial and its validators are KEPT: both the in-run retry
            # and the next installer run resume from them. Deleting here
            # would make "it resumes where it left off" false for exactly
            # the flaky-network case resume exists for. (A corrupt partial
            # cannot loop forever: it surfaces as a hash mismatch below,
            # and that path does delete.)
            # The one exception is HTTP 416: the server rejected the saved
            # Range outright (the partial is longer than the asset, or the
            # validators are gone). That request fails BEFORE any bytes
            # arrive, so the hash-mismatch cleanup below can never run --
            # keeping the partial would repeat the same rejected request on
            # every attempt and every later run, forever. Delete both and
            # let the retry start from byte 0.
            $resumeNote = "retrying (resuming from the partial download)"
            if ((Get-WebResponseStatusCode -ErrorRecord $_) -eq 416) {
                Write-Warn "The server rejected resuming the partial download; starting $Description from the beginning."
                Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
                Remove-Item -LiteralPath $meta -Force -ErrorAction SilentlyContinue
                $resumeNote = "retrying from the beginning"
            }
            if ($attempt -ge 2) {
                Stop-Install "Downloading $Description failed twice: $($_.Exception.Message)" `
                    "Check your internet connection and run the installer again -- it resumes where it left off."
            }
            Write-Warn "Download failed ($($_.Exception.Message)); $resumeNote..."
            continue
        }

        $hash = Get-FileSha256IfReadable -Path $partial
        if ($null -eq $hash) {
            # This run's own partial cannot be read back (an antivirus
            # scan holding it open is the common cause). It is kept: the
            # rerun resumes or re-verifies it.
            Stop-Install "The downloaded $Description could not be read for verification." `
                "Close other programs (an antivirus scan can hold files open) and run the installer again -- it resumes where it left off."
        }
        if ($hash -ieq $ExpectedSha256) {
            Complete-VerifiedDownload -Partial $partial -Meta $meta -Destination $Destination `
                -ExpectedSha256 $ExpectedSha256 -Description $Description
            return
        }

        # Hash-mismatch recovery: DELETE the partial before retrying. A
        # persisted corrupt partial plus Range resume would otherwise be a
        # permanent failure loop that re-running the installer never clears.
        Remove-Item -LiteralPath $partial -Force
        if (Test-Path -LiteralPath $meta) { Remove-Item -LiteralPath $meta -Force }
        if ($attempt -ge 2) {
            Stop-Install "$Description failed its integrity check twice. The downloaded file does not match the expected checksum." `
                "This can mean a proxy or antivirus is altering downloads, or the release asset changed. Run the installer again later; if it keeps failing, file an issue."
        }
        Write-Warn "$Description failed its integrity check; downloading again from the start..."
    }
}

function Get-DownloadStartState {
    param([bool]$Append, [long]$ResumeFrom, [long]$ContentLength)
    # Progress baseline: only a true 206 append resume starts the byte
    # counter at the partial's size. A stale-resume 200 fallback truncates
    # the file, so counting from the old partial's size would push the
    # percentage past 100 -- and Write-Progress throws on
    # PercentComplete > 100 under ErrorActionPreference Stop, which would
    # kill exactly the fallback download that recovers a stale partial.
    # Chunked responses (ContentLength -1) get TotalBytes 0, which
    # disables progress reporting entirely.
    if ($ContentLength -lt 0) {
        $total = 0
    } elseif ($Append) {
        $total = $ContentLength + $ResumeFrom
    } else {
        $total = $ContentLength
    }
    $written = 0
    if ($Append) { $written = $ResumeFrom }
    return @{ Written = $written; TotalBytes = $total }
}

function Get-ResumeState {
    param([string]$Partial, [string]$Meta)
    # The resume validators live in a small JSON side file. If that file is
    # corrupt (crash mid-write, disk hiccup), parsing it would throw on
    # every run -- a permanent failure loop that re-running never clears --
    # so discard both files and start the download fresh. The read uses
    # ReadAllText because PowerShell 5.1's ConvertFrom-Json cannot parse
    # multi-line JSON piped line-by-line from Get-Content, and
    # ConvertTo-Json writes multi-line JSON.
    if ((Test-Path -LiteralPath $Partial) -and (Test-Path -LiteralPath $Meta)) {
        $saved = $null
        try {
            $saved = [System.IO.File]::ReadAllText($Meta) | ConvertFrom-Json
        } catch { $saved = $null }
        if ($null -ne $saved) {
            return @{ ResumeFrom = (Get-Item -LiteralPath $Partial).Length; SavedMeta = $saved }
        }
        Write-Warn "The saved download-resume information is unreadable; starting this download from the beginning."
        Remove-Item -LiteralPath $Partial -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $Meta -Force -ErrorAction SilentlyContinue
    } elseif (Test-Path -LiteralPath $Partial) {
        Remove-Item -LiteralPath $Partial -Force
    } elseif (Test-Path -LiteralPath $Meta) {
        Remove-Item -LiteralPath $Meta -Force
    }
    return @{ ResumeFrom = 0; SavedMeta = $null }
}

function Invoke-DownloadOnce {
    param([string]$Url, [string]$Partial, [string]$Meta, [string]$Description)

    $state = Get-ResumeState -Partial $Partial -Meta $Meta
    $resumeFrom = $state.ResumeFrom
    $savedMeta = $state.SavedMeta

    $request = [System.Net.HttpWebRequest]::Create($Url)
    $request.UserAgent = "wheelhouse-installer/$AppVersion"
    $request.AllowAutoRedirect = $true
    $request.Timeout = 30000
    $request.ReadWriteTimeout = 30000
    if ($resumeFrom -gt 0) {
        $request.AddRange([long]$resumeFrom)
        # Resume freshness: GitHub release assets can be replaced in place at
        # the same URL. If-Range makes a stale resume come back as a full 200
        # instead of splicing bytes from a different file.
        if ($savedMeta -and $savedMeta.ETag) {
            $request.Headers.Add("If-Range", $savedMeta.ETag)
        } elseif ($savedMeta -and $savedMeta.LastModified) {
            $request.Headers.Add("If-Range", $savedMeta.LastModified)
        }
    }

    $response = $request.GetResponse()
    try {
        $status = [int]$response.StatusCode
        $append = $false
        if ($resumeFrom -gt 0 -and $status -eq 206) {
            $append = $true
            Write-Status "Resuming $Description from $([math]::Round($resumeFrom / 1MB)) MB..."
        } elseif ($resumeFrom -gt 0) {
            # Server sent the full file (asset changed or no range support):
            # start over rather than splice.
            Write-Warn "The partial download is stale; starting $Description from the beginning."
        }

        # Record validators for a future resume.
        $newMeta = @{
            ETag = $response.Headers["ETag"]
            LastModified = $response.Headers["Last-Modified"]
            ContentLength = $response.ContentLength
        }
        $newMeta | ConvertTo-Json | Set-Content -LiteralPath $Meta -Encoding ASCII

        $startState = Get-DownloadStartState -Append $append -ResumeFrom $resumeFrom -ContentLength $response.ContentLength
        $totalBytes = $startState.TotalBytes

        $inStream = $response.GetResponseStream()
        $mode = "Create"
        if ($append) { $mode = "Append" }
        $outStream = New-Object System.IO.FileStream($Partial, [System.IO.FileMode]::$mode)
        try {
            $buffer = New-Object byte[] (1MB)
            $written = $startState.Written
            $lastReport = 0
            while ($true) {
                $read = $inStream.Read($buffer, 0, $buffer.Length)
                if ($read -le 0) { break }
                $outStream.Write($buffer, 0, $read)
                $written += $read
                if ($totalBytes -gt 0 -and ($written - $lastReport) -gt 20MB) {
                    $pct = [math]::Min(100, [math]::Round(($written / $totalBytes) * 100))
                    Write-Progress -Activity "Downloading $Description" -Status "$pct% ($([math]::Round($written / 1MB)) MB of $([math]::Round($totalBytes / 1MB)) MB)" -PercentComplete $pct
                    $lastReport = $written
                }
            }
        } finally {
            $outStream.Dispose()
            $inStream.Dispose()
        }
        Write-Progress -Activity "Downloading $Description" -Completed
    } finally {
        $response.Close()
    }
}

# --- App archive extraction with preserve list (step c + update semantics) ------

function Initialize-UpdateStaging {
    param([string]$StagingDir, [string]$MarkerPath)
    # Staging is only trustworthy when the wipe marker proves a previous
    # run entered its destructive phase -- staging then holds the ONLY
    # copy of the user's files (the crash-survivor case). Marker-less
    # staging comes from a run that stopped BEFORE its wipe, most commonly
    # the running-app refusal: the app dir is still the live copy, and the
    # staged snapshots go stale while the user keeps working. Restoring
    # those later would silently roll the user's files back, so discard
    # them and re-stage from the live tree.
    if ((Test-Path -LiteralPath $StagingDir) -and -not (Test-Path -LiteralPath $MarkerPath)) {
        Remove-Item -LiteralPath $StagingDir -Recurse -Force
    }
    if ((Test-Path -LiteralPath $MarkerPath) -and -not (Test-Path -LiteralPath $StagingDir)) {
        Remove-Item -LiteralPath $MarkerPath -Force
    }
}

function Save-PreservedFiles {
    param([string]$StagingDir, [switch]$OverwriteFromLive)
    $saved = @()
    foreach ($rel in $PreservePaths) {
        $dst = Join-Path $StagingDir $rel
        $src = Join-Path $AppDir $rel
        if ((Test-Path -LiteralPath $dst) -and -not ($OverwriteFromLive -and (Test-Path -LiteralPath $src))) {
            # Already in staging, and the caller either wants
            # skip-if-staged semantics or the live file is gone.
            # Update flow (no switch): a crash survivor from an
            # interrupted update -- the app dir was re-created from the
            # fresh archive before that run died, so the live copy is a
            # shipped starter file and overwriting the staged copy with
            # it would destroy the user's data the crashed run saved.
            # Keep-uninstall (-OverwriteFromLive, live file missing): an
            # earlier removal deleted the file from the live tree, so
            # the staged copy is the user's only copy.
            $saved += $rel
            continue
        }
        if (Test-Path -LiteralPath $src) {
            New-Item -ItemType Directory -Force -Path (Split-Path $dst -Parent) | Out-Null
            Copy-Item -LiteralPath $src -Destination $dst -Force
            $saved += $rel
        }
    }
    return $saved
}

function Restore-PreservedFiles {
    param([string]$StagingDir)
    # Restores whatever the staging directory HOLDS, not what this run
    # remembers saving: a crash between the wipe and the restore leaves the
    # user's files only in staging, and the next run must recover them even
    # though its own save step (no app dir) found nothing to save. Returns
    # the number of files restored.
    if (-not (Test-Path -LiteralPath $StagingDir)) { return 0 }
    $files = @(Get-ChildItem -LiteralPath $StagingDir -Recurse -File)
    $stagingRoot = (Get-Item -LiteralPath $StagingDir).FullName.TrimEnd('\')
    foreach ($f in $files) {
        $rel = $f.FullName.Substring($stagingRoot.Length + 1)
        $dst = Join-Path $AppDir $rel
        New-Item -ItemType Directory -Force -Path (Split-Path $dst -Parent) | Out-Null
        Copy-Item -LiteralPath $f.FullName -Destination $dst -Force
    }
    return $files.Count
}

function Install-AppArchive {
    param([string]$Url, [string]$Sha256)

    New-Item -ItemType Directory -Force -Path $DownloadsDir | Out-Null
    $zipPath = Join-Path $DownloadsDir "wheelhouse-$AppVersion.zip"
    Invoke-VerifiedDownload -Url $Url -Destination $zipPath -ExpectedSha256 $Sha256 -Description "the Wheelhouse application"

    $staging = Join-Path $LocalRoot "update-preserve"
    # The marker lives NEXT TO staging, not inside it, so the restore
    # (which copies everything staging holds) never plants it in the app.
    $marker = "$staging.wipe-started"
    if (Test-Path -LiteralPath $AppDir) {
        Write-Status "Updating the existing install (your settings and voice patterns are preserved)..."
        Initialize-UpdateStaging -StagingDir $staging -MarkerPath $marker
        New-Item -ItemType Directory -Force -Path $staging | Out-Null
        Save-PreservedFiles -StagingDir $staging | Out-Null
        # Re-check right before the destructive step: the earlier preflight
        # check ran minutes ago (downloads in between), and wiping a running
        # install fails half-way through on locked files. Verified or stop:
        # an unverifiable check must not proceed to the wipe.
        Test-RunningWheelHouse -RequireVerified
        # The marker records that the destructive phase is beginning: from
        # here until the restore completes, staging holds the only copy of
        # the user's files, and the next run must keep it (crash survivor)
        # rather than discard it as stale. Written only AFTER the running
        # re-check, so a refusal cannot strand fresh staging as a false
        # crash survivor.
        New-Item -ItemType File -Force -Path $marker | Out-Null
        Remove-Item -LiteralPath $AppDir -Recurse -Force
    }

    Write-Status "Unpacking the application..."
    New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
    Expand-Archive -LiteralPath $zipPath -DestinationPath $AppDir -Force

    # If the archive wraps everything in a single top-level directory,
    # flatten it so $AppDir is the repository root.
    if (-not (Test-Path -LiteralPath (Join-Path $AppDir "VERSION"))) {
        $children = @(Get-ChildItem -LiteralPath $AppDir)
        if ($children.Count -eq 1 -and $children[0].PSIsContainer -and (Test-Path -LiteralPath (Join-Path $children[0].FullName "VERSION"))) {
            Get-ChildItem -LiteralPath $children[0].FullName -Force | Move-Item -Destination $AppDir
            Remove-Item -LiteralPath $children[0].FullName -Recurse -Force
        }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $AppDir "VERSION"))) {
        Stop-Install "The unpacked application archive does not look like Wheelhouse (no VERSION file)." `
            "If you overrode the archive URL, check it points at a Wheelhouse release archive."
    }

    # Restore runs off staging CONTENTS so a leftover staging dir from a
    # crashed earlier update is recovered here instead of deleted below.
    $restored = Restore-PreservedFiles -StagingDir $staging
    if ($restored -gt 0) {
        Write-Status "Restored $restored preserved file(s) from the previous install."
    }
    if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
    if (Test-Path -LiteralPath $marker) { Remove-Item -LiteralPath $marker -Force }
}

# --- Environment syncs (step d) ---------------------------------------------------

function Invoke-UvSync {
    param([string]$Uv, [string]$ServiceRelPath, [switch]$Fatal)
    $serviceDir = Join-Path $AppDir $ServiceRelPath
    Write-Status "Setting up $ServiceRelPath..."
    # Every filesystem step below -- the path probe, the push, the native
    # call -- follows the same fatal/non-fatal contract. The probe lives
    # INSIDE the try: under the script-wide ErrorActionPreference Stop, an
    # access-denied or filesystem error from Test-Path itself is a
    # terminating error, and outside the try it would abort the whole
    # install even for the optional components. -PathType Container:
    # plain Test-Path is also true for a FILE left at the path. The probe
    # is still only a snapshot: the folder can vanish (antivirus
    # quarantine, manual deletion) between the check and the push, which
    # the same catch handles.
    $stackDepth = (Get-Location -Stack).Count
    try {
        if (-not (Test-Path -LiteralPath $serviceDir -PathType Container)) {
            if ($Fatal) {
                Stop-Install "Setting up $ServiceRelPath failed: that path in the unpacked application is missing or is not a folder." `
                    "Run the installer again (it re-downloads and re-unpacks the application). An antivirus quarantining files can also cause this."
            }
            Write-InstallNotice "Setting up $ServiceRelPath failed (the path is missing or is not a folder); this optional component will be disabled."
            return $false
        }
        Push-Location -LiteralPath $serviceDir
        $result = Invoke-Native -Exe $Uv -Arguments @("sync", "--locked", "--no-dev")
        $code = $result.ExitCode
    } catch {
        if ($_.ToString() -eq "WHEELHOUSE-INSTALL-STOP") { throw }
        if ($Fatal) {
            Stop-Install "Setting up $ServiceRelPath failed: $($_.Exception.Message)" `
                "Run the installer again (it re-downloads and re-unpacks the application). An antivirus quarantining files can also cause this."
        }
        Write-InstallNotice "Setting up $ServiceRelPath failed ($($_.Exception.Message)); this optional component will be disabled."
        return $false
    } finally {
        # 5.1 pushes the stack entry BEFORE setting the location, so a
        # FAILED push still leaves an orphaned entry behind (probe-verified
        # on 5.1.26100). Restore to the depth this call found rather than
        # guessing whether the push "happened": that pops exactly what this
        # call added -- one entry after a successful push, the orphaned
        # entry after a failed one, nothing if the failure preceded the
        # push -- and never touches an entry the caller owns.
        while ((Get-Location -Stack).Count -gt $stackDepth) { Pop-Location }
    }
    if ($code -ne 0) {
        # Show the tail of uv's output so a failed install is diagnosable
        # from the console (and from an issue report). These are raw native
        # diagnostics that routinely name paths under the user's profile,
        # and they do not pass through the sanitizing writers -- hide the
        # profile here.
        $tail = @($result.Output | Select-Object -Last 5)
        foreach ($line in $tail) { Write-Host "    uv: $(Hide-UserProfilePath -Text $line)" }
        if ($Fatal) {
            Stop-Install "Setting up $ServiceRelPath failed (uv sync exit code $code)." `
                "Check your internet connection and run the installer again. Corporate proxies can also block package downloads."
        }
        Write-InstallNotice "Setting up $ServiceRelPath failed (exit code $code); this optional component will be disabled."
        return $false
    }
    return $true
}

# --- Hardware snapshot for the STT offer (step e) ---------------------------------

function Get-HardwareSnapshot {
    param([string]$Uv)
    # ONE syscheck run, answering two questions: whether to offer the CUDA
    # speech engine, and where -- or whether -- the local AI model runs. Each
    # run is a `uv run` that pays for interpreter startup and an environment
    # check; a second one would spend several seconds of an install on data
    # already in hand. syscheck's venv has its own dependencies that nothing
    # else installs.
    #
    # Returns the parsed snapshot, or $null when it could not be produced. Both
    # callers degrade on $null rather than failing the install: no CUDA offer,
    # and no local AI model. The RAM/CPU floor already ran natively in the
    # preflights, so nothing fatal depends on this.
    $serviceDir = Join-Path $AppDir "services\syscheck"
    $stackDepth = (Get-Location -Stack).Count
    try {
        Push-Location -LiteralPath $serviceDir
        $result = Invoke-Native -Exe $Uv -Arguments @(
            "run", "python", "syscheck.py", "--compact")
        if ($result.ExitCode -ne 0) { return $null }
        # The merged stream carries uv's stderr progress as error records;
        # the JSON is the plain-string stdout lines.
        $json = ($result.Output | Where-Object { $_ -is [string] }) -join "`n"
        if (-not $json) { return $null }
        return ($json | ConvertFrom-Json)
    } catch {
        Write-InstallNotice "Hardware detection failed ($($_.Exception.Message)); offering CPU and cloud speech engines only."
        return $null
    } finally {
        # Same rule as Invoke-UvSync: 5.1 pushes the stack entry BEFORE
        # setting the location, so a FAILED push still leaves an orphan --
        # and under `irm | iex` that orphan outlives the installer in the
        # user's own session. Restore to the depth this call found.
        while ((Get-Location -Stack).Count -gt $stackDepth) { Pop-Location }
    }
}

function Test-CudaCapable {
    param($Snapshot)
    # A capable NVIDIA card is what makes the Distil-Whisper offer honest.
    # No snapshot means no offer.
    if ($null -eq $Snapshot) { return $false }
    foreach ($gpu in $Snapshot.gpu) {
        if ($gpu.software) { continue }
        if ($gpu.vendor_id -eq $NvidiaVendorId -and $gpu.dedicated_vram_bytes -ge $CudaMinVramBytes) {
            return $true
        }
    }
    return $false
}

function Get-LocalAiDecision {
    param($Snapshot)
    # The decision is MADE in services/syscheck/recommendations.py and carried
    # in the snapshot. This reads it; it does not decide anything. A second
    # copy of the thresholds here is the failure where syscheck tells a user
    # their machine is fine and the installer refuses it -- so there are no
    # numbers in this function, on purpose.
    #
    # A snapshot that could not be produced, or one from an older syscheck that
    # predates the local_ai key, answers "no local model" with a sentence that
    # says which of those happened.
    if ($null -eq $Snapshot -or $null -eq $Snapshot.local_ai) {
        return [pscustomobject]@{
            install = $false
            model = $null
            gpu_layers = $null
            reason = "The hardware check did not run on this machine, so the local AI model was not installed."
        }
    }
    return $Snapshot.local_ai
}

function Get-CurrentProvider {
    # The engine an existing (restored) config already uses, or "" on a
    # fresh install. Keeps an update's Enter-through-the-prompt from
    # silently switching a user's engine.
    $config = Join-Path $AppDir "services\wheelhouse\config.toml"
    if (Test-Path -LiteralPath $config) {
        $content = [System.IO.File]::ReadAllText($config)
        # Both TOML string quotings: the write path normalizes to double
        # quotes, but a user-edited config may legally use single quotes,
        # and missing it would silently default an update back to Parakeet.
        if ($content -match '(?m)^last_provider\b\s*=\s*"([^"]+)"') {
            return $Matches[1]
        }
        if ($content -match "(?m)^last_provider\b\s*=\s*'([^']+)'") {
            return $Matches[1]
        }
    }
    return ""
}

function Select-SttProvider {
    param([bool]$CudaCapable, [string]$CurrentProvider = "", [string]$SttProvider = "")
    $names = @{
        "parakeet_tdt" = "Parakeet"
        "google_stt" = "Google Cloud"
        "distil_medium_en" = "Distil-Whisper"
    }
    # Non-interactive path: the graphical installer supplies the engine
    # directly, so the speech-engine question is skipped entirely. An unknown
    # value is a caller bug and stops rather than installing a nonexistent
    # engine; Distil-Whisper asked for without a capable NVIDIA card falls
    # back to Parakeet, mirroring the interactive path that never offers it
    # there.
    if ($SttProvider) {
        if (-not $names.ContainsKey($SttProvider)) {
            throw "Unknown speech engine '$SttProvider'. Valid values: parakeet_tdt, google_stt, distil_medium_en."
        }
        if ($SttProvider -eq "distil_medium_en" -and -not $CudaCapable) {
            Write-InstallNotice "Distil-Whisper needs an NVIDIA graphics card, which was not detected; using Parakeet instead."
            return "parakeet_tdt"
        }
        return $SttProvider
    }
    # On an update, pressing Enter keeps the engine the user already has --
    # as long as it is still offerable on this hardware.
    $default = "parakeet_tdt"
    if ($CurrentProvider -eq "google_stt") { $default = "google_stt" }
    if ($CurrentProvider -eq "distil_medium_en" -and $CudaCapable) {
        $default = "distil_medium_en"
    }

    # Honesty over silence: if the engine the user already runs is no
    # longer offerable on this hardware, say so before the prompt instead
    # of letting Enter quietly switch it.
    if ($CurrentProvider -and $names.ContainsKey($CurrentProvider) -and $default -ne $CurrentProvider) {
        Write-InstallNotice "Your current speech engine ($($names[$CurrentProvider])) is not available on this computer (it needs an NVIDIA graphics card, which was not detected), so the default has changed to $($names[$default])."
    }

    Write-Host ""
    Write-Host "Which speech engine would you like?" -ForegroundColor Cyan
    Write-Host "  1. Parakeet (recommended) - runs on this computer, no account needed, works offline"
    Write-Host "  2. Google Cloud - runs in the cloud; needs a Google Cloud account and internet (advanced)"
    if ($CudaCapable) {
        Write-Host "  3. Distil-Whisper - runs on your NVIDIA graphics card (advanced)"
    }
    if ($default -ne "parakeet_tdt" -or $CurrentProvider -eq "parakeet_tdt") {
        $prompt = "Enter a number and press Enter (default: keep your current engine, $($names[$default]))"
    } else {
        $prompt = "Enter a number and press Enter (default: 1)"
    }
    $answer = Read-Host $prompt
    if ($answer -eq "1") { return "parakeet_tdt" }
    if ($answer -eq "2") { return "google_stt" }
    if ($CudaCapable -and $answer -eq "3") { return "distil_medium_en" }
    return $default
}

# --- Model download + extraction (step e) -------------------------------------------

function Get-ModelFileState {
    param([string]$Path)
    # A required model entry counts only as a regular file with at least one
    # byte. Existence alone is not proof: opening an archive member creates
    # its filename before any bytes are written, so a disk-full or killed
    # extraction can leave a required name present and empty, and antivirus
    # can do the same after the fact. Three answers, because 'the entry is
    # proven unusable' ('bad': absent, empty, or a directory) and 'the
    # entry could not be READ' ('unreadable': access denied, a filter
    # driver holding it, a transient I/O failure) must not collapse into
    # one value -- a read refusal is not evidence the model is incomplete,
    # and treating it as one let a complete model be deleted as residue.
    try {
        $item = Get-Item -LiteralPath $Path -ErrorAction Stop
    } catch [System.Management.Automation.ItemNotFoundException] {
        return 'bad'
    } catch {
        return 'unreadable'
    }
    if ($item.PSIsContainer -or $item.Length -eq 0) { return 'bad' }
    return 'ok'
}

function Get-ModelState {
    param([string]$ModelDir)
    # Mirrors sherpa_engine.py's required-file check: tokens.txt plus the
    # encoder/decoder/joiner trio in either int8 or full-precision naming.
    # tokens.txt alone is NOT proof of a finished extraction -- an
    # interrupted extraction can leave it behind without the much larger
    # ONNX files, and the app then fails at startup with nothing to repair
    # it. Returns 'complete', 'incomplete' (every gap is a PROVEN bad
    # entry), or 'unreadable' (at least one required entry could not be
    # read, so incompleteness is not proven). Only 'incomplete' ever
    # authorizes deleting a tree.
    $sawUnreadable = $false
    $tokens = Get-ModelFileState -Path (Join-Path $ModelDir "tokens.txt")
    if ($tokens -eq 'unreadable') { $sawUnreadable = $true }
    if ($tokens -eq 'ok') {
        foreach ($suffix in @(".int8.onnx", ".onnx")) {
            $allPresent = $true
            foreach ($part in @("encoder", "decoder", "joiner")) {
                $state = Get-ModelFileState -Path (Join-Path $ModelDir "$part$suffix")
                if ($state -eq 'unreadable') { $sawUnreadable = $true }
                if ($state -ne 'ok') {
                    $allPresent = $false
                    break
                }
            }
            if ($allPresent) { return 'complete' }
        }
    }
    if ($sawUnreadable) { return 'unreadable' }
    return 'incomplete'
}

function Test-ModelComplete {
    param([string]$ModelDir)
    # The yes/no view for the accept-winner checks: only a PROVEN complete
    # model is accepted, and neither 'incomplete' nor 'unreadable' leads
    # those callers to delete anything.
    return ((Get-ModelState -ModelDir $ModelDir) -eq 'complete')
}

function Expand-ModelArchive {
    param([string]$Python, [string]$Archive, [string]$Destination)
    # NOT tar.exe: Windows 10's bundled bsdtar (libarchive 3.3.2) is built
    # with zlib only -- no bz2lib -- so it cannot decompress this .tar.bz2
    # and exits 1 instantly on an otherwise healthy machine (observed on
    # build 19045; Windows 11's build does link bz2lib, which is why the
    # failure never showed in testing). The app venv's Python is guaranteed
    # by the earlier fatal uv sync, and its tarfile module decompresses
    # bzip2 on every supported Windows build. filter="data" refuses
    # absolute paths and parent-directory escapes inside the archive.
    # Through Invoke-Native like every other native call: anything the
    # child writes to stderr is a landmine under $ErrorActionPreference =
    # 'Stop' whenever the process stderr is redirected.
    # Single-quoted Python strings on purpose: PowerShell 5.1 does not
    # escape double quotes inside a native-process argument, so a " in
    # this code would be stripped in transit and break the Python syntax.
    $code = @'
import sys, tarfile
with tarfile.open(sys.argv[1], 'r:bz2') as tf:
    tf.extractall(sys.argv[2], filter='data')
'@
    Invoke-Native -Exe $Python -Arguments @("-c", $code, $Archive, $Destination)
}

function Remove-ModelResidue {
    # Best-effort cleanup of extraction leftovers: every working directory
    # under the models directory and the cached archive. Called after a
    # successful promotion AND on the already-installed path, so an
    # interruption between promotion and cleanup cannot strand the 650 MB
    # archive forever -- the next run retries. A failure here (a file held
    # open by antivirus or indexing) must not turn a correct install into a
    # false failure: warn and continue.
    # Trees and files named with a LIVE process id are another run's work
    # in progress -- a working tree mid-extraction, or a moved-aside tree
    # that may be the machine's only complete model mid-restore -- and
    # must never be swept. Dead owners' leavings are removed as before.
    # Both directory probes use the .NET call, not Test-Path: it returns
    # false instead of throwing, so an unreadable directory skips the
    # sweep rather than turning a correct install into a terminating
    # error under the script-wide ErrorActionPreference Stop.
    if ([System.IO.Directory]::Exists($ModelsDir)) {
        foreach ($item in @(Get-ChildItem -LiteralPath $ModelsDir -Filter "$ModelDirName.extracting*" -ErrorAction SilentlyContinue)) {
            if (Test-ResidueOwnerAlive -Name $item.Name) { continue }
            try {
                Remove-Item -LiteralPath $item.FullName -Recurse -Force -ErrorAction Stop
            } catch {
                Write-Warn "Could not remove a leftover working folder ($($item.Name)); it will be removed on the next run."
            }
        }
    }
    # The archive glob also catches the per-run download working files
    # (<archive>.partial-<pid>, .invalid-<pid>) a crashed run left behind.
    if ([System.IO.Directory]::Exists($DownloadsDir)) {
        foreach ($item in @(Get-ChildItem -LiteralPath $DownloadsDir -Filter "$ModelDirName.tar.bz2*" -ErrorAction SilentlyContinue)) {
            if (Test-ResidueOwnerAlive -Name $item.Name) { continue }
            try {
                # -Recurse: a moved-aside occupier can be a folder.
                Remove-Item -LiteralPath $item.FullName -Recurse -Force -ErrorAction Stop
            } catch {
                Write-Warn "Could not remove a leftover download file ($($item.Name)); it will be removed on the next run."
            }
        }
    }
}

function Install-ParakeetModel {
    $python = Join-Path $AppDir "services\wheelhouse\.venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $python)) {
        # Unreachable in a normal run (the services\wheelhouse sync is fatal
        # and runs first); antivirus quarantine or a hand-deleted venv gets
        # a clear stop instead of a confusing extraction error.
        Stop-Install "The application environment needed to unpack the speech model is missing." `
            "Run the installer again (it repairs the application environment)."
    }
    $extractedDir = Join-Path $ModelsDir $ModelDirName
    $modelState = Get-ModelState -ModelDir $extractedDir
    if ($modelState -eq 'complete') {
        Write-Status "The speech model is already installed."
        Remove-ModelResidue
        return
    }
    if ($modelState -eq 'unreadable') {
        # A required entry could not be READ. That is not proof the model
        # is incomplete, and everything below this gate treats the tree as
        # replaceable -- acting on a read refusal could delete a complete
        # model. Leave the tree exactly as it is.
        Stop-Install "A file in the existing speech model could not be read, so the model cannot be verified." `
            "Close other programs (an antivirus scan can hold files open) and run the installer again."
    }
    if (Test-Path -LiteralPath $extractedDir) {
        # A tree the completeness check rejects: an old partial extraction
        # or damage after the fact. It must go so the promoted fresh tree
        # cannot mix old and new files -- but deleting it in place would
        # act on a stale snapshot: a concurrent installer run can promote
        # a COMPLETE model to this exact path between the check above and
        # the delete, and the delete would then destroy the other run's
        # finished install. Move the observed tree aside with an exclusive
        # rename and re-verify the exact tree that was moved; only a tree
        # this run owns and has re-checked is ever deleted. The aside name
        # matches the residue sweep's pattern, so a leftover cannot strand.
        $staleDir = Join-Path $ModelsDir "$ModelDirName.extracting-stale-$PID"
        if (Test-Path -LiteralPath $staleDir) {
            Remove-Item -LiteralPath $staleDir -Recurse -Force
        }
        $movedAside = $false
        try {
            [System.IO.Directory]::Move($extractedDir, $staleDir)
            $movedAside = $true
        } catch {
            if (Test-ModelComplete -ModelDir $extractedDir) {
                # The rename lost to a concurrent run that promoted a
                # complete model here. The machine has what it needs.
                Write-Status "The speech model was installed by another setup running at the same time."
                Remove-ModelResidue
                return
            }
            if (Test-Path -LiteralPath $extractedDir) {
                # Still present, still incomplete, and it cannot be moved
                # (a file inside is held open). Extracting fresh would end
                # at a promotion this same lock defeats; stop with the
                # cause instead.
                Stop-Install "An incomplete speech model from an earlier run could not be moved aside: $($_.Exception.Message)" `
                    "Close other programs (an antivirus scan can hold the folder open) and run the installer again."
            }
            # The tree vanished (a concurrent run removed it): the path is
            # free, continue with a fresh install.
        }
        if ($movedAside) {
            $staleState = Get-ModelState -ModelDir $staleDir
            if ($staleState -ne 'incomplete') {
                # 'complete': the completeness check above was a stale
                # snapshot -- the tree this run moved aside is a complete
                # model a concurrent run promoted after that check.
                # 'unreadable': incompleteness is not proven, so the tree
                # must not be deleted. Either way, put it back.
                try {
                    [System.IO.Directory]::Move($staleDir, $extractedDir)
                } catch {
                    if (Test-ModelComplete -ModelDir $extractedDir) {
                        # Yet another run promoted a complete model while
                        # the restore was in flight; the moved-aside tree
                        # is redundant residue and is swept below.
                        Write-Status "The speech model was installed by another setup running at the same time."
                        Remove-ModelResidue
                        return
                    }
                    # The tree is stranded at the aside name. The next
                    # run re-downloads, and its residue sweep removes the
                    # stranded tree once its owner is gone.
                    Stop-Install "Putting the speech model back in place failed: $($_.Exception.Message)" `
                        "Run the installer again."
                }
                if ($staleState -eq 'complete') {
                    Write-Status "The speech model was installed by another setup running at the same time."
                    Remove-ModelResidue
                    return
                }
                Stop-Install "A file in the existing speech model could not be read, so the model cannot be verified." `
                    "Close other programs (an antivirus scan can hold files open) and run the installer again."
            }
            Write-Warn "An incomplete speech model was found from an earlier run; reinstalling it."
            try {
                Remove-Item -LiteralPath $staleDir -Recurse -Force -ErrorAction Stop
            } catch {
                # Best-effort: the tree is out of the final path, so the
                # install can proceed; any run's residue sweep retries.
                Write-Warn "Could not remove a leftover working folder ($ModelDirName.extracting-stale-$PID); it will be removed on the next run."
            }
        }
    }
    New-Item -ItemType Directory -Force -Path $ModelsDir | Out-Null
    New-Item -ItemType Directory -Force -Path $DownloadsDir | Out-Null
    $archive = Join-Path $DownloadsDir "$ModelDirName.tar.bz2"
    Write-Status "Downloading the speech model (about 650 MB; this is the longest step)..."
    Invoke-VerifiedDownload -Url $ModelUrl -Destination $archive -ExpectedSha256 $ModelSha256 -Description "the speech model"

    Write-Status "Unpacking the speech model..."
    # Extract into a per-run working directory and promote only a verified
    # tree, so the final directory exists only when a completed extraction
    # put it there. Extracting straight into $ModelsDir would leave a
    # partial tree at the exact path the entry gate above reads on the next
    # run, and a disk-full or killed extraction can leave every required
    # filename present there with the last one incomplete. The process id
    # in the name keeps two overlapping installer runs (the script takes no
    # lock) out of each other's working trees.
    $stagingRoot = Join-Path $ModelsDir "$ModelDirName.extracting-$PID"
    if (Test-Path -LiteralPath $stagingRoot) {
        Remove-Item -LiteralPath $stagingRoot -Recurse -Force
    }
    $extract = Expand-ModelArchive -Python $python -Archive $archive -Destination $stagingRoot
    $stagedDir = Join-Path $stagingRoot $ModelDirName
    if ($extract.ExitCode -ne 0 -or -not (Test-ModelComplete -ModelDir $stagedDir)) {
        if (Test-ModelComplete -ModelDir $extractedDir) {
            # Another installer running at the same time finished the model
            # while this extraction failed (it may even have removed this
            # run's working tree as residue). The machine has what it needs.
            Write-Status "The speech model was installed by another setup running at the same time."
            Remove-ModelResidue
            return
        }
        # The extractor's own last lines go into the message: the shipped
        # 'tar exit code 1' failure left the setup log with no diagnosis.
        Stop-Install "Unpacking the speech model failed (exit code $($extract.ExitCode)): $((@($extract.Output) | Select-Object -Last 3) -join ' / ')" `
            "Run the installer again; the download itself is kept and will not repeat."
    }
    # A rename that refuses an existing destination. Move-Item would treat a
    # directory that reappeared here (a second installer run, or restore
    # software) as a container and silently nest the verified tree inside
    # it, then report success with no usable model at the required paths.
    try {
        [System.IO.Directory]::Move($stagedDir, $extractedDir)
    } catch {
        if (Test-ModelComplete -ModelDir $extractedDir) {
            # Another writer promoted a complete model first; this run's
            # tree is residue and is swept below.
            Write-Status "The speech model was installed by another setup running at the same time."
            Remove-ModelResidue
            return
        }
        Stop-Install "Placing the unpacked speech model failed: $($_.Exception.Message)" `
            "Run the installer again; the download itself is kept and will not repeat."
    }
    Remove-ModelResidue
    Write-Status "Speech model installed."
}

# --- Config writing (step f) ----------------------------------------------------------

function Set-TomlProviderDisabled {
    param([string]$ConfigPath)
    # Sets enabled = false inside the [provider] section. Providers whose
    # environments were not set up must be undiscoverable: discovery treats
    # an absent-or-true enabled key as available, and launch cannot repair a
    # missing venv -- the provider would appear in the menu and then fail.
    if (-not (Test-Path -LiteralPath $ConfigPath)) { return }
    # .NET read: Get-Content without -Encoding decodes BOM-less UTF-8 as
    # ANSI on PowerShell 5.1, corrupting any non-ASCII byte it touches.
    # Preserve the file's existing line-ending style: the shipped provider
    # configs are LF (.gitattributes eol=lf), and writing them back through
    # the default CRLF terminator would flip the whole file to CRLF on the
    # common default install. Same detection Write-UserConfig uses.
    $raw = [System.IO.File]::ReadAllText($ConfigPath)
    $newLine = if ($raw -match "`r`n") { "`r`n" } else { "`n" }
    $lines = [System.IO.File]::ReadAllLines($ConfigPath)
    $out = New-Object System.Collections.Generic.List[string]
    $inProvider = $false
    $replaced = $false
    foreach ($line in $lines) {
        if ($line -match '^\s*\[\s*("provider"|''provider''|provider)\s*\]\s*(#.*)?$') {
            $inProvider = $true
            $out.Add($line)
            continue
        }
        if ($inProvider -and $line -match '^\s*\[') {
            if (-not $replaced) { $out.Insert($out.Count, "enabled = false") }
            $inProvider = $false
            $replaced = $true
        }
        if ($inProvider -and $line -match '^\s*enabled\s*=') {
            $out.Add("enabled = false")
            $replaced = $true
            continue
        }
        $out.Add($line)
    }
    if ($inProvider -and -not $replaced) { $out.Add("enabled = false") }
    Write-TomlFile -Path $ConfigPath -Lines $out.ToArray() -NewLine $newLine
}

function Write-UserConfig {
    param([string]$Provider)
    $wheelhouseDir = Join-Path $AppDir "services\wheelhouse"
    $example = Join-Path $wheelhouseDir "config.toml.example"
    $config = Join-Path $wheelhouseDir "config.toml"
    if (-not (Test-Path -LiteralPath $config)) {
        Copy-Item -LiteralPath $example -Destination $config
    }
    # Point the app at the chosen provider, preserving everything else in an
    # existing (restored) config. .NET read: Get-Content -Raw without
    # -Encoding decodes BOM-less UTF-8 as ANSI and corrupts non-ASCII text
    # in a preserved user config.
    $content = [System.IO.File]::ReadAllText($config)
    $newLine = if ($content -match "`r`n") { "`r`n" } else { "`n" }
    $keyLine = "last_provider = `"$Provider`""
    if ($content -match '(?m)^last_provider\b\s*=') {
        # [^\r\n]* not .*$ -- in .NET regex (?m)$ matches before \n and .*
        # consumes a preceding \r, so .*$ would rewrite a CRLF-ended line
        # with a bare LF and leave the config with mixed line endings.
        $content = $content -replace '(?m)^last_provider\b\s*=[^\r\n]*', $keyLine
    } else {
        # A preserved config can lack the key entirely (older app version,
        # hand-edit). A bare -replace would silently write nothing while
        # this function claims success, and the app would start whichever
        # provider it discovers first instead of the chosen one. Insert
        # the key under the [stt] header, or append the section if the
        # file has none. [ \t] not \s inside the header pattern: \s also
        # matches line endings and would let the match run past the line.
        $sttHeader = New-Object System.Text.RegularExpressions.Regex `
            '(?m)^(\[[ \t]*("stt"|''stt''|stt)[ \t]*\][ \t]*(#[^\r\n]*)?)'
        if ($sttHeader.IsMatch($content)) {
            $content = $sttHeader.Replace($content, "`$1$newLine$keyLine", 1)
        } else {
            if ($content.Length -gt 0 -and -not $content.EndsWith("`n")) {
                $content += $newLine
            }
            $content += "[stt]$newLine$keyLine$newLine"
        }
    }
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($config, $content, $encoding)
    Write-Status "Configuration written (speech engine: $Provider)."
}

function Set-TomlSectionValues {
    param(
        [string]$ConfigPath,
        [string]$Section,
        $Updates
    )
    # Update-or-insert each key of $Updates inside the named TOML section,
    # section-scoped and BOM-less and line-ending preserving (same approach as
    # Set-TomlProviderDisabled). $Updates maps a key to its RAW TOML value text
    # -- the caller encodes it (a quoted basic string via ConvertTo-
    # TomlBasicString, or a bare true/false). The section header is matched
    # exactly on its dotted name, so -Section "ai" never matches [ai.server] or
    # [ai.help], and -Section "ai.server" never matches [ai] or [ai.help].
    if (-not (Test-Path -LiteralPath $ConfigPath)) { return }

    # Build the header pattern from the literal section name. Each dotted part is
    # regex-escaped (a literal name matches a literal name), then the parts are
    # joined with a whitespace-tolerant dot: TOML allows optional whitespace
    # around the dot in a table header, so [ai . server] is the SAME table as
    # [ai.server] and must update in place, not append a duplicate table (which
    # tomllib then refuses to load -- defining a table twice is invalid TOML).
    $escapedParts = ($Section -split '\.') | ForEach-Object { [regex]::Escape($_) }
    $namePattern = $escapedParts -join '\s*\.\s*'
    $headerPattern = "^\s*\[\s*$namePattern\s*\]\s*(#.*)?$"

    # .NET read: Get-Content without -Encoding decodes BOM-less UTF-8 as ANSI on
    # PowerShell 5.1 and corrupts any non-ASCII byte. Preserve the file's
    # existing line-ending style (the shipped config.toml.example is LF).
    $raw = [System.IO.File]::ReadAllText($ConfigPath)
    $newLine = if ($raw -match "`r`n") { "`r`n" } else { "`n" }
    $lines = [System.IO.File]::ReadAllLines($ConfigPath)

    $out = New-Object System.Collections.Generic.List[string]
    $inSection = $false
    $sectionSeen = $false
    $written = @{}
    foreach ($k in $Updates.Keys) { $written[$k] = $false }

    foreach ($line in $lines) {
        # The target header. Matched before the generic "any header ends the
        # section" check below so entering is not mistaken for leaving.
        if ($line -match $headerPattern) {
            $inSection = $true
            $sectionSeen = $true
            $out.Add($line)
            continue
        }
        # Any other section header closes the target section: flush not-yet-
        # written keys before leaving so an inserted key lands inside it.
        if ($inSection -and $line -match '^\s*\[') {
            foreach ($k in $Updates.Keys) {
                if (-not $written[$k]) {
                    $out.Add("$k = $($Updates[$k])")
                    $written[$k] = $true
                }
            }
            $inSection = $false
        }
        if ($inSection) {
            # \s*= (not just the key) so "model" never matches a "model_path"
            # line; same anchoring the last_provider rewrite uses.
            $matched = $false
            foreach ($k in $Updates.Keys) {
                if ($line -match "^\s*$k\s*=") {
                    $out.Add("$k = $($Updates[$k])")
                    $written[$k] = $true
                    $matched = $true
                    break
                }
            }
            if ($matched) { continue }
        }
        $out.Add($line)
    }
    # The target was the file's last section: flush remaining keys at EOF.
    if ($inSection) {
        foreach ($k in $Updates.Keys) {
            if (-not $written[$k]) { $out.Add("$k = $($Updates[$k])") }
        }
    }
    # The section is absent (a very old preserved config): append it so an
    # intended write is applied rather than silently lost.
    if (-not $sectionSeen) {
        if ($out.Count -gt 0 -and $out[$out.Count - 1] -ne "") { $out.Add("") }
        $out.Add("[$Section]")
        foreach ($k in $Updates.Keys) { $out.Add("$k = $($Updates[$k])") }
    }
    Write-TomlFile -Path $ConfigPath -Lines $out.ToArray() -NewLine $newLine
}

function Set-AiServerConfig {
    param(
        [string]$ConfigPath,
        [string]$BaseUrl,
        [string]$Model,
        [string]$Kind
    )
    # Writes the [ai.server] section from the installer's AI choice. base_url
    # (empty = AI off, the documented switch at ai/service.py _ai_off) and kind
    # are always set; model only when the caller passes it (the "off" path
    # leaves the shipped model alone -- an empty base_url already disables AI).
    # NEVER writes api_key: the cloud key goes to the environment, never this
    # git-tracked file (wh-ai-key-from-env). Delegates the section surgery to
    # Set-TomlSectionValues; the values are TOML-escaped so a wizard override of
    # base_url/model containing a quote or backslash cannot corrupt the config.
    $updates = [ordered]@{ base_url = (ConvertTo-TomlBasicString $BaseUrl) }
    if ($PSBoundParameters.ContainsKey('Model')) {
        $updates['model'] = (ConvertTo-TomlBasicString $Model)
    }
    $updates['kind'] = (ConvertTo-TomlBasicString $Kind)
    Set-TomlSectionValues -ConfigPath $ConfigPath -Section "ai.server" -Updates $updates
}

function Set-AiRuntimeConfig {
    param(
        [string]$ConfigPath,
        [string]$ModelPath,
        [string]$BinaryDir,
        [int]$GpuLayers
    )
    # Turns on the Wheelhouse-managed model server and tells it where the two
    # pieces are. context_size and startup_timeout_seconds are left alone: the
    # shipped defaults are right for this model, and a value the installer
    # writes is a value a user's later edit has to fight.
    #
    # GpuLayers is typed [int] and written unquoted. runtime_config.py
    # type-checks the key and disables the WHOLE local runtime on a bad type,
    # so a quoted 99 would turn the feature off with nothing but a log line to
    # say why.
    #
    # Writes no key of any kind. The local model needs no account.
    $updates = [ordered]@{
        enabled = "true"
        model_path = (ConvertTo-TomlBasicString $ModelPath)
        binary_dir = (ConvertTo-TomlBasicString $BinaryDir)
        gpu_layers = "$GpuLayers"
    }
    Set-TomlSectionValues -ConfigPath $ConfigPath -Section "ai.runtime" -Updates $updates
}

function Disable-AiRuntimeConfig {
    param([string]$ConfigPath)
    # The refusal path, and the path every non-local mode takes. Writing
    # enabled = false explicitly matters on a RE-RUN: a machine that had the
    # local model and is being switched to cloud or off must stop starting its
    # own server, and leaving the key at its previous true would keep it doing
    # so behind whatever address the new mode wrote.
    Set-TomlSectionValues -ConfigPath $ConfigPath -Section "ai.runtime" `
        -Updates ([ordered]@{ enabled = "false" })
}

function Get-LocalAiRuntimeMarkerName {
    # A function rather than a script variable so that the two callers below
    # cannot disagree about the name, including when they are loaded on their
    # own -- which is how the tests exercise them.
    "wheelhouse-runtime.json"
}

function Write-LocalAiRuntimeMarker {
    # Records what a complete extraction produced: which pinned archive it came
    # from, and every file it wrote. Called only after Expand-Archive has
    # finished, so a marker existing is itself the evidence that extraction
    # completed. Written before the directory is put in its final place.
    param(
        [Parameter(Mandatory = $true)][string]$Directory,
        [Parameter(Mandatory = $true)][string]$Sha256
    )
    $markerPath = Join-Path $Directory (Get-LocalAiRuntimeMarkerName)
    # The marker is written into the same directory, so it must not record
    # itself: on a later check the file list would never be satisfied.
    # Each file is recorded with its length as well as its name, because a
    # name is the one thing damage leaves behind: antivirus quarantines the
    # contents and keeps the entry, and an extraction interrupted mid-file
    # leaves it short. Length is not a checksum and is not meant to be -- the
    # archive is verified by checksum on the way in, and this record answers
    # the different question of whether everything it produced is still there.
    $files = Get-ChildItem -LiteralPath $Directory -Recurse -File |
        Where-Object { $_.FullName -ne $markerPath } |
        ForEach-Object {
            [ordered]@{
                path  = $_.FullName.Substring($Directory.Length).TrimStart('\', '/')
                bytes = $_.Length
            }
        }
    $marker = [ordered]@{
        archive_sha256 = $Sha256
        files          = @($files)
    }
    $json = $marker | ConvertTo-Json -Depth 3
    [System.IO.File]::WriteAllText($markerPath, $json, (New-Object System.Text.UTF8Encoding $false))
}

function Get-LocalAiRuntimeMarker {
    # The marker of a COMPLETE extraction in $Directory, or $null.
    #
    # The runtime is a directory of about 93 MB -- llama-server.exe plus the
    # libraries it loads -- so the executable being present says nothing about
    # the rest. An extraction interrupted after the first file, a library
    # removed by antivirus, or a previous pinned build all leave an executable
    # in place, and all of them produce a server that cannot start. So does a
    # file that is present but damaged, which is why each recorded file is
    # checked for being a file and for still being the length it was written
    # at, rather than merely for something existing at that path.
    #
    # Complete says nothing about WHICH build this is. That is a separate
    # question, and only one of the two callers asks it: the swap below has to
    # ask whether a directory is a whole build without caring which one, since
    # the build it is comparing against is by then the one being replaced.
    #
    # This answers; it does not fail. The whole body is guarded, because the
    # script makes every error terminating and both callers are worse off with
    # an exception than with a no. The repair caller would abandon the install
    # instead of re-extracting, and the swap caller asks this before its own
    # try block, so an exception carries the run past the branch that keeps
    # the last complete build. Anything that stops completeness being
    # established is correctly "not complete": re-extracting costs one 33 MB
    # download, and that is the cheaper mistake in both directions.
    #
    # But "no" then covers two different situations, and one caller has to
    # tell them apart. Every plain no below is something this run observed --
    # no marker, an entry the writer never wrote, a directory where a file
    # belongs, a file that is not the length it was written at. A no from the
    # catch is the opposite: nothing was observed, because the disk would not
    # answer. -Unreadable reports which of the two happened, for the caller
    # that deletes a directory on the strength of the answer. The distinction
    # is where the no came from rather than what kind of error was raised, so
    # it does not depend on classifying exceptions.
    param(
        [Parameter(Mandatory = $true)][string]$Directory,
        [ref]$Unreadable
    )
    try {
        if ($null -ne $Unreadable) { $Unreadable.Value = $false }
        if (-not (Test-Path -LiteralPath $Directory)) { return $null }
        $markerPath = Join-Path $Directory (Get-LocalAiRuntimeMarkerName)
        if (-not (Test-Path -LiteralPath $markerPath)) { return $null }

        # Unreadable or not JSON lands in the catch below, as does anything
        # the filesystem refuses while the files are being checked.
        $marker = Get-Content -Raw -LiteralPath $markerPath | ConvertFrom-Json
        if (-not $marker.files) { return $null }

        foreach ($entry in $marker.files) {
            # An entry this installer's own writer did not produce -- a marker
            # from an older format, or one whose write was cut short -- says
            # nothing about the file it names, so it cannot be counted as
            # satisfied. bytes is compared against $null rather than tested
            # for truth, because a legitimately empty file records a length of
            # zero. The path half of this is belt and braces: Join-Path
            # accepts a null child and hands back $Directory itself, which
            # then fails the file check below, so every marker shape tried
            # reaches the same answer either way. It is kept because that
            # answer arrives by a chain of coincidences, and the requirement
            # is worth stating outright.
            if (-not $entry.path -or $null -eq $entry.bytes) { return $null }
            # One observation, not two. Asking whether the path exists and
            # then reading the item are separate looks at the disk, and
            # antivirus can take the file away in between -- which is how the
            # exception above got in. Fetching the item once and asking it
            # both questions removes that gap as well as the exception.
            $item = Get-Item -LiteralPath (Join-Path $Directory $entry.path)
            # A directory of the right name is not the file. The extraction
            # can leave one, and so can a later archive that moves a library
            # into a folder named after it. The length check does not cover
            # this: PowerShell reports a Length of 1 for any object that has
            # none, directories included, so a one-byte file replaced by a
            # directory passes it.
            if ($item -isnot [System.IO.FileInfo]) { return $null }
            if ($item.Length -ne $entry.bytes) { return $null }
        }
        return $marker
    } catch {
        if ($null -ne $Unreadable) { $Unreadable.Value = $true }
        return $null
    }
}

function Test-LocalAiRuntimeInstalled {
    # True only for a complete extraction of the archive we currently pin.
    param(
        [Parameter(Mandatory = $true)][string]$Directory,
        [Parameter(Mandatory = $true)][string]$ExpectedSha256
    )
    $marker = Get-LocalAiRuntimeMarker -Directory $Directory
    if ($null -eq $marker) { return $false }
    return $marker.archive_sha256 -eq $ExpectedSha256
}

function Remove-LocalAiRuntimeResidue {
    # Removes the three directories the replacement below can leave beside the
    # installed runtime.
    #
    # Only safe where a whole build of the pinned archive has just been shown
    # to be at $Directory. Until that is known, .previous or .unreadable may be
    # the only working build on the machine, which is what the choice inside
    # Move-DirectoryIntoPlace exists to protect.
    #
    # A failure is reported and not thrown. The build is installed and verified
    # by the time this runs, so a directory that will not delete is untidy
    # rather than harmful, and throwing would end the install past the point
    # where the runtime is on disk -- the caller's catch only warns, so the
    # configuration pointing at the runtime would never be written, and a
    # locked directory would turn into no AI features at all.
    #
    # The existence check is inside the try with the removal, not before it.
    # Whatever holds a directory open holds it against the question "is it
    # there?" as firmly as against the delete, so guarding only the delete
    # would move the abort one line rather than remove it.
    param([Parameter(Mandatory = $true)][string]$Directory)
    foreach ($name in @("previous", "unreadable", "incoming")) {
        $residue = "$Directory.$name"
        try {
            if (Test-Path -LiteralPath $residue) { Remove-Item -LiteralPath $residue -Recurse -Force }
        } catch {
            Write-Warn ("The leftover AI runtime at $residue could not " +
                "be removed. It is not in use and is safe to delete by hand. " +
                "This installer tries again each time it runs.")
        }
    }
}

function Move-DirectoryIntoPlace {
    param([string]$Staging, [string]$Final)
    # Replaces $Final with $Staging so that $Final holds one complete
    # directory or the other at every moment, never neither.
    #
    # Deleting $Final first and moving $Staging second is the obvious order
    # and the wrong one: when the move fails the user is left with nothing
    # installed, past a 33 MB download, and with the build that worked this
    # morning gone. A failure here is not exotic either -- antivirus holds a
    # freshly written executable open, and llama-server.exe is exactly the
    # kind of file it holds longest.
    $backup = "$Final.previous"
    # A second holding place, used only on the run that can read neither of
    # the two directories it would otherwise choose between. It is emptied by
    # the last line of this function, so it survives no longer than it takes
    # to install a build that makes both of the old ones residue.
    $held = "$Final.unreadable"
    $movedAside = $false

    # An existing copy is one of two very different things, and the difference
    # decides whether it may be deleted. When the installed build is whole,
    # the copy is residue from a run that lost power between the two moves,
    # and it has to go or the rename below has nowhere to land. When the
    # installed build is missing or partial, the copy is what a failed restore
    # left -- the message that failure prints tells the user to rely on it --
    # and it is then the only whole build on the machine. Deleting it there,
    # or letting the partial build overwrite it, is what leaves a user who
    # reran the installer with no runtime at all rather than an old one.
    #
    # There is a third state, and it decides which of the two survives: this
    # run could not read the installed build at all. A locked file is not
    # evidence that a build is partial, and this is the one place where
    # treating it as such destroys something -- a file lock here would delete
    # a build that works in favour of a leftover that may never have been one.
    # A lock is also not a far-fetched thing to hit at this moment, since it
    # is the same antivirus behaviour the rest of this function exists to
    # survive. So when the installed build cannot be read, ask about the copy
    # instead, and keep whichever of the two is more likely to run: the copy
    # if it can be shown whole, and otherwise the installed build, which is at
    # least the one the machine has been running.
    #
    # And there is a fourth: the copy cannot be read either. Whatever locked
    # one of these directories is as likely to have locked the other, since
    # they sit side by side and hold the same files. Nothing is then known
    # about either one, so every choice between them is a guess, and either
    # guess destroys the only whole build whenever it guesses wrong. Both are
    # kept instead. The rename below needs $backup free to land in, so the
    # copy moves one step further out of the way and waits there.
    if (Test-Path -LiteralPath $backup) {
        $installedUnreadable = $false
        $installedIsWhole = $null -ne (Get-LocalAiRuntimeMarker `
            -Directory $Final -Unreadable ([ref]$installedUnreadable))

        if (-not $installedUnreadable) {
            $keep = if ($installedIsWhole) { 'installed' } else { 'copy' }
        } else {
            $copyUnreadable = $false
            $copyIsWhole = $null -ne (Get-LocalAiRuntimeMarker `
                -Directory $backup -Unreadable ([ref]$copyUnreadable))
            if (-not $copyUnreadable) {
                $keep = if ($copyIsWhole) { 'copy' } else { 'installed' }
            } elseif (Test-Path -LiteralPath $Final) {
                $keep = 'both'
            } else {
                # Nothing to weigh the copy against, so it is not a guess.
                $keep = 'copy'
            }
        }

        if ($keep -eq 'installed') {
            Remove-Item -LiteralPath $backup -Recurse -Force
        } elseif ($keep -eq 'copy') {
            if (Test-Path -LiteralPath $Final) { Remove-Item -LiteralPath $Final -Recurse -Force }
            # Already where a restore expects to find it.
            $movedAside = $true
        } elseif (Test-Path -LiteralPath $held) {
            # A third directory, from an earlier run that kept both and then
            # could not put the build it moved aside back. Three is one more
            # than this function has places for, and none of them can be
            # thrown away on what this run has been able to read.
            throw ("The AI runtime could not be replaced. $Final, $backup " +
                "and $held are all present, and this run could not read " +
                "the first two, so none of them can be discarded safely. " +
                "Delete the ones you do not want and run this installer " +
                "again.")
        } else {
            Move-Item -LiteralPath $backup -Destination $held
        }
    }

    if (-not $movedAside -and (Test-Path -LiteralPath $Final)) {
        Move-Item -LiteralPath $Final -Destination $backup
        $movedAside = $true
    }

    try {
        Move-Item -LiteralPath $Staging -Destination $Final
    } catch {
        $failure = $_
        if ($movedAside) {
            try {
                # A move that failed part-way leaves some of the new build at
                # the destination, and that has to go before the old one can
                # come back.
                if (Test-Path -LiteralPath $Final) { Remove-Item -LiteralPath $Final -Recurse -Force }
                Move-Item -LiteralPath $backup -Destination $Final
            } catch {
                Write-Warn ("The previous AI runtime could not be put " +
                    "back. It is at $backup; rename it to $Final to restore " +
                    "it, or rerun this installer.")
            }
        }
        throw $failure
    }

    # The new build is installed and was verified on the way in, so the copy
    # and any build kept because an earlier run could not read it have nothing
    # left to be kept for. This runs whether or not this run was the one that
    # made them, which is what stops them accumulating across runs.
    Remove-LocalAiRuntimeResidue -Directory $Final
}

function Install-LocalAiRuntime {
    param([int]$GpuLayers)
    # Puts the two pieces on disk and returns the model file's full path.
    # Downloads go through Invoke-VerifiedDownload like every other download in
    # this script: hash-checked, resumable, and re-runnable -- which matters
    # more here than anywhere else, because the model is 5.15 GB and a user on
    # a domestic connection may well be interrupted.
    New-Item -ItemType Directory -Force -Path $AiModelsDir | Out-Null
    New-Item -ItemType Directory -Force -Path $DownloadsDir | Out-Null

    $modelPath = Join-Path $AiModelsDir $LocalAiModelFileName

    if (-not (Test-LocalAiRuntimeInstalled -Directory $LlamaDir `
              -ExpectedSha256 $LlamaServerSha256)) {
        $archive = Join-Path $DownloadsDir "llama-server-vulkan.zip"
        Write-Status "Downloading the AI runtime (about 33 MB)..."
        Invoke-VerifiedDownload -Url $LlamaServerUrl -Destination $archive `
            -ExpectedSha256 $LlamaServerSha256 -Description "the AI runtime"

        # Unpack beside the destination, not into it. An interruption then
        # leaves the staging directory half-filled and the installed one
        # untouched, so a rerun still sees the state it saw before rather
        # than a directory that is neither the old build nor the new one.
        $staging = "$LlamaDir.incoming"
        if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
        New-Item -ItemType Directory -Force -Path $staging | Out-Null
        Write-Status "Unpacking the AI runtime..."
        Expand-Archive -LiteralPath $archive -DestinationPath $staging -Force
        if (-not (Test-Path -LiteralPath (Join-Path $staging "llama-server.exe"))) {
            Remove-Item -LiteralPath $staging -Recurse -Force
            throw "The AI runtime archive did not contain llama-server.exe."
        }
        # Only now, with every file written: the marker is what a later run
        # reads as proof that an extraction finished.
        Write-LocalAiRuntimeMarker -Directory $staging -Sha256 $LlamaServerSha256

        Move-DirectoryIntoPlace -Staging $staging -Final $LlamaDir
        Remove-Item -LiteralPath $archive -Force
    } else {
        Write-Status "The AI runtime is already installed."
        # The only other place these are removed is the end of
        # Move-DirectoryIntoPlace, which this branch does not reach. Without
        # this call a leftover directory beside a current build would never be
        # removed by any later run: each one would come back here and stop.
        # A run interrupted between moving the copy aside and moving the build
        # out of the way ends in exactly that state, and so does a run whose
        # own removal of a copy failed.
        Remove-LocalAiRuntimeResidue -Directory $LlamaDir
    }

    # 5.15 GB, so the size is stated before the wait rather than after it.
    Write-Status "Downloading the AI model (about 5.2 GB; this is a large download)..."
    Invoke-VerifiedDownload -Url $LocalAiModelUrl -Destination $modelPath `
        -ExpectedSha256 $LocalAiModelSha256 -Description "the AI model"

    return $modelPath
}

function Write-ModelOverrideFile {
    # The per-machine model-path channel: the provider resolves
    # [parakeet_tdt].model_path from this file ahead of its tracked config.
    $modelPath = (Join-Path $ModelsDir $ModelDirName) -replace '\\', '/'
    $lines = @(
        "# Written by install-wheelhouse.ps1. Per-machine speech-model paths;",
        "# safe to edit. Sections are keyed by provider name.",
        "",
        "[parakeet_tdt]",
        "model_path = `"$modelPath`""
    )
    Write-TomlFile -Path $OverrideFile -Lines $lines
}

# --- Shortcuts (step g) -------------------------------------------------------------------

function New-AppShortcut {
    param([string]$LnkPath)
    $uv = Find-UvExe
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($LnkPath)
    $workDir = Join-Path $AppDir "services\wheelhouse"
    $shortcut.TargetPath = $uv
    # --locked --no-sync: the same runtime fences the app's own provider
    # launcher uses. Bare `uv run` reconciles the environment at launch --
    # it pulls the dev group the install excluded, touches the network,
    # and can fail offline before the app starts.
    # --directory (rather than relying on WorkingDirectory alone): a
    # working directory is invisible in a process's command line, and the
    # uv.exe parent stays alive for the app's whole lifetime -- without
    # the app path in its arguments, the running-app check cannot see a
    # just-launched Wheelhouse until the Python child exists.
    $shortcut.Arguments = "run --directory `"$workDir`" --locked --no-sync python launcher.py"
    $shortcut.WorkingDirectory = $workDir
    $icon = Join-Path $AppDir "services\wheelhouse\WheelHouse.ico"
    if (Test-Path -LiteralPath $icon) { $shortcut.IconLocation = $icon }
    $shortcut.Description = "Wheelhouse voice control"
    $shortcut.Save()
}

function Remove-AllShortcuts {
    foreach ($folder in @("Programs", "Desktop", "Startup")) {
        $lnk = Join-Path ([Environment]::GetFolderPath($folder)) $ShortcutName
        if (Test-Path -LiteralPath $lnk) { Remove-Item -LiteralPath $lnk -Force }
    }
}

# --- Uninstall -------------------------------------------------------------------------------

function Invoke-Uninstall {
    Write-Host "Wheelhouse uninstaller" -ForegroundColor Cyan
    if (-not (Test-Path -LiteralPath $LocalRoot) -and -not (Test-Path -LiteralPath $RoamingRoot)) {
        Write-Status "Wheelhouse does not appear to be installed for this user. Removing any leftover shortcuts."
        Remove-AllShortcuts
        return
    }
    Test-RunningWheelHouse

    # -Force skips this confirmation. -Force:$false (or omitting -Force) still
    # asks, so an accidental switch value cannot silently green-light removal.
    $confirm = Resolve-YesNoChoice -Specified $Force.IsPresent -Value $true `
        -Prompt "Remove Wheelhouse from this computer? Type yes to continue"
    if (-not $confirm) {
        Write-Status "Nothing was removed."
        return
    }

    # -KeepData keeps data; -Force alone removes it; neither asks. Specified is
    # true when either switch is present, and the value is whether -KeepData
    # was the one present.
    $keep = Resolve-YesNoChoice -Specified ($KeepData.IsPresent -or $Force.IsPresent) `
        -Value $KeepData.IsPresent `
        -Prompt "Keep your personal data (settings, voice patterns, downloaded speech model)? Type yes to keep it"

    # Re-check right before the destructive step -- the first check ran
    # before the prompts, and the app can have been started since.
    # Verified or stop: an unverifiable check must not proceed to removal.
    Test-RunningWheelHouse -RequireVerified

    if ($keep) {
        # Relocate the preserved user files out of the app tree, then remove
        # the app. Models and the override file already live outside it.
        $keepDir = Join-Path $LocalRoot "preserved-user-data"
        # A keep-directory left by an EARLIER uninstall holds that day's
        # copies. Merge per file rather than clearing wholesale: a file
        # present in the live tree is the newer, authoritative copy and
        # overwrites the old kept one (-OverwriteFromLive), while a file
        # MISSING from the live tree keeps its old copy -- after an
        # earlier keep-uninstall crashed mid-removal (app dir gone, or
        # present but partially emptied by a locked-file failure), the
        # kept copy can be the user's only copy.
        New-Item -ItemType Directory -Force -Path $keepDir | Out-Null
        $saved = @(Save-PreservedFiles -StagingDir $keepDir -OverwriteFromLive)
        if (Test-Path -LiteralPath $AppDir) { Remove-Item -LiteralPath $AppDir -Recurse -Force }
        if (Test-Path -LiteralPath $DownloadsDir) { Remove-Item -LiteralPath $DownloadsDir -Recurse -Force }
        Write-Status "Your personal data was kept at: $keepDir (and your speech model at $ModelsDir)."
        Write-Host "    Re-running the installer later will offer a fresh install; copy files back from there if you want them."
    } else {
        if (Test-Path -LiteralPath $LocalRoot) { Remove-Item -LiteralPath $LocalRoot -Recurse -Force }
    }

    # The persisted cloud AI key (WHEELHOUSE_AI_API_KEY) is an app credential,
    # not user data. On a FULL removal, clear it too so a paid-endpoint key does
    # not outlive the app in the user environment (glm52 3.1). On -KeepData we
    # deliberately keep it: -KeepData preserves config.toml, which can enable
    # cloud AI, and a reinstall would then have an enabled cloud config with no
    # key (the keyless-cloud 401 deepseek 1.1 guards against). Guard the clear so
    # a failure to remove the variable cannot abort a completed removal.
    if (-not $keep) {
        try { Clear-AiApiKeyEnv }
        catch { Write-InstallNotice "Could not clear the saved AI key from your environment: $($_.Exception.Message). You can remove WHEELHOUSE_AI_API_KEY manually in Windows Environment Variables." }
    }

    # The roaming root holds provider PID and port files, never user data. A
    # stale PID file surviving into a reinstall could point cleanup at an
    # innocent process that now owns the recycled PID.
    if (Test-Path -LiteralPath $RoamingRoot) { Remove-Item -LiteralPath $RoamingRoot -Recurse -Force }
    Remove-AllShortcuts
    Write-Status "Wheelhouse has been removed."
    Write-Host "    If anything is left behind, the folders to check are:"
    Write-Host "      $LocalRoot"
    Write-Host "      $RoamingRoot"
}

# --- Main -------------------------------------------------------------------------------------

function Invoke-MainInstall {
    Write-Host ""
    Write-Host "Wheelhouse $AppVersion installer" -ForegroundColor Cyan
    Write-Host "Voice control for your Windows PC. This takes about 10-20 minutes,"
    Write-Host "most of it downloading. You can re-run this installer any time to"
    Write-Host "repair or update an install; your settings are preserved."
    Write-Host ""

    Test-Preflights
    Test-RunningWheelHouse

    New-Item -ItemType Directory -Force -Path $LocalRoot | Out-Null

    Write-InstallProgress 5 "Installing the package manager"
    Write-InstallHeartbeat "Installing the package manager (uv)"
    $uv = Install-Uv
    Write-InstallProgress 15 "Downloading Wheelhouse"
    Write-InstallHeartbeat "Downloading and unpacking Wheelhouse (this can take a few minutes)"
    Install-AppArchive -Url $ArchiveUrl -Sha256 $ArchiveSha256

    # Core app first (fatal), then detection, then the providers the user
    # needs.
    Write-InstallProgress 35 "Setting up Wheelhouse"
    Write-InstallHeartbeat "Setting up the Wheelhouse application (this can take a few minutes)"
    Invoke-UvSync -Uv $uv -ServiceRelPath "services\wheelhouse" -Fatal | Out-Null
    $syscheckOk = Invoke-UvSync -Uv $uv -ServiceRelPath "services\syscheck"

    # One hardware snapshot, two answers: the CUDA speech-engine offer here and
    # the local-AI decision in the AI block below.
    $snapshot = $null
    if ($syscheckOk) { $snapshot = Get-HardwareSnapshot -Uv $uv }
    $cudaCapable = Test-CudaCapable -Snapshot $snapshot

    $currentProvider = Get-CurrentProvider
    $provider = Select-SttProvider -CudaCapable $cudaCapable -CurrentProvider $currentProvider -SttProvider $SttProvider

    $providerDirs = @{
        "parakeet_tdt" = "services\stt_providers\sherpa_offline_parakeet_stt_server"
        "google_stt" = "services\stt_providers\google_stt_server"
        "distil_medium_en" = "services\stt_providers\distil_medium_en"
    }

    # The chosen provider must set up (fatal); Parakeet and Google are also
    # set up as switchable alternatives when not chosen (best effort). The
    # CUDA provider is set up only when chosen -- its torch stack is large
    # and only useful on NVIDIA hardware. Anything not set up is disabled in
    # its config.
    Write-InstallProgress 55 "Setting up speech engines"
    Write-InstallHeartbeat "Setting up the speech engine (this can take several minutes)"
    $syncedProviders = @()
    foreach ($name in @("parakeet_tdt", "google_stt", "distil_medium_en")) {
        $shouldSync = $false
        if ($name -eq $provider) { $shouldSync = $true }
        elseif ($name -eq "parakeet_tdt" -or $name -eq "google_stt") { $shouldSync = $true }
        if (-not $shouldSync) { continue }
        if ($name -eq $provider) {
            Invoke-UvSync -Uv $uv -ServiceRelPath $providerDirs[$name] -Fatal | Out-Null
            $syncedProviders += $name
        } else {
            $ok = Invoke-UvSync -Uv $uv -ServiceRelPath $providerDirs[$name]
            if ($ok) { $syncedProviders += $name }
        }
    }
    foreach ($name in $providerDirs.Keys) {
        if ($syncedProviders -notcontains $name) {
            Set-TomlProviderDisabled -ConfigPath (Join-Path $AppDir (Join-Path $providerDirs[$name] "config.toml"))
        }
    }

    Write-InstallProgress 75 "Speech engine ready"

    if ($provider -eq "parakeet_tdt") {
        Write-InstallProgress 80 "Downloading the speech model"
        Write-InstallHeartbeat "Downloading and unpacking the speech model (this can take a few minutes)"
        Install-ParakeetModel
    }
    Write-ModelOverrideFile
    # Fresh-vs-update signal for the AI "keep" default below: config.toml exists
    # here ONLY if Restore-PreservedFiles brought it back from a prior install
    # (the release archive excludes it -- manifest.toml). Capture it BEFORE
    # Write-UserConfig, which creates it from config.toml.example on a fresh
    # install. So $configPreexisted true == re-run/update, false == first install.
    $configPreexisted = Test-Path -LiteralPath (Join-Path $AppDir "services\wheelhouse\config.toml")
    Write-UserConfig -Provider $provider

    # AI helper: write [ai.server] from the -AiMode choice. Four intents:
    #   cloud -- pin Google's Gemini Flash Lite and route the key to the
    #            environment (never config.toml, git-tracked -- wh-ai-key-from-env).
    #   local -- download a llama.cpp build and a model file and configure
    #            Wheelhouse to start its own server. No account, no key. The
    #            hardware check runs FIRST; a machine that cannot run the model
    #            is told why and gets the off state instead of 5 GB that will
    #            not start (wh-ai-installer-local-mode).
    #   off   -- explicit: write the AI-off state (empty base_url, disabled) AND
    #            clear the persisted key so no stale secret lingers (codex 2.3).
    #   keep  -- the default when -AiMode is omitted. A re-run/update PRESERVES
    #            the existing [ai] config (do not clobber a working cloud setup --
    #            codex 2.1); a FRESH install falls back to the off state, so it
    #            does not ship pointed at an Ollama the installer never sets up.
    # $configPreexisted (captured above, before Write-UserConfig) is the
    # fresh-vs-update signal. Runs after Write-UserConfig, which has created
    # config.toml. The AI helper is optional and secondary, so the whole block is
    # guarded: a failure here (for example a transient file lock on the
    # just-written config) must only warn, never abort an otherwise-complete
    # install.
    try {
        $aiConfigPath = Join-Path $AppDir "services\wheelhouse\config.toml"
        if ($AiMode -eq "cloud") {
            $aiKey = Resolve-AiApiKey -AiApiKey $AiApiKey
            if ($aiKey) {
                # Persist the key BEFORE writing the cloud endpoint. If this
                # throws, the surrounding try/catch warns and config.toml is left
                # in its prior state -- not switched on to a cloud endpoint with
                # no key, which would 401 every AI request (deepseek 1.1). The key
                # goes to the environment only, never config.toml (git-tracked).
                Set-AiApiKeyEnv -Key $aiKey
                $aiBaseUrl = if ($AiBaseUrl) { $AiBaseUrl } else { $DefaultAiBaseUrl }
                $aiModel = if ($AiModel) { $AiModel } else { $DefaultAiModel }
                # Switching a machine that had the local model over to cloud
                # must stop it starting its own server; the runtime's enabled
                # key is separate from base_url and would otherwise stay true.
                Disable-AiRuntimeConfig -ConfigPath $aiConfigPath
                Set-AiServerConfig -ConfigPath $aiConfigPath -BaseUrl $aiBaseUrl -Model $aiModel -Kind "cloud"
                # Re-enable the [ai] master switch (a prior "off" install may have
                # turned it off); base_url alone does not undo that.
                Set-TomlSectionValues -ConfigPath $aiConfigPath -Section "ai" -Updates ([ordered]@{ enabled = "true" })
                Write-Status "AI helper configured (cloud model: $aiModel)."
            } else {
                # Cloud was requested but no key is available. Do not switch AI on
                # with no key -- write the safe off state (empty base_url,
                # disabled) so the app is not left failing every AI request
                # against a keyless cloud endpoint. The user can set the key and
                # re-run, or enable AI in config.toml later.
                Disable-AiRuntimeConfig -ConfigPath $aiConfigPath
                Set-AiServerConfig -ConfigPath $aiConfigPath -BaseUrl "" -Kind "local"
                Set-TomlSectionValues -ConfigPath $aiConfigPath -Section "ai" -Updates ([ordered]@{ enabled = "false" })
                # Also clear any previously-persisted key, so the off state is
                # consistent end to end -- config off AND no stale key (glm52 3.2).
                # Otherwise a later manual re-enable in config.toml would silently
                # reuse the old key. Recovery is unaffected: Resolve-AiApiKey reads
                # the out-of-band -AiApiKey / WHEELHOUSE_AI_API_KEY_INPUT, not this
                # persisted variable.
                Clear-AiApiKeyEnv
                Write-InstallNotice "AI helper was set to cloud but no API key was provided, so AI is left off. Set the WHEELHOUSE_AI_API_KEY environment variable and enable AI in config.toml to turn it on later."
            }
        } elseif ($AiMode -eq "local") {
            # The hardware decision is READ, not made -- it comes from
            # services/syscheck/recommendations.py by way of the snapshot
            # fetched above, so syscheck and the installer cannot give a user
            # different answers about the same machine. Read BEFORE any
            # download: finding out after 5 GB that the machine cannot run it
            # is the same mistake in a more expensive form.
            $decision = Get-LocalAiDecision -Snapshot $snapshot
            if ($decision.install) {
                $modelPath = Install-LocalAiRuntime -GpuLayers $decision.gpu_layers
                # Order matters, as in the cloud branch: put the pieces on disk
                # and point the runtime at them BEFORE enabling AI. The
                # surrounding try/catch only warns, so a failure between these
                # steps must not leave AI enabled against a server that has
                # nothing to serve.
                Set-AiRuntimeConfig -ConfigPath $aiConfigPath -ModelPath $modelPath `
                    -BinaryDir $LlamaDir -GpuLayers $decision.gpu_layers
                # base_url carries the port the server is started on -- there is
                # deliberately no port key in [ai.runtime], so this address is
                # the only place it lives (ai/runtime_config.py).
                Set-AiServerConfig -ConfigPath $aiConfigPath -BaseUrl $LocalAiBaseUrl `
                    -Model $LocalAiModelName -Kind "local"
                # A prior "off" install left [ai] enabled = false, and base_url
                # alone does not undo that.
                Set-TomlSectionValues -ConfigPath $aiConfigPath -Section "ai" -Updates ([ordered]@{ enabled = "true" })
                Write-Status "AI helper configured (local model, running on $(if ($decision.gpu_layers -gt 0) { 'the graphics card' } else { 'the processor' }))."
            } else {
                # The machine cannot run it. Say why in the ladder's own words
                # -- a wording rewritten here would be a second place for it to
                # go stale -- and write the off state rather than installing
                # something that cannot start.
                Disable-AiRuntimeConfig -ConfigPath $aiConfigPath
                Set-AiServerConfig -ConfigPath $aiConfigPath -BaseUrl "" -Kind "local"
                Set-TomlSectionValues -ConfigPath $aiConfigPath -Section "ai" -Updates ([ordered]@{ enabled = "false" })
                Write-InstallNotice "$($decision.reason)"
            }
        } elseif ($AiMode -eq "off") {
            # Explicit off: write the off state and clear the persisted key so a
            # switch-off actually turns AI off end to end (codex 2.3).
            Disable-AiRuntimeConfig -ConfigPath $aiConfigPath
            Set-AiServerConfig -ConfigPath $aiConfigPath -BaseUrl "" -Kind "local"
            # Disable the [ai] master switch too, so the app does not log an
            # every-startup "enabled but no server" upgrade warning on a fresh
            # off install (ai/service.py). base_url="" alone leaves that warning.
            Set-TomlSectionValues -ConfigPath $aiConfigPath -Section "ai" -Updates ([ordered]@{ enabled = "false" })
            Clear-AiApiKeyEnv
            Write-Status "AI helper turned off. You can set one up later in config.toml."
        } elseif ($configPreexisted) {
            # keep + re-run/update: preserve the existing [ai] config and the
            # persisted key untouched, so a re-run without -AiMode never clobbers
            # a working cloud setup the user configured earlier (codex 2.1).
            Write-Status "AI helper left as previously configured."
        } else {
            # keep + fresh install: no prior config to preserve, so fall back to
            # the off state (empty base_url, disabled) rather than shipping the
            # example's default pointed at an Ollama the installer never set up.
            # No persisted key exists on a fresh machine, so nothing to clear.
            Disable-AiRuntimeConfig -ConfigPath $aiConfigPath
            Set-AiServerConfig -ConfigPath $aiConfigPath -BaseUrl "" -Kind "local"
            Set-TomlSectionValues -ConfigPath $aiConfigPath -Section "ai" -Updates ([ordered]@{ enabled = "false" })
            Write-Status "AI helper left off. You can set one up later in config.toml."
        }
    } catch {
        Write-InstallNotice "Could not configure the AI helper: $($_.Exception.Message). Wheelhouse is installed; you can set up AI later in config.toml."
    }

    if ($provider -eq "google_stt") {
        Write-InstallNotice "The Google Cloud engine needs credentials before it can hear you. See the Google Cloud section of INSTALL.md in the install folder: $AppDir"
    }
    if ($provider -eq "distil_medium_en") {
        Write-InstallNotice "The Distil-Whisper engine downloads its own model on first start; the first launch will take a few minutes."
    }

    Write-InstallProgress 95 "Finishing up"
    # Shortcuts + the auto-start question. A failed shortcut must not abort an
    # otherwise complete install, but it must be loud and name the exact path:
    # the first physical-machine install ended with a desktop shortcut and no
    # Start-menu entry, and nothing recorded where the Start-menu attempt went
    # (wh-startmenu-shortcut-check). Logging the resolved path per location
    # makes the next field failure diagnosable from the setup log.
    foreach ($shortcutFolder in @("Programs", "Desktop")) {
        $lnkPath = Join-Path ([Environment]::GetFolderPath($shortcutFolder)) $ShortcutName
        try {
            New-AppShortcut -LnkPath $lnkPath
            Write-Status "Shortcut created: $lnkPath"
        } catch {
            Write-InstallNotice "Could not create the $shortcutFolder shortcut at ${lnkPath}: $($_.Exception.Message)"
        }
    }

    Write-Host ""
    $autoStart = Resolve-YesNoChoice -Specified ($AutoStart -ne "") -Value ($AutoStart -eq "yes") `
        -Prompt "Start Wheelhouse automatically when you log in? For hands-free use this is strongly recommended. Type yes or no (default: no)"
    if ($autoStart) {
        $startupLnk = Join-Path ([Environment]::GetFolderPath("Startup")) $ShortcutName
        try {
            New-AppShortcut -LnkPath $startupLnk
            Write-Status "Wheelhouse will start automatically at login ($startupLnk)."
        } catch {
            Write-InstallNotice "Could not create the Startup shortcut at ${startupLnk}: $($_.Exception.Message)"
        }
    }

    Write-Host ""
    Write-Status "Wheelhouse $AppVersion is installed."
    Write-InstallProgress 100 "Wheelhouse is installed"
    # Only when it is actually off. This used to tell every user to go and check
    # a Windows setting whether or not anything was wrong with it, which is a
    # chore for the many and buries the warning for the few
    # (wh-wizard-mic-permission-noise). Checked here, at the end, because the
    # graphical installer offers to fix it on a page near the start and the
    # answer can have changed since.
    if (-not (Test-MicrophonePermission)) {
        Write-InstallNotice ("Windows is blocking desktop apps from the microphone, so Wheelhouse will hear nothing. " +
            "Turn it on at Settings > Privacy and security > Microphone > Let desktop apps access your microphone.")
    }
    Write-Host ""

    $startNow = Resolve-YesNoChoice -Specified ($StartNow -ne "") -Value ($StartNow -eq "yes") `
        -Prompt "Start Wheelhouse now? Type yes or no (default: no)"
    if ($startNow) {
        # --directory mirrors the shortcut arguments: the app path must be in
        # the uv command line (not only the working directory) so the
        # running-app check can see a just-launched Wheelhouse.
        $runDir = Join-Path $AppDir "services\wheelhouse"
        Start-Process -FilePath $uv -ArgumentList "run", "--directory", "`"$runDir`"", "--locked", "--no-sync", "python", "launcher.py" -WorkingDirectory $runDir
        Write-Status "Wheelhouse is starting -- look for the tray icon."
    }
}

# Resolve archive source: parameter > environment variable > pinned default.
if (-not $ArchiveUrl) {
    if ($env:WHEELHOUSE_ARCHIVE_URL) { $ArchiveUrl = $env:WHEELHOUSE_ARCHIVE_URL }
    else { $ArchiveUrl = $DefaultArchiveUrl }
}
if (-not $ArchiveSha256) {
    if ($env:WHEELHOUSE_ARCHIVE_SHA256) { $ArchiveSha256 = $env:WHEELHOUSE_ARCHIVE_SHA256 }
    else { $ArchiveSha256 = $DefaultArchiveSha256 }
}

# One top-level handler for both paths. Stop-Install has already printed
# its plain-English guidance by the time control lands here; anything else
# is unexpected and gets a generic honest message. `exit` is used only when
# running from a file -- under irm|iex it would close the user's console.
try {
    if ($Uninstall) {
        Invoke-Uninstall
    } else {
        Invoke-MainInstall
    }
} catch {
    if ($_.ToString() -notlike "*WHEELHOUSE-INSTALL-STOP*") {
        Write-Fail "Unexpected error: $($_.Exception.Message)"
        Write-Host "    Please run the installer again; if it keeps failing, file an issue: $IssuesUrl"
        Write-Host "    or email help@wheelhouse-project.org"
        # The graphical installer's failure dialog is built from these tagged
        # lines. Without them the one failure with no plain-English reason --
        # the unexpected one -- would also be the one the user is told nothing
        # about.
        Write-InstallFailure -Reason "Unexpected error: $($_.Exception.Message)" `
            -WhatToTry "Run Setup again. If it stops the same way, send the setup log to help@wheelhouse-project.org."
    }
    if ($script:RunningFromFile) { exit 1 }
}
