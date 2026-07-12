$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$sut = Join-Path (Split-Path -Parent (Split-Path -Parent $here)) "bootstrap.ps1"

# Dot-source the script to load functions without running main
. $sut -FunctionsOnly

Describe "Find-Python312" {
    It "finds Python 3.12 on this machine" {
        $result = Find-Python312
        $result | Should Not BeNullOrEmpty
    }
}

Describe "Test-PythonAvailable" {
    It "returns true when Python 3.12 is available" {
        $result = Test-PythonAvailable
        $result | Should Be $true
    }
}

Describe "Test-UvAvailable" {
    It "returns true when uv is available" {
        $result = Test-UvAvailable
        $result | Should Be $true
    }
}

Describe "Get-UvVersion" {
    It "returns a semver-shaped uv version string" {
        $result = Get-UvVersion
        $result | Should Match "^\d+\.\d+\.\d+"
    }
}

Describe "Get-PythonVersion" {
    It "returns version string matching 3.12.x" {
        $result = Get-PythonVersion
        $result | Should Match "^3\.12\.\d+"
    }
}

Describe "Find-ServiceDirectories" {
    It "discovers pyproject.toml files under services/" {
        $services = Find-ServiceDirectories
        $services.Count | Should BeGreaterThan 5
    }

    It "returns shared/ first among STT providers" {
        $services = Find-ServiceDirectories
        $sttProviders = $services | Where-Object { -not $_.IsCore }
        $sttProviders[0].Name | Should Be "shared"
    }

    It "classifies core vs STT provider services" {
        $services = Find-ServiceDirectories
        $core = $services | Where-Object { $_.IsCore }
        $stt = $services | Where-Object { -not $_.IsCore }
        $core.Count | Should BeGreaterThan 0
        $stt.Count | Should BeGreaterThan 0
    }
}

Describe "Test-OllamaAvailable" {
    It "detects Ollama installation" {
        $result = Test-OllamaAvailable
        $result | Should Be $true
    }
}

Describe "Test-GrepaiAvailable" {
    It "detects grepai installation" {
        $result = Test-GrepaiAvailable
        $result | Should Be $true
    }
}

Describe "Test-JqAvailable" {
    It "detects jq installation" {
        $result = Test-JqAvailable
        $result | Should Be $true
    }
}
