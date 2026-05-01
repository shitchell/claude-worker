# 092 TECHNICAL — thread-send-silent-drop-on-argparse-failure

## TL;DR

Two failure classes share one root cause — the shell mangles or
splits the message body before argparse sees it. Class A is loud
(argparse rejects an `--unknown-flag` token and exits 2 with a stack
trace before our code runs). Class B is silent (argparse accepts a
mangled body, our code joins it back with spaces, ships the wrong
content downstream). Workers receive the wrong message; the sender
sees exit 0 and a clean status line.

The robust shipping path is stdin. It bypasses argparse entirely,
preserves whitespace and newlines, and is immune to shell command
substitution because the heredoc `<<'EOF'` form (single-quoted
sentinel) disables interpretation. Every cross-Claude orchestration
flow that has ever survived multiple sessions uses stdin. The bug
is that the positional path looks ergonomic for short messages, so
operators reach for it; long/markdown bodies leak silently.

**Recommended fix (PM-aligned (1)+(4) plus a third leg):**

1. **Heuristic refusal.** After argparse + `_reparse_send_flags`,
   scan the joined content for tokens that strongly indicate shell
   mangling or risky positional use (literal backticks, `$(`, `${`,
   `--<word>` survivors that argparse stripped, em-dashes, double
   asterisks combined with multi-token positional). Refuse with a
   clear stderr error pointing at the stdin canonical pattern.
2. **Loud-error capture.** Override the parser's `error` method
   (or wrap `parse_args`) so the existing argparse "unrecognized
   arguments" message gets a postscript: "If your message body
   contains shell-special characters, pass via stdin: `cat <<'EOF' |
   claude-worker thread send <name>\n...\nEOF`."
3. **Documentation.** README's `thread send` section + `--help`
   epilog explicitly recommend stdin for any message that:
   - is multi-line, OR
   - contains backticks, em-dashes, or markdown formatting, OR
   - includes `--<word>` tokens (option-like).

## (a) Where the message-positional path lives

`claude_worker/cli.py`:

- **Argparse declarations** for the `thread send` subcommand:
  - `name`: `nargs="?"`, default `None` (cli.py:6650-6658).
  - `message`: `nargs="*"`, help "Message text (reads stdin if
    omitted)" (cli.py:6659-6661).
- **`_reparse_send_flags`** (cli.py:1872-1909) — peels recognized
  trailing flags (`--queue`, `--dry-run`, `--verbose`,
  `--show-response`, `--show-full-response`, `--broadcast`,
  `--alive`, `--all-chats`, `--chat`, `--role`, `--status`, `--cwd`)
  off the end of `args.message` and applies them to the namespace.
  Anything before the trailing flag run is kept as message body.
- **`cmd_send`** (cli.py:1912-1958) — the body→content reduction:

  ```python
  args = _reparse_send_flags(args)
  ...
  if args.message:
      content = " ".join(args.message)
  else:
      content = sys.stdin.read()
  if not content.strip():
      print("Error: empty message", file=sys.stderr)
      sys.exit(1)
  ...
  rc = _send_to_single_worker(args.name, content, args)
  _print_worker_status(args.name)
  sys.exit(rc)
  ```
- **`cmd_broadcast`** (cli.py:1961-2008) — same body→content
  pattern with the same hazard surface.

The stdin path is **only** reached when `args.message` is empty
(falsy list). If positional has anything, stdin is never read,
even when the caller pipes data in. That's intentional and
documented today.

## (b) Two repro classes — what argparse state distinguishes them

I confirmed the boundary empirically by building the same parser
shape and feeding it candidate token lists.

| argv body                          | argparse outcome                       | Class  |
|------------------------------------|----------------------------------------|--------|
| `["use", "--port", "8080"]`        | `error: unrecognized arguments: --port 8080`, SystemExit 2 | LOUD A |
| `["use", "—", "em", "dash", "**bold**"]` | parsed: `message=["use","—","em","dash","**bold**"]` | SILENT |
| `["message", "body", "--queue"]`   | parsed: `message=["message","body"]`, `queue=True` | benign  |
| `["before", "--", "after"]`        | parsed: `message=["before","after"]` (the `--` is silently stripped) | SILENT |

