package quality

import "fmt"

type Level string

const (
	LevelError Level = "error"
	LevelWarn  Level = "warning"
)

type Issue struct {
	Level   Level  `json:"level"`
	Code    string `json:"code,omitempty"`
	Date    string `json:"date,omitempty"`
	Field   string `json:"field"`
	Message string `json:"message"`
}

type Report struct {
	Errors   int     `json:"errors"`
	Warnings int     `json:"warnings"`
	Issues   []Issue `json:"issues"`
}

func (r Report) HasErrors() bool {
	return r.Errors > 0
}

func (r Report) String() string {
	return fmt.Sprintf("quality errors=%d warnings=%d", r.Errors, r.Warnings)
}

func summarize(issues []Issue) Report {
	report := Report{Issues: issues}
	for _, issue := range issues {
		if issue.Level == LevelError {
			report.Errors++
		} else if issue.Level == LevelWarn {
			report.Warnings++
		}
	}
	return report
}
