# 092: `thread send` silently drops messages when argparse mis-parses body

## Problem

`claude-worker thread send <name> "<msg>"` exits 0 and prints the
worker status line, but the message never reaches the recipient.
Re-sending the IDENTICAL content via stdin/file (`cat $file | thread
send`) succeeds. The shell-quoting path of the positional message
parses to something argparse accepts as valid but produces wrong/
empty content.

Class: send-side silent data loss, distinct from #090 (receive-side
preview truncation) and #091 (queue-tag exit code semantics).

## Repro signals

- Multi-line messages (~30+ lines) with backticks, em-dashes, double
  asterisks, parens, fenced code blocks
- One related instance produced `claude-worker: error: unrecognized
  arguments: <message contents>` — suggesting argv parsing breaks
  when the body contains option-like tokens (e.g., backticks around
  `--port`)
- The silent-drop case is likely the same parser mis-route without
  the error path firing — argparse accepts a partial parse and
  treats fragments as the positional `name`/`message`

## Live evidence (2026-04-30)

Reporter: playlite-pm (cross-project). Two instances in one session:

1. ~01:09Z: TL bootstrap brief, ~30 lines, dropped silently. Shell
   exit 0, no error. Workaround `cat $file | thread send` worked
   first try.
2. ~01:39Z: impl-015-rebase brief to the same TL post-PR-merge.
   Same pattern: status `waiting` at send-time, message dropped,
   re-send via heredoc succeeded.

This PM observed the same class earlier in the cw-lead thread: a
positional message containing `— clean P13 break` and `**#087
name**` produced `unrecognized arguments` (OBS29 in PM library).

## GVP alignment

- G2 (loud-over-silent-failure): silent drop is the worst kind —
  caller sees exit 0 + status line, has no signal anything went
  wrong
- V2 (explicit-over-implicit): argparse partial-parse ambiguity
  silently routes message fragments to the wrong fields

## Priority

medium — workaround (stdin/heredoc) is reliable and easy. But this
is a worker-to-worker primitive; any drop pattern erodes trust in
the messaging layer.

## Origin

Cross-project bug report 2026-04-30 from playlite-pm (CWD:
~/git/github.com/shitchell/playlite, session d46cf4f6). Originally
surfaced by Shaun via [chat:repl-15383-pts2] after the second
instance.

## Acceptance criteria

1. `thread send <name> "<long-msg>"` either delivers the full
   message OR exits non-zero with a clear error. No silent partial
   delivery.
2. Either:
   (a) Detect argparse-ambiguous bodies and refuse with a hint
       ("message contains characters that may confuse the shell;
       pass via stdin: `... | claude-worker thread send <name>`"),
       OR
   (b) Always-on stdin-preferred path: when the positional message
       arg is empty AND stdin has data, read from stdin (this is
       reportedly already the behavior; verify and document
       prominently)
3. Regression test: send a known-problematic message body
   (backticks + em-dashes + double-asterisks) via positional and
   stdin paths; assert positional EITHER delivers verbatim OR
   errors clearly; assert stdin always delivers verbatim.

## Possible directions

1. **Refuse positional messages with shell-confusing chars**: scan
   the parsed message arg for known-bad patterns and error.
2. **Recommend stdin in error path**: when argparse rejects a
   message ("unrecognized arguments"), include in the error
   message: "If your message contains backticks, em-dashes, or
   asterisks, pass it via stdin instead."
3. **Detect short-positional-vs-long-stdin asymmetry**: if the
   positional message is suspiciously short (< some threshold) AND
   stdin has data, prefer stdin.
4. **Document the failure mode prominently**: README + thread
   send --help should call out the multi-line/special-char
   pitfall and the canonical stdin pattern.

Direction (1) is the loudest fix; (4) is the cheapest. Recommend
(1) + (4) combined.