The trigger for **Class A (loud)** is: argparse encounters a token
that begins with `-` or `--`, is not in the registered flag set,
and is not the trailing `--` separator. argparse's
`prefix_chars="-"` default with `nargs="*"` does NOT absorb an
unknown `--foo` into the positional list — instead it hits the
error path. argparse exits with code 2 BEFORE our wrapper runs;
we cannot post-process this case unless we override `error()`.

The trigger for **Class B (silent)** is anything else. Five common
roads in:

1. **Shell command substitution succeeded but produced empty/wrong
   text.** Backticks ` `cmd` ` or `$(cmd)` inside `"..."` run the
   command. If the command fails or returns "", the substitution
   becomes empty. argv has the surrounding context with a hole.
   argparse parses the holes-and-context as benign words.
2. **Shell glob expanded inside `"..."` was disabled, but outside
   `"..."` was not.** `**bold**` outside quotes globs against cwd;
   if no matches and `nullglob` is unset, the literal `**bold**`
   reaches argparse, gets absorbed; if matches, the message body
   silently mutates to filenames.
3. **Em-dash, en-dash, double asterisks, parens.** Not shell-
   special, not flag-prefix-special. argparse passes them through
   as message words. They're a marker for "this looks like prose
   that the user wrote in markdown and pasted naively into a
   positional arg" — the heuristic value is high even though they
   themselves are benign to argparse.
