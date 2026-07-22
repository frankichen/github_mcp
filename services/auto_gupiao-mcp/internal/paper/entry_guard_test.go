package paper

import (
	"testing"

	"github.com/frankichen/auto_gupiao/internal/domain"
)

func TestEntryGuardReasonMatchesEachRule(t *testing.T) {
	tests := []struct {
		name   string
		s      domain.StockSnapshot
		guard  EntryGuard
		reason string
	}{
		{
			name:   "atr",
			s:      domain.StockSnapshot{ATR20Pct: 8.1},
			guard:  EntryGuard{MaxATR20Pct: 8},
			reason: "entry_atr20_gt_8.00",
		},
		{
			name:   "drawdown20",
			s:      domain.StockSnapshot{MaxDrawdown20Pct: 18.1},
			guard:  EntryGuard{MaxDrawdown20Pct: 18},
			reason: "entry_drawdown20_gt_18.00",
		},
		{
			name:   "daily_drop20",
			s:      domain.StockSnapshot{MaxDailyDrop20Pct: -7.1},
			guard:  EntryGuard{MaxDailyDrop20Pct: -7},
			reason: "entry_daily_drop20_lt_-7.00",
		},
		{
			name:   "close_ma20_distance",
			s:      domain.StockSnapshot{CloseMA20DistancePct: 12.1},
			guard:  EntryGuard{MaxCloseMA20DistancePct: 12},
			reason: "entry_close_ma20_distance_gt_12.00",
		},
		{
			name:   "five_day",
			s:      domain.StockSnapshot{FiveDayPct: 15.1},
			guard:  EntryGuard{MaxFiveDayPct: 15},
			reason: "entry_five_day_gt_15.00",
		},
		{
			name:   "twenty_day",
			s:      domain.StockSnapshot{TwentyDayPct: 35.1},
			guard:  EntryGuard{MaxTwentyDayPct: 35},
			reason: "entry_twenty_day_gt_35.00",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := entryGuardReason(tt.s, tt.guard); got != tt.reason {
				t.Fatalf("unexpected reason: %s", got)
			}
		})
	}
}

func TestEntryGuardReasonEmptyGuardDoesNotFilter(t *testing.T) {
	s := domain.StockSnapshot{
		ATR20Pct:             100,
		MaxDrawdown20Pct:     100,
		MaxDailyDrop20Pct:    -100,
		CloseMA20DistancePct: 100,
		FiveDayPct:           100,
		TwentyDayPct:         100,
	}
	if got := entryGuardReason(s, EntryGuard{}); got != "" {
		t.Fatalf("empty guard should not filter: %s", got)
	}
}
