package httpapi

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestHealth(t *testing.T) {
	w := httptest.NewRecorder()
	NewHandler().ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/health", nil))
	if w.Code != 200 || w.Body.String() != `{"status":"ok"}` {
		t.Fatalf("unexpected health response: %d %s", w.Code, w.Body.String())
	}
}
