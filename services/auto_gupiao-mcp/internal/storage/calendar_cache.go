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

type CalendarCache struct {
	Path string
}

type calendarCacheFile struct {
	Version   int                 `json:"version"`
	UpdatedAt time.Time           `json:"updated_at"`
	Exchange  string              `json:"exchange"`
	StartDate string              `json:"start_date"`
	EndDate   string              `json:"end_date"`
	Days      []domain.TradingDay `json:"days"`
}

func NewCalendarCache(path string) CalendarCache {
	return CalendarCache{Path: path}
}

func (c CalendarCache) Load(exchange string, startDate string, endDate string) ([]domain.TradingDay, bool, error) {
	if c.Path == "" {
		return nil, false, errors.New("empty calendar cache path")
	}
	path := c.filePath(exchange, startDate, endDate)
	file, err := os.Open(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, fmt.Errorf("open calendar cache: %w", err)
	}
	defer file.Close()

	var payload calendarCacheFile
	if err := json.NewDecoder(file).Decode(&payload); err != nil {
		return nil, false, fmt.Errorf("decode calendar cache: %w", err)
	}
	if payload.Exchange != normalizeExchange(exchange) || payload.StartDate != normalizeDate(startDate) || payload.EndDate != normalizeDate(endDate) {
		return nil, false, nil
	}
	return payload.Days, true, nil
}

func (c CalendarCache) Save(exchange string, startDate string, endDate string, days []domain.TradingDay) error {
	if c.Path == "" {
		return errors.New("empty calendar cache path")
	}
	ex := normalizeExchange(exchange)
	start := normalizeDate(startDate)
	end := normalizeDate(endDate)
	path := c.filePath(ex, start, end)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return fmt.Errorf("create calendar cache dir: %w", err)
	}
	tmp := path + ".tmp"
	file, err := os.Create(tmp)
	if err != nil {
		return fmt.Errorf("create calendar cache: %w", err)
	}
	payload := calendarCacheFile{
		Version:   1,
		UpdatedAt: time.Now().UTC(),
		Exchange:  ex,
		StartDate: start,
		EndDate:   end,
		Days:      days,
	}
	encoder := json.NewEncoder(file)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(payload); err != nil {
		file.Close()
		_ = os.Remove(tmp)
		return fmt.Errorf("encode calendar cache: %w", err)
	}
	if err := file.Close(); err != nil {
		_ = os.Remove(tmp)
		return fmt.Errorf("close calendar cache: %w", err)
	}
	if err := os.Rename(tmp, path); err != nil {
		_ = os.Remove(tmp)
		return fmt.Errorf("replace calendar cache: %w", err)
	}
	return nil
}

func (c CalendarCache) filePath(exchange string, startDate string, endDate string) string {
	ex := normalizeExchange(exchange)
	start := normalizeDate(startDate)
	end := normalizeDate(endDate)
	if looksLikeJSONFile(c.Path) {
		return c.Path
	}
	return filepath.Join(c.Path, ex+"_"+start+"_"+end+".json")
}

func normalizeExchange(exchange string) string {
	ex := strings.ToUpper(strings.TrimSpace(exchange))
	if ex == "" {
		return "SSE"
	}
	return ex
}
