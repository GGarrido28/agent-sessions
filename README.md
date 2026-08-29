# agent-sessions

One view of every local Claude Code and Codex session, including which ones are running right now.

Reads transcripts straight off disk. No API key, no network, no daemon. That matters if you are on a Claude Max or Codex subscription and never issue API keys: the tools that need one are usage and cost dashboards, which are a different thing from a session viewer.

```
TOOL    LIVE     LAST   TURNS  DIR                     BRANCH  TITLE
claude  busy     0m     8      ~/code/web-app          main    Refactor the auth middleware
claude  waiting  0m     21     ~/code/api-server       main    Add rate limiting to the API
claude  -        18h    36     ~/code/web-app          main    Track down a flaky test
codex   -        08-05  20     ~/code/data-pipeline    -       Backfill the events table
```

## Install

Requires Python 3, standard library only.

```sh
git clone https://github.com/GGarrido28/agent-sessions.git ~/code/agent-sessions
ln -s ~/code/agent-sessions/agent-sessions ~/.local/bin/agent-sessions
```

Any directory on your `PATH` works.

### Windows

The symlink above relies on the `#!/usr/bin/env python3` shebang, which fails on
most Windows Python installs — they ship `python.exe`, not `python3`, so the
script is found but the interpreter is not. Write a small wrapper instead of a
symlink, in a directory already on your `PATH` such as `~/.local/bin`.

For Git Bash, an extensionless file:

```sh
#!/usr/bin/env bash
exec "/c/Path/To/python.exe" "/c/path/to/agent-sessions/agent-sessions" "$@"
```

For PowerShell and cmd, the same thing as `agent-sessions.cmd`:

```bat
@echo off
"C:\Path\To\python.exe" "C:\path\to\agent-sessions\agent-sessions" %*
```

`python -c "import sys; print(sys.executable)"` prints the interpreter path to
use. Note that these hardcode the paths, so moving the repo or upgrading Python
means editing them.

Claude sessions are detected through the Win32 process API, so their live
status is exact. Codex has no process registry and `lsof` is not available on
Windows, so Codex liveness falls back to reading the transcript for a turn in
progress, and a session is only counted as running if it wrote to its
transcript in the last five minutes.

That recency is measured from the timestamp on the transcript's last record,
not the file's mtime, because Windows does not refresh a file's mtime while
its writer holds it open. Reading mtime there gets it wrong in both
directions: a session mid-turn stats several minutes stale and drops out of
the table, and one that quit an hour ago picks up a fresh mtime when the
handle finally closes and reappears as `busy`.

Watch mode uses the alternate-screen escape sequence. Windows Terminal, Ghostty,
and VS Code render it correctly; the legacy `conhost.exe` console may not.

## Usage

```sh
agent-sessions                 # what is running right now
agent-sessions -H              # include history: everything, live first
agent-sessions -w              # watch mode: stay up and log status changes
agent-sessions -w 0.5          # faster tick; the floor is 0.5s
agent-sessions -H -C api       # only sessions whose working dir matches
agent-sessions -H -s rate      # title search
agent-sessions -e              # last prompt and reply, in place of the title
agent-sessions -H -d 7 -a      # last 7 days, no row cap
agent-sessions -H -t codex -m  # one tool, with a model column
agent-sessions -j              # JSON, full session ids and live records
agent-sessions -p              # transcript file paths only
agent-sessions --color never   # auto, always, or never
```

The default is live sessions only, which is the view you want most of the time. `-H` adds everything else and is what the search and date filters are for.

The ids in `-j` are what `claude --resume <id>` and `codex resume <id>` take, so the view is one step from reopening a session.

### Watch mode

`-w` keeps the table up and redraws it, so a session going busy, blocking on a permission prompt, or exiting shows up without you asking again.

```
agent-sessions  11:35:23  3 live, 1 waiting on you  ·  0.5s  ·  Ctrl-C to quit

TOOL    LIVE     LAST   TURNS  DIR                     BRANCH  TITLE
claude  busy     0m     8      ~/code/web-app          main    Refactor the auth middleware
...
```

The header counts every live session in scope, including any the row limit or `-d` trimmed out of the table.

### The last exchange

A title tells you what a session was about. `-e` tells you where it is right now: your most recent prompt, and the last thing the agent said back. It takes the place of `TITLE`, and the two share the width evenly.

```
TOOL    LIVE     LAST   TURNS  DIR                     BRANCH  PROMPT                   REPLY
claude  busy     0m     8      ~/code/web-app          main    now do the same for th…  Reading the middleware…
claude  waiting  0m     21     ~/code/api-server       main    yes, 100 requests per …  This would reject requ…
codex   -        08-05  20     ~/code/data-pipeline    -       actually, for the back…  Done. The backfill ran…
```

