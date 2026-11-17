# AGENTS.md

Python clipper for GTA 6 shorts (`clipper/`) and its local server with a dashboard (`automate/`); see README.md for the commands and what each one does, docs/INSTALLATION.md (German) for the owner's install guide.

## Commands
- Install: `uv sync` (Python deps incl. yt-dlp from `pyproject.toml` / `uv.lock`; `--extra stimmen` adds Demucs + PyTorch, and a plain `uv sync` removes it again); system tools (ffmpeg, espeak-ng, whisper.cpp): `uv run python -m clipper setup [--install]`
- Run the server (dashboard on http://localhost:8765): `./start.sh [--port N] [--host H] [--no-browser]` (uv sync keeping the `stimmen` extra when installed, then `python -m automate`); autostart: `uv run python -m automate.autostart install|status|restart|remove`
- Test: `uv run python -m unittest discover -s tests`; the server's only: `uv run python -m unittest discover -s tests -p 'test_automate_*.py'`
- Build/lint: none configured

## Rules
- CI lives in `.github/workflows/ci.yml` and must stay green.
- Never commit with `--no-verify`.
- Never commit `.env`, `data/`, `jobs/`, `exports/` or `logs/` (`data/automate.db` holds API keys and OAuth tokens in plain text; the rest are secrets and large media; all in `.gitignore`).
- docs/INSTALLATION.md names commands, flags and dashboard labels: change it with them.
