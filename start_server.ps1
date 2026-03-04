# Start the Content Publishing Agent
Try {
    py app.py
} Catch {
    Write-Host "Error: Could not start the server using 'py'. Ensure Python is installed." -ForegroundColor Red
    Pause
}
