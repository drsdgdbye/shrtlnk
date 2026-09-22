package shortener

import (
	"errors"
	"strings"
	"testing"
	"unicode/utf8"
)

func TestValidateURL(t *testing.T) {
	prefix := "https://example.com/"
	maxLenURL := prefix + strings.Repeat("a", MaxURLLength-len(prefix))

	tests := []struct {
		name    string
		in      string
		want    string
		wantErr bool
	}{
		{name: "http", in: "http://host/x", want: "http://host/x"},
		{name: "https c query", in: "https://host/x?y=1", want: "https://host/x?y=1"},
		{name: "пробелы по краям", in: "  https://host/x  ", want: "https://host/x"},
		{name: "верхний регистр схемы", in: "HTTPS://host/x", want: "HTTPS://host/x"},
		{name: "ровно максимальная длина", in: maxLenURL, want: maxLenURL},
		{name: "ftp", in: "ftp://host", wantErr: true},
		{name: "mailto", in: "mailto:a@b", wantErr: true},
		{name: "пусто", in: "", wantErr: true},
		{name: "только пробелы", in: "   ", wantErr: true},
		{name: "нет хоста", in: "http://", wantErr: true},
		{name: "относительный путь", in: "/x", wantErr: true},
		{name: "схема без разделителя", in: "https:/host", wantErr: true},
		{name: "длиннее максимума", in: "https://example.com/" + strings.Repeat("a", MaxURLLength), wantErr: true},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := ValidateURL(tt.in)
			if tt.wantErr {
				if !errors.Is(err, ErrInvalidURL) {
					t.Fatalf("ValidateURL(%q) ошибка = %v, ожидалась ErrInvalidURL", tt.in, err)
				}
				return
			}
			if err != nil {
				t.Fatalf("ValidateURL(%q) неожиданная ошибка: %v", tt.in, err)
			}
			if got != tt.want {
				t.Errorf("ValidateURL(%q) = %q, ожидалось %q", tt.in, got, tt.want)
			}
		})
	}
}

func TestTruncateTitle(t *testing.T) {
	tests := []struct {
		name string
		in   string
		want string
	}{
		{name: "пусто", in: "", want: ""},
		{name: "короче предела", in: "Коротко", want: "Коротко"},
		{name: "ровно 32 латиницы", in: strings.Repeat("a", MaxTitleLength), want: strings.Repeat("a", MaxTitleLength)},
		{
			name: "33 латиницы",
			in:   strings.Repeat("a", MaxTitleLength+1),
			want: strings.Repeat("a", MaxTitleLength-1) + "…",
		},
		{
			name: "ровно 32 кириллицы",
			in:   strings.Repeat("я", MaxTitleLength),
			want: strings.Repeat("я", MaxTitleLength),
		},
		{
			name: "33 кириллицы",
			in:   strings.Repeat("я", MaxTitleLength+1),
			want: strings.Repeat("я", MaxTitleLength-1) + "…",
		},
		{
			name: "кириллица считается по рунам",
			in:   strings.Repeat("я", MaxTitleLength-1) + "абв",
			want: strings.Repeat("я", MaxTitleLength-1) + "…",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := TruncateTitle(tt.in); got != tt.want {
				t.Errorf("TruncateTitle(%q) = %q, ожидалось %q", tt.in, got, tt.want)
			}
		})
	}
}

func TestDomain(t *testing.T) {
	tests := []struct {
		name string
		in   string
		want string
	}{
		{name: "обычный хост", in: "https://blog.example.com/x", want: "blog.example.com"},
		{name: "www и порт", in: "http://www.example.com:8080/x", want: "example.com"},
		{name: "регистр сохраняется", in: "https://WWW.Example.COM/x", want: "Example.COM"},
		{name: "www2 не срезается", in: "https://www2.example.com/x", want: "www2.example.com"},
		{name: "wwww не срезается", in: "https://wwww.example.com/x", want: "wwww.example.com"},
		{name: "хост ровно www.", in: "https://www./x", want: ""},
		{name: "пустой хост с портом", in: "http://:8080/x", want: ""},
		{name: "только порт", in: "http://example.com:443/x", want: "example.com"},
		{name: "IPv6-литерал", in: "https://[2001:db8::1]:8443/x", want: "2001:db8::1"},
		{name: "не URL", in: "not a link", want: ""},
		{name: "пусто", in: "", want: ""},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := Domain(tt.in); got != tt.want {
				t.Errorf("Domain(%q) = %q, ожидалось %q", tt.in, got, tt.want)
			}
		})
	}
}

