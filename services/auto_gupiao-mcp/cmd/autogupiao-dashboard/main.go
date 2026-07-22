package main

import (
	"flag"
	"fmt"
	"os"

	"github.com/frankichen/auto_gupiao/internal/appconfig"
	"github.com/frankichen/auto_gupiao/internal/dashboard"
)

func main() {
	configPath := flag.String("config", "configs/server.json", "server JSON config path")
	addr := flag.String("addr", "", "dashboard listen address, overrides dashboard.address")
	flag.Parse()

	cfg, err := appconfig.Load(*configPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "dashboard: %v\n", err)
		os.Exit(1)
	}
	address := cfg.Dashboard.Address
	if *addr != "" {
		address = *addr
	}
	if address == "" {
		address = ":8080"
	}
	dbPath := cfg.Database.Path
	if dbPath == "" {
		dbPath = "data/autogupiao.db"
	}
	server := dashboard.NewServer(dashboard.Config{
		Address:  address,
		DBPath:   dbPath,
		Username: cfg.Dashboard.Username,
		Password: cfg.Dashboard.Password,
	})
	fmt.Fprintf(os.Stderr, "dashboard listening on %s, db=%s\n", address, dbPath)
	if err := server.ListenAndServe(address); err != nil {
		fmt.Fprintf(os.Stderr, "dashboard: %v\n", err)
		os.Exit(1)
	}
}
