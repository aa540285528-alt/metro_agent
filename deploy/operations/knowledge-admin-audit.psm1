Set-StrictMode -Version Latest

function Assert-KnowledgeAdminAuditAncestors {
    param([Parameter(Mandatory = $true)][string]$Path)

    $current = [IO.Path]::GetFullPath($Path)
    while ($true) {
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "knowledge administration audit path is unsafe"
            }
        }
        $parent = [IO.Directory]::GetParent($current)
        if ($null -eq $parent) {
            return
        }
        $current = $parent.FullName
    }
}

function Assert-KnowledgeAdminAuditAcl {
    param([Parameter(Mandatory = $true)][string]$Path)

    $administrators = New-Object Security.Principal.SecurityIdentifier([Security.Principal.WellKnownSidType]::BuiltinAdministratorsSid, $null)
    $system = New-Object Security.Principal.SecurityIdentifier([Security.Principal.WellKnownSidType]::LocalSystemSid, $null)
    $expected = @($administrators.Value, $system.Value)
    $acl = Get-Acl -LiteralPath $Path
    if (-not $acl.AreAccessRulesProtected) {
        throw "knowledge administration audit ACL is unsafe"
    }
    $allowed = @($acl.Access | Where-Object { $_.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow })
    if ($allowed.Count -eq 0) {
        throw "knowledge administration audit ACL is unsafe"
    }
    foreach ($rule in $allowed) {
        $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        if ($sid -notin $expected -or ($rule.FileSystemRights -band [Security.AccessControl.FileSystemRights]::FullControl) -ne [Security.AccessControl.FileSystemRights]::FullControl) {
            throw "knowledge administration audit ACL is unsafe"
        }
    }
    foreach ($sid in $expected) {
        if (-not ($allowed | Where-Object { $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -eq $sid })) {
            throw "knowledge administration audit ACL is unsafe"
        }
    }
}

function New-KnowledgeAdminAuditLine {
    param(
        [Parameter(Mandatory = $true)][string]$Identity,
        [Parameter(Mandatory = $true)][string]$Operation,
        [Parameter(Mandatory = $true)][string]$ParameterStatus
    )

    [ordered]@{
        timestamp_utc = [DateTime]::UtcNow.ToString("O")
        operator_identity = $Identity
        command = $Operation
        parameter_status = $ParameterStatus
    } | ConvertTo-Json -Compress
}

function Write-KnowledgeAdminAuditRecord {
    param(
        [Parameter(Mandatory = $true)][string]$Identity,
        [Parameter(Mandatory = $true)][string]$Operation,
        [Parameter(Mandatory = $true)][string]$ParameterStatus
    )

    $programData = [Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)
    $auditDirectory = Join-Path $programData "MetroAgent\knowledge-admin"
    $auditFile = Join-Path $auditDirectory "audit.log"
    if (-not (Test-Path -LiteralPath $auditDirectory -PathType Container) -or -not (Test-Path -LiteralPath $auditFile -PathType Leaf)) {
        throw "knowledge administration audit path must be pre-provisioned"
    }
    Assert-KnowledgeAdminAuditAncestors -Path $auditFile
    Assert-KnowledgeAdminAuditAcl -Path $auditDirectory
    Assert-KnowledgeAdminAuditAcl -Path $auditFile

    $options = [IO.FileOptions]::WriteThrough -bor [IO.FileOptions]::OpenReparsePoint
    $stream = [IO.FileStream]::new(
        $auditFile,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Write,
        [IO.FileShare]::Read,
        4096,
        $options
    )
    try {
        if ($stream.SafeFileHandle.IsInvalid -or ([IO.File]::GetAttributes($auditFile) -band [IO.FileAttributes]::ReparsePoint)) {
            throw "knowledge administration audit path is unsafe"
        }
        $line = New-KnowledgeAdminAuditLine -Identity $Identity -Operation $Operation -ParameterStatus $ParameterStatus
        $bytes = [Text.UTF8Encoding]::new($false).GetBytes("$line`n")
        $stream.Seek(0, [IO.SeekOrigin]::End) | Out-Null
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    } finally {
        $stream.Dispose()
    }
}

Export-ModuleMember -Function Assert-KnowledgeAdminAuditAncestors, New-KnowledgeAdminAuditLine, Write-KnowledgeAdminAuditRecord
