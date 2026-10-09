# One-time database setup for the MacGear Rebate Master.
#
# Creates:
#   - login role `mgrm_owner` (owns the databases and their structure; used only by `migrate`)
#   - login role `mgrm_app`   (the platform's own account: reads and writes rows, nothing more)
#   - database `mgrm`      (the live rebate master)
#   - database `mgrm_test` (used only by the test suite; wiped on every test run; leave it off a server)
# and writes the platform's connection settings and session-signing key to `.env`, and the
# owner's to `.env.owner`, readable only by this Windows account, administrators and the system.
#
# Separate from the forecasting platform's accounts and databases on purpose:
# neither platform can read or change the other's data directly.
#
# Both passwords are generated at random and written only to those files.
# psql will ask for the `postgres` superuser password; that is not stored anywhere.
# Safe to re-run: existing roles and databases are kept, and an existing .env is not overwritten.
# Then run: .venv\Scripts\python.exe -m mgrm migrate

$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $projectRoot '.env'
$ownerFile = Join-Path $projectRoot '.env.owner'
$psql = Get-ChildItem 'C:\Program Files\PostgreSQL\*\bin\psql.exe' |
    Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
if (-not $psql) { throw 'psql.exe not found under C:\Program Files\PostgreSQL. Is PostgreSQL installed?' }

if ((Test-Path $envFile) -or (Test-Path $ownerFile)) {
    throw ".env or .env.owner already exists at $projectRoot. Setup has already run."
}

# Letters and digits only, so the passwords need no quoting in SQL or in a URL.
$chars = [char[]]'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789'
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
function New-Password {
    $bytes = New-Object byte[] 32
    $rng.GetBytes($bytes)
    -join ($bytes | ForEach-Object { $chars[$_ % $chars.Length] })
}
$password = New-Password
$ownerPassword = New-Password

$sql = @"
SELECT 'CREATE ROLE mgrm_owner LOGIN PASSWORD ''$ownerPassword'''
  WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mgrm_owner')\gexec
ALTER ROLE mgrm_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '$ownerPassword';
SELECT 'CREATE ROLE mgrm_app LOGIN PASSWORD ''$password'''
  WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mgrm_app')\gexec
ALTER ROLE mgrm_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '$password';
SELECT 'CREATE DATABASE mgrm OWNER mgrm_owner'
  WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'mgrm')\gexec
SELECT 'CREATE DATABASE mgrm_test OWNER mgrm_owner'
  WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'mgrm_test')\gexec
REVOKE ALL ON DATABASE mgrm FROM PUBLIC;
REVOKE ALL ON DATABASE mgrm_test FROM PUBLIC;
GRANT CONNECT ON DATABASE mgrm TO mgrm_app;
GRANT CONNECT ON DATABASE mgrm_test TO mgrm_app;
"@

Write-Host 'Connecting to PostgreSQL as the postgres superuser.'
Write-Host 'Enter the postgres password (the one you set with reset_postgres_password.ps1).'
$sql | & $psql -U postgres -h localhost -p 5432 -d postgres -v ON_ERROR_STOP=1 -q
if ($LASTEXITCODE -ne 0) { throw "psql failed with exit code $LASTEXITCODE. Nothing was written to .env." }

$keyBytes = New-Object byte[] 48
$rng.GetBytes($keyBytes)
$secretKey = [Convert]::ToBase64String($keyBytes).Replace('+', '-').Replace('/', '_').TrimEnd('=')

@"
# Created by scripts/setup_database.ps1 on $(Get-Date -Format 'yyyy-MM-dd HH:mm').
# Holds the rebate master database password. Never commit, email or share this file.
DATABASE_URL=postgresql+psycopg://mgrm_app:$password@localhost:5432/mgrm
TEST_DATABASE_URL=postgresql+psycopg://mgrm_app:$password@localhost:5432/mgrm_test
SECRET_KEY=$secretKey
"@ | Out-File -FilePath $envFile -Encoding utf8

@"
# Created by scripts/setup_database.ps1 on $(Get-Date -Format 'yyyy-MM-dd HH:mm').
# The database OWNER account, used only by `python -m mgrm migrate` (and the test suite's set-up).
# The platform itself never reads this file. Never commit, email or share it.
OWNER_DATABASE_URL=postgresql+psycopg://mgrm_owner:$ownerPassword@localhost:5432/mgrm
TEST_OWNER_DATABASE_URL=postgresql+psycopg://mgrm_owner:$ownerPassword@localhost:5432/mgrm_test
"@ | Out-File -FilePath $ownerFile -Encoding utf8

# Only this Windows account, administrators and the system account can read the secrets files.
foreach ($file in @($envFile, $ownerFile)) {
    icacls $file /inheritance:r /grant:r "$($env:USERDOMAIN)\$($env:USERNAME):(F)" '*S-1-5-32-544:(F)' '*S-1-5-18:(F)' | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Warning "Could not restrict who can read $file. Ask IT to do it." }
}

Write-Host ''
Write-Host 'Done. Databases mgrm and mgrm_test are ready, and .env and .env.owner have been written.'
Write-Host 'Next: .venv\Scripts\python.exe -m mgrm migrate'
