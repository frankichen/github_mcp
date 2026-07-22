package storage

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/frankichen/auto_gupiao/internal/domain"
)

type SnapshotCache struct {
	Path string
}

type snapshotCacheFile struct {
	Version    int                    `json:"version"`
	UpdatedAt  time.Time              `json:"updated_at"`
	TradingDay string                 `json:"trading_day"`
	Snapshots  []domain.StockSnapshot `json:"snapshots"`
}

func NewSnapshotCache(path string) SnapshotCache {
	return SnapshotCache{Path: path}
}

func (c SnapshotCache) Load(tradingDay string) ([]domain.StockSnapshot, bool, error) {
	if c.Path == "" {
		return nil, false, errors.New("empty snapshot cache path")
	}
	path := c.filePath(tradingDay)
	file, err := os.Open(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, fmt.Errorf("open snapshot cache: %w", err)
	}
	defer file.Close()

	var payload snapshotCacheFile
	if err := json.NewDecoder(file).Decode(&payload); err != nil {
		return nil, false, fmt.Errorf("decode snapshot cache: %w", err)
	}
	if tradingDay != "" && payload.TradingDay != normalizeDate(tradingDay) {
		return nil, false, nil
	}
	return payload.Snapshots, true, nil
}

func (c SnapshotCache) Save(tradingDay string, snapshots []domain.StockSnapshot) error {
	if c.Path == "" {
		return errors.New("empty snapshot cache path")
	}
	day := normalizeDate(tradingDay)
	path := c.filePath(day)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return fmt.Errorf("create cache dir: %w", err)
	}
	tmp := path + ".tmp"
	file, err := os.Create(tmp)
	if err != nil {
		return fmt.Errorf("create snapshot cache: %w", err)
	}
	payload := snapshotCacheFile{
		Version:    1,
		UpdatedAt:  time.Now().UTC(),
		TradingDay: day,
		Snapshots:  snapshots,
	}
	encoder := json.NewEncoder(file)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(payload); err != nil {
		file.Close()
		_ = os.Remove(tmp)
		return fmt.Errorf("encode snapshot cache: %w", err)
	}
	if err := file.Close(); err != nil {
		_ = os.Remove(tmp)
		return fmt.Errorf("close snapshot cache: %w", err)
	}
	if err := os.Rename(tmp, path); err != nil {
		_ = os.Remove(tmp)
		return fmt.Errorf("replace snapshot cache: %w", err)
	}
	return nil
}

func (c SnapshotCache) filePath(tradingDay string) string {
	day := normalizeDate(tradingDay)
	if day == "" {
		return c.Path
	}
	if looksLikeJSONFile(c.Path) {
		return c.Path
	}
	return filepath.Join(c.Path, day+".json")
}

func looksLikeJSONFile(path string) bool {
	return strings.EqualFold(filepath.Ext(path), ".json")
}

func normalizeDate(date string) string {
	return strings.ReplaceAll(strings.TrimSpace(date), "-", "")
}
