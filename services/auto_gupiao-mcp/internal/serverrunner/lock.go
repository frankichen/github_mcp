package serverrunner

import (
	"fmt"
	"os"
	"path/filepath"
)

type runLock struct {
	path string
	file *os.File
}

func acquireRunLock(path string) (*runLock, error) {
	if path == "" {
		return &runLock{}, nil
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return nil, fmt.Errorf("create lock dir: %w", err)
	}
	file, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o644)
	if os.IsExist(err) {
		return nil, fmt.Errorf("another auto_gupiao run is already active: lock file %s exists", path)
	}
	if err != nil {
		return nil, fmt.Errorf("create lock file: %w", err)
	}
	_, _ = fmt.Fprintf(file, "%d\n", os.Getpid())
	return &runLock{path: path, file: file}, nil
}

func (l *runLock) Release() error {
	if l == nil || l.path == "" {
		return nil
	}
	if l.file != nil {
		_ = l.file.Close()
	}
	if err := os.Remove(l.path); err != nil && !os.IsNotExist(err) {
		return fmt.Errorf("remove lock file: %w", err)
	}
	return nil
}
