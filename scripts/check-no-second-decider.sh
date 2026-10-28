#!/usr/bin/env bash
# Fail if this repo grows display / state / thermal decision logic of its own.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
bad=0

# Deleted Python totals encoder must stay gone.
if grep -RIn --include='*.py' 'def encode_verdict_totals' "$root/klvr_emulator" "$root/tests" 2>/dev/null; then
	echo "check-no-second-decider: encode_verdict_totals must not return"
	bad=1
fi

# The linear SoC slope from #18 must stay deleted.
if grep -RIn --include='*.py' 'total_time_for_full_charge = 7200' "$root" 2>/dev/null; then
	echo "check-no-second-decider: 7200 s / 95% SoC slope must not return"
	bad=1
fi
if grep -RIn --include='*.py' '0\.0264' "$root/klvr_emulator" 2>/dev/null; then
	echo "check-no-second-decider: SoC increment loop must not return"
	bad=1
fi

# No local thermal threshold copies.
if grep -RIn --include='*.py' -E 'OVERTEMP|XTAR_OVER|THERMAL_THROTTLE_ENTER' "$root/klvr_emulator" 2>/dev/null; then
	echo "check-no-second-decider: thermal constants must stay in firmware"
	bad=1
fi

if [[ "$bad" -ne 0 ]]; then
	exit 1
fi
echo "check-no-second-decider: ok"
