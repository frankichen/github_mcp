package dashboard

import (
	"database/sql"
	"html/template"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/frankichen/auto_gupiao/internal/storage"
)

type Config struct {
	Address  string
	DBPath   string
	Username string
	Password string
}

type Server struct {
	store    storage.SQLiteStore
	username string
	password string
	mux      *http.ServeMux
}

func NewServer(cfg Config) *Server {
	mux := http.NewServeMux()
	s := &Server{store: storage.NewSQLiteStore(cfg.DBPath), username: cfg.Username, password: cfg.Password, mux: mux}
	mux.HandleFunc("/", s.handleIndex)
	mux.HandleFunc("/run", s.handleRun)
	mux.HandleFunc("/note", s.handleNote)
	mux.HandleFunc("/healthz", s.handleHealth)
	return s
}

func (s *Server) ListenAndServe(addr string) error {
	if strings.TrimSpace(addr) == "" {
		addr = ":8080"
	}
	server := &http.Server{Addr: addr, Handler: s.authMiddleware(s.mux), ReadHeaderTimeout: 5 * time.Second}
	return server.ListenAndServe()
}

func (s *Server) authMiddleware(next http.Handler) http.Handler {
	if s.username == "" && s.password == "" {
		return next
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		user, pass, ok := r.BasicAuth()
		if !ok || user != s.username || pass != s.password {
			w.Header().Set("WWW-Authenticate", `Basic realm="auto_gupiao"`)
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

func (s *Server) handleIndex(w http.ResponseWriter, r *http.Request) {
	runs, err := s.store.ListRuns(r.Context(), 60)
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	data := struct {
		Runs []storage.RunSummary
	}{Runs: runs}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	if err := indexTemplate.Execute(w, data); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
	}
}

func (s *Server) handleRun(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(r.URL.Query().Get("id"), 10, 64)
	if err != nil || id <= 0 {
		http.Error(w, "invalid run id", http.StatusBadRequest)
		return
	}
	detail, err := s.store.GetRun(r.Context(), id)
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	note, err := s.store.GetRunNote(r.Context(), id)
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	data := struct {
		storage.RunDetail
		Note storage.RunNote
	}{RunDetail: detail, Note: note}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	if err := runTemplate.Execute(w, data); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
	}
}

func (s *Server) handleNote(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if err := r.ParseForm(); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	runID, err := strconv.ParseInt(r.FormValue("run_id"), 10, 64)
	if err != nil || runID <= 0 {
		http.Error(w, "invalid run id", http.StatusBadRequest)
		return
	}
	note := storage.RunNote{RunID: runID, Status: r.FormValue("status"), Memo: r.FormValue("memo")}
	if err := s.store.SaveRunNote(r.Context(), note); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	http.Redirect(w, r, "/run?id="+strconv.FormatInt(runID, 10), http.StatusSeeOther)
}

func formatProfitFactor(value sql.NullFloat64) string {
	if !value.Valid {
		return "-"
	}
	return strconv.FormatFloat(value.Float64, 'f', 4, 64)
}

var funcMap = template.FuncMap{
	"profitFactor": formatProfitFactor,
	"pctClass": func(v float64) string {
		if v >= 0 {
			return "pos"
		}
		return "neg"
	},
}

var indexTemplate = template.Must(template.New("index").Funcs(funcMap).Parse(baseCSS + `
<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>A股观察盘</title></head>
<body>
<header><h1>A股观察盘</h1><p>最近 60 次运行记录</p></header>
<main>
<table>
<thead><tr><th>ID</th><th>生成时间</th><th>交易日</th><th>收益率</th><th>最大回撤</th><th>交易</th><th>胜率</th><th>盈利因子</th><th>风险</th><th>报告</th></tr></thead>
<tbody>
{{range .Runs}}
<tr>
<td><a href="/run?id={{.ID}}">#{{.ID}}</a></td>
<td>{{.GeneratedAt}}</td>
<td>{{.TradeDate}}</td>
<td class="{{pctClass .TotalReturnPct}}">{{printf "%.4f%%" .TotalReturnPct}}</td>
<td class="neg">{{printf "%.4f%%" .MaxDrawdownPct}}</td>
<td>{{.Trades}}</td>
<td>{{printf "%.2f%%" .WinRatePct}}</td>
<td>{{profitFactor .ProfitFactor}}</td>
<td><span class="badge">{{.RiskLevel}}</span></td>
<td>{{if .MarkdownURL}}<a href="{{.MarkdownURL}}" target="_blank">Markdown</a>{{end}}</td>
</tr>
{{else}}
<tr><td colspan="10">暂无数据。请先运行 autogupiao-server 写入数据库。</td></tr>
{{end}}
</tbody>
</table>
</main>
</body></html>`))

var runTemplate = template.Must(template.New("run").Funcs(funcMap).Parse(baseCSS + `
<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>A股观察盘 #{{.Summary.ID}}</title></head>
<body>
<header><h1>A股观察盘 #{{.Summary.ID}}</h1><p><a href="/">返回列表</a></p></header>
<main>
<section class="cards">
<div class="card"><b>总收益率</b><span class="{{pctClass .Summary.TotalReturnPct}}">{{printf "%.4f%%" .Summary.TotalReturnPct}}</span></div>
<div class="card"><b>最大回撤</b><span class="neg">{{printf "%.4f%%" .Summary.MaxDrawdownPct}}</span></div>
<div class="card"><b>最终权益</b><span>{{printf "%.2f" .Summary.FinalEquity}}</span></div>
<div class="card"><b>盈利因子</b><span>{{profitFactor .Summary.ProfitFactor}}</span></div>
<div class="card"><b>交易次数</b><span>{{.Summary.Trades}}</span></div>
<div class="card"><b>胜率</b><span>{{printf "%.2f%%" .Summary.WinRatePct}}</span></div>
</section>
<section><h2>观察备注</h2><form method="post" action="/note"><input type="hidden" name="run_id" value="{{.Summary.ID}}"><label>状态 <select name="status"><option value="pending">待查看</option><option value="checked">已查看</option><option value="skipped">跳过</option><option value="review">需要复盘</option></select></label><label>备注 <input name="memo" value="{{.Note.Memo}}" placeholder="记录你在同花顺/券商软件里已查看或手工处理的情况"></label><button type="submit">保存</button></form><p>当前状态：<b>{{.Note.Status}}</b>{{if .Note.UpdatedAt}}，更新时间：{{.Note.UpdatedAt}}{{end}}</p></section>
<section><h2>观察结论</h2><p><b>风险等级：</b>{{.Summary.RiskLevel}}</p><p>{{.Summary.Conclusion}}</p></section>
<section><h2>报告文件</h2><p>{{if .Summary.MarkdownURL}}<a href="{{.Summary.MarkdownURL}}" target="_blank">Markdown</a>{{end}} {{if .Summary.TradesURL}}<a href="{{.Summary.TradesURL}}" target="_blank">交易明细</a>{{end}} {{if .Summary.EquityURL}}<a href="{{.Summary.EquityURL}}" target="_blank">权益曲线</a>{{end}}</p></section>
<section><h2>按股票归因</h2>{{template "attrTable" .ByCode}}</section>
<section><h2>按卖出原因归因</h2>{{template "attrTable" .ByExitReason}}</section>
<section><h2>最近权益</h2><table><thead><tr><th>日期</th><th>现金</th><th>持仓市值</th><th>权益</th><th>日收益</th><th>持仓数</th></tr></thead><tbody>{{range .Equity}}<tr><td>{{.Date}}</td><td>{{printf "%.2f" .Cash}}</td><td>{{printf "%.2f" .MarketValue}}</td><td>{{printf "%.2f" .Equity}}</td><td class="{{pctClass .DailyReturnPct}}">{{printf "%.4f%%" .DailyReturnPct}}</td><td>{{.OpenPositions}}</td></tr>{{end}}</tbody></table></section>
<section><h2>交易明细</h2><table><thead><tr><th>买入日</th><th>卖出日</th><th>代码</th><th>原因</th><th>股数</th><th>买入价</th><th>卖出价</th><th>净利润</th><th>收益率</th></tr></thead><tbody>{{range .Trades}}<tr><td>{{.BuyDate}}</td><td>{{.SellDate}}</td><td>{{.Code}}</td><td>{{.ExitReason}}</td><td>{{.Shares}}</td><td>{{printf "%.4f" .BuyPrice}}</td><td>{{printf "%.4f" .SellPrice}}</td><td class="{{pctClass .NetProfit}}">{{printf "%.2f" .NetProfit}}</td><td class="{{pctClass .NetReturnPct}}">{{printf "%.4f%%" .NetReturnPct}}</td></tr>{{end}}</tbody></table></section>
</main>
</body></html>
{{define "attrTable"}}<table><thead><tr><th>项目</th><th>笔数</th><th>胜率</th><th>净利润</th><th>平均收益</th><th>最大盈利</th><th>最大亏损</th></tr></thead><tbody>{{range .}}<tr><td>{{.Key}}</td><td>{{.Trades}}</td><td>{{printf "%.2f%%" .WinRatePct}}</td><td class="{{pctClass .NetProfit}}">{{printf "%.2f" .NetProfit}}</td><td class="{{pctClass .AvgReturnPct}}">{{printf "%.4f%%" .AvgReturnPct}}</td><td class="pos">{{printf "%.2f" .MaxProfit}}</td><td class="neg">{{printf "%.2f" .MaxLoss}}</td></tr>{{else}}<tr><td colspan="7">暂无</td></tr>{{end}}</tbody></table>{{end}}`))

const baseCSS = `<style>
:root{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#172033;background:#f6f7fb}body{margin:0}header{padding:24px 32px;background:#111827;color:white}header h1{margin:0 0 6px;font-size:24px}header p{margin:0;color:#cbd5e1}main{padding:24px 32px}a{color:#2563eb;text-decoration:none}table{width:100%;border-collapse:collapse;background:white;border-radius:12px;overflow:hidden;box-shadow:0 1px 8px #0001;margin:12px 0 28px}th,td{padding:10px 12px;border-bottom:1px solid #e5e7eb;text-align:left;font-size:14px}th{background:#f1f5f9;color:#334155}.pos{color:#047857;font-weight:600}.neg{color:#dc2626;font-weight:600}.badge{display:inline-block;background:#eef2ff;color:#3730a3;border-radius:999px;padding:3px 10px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:24px}.card{background:white;border-radius:14px;padding:16px;box-shadow:0 1px 8px #0001}.card b{display:block;color:#64748b;font-size:13px}.card span{font-size:22px;font-weight:700}section h2{margin-top:28px}p{line-height:1.7}form{display:flex;gap:12px;flex-wrap:wrap;align-items:center;background:white;border-radius:12px;padding:14px;box-shadow:0 1px 8px #0001}input,select{padding:8px 10px;border:1px solid #cbd5e1;border-radius:8px;min-width:180px}button{padding:8px 14px;border:0;border-radius:8px;background:#2563eb;color:white;font-weight:600}
</style>`
