[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$EnvFile,

    [ValidateNotNullOrEmpty()]
    [string]$ProjectName = "metro-agent-pilot"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
    throw "Deployment env file was not found."
}

$envMap = @{}
foreach ($line in Get-Content -LiteralPath $EnvFile) {
    $trimmed = $line.Trim()
    if (-not $trimmed -or $trimmed.StartsWith("#")) {
        continue
    }
    $match = [regex]::Match($trimmed, "^(?<name>[A-Za-z_][A-Za-z0-9_]*)=(?<content>.*)$")
    if (-not $match.Success) {
        throw "Deployment env file contains an invalid variable declaration."
    }
    $envMap[$match.Groups["name"].Value] = $match.Groups["content"].Value.Trim()
}

$required = @(
    "POSTGRES_PASSWORD",
    "AUTH_MYSQL_PASSWORD",
    "MYSQL_ROOT_PASSWORD",
    "AUTH_SESSION_PEPPER",
    "MODEL_DIR",
    "KNOWLEDGE_SOURCE_DIR",
    "KNOWLEDGE_PUBLISHER_INTERNAL_BEARER_SECRET",
    "APP_HOST_PORT"
)
$missing = @(
    $required | Where-Object {
        -not $envMap.ContainsKey($_) -or [string]::IsNullOrWhiteSpace($envMap[$_])
    }
)
if ($missing.Count -gt 0) {
    throw "Missing required deployment variables: $($missing -join ', ')"
}

$hostPort = 0
if (-not [int]::TryParse($envMap["APP_HOST_PORT"], [ref]$hostPort) -or $hostPort -lt 1 -or $hostPort -gt 65535) {
    throw "APP_HOST_PORT must be an integer between 1 and 65535."
}

& docker compose --env-file $EnvFile --project-name $ProjectName config --quiet
if ($LASTEXITCODE -ne 0) {
    throw "Docker Compose configuration validation failed."
}

$configJson = & docker compose --env-file $EnvFile --project-name $ProjectName config --format json
if ($LASTEXITCODE -ne 0) {
    throw "Docker Compose configuration rendering failed."
}
$config = $configJson | ConvertFrom-Json
$ports = @($config.services.app.ports)
$expectedPort = "127.0.0.1:$hostPort:8000"
$loopbackPort = $ports | Where-Object {
    $_.host_ip -eq "127.0.0.1" -and [string]$_.published -eq [string]$hostPort -and $_.target -eq 8000
}
if ($loopbackPort.Count -ne 1 -or $ports.Count -ne 1) {
    throw "App port mapping does not match the required loopback contract: $expectedPort."
}

$healthcheck = [string]::Join(" ", @($config.services.app.healthcheck.test))
if ($healthcheck -notmatch "/api/health" -or $healthcheck -match "/api/ready") {
    throw "App healthcheck must use /api/health only."
}

$projectsJson = & docker compose ls --format json
if ($LASTEXITCODE -ne 0) {
    throw "Unable to inspect Docker Compose projects."
}
$projects = @($projectsJson | ConvertFrom-Json)
if ($projects.name -contains $ProjectName) {
    $servicesJson = & docker compose --env-file $EnvFile --project-name $ProjectName ps --format json
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to inspect deployment container status."
    }
    $services = @($servicesJson | ConvertFrom-Json)
    foreach ($serviceName in "app", "knowledge-read-proxy", "knowledge-publisher") {
        $service = @($services | Where-Object { $_.Service -eq $serviceName })
        if ($service.Count -gt 0 -and $service[0].State -notin "running") {
            throw "Service $serviceName is not running."
        }
    }
    foreach ($migrationName in "db-migrate", "auth-migrate") {
        $migration = @($services | Where-Object { $_.Service -eq $migrationName })
        if ($migration.Count -gt 0 -and ($migration[0].State -ne "exited" -or $migration[0].ExitCode -ne 0)) {
            throw "Migration $migrationName did not exit successfully."
        }
    }
    Write-Output "Compose deployment configuration and observed service state satisfy the contract."
} else {
    Write-Output "Compose configuration satisfies the contract; project is not currently running, so service-state checks were skipped."
}
