# Downloads the newest encrypted copy of the database (artifacts db-copy-* / db-daily-* made by the GitHub runs while Turso is
# blocked, until 2026-11-01) and decrypts it into data\algowinbet.db, read by the local site (launch configuration web-offline).
# The encrypted file always lands in data\dl\algowinbet.db.gz.gpg, overwriting the previous one.
# gpg asks for the backup passphrase (the BACKUP_PASSPHRASE secret) in this terminal; it is never stored.
# Run from anywhere:  powershell -ExecutionPolicy Bypass -File scripts\aggiorna-copia.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$dl = Join-Path $root "data\dl"
$tmp = Join-Path $dl "tmp"
$bash = "C:\Users\asarchione\AppData\Local\Programs\Git\usr\bin\bash.exe"
New-Item -ItemType Directory -Force $dl | Out-Null

$jq = '[.artifacts[] | select(.expired==false and (.name|test("^db-(copy|daily)-")))] | sort_by(.created_at) | last | "\(.workflow_run.id) \(.name) \(.created_at)"'
$newest = gh api "repos/AntonioSarchione/AlgoWinBet/actions/artifacts?per_page=50" --jq $jq
if (-not $newest -or $newest -eq "null") { Write-Host "Nessuna copia disponibile su GitHub."; exit 1 }
$run, $name, $created = $newest -split " "
Write-Host "Copia piu recente: $name (creata $created UTC)"

if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
gh run download $run -n $name -D $tmp
if ($LASTEXITCODE -ne 0) { Write-Host "Download non riuscito."; exit 1 }
$file = Get-ChildItem $tmp -Filter *.gpg | Select-Object -First 1
Move-Item -Force $file.FullName (Join-Path $dl "algowinbet.db.gz.gpg")
Remove-Item -Recurse -Force $tmp

Write-Host "Inserisci la passphrase del backup (non si vede mentre scrivi):"
Push-Location $root
try {
  & $bash -lc "gpg --pinentry-mode loopback --decrypt data/dl/algowinbet.db.gz.gpg | gunzip > data/algowinbet.db.new"
  $ok = ($LASTEXITCODE -eq 0) -and ((Get-Item "data\algowinbet.db.new").Length -gt 0)
} finally { Pop-Location }
$new = Join-Path $root "data\algowinbet.db.new"
if (-not $ok) { Remove-Item -Force $new -ErrorAction SilentlyContinue; Write-Host "Decifratura non riuscita (passphrase sbagliata?)."; exit 1 }
try {
  Move-Item -Force $new (Join-Path $root "data\algowinbet.db")
} catch {
  Write-Host "Il sito locale tiene aperto data\algowinbet.db: fermalo, poi rilancia questo script."
  exit 1
}
Write-Host "Fatto: data\algowinbet.db aggiornato ($name). Ricarica la pagina del sito locale."
