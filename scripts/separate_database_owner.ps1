# One-time change: give the rebate master's databases a separate OWNER account.
#
# Before: the platform's own account (mgrm_app) owned every table, so anyone who
# copied its password out of .env could switch off the protections on the audit
# log, the evidence and approved rates.
# After:  a new account, mgrm_owner, owns the databases and tables. The platform
# runs as mgrm_app, which can only read and write rows, never change the
# structure, empty a table, or alter the audit log or evidence.
#
# What it does, in order:
#   1. Creates the login role mgrm_owner with a random password.
#   2. Makes it the owner of databases mgrm and mgrm_test and of everything in them.
#   3. Writes its connection settings to .env.owner (never committed), readable
#      only by you, the administrators group and the system account.
#   Then run `python -m mgrm migrate`, which upgrades the structure as the owner,
#   limits mgrm_app to rows, and checks the result. Until then the platform cannot
#   read its tables, so stop it first and run migrate straight after.
#
# psql asks for the `postgres` superuser password; it is not stored anywhere.
# For a brand-new installation use setup_database.ps1, which does all this itself.

$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $projectRoot '.env'
$ownerFile = Join-Path $projectRoot '.env.owner'
$psql = Get-ChildItem 'C:\Program Files\PostgreSQL\*\bin\psql.exe' |
    Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
if (-not $psql) { throw 'psql.exe not found under C:\Program Files\PostgreSQL. Is PostgreSQL installed?' }
if (-not (Test-Path $envFile)) { throw ".env not found. This script changes an existing installation; for a new one run setup_database.ps1." }
if (Test-Path $ownerFile) { throw ".env.owner already exists, so this has already been done. Nothing was changed." }

# Letters and digits only, so the password needs no quoting in SQL or in a URL.
$chars = [char[]]'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789'
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
$bytes = New-Object byte[] 32
$rng.GetBytes($bytes)
$password = -join ($bytes | ForEach-Object { $chars[$_ % $chars.Length] })

$sql = @"
SELECT 'CREATE ROLE mgrm_owner LOGIN PASSWORD ''$password'''
  WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mgrm_owner')\gexec
ALTER ROLE mgrm_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '$password';
ALTER ROLE mgrm_app NOSUPERUSER NOCREATEDB NOCREATEROLE;
ALTER DATABASE mgrm OWNER TO mgrm_owner;
REVOKE ALL ON DATABASE mgrm FROM PUBLIC;
GRANT CONNECT ON DATABASE mgrm TO mgrm_app;
SELECT 'ALTER DATABASE mgrm_test OWNER TO mgrm_owner' WHERE EXISTS (SELECT FROM pg_database WHERE datname = 'mgrm_test')\gexec
\connect mgrm
REASSIGN OWNED BY mgrm_app TO mgrm_owner;
SELECT EXISTS (SELECT FROM pg_database WHERE datname = 'mgrm_test') AS has_test \gset
\if :has_test
\connect mgrm_test
REASSIGN OWNED BY mgrm_app TO mgrm_owner;
REVOKE ALL ON DATABASE mgrm_test FROM PUBLIC;
GRANT CONNECT ON DATABASE mgrm_test TO mgrm_app;
\endif
"@

Write-Host 'Connecting to PostgreSQL as the postgres superuser.'
Write-Host 'Enter the postgres password (the one you set with reset_postgres_password.ps1).'
$sql | & $psql -U postgres -h localhost -p 5432 -d postgres -v ON_ERROR_STOP=1 -q
if ($LASTEXITCODE -ne 0) { throw "psql failed with exit code $LASTEXITCODE. Nothing was written to .env.owner; it is safe to run this again." }

@"
# Created by scripts/separate_database_owner.ps1 on $(Get-Date -Format 'yyyy-MM-dd HH:mm').
# The database OWNER account, used only by `python -m mgrm migrate` (and the test suite's set-up).
# The platform itself never reads this file. Never commit, email or share it.
OWNER_DATABASE_URL=postgresql+psycopg://mgrm_owner:$password@localhost:5432/mgrm
TEST_OWNER_DATABASE_URL=postgresql+psycopg://mgrm_owner:$password@localhost:5432/mgrm_test
"@ | Out-File -FilePath $ownerFile -Encoding utf8

# Only this Windows account, administrators and the system account can read the secrets files.
foreach ($file in @($envFile, $ownerFile)) {
    icacls $file /inheritance:r /grant:r "$($env:USERDOMAIN)\$($env:USERNAME):(F)" '*S-1-5-32-544:(F)' '*S-1-5-18:(F)' | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Warning "Could not restrict who can read $file. Ask IT to do it." }
}

Write-Host ''
Write-Host 'Done. The owner account is in place and .env.owner has been written.'
Write-Host 'Next: .venv\Scripts\python.exe -m mgrm migrate   (then restart the rebate master)'
