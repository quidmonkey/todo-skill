---
name: todo
description: List everything the user still owes someone, pulled from recent Claude Code sessions, Jira (and Asana/Linear/Monday if connected), meeting notes in Gmail/Drive, local git repos and GitHub. Each item has a short description, who asked, and a target date. Use when the user says "todo", "what do I owe", "what's outstanding", "what am I forgetting", or invokes /todo. Also handles "/todo done 3", "/todo snooze 2 until friday", "/todo add <text>", "/todo dismiss 4", "/todo init". Accepts an optional window such as "48h" or "since friday".
argument-hint: "[48h | since <day>] | done N.. | dismiss N.. | snooze N until <date> | add <text> | init"
allowed-tools: Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/todo.py:*)
---

# Todo

Collect outstanding items from every available source, drop ones the user already closed, and show a short list.

Script: `python3 "${CLAUDE_SKILL_DIR}/scripts/todo.py"`. Config is `~/.todorc` (template: `${CLAUDE_SKILL_DIR}/todorc.example`). State is `~/.todo-state.json`. Only the script writes the state file.

## 1. Handle subcommands first

If the arguments start with one of these, run it, print the script's one-line result, and stop:

- `done N [N…]` → `todo.py state done N…`. N is a number from the last report or an item id.
- `dismiss N [N…]` → `todo.py state dismiss N…`. Use when it isn't the user's to do.
- `snooze N until <date>` → convert the date to `YYYY-MM-DD` and run `todo.py state snooze N --until DATE`.
- `reopen <id>` → `todo.py state reopen <id>`.
- `add <text> [from <who>] [by <date>]` → `todo.py state add <text> --who WHO --due YYYY-MM-DD`.
- `init` → go to step 2 and create the rc file even if one exists, after showing the user the current one.

Otherwise the argument is an optional window: `Nh`, or `since <day/date>` converted to hours from now. With no argument, use `lookback_hours` from the config (default 72).

## 2. Load config

Run `todo.py config`. It returns JSON with the user's identity, enabled sources, queries and repo settings.

If `rc_found` is false, copy `todorc.example` to `~/.todorc` and fill `[me]` from the config JSON. Tell the user in one line that you created it and that they can add aliases and repos there. Then continue with the defaults; don't stop to ask.

Use `me.name`, `me.aliases` and `me.emails` to tell which meeting-note action items belong to the user. "Abe to send…", "@Abraham", "AW will…" and "you" in a 1:1 all count.

## 3. Collect in parallel

Skip any source whose `sources.<name>` is false. Run the independent calls below in a single message. If a source fails or isn't connected, skip it and note it in the footer. Don't try to authenticate.

- **Claude sessions:** `todo.py sessions --hours H`. For each session, decide whether anything is still open. Count: a question Claude asked that the user never answered, work described as in progress or blocked, a step the user had to do themselves, or a follow-up the user said they'd do. Ignore sessions that ended with the work done. Requester is "you" unless the prompts name someone ("Molly asked me to…").
- **Git and GitHub:** `todo.py git --hours H`. Each line already has an id in brackets. Mention "PRs awaiting your review" individually only if updated within the window, and roll the rest into one item ("N older review requests"). Do the same for "Your open PRs" untouched for over 90 days.
- **Jira:** call `getAccessibleAtlassianResources` once for the cloudId (or use `jira.site` if set). Then call `searchJiraIssuesUsingJql` with `jira.jql`, asking for summary, status, reporter, duedate, priority and the sprint field if available. Requester is the reporter; target date is the due date, or the sprint end date if there's no due date. Id: `jira:KEY`.
- **Asana / Linear / Monday:** run `ToolSearch` for each name. If tools come back that aren't just `authenticate`, query tasks assigned to the user that are incomplete. Ids: `asana:<gid>`, `linear:<identifier>`, `monday:<id>`.
- **Meeting notes (Gmail):** `search_threads` with `gmail.query`, up to 15 threads, then `get_thread` on each. Pull out action items assigned to the user. Requester is whoever assigned it, or else the meeting organizer. Use a date only if one is stated. Id: `gmail:<threadId>:<3-word-slug>`.
- **Meeting notes (Drive):** `search_files` for docs whose name contains `drive.query`, modified within the window. Read only those not already covered by a Gmail thread for the same meeting. Id: `drive:<fileId>:<3-word-slug>`.

If there are more than 6 meeting threads/docs, hand the extraction to one `Agent` (general-purpose). Give it the thread ids, the user's identity, and the item format, and ask it to return only the items. This keeps the transcripts out of your context.

## 4. Filter, dedupe, number

1. Run `todo.py state suppressed`. Drop any item whose id is listed. For items with model-made slugs (claude, gmail, drive), also drop ones that clearly describe the same task as a suppressed item's `text`. Add the `manual_open` items, which count as source `manual`.
2. Merge duplicates across sources, for example a Jira key mentioned in meeting notes or a PR branch named after a ticket. Keep the most specific id and list both sources.
3. Don't invent items, and don't upgrade a vague mention into a commitment. If it's unclear whether something is the user's, include it with `(?)`.

## 5. Output

Group by target date relative to today: **Overdue**, **This week**, **Later**, **No date**. Omit empty groups. Within a group, sort by date, then by importance. Number items continuously across groups.

```
**Overdue**
1. Send revised SOW to Anna — Anna Chaney · due Oct 30 · jira EPA-261
**This week**
2. Review data-shape findings PR — rumit-singh · Nov 4 · github spatial-ml#174
**No date**
3. Pick Option A/B for Podman docker shim — you · claude (frontier-model-service)
4. Commit 3 uncommitted changes on `feature-x` — you · git (jira-watcher)
```

- Each line is at most about 110 characters: `N. <imperative description> — <who asked> · <date> · <source + short ref>`. Leave out the date part when there is none.
- Bold the group headings. Don't use tables.
- Keep the list to about 25 items. If there are more, show the top 25 by urgency and add one line: "+N lower-priority items. Say 'show all' to see them."
- Footer, one line each: sources skipped or failed (if any), then `Mark items with /todo done 1 3 · snooze 2 until fri · add <text>`.

## 6. Save the numbering

Pipe the displayed items, in order, to `todo.py state set-last` as a JSON array of `{"id": ..., "text": ...}` so that `/todo done 3` resolves. Run this with a heredoc, not echo, because the text may contain quotes.