4. **Literal `--` separator inside the body.** argparse silently
   strips the first standalone `--` token (it's the standard "end
   of options" marker). Operators don't expect their `--` text to
   vanish; this is the most insidious of the silent classes.
5. **Whitespace collapse via `" ".join(args.message)`.** Multiple
   contiguous newlines / leading blank lines / tab indentation
   collapse to single spaces. Not a drop, but a content mutation
   that breaks markdown formatting in the recipient's view.

## (c) Fix proposal

### Fix-1 (REQUIRED): heuristic refusal of risky positional content

Add a post-`_reparse_send_flags` validator that inspects the
content the user is about to send and refuses with a clear stderr
error when any of the following hold:

| Trigger                                            | Why it's risky                                     |
|----------------------------------------------------|---------------------------------------------------|
| Backtick character `` ` `` in any token           | shell command-substitution remnant or markdown    |
| Substring `$(` or `${` in any token               | shell command/parameter substitution              |
| A token equal to `--`                             | argparse will silently strip; cannot recover here, but if we see one survived in message it means it appeared multiple times and one was stripped |
| A token starting with `--` but matching a known flag of any subcommand (e.g., `--port`, `--config`) | suggests the body intended a literal flag-name |
| Em-dash `—` or en-dash `–` AND multi-token message | strongly indicates pasted markdown/prose         |
| Double-asterisk `**` token AND multi-token message | strongly indicates pasted markdown/prose         |
| Newline `\n` inside any single token (rare; only happens if shell preserves) | indicates intent for multi-line that shouldn't be positional |

When any trigger fires, exit 1 with stderr like:

```
Error: positional message contains characters that may be
shell-mangled (matched: backtick).

Pass the message via stdin instead:

    cat <<'EOF' | claude-worker thread send <name>
    ...your message...
    EOF

Or from a file:

    claude-worker thread send <name> < message.md

Note the single-quoted EOF: it disables shell interpretation
inside the heredoc, which is what makes long/markdown messages
survive intact.
```

The error names the matched trigger so operators can fix the
specific issue rather than guess. Refuse-by-default has the right
incentives: short benign messages still work, risky messages are
forced through the stdin path that always works.

### Fix-2 (REQUIRED): loud-error postscript on argparse failure

Override the parser's `error` method (or use `parse_known_args` +
manual unknown-arg check) so that when argparse would say
`unrecognized arguments: --port 8080`, our error path appends:

```
unrecognized arguments: --port 8080

This usually means a shell-special character or option-like
token leaked into the positional message. Pass the message via
stdin to bypass argparse:

    cat <<'EOF' | claude-worker thread send <name>
    ...
    EOF
```

Implementation: argparse's `ArgumentParser.error()` is the single
hook point. Subclass once, plumb through `add_parser()` factory
calls. We already have a custom argparse setup in `cli.py`'s
parser-build path; one subclass with the postscript is ~15 lines.

### Fix-3 (REQUIRED): documentation

- `--help` epilog for `thread send` and `broadcast`:
  ```
  Tip: for messages with backticks, em-dashes, double-asterisks, or
  multi-line markdown, pass via stdin to avoid shell-quoting
  surprises:
      cat <<'EOF' | claude-worker thread send NAME
      ...message...
      EOF
  ```
- README `thread send` section: add a "Quoting hazards" callout
  immediately after the existing positional/stdin example,
  enumerating the shell triggers and pointing at the stdin
  pattern.

### Considered and rejected

- **Always-prefer-stdin-when-non-tty (direction 3 in TICKET).**
  Considered. Rejected because it breaks an established UX
  expectation (positional always wins). It would also surprise
  callers who happen to have a non-TTY stdin without realizing it
  (CI runners, nested shells). The PM-recommended (1)+(4) path is
  more conservative.
- **Read every message via stdin, deprecate positional.** Too
  aggressive. Short positional messages are fine 95% of the time;
  refusing is the right granularity.
- **Auto-detect the shell and refuse based on shell version.**
  Out of scope; the universal hazard is shell-shape-agnostic.

## (d) Silent-drop trace — how does empty/wrong content reach `append_message`?

Walking the path for a representative silent-drop user input:

User runs:
```bash
claude-worker thread send pm-foo "Hi! Run \`ls\` and tell me **why** —"
```

What the shell hands argparse (assuming `ls` succeeds and outputs
filenames `a b c`):

```
argv = ["claude-worker","thread","send","pm-foo",
        "Hi! Run a b c and tell me **why** —"]
```

Wait — that's still one positional token because the outer `"..."`
re-quotes the whole substituted form. Right. So `args.message =
["Hi! Run a b c and tell me **why** —"]`. The user's intent ("ls"
literal) is lost; recipient sees the cwd's filenames inlined. That
content is non-empty, passes the empty check, goes through
`append_message` unchanged. Recipient gets a wrong-but-plausible
message.

Variant where the substitution fails (command not found):
```
argv = ["claude-worker","thread","send","pm-foo",
        "Hi! Run  and tell me **why** —"]
```
Same path; empty middle, no error. Recipient sees the gappy
message.

Variant with `$VAR` and `VAR` unset (and `set -u` not in effect —
typical interactive shells):
```
argv = ["...send","pm-foo","Hi! "]
```
Truncated to "Hi! ". Still non-empty, still passes empty check.

The ONLY current guard is `if not content.strip()` (cli.py:1938).
That fires only when the shell mangled the message ALL the way to
whitespace-only. Any non-whitespace remnant survives.

There is no checksum, length comparison, or "did the user mean to
type this?" sanity check. There can't be one without round-tripping
through the user's terminal — the canonical fix is to refuse
risky positional shapes and direct them to stdin where the input
goes byte-for-byte.

## (e) Test plan

New tests in `tests/test_send_positional_validation.py` (new file)
unless the existing `tests/test_send_flag_ordering.py` is the
better home — recommend a new file, since this is a separate
concern from the flag-reparse logic.

| #   | Case                                                        | Expected                                  |
|-----|-------------------------------------------------------------|-------------------------------------------|
| T1  | Positional message containing literal backtick token        | exit 1 + stderr names "backtick"          |
| T2  | Positional message containing `$(...)` substring            | exit 1 + stderr names "shell-substitution"|
| T3  | Positional message containing `${...}` substring            | exit 1 + stderr names "shell-substitution"|
| T4  | Positional message with em-dash and ≥3 tokens               | exit 1 + stderr names "em-dash"           |
| T5  | Positional message with `**bold**` and ≥3 tokens            | exit 1 + stderr names "double-asterisk"   |
| T6  | Positional message with `--port` (unknown flag) — argparse error path | exit 2 + stderr postscript names stdin pattern |
| T7  | Same content as T1-T5 piped via stdin                       | exit 0 + verbatim delivery (verified by reading thread JSONL) |
| T8  | Simple positional `"hello world"`                           | exit 0 + delivered (no regression)        |
| T9  | Positional message with single em-dash but ONE token        | exit 0 + delivered (em-dash alone is benign — heuristic only fires with multi-token bodies) |
| T10 | Positional `"--queue"` as the entire message                | reparse correctly extracts queue flag (existing behavior preserved) |
| T11 | Trailing `--queue` after positional message                 | reparse correctly extracts (no regression) |
| T12 | Multi-line stdin via `cat <<'EOF' \| claude-worker thread send` | exit 0 + verbatim including newlines |

T7 and T8 are the "no regression on simple cases" guards PM
asked for in the brief. T9 acknowledges that a single em-dash in a
short message ("Hello — friend") is fine; only multi-token bodies
with markdown markers are risky.

End-to-end test (extend `tests/test_end_to_end.py`): drive a real
manager + stub-claude through the stdin pipe path with a body
containing backticks and em-dashes; assert the thread JSONL
contains the bytes exactly as sent. This is the "stdin always
works verbatim" guarantee.

## (f) GVP mapping

- **G2 (loud-over-silent-failure)** — every Class B path becomes
  loud. Operators get an actionable message naming the trigger and
  pointing at the canonical stdin pattern.
- **V2 (explicit-over-implicit)** — the help text + README make
  the hazard explicit before the user types a positional message;
  the heuristic refusal makes the failure mode explicit at exec
  time.
- **G4 (composable-subcommands)** — the validator is shared by
  `cmd_send` and `cmd_broadcast` (both have the same positional
  path); single helper, two callers.
- **V5 (one-contiguous-block)** — change is contained to a new
  validator function in cli.py, the parser `error()` override (or
  wrapper), the help-text/README updates, and the new test file.
  No multi-file ripple.

D110 (proposed) — `positional-message-shell-hazard-detection`,
`extends: nothing` (orthogonal class), `maps_to: [project:G2,
project:V2]`.

OBS29 (PM library) — this fix CLOSES OBS29. Once D110 lands, the
PM's library can mark the observation resolved with a ref to D110.

## (g) Estimated LOE

- New validator function in cli.py: ~40 lines including the
  trigger table and stderr formatting.
- Argparse `error()` override (subclass + plumbing): ~20 lines.
- `cmd_send` + `cmd_broadcast` integration: ~10 lines (one call
  apiece; share the validator).
- README quoting-hazards callout: ~25 lines.
- Help-text epilog wiring on `thread send` and `broadcast`
  parsers: ~15 lines.
- New test file `tests/test_send_positional_validation.py`: ~150
  lines for T1-T11.
- E2e extension: ~30 lines.
- D110 entry: ~30 lines.
- Total: ~320 lines, single commit. One ephemeral worker
  (`impl-092`) round-trip.

## (h) Out of scope

- The `_reparse_send_flags` mechanism. It works. Heuristic
  refusal runs AFTER it.
- argparse's silent-strip of `--` separator. Detecting a stripped
  `--` requires inspecting `sys.argv` directly (we'd see the `--`
  there but not in `args.message`). Recommend deferring this
  detection unless a real-world report comes in — the heuristic
  refusal already catches the high-frequency cases.
- Auto-detecting whether stdin has data at exec time. The
  recommended fix doesn't depend on it; `cmd_send`'s current
  positional-wins-over-stdin behavior stays intact.
- Documentation of the chat-tag auto-prepend or queue-tag
  contract. Those are separate (D86, D109) and fine.
- Any change to `cmd_repl`'s send path. The TUI/REPL takes input
  via the curses-style cooked-mode entry, not argparse positional;
  no hazard.
