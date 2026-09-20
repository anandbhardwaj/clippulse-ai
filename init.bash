# 1. Create the directory tree structure
mkdir -p src tests output temp .github/workflows .vscode

# 2. Create the empty module files
touch src/__init__.py tests/__init__.py
touch src/config.py src/ingestion.py src/transcriber.py src/reasoner.py src/player.py
touch main.py README.md .env.example

# 3. Create .gitignore
cat << 'EOF' > .gitignore
__pycache__/
*.py[cod]
.venv/
.env
temp/
output/
*.mp4
*.mp3
.pytest_cache/
EOF

# 4. Create .env.example
cat << 'EOF' > .env.example
GROQ_API_KEY=gsk_your_groq_api_key_here
ANTHROPIC_API_KEY=sk-ant-your_anthropic_key_here
GEMINI_API_KEY=your_gemini_api_key_here
EOF

# 5. Commit and push directly to GitHub
git add .
git commit -m "chore: scaffold project structure, backlog templates, and ignore rules"
git push origin main