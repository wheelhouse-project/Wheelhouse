#Requires -Version 5.1
<#
.SYNOPSIS
    WheelHouse development environment bootstrap script.
.DESCRIPTION
    Takes a fresh git clone to a fully working dev environment.
    Run from the repo root: .\bootstrap.ps1
.NOTES
    Requires: Windows 11, internet connection
    See docs/plans/2026-03-12-bootstrap-script-design.md for design details.
#>

param(
    [switch]$FunctionsOnly  # Dot-source mode: load functions without executing main
)

$ErrorActionPreference = "Stop"
$script:RepoRoot = $PSScriptRoot

# --- Status output helpers ---

function Write-Status {
    param([string]$Message)
    Write-Host "[+] $Message" -ForegroundColor Green
}

function Write-WarningStatus {
    param([string]$Message)
    Write-Host "[!] $Message" -ForegroundColor Yellow
}

function Write-ErrorStatus {
    param([string]$Message)
    Write-Host "[x] $Message" -ForegroundColor Red
}

# --- Phase 1: Prerequisites ---

function Find-Python312 {
    # Check if python is in PATH and is 3.12
    if (Get-Command python -ErrorAction SilentlyContinue) {
        try {
            $output = & python --version 2>&1
            if ($output -match "Python (3\.12\.\d+)") {
                return (Get-Command python).Source
            }
        } catch {}
    }

    # Check common install locations
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:ProgramFiles\Python312\python.exe",
        "$env:ProgramFiles(x86)\Python312\python.exe",
        "C:\Python312\python.exe"
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            try {
                $output = & $candidate --version 2>&1
                if ($output -match "Python (3\.12\.\d+)") {
                    return $candidate
                }
            } catch {}
        }
    }

    return $null
}

function Get-PythonVersion {
    $pythonPath = Find-Python312
    if (-not $pythonPath) {
        # Fallback: try bare 'python' for any version
        try {
            $output = & python --version 2>&1
            if ($output -match "Python (\d+\.\d+\.\d+)") {
                return $Matches[1]
            }
        } catch {}
        return $null
    }
    $output = & $pythonPath --version 2>&1
    if ($output -match "Python (\d+\.\d+\.\d+)") {
        return $Matches[1]
    }
    return $null
}

function Test-PythonAvailable {
    $pythonPath = Find-Python312
    return ($null -ne $pythonPath)
}

function Add-PythonToSessionPath {
    $pythonPath = Find-Python312
    if ($pythonPath) {
        $pythonDir = Split-Path $pythonPath -Parent
        $scriptsDir = Join-Path $pythonDir "Scripts"
        if ($env:Path -notlike "*$pythonDir*") {
            $env:Path = "$pythonDir;$scriptsDir;$env:Path"
            Write-Status "Added Python 3.12 to session PATH: $pythonDir"
        }
    }
}

function Test-UvExecutable {
    param([string]$Path)
    try {
        $output = & $Path --version 2>&1
        if ($LASTEXITCODE -eq 0 -and $output -match "^uv ") {
            return $true
        }
    } catch {}
    return $false
}

function Find-Uv {
    # Check common install locations first (prefer known-good over PATH)
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\uv\uv.exe",
        "$env:USERPROFILE\.local\bin\uv.exe",
        "$env:APPDATA\Python\Python312\Scripts\uv.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312\Scripts\uv.exe",
        "$env:APPDATA\pipx\venvs\uv\Scripts\uv.exe",
        "$env:LOCALAPPDATA\pipx\venvs\uv\Scripts\uv.exe"
    )
    foreach ($candidate in $candidates) {
        if ((Test-Path $candidate) -and (Test-UvExecutable $candidate)) {
            return $candidate
        }
    }

    # Fall back to PATH, but validate it works
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        $pathUv = (Get-Command uv).Source
        if (Test-UvExecutable $pathUv) {
            return $pathUv
        }
    }

    return $null
}

function Add-UvToSessionPath {
    $uvPath = Find-Uv
    if ($uvPath) {
        $uvDir = Split-Path $uvPath -Parent
        if ($env:Path -notlike "*$uvDir*") {
            $env:Path = "$uvDir;$env:Path"
            Write-Status "Added uv to session PATH: $uvDir"
        }
    }
}

function Get-UvVersion {
    $uvPath = Find-Uv
    if ($uvPath) {
        try {
            $output = & $uvPath --version 2>&1
            if ($output -match "^uv (\d+\.\d+\.\d+)") {
                return $Matches[1]
            }
        } catch {}
    }
    # Fallback: try bare 'uv'
    try {
        $output = & uv --version 2>&1
        if ($output -match "^uv (\d+\.\d+\.\d+)") {
            return $Matches[1]
        }
    } catch {}
    return $null
}

