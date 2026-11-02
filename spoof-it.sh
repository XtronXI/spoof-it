#!/bin/bash
# Launch a dummy executable that Discord detects as a game.
#
#   ./spoof-it.sh "Counter-Strike 2"
#   ./spoof-it.sh --list counter
#   ./spoof-it.sh --update-db
#
# Run ./spoof-it.sh --help for all options.

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)

# --help, --list, --dry-run and --update-db only need Python; launching needs gcc too.
needs_gcc=1
for arg in "$@"; do
    case "$arg" in
        --)
            break
            ;;
        -h|--help|-l*|--list|--list=*|-n|--dry-run|--update-db)
            needs_gcc=0
            ;;
    esac
done

missing=()
if ! command -v python3 >/dev/null 2>&1; then
    missing+=("python3")
fi
if [ "$needs_gcc" -eq 1 ] && ! command -v gcc >/dev/null 2>&1; then
    missing+=("gcc")
fi

if [ "${#missing[@]}" -gt 0 ]; then
    echo "Missing required command(s): ${missing[*]}" >&2
    if [ "$needs_gcc" -eq 1 ]; then
        echo "spoof-it needs Python 3 to run and gcc to build the dummy executable." >&2
    else
        echo "spoof-it needs Python 3 to run." >&2
    fi

    install_command=""
    if command -v apt-get >/dev/null 2>&1; then
        install_command="apt-get update && apt-get install -y python3 gcc"
    elif command -v dnf >/dev/null 2>&1; then
        install_command="dnf install -y python3 gcc"
    elif command -v pacman >/dev/null 2>&1; then
        install_command="pacman -S --needed --noconfirm python gcc"
    elif command -v zypper >/dev/null 2>&1; then
        install_command="zypper --non-interactive install python3 gcc"
    elif command -v apk >/dev/null 2>&1; then
        install_command="apk add python3 gcc"
    elif command -v xbps-install >/dev/null 2>&1; then
        install_command="xbps-install -y python3 gcc"
    elif command -v eopkg >/dev/null 2>&1; then
        install_command="eopkg install -y python3 gcc"
    fi

    if [ -z "$install_command" ]; then
        echo "Could not detect a supported package manager. Install the missing commands manually." >&2
        exit 1
    fi

    echo "Detected package manager command:" >&2
    echo "  $install_command" >&2
    if [ "$(id -u)" -ne 0 ]; then
        echo "  (run it with sudo, or let this script run it for you)" >&2
    fi

    if [ ! -t 0 ]; then
        echo "Run the command above, then try again." >&2
        exit 1
    fi

    if ! read -r -p "Install the missing dependencies now? [y/N] " answer; then
        echo >&2
        echo "Not installing. Run the command above, then try again." >&2
        exit 1
    fi

    case "$answer" in
        [yY]|[yY][eE][sS]) ;;
        *)
            echo "Not installing. Run the command above, then try again." >&2
            exit 1
            ;;
    esac

    if [ "$(id -u)" -eq 0 ]; then
        if ! bash -c "$install_command"; then
            echo "Dependency installation failed." >&2
            exit 1
        fi
    elif command -v sudo >/dev/null 2>&1; then
        if ! sudo bash -c "$install_command"; then
            echo "Dependency installation failed." >&2
            exit 1
        fi
    else
        echo "Root privileges are required. Run the command above as root." >&2
        exit 1
    fi

    if ! command -v python3 >/dev/null 2>&1; then
        echo "python3 is still missing after installation." >&2
        exit 1
    fi
    if [ "$needs_gcc" -eq 1 ] && ! command -v gcc >/dev/null 2>&1; then
        echo "gcc is still missing after installation." >&2
        exit 1
    fi
fi

exec python3 "$SCRIPT_DIR/spoof-it.py" "$@"
