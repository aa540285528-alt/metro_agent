[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Command,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$IndexerArguments
)

$ErrorActionPreference = "Stop"
if ($env:METRO_AGENT_KNOWLEDGE_ADMIN -ne "1") {
    throw "knowledge administration requires METRO_AGENT_KNOWLEDGE_ADMIN=1"
}

$allowed = @("build-and-publish", "rollback", "verify", "status")
if ($Command -notin $allowed) {
    throw "allowed commands: build-and-publish, rollback, verify, status"
}

$operator = if ($env:USERNAME) { $env:USERNAME } else { "unknown" }
$dockerArguments = @("compose", "--profile", "knowledge-admin", "run", "--rm", "knowledge-indexer", $Command)
if ($Command -in @("build-and-publish", "rollback")) {
    $dockerArguments += @("--operator-assertion", $operator)
}
$dockerArguments += $IndexerArguments
& docker @dockerArguments
exit $LASTEXITCODE
