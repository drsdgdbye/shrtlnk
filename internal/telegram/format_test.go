package telegram

import (
	"testing"

	"yt_dw/internal/shortener"
)

func TestFallbackText(t *testing.T) {
	const want = "Expected link of format: https://your/own?long&link"
	if fallbackText != want {
		t.Errorf("fallbackText = %q, ожидалось %q", fallbackText, want)
	}
}

func TestAnchor(t *testing.T) {
	got := anchor("https://example.com/a?b=1&c=2")
	want := "<a href=\"https://example.com/a?b=1&amp;c=2\">https://example.com/a?b=1&amp;c=2</a>"
	if got != want {
		t.Errorf("anchor = %q, ожидалось %q", got, want)
	}
}

func TestAbsoluteShortURL(t *testing.T) {
	tests := []struct {
		base string
		code string
		want string
	}{
		{base: "https://shrt.example", code: "abc1234", want: "https://shrt.example/abc1234"},
		{base: "https://shrt.example/", code: "abc1234", want: "https://shrt.example/abc1234"},
		{base: "https://shrt.example///", code: "abc1234", want: "https://shrt.example/abc1234"},
	}
	for _, tt := range tests {
		if got := absoluteShortURL(tt.base, tt.code); got != tt.want {
			t.Errorf("absoluteShortURL(%q, %q) = %q, ожидалось %q", tt.base, tt.code, got, tt.want)
		}
	}
}

func TestFormatLinks(t *testing.T) {
	const base = "https://shrt.example"

	if got := formatLinks(base, nil); got != emptyLinksText {
		t.Errorf("formatLinks(пусто) = %q, ожидалось %q", got, emptyLinksText)
	}

	links := []shortener.Link{
		{Code: "aaa1111"},
		{Code: "bbb2222"},
	}
	got := formatLinks(base, links)
	want := linksHeader +
		"\n1. <a href=\"https://shrt.example/aaa1111\">https://shrt.example/aaa1111</a>" +
		"\n2. <a href=\"https://shrt.example/bbb2222\">https://shrt.example/bbb2222</a>"
	if got != want {
		t.Errorf("formatLinks = %q, ожидалось %q", got, want)
	}
}

func TestFormatShort(t *testing.T) {
	got := formatShort("https://shrt.example/abc1234")
	want := "Your short link: <a href=\"https://shrt.example/abc1234\">https://shrt.example/abc1234</a>"
	if got != want {
		t.Errorf("formatShort = %q, ожидалось %q", got, want)
	}
}
