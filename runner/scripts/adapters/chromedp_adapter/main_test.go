package main

import (
	"context"
	"testing"

	"github.com/chromedp/cdproto/cdp"
	"github.com/chromedp/cdproto/target"
)

type closeTargetExecutor struct {
	responses []bool
	calls     int
}

func (executor *closeTargetExecutor) Execute(
	_ context.Context,
	method string,
	_ any,
	response any,
) error {
	if method != target.CommandCloseTarget {
		panic("unexpected CDP method: " + method)
	}
	result, ok := response.(*closeTargetResult)
	if !ok {
		panic("unexpected close-target response type")
	}
	result.Success = executor.responses[executor.calls]
	executor.calls++
	return nil
}

func TestCleanupTargetsRequiresExplicitSuccess(t *testing.T) {
	tests := []struct {
		name              string
		responses         []bool
		wantConfirmed     bool
		wantCreationState string
	}{
		{
			name:              "success false remains unconfirmed",
			responses:         []bool{false, false},
			wantConfirmed:     false,
			wantCreationState: "cleanup_unconfirmed",
		},
		{
			name:              "retry requires success true",
			responses:         []bool{false, true},
			wantConfirmed:     true,
			wantCreationState: "closed",
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			executor := &closeTargetExecutor{responses: test.responses}
			creation := &targetCreation{
				Attempt:  1,
				Source:   "test",
				State:    "created",
				TargetID: target.ID("target-1"),
				Ctx:      context.Background(),
			}
			adapter := &adapter{
				rootCtx:         cdp.WithExecutor(context.Background(), executor),
				targetCreations: []*targetCreation{creation},
			}

			cleanup := adapter.cleanupTargets()

			if cleanup["confirmed"] != test.wantConfirmed {
				t.Fatalf("confirmed = %v, want %v", cleanup["confirmed"], test.wantConfirmed)
			}
			if creation.State != test.wantCreationState {
				t.Fatalf("creation state = %q, want %q", creation.State, test.wantCreationState)
			}
			if executor.calls != len(test.responses) {
				t.Fatalf("close calls = %d, want %d", executor.calls, len(test.responses))
			}
			attempts, ok := cleanup["attempts"].([]map[string]any)
			if !ok || len(attempts) != len(test.responses) {
				t.Fatalf("cleanup attempts = %#v", cleanup["attempts"])
			}
			for index, expected := range test.responses {
				if attempts[index]["success"] != expected {
					t.Fatalf("attempt %d success = %v, want %v", index+1, attempts[index]["success"], expected)
				}
			}
		})
	}
}
