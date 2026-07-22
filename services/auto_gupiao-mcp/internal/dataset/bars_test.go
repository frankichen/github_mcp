package dataset

import (
	"strings"
	"testing"

	"github.com/frankichen/auto_gupiao/internal/domain"
)

func TestParseCodes(t *testing.T) {
	codes := ParseCodes("000001, sh600000\nSZ000002; bj430047 000001")
	want := []string{"000001", "600000", "000002", "430047"}
	if len(codes) != len(want) {
		t.Fatalf("unexpected codes: %+v", codes)
	}
	for i := range want {
		if codes[i] != want[i] {
			t.Fatalf("codes[%d]=%s want %s", i, codes[i], want[i])
		}
	}
}

func TestParsePathsKeepsWindowsPathsWithSpaces(t *testing.T) {
	paths := ParsePaths(`C:\Users\me\My Data\a.csv;D:\bars\b.csv,C:\Users\me\My Data\a.csv`)
	want := []string{`C:\Users\me\My Data\a.csv`, `D:\bars\b.csv`}
	if len(paths) != len(want) {
		t.Fatalf("unexpected paths: %+v", paths)
	}
	for i := range want {
		if paths[i] != want[i] {
			t.Fatalf("paths[%d]=%s want %s", i, paths[i], want[i])
		}
	}
}

func TestWriteBarsCSVSortsAndSummarizes(t *testing.T) {
	bars := []domain.DailyBar{
		{Date: "20260520", Code: "600000", Open: 2, High: 3, Low: 1, Close: 2.5},
		{Date: "20260519", Code: "000001", Open: 1, High: 2, Low: 1, Close: 1.5},
	}
	csvText := RenderBarsCSV(bars)
	if !strings.Contains(csvText, "date,code,open") {
		t.Fatalf("missing header: %s", csvText)
	}
	firstData := strings.Split(strings.TrimSpace(csvText), "\n")[1]
	if !strings.Contains(firstData, "20260519,000001") {
		t.Fatalf("not sorted: %s", csvText)
	}
	summary := SummarizeBars(bars, "out.csv")
	if summary.Rows != 2 || summary.StartDate != "20260519" || summary.EndDate != "20260520" || len(summary.Codes) != 2 {
		t.Fatalf("unexpected summary: %+v", summary)
	}
}

func TestMergeCodes(t *testing.T) {
	merged := MergeCodes([]string{"000001", "600000"}, []string{"600000", "000002"})
	if strings.Join(merged, ",") != "000001,600000,000002" {
		t.Fatalf("unexpected merged codes: %+v", merged)
	}
}
