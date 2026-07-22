package mcpserver

import (
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/frankichen/auto_gupiao/internal/storage"
)

type Config struct {
	Address       string
	DBPath        string
	LatestFile    string
	Token         string
	NoAuth        bool
	PublicBaseURL string
}

type Server struct {
	store         storage.SQLiteStore
	latestFile    string
	token         string
	noAuth        bool
	publicBaseURL string
	mux           *http.ServeMux
}

type RPCRequest struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      any             `json:"id,omitempty"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params,omitempty"`
}

type RPCResponse struct {
	JSONRPC string    `json:"jsonrpc"`
	ID      any       `json:"id,omitempty"`
	Result  any       `json:"result,omitempty"`
	Error   *RPCError `json:"error,omitempty"`
}

type RPCError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

type Tool struct {
	Name        string         `json:"name"`
	Description string         `json:"description"`
	InputSchema map[string]any `json:"inputSchema"`
}

type ToolCallParams struct {
	Name      string         `json:"name"`
	Arguments map[string]any `json:"arguments"`
}

type LatestReport struct {
	RunID              int64    `json:"run_id"`
	TradeDate          string   `json:"trade_date"`
	GeneratedAt        string   `json:"generated_at"`
	DataStatus         string   `json:"data_status"`
	DataStatusReason   string   `json:"data_status_reason,omitempty"`
	RiskLevel          string   `json:"risk_level"`
	Conclusion         string   `json:"conclusion"`
	InitialCash        float64  `json:"initial_cash"`
	FinalEquity        float64  `json:"final_equity"`
	TotalReturnPct     float64  `json:"total_return_pct"`
	MaxDrawdownPct     float64  `json:"max_drawdown_pct"`
	Trades             int      `json:"trades"`
	WinRatePct         float64  `json:"win_rate_pct"`
	ProfitFactor       *float64 `json:"profit_factor"`
	MaxConsecutiveLoss int      `json:"max_consecutive_loss"`
	BarsStartDate      string   `json:"bars_start_date"`
	BarsEndDate        string   `json:"bars_end_date"`
	Codes              []string `json:"codes"`
	Rows               int      `json:"rows"`
	MarkdownURL        string   `json:"markdown_url"`
	TradesURL          string   `json:"trades_url"`
	EquityURL          string   `json:"equity_url"`
}

func NewServer(cfg Config) *Server {
	mux := http.NewServeMux()
	s := &Server{
		store:         storage.NewSQLiteStore(cfg.DBPath),
		latestFile:    cfg.LatestFile,
		token:         strings.TrimSpace(cfg.Token),
		noAuth:        cfg.NoAuth,
		publicBaseURL: strings.TrimSpace(cfg.PublicBaseURL),
		mux:           mux,
	}
	mux.HandleFunc("/healthz", s.handleHealth)
	mux.HandleFunc("/mcp", s.handleMCP)
	mux.HandleFunc("/messages", s.handleRPC)
	mux.HandleFunc("/sse", s.handleSSE)
	return s
}

func (s *Server) ListenAndServe(addr string) error {
	if strings.TrimSpace(addr) == "" {
		addr = ":8090"
	}
	server := &http.Server{Addr: addr, Handler: s.authMiddleware(s.mux), ReadHeaderTimeout: 5 * time.Second}
	return server.ListenAndServe()
}

func (s *Server) authMiddleware(next http.Handler) http.Handler {
	if s.noAuth {
		return next
	}
	if s.token == "" {
		return next
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/healthz" {
			next.ServeHTTP(w, r)
			return
		}
		got := strings.TrimSpace(r.Header.Get("Authorization"))
		want := "Bearer " + s.token
		if got != want {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		next.ServeHTTP(w, r)
	})
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	_, _ = w.Write([]byte("ok"))
}

