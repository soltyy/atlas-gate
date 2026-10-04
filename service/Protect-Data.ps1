[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$DataDirectory,
      [Parameter(Mandatory=$true)][string]$OperatorIdentity,
      [Parameter(Mandatory=$true)][string]$ServiceIdentity)
$ErrorActionPreference = 'Stop'
$taskData = (Resolve-Path -LiteralPath $DataDirectory).Path
if ($taskData -eq [IO.Path]::GetPathRoot($taskData)) { throw 'Нельзя менять ACL корня диска' }
$acl = [Security.AccessControl.DirectorySecurity]::new()
$acl.SetAccessRuleProtection($true, $false)
foreach ($identity in @($OperatorIdentity, $ServiceIdentity, 'NT AUTHORITY\SYSTEM')) {
    $rule = [Security.AccessControl.FileSystemAccessRule]::new($identity, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
    $acl.AddAccessRule($rule)
}
Set-Acl -LiteralPath $taskData -AclObject $acl
Write-Output "Права каталога данных ограничены оператором, service account и SYSTEM: $taskData"
