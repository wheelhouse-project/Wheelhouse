# install-wheelhouse.ps1 -- one-command WheelHouse installer for end users.
#
# Primary invocation (from the README):
#   irm https://github.com/wheelhouse-project/WheelHouse/releases/latest/download/install-wheelhouse.ps1 | iex
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
    [ValidateSet("keep", "off", "cloud")]
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
    [switch]$KeepData
)

$ErrorActionPreference = "Stop"

# Empty when the script text is piped through Invoke-Expression (the primary
# irm|iex path); set when run as a downloaded file. Failure handling must not
# call `exit` in the iex case -- that closes the user's console window.
$script:RunningFromFile = [bool]$PSCommandPath

# --- Pinned versions and sources -------------------------------------------
# The archive URL and hash are stamped on publish day: build the release
# archive, hash it, stamp both values here, upload archive + this script.

$AppVersion = "1.0.2"
$DefaultArchiveUrl = "https://github.com/wheelhouse-project/WheelHouse/releases/download/v$AppVersion/wheelhouse-$AppVersion.zip"
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

# --- Paths ------------------------------------------------------------------

$LocalRoot = Join-Path $env:LOCALAPPDATA "WheelHouse"
$AppDir = Join-Path $LocalRoot "app"
$ModelsDir = Join-Path $LocalRoot "models"
$DownloadsDir = Join-Path $LocalRoot "downloads"
$RoamingRoot = Join-Path $env:APPDATA "WheelHouse"
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

$ShortcutName = "WheelHouse.lnk"
$IssuesUrl = "https://github.com/wheelhouse-project/WheelHouse/issues"

# --- Output helpers ----------------------------------------------------------

function Write-Status { param([string]$Message) Write-Host "[+] $Message" -ForegroundColor Green }
function Write-Warn { param([string]$Message) Write-Host "[!] $Message" -ForegroundColor Yellow }
function Write-Fail { param([string]$Message) Write-Host "[x] $Message" -ForegroundColor Red }

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
    Write-Fail $Message
    if ($WhatToTry) { Write-Host "    What to try: $WhatToTry" }
    Write-Host "    If you are stuck, please file an issue: $IssuesUrl"
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
        Stop-Install "WheelHouse needs 64-bit Windows; this system is 32-bit."
    }
    $os = Get-CimInstance Win32_OperatingSystem
    $osVersion = [Version]$os.Version
    if ($osVersion.Major -lt 10) {
        Stop-Install "WheelHouse needs Windows 10 or 11; this system reports Windows version $($os.Version)."
    }

    # Disk space on the drive that will hold the install.
    $driveName = (Get-Item $env:LOCALAPPDATA).PSDrive.Name
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
            "Use the Google Cloud speech engine instead -- it runs in the cloud and needs far less memory, but requires a Google Cloud account (see INSTALL.md in the WheelHouse repository). Or add memory to this computer."
    }
    if ($ram -lt $RamRecommendedBytes) {
        Write-Warn "This computer has $([math]::Round($ram / 1GB, 1)) GB of memory. WheelHouse will run, but 16 GB is recommended."
    }

    # CPU floor: warn and continue (see the note on the constant above).
    $cores = (Get-CimInstance Win32_Processor | Measure-Object -Property NumberOfCores -Sum).Sum
    if ($cores -lt $CpuWarnCores) {
        Write-Warn "This computer has $cores CPU cores. Speech recognition may respond slowly; 4 or more cores are recommended."
    }

    # Microphone presence: warn only -- the user may plug one in later.
    $micCount = 0
    try {
        $captureKey = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture"
        if (Test-Path $captureKey) {
            foreach ($endpoint in Get-ChildItem $captureKey) {
                $state = (Get-ItemProperty -Path $endpoint.PSPath -Name DeviceState -ErrorAction SilentlyContinue).DeviceState
                if ($state -eq 1) { $micCount++ }
            }
        }
    } catch { $micCount = -1 }
    if ($micCount -eq 0) {
        Write-Warn "No active microphone was found. WheelHouse needs one to hear you -- plug one in before the first run."
    }

    # tar.exe ships with Windows 10 1803+ but is absent on some older/LTSC
    # images. It is needed to unpack the speech model archive; enforced hard
    # only if the Parakeet download actually runs.
    $script:HasTar = [bool](Get-Command tar.exe -ErrorAction SilentlyContinue)
    if (-not $script:HasTar) {
        Write-Warn "tar.exe was not found. It is needed to unpack the offline speech model; the installer will stop at that step if you choose the default engine."
    }

    Write-Status "Requirement checks passed."
}

