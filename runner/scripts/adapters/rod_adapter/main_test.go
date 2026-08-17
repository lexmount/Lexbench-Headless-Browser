package main

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"github.com/go-rod/rod"
	"github.com/go-rod/rod/lib/cdp"
	"github.com/go-rod/rod/lib/proto"
)

type closeTargetReply struct {
	success bool
	err     error
}

type closeTargetCDPClient struct {
	success bool
	calls   int
	events  chan *cdp.Event
}

func (client *closeTargetCDPClient) Event() <-chan *cdp.Event {
	return client.events
}

func (client *closeTargetCDPClient) Call(
	_ context.Context,
	sessionID string,
	method string,
	params interface{},
) ([]byte, error) {
	if sessionID != "" {
		panic("Target.closeTarget was not sent on the root connection")
	}
	if method != "Target.closeTarget" {
		panic("unexpected CDP method: " + method)
	}
	request, ok := params.(proto.TargetCloseTarget)
	if !ok || request.TargetID != proto.TargetTargetID("target-1") {
		panic("unexpected Target.closeTarget request")
	}
	client.calls++
	return json.Marshal(map[string]bool{"success": client.success})
}

func TestCloseTargetForIsolationReadsExplicitSuccess(t *testing.T) {
	for _, expected := range []bool{false, true} {
		client := &closeTargetCDPClient{
			success: expected,
			events:  make(chan *cdp.Event),
		}
		adapter := &adapter{browser: rod.New().Client(client)}

		actual, err := adapter.closeTargetForIsolation(
			proto.TargetTargetID("target-1"),
			time.Second,
		)

		if err != nil {
			t.Fatalf("close error = %v", err)
		}
		if actual != expected {
			t.Fatalf("success = %v, want %v", actual, expected)
		}
		if client.calls != 1 {
			t.Fatalf("close calls = %d, want 1", client.calls)
		}
	}
}

func TestCleanupPagesRequiresExplicitCloseTargetSuccess(t *testing.T) {
	tests := []struct {
		name          string
		replies       []closeTargetReply
		wantConfirmed bool
		wantState     string
	}{
		{
			name: "success false remains unconfirmed",
			replies: []closeTargetReply{
				{success: false},
				{success: false},
			},
			wantConfirmed: false,
			wantState:     "cleanup_unconfirmed",
		},
		{
			name: "retry requires success true",
			replies: []closeTargetReply{
				{success: false},
				{success: true},
			},
			wantConfirmed: true,
			wantState:     "closed",
		},
		{
			name: "deadline error is retried",
			replies: []closeTargetReply{
				{err: context.DeadlineExceeded},
				{success: true},
			},
			wantConfirmed: true,
			wantState:     "closed",
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			page := &rod.Page{TargetID: proto.TargetTargetID("target-1")}
			creation := &pageCreation{
				Attempt:  1,
				State:    "created",
				TargetID: "target-1",
				Page:     page,
			}
			calls := 0
			adapter := &adapter{
				pages:         []*rod.Page{page},
				pageCreations: []*pageCreation{creation},
				closeTargetHook: func(
					targetID proto.TargetTargetID,
					timeout time.Duration,
				) (bool, error) {
					if targetID != proto.TargetTargetID("target-1") {
						t.Fatalf("target id = %q", targetID)
					}
					if timeout != 3*time.Second {
						t.Fatalf("timeout = %s", timeout)
					}
					reply := test.replies[calls]
					calls++
					return reply.success, reply.err
				},
			}

			cleanup := adapter.cleanupPages()

			if cleanup["confirmed"] != test.wantConfirmed {
				t.Fatalf("confirmed = %v, want %v", cleanup["confirmed"], test.wantConfirmed)
			}
			if cleanup["backend"] != "Target.closeTarget via rod root connection" {
				t.Fatalf("backend = %v", cleanup["backend"])
			}
			if creation.State != test.wantState {
				t.Fatalf("creation state = %q, want %q", creation.State, test.wantState)
			}
			if calls != len(test.replies) {
				t.Fatalf("close calls = %d, want %d", calls, len(test.replies))
			}
			attempts, ok := cleanup["attempts"].([]map[string]any)
			if !ok || len(attempts) != len(test.replies) {
				t.Fatalf("cleanup attempts = %#v", cleanup["attempts"])
			}
			for index, reply := range test.replies {
				wantAttemptConfirmed := reply.err == nil && reply.success
				if attempts[index]["confirmed"] != wantAttemptConfirmed {
					t.Fatalf(
						"attempt %d confirmed = %v, want %v",
						index+1,
						attempts[index]["confirmed"],
						wantAttemptConfirmed,
					)
				}
			}
		})
	}
}
