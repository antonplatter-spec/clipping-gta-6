#!/bin/sh
# GTA Clipper – Automatik: bring the Python packages up to date with uv, then run the server (dashboard, scheduler,
# workers) in this process, so Ctrl+C / SIGTERM reach it directly.
#
#   ./start.sh [--stimmen | --ohne-stimmen] [--port 8765] [--host 127.0.0.1] [--no-browser] [--public-url URL]
#
# --stimmen installs the optional Demucs voice separation (PyTorch, several GB on Linux) once; later runs keep it
# because they find it installed (a plain `uv sync` would remove it). --ohne-stimmen removes it again. Every other
# argument goes to `python -m automate`. The autostart service (python -m automate.autostart) runs this script too.
set -eu

cd "$(dirname "$0")"

case "$(uname -s)" in
    MINGW* | MSYS* | CYGWIN*)
        echo "Unter Windows läuft der Clipper in WSL2 (Ubuntu): siehe docs/INSTALLATION.md." >&2
        exit 1 ;;
    Darwin)
        if [ "$(uname -m)" != arm64 ]; then
            echo "Dieser Mac meldet sich als Intel (x86_64). Der Clipper braucht einen Mac mit Apple-Chip (M1 oder" >&2
            echo "neuer); läuft das Terminal unter Rosetta, schalte das in den Terminal-Informationen aus." >&2
            exit 1
        fi ;;
esac

# uv's installer puts uv into ~/.local/bin (older versions: ~/.cargo/bin). A terminal opened before the install, a
# double-click in the Finder and a systemd/launchd service may not have that folder on PATH.
if ! command -v uv >/dev/null 2>&1; then
    for dir in "$HOME/.local/bin" "$HOME/.cargo/bin"; do
        if [ -x "$dir/uv" ]; then
            PATH="$dir:$PATH"
            export PATH
            break
        fi
    done
fi
if ! command -v uv >/dev/null 2>&1; then
    echo "uv fehlt (das Programm, das die Python-Pakete des Clippers installiert). Installieren mit:" >&2
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    echo "Danach ein neues Terminal öffnen und ./start.sh noch einmal starten." >&2
    exit 1
fi

# Our own flags out, the rest stays in "$@" (in order) for the server.
voices=keep
n=$#
while [ "$n" -gt 0 ]; do
    arg=$1
    shift
    n=$((n - 1))
    case $arg in
        --stimmen) voices=on ;;
        --ohne-stimmen) voices=off ;;
        *) set -- "$@" "$arg" ;;
    esac
done
if [ "$voices" = keep ]; then
    if [ -x .venv/bin/demucs ]; then voices=on; else voices=off; fi
fi

for tool in ffmpeg ffprobe; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "Hinweis: $tool fehlt – ohne geht keine Analyse und kein Rendern. Installieren mit" >&2
        if [ "$(uname -s)" = Darwin ]; then echo "  brew install ffmpeg" >&2; else echo "  sudo apt install ffmpeg" >&2; fi
        break
    fi
done

extra=""
if [ "$voices" = on ]; then
    extra="--extra stimmen"
fi
if [ ! -x .venv/bin/python ]; then
    echo "Erster Start: installiere die Python-Pakete (ein paar Minuten) …"
elif [ "$voices" = on ] && [ ! -x .venv/bin/demucs ]; then
    echo "Installiere die Stimmen-Trennung (PyTorch; unter Linux rund 3 GB Download) …"
fi
# Python 3.11–3.13 is what this was tested on (3.10 only gets an old yt-dlp); uv downloads one if the PC has none.
# shellcheck disable=SC2086  # $extra is empty or "--extra stimmen": split on purpose
if ! uv sync --python ">=3.11,<3.14" $extra; then
    echo "" >&2
    echo "Die Python-Pakete ließen sich nicht installieren (Meldung oben). Häufig: keine Internetverbindung oder zu" >&2
    echo "wenig Speicherplatz. Mehr dazu in docs/INSTALLATION.md, Abschnitt Fehlersuche." >&2
    exit 1
fi

exec .venv/bin/python -m automate "$@"
