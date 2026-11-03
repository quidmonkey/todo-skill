# todo-skill

A Claude Code skill that answers, "What do I still need to do?" or, "Who do I owe something?"

It reads your recent Claude Code sessions, Jira, meeting notes in Gmail and Drive, local git repos and GitHub, then gives you one short list. Each item says what to do, who asked for it, and when it's due. It also checks Asana, Linear and Monday if you have them connected.

```
**Overdue**
1. Send revised SOW to Anna — Anna Chaney · due Oct 30 · jira EPA-261
**This week**
2. Review data-shape findings PR — rumit-singh · Nov 4 · github spatial-ml#174
**No date**
3. Commit 3 uncommitted changes on `feature-x` — you · git (jira-watcher)
```

## Install

```sh
git clone https://github.com/quidmonkey/todo-skill ~/.claude/skills/todo
```

You'll need Python 3. GitHub items also need the `gh` CLI, logged in. Jira, Gmail, Drive and the other services come from whatever MCP connectors you already have in Claude Code. If a source isn't connected, the skill skips it.

## Use

| Command | What it does |
| --- | --- |
| `/todo` | List everything from the last 72 hours |
| `/todo 48h`, `/todo since friday` | Look back over a different window |
| `/todo done 1 3` | Mark items done |
| `/todo dismiss 4` | Hide an item that isn't yours to do |
| `/todo snooze 2 until friday` | Hide an item until a date |
| `/todo add Send deck from Anna by Nov 5` | Add your own item |
| `/todo init` | Create or redo your config |

The numbers refer to the most recent list.

## Config

The first time you run `/todo`, it creates `~/.claude/todo/todorc` from [`todorc.example`](todorc.example). Edit that file to:

- add the nicknames and initials people use for you in meeting notes
- turn sources on or off
- choose which repos to check (by default it scans `~/Development`)
- change the Jira JQL, the Gmail search, or the GitHub orgs

Items you mark done, dismiss or snooze are stored in `~/.claude/todo/state.json`. To use different paths, set `TODORC` and `TODO_STATE`. If you used an earlier version, `~/.todorc` and `~/.todo-state.json` are moved there automatically.

## Files

- `SKILL.md` has the instructions Claude follows.
- `scripts/todo.py` does the local work: reading config, Claude sessions, git and GitHub, and saved state. Run `python3 scripts/todo.py --help` for details.
