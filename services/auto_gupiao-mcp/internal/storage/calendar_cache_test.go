package storage

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/frankichen/auto_gupiao/internal/domain"
)

func TestCalendarCacheRoundTripWithDirectoryPath(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "calendar")
	cache := NewCalendarCache(dir)
	days := []domain.TradingDay{
		{Exchange: "SSE", Date: "20260518", IsOpen: true, PreTradeDate: "20260515"},
		{Exchange: "SSE", Date: "20260519", IsOpen: true, PreTradeDate: "20260518"},
	}
	if err := cache.Save("sse", "2026-05-18", "2026-05-19", days); err != nil {
		t.Fatalf("save calendar: %v", err)
	}
	filePath := filepath.Join(dir, "SSE_20260518_20260519.json")
	if _, err := os.Stat(filePath); err != nil {
		t.Fatalf("expected calendar cache file %s: %v", filePath, err)
	}
	got, ok, err := cache.Load("SSE", "20260518", "20260519")
	if err != nil {
		t.Fatalf("load calendar: %v", err)
	}
	if !ok || len(got) != 2 || !got[0].IsOpen {
		t.Fatalf("unexpected calendar result ok=%v got=%+v", ok, got)
	}
}

func TestCalendarCacheMissForDifferentRange(t *testing.T) {
	cache := NewCalendarCache(filepath.Join(t.TempDir(), "calendar.json"))
	if err := cache.Save("SSE", "20260518", "20260519", []domain.TradingDay{{Exchange: "SSE", Date: "20260518", IsOpen: true}}); err != nil {
		t.Fatalf("save calendar: %v", err)
	}
	_, ok, err := cache.Load("SSE", "20260517", "20260519")
	if err != nil {
		t.Fatalf("load calendar: %v", err)
	}
	if ok {
		t.Fatalf("expected cache miss for a different date range")
	}
}
