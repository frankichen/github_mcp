package dashboard

import (
	"context"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"
	"time"

	"github.com/frankichen/auto_gupiao/internal/dataset"
	"github.com/frankichen/auto_gupiao/internal/paper"
	"github.com/frankichen/auto_gupiao/internal/storage"
)

func TestDashboardIndexWithAuth(t *testing.T) {
	dbPath := filepath.Join(t.TempDir(), "autogupiao.db")
	store := storage.NewSQLiteStore(dbPath)
	_, err := store.SaveDailyRun(context.Background(), storage.DailyRunRecord{
		GeneratedAt: time.Date(2026, 5, 20, 16, 0, 0, 0, time.UTC),
		TradeDate:   "20260520",
		Bars:        dataset.Summary{Rows: 1, Codes: []string{"000001"}, StartDate: "20260520", EndDate: "20260520"},
		Paper:       paper.Result{InitialCash: 10000, FinalEquity: 10100, TotalReturnPct: 1, Days: 1, Trades: 0, DailyEquity: []paper.DailyEquity{{Date: "20260520", Equity: 10100}}},
		RiskLevel:   "低",
		Conclusion:  "测试结论",
	})
	if err != nil {
		t.Fatalf("SaveDailyRun failed: %v", err)
	}
	server := NewServer(Config{DBPath: dbPath, Username: "u", Password: "p"})

	request := httptest.NewRequest(http.MethodGet, "/", nil)
	response := httptest.NewRecorder()
	server.authMiddleware(server.mux).ServeHTTP(response, request)
	if response.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401, got %d", response.Code)
	}

	request = httptest.NewRequest(http.MethodGet, "/", nil)
	request.SetBasicAuth("u", "p")
	response = httptest.NewRecorder()
	server.authMiddleware(server.mux).ServeHTTP(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d body=%s", response.Code, response.Body.String())
	}
	if !stringsContains(response.Body.String(), "A股观察盘") || !stringsContains(response.Body.String(), "20260520") {
		t.Fatalf("unexpected body: %s", response.Body.String())
	}
}

func stringsContains(s, sub string) bool {
	return len(sub) == 0 || (len(s) >= len(sub) && (s == sub || stringsContains(s[1:], sub) || s[:len(sub)] == sub))
}
