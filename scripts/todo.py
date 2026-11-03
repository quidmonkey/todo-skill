#!/usr/bin/env python3
"""Local collectors and state for the /todo skill.

Subcommands:
  config                 print the merged ~/.claude/todo/todorc config as JSON
  sessions [--hours N]   digest of recent Claude Code sessions, focused on unfinished work
  git [--hours N]        outstanding git work: dirty trees, unpushed branches, new TODOs, PRs
  state ...              read/update ~/.claude/todo/state.json (done, dismiss, snooze, add, last)
"""

import argparse
import configparser
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HOME = Path.home()
TODO_DIR = HOME / ".claude" / "todo"
RC_PATH = Path(os.environ.get("TODORC", TODO_DIR / "todorc"))
STATE_PATH = Path(os.environ.get("TODO_STATE", TODO_DIR / "state.json"))
LEGACY_PATHS = [  # pre-~/.claude locations, migrated on first run
    ("TODORC", HOME / ".todorc", RC_PATH),
    ("TODO_STATE", HOME / ".todo-state.json", STATE_PATH),
]


def migrate_legacy():
    for env, old, new in LEGACY_PATHS:
        if env not in os.environ and old.exists() and not new.exists():
            new.parent.mkdir(parents=True, exist_ok=True)
            old.replace(new)
PROJECTS_DIR = HOME / ".claude" / "projects"

SOURCES = ["claude", "jira", "asana", "linear", "monday", "gmail", "drive", "git", "github"]
DEFAULTS = {
    "lookback_hours": 72,
    "scan_depth": 5,
    "stale_days": 14,
    "jql": "assignee = currentUser() AND statusCategory != Done ORDER BY duedate ASC, priority DESC",
    "gmail_query": (
        "newer_than:{days}d (from:gemini-notes@google.com OR from:no-reply@zoom.us "
        'OR subject:("notes by gemini" OR "meeting notes" OR "meeting summary" '
        'OR "action items" OR recap))'
    ),
    "drive_query": "Notes by Gemini",
}

FOLLOWUP_RE = re.compile(
    r"\b(TODO|FIXME|next steps?|follow[- ]?up|still (?:need|needs|to do)|remaining|"
    r"not yet|left to do|you(?:'ll| will) need to|blocked|waiting (?:on|for)|"
    r"once .{0,40} (?:is|are) (?:merged|approved|deployed)|want me to|should I)\b",
    re.IGNORECASE,
)
SKIP_COMMANDS = {"/clear", "/compact", "/exit", "/resume", "/model", "/config", "/todo"}


# ---------------------------------------------------------------- helpers


def sh(*cmd, cwd=None, timeout=20):
    try:
        r = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def short_path(p):
    p = str(p)
    return "~" + p[len(str(HOME)) :] if p.startswith(str(HOME)) else p


def expand(p):
    return Path(os.path.expandvars(os.path.expanduser(p.strip())))


