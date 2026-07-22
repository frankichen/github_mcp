package storage

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"github.com/frankichen/auto_gupiao/internal/sim"
)

type AccountStore struct {
	Path string
}

type accountFile struct {
	Version   int        `json:"version"`
	UpdatedAt time.Time  `json:"updated_at"`
	Report    sim.Report `json:"report"`
}

func NewAccountStore(path string) AccountStore {
	return AccountStore{Path: path}
}

func (s AccountStore) Save(report sim.Report) error {
	if s.Path == "" {
		return errors.New("empty account store path")
	}
	if err := os.MkdirAll(filepath.Dir(s.Path), 0o755); err != nil {
		return fmt.Errorf("create account store dir: %w", err)
	}
	tmp := s.Path + ".tmp"
	file, err := os.Create(tmp)
	if err != nil {
		return fmt.Errorf("create account store: %w", err)
	}
	payload := accountFile{Version: 1, UpdatedAt: time.Now().UTC(), Report: report}
	encoder := json.NewEncoder(file)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(payload); err != nil {
		file.Close()
		_ = os.Remove(tmp)
		return fmt.Errorf("encode account store: %w", err)
	}
	if err := file.Close(); err != nil {
		_ = os.Remove(tmp)
		return fmt.Errorf("close account store: %w", err)
	}
	if err := os.Rename(tmp, s.Path); err != nil {
		_ = os.Remove(tmp)
		return fmt.Errorf("replace account store: %w", err)
	}
	return nil
}

func (s AccountStore) Load() (sim.Report, bool, error) {
	if s.Path == "" {
		return sim.Report{}, false, errors.New("empty account store path")
	}
	file, err := os.Open(s.Path)
	if errors.Is(err, os.ErrNotExist) {
		return sim.Report{}, false, nil
	}
	if err != nil {
		return sim.Report{}, false, fmt.Errorf("open account store: %w", err)
	}
	defer file.Close()
	var payload accountFile
	if err := json.NewDecoder(file).Decode(&payload); err != nil {
		return sim.Report{}, false, fmt.Errorf("decode account store: %w", err)
	}
	return payload.Report, true, nil
}
