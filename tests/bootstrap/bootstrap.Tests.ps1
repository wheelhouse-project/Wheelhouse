# Pester 5 test suite for bootstrap.ps1.
#
# Two rules keep this file working, both learned the hard way (wh-bootstrap-pester-broken):
#
# 1. The dot-source MUST stay inside BeforeAll. Pester 5 executes the top level
#    of a test file during its discovery phase, then runs the It blocks in a
#    separate run phase. Functions loaded at the top level do not survive into
#    the run phase, so every test fails with CommandNotFoundException.
#
# 2. Assertions MUST use the hyphenated operator form (Should -Be, not
#    Should Be). Pester 5 removed the unhyphenated Pester 3 syntax. This
#    machine has both Pester 5.7.1 and the Windows-bundled Pester 3.4.0
#    installed, so a file written for version 3 looks correct until version 5
#    is the one that loads.
#
# Several tests below assert facts about the machine they run on (Python 3.12
# is installed, uv is installed, and so on). That is deliberate: bootstrap.ps1
# exists to detect and install development prerequisites, so the tests confirm
# its detection agrees with reality on an already-bootstrapped machine.

BeforeAll {
    $repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
    $script:SutPath = Join-Path $repoRoot "bootstrap.ps1"

    # -FunctionsOnly makes bootstrap.ps1 define its functions and stop, instead
    # of running the three installation phases.
    . $script:SutPath -FunctionsOnly
}

Describe "test harness" {
    It "loaded bootstrap.ps1 into the run phase" {
        # Guards rule 1 above. Without this, a regression to a top-level
        # dot-source shows up as ten confusing CommandNotFoundException
        # failures instead of one clear message.
        Test-Path $script:SutPath | Should -BeTrue
        Get-Command Find-Python312 -ErrorAction SilentlyContinue | Should -Not -BeNullOrEmpty
    }
}

Describe "Find-Python312" {
    It "finds Python 3.12 on this machine" {
        $result = Find-Python312
        $result | Should -Not -BeNullOrEmpty
    }
}

Describe "Test-PythonAvailable" {
    It "returns true when Python 3.12 is available" {
        $result = Test-PythonAvailable
        $result | Should -Be $true
    }
}

Describe "Test-UvAvailable" {
    It "returns true when uv is available" {
        $result = Test-UvAvailable
        $result | Should -Be $true
    }
}

Describe "Get-UvVersion" {
    It "returns a semver-shaped uv version string" {
        $result = Get-UvVersion
        $result | Should -Match "^\d+\.\d+\.\d+"
    }
}

Describe "Get-PythonVersion" {
    It "returns version string matching 3.12.x" {
        $result = Get-PythonVersion
        $result | Should -Match "^3\.12\.\d+"
    }
}

Describe "Find-ServiceDirectories" {
    It "discovers pyproject.toml files under services/" {
        $services = Find-ServiceDirectories
        $services.Count | Should -BeGreaterThan 5
    }

    It "returns shared/ first among STT providers" {
        $services = Find-ServiceDirectories
        $sttProviders = $services | Where-Object { -not $_.IsCore }
        $sttProviders[0].Name | Should -Be "shared"
    }

    It "classifies core vs STT provider services" {
        $services = Find-ServiceDirectories
        $core = $services | Where-Object { $_.IsCore }
        $stt = $services | Where-Object { -not $_.IsCore }
        $core.Count | Should -BeGreaterThan 0
        $stt.Count | Should -BeGreaterThan 0
    }
}

Describe "Test-OllamaAvailable" {
    It "detects Ollama installation" {
        $result = Test-OllamaAvailable
        $result | Should -Be $true
    }
}

Describe "Test-JqAvailable" {
    It "detects jq installation" {
        $result = Test-JqAvailable
        $result | Should -Be $true
    }
}