function Test-UvAvailable {
    $uvPath = Find-Uv
    return ($null -ne $uvPath)
}

function Install-Python {
    Write-Status "Installing Python 3.12 via winget..."
    & winget install Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    # winget returns non-zero for "already installed, no upgrade" -- not a real failure
    # Refresh PATH from registry to pick up newly installed Python
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path", "User")
    # Also check common install locations in case PATH wasn't updated
    Add-PythonToSessionPath
    if (-not (Test-PythonAvailable)) {
        Write-ErrorStatus "Python 3.12 not found after install. Check PATH or install manually."
        throw "Python installation failed -- python 3.12 not found in PATH or standard locations"
    }
    Write-Status "Python 3.12 installed"
}

function Test-JqAvailable {
    # jq is a dev-tooling dependency, not a WheelHouse runtime requirement.
    # Claude Code hooks (user-level) and ad-hoc Bash JSON parsing use it.
    # Missing jq is recoverable (hooks fall back to Python) but noisy -- keep
    # it on the prereq list so ikon/yoga stay symmetric.
    return [bool](Get-Command jq -ErrorAction SilentlyContinue)
}

function Install-Jq {
    Write-Status "Installing jq via winget..."
    & winget install jqlang.jq --accept-package-agreements --accept-source-agreements
    # winget may return non-zero for "already installed, no upgrade" -- not a real failure
    # Refresh PATH from registry so this session sees the newly-installed shim
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path", "User")
    if (-not (Test-JqAvailable)) {
        Write-WarningStatus "jq not found after install. Continuing -- dev-tooling only, not required for WheelHouse itself."
        return
    }
    Write-Status "jq installed"
}

function Install-Uv {
    # Try winget first, fall back to the official standalone installer.
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Status "Installing uv via winget..."
        & winget install astral-sh.uv --accept-package-agreements --accept-source-agreements
        # winget returns non-zero for "already installed, no upgrade" -- not a real failure
    } else {
        Write-WarningStatus "winget not found, falling back to standalone installer"
    }

    # Refresh PATH and check
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path", "User")
    Add-UvToSessionPath

    if (Test-UvAvailable) {
        Write-Status "uv installed"
        return
    }

    # Fallback: Astral's official standalone installer
    Write-Status "Installing uv via standalone installer..."
    try {
        Invoke-Expression (Invoke-WebRequest -UseBasicParsing -Uri "https://astral.sh/uv/install.ps1").Content
    } catch {
        Write-ErrorStatus "uv standalone installer failed: $_"
        throw "uv installation failed"
    }

    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path", "User")
    Add-UvToSessionPath
    if (-not (Test-UvAvailable)) {
        Write-ErrorStatus "Failed to install uv"
        throw "uv installation failed"
    }
    Write-Status "uv installed"
}

function Invoke-Phase1 {
    Write-Host "`n=== Phase 1: Prerequisites ===" -ForegroundColor Cyan

    # Python -- ensure it's in session PATH even if already installed
    Add-PythonToSessionPath

    if (Test-PythonAvailable) {
        Write-Status "Python $(Get-PythonVersion) found"
    } else {
        $existing = Get-PythonVersion
        if ($existing) {
            Write-ErrorStatus "Python $existing found but 3.12.x required"
            throw "Wrong Python version. Install Python 3.12 and ensure it's first in PATH."
        }
        Install-Python
    }

    # uv -- ensure it's in session PATH even if already installed
    Add-UvToSessionPath

    if (Test-UvAvailable) {
        Write-Status "uv $(Get-UvVersion) found"
    } else {
        Install-Uv
    }

    # jq -- dev-tooling for Claude Code hooks and ad-hoc Bash JSON work.
    # Soft dependency: bootstrap succeeds even if install fails.
    if (Test-JqAvailable) {
        Write-Status "jq found"
    } else {
        Install-Jq
    }
}

# --- Phase 2: Service Installation ---

function Find-ServiceDirectories {
    $servicesDir = Join-Path $script:RepoRoot "services"
    $pyprojects = Get-ChildItem -Path $servicesDir -Recurse -Filter "pyproject.toml" |
        Where-Object {
            $_.FullName -notlike "*\.venv\*" `
            -and $_.FullName -notlike "*\__pycache__\*" `
            -and $_.FullName -notlike "*\archive\*"
        }

    $services = @()
    foreach ($pyproject in $pyprojects) {
        $dir = $pyproject.DirectoryName
        $relativePath = $dir.Substring($script:RepoRoot.Length + 1)
        $isCore = $relativePath -notlike "*stt_providers*"

        $services += [PSCustomObject]@{
            Path         = $relativePath
            FullPath     = $dir
            IsCore       = $isCore
            Name         = Split-Path $dir -Leaf
        }
    }

    # Sort: core first, then shared/ first among STT providers, then rest alphabetically
    $core = $services | Where-Object { $_.IsCore } | Sort-Object Name
    $sttShared = $services | Where-Object { -not $_.IsCore -and $_.Name -eq "shared" }
    $sttOther = $services | Where-Object { -not $_.IsCore -and $_.Name -ne "shared" } | Sort-Object Name

    return @($core) + @($sttShared) + @($sttOther)
}

