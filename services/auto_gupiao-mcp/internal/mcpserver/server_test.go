package mcpserver

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/frankichen/auto_gupiao/internal/dataset"
	"github.com/frankichen/auto_gupiao/internal/paper"
	"github.com/frankichen/auto_gupiao/internal/report"
	"github.com/frankichen/auto_gupiao/internal/storage"
)

func TestMCPToolsListAndLatest(t *testing.T) {
	dir := t.TempDir()
	latestFile := filepath.Join(dir, "latest.json")
	latest := LatestReport{RunID: 7, TradeDate: "20260522", DataStatus: "fresh", RiskLevel: "中", MarkdownURL: "https://example.com/a.md"}
	content, _ := json.Marshal(latest)
	if err := os.WriteFile(latestFile, content, 0644); err != nil {
		t.Fatalf("write latest: %v", err)
	}
	server := NewServer(Config{LatestFile: latestFile, DBPath: filepath.Join(dir, "test.db"), Token: "secret"})

	listResp := rpc(t, server, "secret", RPCRequest{JSONRPC: "2.0", ID: 1, Method: "tools/list"})
	if listResp.Error != nil {
		t.Fatalf("tools/list error: %+v", listResp.Error)
	}
	body, _ := json.Marshal(listResp.Result)
	if !bytes.Contains(body, []byte("get_latest_report")) || !bytes.Contains(body, []byte("list_recent_runs")) {
		t.Fatalf("unexpected tools/list: %s", string(body))
	}

	call := RPCRequest{JSONRPC: "2.0", ID: 2, Method: "tools/call", Params: mustJSON(ToolCallParams{Name: "get_latest_report"})}
	latestResp := rpc(t, server, "secret", call)
	if latestResp.Error != nil {
		t.Fatalf("latest error: %+v", latestResp.Error)
	}
	body, _ = json.Marshal(latestResp.Result)
	if !bytes.Contains(body, []byte("20260522")) || !bytes.Contains(body, []byte("fresh")) {
		t.Fatalf("unexpected latest result: %s", string(body))
	}
}

func TestMCPRecentRunsDetailAndNote(t *testing.T) {
	dir := t.TempDir()
	store := storage.NewSQLiteStore(filepath.Join(dir, "test.db"))
	pf := 1.23
	runID, err := store.SaveDailyRun(context.Background(), storage.DailyRunRecord{
		GeneratedAt: time.Date(2026, 5, 22, 16, 0, 0, 0, time.UTC),
		TradeDate:   "20260522",
		Bars:        dataset.Summary{Rows: 1, Codes: []string{"000001"}},
		Paper: paper.Result{
			InitialCash:        10000,
			FinalEquity:        10100,
			TotalReturnPct:     1,
			MaxDrawdownPct:     0.5,
			Trades:             1,
			Wins:               1,
			WinRatePct:         100,
			ProfitFactor:       &pf,
			MaxConsecutiveLoss: 0,
			DailyEquity:        []paper.DailyEquity{{Date: "20260522", Cash: 10100, Equity: 10100}},
			TradesList:         []paper.Trade{{Code: "000001", BuyDate: "20260521", SellDate: "20260522", ExitReason: "take_profit", NetProfit: 100}},
		},
		Report:     report.Manifest{MarkdownPath: "reports/a.md", TradesCSV: "reports/a_trades.csv", EquityCSV: "reports/a_equity.csv"},
		ReportURLs: storage.ReportURLs{MarkdownURL: "https://example.com/a.md"},
	})
	if err != nil {
		t.Fatalf("save run: %v", err)
	}
	if err := store.SaveRunNote(context.Background(), storage.RunNote{RunID: runID, Status: storage.NoteStatusChecked, Memo: "ok"}); err != nil {
		t.Fatalf("save note: %v", err)
	}
	server := NewServer(Config{DBPath: store.Path, LatestFile: filepath.Join(dir, "latest.json")})

	recent := rpc(t, server, "", RPCRequest{JSONRPC: "2.0", ID: 1, Method: "tools/call", Params: mustJSON(ToolCallParams{Name: "list_recent_runs", Arguments: map[string]any{"limit": 5}})})
	if recent.Error != nil {
		t.Fatalf("recent error: %+v", recent.Error)
	}
	body, _ := json.Marshal(recent.Result)
	if !bytes.Contains(body, []byte("20260522")) || !bytes.Contains(body, []byte("https://example.com/a.md")) {
		t.Fatalf("unexpected recent result: %s", string(body))
	}

	detail := rpc(t, server, "", RPCRequest{JSONRPC: "2.0", ID: 2, Method: "tools/call", Params: mustJSON(ToolCallParams{Name: "get_run_detail", Arguments: map[string]any{"run_id": float64(runID)}})})
	if detail.Error != nil {
		t.Fatalf("detail error: %+v", detail.Error)
	}
	body, _ = json.Marshal(detail.Result)
	if !bytes.Contains(body, []byte("take_profit")) || !bytes.Contains(body, []byte("000001")) {
		t.Fatalf("unexpected detail result: %s", string(body))
	}

	note := rpc(t, server, "", RPCRequest{JSONRPC: "2.0", ID: 3, Method: "tools/call", Params: mustJSON(ToolCallParams{Name: "get_run_note", Arguments: map[string]any{"run_id": float64(runID)}})})
	if note.Error != nil {
		t.Fatalf("note error: %+v", note.Error)
	}
	body, _ = json.Marshal(note.Result)
	if !bytes.Contains(body, []byte("checked")) || !bytes.Contains(body, []byte("ok")) {
		t.Fatalf("unexpected note result: %s", string(body))
	}
}

