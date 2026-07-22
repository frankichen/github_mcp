package tushare

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestListSnapshotsMergesDailyBasicDataAndIndicators(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req requestBody
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			t.Fatalf("decode request: %v", err)
		}
		if req.Token != "token" {
			t.Fatalf("unexpected token: %s", req.Token)
		}
		switch req.APIName {
		case "stock_basic":
			writeResponse(t, w, []string{"ts_code", "symbol", "name", "area", "industry", "market", "list_date", "list_status", "is_hs"}, [][]any{{"600001.SH", "600001", "测试银行", "上海", "银行", "主板", "20000101", "L", "H"}})
		case "daily":
			writeResponse(t, w, []string{"ts_code", "trade_date", "open", "high", "low", "close", "pre_close", "change", "pct_chg", "vol", "amount"}, dailyItems())
		case "daily_basic":
			writeResponse(t, w, []string{"ts_code", "trade_date", "turnover_rate", "volume_ratio", "pe", "pb", "total_mv"}, [][]any{{"600001.SH", "20260518", 5.2, 1.8, 18, 2.2, 4800000}})
		default:
			t.Fatalf("unexpected api: %s", req.APIName)
		}
	}))
	defer server.Close()

	client := NewClient("token", WithBaseURL(server.URL), WithHTTPClient(server.Client()))
	got, err := client.ListSnapshots(context.Background(), "2026-05-18")
	if err != nil {
		t.Fatalf("ListSnapshots: %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("expected one snapshot, got %d", len(got))
	}
	if got[0].Code != "600001.SH" || got[0].Name != "测试银行" || got[0].TurnoverRate != 5.2 {
		t.Fatalf("unexpected snapshot: %+v", got[0])
	}
	if got[0].MA5 == 0 || got[0].RSI6 == 0 || got[0].FiveDayPct == 0 {
		t.Fatalf("expected technical indicators to be filled: %+v", got[0])
	}
}

func TestTradeCalendar(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req requestBody
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			t.Fatalf("decode request: %v", err)
		}
		if req.APIName != "trade_cal" {
			t.Fatalf("unexpected api: %s", req.APIName)
		}
		if req.Params["exchange"] != "SSE" || req.Params["start_date"] != "20260518" || req.Params["end_date"] != "20260519" {
			t.Fatalf("unexpected params: %+v", req.Params)
		}
		writeResponse(t, w, []string{"exchange", "cal_date", "is_open", "pretrade_date"}, [][]any{{"SSE", "20260518", 1, "20260515"}, {"SSE", "20260519", 1, "20260518"}})
	}))
	defer server.Close()

	client := NewClient("token", WithBaseURL(server.URL), WithHTTPClient(server.Client()))
	got, err := client.TradeCalendar(context.Background(), "sse", "2026-05-18", "2026-05-19")
	if err != nil {
		t.Fatalf("TradeCalendar: %v", err)
	}
	if len(got) != 2 || got[0].Exchange != "SSE" || !got[0].IsOpen || got[0].PreTradeDate != "20260515" {
		t.Fatalf("unexpected calendar: %+v", got)
	}
}

func TestLookbackStartDate(t *testing.T) {
	if got := LookbackStartDate("2026-05-18", 10); got != "20260508" {
		t.Fatalf("unexpected lookback start date: %s", got)
	}
}

func dailyItems() [][]any {
	items := make([][]any, 0, 61)
	for i := 0; i < 60; i++ {
		day := i + 1
		items = append(items, []any{"600001.SH", fmt.Sprintf("202604%02d", day), float64(day), float64(day), float64(day), float64(day), float64(day - 1), 1, 1, 10000, 350000})
	}
	items = append(items, []any{"600001.SH", "20260518", 61.0, 61.0, 61.0, 61.0, 60.0, 1.0, 1.67, 10000.0, 350000.0})
	return items
}

func writeResponse(t *testing.T, w http.ResponseWriter, fields []string, items [][]any) {
	t.Helper()
	w.Header().Set("Content-Type", "application/json")
	resp := responseBody{Code: 0}
	resp.Data.Fields = fields
	resp.Data.Items = items
	if err := json.NewEncoder(w).Encode(resp); err != nil {
		t.Fatalf("encode response: %v", err)
	}
}
