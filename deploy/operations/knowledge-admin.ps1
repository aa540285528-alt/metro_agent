[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Command,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
)

$ErrorActionPreference = "Stop"
Import-Module (Join-Path $PSScriptRoot "knowledge-admin-audit.psm1") -Force
$principal = [Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "knowledge administration requires a Windows Administrator operator"
}

$allowed = @("build-and-publish", "rollback", "verify", "status")
if ($Command -notin $allowed) {
    throw "allowed commands: build-and-publish, rollback, verify, status"
}

$operator = $principal.Identity.Name
$dockerArguments = @(
    "compose", "--profile", "knowledge-admin", "run", "--rm", "knowledge-indexer",
    "python", "-m", "metro_agent.tools.knowledge_indexer", $Command
)
$forceReasons = @("indexer-upgrade", "embedding-model-change", "reranker-model-change", "chunker-change", "recovery")
if ($Command -eq "build-and-publish") {
    if ($Arguments.Count -eq 0) {
        $forceReason = $null
    } elseif ($Arguments.Count -eq 2 -and $Arguments[0] -eq "--force-rebuild" -and $Arguments[1] -in $forceReasons) {
        $forceReason = $Arguments[1]
    } else {
        throw "unknown or duplicate knowledge administration parameter"
    }
    if ($null -ne $forceReason) {
        $dockerArguments += @("--force-rebuild", $forceReason)
        $parameterStatus = "force-rebuild"
    } else {
        $parameterStatus = "none"
    }
} elseif ($Arguments.Count -ne 0) {
    throw "unknown or duplicate knowledge administration parameter"
} else {
    $parameterStatus = "none"
}
Write-KnowledgeAdminAuditRecord -Identity $operator -Operation $Command -ParameterStatus $parameterStatus
& docker @dockerArguments
exit $LASTEXITCODE
