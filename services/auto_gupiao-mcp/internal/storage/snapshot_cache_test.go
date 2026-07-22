package storage

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/frankichen/auto_gupiao/internal/domain"
)

func TestSnapshotCacheRoundTripWithSingleFilePath(t *testing.T) {
	cache := NewSnapshotCache(filepath.Join(t.TempDir(), "snapshots.json"))
	snapshots := []domain.StockSnapshot{{Date: "20260518", Code: "600001.SH", Name: "测试银行", Close: 9.8}}
	if err := cache.Save("20260518", snapshots); err != nil {
		t.Fatalf("save cache: %v", err)
	}
	got, ok, err := cache.Load("20260518")
	if err != nil {
		t.Fatalf("load cache: %v", err)
	}
	if !ok || len(got) != 1 || got[0].Code != "600001.SH" {
		t.Fatalf("unexpected cache result ok=%v got=%+v", ok, got)
	}
	_, ok, err = cache.Load("20260519")
	if err != nil {
		t.Fatalf("load other day: %v", err)
	}
	if ok {
		t.Fatalf("expected cache miss for another trading day")
	}
}

func TestSnapshotCacheRoundTripWithDirectoryPath(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "snapshots")
	cache := NewSnapshotCache(dir)
	snapshots := []domain.StockSnapshot{{Date: "20260518", Code: "000001.SZ", Name: "平安银行", Close: 12.3}}
	if err := cache.Save("2026-05-18", snapshots); err != nil {
		t.Fatalf("save cache: %v", err)
	}
	filePath := filepath.Join(dir, "20260518.json")
	if _, err := os.Stat(filePath); err != nil {
		t.Fatalf("expected sharded cache file %s: %v", filePath, err)
	}
	got, ok, err := cache.Load("20260518")
	if err != nil {
		t.Fatalf("load cache: %v", err)
	}
	if !ok || len(got) != 1 || got[0].Code != "000001.SZ" {
		t.Fatalf("unexpected sharded cache result ok=%v got=%+v", ok, got)
	}
}
