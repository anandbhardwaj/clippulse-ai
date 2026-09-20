## checkout and setup project
git clone https://github.com/anandbhardwaj/clippulse-ai.git  

cd .\clippulse-ai\  

winget install Gyan.FFmpeg

powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

pip install uv

uv sync

copy .env.example .env

## installing claude
claude


## installing git cli

winget install GitHub.cli

# Restart PowerShell, then authenticate:
gh auth login
