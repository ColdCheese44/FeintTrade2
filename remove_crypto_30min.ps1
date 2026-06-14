# =====================================================================
#  Remove the temporary 30-minute crypto cycle task
#  RUN AS ADMINISTRATOR:
#    powershell -ExecutionPolicy Bypass -File remove_crypto_30min.ps1
# =====================================================================

$Name = "Trading - Crypto 30min"

$task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
if ($task) {
    Unregister-ScheduledTask -TaskName $Name -Confirm:$false
    Write-Host "  Removed: $Name"
} else {
    Write-Host "  Not found: $Name (already removed or never registered)"
}