func (s *Server) handleMCP(w http.ResponseWriter, r *http.Request) {
	start := time.Now()
	status := http.StatusOK
	switch r.Method {
	case http.MethodOptions:
		w.Header().Set("Allow", "GET, POST, OPTIONS")
		w.Header().Set("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "Authorization, Content-Type, Accept, MCP-Protocol-Version")
		status = http.StatusNoContent
		w.WriteHeader(status)
		logMCPRequest(r, "", "", status, time.Since(start))
	case http.MethodGet:
		s.handleSSE(w, r)
		logMCPRequest(r, "", "", status, time.Since(start))
	case http.MethodPost:
		s.handleRPC(w, r)
	default:
		status = http.StatusMethodNotAllowed
		http.Error(w, "method not allowed", status)
		logMCPRequest(r, "", "", status, time.Since(start))
	}
}

func (s *Server) handleSSE(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache, no-transform")
	w.Header().Set("Connection", "keep-alive")
	w.Header().Set("X-Accel-Buffering", "no")
	endpoint := s.sseEndpoint(r)
	_, _ = fmt.Fprintf(w, "event: endpoint\ndata: %s\n\n", endpoint)
	if flusher, ok := w.(http.Flusher); ok {
		flusher.Flush()
	}
}

func (s *Server) sseEndpoint(r *http.Request) string {
	path := "/messages?sessionId=" + newSessionID()
	if base := strings.TrimRight(strings.TrimSpace(s.publicBaseURL), "/"); base != "" {
		return base + path
	}
	proto := firstHeaderValue(r.Header.Get("X-Forwarded-Proto"))
	host := firstHeaderValue(r.Header.Get("X-Forwarded-Host"))
	if host == "" {
		host = r.Host
	}
	if proto == "" && r.URL != nil {
		proto = r.URL.Scheme
	}
	if proto != "" && host != "" {
		return strings.TrimRight(proto+"://"+host, "/") + path
	}
	return path
}

func firstHeaderValue(v string) string {
	if i := strings.IndexByte(v, ','); i >= 0 {
		v = v[:i]
	}
	return strings.TrimSpace(v)
}

func (s *Server) handleRPC(w http.ResponseWriter, r *http.Request) {
	start := time.Now()
	status := http.StatusOK
	rpcMethod := ""
	toolName := ""
	defer func() {
		logMCPRequest(r, rpcMethod, toolName, status, time.Since(start))
	}()
	if r.Method != http.MethodPost {
		status = http.StatusMethodNotAllowed
		http.Error(w, "method not allowed", status)
		return
	}
	w.Header().Set("MCP-Protocol-Version", negotiateProtocolVersion(nil))
	var req RPCRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		status = http.StatusBadRequest
		writeRPC(w, RPCResponse{JSONRPC: "2.0", ID: nil, Error: &RPCError{Code: -32700, Message: "parse error"}})
		return
	}
	rpcMethod = req.Method
	if req.Method == "tools/call" {
		var params ToolCallParams
		if json.Unmarshal(req.Params, &params) == nil {
			toolName = params.Name
		}
	}
	resp := RPCResponse{JSONRPC: "2.0", ID: req.ID}
	result, err := s.handleMethod(r.Context(), req)
	if err != nil {
		resp.Error = &RPCError{Code: -32000, Message: err.Error()}
	} else {
		resp.Result = result
	}
	writeRPC(w, resp)
}

func logMCPRequest(r *http.Request, rpcMethod string, toolName string, status int, duration time.Duration) {
	path := ""
	if r.URL != nil {
		path = r.URL.Path
	}
	if rpcMethod == "" {
		rpcMethod = "-"
	}
	if toolName == "" {
		toolName = "-"
	}
	log.Printf("mcp_request http_method=%s path=%s rpc_method=%s tool_name=%s status=%d duration_ms=%d", r.Method, path, rpcMethod, toolName, status, duration.Milliseconds())
}

func writeRPC(w http.ResponseWriter, resp RPCResponse) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	_ = json.NewEncoder(w).Encode(resp)
}

func (s *Server) handleMethod(ctx context.Context, req RPCRequest) (any, error) {
	switch req.Method {
	case "initialize":
		return initializeResult(req.Params), nil
	case "notifications/initialized":
		return map[string]any{}, nil
	case "tools/list":
		return map[string]any{"tools": tools()}, nil
	case "tools/call":
		var params ToolCallParams
		if err := json.Unmarshal(req.Params, &params); err != nil {
			return nil, fmt.Errorf("invalid tools/call params")
		}
		return s.callTool(ctx, params)
	default:
		return nil, fmt.Errorf("unsupported method %s", req.Method)
	}
}

func initializeResult(params json.RawMessage) map[string]any {
	return map[string]any{
		"protocolVersion": negotiateProtocolVersion(params),
		"serverInfo":      map[string]any{"name": "auto_gupiao", "version": "0.1.0"},
		"capabilities":    map[string]any{"tools": map[string]any{}},
	}
}

func negotiateProtocolVersion(params json.RawMessage) string {
	const fallback = "2024-11-05"
	supported := map[string]bool{
		"2024-11-05": true,
		"2025-03-26": true,
		"2025-06-18": true,
	}
	var initParams struct {
		ProtocolVersion string `json:"protocolVersion"`
	}
	if len(params) > 0 && json.Unmarshal(params, &initParams) == nil && supported[initParams.ProtocolVersion] {
		return initParams.ProtocolVersion
	}
	return fallback
}

func newSessionID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return strconv.FormatInt(time.Now().UnixNano(), 36)
	}
	return hex.EncodeToString(b[:])
}

