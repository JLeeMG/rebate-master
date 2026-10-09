# Resets the password of the PostgreSQL `postgres` superuser on this machine.
#
# Use when the password chosen at installation has been lost.
# Must be run in PowerShell opened with "Run as administrator".
#
# What it does, in order:
#   1. Backs up pg_hba.conf (the file that controls who may log in).
#   2. Temporarily lets `postgres` log in without a password, from this
#      machine only (127.0.0.1 / ::1). Nothing on the network is affected.
#   3. Asks you for a new password (twice) and sets it.
#   4. Puts the original pg_hba.conf back and restarts PostgreSQL.
# Step 4 runs even if step 3 fails, so the database is never left open.

$ErrorActionPreference = 'Stop'

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    throw 'This script must be run in PowerShell opened with "Run as administrator".'
}

$service = Get-Service -Name 'postgresql*' | Select-Object -First 1
if (-not $service) { throw 'No PostgreSQL service found on this machine.' }

# The service's start command names its data folder after -D.
$imagePath = (Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Services\$($service.Name)").ImagePath
if ($imagePath -notmatch '-D\s+"([^"]+)"') { throw "Could not find the data folder in: $imagePath" }
$dataDir = $Matches[1]
$hba = Join-Path $dataDir 'pg_hba.conf'
$backup = Join-Path $dataDir 'pg_hba.conf.before-password-reset'
$psql = Join-Path (Split-Path -Parent (($imagePath -split '"')[1])) 'psql.exe'
if (-not (Test-Path $hba)) { throw "pg_hba.conf not found at $hba" }
if (-not (Test-Path $psql)) { throw "psql.exe not found at $psql" }

$first = Read-Host 'Choose a NEW password for the postgres superuser' -AsSecureString
$second = Read-Host 'Type the same password again' -AsSecureString
$toPlain = { param($s) [Runtime.InteropServices.Marshal]::PtrToStringAuto(
    [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)) }
$newPassword = & $toPlain $first
if ($newPassword -ne (& $toPlain $second)) { throw 'The two passwords did not match. Nothing was changed.' }
if ($newPassword.Length -lt 8) { throw 'Please use at least 8 characters. Nothing was changed.' }

Copy-Item $hba $backup -Force
try {
    $original = [IO.File]::ReadAllText($hba)
    $temporary = "host all postgres 127.0.0.1/32 trust`r`nhost all postgres ::1/128 trust`r`n" + $original
    [IO.File]::WriteAllText($hba, $temporary, (New-Object Text.UTF8Encoding($false)))
    Restart-Service $service.Name
    Start-Sleep -Seconds 3

    $escaped = $newPassword.Replace("'", "''")
    "ALTER USER postgres PASSWORD '$escaped';" |
        & $psql -U postgres -h 127.0.0.1 -p 5432 -d postgres -v ON_ERROR_STOP=1 -q
    if ($LASTEXITCODE -ne 0) { throw "psql failed with exit code $LASTEXITCODE. The password was NOT changed." }
    $changed = $true
}
finally {
    Copy-Item $backup $hba -Force
    Remove-Item $backup -Force
    Restart-Service $service.Name
}

if ($changed) {
    Write-Host ''
    Write-Host 'Done. The postgres password has been changed and normal security is back in place.'
}
