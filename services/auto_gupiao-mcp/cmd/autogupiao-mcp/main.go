package main

import (
	"flag"
	"fmt"
	"os"
	"strings"

	"github.com/frankichen/auto_gupiao/internal/appconfig"
	"github.com/frankichen/auto_gupiao/internal/mcpserver"
)

func main() {
	configPath := flag.String("config", "configs/server.json", "server JSON config path")
	addr := flag.String("addr", "", "MCP listen address")
	dbPathFlag := flag.String("db", "", "SQLite database path")
	latestFileFlag := flag.String("latest", "", "latest.json path")
	tokenFlag := flag.String("token", "", "Bearer token, overrides AUTO_GUPIAO_MCP_TOKEN")
	noAuthFlag := flag.Bool("no-auth", false, "disable auth for temporary local MCP diagnostics")
	flag.Parse()

	cfg, err := appconfig.Load(*configPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "mcp: %v\n", err)
		os.Exit(1)
	}
	address := *addr
	if address == "" {
		address = ":8090"
	}
	dbPath := *dbPathFlag
	if dbPath == "" {
		dbPath = cfg.Database.Path
	}
	if dbPath == "" {
		dbPath = "data/autogupiao.db"
	}
	latestFile := *latestFileFlag
	if latestFile == "" {
		latestFile = cfg.Report.LatestFile
	}
	if latestFile == "" {
		latestFile = "reports/latest.json"
	}
	token := *tokenFlag
	if token == "" {
		token = os.Getenv("AUTO_GUPIAO_MCP_TOKEN")
	}
	noAuth := *noAuthFlag
	if !noAuth {
		noAuth = strings.EqualFold(strings.TrimSpace(os.Getenv("AUTO_GUPIAO_MCP_NO_AUTH")), "true")
	}
	server := mcpserver.NewServer(mcpserver.Config{Address: address, DBPath: dbPath, LatestFile: latestFile, Token: token, NoAuth: noAuth})
	fmt.Fprintf(os.Stderr, "mcp listening on %s, db=%s, latest=%s, no_auth=%t\n", address, dbPath, latestFile, noAuth)
	if err := server.ListenAndServe(address); err != nil {
		fmt.Fprintf(os.Stderr, "mcp: %v\n", err)
		os.Exit(1)
	}
}