# --- Running-app refusal (stop before wipe) ------------------------------------

function Test-CommandLineInAppDir {
    param([string]$CommandLine)
    # Path-boundary match: the app directory must be followed by a path
    # separator, a closing quote, whitespace, or the end of the command
    # line. A bare substring test would also match sibling directories
    # that merely start with the same text (e.g. WheelHouse\app_backup)
    # and falsely refuse an install.
    if (-not $CommandLine) { return $false }
    $pattern = [regex]::Escape($AppDir.ToLower()) + '($|[\\/"''\s])'
    return [bool]($CommandLine.ToLower() -match $pattern)
}

function Test-RunningWheelHouse {
    param([switch]$RequireVerified)
    # All four WheelHouse processes are python.exe inside venvs, so detection
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
            Stop-Install "Could not check whether WheelHouse is currently running ($($_.Exception.Message))." `
                "Close WheelHouse if it is running (right-click the WheelHouse tray icon and choose Exit), or restart this computer, then run the installer again."
        }
        Write-Warn "Could not check for a running WheelHouse: $($_.Exception.Message)"
        return
    }
    if ($running.Count -gt 0) {
        Stop-Install "WheelHouse appears to be running ($($running.Count) process(es) found)." `
            "Close WheelHouse first: right-click the WheelHouse tray icon and choose Exit, or say the exit voice command. Then run this installer again."
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
        if (Test-Path $c) { return $c }
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
        $userPath = (Get-ItemProperty -Path "HKCU:\Environment" -Name Path -ErrorAction SilentlyContinue).Path
        if ($userPath) { $values += $userPath }
    } catch {}
    try {
        $machinePath = (Get-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" -Name Path -ErrorAction SilentlyContinue).Path
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
        [WheelHouseInstaller.NativeMethods]::SendMessageTimeout(
            [IntPtr]0xffff, 0x1A, [UIntPtr]::Zero, "Environment",
            2, 5000, [ref]$result) | Out-Null
    } catch {
        Write-Warn "Could not tell running programs about the PATH change. If WheelHouse cannot find uv when it starts, sign out and back in once."
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
    Send-EnvironmentChangeBroadcast
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
    Send-EnvironmentChangeBroadcast
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
        Write-Status "Installing uv (the Python environment manager WheelHouse uses)..."
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
        Write-Status "Adding uv to your PATH so WheelHouse can start its speech engines..."
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

    if (Test-Path $Destination) {
        $existing = (Get-FileHash -Path $Destination -Algorithm SHA256).Hash
        if ($existing -ieq $ExpectedSha256) {
            Write-Status "$Description is already downloaded and verified."
            return
        }
        Remove-Item $Destination -Force
    }

    $partial = "$Destination.partial"
    $meta = "$Destination.partial.meta"

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
                Remove-Item $partial -Force -ErrorAction SilentlyContinue
                Remove-Item $meta -Force -ErrorAction SilentlyContinue
                $resumeNote = "retrying from the beginning"
            }
            if ($attempt -ge 2) {
                Stop-Install "Downloading $Description failed twice: $($_.Exception.Message)" `
                    "Check your internet connection and run the installer again -- it resumes where it left off."
            }
            Write-Warn "Download failed ($($_.Exception.Message)); $resumeNote..."
            continue
        }

        $hash = (Get-FileHash -Path $partial -Algorithm SHA256).Hash
        if ($hash -ieq $ExpectedSha256) {
            Move-Item -Path $partial -Destination $Destination -Force
            if (Test-Path $meta) { Remove-Item $meta -Force }
            Write-Status "$Description downloaded and verified."
            return
        }

        # Hash-mismatch recovery: DELETE the partial before retrying. A
        # persisted corrupt partial plus Range resume would otherwise be a
        # permanent failure loop that re-running the installer never clears.
        Remove-Item $partial -Force
        if (Test-Path $meta) { Remove-Item $meta -Force }
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
    if ((Test-Path $Partial) -and (Test-Path $Meta)) {
        $saved = $null
        try {
            $saved = [System.IO.File]::ReadAllText($Meta) | ConvertFrom-Json
        } catch { $saved = $null }
        if ($null -ne $saved) {
            return @{ ResumeFrom = (Get-Item $Partial).Length; SavedMeta = $saved }
        }
        Write-Warn "The saved download-resume information is unreadable; starting this download from the beginning."
        Remove-Item $Partial -Force -ErrorAction SilentlyContinue
        Remove-Item $Meta -Force -ErrorAction SilentlyContinue
    } elseif (Test-Path $Partial) {
        Remove-Item $Partial -Force
    } elseif (Test-Path $Meta) {
        Remove-Item $Meta -Force
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
        $newMeta | ConvertTo-Json | Set-Content -Path $Meta -Encoding ASCII

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
    if ((Test-Path $StagingDir) -and -not (Test-Path $MarkerPath)) {
        Remove-Item $StagingDir -Recurse -Force
    }
    if ((Test-Path $MarkerPath) -and -not (Test-Path $StagingDir)) {
        Remove-Item $MarkerPath -Force
    }
}

function Save-PreservedFiles {
    param([string]$StagingDir, [switch]$OverwriteFromLive)
    $saved = @()
    foreach ($rel in $PreservePaths) {
        $dst = Join-Path $StagingDir $rel
        $src = Join-Path $AppDir $rel
        if ((Test-Path $dst) -and -not ($OverwriteFromLive -and (Test-Path $src))) {
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
        if (Test-Path $src) {
            New-Item -ItemType Directory -Force -Path (Split-Path $dst -Parent) | Out-Null
            Copy-Item -Path $src -Destination $dst -Force
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
    if (-not (Test-Path $StagingDir)) { return 0 }
    $files = @(Get-ChildItem -Path $StagingDir -Recurse -File)
    $stagingRoot = (Get-Item $StagingDir).FullName.TrimEnd('\')
    foreach ($f in $files) {
        $rel = $f.FullName.Substring($stagingRoot.Length + 1)
        $dst = Join-Path $AppDir $rel
        New-Item -ItemType Directory -Force -Path (Split-Path $dst -Parent) | Out-Null
        Copy-Item -Path $f.FullName -Destination $dst -Force
    }
    return $files.Count
}

function Install-AppArchive {
    param([string]$Url, [string]$Sha256)

    New-Item -ItemType Directory -Force -Path $DownloadsDir | Out-Null
    $zipPath = Join-Path $DownloadsDir "wheelhouse-$AppVersion.zip"
    Invoke-VerifiedDownload -Url $Url -Destination $zipPath -ExpectedSha256 $Sha256 -Description "the WheelHouse application"

    $staging = Join-Path $LocalRoot "update-preserve"
    # The marker lives NEXT TO staging, not inside it, so the restore
    # (which copies everything staging holds) never plants it in the app.
    $marker = "$staging.wipe-started"
    if (Test-Path $AppDir) {
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
        Remove-Item -Path $AppDir -Recurse -Force
    }

    Write-Status "Unpacking the application..."
    New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
    Expand-Archive -Path $zipPath -DestinationPath $AppDir -Force

    # If the archive wraps everything in a single top-level directory,
    # flatten it so $AppDir is the repository root.
    if (-not (Test-Path (Join-Path $AppDir "VERSION"))) {
        $children = @(Get-ChildItem $AppDir)
        if ($children.Count -eq 1 -and $children[0].PSIsContainer -and (Test-Path (Join-Path $children[0].FullName "VERSION"))) {
            Get-ChildItem $children[0].FullName -Force | Move-Item -Destination $AppDir
            Remove-Item $children[0].FullName -Recurse -Force
        }
    }
    if (-not (Test-Path (Join-Path $AppDir "VERSION"))) {
        Stop-Install "The unpacked application archive does not look like WheelHouse (no VERSION file)." `
            "If you overrode the archive URL, check it points at a WheelHouse release archive."
    }

    # Restore runs off staging CONTENTS so a leftover staging dir from a
    # crashed earlier update is recovered here instead of deleted below.
    $restored = Restore-PreservedFiles -StagingDir $staging
    if ($restored -gt 0) {
        Write-Status "Restored $restored preserved file(s) from the previous install."
    }
    if (Test-Path $staging) { Remove-Item $staging -Recurse -Force }
    if (Test-Path $marker) { Remove-Item $marker -Force }
}