func TestMCPAuth(t *testing.T) {
	server := NewServer(Config{Token: "secret", DBPath: filepath.Join(t.TempDir(), "test.db")})
	req := httptest.NewRequest(http.MethodPost, "/mcp", bytes.NewReader(mustJSON(RPCRequest{JSONRPC: "2.0", ID: 1, Method: "tools/list"})))
	w := httptest.NewRecorder()
	server.authMiddleware(server.mux).ServeHTTP(w, req)
	if w.Code != http.StatusUnauthorized {
		t.Fatalf("expected unauthorized, got %d", w.Code)
	}
}

func TestMCPNoAuthBypassesTokenCheck(t *testing.T) {
	server := NewServer(Config{Token: "secret", NoAuth: true, DBPath: filepath.Join(t.TempDir(), "test.db")})
	resp := rpc(t, server, "", RPCRequest{JSONRPC: "2.0", ID: 1, Method: "tools/list"})
	if resp.Error != nil {
		t.Fatalf("tools/list error: %+v", resp.Error)
	}
	tools, ok := resultMap(t, resp)["tools"].([]any)
	if !ok || len(tools) != 4 {
		t.Fatalf("unexpected tools: %#v", resp.Result)
	}
}

func TestMCPTokenStillRequiredWhenNoAuthDisabled(t *testing.T) {
	server := NewServer(Config{Token: "secret", DBPath: filepath.Join(t.TempDir(), "test.db")})
	req := httptest.NewRequest(http.MethodPost, "/mcp", bytes.NewReader(mustJSON(RPCRequest{JSONRPC: "2.0", ID: 1, Method: "tools/list"})))
	w := httptest.NewRecorder()
	server.authMiddleware(server.mux).ServeHTTP(w, req)
	if w.Code != http.StatusUnauthorized {
		t.Fatalf("expected unauthorized, got %d", w.Code)
	}
}

func TestMCPInitializeProtocolVersions(t *testing.T) {
	server := NewServer(Config{DBPath: filepath.Join(t.TempDir(), "test.db")})
	for _, version := range []string{"2024-11-05", "2025-03-26", "2025-06-18"} {
		t.Run(version, func(t *testing.T) {
			resp := rpc(t, server, "", RPCRequest{JSONRPC: "2.0", ID: 1, Method: "initialize", Params: mustJSON(map[string]any{"protocolVersion": version})})
			if resp.Error != nil {
				t.Fatalf("initialize error: %+v", resp.Error)
			}
			result := resultMap(t, resp)
			if got := result["protocolVersion"]; got != version {
				t.Fatalf("expected protocolVersion %s, got %v", version, got)
			}
			capabilities, ok := result["capabilities"].(map[string]any)
			if !ok {
				t.Fatalf("missing capabilities: %#v", result["capabilities"])
			}
			if _, ok := capabilities["tools"].(map[string]any); !ok {
				t.Fatalf("missing tools capability: %#v", capabilities)
			}
		})
	}
}

func TestMCPSSEEndpointUsesPublicBaseURL(t *testing.T) {
	server := NewServer(Config{DBPath: filepath.Join(t.TempDir(), "test.db"), PublicBaseURL: "https://mcp.555044.xyz/"})
	body, headers := sseBody(t, server, httptest.NewRequest(http.MethodGet, "/sse", nil))
	if !strings.Contains(body, "data: https://mcp.555044.xyz/messages?sessionId=") {
		t.Fatalf("unexpected SSE body: %q", body)
	}
	if got := headers.Get("Cache-Control"); got != "no-cache, no-transform" {
		t.Fatalf("unexpected cache control %q", got)
	}
	if got := headers.Get("X-Accel-Buffering"); got != "no" {
		t.Fatalf("unexpected buffering header %q", got)
	}
}