function Install-Service {
    param(
        [PSCustomObject]$Service
    )

    $label = if ($Service.IsCore) { "core" } else { "stt" }
    Write-Host "  Installing [$label] $($Service.Name)..." -NoNewline

    try {
        # Use the validated uv executable, not whatever 'uv' resolves to in PATH
        $uvExe = Find-Uv
        if (-not $uvExe) { throw "uv not found" }

        # Temporarily relax error preference for native commands.
        # uv writes status messages (e.g., "Creating virtual environment") to stderr,
        # which PowerShell 5.1 converts to ErrorRecords that trigger Stop mode.
        $prevErrorPref = $ErrorActionPreference
        $ErrorActionPreference = "Continue"

        # Pass --python so uv pins to our discovered Python 3.12 even if a venv
        # already exists with a different interpreter.
        $pythonPath = Find-Python312
        $syncArgs = @("sync", "--directory", $Service.FullPath)
        if ($pythonPath) {
            $syncArgs += @("--python", $pythonPath)
        }
        $output = & $uvExe @syncArgs 2>&1

        $ErrorActionPreference = $prevErrorPref

        if ($LASTEXITCODE -ne 0) {
            throw "uv sync exited with code $LASTEXITCODE"
        }
        Write-Host " OK" -ForegroundColor Green
        return $true
    } catch {
        if ($Service.IsCore) {
            Write-Host " FAILED" -ForegroundColor Red
            Write-ErrorStatus "$($Service.Name): $_"
            throw "Core service '$($Service.Name)' failed to install"
        } else {
            Write-Host " SKIPPED" -ForegroundColor Yellow
            Write-WarningStatus "$($Service.Name): $_"
            return $false
        }
    }
}

function Invoke-Phase2 {
    Write-Host "`n=== Phase 2: Service Installation ===" -ForegroundColor Cyan

    $services = Find-ServiceDirectories
    Write-Status "Found $($services.Count) services"

    $coreCount = 0
    $sttCount = 0
    $sttFail = 0
    $skipped = @()

    foreach ($svc in $services) {
        $success = Install-Service -Service $svc
        if ($svc.IsCore) {
            $coreCount++
        } else {
            $sttCount++
            if (-not $success) {
                $sttFail++
                $skipped += $svc.Name
            }
        }
    }

    Write-Status "Core services: $coreCount/$coreCount installed"
    $sttInstalled = $sttCount - $sttFail
    if ($sttFail -eq 0) {
        Write-Status "STT providers: $sttInstalled/$sttCount installed"
    } else {
        Write-WarningStatus "STT providers: $sttInstalled/$sttCount installed (skipped: $($skipped -join ', '))"
    }

    return [PSCustomObject]@{
        CoreInstalled = $coreCount
        SttInstalled  = $sttInstalled
        SttTotal      = $sttCount
        Skipped       = $skipped
    }
}

# --- Phase 3: Tooling ---

function Test-OllamaAvailable {
    if (Get-Command ollama -ErrorAction SilentlyContinue) {
        return $true
    }
    return $false
}

function Install-Ollama {
    Write-Status "Installing Ollama via winget..."
    & winget install Ollama.Ollama --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        Write-ErrorStatus "Failed to install Ollama via winget"
        throw "Ollama installation failed"
    }
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path", "User")
    Write-Status "Ollama installed"
}

function Test-OllamaRunning {
    try {
        $response = Invoke-RestMethod -Uri "http://localhost:11434/api/tags" -TimeoutSec 3 -ErrorAction Stop
        return $true
    } catch {
        return $false
    }
}

function Start-OllamaService {
    Write-Status "Starting Ollama service..."
    Start-Process "ollama" -ArgumentList "serve" -WindowStyle Hidden
    Start-Sleep -Seconds 3
    if (Test-OllamaRunning) {
        return $true
    }
    # Give it a bit more time
    Start-Sleep -Seconds 5
    return (Test-OllamaRunning)
}