# --- Environment syncs (step d) ---------------------------------------------------

function Invoke-UvSync {
    param([string]$Uv, [string]$ServiceRelPath, [switch]$Fatal)
    $serviceDir = Join-Path $AppDir $ServiceRelPath
    Write-Status "Setting up $ServiceRelPath..."
    # A missing or non-directory service path (interrupted extraction,
    # antivirus quarantine) must follow the same fatal/non-fatal contract
    # as a failed sync -- an unguarded Push-Location would crash the whole
    # install even for the optional components. -PathType Container:
    # plain Test-Path is also true for a FILE left at the path.
    if (-not (Test-Path $serviceDir -PathType Container)) {
        if ($Fatal) {
            Stop-Install "Setting up $ServiceRelPath failed: that path in the unpacked application is missing or is not a folder." `
                "Run the installer again (it re-downloads and re-unpacks the application). An antivirus quarantining files can also cause this."
        }
        Write-Warn "Setting up $ServiceRelPath failed (the path is missing or is not a folder); this optional component will be disabled."
        return $false
    }
    Push-Location $serviceDir
    try {
        $result = Invoke-Native -Exe $Uv -Arguments @("sync", "--locked", "--no-dev")
        $code = $result.ExitCode
    } finally {
        Pop-Location
    }
    if ($code -ne 0) {
        # Show the tail of uv's output so a failed install is diagnosable
        # from the console (and from an issue report).
        $tail = @($result.Output | Select-Object -Last 5)
        foreach ($line in $tail) { Write-Host "    uv: $line" }
        if ($Fatal) {
            Stop-Install "Setting up $ServiceRelPath failed (uv sync exit code $code)." `
                "Check your internet connection and run the installer again. Corporate proxies can also block package downloads."
        }
        Write-Warn "Setting up $ServiceRelPath failed (exit code $code); this optional component will be disabled."
        return $false
    }
    return $true
}

