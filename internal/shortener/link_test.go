package shortener

import (
	"errors"
	"strings"
	"testing"
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
