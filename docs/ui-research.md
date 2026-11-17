# UI research: what users complain about in coding-agent harness UIs

Date: 2026-11-17. Purpose: borrow lessons for the plotter web UI, which has the same shape as an agent
harness (a long-running subprocess, streamed output, a state the operator must be able to read at a glance,
and an action that is hard to undo).

## Which harnesses

- **OpenCode** (`sst/opencode`, now `anomalyco/opencode`): terminal UI plus a web UI and desktop app. Clearly
  identified, covered below.
- **"The OpenRouter-based harness"**: could not be pinned down. OpenRouter itself has no coding harness of its
  own beyond "Ori Harness", a CLI wrapper that runs Claude Code, Codex, OpenCode and others through OpenRouter.
  OpenRouter lists about 34 coding agents that work with it. The most discussed ones with a public issue trail
  about OpenRouter are OpenClaw and OpenCode (often paired with OpenRouter in write-ups). Covered: OpenClaw's
  OpenRouter issues (via a third-party survey of 100 issues) and the OpenRouter + OpenCode pairing. If the user
  meant Cline, Roo Code or Kilo Code, those were not researched separately.
- Adjacent evidence from Claude Code / Gemini CLI threads is used where the complaint is the same pattern.

All web content was read as data. Links are to the pages read; third-party mirrors are marked.

## Complaint themes and the design rule taken from each

### 1. Opaque status: "is it stuck or working?"

- OpenCode `run` hangs during init with no session, no output and no error; the only way out is an external
  timeout ([anomalyco/opencode#38723](https://github.com/anomalyco/opencode/issues/38723)).
- Session status reported as "unknown" indefinitely, no status-changed event; parent session looks "loading"
  forever while subagents run; sessions hang silently and users resend messages, assuming they were lost
  (issues #10802, #12860, #27319, read through mirrors
  [1](https://gh.ali360.cloudns.ch/anomalyco/opencode/issues/10802),
  [2](https://gh.ali360.cloudns.ch/anomalyco/opencode/issues/12860)).
- A third-party guide exists purely to tell which OpenCode/Claude Code/Codex session is stuck
  ([happier.dev](https://guides.happier.dev/how-to-tell-which-claude-code-codex-and-opencode-session-is-stuck)).

**Rule:** one explicit state machine (idle, preflight, running, pausing, paused for pen swap, paused by
operator, stopping, complete, interrupted, error) shown in one place, always. Every state names what the
machine is doing and what the operator should do next. A heartbeat shows the link is alive; if no event arrives
for a while the UI says "no news from the plotter for N s" instead of looking busy.

### 2. Cancel that does not cancel, or is unclear about what it did

- Cancelling needed several Escape presses (opencode #888); Escape could not interrupt while a permission dialog
  was open, causing a dialog loop ([opencode PR #27776](https://github.com/anomalyco/opencode/pull/27776)).

**Rule:** Stop is always visible during a job, works while any dialog is open, and says honestly what it does:
"Firmware 2.0.1 has no e-stop. Stop finishes the move already sent (at most about 3 s), raises the pen and
releases the motors. For an instant stop, switch the plotter off." The UI shows "stopping..." until the
process has actually ended, then the final state and where the carriage was left.

### 3. Lost state on reload or across clients

- OpenCode web UI shows an empty sidebar for sessions created in another client; session persistence breaks for
  secondary projects in multi-workspace web mode
  ([opencode#19672](https://github.com/anomalyco/opencode/issues/19672) and a related report).
- A tool call returned "user rejected" although no prompt was ever shown: the permission stream closed in a web
  session (reported against Claude Code's web interface,
  [claudeissues mirror #78473](https://claudeissues.com/issue/78473-bug-web-interface-tool-calls-file-writes-intermittently-return-user-rejected-wit)).

**Rule:** the server owns all job state; the browser is a view. Reloading, a second laptop or a phone all get
the same snapshot (state, progress, position, pen-swap prompt, console history) and resume the event stream from
the last sequence number. A pending pen-swap prompt is part of the state, never a transient popup, so it cannot
be missed. Per-file placement and layer settings are stored on the server.

### 4. Scroll jank, runaway auto-scroll and output you cannot read back

- Streaming output janks and maxes the CPU; full redraws per update; scroll jumps to the top of the whole
  conversation; users cannot scroll back through a long answer
  ([Ask HN thread](https://nuxi.hackernews-40s.pages.dev/item/46286057) (mirror),
  [Claude Code scroll jitter in tmux](https://claudeissues.com/issue/9935-excessive-scroll-events-causing-ui-jitter-in-terminal-multiplexers-4-000-6-700-s)).
  OpenCode's move to its own renderer (opentui) was noted as better in the same HN thread.

**Rule:** console appends rows in batches (one frame), caps its DOM length, and only auto-scrolls when the user
is already at the bottom; scrolling up pauses it and shows a "jump to latest" button. Position updates never
touch the console; they go to the status panel and the canvas, throttled to a few per second.

### 5. Poor or missing error messages; settings and IDs that confuse

- OpenRouter-related issues in OpenClaw (survey of 100 issues): provider errors that do not trigger failover,
  model IDs mangled with double prefixes (`openrouter/openrouter/auto`), 401 "missing authentication header"
  despite a valid key, no visibility into which model `auto` picked
  ([dev.to survey](https://dev.to/1bcmax/we-read-100-openclaw-issues-about-openrouter-heres-what-we-built-instead-3ohn)).
- Layout complaints in OpenCode: narrow content and wasted space hurting readability (opencode #525).

**Rule:** every error says what happened, why, and the next action, in plain words ("The drawing runs 12 mm past
the bottom of the canvas. Make it smaller or move it up."), with the raw tool output one click away in the
console, not instead of it. Settings show their unit, limit and default next to the field; values the hardware
will refuse are rejected at input time with the limit stated, and the server still re-validates. No hidden
"auto" choices: the preflight lists exactly what will be sent (speeds, pens, pressure, layers, estimate).

## Smaller patterns noted

- Silent failure modes in streams (permission, CDN-loaded UI) argue for a self-contained UI with no CDN: the lab
  PC serves every file itself.
- Users of multiple clients expect the same view everywhere: the event stream is the single source.