# --- Hardware snapshot for the STT offer (step e) ---------------------------------

function Get-CudaCapable {
    param([string]$Uv)
    # syscheck's venv has its own dependencies that nothing else installs;
    # its role here is GPU detection only (the RAM/CPU floor already ran
    # natively in the preflights). Any failure degrades to "no CUDA offer".
    $serviceDir = Join-Path $AppDir "services\syscheck"
    try {
        Push-Location $serviceDir
        try {
            $result = Invoke-Native -Exe $Uv -Arguments @(
                "run", "python", "syscheck.py", "--compact")
        } finally {
            Pop-Location
        }
        if ($result.ExitCode -ne 0) { return $false }
        # The merged stream carries uv's stderr progress as error records;
        # the JSON is the plain-string stdout lines.
        $json = ($result.Output | Where-Object { $_ -is [string] }) -join "`n"
        if (-not $json) { return $false }
        $data = $json | ConvertFrom-Json
        foreach ($gpu in $data.gpu) {
            if ($gpu.software) { continue }
            if ($gpu.vendor_id -eq $NvidiaVendorId -and $gpu.dedicated_vram_bytes -ge $CudaMinVramBytes) {
                return $true
            }
        }
    } catch {
        Write-Warn "GPU detection failed ($($_.Exception.Message)); offering CPU and cloud speech engines only."
    }
    return $false
}

