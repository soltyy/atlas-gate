[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$ProjectRoot,
    [Parameter(Mandatory=$true)][string]$PythonPath,
    [Parameter(Mandatory=$true)][string]$WinSWPath,
    [Parameter(Mandatory=$true)][ValidatePattern('^[a-fA-F0-9]{64}$')][string]$WinSWSha256,
    [Parameter(Mandatory=$true)][string]$ServiceDirectory,
    [ValidateSet('Prepare','Install')][string]$Mode = 'Prepare',
    [ValidateSet('LocalService','NetworkService','LocalSystem')][string]$Account = 'LocalService',
    [ValidatePattern('^[A-Za-z][A-Za-z0-9_-]{0,50}$')][string]$Name = 'AtlasGate'
)
$ErrorActionPreference = 'Stop'
$taskProject = (Resolve-Path -LiteralPath $ProjectRoot).Path
$taskPython = (Resolve-Path -LiteralPath $PythonPath).Path
$taskWrapper = (Resolve-Path -LiteralPath $WinSWPath).Path
$taskService = [IO.Path]::GetFullPath($ServiceDirectory)
if (-not (Test-Path -LiteralPath (Join-Path $taskProject 'atlas_gate/main.py'))) { throw 'Не найден проект Atlas Gate' }
if ((Get-FileHash -LiteralPath $taskWrapper -Algorithm SHA256).Hash -ne $WinSWSha256) { throw 'SHA-256 WinSW не совпадает' }
if (Get-Service -Name $Name -ErrorAction SilentlyContinue) { throw 'Служба уже существует; обновление выполнять по плану миграции' }
if (Test-Path -LiteralPath (Join-Path $taskService "$Name.xml")) { throw 'Существующая конфигурация службы не затирается' }
New-Item -ItemType Directory -Path $taskService -Force | Out-Null
Copy-Item -LiteralPath $taskWrapper -Destination (Join-Path $taskService "$Name.exe")
$accountName = switch ($Account) { 'LocalService' { 'NT AUTHORITY\LocalService' }; 'NetworkService' { 'NT AUTHORITY\NetworkService' }; default { 'LocalSystem' } }
function XmlEscape([string]$Value) { [Security.SecurityElement]::Escape($Value) }
$xml = @"
<service>
  <id>$(XmlEscape $Name)</id>
  <name>$(XmlEscape $Name)</name>
  <description>Централизованный Atlas Gate, без SDK credentials</description>
  <executable>$(XmlEscape $taskPython)</executable>
  <arguments>-m atlas_gate.main</arguments>
  <workingdirectory>$(XmlEscape $taskProject)</workingdirectory>
  <serviceaccount><username>$(XmlEscape $accountName)</username></serviceaccount>
  <logpath>$(XmlEscape (Join-Path $taskService 'logs'))</logpath>
  <log mode="roll-by-size"><sizeThreshold>10240</sizeThreshold><keepFiles>5</keepFiles></log>
  <stoptimeout>45sec</stoptimeout>
  <onfailure action="restart" delay="10sec" />
  <resetfailure>1hour</resetfailure>
</service>
"@
$xmlPath = Join-Path $taskService "$Name.xml"
[IO.File]::WriteAllText($xmlPath, $xml, [Text.UTF8Encoding]::new($false))
$null = [xml](Get-Content -LiteralPath $xmlPath -Raw)
Write-Output "Подготовлена служба $Name в $taskService; Gate и Router используют разные службы и каталоги данных."
if ($Mode -eq 'Install') {
    & (Join-Path $taskService "$Name.exe") install
    if ($LASTEXITCODE -ne 0) { throw 'WinSW install завершился ошибкой' }
    Write-Output 'Служба установлена; не запущена. Проверьте .env, права service account и свободные порты перед start.'
}