def truncate(text, limit):
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def parse_ts(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def on(value):
    return str(value).strip().lower() not in {"off", "no", "false", "0"}


# ---------------------------------------------------------------- config


def load_config():
    cp = configparser.ConfigParser(
        allow_no_value=True,
        delimiters=("=",),
        comment_prefixes=("#", ";"),
        inline_comment_prefixes=("#",),
        interpolation=None,
        strict=False,
    )
    cp.optionxform = str  # keep case: repo paths are keys
    found = RC_PATH.exists()
    if found:
        cp.read(RC_PATH)

    def get(section, key, default=None):
        return cp.get(section, key, fallback=default) if cp.has_section(section) else default

    def as_list(value):
        return [v.strip() for v in (value or "").split(",") if v.strip()]

    git_name = sh("git", "config", "--global", "user.name")
    git_email = sh("git", "config", "--global", "user.email")
    hours = float(get("settings", "lookback_hours", DEFAULTS["lookback_hours"]))

    repos, scan_roots = [], []
    if cp.has_section("repos"):
        for key, val in cp.items("repos"):
            line = key if val is None else f"{key}={val}"
            if line.startswith("scan "):
                scan_roots.append(str(expand(line[5:])))
            else:
                repos.append(str(expand(line)))

    sources = {s: on(get("sources", s, "on")) for s in SOURCES}
    return {
        "rc_path": str(RC_PATH),
        "rc_found": found,
        "state_path": str(STATE_PATH),
        "me": {
            "name": get("me", "name", git_name),
            "aliases": as_list(get("me", "aliases", "")),
            "emails": as_list(get("me", "emails", git_email)),
            "github": get("me", "github", "") or sh("gh", "api", "user", "--jq", ".login"),
        },
        "lookback_hours": hours,
        "sources": sources,
        "repos": repos,
        "scan_roots": scan_roots or [str(HOME / "Development")],
        "scan_depth": int(get("settings", "scan_depth", DEFAULTS["scan_depth"])),
        "stale_days": int(get("settings", "stale_days", DEFAULTS["stale_days"])),
        "jira": {
            "jql": get("jira", "jql", DEFAULTS["jql"]),
            "site": get("jira", "site", ""),
        },
        "gmail": {
            "query": get("gmail", "query", DEFAULTS["gmail_query"]).replace(
                "{days}", str(max(1, -(-int(hours) // 24)))
            ),
        },
        "drive": {"query": get("drive", "query", DEFAULTS["drive_query"])},
        "github": {"orgs": as_list(get("github", "orgs", ""))},
    }


# ---------------------------------------------------------------- claude sessions


def clean_prompt(text):
    if text.startswith(("<local-command", "<bash-stdout", "<bash-stderr", "Caveat:")):
        return None
    text = re.sub(
        r"<pasted_content[^>]*>.*?</pasted_content[^>]*>", "[pasted]", text, flags=re.DOTALL
    )
    cmd = re.search(r"<command-name>(.*?)</command-name>", text)
    if cmd:
        name = cmd.group(1).strip()
        if name in SKIP_COMMANDS:
            return None
        args = re.search(r"<command-args>(.*?)</command-args>", text, re.DOTALL)
        return f"{name} {args.group(1).strip() if args else ''}".strip()
    return text.strip() or None


def human_prompt(rec):
    if rec.get("type") != "user" or rec.get("isMeta") or rec.get("isSidechain"):
        return None
    origin = rec.get("origin") or {}
    if origin and origin.get("kind") != "human":
        return None
    content = rec.get("message", {}).get("content")
    if isinstance(content, list):
        if any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
            return None
        content = "\n".join(
            c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"
        )
    return clean_prompt(content) if isinstance(content, str) else None


def followup_lines(text):
    out = []
    for line in re.split(r"(?<=[.!?])\s+|\n+", text):
        line = line.strip(" -*#>")
        if 12 < len(line) < 300 and FOLLOWUP_RE.search(line):
            out.append(line)
    return out


def parse_session(path, cutoff):
    s = {
        "id": path.stem,
        "title": None,
        "prompts": [],
        "cwds": defaultdict(int),
        "branches": set(),
        "last": None,
        "finals": [],
        "commits": 0,
        "edits": 0,
        "automated": False,
        "followups": [],
    }
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") == "ai-title":
                s["title"] = rec.get("aiTitle")
                continue
            if str(rec.get("entrypoint", "")).startswith("sdk"):
                s["automated"] = True
            ts = parse_ts(rec.get("timestamp"))
            if ts is None or ts < cutoff or rec.get("isSidechain"):
                continue
            if rec.get("cwd"):
                s["cwds"][rec["cwd"]] += 1
            if rec.get("gitBranch") and rec["gitBranch"] != "HEAD":
                s["branches"].add(rec["gitBranch"])
            prompt = human_prompt(rec)
            if prompt:
                s["prompts"].append(truncate(prompt, 300))
                s["followups"] += [f"(you) {l}" for l in followup_lines(prompt)]
            if rec.get("type") == "assistant":
                for block in rec.get("message", {}).get("content") or []:
                    if block.get("type") == "tool_use":
                        name, inp = block.get("name"), block.get("input") or {}
                        if name in {"Edit", "Write", "MultiEdit", "NotebookEdit"}:
                            s["edits"] += 1
                        elif name == "Bash" and "git commit" in inp.get("command", ""):
                            s["commits"] += 1
                    elif block.get("type") == "text" and block.get("text", "").strip():
                        s["finals"] = (s["finals"] + [block["text"]])[-2:]
            s["last"] = ts
    if not s["prompts"] or s["automated"]:
        return None
    if all(p.startswith("/todo") for p in s["prompts"]):
        return None  # the session running this skill
    for text in s["finals"]:
        s["followups"] += [f"(claude) {l}" for l in followup_lines(text)]
    return s


def cmd_sessions(args, cfg):
    hours = args.hours or cfg["lookback_hours"]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    sessions = []
    for path in PROJECTS_DIR.glob("*/*.jsonl"):
        if datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < cutoff:
            continue
        s = parse_session(path, cutoff)
        if s:
            s["cwd"] = max(s["cwds"], key=s["cwds"].get) if s["cwds"] else "?"
            sessions.append(s)
    sessions.sort(key=lambda s: s["last"], reverse=True)

    print(f"# Claude Code sessions in the last {hours:g}h ({len(sessions)})\n")
    for s in sessions:
        branch = f" [{', '.join(sorted(s['branches']))}]" if s["branches"] else ""
        print(f"## {s['title'] or truncate(s['prompts'][0], 70)} — {short_path(s['cwd'])}{branch}")
        print(f"id: claude:{s['id'][:8]}  last active: {s['last'].astimezone():%a %b %d %H:%M}"
              f"  edits: {s['edits']}  commits: {s['commits']}")
        prompts = s["prompts"]
        shown = prompts if len(prompts) <= 8 else prompts[:2] + ["…"] + prompts[-5:]
        print("Prompts:")
        for p in shown:
            print(f"- {p}")
        if s["followups"]:
            print("Follow-up signals:")
            seen = set()
            for f in s["followups"]:
                if f not in seen:
                    seen.add(f)
                    print(f"- {truncate(f, 280)}")
        if s["finals"]:
            print("Last assistant message: " + truncate(s["finals"][-1], 900))
        print()


# ---------------------------------------------------------------- git


def find_repos(root, depth):
    root = Path(root)
    if not root.is_dir():
        return []
    found, frontier = [], [(root, 0)]
    while frontier:
        d, lvl = frontier.pop()
        if (d / ".git").exists():
            found.append(d)
            continue
        if lvl >= depth:
            continue
        try:
            for child in d.iterdir():
                if child.is_dir() and not child.name.startswith(".") and child.name not in {
                    "node_modules", "venv", ".venv", "vendor", "dist", "build"
                }:
                    frontier.append((child, lvl + 1))
        except OSError:
            pass
    return found


def recently_touched(repo, days):
    cutoff = datetime.now().timestamp() - days * 86400
    for name in ("index", "HEAD", "FETCH_HEAD", "logs/HEAD"):
        p = repo / ".git" / name
        if p.exists() and p.stat().st_mtime >= cutoff:
            return True
    return False


def repo_report(repo, emails, since_iso):
    g = lambda *a: sh("git", "-C", str(repo), *a)
    lines = []
    status = g("status", "--porcelain")
    if status:
        n = len(status.splitlines())
        branch = g("rev-parse", "--abbrev-ref", "HEAD")
        lines.append(f"- [git:{repo.name}:dirty:{branch}] {n} uncommitted change(s) on `{branch}`")
    for row in g("for-each-ref", "--format=%(refname:short)|%(upstream:short)|%(upstream:track)",
                 "refs/heads").splitlines():
        name, upstream, track = (row.split("|") + ["", ""])[:3]
        ahead = re.search(r"ahead (\d+)", track)
        if ahead:
            lines.append(f"- [git:{repo.name}:unpushed:{name}] `{name}` is {ahead.group(1)} commit(s) ahead of {upstream}")
        elif not upstream and name not in {"main", "master", "develop"}:
            mine = g("log", name, "--not", "--remotes", f"--since={since_iso}", "--format=%h",
                     *[f"--author={e}" for e in emails])
            if mine:
                lines.append(f"- [git:{repo.name}:unpushed:{name}] local branch `{name}` has "
                             f"{len(mine.splitlines())} recent commit(s), never pushed")
    stash = g("stash", "list", f"--since={since_iso}")
    if stash:
        lines.append(f"- [git:{repo.name}:stash] {len(stash.splitlines())} recent stash(es)")
    if emails:
        diff = g("log", "--all", f"--since={since_iso}", *[f"--author={e}" for e in emails],
                 "-p", "--unified=0", "--format=")
        for l in diff.splitlines():
            if l.startswith("+") and not l.startswith("+++") and re.search(r"\b(TODO|FIXME|XXX)\b", l):
                text = truncate(l[1:].strip(), 160)
                h = hashlib.sha1(text.encode()).hexdigest()[:6]
                lines.append(f"- [git:{repo.name}:todo:{h}] new {text}")
    return lines


def gh_json(*args):
    out = sh("gh", *args, timeout=30)
    try:
        return json.loads(out) if out else []
    except json.JSONDecodeError:
        return []


def cmd_git(args, cfg):
    hours = args.hours or cfg["lookback_hours"]
    since_iso = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    emails = cfg["me"]["emails"]

    repos = {Path(r).resolve() for r in cfg["repos"] if (Path(r) / ".git").exists()}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    for path in PROJECTS_DIR.glob("*/*.jsonl"):  # repos you worked in via Claude
        if datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) >= cutoff:
            for line in path.open(encoding="utf-8", errors="replace"):
                m = re.search(r'"cwd":"([^"]+)"', line)
                if m:
                    top = sh("git", "-C", m.group(1), "rev-parse", "--show-toplevel")
                    if top:
                        repos.add(Path(top).resolve())
                    break
    for root in cfg["scan_roots"]:
        for r in find_repos(root, cfg["scan_depth"]):
            if recently_touched(r, cfg["stale_days"]):
                repos.add(r.resolve())

    print(f"# Local git ({len(repos)} repos checked)\n")
    any_found = False
    for repo in sorted(repos):
        lines = repo_report(repo, emails, since_iso)
        if lines:
            any_found = True
            print(f"## {short_path(repo)}")
            print("\n".join(lines[:15]))
            if len(lines) > 15:
                print(f"- … {len(lines) - 15} more")
            print()
    if not any_found:
        print("_Nothing outstanding in local repos._\n")

    if not cfg["sources"].get("github", True) or not sh("gh", "auth", "token"):
        print("# GitHub\n_skipped (disabled or gh not logged in)_")
        return
    owners = [f"--owner={o}" for o in cfg["github"]["orgs"]]
    fields = "--json=title,url,repository,author,createdAt,updatedAt,isDraft"
    sections = [
        ("PRs awaiting your review", ["search", "prs", "--review-requested=@me", "--state=open"]),
        ("Your open PRs", ["search", "prs", "--author=@me", "--state=open"]),
        ("GitHub issues assigned to you", ["search", "issues", "--assignee=@me", "--state=open"]),
    ]
    for title, base in sections:
        f = fields if "prs" in base else "--json=title,url,repository,author,createdAt,updatedAt"
        items = gh_json(*base, *owners, "--archived=false", f, "--limit=30")
        print(f"# {title} ({len(items)})")
        for it in items:
            repo = it.get("repository", {}).get("nameWithOwner", "?")
            num = it["url"].rstrip("/").split("/")[-1]
            draft = " (draft)" if it.get("isDraft") else ""
            print(f"- [gh:{repo}#{num}] {it['title']}{draft} — by {it.get('author', {}).get('login', '?')}, "
                  f"opened {it['createdAt'][:10]}, updated {it['updatedAt'][:10]}  {it['url']}")
        print()


# ---------------------------------------------------------------- state


def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {"items": {}, "last": []}


def save_state(state):
    cutoff = (date.today() - timedelta(days=60)).isoformat()
    state["items"] = {
        k: v for k, v in state["items"].items()
        if v["status"] in {"open", "snoozed"} or v.get("updated", "9999") >= cutoff
    }
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(STATE_PATH)


def resolve(state, ref):
    """Accept an item id, or a number from the last report."""
    if ref.isdigit():
        n = int(ref)
        if 1 <= n <= len(state["last"]):
            return state["last"][n - 1]
        sys.exit(f"No item #{n} in the last report ({len(state['last'])} items).")
    for entry in state["last"]:
        if entry["id"] == ref:
            return entry
    return {"id": ref, "text": state["items"].get(ref, {}).get("text", "")}


def cmd_state(args, _cfg):
    state = load_state()
    today = date.today().isoformat()
    action = args.action

    if action == "list":
        print(json.dumps(state, indent=2))
        return
    if action == "set-last":
        state["last"] = json.load(sys.stdin)
        save_state(state)
        print(f"Saved {len(state['last'])} items as the last report.")
        return
    if action == "add":
        text = " ".join(args.refs)
        iid = "manual:" + hashlib.sha1(text.encode()).hexdigest()[:8]
        state["items"][iid] = {"status": "open", "text": text, "who": args.who or "you",
                               "due": args.due, "added": today, "updated": today}
        save_state(state)
        print(f"Added {iid}: {text}")
        return
    if action in {"done", "dismiss", "snooze", "reopen"}:
        if action == "snooze" and not args.until:
            sys.exit("snooze needs --until YYYY-MM-DD")
        for ref in args.refs:
            entry = resolve(state, ref)
            item = state["items"].get(entry["id"], {})
            if action == "reopen":
                state["items"].pop(entry["id"], None)
            else:
                status = {"done": "done", "dismiss": "dismissed", "snooze": "snoozed"}[action]
                item.update({"status": status, "text": item.get("text") or entry.get("text", ""),
                             "updated": today})
                if action == "snooze":
                    item["until"] = args.until
                state["items"][entry["id"]] = item
            print(f"{action}: {entry['id']} {entry.get('text', '')}".rstrip())
        save_state(state)
        return
    if action == "suppressed":
        # Compact list the skill uses to filter a fresh report.
        out = []
        for iid, it in state["items"].items():
            if it["status"] in {"done", "dismissed"} or (
                it["status"] == "snoozed" and it.get("until", "") > today
            ):
                out.append({"id": iid, "status": it["status"], "text": it.get("text", ""),
                            "until": it.get("until")})
        manual = [{"id": k, **v} for k, v in state["items"].items() if v["status"] == "open"]
        print(json.dumps({"suppressed": out, "manual_open": manual}, indent=2))
        return
    sys.exit(f"unknown state action: {action}")


# ---------------------------------------------------------------- main


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("config")
    for name in ("sessions", "git"):
        sp = sub.add_parser(name)
        sp.add_argument("--hours", type=float)
    st = sub.add_parser("state")
    st.add_argument("action", choices=["list", "suppressed", "set-last", "add", "done",
                                       "dismiss", "snooze", "reopen"])
    st.add_argument("refs", nargs="*")
    st.add_argument("--until")
    st.add_argument("--who")
    st.add_argument("--due")
    args = p.parse_args()

    migrate_legacy()
    cfg = load_config()
    if args.cmd == "config":
        print(json.dumps(cfg, indent=2))
    elif args.cmd == "sessions":
        cmd_sessions(args, cfg)
    elif args.cmd == "git":
        cmd_git(args, cfg)
    else:
        cmd_state(args, cfg)


if __name__ == "__main__":
    main()
