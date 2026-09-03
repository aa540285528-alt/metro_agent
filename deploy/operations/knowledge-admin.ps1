[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Command,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
)

$ErrorActionPreference = "Stop"
$principal = [Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "knowledge administration requires a Windows Administrator operator"
}

$allowed = @("build-and-publish", "rollback", "verify", "status")
if ($Command -notin $allowed) {
    throw "allowed commands: build-and-publish, rollback, verify, status"
}

function Write-KnowledgeAdminAuditRecord {
    param(
        [Parameter(Mandatory = $true)][string]$Identity,
        [Parameter(Mandatory = $true)][string]$Operation,
        [Parameter(Mandatory = $true)][string]$ParameterStatus
    )

    $auditDirectory = Join-Path ([Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)) "MetroAgent\knowledge-admin"
    $auditFile = Join-Path $auditDirectory "audit.log"
    if (Test-Path $auditDirectory -and ((Get-Item -Force $auditDirectory).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "knowledge administration audit path is unsafe"
    }
    New-Item -ItemType Directory -Force -Path $auditDirectory | Out-Null
    $administrators = New-Object Security.Principal.SecurityIdentifier([Security.Principal.WellKnownSidType]::BuiltinAdministratorsSid, $null)
    $system = New-Object Security.Principal.SecurityIdentifier([Security.Principal.WellKnownSidType]::LocalSystemSid, $null)
    $directoryAcl = New-Object Security.AccessControl.DirectorySecurity
    $directoryAcl.SetAccessRuleProtection($true, $false)
    $inheritance = [Security.AccessControl.InheritanceFlags]"ContainerInherit, ObjectInherit"
    foreach ($identity in @($administrators, $system)) {
        $directoryAcl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($identity, "FullControl", $inheritance, "None", "Allow")))
    }
    Set-Acl -Path $auditDirectory -AclObject $directoryAcl
    if (Test-Path $auditFile -and ((Get-Item -Force $auditFile).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "knowledge administration audit path is unsafe"
    }
    New-Item -ItemType File -Force -Path $auditFile | Out-Null
    $fileAcl = New-Object Security.AccessControl.FileSecurity
    $fileAcl.SetAccessRuleProtection($true, $false)
    foreach ($identity in @($administrators, $system)) {
        $fileAcl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($identity, "FullControl", "Allow")))
    }
    Set-Acl -Path $auditFile -AclObject $fileAcl
    $record = [ordered]@{
        timestamp_utc = [DateTime]::UtcNow.ToString("O")
        operator_identity = $Identity
        host = [Environment]::MachineName
        command = $Operation
        parameter_status = $ParameterStatus
    } | ConvertTo-Json -Compress
    Add-Content -LiteralPath $auditFile -Value $record -Encoding utf8
}

$operator = $principal.Identity.Name
$dockerArguments = @("compose", "--profile", "knowledge-admin", "run", "--rm", "knowledge-indexer", $Command)
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
