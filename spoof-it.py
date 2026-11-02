#!/usr/bin/env python3
"""Spoof a Discord-detectable game with a dummy Linux process.

Discord detects games by looking at the executable name of running processes. This script looks a game up by title (or alias/nickname) in Discord's detectable-games JSON, creates a tiny dummy ELF with the executable name that
Discord expects, and launches it.

The database is ``detectable.json`` next to the script; ``--update-db`` refreshes it. Games with executable entries use the listed executable name. Games with an empty ``executables`` list use the game title as the dummy name; Discord's own name lookup can resolve that to the game, and may count it toward quests.

Usage:
    ./spoof-it.sh "Counter-Strike 2"
    ./spoof-it.sh --list counter
    ./spoof-it.sh --exe PATH
    ./spoof-it.sh "EA Sports FC 27"
    ./spoof-it.sh --update-db
"""

from __future__ import annotations

import argparse
import difflib
import gzip
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent

DEFAULT_DURATION = 31536000  # one year
DEFAULT_BASE_DIR = Path("/tmp")
DEFAULT_DB_NAME = "detectable.json"
DETECTABLE_APPS_URL = "https://discord.com/api/v10/applications/detectable"

# Executables containing these words are tries/helpers rather than the game.
SUSPICIOUS_EXE_WORDS = ("battleye", "easyanticheat", "anticheat", "_be.exe", "be.exe", "dedicated", "server", "launcher", "updater", "update", "install", "setup", "unins", "crash", "report", "helper", "benchmark", "editor", "workshop", "sdk", "config", "diagnostic")

SUSPICIOUS_PATH_WORDS = ("_beta_", "_ptr_", "/beta/", "/ptr/", "/test/","/demo/")

C_SOURCE = r"""
#include <unistd.h>
#include <stdlib.h>

int main(int argc, char *argv[]) {
    int duration = (argc > 1) ? atoi(argv[1]) : 31536000;
    sleep(duration);
    return 0;
}
"""


class SpoofError(Exception):
    """A user-facing error."""


@dataclass(frozen=True)
class Executable:
    name: str
    os_name: str
    launcher: bool


@dataclass(frozen=True)
class Game:
    id: str
    name: str
    aliases: tuple[str, ...]
    executables: tuple[Executable, ...]

    @property
    def detectable(self) -> bool:
        return bool(self.executables)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


def default_db_path() -> Path:
    return SCRIPT_DIR / DEFAULT_DB_NAME