function Get-CurrentProvider {
    # The engine an existing (restored) config already uses, or "" on a
    # fresh install. Keeps an update's Enter-through-the-prompt from
    # silently switching a user's engine.
    $config = Join-Path $AppDir "services\wheelhouse\config.toml"
    if (Test-Path $config) {
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
            Write-Warn "Distil-Whisper needs an NVIDIA graphics card, which was not detected; using Parakeet instead."
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
        Write-Warn "Your current speech engine ($($names[$CurrentProvider])) is not available on this computer (it needs an NVIDIA graphics card, which was not detected), so the default has changed to $($names[$default])."
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

function Test-ModelComplete {
    param([string]$ModelDir)
    # Mirrors sherpa_engine.py's required-file check: tokens.txt plus the
    # encoder/decoder/joiner trio in either int8 or full-precision naming.
    # tokens.txt alone is NOT proof of a finished extraction -- an
    # interrupted tar can leave it behind without the much larger ONNX
    # files, and the app then fails at startup with nothing to repair it.
    if (-not (Test-Path (Join-Path $ModelDir "tokens.txt"))) { return $false }
    foreach ($suffix in @(".int8.onnx", ".onnx")) {
        $allPresent = $true
        foreach ($part in @("encoder", "decoder", "joiner")) {
            if (-not (Test-Path (Join-Path $ModelDir "$part$suffix"))) {
                $allPresent = $false
                break
            }
        }
        if ($allPresent) { return $true }
    }
    return $false
}

function Install-ParakeetModel {
    if (-not $script:HasTar) {
        Stop-Install "tar.exe is needed to unpack the speech model, but it was not found on this system." `
            "tar.exe ships with Windows 10 version 1803 and later. On older editions, install it or choose the Google Cloud engine instead."
    }
    $extractedDir = Join-Path $ModelsDir $ModelDirName
    if (Test-ModelComplete -ModelDir $extractedDir) {
        Write-Status "The speech model is already installed."
        return
    }
    if (Test-Path $extractedDir) {
        # Leftover from an interrupted extraction. Remove it so the fresh
        # extraction cannot mix old and new files.
        Write-Warn "An incomplete speech model was found from an earlier run; reinstalling it."
        Remove-Item $extractedDir -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $ModelsDir | Out-Null
    New-Item -ItemType Directory -Force -Path $DownloadsDir | Out-Null
    $archive = Join-Path $DownloadsDir "$ModelDirName.tar.bz2"
    Write-Status "Downloading the speech model (about 650 MB; this is the longest step)..."
    Invoke-VerifiedDownload -Url $ModelUrl -Destination $archive -ExpectedSha256 $ModelSha256 -Description "the speech model"

    Write-Status "Unpacking the speech model..."
    # Through Invoke-Native like every other native call: bsdtar warnings on
    # stderr are the same under-Stop landmine as uv's progress output
    # whenever the process stderr is redirected.
    $tar = Invoke-Native -Exe "tar.exe" -Arguments @("-xjf", $archive, "-C", $ModelsDir)
    if ($tar.ExitCode -ne 0 -or -not (Test-ModelComplete -ModelDir $extractedDir)) {
        Stop-Install "Unpacking the speech model failed (tar exit code $($tar.ExitCode))." `
            "Run the installer again; the download itself is kept and will not repeat."
    }
    Remove-Item $archive -Force
    Write-Status "Speech model installed."
}

# --- Config writing (step f) ----------------------------------------------------------

function Set-TomlProviderDisabled {
    param([string]$ConfigPath)
    # Sets enabled = false inside the [provider] section. Providers whose
    # environments were not set up must be undiscoverable: discovery treats
    # an absent-or-true enabled key as available, and launch cannot repair a
    # missing venv -- the provider would appear in the menu and then fail.
    if (-not (Test-Path $ConfigPath)) { return }
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
    if (-not (Test-Path $config)) {
        Copy-Item -Path $example -Destination $config
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
    if (-not (Test-Path $ConfigPath)) { return }

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
    # just-launched WheelHouse until the Python child exists.
    $shortcut.Arguments = "run --directory `"$workDir`" --locked --no-sync python launcher.py"
    $shortcut.WorkingDirectory = $workDir
    $icon = Join-Path $AppDir "services\wheelhouse\WheelHouse.ico"
    if (Test-Path $icon) { $shortcut.IconLocation = $icon }
    $shortcut.Description = "WheelHouse voice control"
    $shortcut.Save()
}

function Remove-AllShortcuts {
    foreach ($folder in @("Programs", "Desktop", "Startup")) {
        $lnk = Join-Path ([Environment]::GetFolderPath($folder)) $ShortcutName
        if (Test-Path $lnk) { Remove-Item $lnk -Force }
    }
}

# --- Uninstall -------------------------------------------------------------------------------

function Invoke-Uninstall {
    Write-Host "WheelHouse uninstaller" -ForegroundColor Cyan
    if (-not (Test-Path $LocalRoot) -and -not (Test-Path $RoamingRoot)) {
        Write-Status "WheelHouse does not appear to be installed for this user. Removing any leftover shortcuts."
        Remove-AllShortcuts
        return
    }
    Test-RunningWheelHouse

    # -Force skips this confirmation. -Force:$false (or omitting -Force) still
    # asks, so an accidental switch value cannot silently green-light removal.
    $confirm = Resolve-YesNoChoice -Specified $Force.IsPresent -Value $true `
        -Prompt "Remove WheelHouse from this computer? Type yes to continue"
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
        if (Test-Path $AppDir) { Remove-Item $AppDir -Recurse -Force }
        if (Test-Path $DownloadsDir) { Remove-Item $DownloadsDir -Recurse -Force }
        Write-Status "Your personal data was kept at: $keepDir (and your speech model at $ModelsDir)."
        Write-Host "    Re-running the installer later will offer a fresh install; copy files back from there if you want them."
    } else {
        if (Test-Path $LocalRoot) { Remove-Item $LocalRoot -Recurse -Force }
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
        catch { Write-Warn "Could not clear the saved AI key from your environment: $($_.Exception.Message). You can remove WHEELHOUSE_AI_API_KEY manually in Windows Environment Variables." }
    }

    # The roaming root holds provider PID and port files, never user data. A
    # stale PID file surviving into a reinstall could point cleanup at an
    # innocent process that now owns the recycled PID.
    if (Test-Path $RoamingRoot) { Remove-Item $RoamingRoot -Recurse -Force }
    Remove-AllShortcuts
    Write-Status "WheelHouse has been removed."
    Write-Host "    If anything is left behind, the folders to check are:"
    Write-Host "      $LocalRoot"
    Write-Host "      $RoamingRoot"
}

# --- Main -------------------------------------------------------------------------------------

function Invoke-MainInstall {
    Write-Host ""
    Write-Host "WheelHouse $AppVersion installer" -ForegroundColor Cyan
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
    Write-InstallProgress 15 "Downloading WheelHouse"
    Write-InstallHeartbeat "Downloading and unpacking WheelHouse (this can take a few minutes)"
    Install-AppArchive -Url $ArchiveUrl -Sha256 $ArchiveSha256

    # Core app first (fatal), then detection, then the providers the user
    # needs.
    Write-InstallProgress 35 "Setting up WheelHouse"
    Write-InstallHeartbeat "Setting up the WheelHouse application (this can take a few minutes)"
    Invoke-UvSync -Uv $uv -ServiceRelPath "services\wheelhouse" -Fatal | Out-Null
    $syscheckOk = Invoke-UvSync -Uv $uv -ServiceRelPath "services\syscheck"

    $cudaCapable = $false
    if ($syscheckOk) { $cudaCapable = Get-CudaCapable -Uv $uv }

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
    $configPreexisted = Test-Path (Join-Path $AppDir "services\wheelhouse\config.toml")
    Write-UserConfig -Provider $provider

    # AI helper: write [ai.server] from the -AiMode choice. Three intents:
    #   cloud -- pin Google's Gemini Flash Lite and route the key to the
    #            environment (never config.toml, git-tracked -- wh-ai-key-from-env).
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
                Set-AiServerConfig -ConfigPath $aiConfigPath -BaseUrl "" -Kind "local"
                Set-TomlSectionValues -ConfigPath $aiConfigPath -Section "ai" -Updates ([ordered]@{ enabled = "false" })
                # Also clear any previously-persisted key, so the off state is
                # consistent end to end -- config off AND no stale key (glm52 3.2).
                # Otherwise a later manual re-enable in config.toml would silently
                # reuse the old key. Recovery is unaffected: Resolve-AiApiKey reads
                # the out-of-band -AiApiKey / WHEELHOUSE_AI_API_KEY_INPUT, not this
                # persisted variable.
                Clear-AiApiKeyEnv
                Write-Warn "AI helper was set to cloud but no API key was provided, so AI is left off. Set the WHEELHOUSE_AI_API_KEY environment variable and enable AI in config.toml to turn it on later."
            }
        } elseif ($AiMode -eq "off") {
            # Explicit off: write the off state and clear the persisted key so a
            # switch-off actually turns AI off end to end (codex 2.3).
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
            Set-AiServerConfig -ConfigPath $aiConfigPath -BaseUrl "" -Kind "local"
            Set-TomlSectionValues -ConfigPath $aiConfigPath -Section "ai" -Updates ([ordered]@{ enabled = "false" })
            Write-Status "AI helper left off. You can set one up later in config.toml."
        }
    } catch {
        Write-Warn "Could not configure the AI helper: $($_.Exception.Message). WheelHouse is installed; you can set up AI later in config.toml."
    }

    if ($provider -eq "google_stt") {
        Write-Warn "The Google Cloud engine needs credentials before it can hear you. See the Google Cloud section of INSTALL.md in the install folder: $AppDir"
    }
    if ($provider -eq "distil_medium_en") {
        Write-Warn "The Distil-Whisper engine downloads its own model on first start; the first launch will take a few minutes."
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
            Write-Warn "Could not create the $shortcutFolder shortcut at ${lnkPath}: $($_.Exception.Message)"
        }
    }

    Write-Host ""
    $autoStart = Resolve-YesNoChoice -Specified ($AutoStart -ne "") -Value ($AutoStart -eq "yes") `
        -Prompt "Start WheelHouse automatically when you log in? For hands-free use this is strongly recommended. Type yes or no (default: no)"
    if ($autoStart) {
        $startupLnk = Join-Path ([Environment]::GetFolderPath("Startup")) $ShortcutName
        try {
            New-AppShortcut -LnkPath $startupLnk
            Write-Status "WheelHouse will start automatically at login ($startupLnk)."
        } catch {
            Write-Warn "Could not create the Startup shortcut at ${startupLnk}: $($_.Exception.Message)"
        }
    }

    Write-Host ""
    Write-Status "WheelHouse $AppVersion is installed."
    Write-InstallProgress 100 "WheelHouse is installed"
    Write-Host "    Before the first run: check Windows microphone permission is on"
    Write-Host "    (Settings > Privacy and security > Microphone > Let desktop apps access your microphone)."
    Write-Host ""

    $startNow = Resolve-YesNoChoice -Specified ($StartNow -ne "") -Value ($StartNow -eq "yes") `
        -Prompt "Start WheelHouse now? Type yes or no (default: no)"
    if ($startNow) {
        # --directory mirrors the shortcut arguments: the app path must be in
        # the uv command line (not only the working directory) so the
        # running-app check can see a just-launched WheelHouse.
        $runDir = Join-Path $AppDir "services\wheelhouse"
        Start-Process -FilePath $uv -ArgumentList "run", "--directory", "`"$runDir`"", "--locked", "--no-sync", "python", "launcher.py" -WorkingDirectory $runDir
        Write-Status "WheelHouse is starting -- look for the tray icon."
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
    }
    if ($script:RunningFromFile) { exit 1 }
}