The prompt is dimmed and the reply is not, because the reply is the half you do not already know. Paired with `-w`, this is the view that tells you which session is worth switching to.

Two details it gets right. Most assistant turns are a tool call with nothing to say, so `REPLY` is the last turn that carried actual text rather than the last turn. And sub-agent turns, which Claude Code writes inline into the parent transcript, are skipped, because those are the agent talking to itself rather than to you.

Both fields are in `-j` output too, capped at 500 characters.

### Colour

Status carries the colour, because status is what you scan for:

| | |
|---|---|
| green | `busy`, actively working |
| amber | `idle`, open but doing nothing |
| red, bold | `waiting`, blocked on you |
| cyan | `live`, running but its registry entry reports no status |
| dim | not live, and the low-signal columns |

Tool names are tinted too, orange for `claude` and blue for `codex`.

Colour is on for a terminal and off when piped, so `| grep` and `| jq` stay clean. `--color always` forces it, `--color never` disables it, and `NO_COLOR` is honoured.

The palette is 256-color rather than truecolor. Apple Terminal exports `COLORTERM=truecolor` but does not actually render 24-bit sequences, and 256 works everywhere.

## Where the data comes from

| | Claude Code | Codex |
|---|---|---|
| Transcripts | `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl` | `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl` |
| Liveness | `~/.claude/sessions/<pid>.json` | open writer locks plus active-turn events |

Both formats are newline-delimited JSON written without spacing, so the scan pulls fields with a regex and only parses the handful of lines it actually needs. Transcripts are append-only, so a file whose mtime and size are unchanged cannot hold new content; results are cached on that. A full sweep of ~84MB runs in about 0.1s, and a watch tick costs roughly 70 `stat` calls plus one `ps`.

## Four things that are not obvious

**A turn is any `promptSource` except `system`.** Claude Code writes five values: `typed`, `suggestion_accepted`, `queued`, `sdk`, and `system`. Counting only `typed` undercounts badly and reports whole sessions as empty. Tool-result lines carry no `promptSource` at all, which is what separates a prompt from a result.

**Codex stamps the parent's id into a sub-agent's `session_meta`.** Each spawned sub-agent gets its own rollout file, but `session_id` holds the parent's id, so keying on it makes one fan-out look like four copies of a single session. Identify a thread by the uuid in the filename instead. Sub-agent threads are hidden by default; `-S` shows them, tagged `codex▸Newton`.

**A liveness registry file outlives a crash.** A session that exits cleanly deletes its own `~/.claude/sessions/<pid>.json`, but one that is killed cannot, so the file is a claim rather than proof and needs three checks before a session counts as live:

- the pid is running,
- that process started at the second the entry recorded in `procStart`, so a recycled pid cannot resurrect a dead session,
- and the entry has been refreshed in the last five minutes, so a session whose process outlives it does not sit in the table forever. Refreshes are irregular, with a median around 15s and gaps past a minute mid-turn, hence the wide window. Versions that never write `updatedAt` are judged on the first two checks alone.

**Codex lock files persist, but their open descriptors do not.** Codex holds its thread's file in `~/.codex/thread-writer-locks/` open for the lifetime of the TUI. The file remains after exit, so existence is not a live signal; `lsof` identifies the locks currently held open. Newer transcripts also bracket work with `task_started` and `task_complete`, separating `busy` from `idle` and providing a fallback when process inspection is unavailable.

**Not every Codex turn writes its own ending.** About one turn in twenty ends with neither `task_complete` nor `turn_aborted` -- interrupted, superseded by the next prompt, or killed mid-turn -- and the transcript then ends on a bare `task_started` for good. Taken at face value that reads as a turn still running, so a session that stopped working in April reports `busy` in August. A turn that is genuinely running writes as it goes, so `busy` also requires the transcript to have been written to in the last five minutes, whether or not a held lock has already proved the TUI is open.

## Limits

- Codex liveness requires `lsof` to distinguish an open idle TUI from stale lock files. Active turns remain detectable from the transcript when `lsof` is unavailable, so on Windows a Codex session shows up while it is working and drops out of the table between turns.
- Codex records workspace roots rather than the checked-out branch, so `BRANCH` is empty for Codex rows.
- Watch mode needs a terminal and exits with a message when stdout is not a tty.
- Read-only by design. It never writes to either tool's directories.

## License

MIT. See [LICENSE](LICENSE).