func TestTitleFor(t *testing.T) {
	tests := []struct {
		name      string
		pageTitle string
		rawURL    string
		want      string
	}{
		{name: "заголовок побеждает домен", pageTitle: "Заголовок", rawURL: "https://www.example.com/x", want: "Заголовок"},
		{name: "пробельный заголовок — домен", pageTitle: "  \n ", rawURL: "https://www.example.com/x", want: "example.com"},
		{name: "нет заголовка — домен", pageTitle: "", rawURL: "https://www.example.com/x", want: "example.com"},
		{name: "нет домена — URL", pageTitle: "", rawURL: "https://www./x", want: "https://www./x"},
		{
			name:      "заголовок усечён",
			pageTitle: strings.Repeat("я", MaxTitleLength+1),
			rawURL:    "https://example.com/x",
			want:      strings.Repeat("я", MaxTitleLength-1) + "…",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := TitleFor(tt.pageTitle, tt.rawURL); got != tt.want {
				t.Errorf("TitleFor(%q, %q) = %q, ожидалось %q", tt.pageTitle, tt.rawURL, got, tt.want)
			}
		})
	}
}

func TestDisplayTitle(t *testing.T) {
	longNoHostURL := "https://www./" + strings.Repeat("x", 100)

	tests := []struct {
		name string
		link Link
		want string
	}{
		{
			name: "сохранённое название",
			link: Link{Code: "a", URL: "https://www.example.com/x", Title: "Название страницы"},
			want: "Название страницы",
		},
		{
			name: "название усечено",
			link: Link{Code: "a", URL: "https://www.example.com/x", Title: strings.Repeat("я", MaxTitleLength+1)},
			want: strings.Repeat("я", MaxTitleLength-1) + "…",
		},
		{
			name: "без названия — домен без www",
			link: Link{Code: "a", URL: "https://www.example.com/x"},
			want: "example.com",
		},
		{
			name: "домен пуст — сам URL",
			link: Link{Code: "a", URL: "https://www./x"},
			want: "https://www./x",
		},
		{
			name: "длинный URL без домена усечён",
			link: Link{Code: "a", URL: longNoHostURL},
			want: "https://www./" + strings.Repeat("x", 18) + "…",
		},
		{
			name: "пустая ссылка",
			link: Link{Code: "a"},
			want: "",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := tt.link.DisplayTitle(); got != tt.want {
				t.Errorf("DisplayTitle() = %q, ожидалось %q", got, tt.want)
			}
		})
	}
}

func FuzzTruncateTitle(f *testing.F) {
	f.Add("")
	f.Add("Короткий заголовок")
	f.Add(strings.Repeat("a", MaxTitleLength))
	f.Add(strings.Repeat("б", MaxTitleLength+1))
	f.Fuzz(func(t *testing.T, s string) {
		got := TruncateTitle(s)
		gotRunes := utf8.RuneCountInString(got)
		if gotRunes > MaxTitleLength {
			t.Fatalf("TruncateTitle(%q) = %q: %d рун, предел %d", s, got, gotRunes, MaxTitleLength)
		}
		inRunes := utf8.RuneCountInString(s)
		if inRunes <= MaxTitleLength {
			if got != s {
				t.Fatalf("TruncateTitle(%q) = %q, вход не должен меняться", s, got)
			}
			return
		}
		if !strings.HasSuffix(got, "…") || gotRunes != MaxTitleLength {
			t.Fatalf("TruncateTitle(%q) = %q: ожидались %d рун с многоточием", s, got, MaxTitleLength)
		}
	})
}

func TestGenerateCode(t *testing.T) {
	seen := make(map[string]struct{})
	for i := 0; i < 100; i++ {
		code, err := GenerateCode()
		if err != nil {
			t.Fatalf("GenerateCode() ошибка: %v", err)
		}
		if len(code) != CodeLength {
			t.Fatalf("GenerateCode() = %q, длина %d, ожидалась %d", code, len(code), CodeLength)
		}
		for _, r := range code {
			if !strings.ContainsRune(codeAlphabet, r) {
				t.Fatalf("GenerateCode() = %q, недопустимый символ %q", code, r)
			}
		}
		seen[code] = struct{}{}
	}
	if len(seen) != 100 {
		t.Errorf("GenerateCode() вернул %d уникальных кодов из 100", len(seen))
	}
}