func TestMCPSSEEndpointUsesForwardedHeaders(t *testing.T) {
	server := NewServer(Config{DBPath: filepath.Join(t.TempDir(), "test.db")})
	req := httptest.NewRequest(http.MethodGet, "/sse", nil)
	req.Header.Set("X-Forwarded-Proto", "https")
	req.Header.Set("X-Forwarded-Host", "mcp.555044.xyz")
	body, _ := sseBody(t, server, req)
	if !strings.Contains(body, "data: https://mcp.555044.xyz/messages?sessionId=") {
		t.Fatalf("unexpected SSE body: %q", body)
	}
}

func TestMCPSSEEndpointFallsBackToRelativePath(t *testing.T) {
	server := NewServer(Config{DBPath: filepath.Join(t.TempDir(), "test.db")})
	req := httptest.NewRequest(http.MethodGet, "/sse", nil)
	req.Host = ""
	body, _ := sseBody(t, server, req)
	if !strings.Contains(body, "data: /messages?sessionId=") {
		t.Fatalf("unexpected SSE body: %q", body)
	}
}

func TestMCPMessagesWithSessionID(t *testing.T) {
	server := NewServer(Config{DBPath: filepath.Join(t.TempDir(), "test.db")})
	initResp := rpcPath(t, server, "/messages?sessionId=test-session", "", RPCRequest{JSONRPC: "2.0", ID: 1, Method: "initialize", Params: mustJSON(map[string]any{"protocolVersion": "2025-03-26"})})
	if initResp.Error != nil {
		t.Fatalf("initialize error: %+v", initResp.Error)
	}
	if got := resultMap(t, initResp)["protocolVersion"]; got != "2025-03-26" {
		t.Fatalf("unexpected protocolVersion %v", got)
	}
	listResp := rpcPath(t, server, "/messages?sessionId=test-session", "", RPCRequest{JSONRPC: "2.0", ID: 2, Method: "tools/list"})
	if listResp.Error != nil {
		t.Fatalf("tools/list error: %+v", listResp.Error)
	}
	tools, ok := resultMap(t, listResp)["tools"].([]any)
	if !ok {
		t.Fatalf("missing tools: %#v", listResp.Result)
	}
	if len(tools) != 4 {
		t.Fatalf("expected 4 tools, got %d", len(tools))
	}
}

func TestMCPInitializedNotificationIgnored(t *testing.T) {
	server := NewServer(Config{DBPath: filepath.Join(t.TempDir(), "test.db")})
	resp := rpc(t, server, "", RPCRequest{JSONRPC: "2.0", ID: 1, Method: "notifications/initialized"})
	if resp.Error != nil {
		t.Fatalf("initialized notification error: %+v", resp.Error)
	}
}

func sseBody(t *testing.T, s *Server, req *http.Request) (string, http.Header) {
	t.Helper()
	ctx, cancel := context.WithCancel(req.Context())
	req = req.WithContext(ctx)
	w := httptest.NewRecorder()
	done := make(chan struct{})
	go func() {
		s.authMiddleware(s.mux).ServeHTTP(w, req)
		close(done)
	}()
	deadline := time.Now().Add(time.Second)
	for !strings.Contains(w.Body.String(), "event: endpoint") && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	cancel()
	<-done
	if w.Code != http.StatusOK {
		t.Fatalf("unexpected HTTP status %d: %s", w.Code, w.Body.String())
	}
	if got := w.Header().Get("Content-Type"); got != "text/event-stream" {
		t.Fatalf("unexpected content type %q", got)
	}
	return w.Body.String(), w.Header()
}

func rpc(t *testing.T, s *Server, token string, req RPCRequest) RPCResponse {
	t.Helper()
	return rpcPath(t, s, "/mcp", token, req)
}

func rpcPath(t *testing.T, s *Server, path string, token string, req RPCRequest) RPCResponse {
	t.Helper()
	body := bytes.NewReader(mustJSON(req))
	hreq := httptest.NewRequest(http.MethodPost, path, body)
	if token != "" {
		hreq.Header.Set("Authorization", "Bearer "+token)
	}
	w := httptest.NewRecorder()
	s.authMiddleware(s.mux).ServeHTTP(w, hreq)
	if w.Code != http.StatusOK {
		t.Fatalf("unexpected HTTP status %d: %s", w.Code, w.Body.String())
	}
	var resp RPCResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode rpc response: %v body=%s", err, w.Body.String())
	}
	return resp
}

func resultMap(t *testing.T, resp RPCResponse) map[string]any {
	t.Helper()
	content, err := json.Marshal(resp.Result)
	if err != nil {
		t.Fatalf("marshal result: %v", err)
	}
	var result map[string]any
	if err := json.Unmarshal(content, &result); err != nil {
		t.Fatalf("decode result: %v body=%s", err, string(content))
	}
	return result
}

func mustJSON(v any) json.RawMessage {
	content, err := json.Marshal(v)
	if err != nil {
		panic(err)
	}
	return content
}
