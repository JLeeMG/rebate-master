# One-time database setup for the MacGear Rebate Master.
#
# Creates:
#   - a login role `mgrm_app` (the platform's own account, not a superuser)
#   - database `mgrm`      (the live rebate master)
#   - database `mgrm_test` (used only by the test suite; wiped on every test run)
# and writes the connection settings and a session-signing key to `.env` in the project root.
#
# Separate from the forecasting platform's account and databases on purpose:
# neither platform can read or change the other's data directly.
#
# The mgrm_app password is generated at random and written only to .env.
# psql will ask for the `postgres` superuser password; that is not stored anywhere.
# Safe to re-run: existing roles and databases are left alone, and an existing .env is not overwritten.

$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $projectRoot '.env'
$psql = Get-ChildItem 'C:\Program Files\PostgreSQL\*\bin\psql.exe' |
    Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
if (-not $psql) { throw 'psql.exe not found under C:\Program Files\PostgreSQL. Is PostgreSQL installed?' }

if (Test-Path $envFile) {
    throw ".env already exists at $envFile. Setup has already run; delete .env only if you mean to reset the platform account password."
}

# Letters and digits only, so the password needs no quoting in SQL or in a URL.
$chars = [char[]]'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789'
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
$bytes = New-Object byte[] 32
$rng.GetBytes($bytes)
$password = -join ($bytes | ForEach-Object { $chars[$_ % $chars.Length] })

$sql = @"
SELECT 'CREATE ROLE mgrm_app LOGIN PASSWORD ''$password'''
  WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mgrm_app')\gexec
ALTER ROLE mgrm_app LOGIN PASSWORD '$password';
SELECT 'CREATE DATABASE mgrm OWNER mgrm_app'
  WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'mgrm')\gexec
SELECT 'CREATE DATABASE mgrm_test OWNER mgrm_app'
  WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'mgrm_test')\gexec
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

Write-Host ''
Write-Host 'Done. Databases mgrm and mgrm_test are ready, and .env has been written.'