function Install-OllamaModel {
    param([string]$ModelName)

    Write-Status "Checking for $ModelName model..."
    try {
        $tags = Invoke-RestMethod -Uri "http://localhost:11434/api/tags" -TimeoutSec 5
        $installed = $tags.models | Where-Object { $_.name -like "$ModelName*" }
    } catch {
        Write-WarningStatus "Cannot reach Ollama API -- skipping model check"
        return
    }

    if ($installed) {
        Write-Status "$ModelName already pulled"
    } else {
        Write-Status "Pulling $ModelName (this may take a few minutes)..."
        & ollama pull $ModelName
        if ($LASTEXITCODE -ne 0) {
            Write-WarningStatus "Failed to pull $ModelName -- you can pull it manually later: ollama pull $ModelName"
        } else {
            Write-Status "$ModelName pulled successfully"
        }
    }
}

function Test-GrepaiAvailable {
    if (Get-Command grepai -ErrorAction SilentlyContinue) {
        return $true
    }
    return $false
}

function Install-Grepai {
    Write-Status "Installing grepai..."
    try {
        $installScript = Invoke-RestMethod -Uri "https://raw.githubusercontent.com/yoanbernabeu/grepai/main/install.ps1"
        Invoke-Expression $installScript
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                    [System.Environment]::GetEnvironmentVariable("Path", "User")
    } catch {
        Write-WarningStatus "grepai auto-install failed. Install manually: https://yoanbernabeu.github.io/grepai/installation/"
        return
    }
    if (Test-GrepaiAvailable) {
        Write-Status "grepai installed"
    } else {
        Write-WarningStatus "grepai installed but not in PATH -- restart terminal or add to PATH manually"
    }
}

function Initialize-Grepai {
    $configDir = Join-Path $script:RepoRoot ".grepai"
    $configFile = Join-Path $configDir "config.yaml"
    $templateFile = Join-Path $configDir "config.template.yaml"

    if (Test-Path $configFile) {
        Write-Status "grepai config already exists"
    } elseif (Test-Path $templateFile) {
        Copy-Item $templateFile $configFile
        Write-Status "grepai config seeded from template"
    } else {
        Write-WarningStatus "No grepai config template found -- run 'grepai init' manually"
        return
    }

    # Start indexing in background
    if (Test-GrepaiAvailable) {
        Write-Status "Starting grepai index (background)..."
        Start-Process "grepai" -ArgumentList "index" -WorkingDirectory $script:RepoRoot -WindowStyle Hidden
    }
}

function Invoke-Phase3 {
    Write-Host "`n=== Phase 3: Tooling ===" -ForegroundColor Cyan

    # Ollama
    if (Test-OllamaAvailable) {
        Write-Status "Ollama found"
    } else {
        try {
            Install-Ollama
        } catch {
            Write-WarningStatus "Ollama installation failed -- install manually: winget install Ollama.Ollama"
        }
    }

    if (Test-OllamaAvailable) {
        if (Test-OllamaRunning) {
            Write-Status "Ollama running on localhost:11434"
        } else {
            $started = Start-OllamaService
            if ($started) {
                Write-Status "Ollama started on localhost:11434"
            } else {
                Write-WarningStatus "Ollama not responding -- skipping model pull (start manually and run: ollama pull nomic-embed-text)"
            }
        }

        if (Test-OllamaRunning) {
            Install-OllamaModel -ModelName "nomic-embed-text"
        }
    }

    # grepai
    if (Test-GrepaiAvailable) {
        Write-Status "grepai found"
    } else {
        Install-Grepai
    }

    Initialize-Grepai
}

# --- Main ---

if (-not $FunctionsOnly) {
    $startTime = Get-Date

    Write-Host "=== WheelHouse Bootstrap ===" -ForegroundColor Cyan
    Write-Host "Repo: $script:RepoRoot"
    Write-Host ""

    Invoke-Phase1
    $phase2Result = Invoke-Phase2
    Invoke-Phase3

    # Summary
    $elapsed = (Get-Date) - $startTime
    Write-Host "`n=== WheelHouse Bootstrap Complete ===" -ForegroundColor Cyan
    Write-Host ""
    Write-Status "Python $(Get-PythonVersion)"
    Write-Status "uv $(Get-UvVersion)"
    Write-Status "Services installed: $($phase2Result.CoreInstalled) core, $($phase2Result.SttInstalled)/$($phase2Result.SttTotal) STT providers"
    if ($phase2Result.Skipped.Count -gt 0) {
        Write-WarningStatus "Skipped: $($phase2Result.Skipped -join ', ')"
    }
    if (Test-OllamaAvailable) {
        Write-Status "Ollama available"
    }
    if (Test-GrepaiAvailable) {
        Write-Status "grepai available"
    }
    Write-Host ""
    Write-Host "Elapsed: $($elapsed.Minutes)m $($elapsed.Seconds)s" -ForegroundColor Gray
    Write-Host ""
    Write-Host "Ready to develop. Run WheelHouse with:" -ForegroundColor White
    Write-Host "  cd services\wheelhouse" -ForegroundColor White
    Write-Host "  uv run python launcher.py" -ForegroundColor White
}
