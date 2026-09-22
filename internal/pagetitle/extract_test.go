package pagetitle

import (
	"context"
	"net/http"
	"strings"
	"testing"
	"unicode/utf8"
)

// cp1251Привет — «Привет» в кодировке windows-1251.
var cp1251Привет = []byte{0xCF, 0xF0, 0xE8, 0xE2, 0xE5, 0xF2}

// titleFromBody прогоняет тело через Fetcher с транспортом-заглушкой.
func titleFromBody(t *testing.T, contentType, body string) string {
	t.Helper()
	fetch := newTestFetcher(func(*http.Request) (*http.Response, error) {
		return htmlResponse(http.StatusOK, contentType, body), nil
	})
	got, err := fetch.Title(context.Background(), "https://example.com/")
	if err != nil {
		t.Fatalf("Title: %v", err)
	}
	return got
}

func TestTitle(t *testing.T) {
	tests := []struct {
		name string
		body string
		want string
	}{
		{
			name: "простой заголовок",
			body: `<html><head><title>Простой заголовок</title></head><body></body></html>`,
			want: "Простой заголовок",
		},
		{
			name: "пробелы и переводы строк",
			body: "<html><head><title>  Пробелы\n\tи   переводы\nстрок </title></head></html>",
			want: "Пробелы и переводы строк",
		},
		{
			name: "сущности раскрываются",
			body: `<html><head><title>Fish &amp; Chips &lt;3</title></head></html>`,
			want: "Fish & Chips <3",
		},
		{
			name: "числовые сущности",
			body: `<html><head><title>&#1055;&#1088;&#1080;&#1074;&#1077;&#1090;</title></head></html>`,
			want: "Привет",
		},
		{
			name: "заголовка нет",
			body: `<html><body><h1>Только h1</h1></body></html>`,
			want: "",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := titleFromBody(t, "text/html", tt.body); got != tt.want {
				t.Errorf("Title = %q, ожидалось %q", got, tt.want)
			}
		})
	}
}

func TestTitlePriority(t *testing.T) {
	tests := []struct {
		name string
		body string
		want string
	}{
		{
			name: "og:title выше title",
			body: `<html><head><meta property="og:title" content="OG заголовок"><title>HTML заголовок</title></head></html>`,
			want: "OG заголовок",
		},
		{
			name: "title выше og:title",
			body: `<html><head><title>HTML заголовок</title><meta property="og:title" content="OG заголовок"></head></html>`,
			want: "HTML заголовок",
		},
		{
			name: "пустой og:title пропускается",
			body: `<html><head><meta property="og:title" content="   "><title>HTML заголовок</title></head></html>`,
			want: "HTML заголовок",
		},
		{
			name: "пустой title пропускается",
			body: `<html><head><title></title><meta property="og:title" content="OG заголовок"></head></html>`,
			want: "OG заголовок",
		},
		{
			name: "og:title без content пропускается",
			body: `<html><head><meta property="og:title"><title>HTML заголовок</title></head></html>`,
			want: "HTML заголовок",
		},
		{
			name: "регистр и пробелы в property",
			body: `<html><head><meta property=" OG:TITLE " content="OG заголовок"><title>HTML заголовок</title></head></html>`,
			want: "OG заголовок",
		},
		{
			name: "twitter:title и h1 игнорируются",
			body: `<html><head><meta name="twitter:title" content="Twitter"><title></title></head><body><h1>Заголовок h1</h1></body></html>`,
			want: "",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := titleFromBody(t, "text/html", tt.body); got != tt.want {
				t.Errorf("Title = %q, ожидалось %q", got, tt.want)
			}
		})
	}
}

func TestTitleCharset(t *testing.T) {
	tests := []struct {
		name        string
		contentType string
		body        []byte
		want        string
	}{
		{
			name:        "windows-1251 через meta",
			contentType: "text/html",
			body:        []byte(`<html><head><meta charset="windows-1251"><title>` + string(cp1251Привет) + `</title></head></html>`),
			want:        "Привет",
		},
		{
			name:        "windows-1251 через Content-Type",
			contentType: "text/html; charset=windows-1251",
			body:        []byte(`<html><head><title>` + string(cp1251Привет) + `</title></head></html>`),
			want:        "Привет",
		},
		{
			name:        "UTF-8 без изменений",
			contentType: "text/html; charset=utf-8",
			body:        []byte(`<html><head><title>Привет</title></head></html>`),
			want:        "Привет",
		},
		{
			name:        "UTF-8 с BOM",
			contentType: "text/html",
			body:        append([]byte{0xEF, 0xBB, 0xBF}, []byte(`<html><head><title>Привет</title></head></html>`)...),
			want:        "Привет",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			fetch := newTestFetcher(func(*http.Request) (*http.Response, error) {
				resp := htmlResponse(http.StatusOK, tt.contentType, string(tt.body))
				return resp, nil
			})
			got, err := fetch.Title(context.Background(), "https://example.com/")
			if err != nil {
				t.Fatalf("Title: %v", err)
			}
			if got != tt.want {
				t.Errorf("Title = %q, ожидалось %q", got, tt.want)
			}
		})
	}
}

func FuzzExtractTitle(f *testing.F) {
	for _, seed := range []string{
		"",
		`<title>Простой</title>`,
		`<meta property="og:title" content="OG">`,
		"<title>Fish &amp; Chips</title>",
		"<title>\xff\xfe\x00</title>",
		`<html><head><meta charset="windows-1251"><title>` + string(cp1251Привет) + `</title></head></html>`,
	} {
		f.Add(seed)
	}
	f.Fuzz(func(t *testing.T, body string) {
		got := extractTitle(strings.NewReader(body), "text/html; charset=utf-8")
		if !utf8.ValidString(got) {
			t.Fatalf("extractTitle вернул невалидный UTF-8: %q", got)
		}
	})
}
