package storage

import (
	"context"
	"database/sql"
	"fmt"
	"strings"
	"time"
)

const (
	NoteStatusPending = "pending"
	NoteStatusChecked = "checked"
	NoteStatusSkipped = "skipped"
	NoteStatusReview  = "review"
)

type RunNote struct {
	RunID     int64  `json:"run_id"`
	Status    string `json:"status"`
	Memo      string `json:"memo"`
	UpdatedAt string `json:"updated_at"`
}

func (s SQLiteStore) SaveRunNote(ctx context.Context, note RunNote) error {
	db, err := s.Open(ctx)
	if err != nil {
		return err
	}
	defer db.Close()
	if err := ensureRunNotesSchema(ctx, db); err != nil {
		return err
	}
	return saveRunNote(ctx, db, note)
}

func (s SQLiteStore) GetRunNote(ctx context.Context, runID int64) (RunNote, error) {
	db, err := s.Open(ctx)
	if err != nil {
		return RunNote{}, err
	}
	defer db.Close()
	if err := ensureRunNotesSchema(ctx, db); err != nil {
		return RunNote{}, err
	}
	return getRunNote(ctx, db, runID)
}

func ensureRunNotesSchema(ctx context.Context, db *sql.DB) error {
	_, err := db.ExecContext(ctx, `CREATE TABLE IF NOT EXISTS run_notes (
		run_id INTEGER PRIMARY KEY,
		status TEXT NOT NULL,
		memo TEXT NOT NULL,
		updated_at TEXT NOT NULL,
		FOREIGN KEY(run_id) REFERENCES daily_runs(id) ON DELETE CASCADE
	);`)
	if err != nil {
		return fmt.Errorf("ensure run notes schema: %w", err)
	}
	return nil
}

func saveRunNote(ctx context.Context, db *sql.DB, note RunNote) error {
	if note.RunID <= 0 {
		return fmt.Errorf("missing run_id")
	}
	note.Status = normalizeNoteStatus(note.Status)
	if note.UpdatedAt == "" {
		note.UpdatedAt = time.Now().Format(time.RFC3339)
	}
	_, err := db.ExecContext(ctx, `INSERT INTO run_notes (run_id, status, memo, updated_at)
VALUES (?, ?, ?, ?)
ON CONFLICT(run_id) DO UPDATE SET status = excluded.status, memo = excluded.memo, updated_at = excluded.updated_at`, note.RunID, note.Status, strings.TrimSpace(note.Memo), note.UpdatedAt)
	if err != nil {
		return fmt.Errorf("save run note: %w", err)
	}
	return nil
}

func getRunNote(ctx context.Context, db *sql.DB, runID int64) (RunNote, error) {
	if runID <= 0 {
		return RunNote{Status: NoteStatusPending}, nil
	}
	var note RunNote
	err := db.QueryRowContext(ctx, `SELECT run_id, status, memo, updated_at FROM run_notes WHERE run_id = ?`, runID).Scan(&note.RunID, &note.Status, &note.Memo, &note.UpdatedAt)
	if err == sql.ErrNoRows {
		return RunNote{RunID: runID, Status: NoteStatusPending}, nil
	}
	if err != nil {
		return RunNote{}, fmt.Errorf("get run note: %w", err)
	}
	return note, nil
}

func normalizeNoteStatus(status string) string {
	switch strings.TrimSpace(status) {
	case NoteStatusChecked:
		return NoteStatusChecked
	case NoteStatusSkipped:
		return NoteStatusSkipped
	case NoteStatusReview:
		return NoteStatusReview
	default:
		return NoteStatusPending
	}
}
