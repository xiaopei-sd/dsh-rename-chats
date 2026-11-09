"""Minimal client for the local dsh-rename-chats bridge.

The bridge URL is a per-process capability written by the DSH host plugin to
``$DSH_HOME/rename-chats/bridge.json``. This script does not read cookies,
credentials, SQLite, or compressed session logs.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


DEFAULT_TIMEOUT = 15.0


class BridgeError(RuntimeError):
    pass


def default_bridge_file():
    root = Path(os.environ.get("DSH_HOME") or Path.home() / ".dsh")
    return root / "rename-chats" / "bridge.json"


def load_bridge(path):
    explicit = os.environ.get("DSH_RENAME_BRIDGE_URL")
    if explicit:
        return explicit.rstrip("/")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as error:
        raise BridgeError(
            f"bridge file not found: {path}. Install dsh-rename-chats and restart the DSH host."
        ) from error
    except (OSError, json.JSONDecodeError) as error:
        raise BridgeError(f"could not read bridge file {path}: {error}") from error
    base_url = data.get("baseUrl")
    if not isinstance(base_url, str) or not base_url.startswith(("http://127.0.0.1:", "http://localhost:")):
        raise BridgeError("bridge file does not contain a loopback baseUrl; is the plugin mounted?")
    return base_url.rstrip("/")


def request(base_url, method, path, payload=None, query=None, timeout=DEFAULT_TIMEOUT):
    url = base_url + path
    if query:
        url += "?" + urllib.parse.urlencode({key: value for key, value in query.items() if value is not None})
    data = None
    headers = {"accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["content-type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body)
            detail = parsed.get("error", {}).get("message") or body
            code = parsed.get("error", {}).get("code") or f"http-{error.code}"
        except json.JSONDecodeError:
            detail = body
            code = f"http-{error.code}"
        raise BridgeError(f"{code}: {detail}") from error
    except urllib.error.URLError as error:
        raise BridgeError(
            "bridge unavailable; the DSH host may have restarted, or the bridge file is stale. "
            "Restart DSH and retry."
        ) from error
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as error:
        raise BridgeError(f"bridge returned non-JSON content: {body[:200]}") from error
    if not isinstance(parsed, dict) or parsed.get("ok") is not True:
        raise BridgeError(f"bridge refused the request: {parsed}")
    return parsed


def print_json(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge-file", type=Path, default=default_bridge_file())
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("health")

    listing = commands.add_parser("list")
    listing.add_argument("--workspace-id")
    listing.add_argument("--limit", type=int, default=40)
    listing.add_argument("--include-subagents", action="store_true")
    listing.add_argument("--include-archived", action="store_true")

    read = commands.add_parser("read")
    read.add_argument("--session-id", required=True)

    rename = commands.add_parser("rename")
    rename.add_argument("--session-id", required=True)
    rename.add_argument("--expected-title", required=True)
    rename.add_argument("--title", required=True)

    args = parser.parse_args()
    try:
        base_url = load_bridge(args.bridge_file)
        if args.command == "health":
            print_json(request(base_url, "GET", "/health", timeout=args.timeout))
        elif args.command == "list":
            print_json(request(base_url, "GET", "/sessions", query={
                "workspaceId": args.workspace_id,
                "limit": max(1, min(40, args.limit)),
                "includeSubagents": "1" if args.include_subagents else "0",
                "includeArchived": "1" if args.include_archived else "0",
            }, timeout=args.timeout))
        elif args.command == "read":
            print_json(request(base_url, "POST", "/read", {"sessionId": args.session_id}, timeout=args.timeout))
        else:
            print_json(request(base_url, "POST", "/rename", {
                "sessionId": args.session_id,
                "expectedTitle": args.expected_title,
                "title": args.title,
            }, timeout=args.timeout))
    except (BridgeError, OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
