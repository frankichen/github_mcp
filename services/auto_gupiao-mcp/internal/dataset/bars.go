package dataset

import (
	"bytes"
	"encoding/csv"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/frankichen/auto_gupiao/internal/domain"
)

type Summary struct {
	Rows      int      `json:"rows"`
	Codes     []string `json:"codes"`
	StartDate string   `json:"start_date,omitempty"`
	EndDate   string   `json:"end_date,omitempty"`
	Output    string   `json:"output,omitempty"`
}

func ParseCodes(value string) []string {
	seen := map[string]struct{}{}
	codes := make([]string, 0)
	for _, part := range splitList(value) {
		code := strings.TrimSpace(part)
		if code == "" {
			continue
		}
		code = strings.TrimPrefix(strings.TrimPrefix(strings.ToLower(code), "sh"), "sz")
		code = strings.TrimPrefix(code, "bj")
		if _, ok := seen[code]; ok {
			continue
		}
		seen[code] = struct{}{}
		codes = append(codes, code)
	}
	return codes
}

func ParsePaths(value string) []string {
	seen := map[string]struct{}{}
	paths := make([]string, 0)
	for _, part := range splitPathList(value) {
		path := strings.TrimSpace(part)
		if path == "" {
			continue
		}
		if _, ok := seen[path]; ok {
			continue
		}
		seen[path] = struct{}{}
		paths = append(paths, path)
	}
	return paths
}

func splitList(value string) []string {
	return strings.FieldsFunc(value, func(r rune) bool {
		return r == ',' || r == ';' || r == '\n' || r == '\r' || r == '\t' || r == ' '
	})
}

func splitPathList(value string) []string {
	return strings.FieldsFunc(value, func(r rune) bool {
		return r == ',' || r == ';' || r == '\n' || r == '\r' || r == '\t'
	})
}

func ReadCodesFile(path string) ([]string, error) {
	if path == "" {
		return nil, nil
	}
	content, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read codes file: %w", err)
	}
	return ParseCodes(string(content)), nil
}

func MergeCodes(inline []string, fromFile []string) []string {
	seen := map[string]struct{}{}
	out := make([]string, 0, len(inline)+len(fromFile))
	for _, code := range append(inline, fromFile...) {
		code = strings.TrimSpace(code)
		if code == "" {
			continue
		}
		if _, ok := seen[code]; ok {
			continue
		}
		seen[code] = struct{}{}
		out = append(out, code)
	}
	return out
}

func SortBars(bars []domain.DailyBar) {
	sort.SliceStable(bars, func(i, j int) bool {
		if bars[i].Date == bars[j].Date {
			return bars[i].Code < bars[j].Code
		}
		return bars[i].Date < bars[j].Date
	})
}

func WriteBarsCSVFile(path string, bars []domain.DailyBar) error {
	dir := filepath.Dir(path)
	if dir != "." && dir != "" {
		if err := os.MkdirAll(dir, 0755); err != nil {
			return fmt.Errorf("create output directory: %w", err)
		}
	}
	file, err := os.Create(path)
	if err != nil {
		return fmt.Errorf("create bars csv: %w", err)
	}
	defer file.Close()
	return WriteBarsCSV(file, bars)
}

func WriteBarsCSV(w io.Writer, bars []domain.DailyBar) error {
	SortBars(bars)
	writer := csv.NewWriter(w)
	if err := writer.Write([]string{"date", "code", "open", "high", "low", "close", "prev_close", "change", "change_pct", "volume", "amount"}); err != nil {
		return err
	}
	for _, bar := range bars {
		record := []string{
			bar.Date,
			bar.Code,
			fmtFloat(bar.Open),
			fmtFloat(bar.High),
			fmtFloat(bar.Low),
			fmtFloat(bar.Close),
			fmtFloat(bar.PrevClose),
			fmtFloat(bar.Change),
			fmtFloat(bar.ChangePct),
			fmtFloat(bar.Volume),
			fmtFloat(bar.Amount),
		}
		if err := writer.Write(record); err != nil {
			return err
		}
	}
	writer.Flush()
	return writer.Error()
}

func RenderBarsCSV(bars []domain.DailyBar) string {
	var buf bytes.Buffer
	_ = WriteBarsCSV(&buf, bars)
	return buf.String()
}

func SummarizeBars(bars []domain.DailyBar, output string) Summary {
	seen := map[string]struct{}{}
	codes := make([]string, 0)
	start := ""
	end := ""
	for _, bar := range bars {
		if _, ok := seen[bar.Code]; !ok && bar.Code != "" {
			seen[bar.Code] = struct{}{}
			codes = append(codes, bar.Code)
		}
		if bar.Date != "" && (start == "" || bar.Date < start) {
			start = bar.Date
		}
		if bar.Date != "" && bar.Date > end {
			end = bar.Date
		}
	}
	sort.Strings(codes)
	return Summary{Rows: len(bars), Codes: codes, StartDate: start, EndDate: end, Output: output}
}

func fmtFloat(v float64) string {
	return fmt.Sprintf("%.6f", v)
}
