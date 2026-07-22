package webhook

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestSignStable(t *testing.T) {
	got := Sign("secret", "123", []byte(`{"a":1}`))
	want := "3f9ad62954696e793fcbb94a80946e0ddeede9b8e19f2b79567b32c5a10fb24e"
	if got != want {
		t.Fatalf("unexpected signature: got %s want %s", got, want)
	}
}

func TestSendWebhookSuccessWithSignature(t *testing.T) {
	var received Payload
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get(HeaderTimestamp) != "1770000000" {
			t.Fatalf("unexpected timestamp: %s", r.Header.Get(HeaderTimestamp))
		}
		if r.Header.Get(HeaderEvent) != "daily_report.generated" {
			t.Fatalf("unexpected event: %s", r.Header.Get(HeaderEvent))
		}
		if r.Header.Get(HeaderIdempotencyKey) != "auto_gupiao:daily_report.generated:20260522:123" {
			t.Fatalf("unexpected idempotency key: %s", r.Header.Get(HeaderIdempotencyKey))
		}
		if r.Header.Get(HeaderSignature) == "" {
			t.Fatalf("missing signature")
		}
		if err := json.NewDecoder(r.Body).Decode(&received); err != nil {
			t.Fatalf("decode payload: %v", err)
		}
		w.WriteHeader(http.StatusAccepted)
		_, _ = w.Write([]byte("ok"))
	}))
	defer server.Close()

	client := NewClient(server.URL, "secret")
	client.Now = func() time.Time { return time.Unix(1770000000, 0) }
	resp, err := client.Send(context.Background(), samplePayload())
	if err != nil {
		t.Fatalf("Send failed: %v", err)
	}
	if resp.StatusCode != http.StatusAccepted || received.RunID != 123 {
		t.Fatalf("unexpected response=%+v payload=%+v", resp, received)
	}
}

func TestSendWebhookEmptyURL(t *testing.T) {
	_, err := NewClient("", "").Send(context.Background(), samplePayload())
	if err == nil {
		t.Fatalf("expected error")
	}
}

func TestSendWebhookNon2xx(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "bad", http.StatusBadGateway)
	}))
	defer server.Close()
	_, err := NewClient(server.URL, "").Send(context.Background(), samplePayload())
	if err == nil {
		t.Fatalf("expected non-2xx error")
	}
}

func samplePayload() Payload {
	return Payload{
		Source:         "auto_gupiao",
		Event:          "daily_report.generated",
		IdempotencyKey: "auto_gupiao:daily_report.generated:20260522:123",
		RunID:          123,
		TradeDate:      "20260522",
		GeneratedAt:    "2026-05-22T15:30:00+08:00",
		Bars:           BarsPayload{StartDate: "20250102", EndDate: "20260522", Codes: []string{"000001"}, Rows: 1},
		Summary:        SummaryPayload{RiskLevel: "低", Conclusion: "ok", InitialCash: 10000, FinalEquity: 10100, TotalReturnPct: 1, MaxDrawdownPct: 0.5, Trades: 1, WinRatePct: 100, MaxConsecutiveLoss: 0},
		Reports:        ReportsPayload{MarkdownURL: "https://example.com/a.md", TradesURL: "https://example.com/a_trades.csv", EquityURL: "https://example.com/a_equity.csv"},
	}
}
