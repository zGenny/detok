from __future__ import annotations

import argparse
import os
import sys

from . import __version__, store, timer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="detok", description="Detok — Keep the Streak. Keeps your TikTok streaks alive.")
    parser.add_argument("--version", action="version", version=f"detok {__version__}")
    commands = parser.add_subparsers(dest="command")

    web = commands.add_parser("web", help="start the web UI (default)")
    web.add_argument("--host", default=os.environ.get("DETOK_HOST", "127.0.0.1"),
                     help="address to listen on (0.0.0.0 for a server: requires DETOK_PASSWORD)")
    web.add_argument("--port", type=int, default=int(os.environ.get("DETOK_PORT", 8765)))
    web.add_argument("--no-browser", action="store_true", help="don't open the browser")

    send = commands.add_parser("send", help="send today's messages (respects the time unless --force)")
    send.add_argument("--force", action="store_true", help="send now, ignoring the time")

    commands.add_parser("login", help="open the browser to log in to TikTok")
    commands.add_parser("setup", help="download Chromium (also done automatically on first use)")

    check = commands.add_parser("check", help="check accounts (name, avatar, whether you can message them)")
    check.add_argument("usernames", nargs="*")

    auto = commands.add_parser("timer", help="turn automatic sending on or off")
    auto.add_argument("action", choices=["on", "off", "status"])

    session = commands.add_parser("session", help="export/import the TikTok session (to run Detok on a server)")
    session.add_argument("action", choices=["export", "import"])
    session.add_argument("file")
    session.add_argument("--delete", action="store_true", help=argparse.SUPPRESS)

    commands.add_parser("status", help="show today's status")
    return parser


def print_status() -> None:
    cfg, state = store.load_config(), store.load_state()
    expiry = store.session_expiry()
    print(f"Data: {store.DATA}")
    print(f"TikTok session: {f'valid until {expiry}' if expiry else 'NOT connected'}")
    print(f"Automatic sending: {timer.status()}  |  send after {cfg['send_after']}")
    for friend in cfg["friends"]:
        username = friend["username"]
        if store.sent_today(state, username):
            mark = "✓"
        elif friend["enabled"]:
            mark = "…"
        else:
            mark = "–"
        error = state["users"].get(username, {}).get("last_error")
        print(f"  {mark} {username:<25} {friend['message'] or '(random)'}{f'  error: {error}' if error else ''}")


def run(args: argparse.Namespace) -> int:
    if args.command in (None, "web"):
        from .web import serve
        serve(getattr(args, "host", "127.0.0.1"), getattr(args, "port", 8765),
              open_browser=not getattr(args, "no_browser", False))
        return 0
    if args.command == "timer":
        if args.action == "on":
            timer.enable()
        elif args.action == "off":
            timer.disable()
        print(timer.status())
        return 0
    if args.command == "status":
        print_status()
        return 0

    from . import sender
    if args.command == "send":
        return sender.send(force=args.force)
    if args.command == "check":
        return sender.check(args.usernames)
    if args.command == "login":
        return sender.login()
    if args.command == "setup":
        return sender.install_browser()
    if args.action == "export":
        return sender.session_export(args.file)
    return sender.session_import(args.file, args.delete)


def main() -> None:
    args = build_parser().parse_args()
    try:
        sys.exit(run(args))
    except store.Busy as e:
        print(e, file=sys.stderr)
        sys.exit(3)


if __name__ == "__main__":
    main()
