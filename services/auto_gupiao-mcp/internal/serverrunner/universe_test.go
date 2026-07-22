package serverrunner

import (
	"context"
	"reflect"
	"testing"

	"github.com/frankichen/auto_gupiao/internal/appconfig"
	"github.com/frankichen/auto_gupiao/internal/domain"
)

type fakeSnapshotProvider struct {
	snapshots []domain.StockSnapshot
}

func (p fakeSnapshotProvider) ListSnapshots(context.Context, string) ([]domain.StockSnapshot, error) {
	return p.snapshots, nil
}

func TestResolveUniverseCodesCustom(t *testing.T) {
	codes, err := resolveUniverseCodes(context.Background(), appconfig.Config{Data: appconfig.DataConfig{Universe: UniverseCustom, Codes: []string{"000001", "600000", "000001"}}}, nil)
	if err != nil {
		t.Fatalf("resolve custom failed: %v", err)
	}
	if !reflect.DeepEqual(codes, []string{"000001", "600000"}) {
		t.Fatalf("unexpected codes: %+v", codes)
	}
}

func TestSelectUniverseCodesFiltersAndSorts(t *testing.T) {
	snapshots := []domain.StockSnapshot{
		{Code: "000001", Name: "ok low amount", Close: 10, Amount: 50000000},
		{Code: "600000", Name: "best", Close: 12, Amount: 500000000},
		{Code: "601288", Name: "second", Close: 6, Amount: 300000000},
		{Code: "300750", Name: "too expensive", Close: 200, Amount: 900000000},
		{Code: "000002", Name: "st", Close: 8, Amount: 400000000, ST: true},
		{Code: "600001", Name: "suspended", Close: 7, Amount: 400000000, Suspended: true},
	}
	codes := selectUniverseCodes(snapshots, appconfig.DataConfig{UniverseLimit: 2, MinUniversePrice: 2, MaxUniversePrice: 80, MinUniverseAmount: 100000000})
	want := []string{"600000", "601288"}
	if !reflect.DeepEqual(codes, want) {
		t.Fatalf("unexpected universe codes: got=%+v want=%+v", codes, want)
	}
}

func TestResolveUniverseCodesAllA(t *testing.T) {
	provider := fakeSnapshotProvider{snapshots: []domain.StockSnapshot{
		{Code: "000001", Close: 10, Amount: 200000000},
		{Code: "600000", Close: 12, Amount: 300000000},
	}}
	codes, err := resolveUniverseCodes(context.Background(), appconfig.Config{Data: appconfig.DataConfig{Universe: UniverseAllA, UniverseLimit: 10, MinUniversePrice: 2, MaxUniversePrice: 80, MinUniverseAmount: 100000000}}, provider)
	if err != nil {
		t.Fatalf("resolve all_a failed: %v", err)
	}
	want := []string{"600000", "000001"}
	if !reflect.DeepEqual(codes, want) {
		t.Fatalf("unexpected all_a codes: got=%+v want=%+v", codes, want)
	}
}

func TestResolveUniverseCodesAllARequiresSnapshotProvider(t *testing.T) {
	_, err := resolveUniverseCodes(context.Background(), appconfig.Config{Data: appconfig.DataConfig{Universe: UniverseAllA}}, nil)
	if err == nil {
		t.Fatalf("expected error")
	}
}