func tools() []Tool {
	return []Tool{
		{Name: "get_latest_report", Description: "读取 reports/latest.json，返回最新观察盘摘要和报告链接。", InputSchema: objectSchema(nil)},
		{Name: "list_recent_runs", Description: "读取 SQLite，返回最近 N 次观察盘运行记录。", InputSchema: objectSchema(map[string]any{"limit": map[string]any{"type": "integer", "description": "返回数量，默认 10，最大 50"}})},
		{Name: "get_run_detail", Description: "读取 SQLite，返回指定 run_id 的详情、权益、交易和归因。", InputSchema: objectSchema(map[string]any{"run_id": map[string]any{"type": "integer", "description": "运行 ID"}})},
		{Name: "get_run_note", Description: "读取 SQLite，返回指定 run_id 的人工备注。", InputSchema: objectSchema(map[string]any{"run_id": map[string]any{"type": "integer", "description": "运行 ID"}})},
	}
}

func objectSchema(properties map[string]any) map[string]any {
	if properties == nil {
		properties = map[string]any{}
	}
	return map[string]any{"type": "object", "properties": properties}
}

func (s *Server) callTool(ctx context.Context, params ToolCallParams) (any, error) {
	switch params.Name {
	case "get_latest_report":
		return s.getLatestReport()
	case "list_recent_runs":
		limit := intArg(params.Arguments, "limit", 10)
		if limit <= 0 || limit > 50 {
			limit = 10
		}
		runs, err := s.store.ListRuns(ctx, limit)
		if err != nil {
			return nil, err
		}
		return map[string]any{"runs": summaries(runs)}, nil
	case "get_run_detail":
		runID := int64Arg(params.Arguments, "run_id", 0)
		if runID <= 0 {
			return nil, fmt.Errorf("missing run_id")
		}
		detail, err := s.store.GetRun(ctx, runID)
		if err != nil {
			return nil, err
		}
		return detailResponse(detail), nil
	case "get_run_note":
		runID := int64Arg(params.Arguments, "run_id", 0)
		if runID <= 0 {
			return nil, fmt.Errorf("missing run_id")
		}
		note, err := s.store.GetRunNote(ctx, runID)
		if err != nil {
			return nil, err
		}
		return map[string]any{"note": note}, nil
	default:
		return nil, fmt.Errorf("unknown tool %s", params.Name)
	}
}

func (s *Server) getLatestReport() (any, error) {
	if strings.TrimSpace(s.latestFile) == "" {
		return nil, fmt.Errorf("latest file is not configured")
	}
	content, err := os.ReadFile(s.latestFile)
	if err != nil {
		return nil, fmt.Errorf("read latest report: %w", err)
	}
	var latest LatestReport
	if err := json.Unmarshal(content, &latest); err != nil {
		return nil, fmt.Errorf("parse latest report: %w", err)
	}
	return map[string]any{"latest": latest}, nil
}

func summaries(runs []storage.RunSummary) []map[string]any {
	out := make([]map[string]any, 0, len(runs))
	for _, run := range runs {
		out = append(out, summary(run))
	}
	return out
}

func detailResponse(detail storage.RunDetail) map[string]any {
	return map[string]any{"summary": summary(detail.Summary), "equity": detail.Equity, "trades": detail.Trades, "by_code": detail.ByCode, "by_exit_reason": detail.ByExitReason}
}

func summary(run storage.RunSummary) map[string]any {
	return map[string]any{
		"id": run.ID, "generated_at": run.GeneratedAt, "trade_date": run.TradeDate, "data_start": run.DataStart, "data_end": run.DataEnd,
		"stock_pool": run.StockPool, "bars_rows": run.BarsRows, "initial_cash": run.InitialCash, "final_equity": run.FinalEquity,
		"total_return_pct": run.TotalReturnPct, "max_drawdown_pct": run.MaxDrawdownPct, "trades": run.Trades, "wins": run.Wins,
		"losses": run.Losses, "win_rate_pct": run.WinRatePct, "profit_factor": nullFloat(run.ProfitFactor), "max_consecutive_loss": run.MaxConsecutiveLoss,
		"risk_level": run.RiskLevel, "conclusion": run.Conclusion, "markdown_url": run.MarkdownURL, "trades_url": run.TradesURL, "equity_url": run.EquityURL,
	}
}

func nullFloat(v sql.NullFloat64) any {
	if !v.Valid {
		return nil
	}
	return v.Float64
}

func intArg(args map[string]any, key string, def int) int {
	return int(int64Arg(args, key, int64(def)))
}

func int64Arg(args map[string]any, key string, def int64) int64 {
	if args == nil {
		return def
	}
	raw, ok := args[key]
	if !ok {
		return def
	}
	switch v := raw.(type) {
	case float64:
		return int64(v)
	case int64:
		return v
	case int:
		return int64(v)
	case string:
		parsed, err := strconv.ParseInt(v, 10, 64)
		if err == nil {
			return parsed
		}
	}
	return def
}
