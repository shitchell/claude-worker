# 030: Investigation — wait-for-turn silent no-op

## Verdict: Bug NOT confirmed. Code is correct.

## Investigation (P6: look before trying)

Traced _wait_for_turn for the reported scenario: after_uuid=X where
X is the most recent log entry (a result message).

### Reverse scan path:
1. Iterator yields entries newest→oldest
2. First entry: result with uuid=X
3. after_uuid check: _uuid_matches(X, X) → True → **break**
4. turn_end_after_last_user stays None (no turn found after marker)
5. Falls through settle check (None gate)
6. Checks _manager_alive() → depends on worker state

### If manager is alive:
- Falls into tail loop → seeks to EOF → polls for new lines
- No new lines → loops with sleep(POLL_INTERVAL_SECONDS)
- Returns 2 (timeout) after timeout expires
- **Correct behavior**

### If manager is dead:
- Returns 1 ("worker process died") with stderr message
- **Correct behavior — but consumer might misread exit code**

### Tests written:
1. after_uuid = last result, alive → rc=2 (timeout) ✓
2. after_uuid = assistant before result, alive → rc=0 (finds result) ✓
3. after_uuid = last result, dead → rc=1 ✓

All pass — no code change needed.

### Likely explanation for the consumer's report:
The GVP PM was idle for 8h38m. If it was actually dead (stale PID
from the /tmp→~/.cwork migration), _manager_alive() returns False,
_wait_for_turn returns 1 (dead) quickly. The consumer may have
checked $? incorrectly or confused exit 1 with exit 0.

Alternative: the consumer ran without --after-uuid and the existing
result in the log matched immediately (the baseline behavior before
D2 was applied).

## Decision

No code fix. The tests confirm correct behavior for all three
hypotheses. The consumer's report is not reproducible with the
current code. The tests serve as regression guards (G3).