def load_games(path: Path) -> list[Game]:
    try:
        with path.open(encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        raise SpoofError(
            f"game database not found: {path}\n"
            "Run --update-db, or place detectable.json next to the script."
        )
    except json.JSONDecodeError as exc:
        raise SpoofError(f"could not parse {path}: {exc}") from exc

    if isinstance(raw, dict):
        raw = raw.get("games", raw.get("data", []))

    if not isinstance(raw, list):
        raise SpoofError(f"unexpected database format in {path} (expected a list)")

    games: list[Game] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        executables: list[Executable] = []
        for exe in entry.get("executables") or []:
            if not isinstance(exe, dict):
                continue
            name = str(exe.get("name") or "").strip()
            if not name:
                continue
            executables.append(
                Executable(
                    name=name,
                    os_name=str(exe.get("os") or "").casefold(),
                    launcher=bool(exe.get("is_launcher")),
                )
            )
        games.append(
            Game(
                id=str(entry.get("id") or ""),
                name=str(entry.get("name") or "").strip(),
                aliases=tuple(str(alias) for alias in entry.get("aliases") or []),
                executables=tuple(executables),
            )
        )
    return games


def compact_game(entry: dict) -> Optional[dict]:
    name = str(entry.get("name") or "").strip()
    if not name:
        return None

    game: dict = {}
    game_id = entry.get("id")
    if game_id:
        game["id"] = str(game_id)
    game["name"] = name

    aliases = []
    for alias in entry.get("aliases") or []:
        if alias is None:
            continue
        value = str(alias).strip()
        if value:
            aliases.append(value)
    if aliases:
        game["aliases"] = aliases

    executables = []
    for exe in entry.get("executables") or []:
        if not isinstance(exe, dict):
            continue
        exe_name = str(exe.get("name") or "").strip()
        if not exe_name:
            continue
        compact_exe = {"name": exe_name}
        os_name = str(exe.get("os") or "").strip()
        if os_name:
            compact_exe["os"] = os_name
        if exe.get("is_launcher"):
            compact_exe["is_launcher"] = True
        executables.append(compact_exe)
    if executables:
        game["executables"] = executables

    return game


def update_database(target: Path, dry_run: bool = False) -> int:
    print(f"[+] Downloading {DETECTABLE_APPS_URL}")

    request = urllib.request.Request(
        DETECTABLE_APPS_URL,
        headers={
            "User-Agent": "spoof-it/1.0",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = response.read()
            if response.headers.get("Content-Encoding", "").casefold() == "gzip":
                payload = gzip.decompress(payload)
    except urllib.error.HTTPError as exc:
        raise SpoofError(
            f"could not download {DETECTABLE_APPS_URL}: HTTP {exc.code} {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise SpoofError(f"could not download {DETECTABLE_APPS_URL}: {exc.reason}") from exc
    except OSError as exc:
        raise SpoofError(f"could not download {DETECTABLE_APPS_URL}: {exc}") from exc

    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise SpoofError(f"Discord returned invalid JSON: {exc}") from exc

    if isinstance(raw, dict):
        raw = raw.get("games", raw.get("data", []))
    if not isinstance(raw, list):
        raise SpoofError("unexpected response from Discord: expected a list of games")

    compact = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        game = compact_game(entry)
        if game is not None:
            compact.append(game)

    if not compact:
        raise SpoofError("Discord returned an empty game list; refusing to overwrite the database")

    data = (
        json.dumps(compact, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")

    if dry_run:
        print(f"[dry-run] would write {target} ({len(compact)} games, {len(data)} bytes)")
        return 0

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".spoof-it-db-",
            suffix=".json",
            dir=str(target.parent),
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.chmod(tmp_name, 0o644)
            os.replace(tmp_name, target)
        finally:
            Path(tmp_name).unlink(missing_ok=True)
    except OSError as exc:
        raise SpoofError(f"could not write {target}: {exc}") from exc

    print(f"[+] Updated {target} ({len(compact)} games, {len(data)} bytes)")
    return 0


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()
    text = re.sub(r"[\W_]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def acronym(text: str) -> str:
    return "".join(word[0] for word in normalize(text).split() if word)


def clean_exe_path(name: str) -> str:
    name = name.strip().replace("\\", "/")
    name = name.lstrip(">").strip()
    name = name.rstrip('"').strip()
    parts = [part for part in name.split("/") if part not in ("", ".", "..")]
    return "/".join(parts)


def resolve_custom_executable(value: str) -> Path:
    raw = value.strip()
    if not raw:
        raise SpoofError("--exe needs a path")
    try:
        return Path(raw).expanduser().resolve()
    except OSError as exc:
        raise SpoofError(f"invalid --exe path: {value!r}: {exc}") from exc


def exe_stem(name: str) -> str:
    base = clean_exe_path(name).rsplit("/", 1)[-1]
    stem, _ = os.path.splitext(base)
    return stem or base


def safe_dummy_name(name: str) -> str:
    value = re.sub(r"[/\\\x00-\x1f]", "-", name)
    value = " ".join(value.split())
    value = value.strip(" .")
    return value or "spoof-it-game"


def dummy_filename(name: str) -> str:
    safe = safe_dummy_name(name)
    return safe if safe.casefold().endswith(".exe") else f"{safe}.exe"


def name_only_label(game: Game, games: Iterable[Game]) -> tuple[str, bool]:
    target = game.id or game.name
    index: dict[str, set[str]] = {}
    for candidate in games:
        for label in (candidate.name, *candidate.aliases):
            key = normalize(label)
            if key:
                index.setdefault(key, set()).add(candidate.id or candidate.name)
    for label in (game.name, *game.aliases):
        key = normalize(label)
        if key and index.get(key) == {target}:
            return label, True
    return game.name, False


def executable_frequencies(games: Iterable[Game]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for game in games:
        for exe in game.executables:
            base = clean_exe_path(exe.name).rsplit("/", 1)[-1].casefold()
            if base:
                counts[base] = counts.get(base, 0) + 1
    return counts


def text_score(query: str, candidate: str) -> float:
    if not query or not candidate:
        return 0.0
    if candidate == query:
        return 100.0
    q_compact = query.replace(" ", "")
    c_compact = candidate.replace(" ", "")
    if c_compact == q_compact:
        return 98.0
    if candidate.startswith(query) or (
        len(q_compact) >= 3 and c_compact.startswith(q_compact)
    ):
        return 86.0
    if query in candidate or (
        len(q_compact) >= 3 and q_compact in c_compact
    ):
        return 74.0
    return difflib.SequenceMatcher(None, query, candidate).ratio() * 60.0


def score_game(query: str, game: Game) -> float:
    q = normalize(query)
    if not q:
        return 0.0
    q_compact = q.replace(" ", "")
    best = 0.0

    for label in (game.name, *game.aliases):
        best = max(best, text_score(q, normalize(label)))
        ac = acronym(label)
        if ac:
            if ac == q_compact:
                best = max(best, 95.0)
            elif len(q_compact) >= 3 and ac.startswith(q_compact):
                best = max(best, 78.0)

    for exe in game.executables:
        stem = exe_stem(exe.name)
        best = max(best, text_score(q, normalize(stem)) - 3.0)
        ac = acronym(stem)
        if ac:
            if ac == q_compact:
                best = max(best, 93.0)
            elif len(q_compact) >= 3 and ac.startswith(q_compact):
                best = max(best, 76.0)
    return best


def find_matches(games: Iterable[Game], query: str, minimum: float = 55.0):
    scored = []
    for game in games:
        value = score_game(query, game)
        if value >= minimum:
            scored.append((value, game))
    scored.sort(key=lambda item: (-item[0], not item[1].detectable, item[1].name.casefold()))
    return scored


def choose_game(
    matches,
    query: str,
    assume_yes: bool,
    exe_counts: Optional[dict[str, int]] = None) -> Game:
    if not matches:
        raise SpoofError("no game matched that name")

    best_score = matches[0][0]
    top = [game for score, game in matches if score >= best_score - 0.51]
    if len({game.id or game.name for game in top}) == 1:
        return top[0]

    if len(matches) > 1 and best_score - matches[1][0] >= 8:
        return matches[0][1]

    if assume_yes:
        print(
            f"[!] Several games matched; --yes picked {matches[0][1].name!r}.",
            file=sys.stderr,
        )
        return matches[0][1]

    candidates = []
    seen = set()
    for score, game in matches:
        if score < best_score - 20:
            break
        key = (game.id, game.name)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(game)
        if len(candidates) == 10:
            break

    print(f'Multiple games match "{query}":', file=sys.stderr)
    for index, game in enumerate(candidates, 1):
        print(f"  {index:2}. {describe_game(game, exe_counts)}", file=sys.stderr)

    if not sys.stdin.isatty():
        raise SpoofError(
            "several games match; use a more specific name, --list, or --yes"
        )

    while True:
        answer = input(f"Select a game [1-{len(candidates)}] (Enter = 1, q = quit): ").strip()
        if answer.casefold() in ("q", "quit"):
            raise SpoofError("cancelled")
        if not answer:
            return candidates[0]
        if answer.isdigit() and 1 <= int(answer) <= len(candidates):
            return candidates[int(answer) - 1]
        print("Enter one of the numbers shown.", file=sys.stderr)


def describe_game(game: Game, exe_counts: Optional[dict[str, int]] = None) -> str:
    if not game.executables:
        return f"{game.name}  [no executable - will use title name]"
    exe = best_executable(game, exe_counts)
    path = clean_exe_path(exe.name)
    return (
        f"{game.name}  [{path}, {exe.os_name or 'unknown'} os]"
        f"  ({len(game.executables)} executable{'s' if len(game.executables) != 1 else ''})"
    )


# ---------------------------------------------------------------------------
# Executable selection
# ---------------------------------------------------------------------------


def score_executable(exe: Executable, game: Game, exe_counts: Optional[dict[str, int]] = None) -> float:
    rel = clean_exe_path(exe.name)
    base = rel.rsplit("/", 1)[-1].casefold()
    stem = exe_stem(rel)
    score = 0.0

    titles = [normalize(game.name)] + [normalize(alias) for alias in game.aliases]
    for title in titles:
        if not title:
            continue
        normalized_stem = normalize(stem)
        if normalized_stem == title:
            score += 60.0
        elif title.startswith(normalized_stem) or normalized_stem.startswith(title):
            score += 30.0
        elif title in normalized_stem or normalized_stem in title:
            score += 18.0
        else:
            score += difflib.SequenceMatcher(None, normalized_stem, title).ratio() * 15.0

    if exe.os_name == "linux":
        score += 12.0
    elif exe.os_name == "win32":
        score += 6.0

    score -= 6.0 * rel.count("/")

    if exe_counts:
        shared = exe_counts.get(base, 1)
        score -= min(max(shared - 1, 0), 10) * 8.0

    if any(word in base for word in SUSPICIOUS_EXE_WORDS) or any(word in rel.casefold() for word in SUSPICIOUS_PATH_WORDS):
        score -= 50.0

    return score


def best_executable(game: Game, exe_counts: Optional[dict[str, int]] = None) -> Executable:
    available = [exe for exe in game.executables if clean_exe_path(exe.name)]
    if not available:
        raise SpoofError(f"{game.name!r} has no usable executables in the database")
    real = [exe for exe in available if not exe.launcher] or available
    return max(real, key=lambda exe: score_executable(exe, game, exe_counts))


# ---------------------------------------------------------------------------
# Building and launching
# ---------------------------------------------------------------------------


def compile_dummy(target: Path) -> None:
    if shutil.which("gcc") is None:
        raise SpoofError("gcc is required to build the dummy executable. Install it with your package manager, then try again.")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        build_dir = Path(tempfile.mkdtemp(prefix=".spoof-it-", dir=str(target.parent)))
    except OSError as exc:
        raise SpoofError(f"could not create a build directory for {target}: {exc}") from exc

    source_fd, source_name = tempfile.mkstemp(prefix="spoof-it-", suffix=".c")
    source = Path(source_name)
    built = build_dir / "dummy"
    try:
        with os.fdopen(source_fd, "w", encoding="utf-8") as handle:
            handle.write(C_SOURCE)

        result = subprocess.run(
            ["gcc", str(source), "-O2", "-o", str(built)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise SpoofError("gcc failed to build the dummy executable:\n" + result.stderr.strip())

        os.chmod(built, 0o755)
        os.replace(built, target)
    except OSError as exc:
        raise SpoofError(f"could not build {target}: {exc}") from exc
    finally:
        shutil.rmtree(build_dir, ignore_errors=True)
        source.unlink(missing_ok=True)


def stop_existing(targets: Sequence[Path]) -> None:
    wanted = {str(target) for target in targets}
    try:
        processes = list(Path("/proc").iterdir())
    except OSError:
        return
    for entry in processes:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == os.getpid():
            continue
        try:
            exe = os.readlink(entry / "exe")
        except OSError:
            continue
        if exe.endswith(" (deleted)"):
            exe = exe[: -len(" (deleted)")]
        if exe not in wanted:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            continue
        print(f"[i] Stopped previous dummy (PID {pid})")


def launch(target: Path, duration: int) -> int:
    process = subprocess.Popen(
        [str(target), str(duration)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    return process.pid


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spoof-it.sh",
        description=("Launch a dummy executable that Discord detects as a game."),
        epilog=("The game name is fuzzy-matched against Discord's detectable-games list."),
    )
    parser.add_argument(
        "query",
        nargs="*",
        help="game title, alias, or nickname",
    )
    parser.add_argument(
        "-l",
        "--list",
        nargs="?",
        const="",
        metavar="TERM",
        help="list matching games instead of launching one",
    )
    parser.add_argument(
        "-e",
        "--exe",
        metavar="PATH",
        help="use an exact custom executable path instead of a game title",
    )
    parser.add_argument(
        "--update-db",
        action="store_true",
        help="download Discord's detectable-games list and write detectable.json",
    )
    parser.add_argument(
        "-t",
        "--duration",
        type=int,
        default=DEFAULT_DURATION,
        metavar="SECONDS",
        help=f"how long the dummy should live (default: {DEFAULT_DURATION})",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="pick the best match automatically when several games match",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="show what would be launched without building or running it",
    )
    parser.add_argument(
        "-k",
        "--keep",
        action="store_true",
        help="do not stop a previous dummy for the same executable",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        metavar="N",
        help="maximum number of games shown by --list (default: 20)",
    )
    return parser


def run_list(
    games: list[Game],
    term: str,
    limit: int,
    exe_counts: Optional[dict[str, int]] = None) -> int:
    if not term.strip():
        raise SpoofError("--list needs a search term, e.g. --list counter")
    matches = find_matches(games, term)
    if not matches:
        print(f'No game matched "{term}".')
        return 0

    print(f'Games matching "{term}":')
    width = min(max((len(game.name) for _, game in matches[:limit]), default=0), 46)
    for index, (score, game) in enumerate(matches[:limit], 1):
        if game.executables:
            exe = best_executable(game, exe_counts)
            detail = f"{clean_exe_path(exe.name)} [{exe.os_name or 'unknown'}]"
        else:
            detail = "[no executable - will use title name]"
        print(f"  {index:2}. {game.name[:width]:<{width}}  {detail}")

    if len(matches) > limit:
        print(f"  ... {len(matches) - limit} more (use --limit to see more)")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    query = " ".join(args.query).strip()
    games: Optional[list[Game]] = None

    if args.update_db:
        if args.exe is not None or args.list is not None or query:
            raise SpoofError(
                "--update-db cannot be combined with a game title, --exe, or --list"
            )
        return update_database(default_db_path(), dry_run=args.dry_run)

    if args.list is not None:
        if args.exe is not None:
            raise SpoofError("--exe cannot be combined with --list")
        games = load_games(default_db_path())
        exe_counts = executable_frequencies(games)
        return run_list(games, args.list or query, args.limit, exe_counts)

    game: Optional[Game] = None
    if args.exe is not None:
        if query:
            raise SpoofError("--exe cannot be combined with a game title")
        targets = [resolve_custom_executable(args.exe)]
    else:
        if not query:
            raise SpoofError("provide a game title or use --exe PATH")
        games = load_games(default_db_path())
        exe_counts = executable_frequencies(games)
        matches = find_matches(games, query)
        if not matches:
            raise SpoofError(
                f'no game matched "{query}". Try --list {query!r} to browse.'
            )
        game = choose_game(matches, query, args.yes, exe_counts)

        if not game.executables:
            label, unique = name_only_label(game, games)
            if not unique:
                print(
                    f"[!] {game.name!r} shares its name with another game; Discord may resolve a different entry.",
                    file=sys.stderr,
                )
            dummy_name = dummy_filename(label)
            dummy_dir = safe_dummy_name(label)
            relative_paths = [str(Path("spoof-it-steam") / "steamapps" / "common" / dummy_dir / dummy_name)]
            print(
                f"[!] {game.name!r} has no executable in Discord's database.\n"
                "[!] Using the title as the dummy name; Discord may resolve it by name and count quest progress.",
                file=sys.stderr,
            )
        else:
            exe = best_executable(game, exe_counts)
            relative_paths = [clean_exe_path(exe.name)]

        targets = [DEFAULT_BASE_DIR / path for path in dict.fromkeys(relative_paths)]

    if game:
        print(f"[+] Game:       {game.name}")
    else:
        print("[+] Game:       (custom executable)")

    if not args.keep and not args.dry_run:
        stop_existing(targets)

    for target in targets:
        if args.dry_run:
            print(f"[dry-run]      {target}")
            continue
        compile_dummy(target)
        pid = launch(target, args.duration)
        print(f"[+] Executable: {target}")
        print(f"[+] PID:        {pid}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SpoofError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        raise SystemExit(130)
