package shortener

import (
	"errors"
	"fmt"
	"math/rand"
	"net/netip"
	"net/url"
	"strings"
	"testing"
)

// validateURLReference — эталон: дословная копия реализации ValidateURL на базе
// url.Parse. Это единственное место, где url.Parse допустим после перевода
// валидации на аллокационно-свободную проверку.
func validateURLReference(raw string) (string, error) {
	trimmed := strings.TrimSpace(raw)
	if trimmed == "" || len(trimmed) > MaxURLLength {
		return "", ErrInvalidURL
	}
	u, err := url.Parse(trimmed)
	if err != nil {
		return "", ErrInvalidURL
	}
	scheme := strings.ToLower(u.Scheme)
	if (scheme != "http" && scheme != "https") || u.Host == "" {
		return "", ErrInvalidURL
	}
	return trimmed, nil
}

// urlCorpus возвращает входы дифференциального теста: обязательные seeds брифа
// T-20260919-03 и пограничные случаи, найденные при разборе net/url.
func urlCorpus() []string {
	corpus := []string{
		// Префиксы, схемы, относительные ссылки.
		"HTTP://H", "hTTpS://h", "http:/h", "https:/host", "http:foo",
		"http:///path", "http://", "//h/p", "/path", "mailto:a@b", ":foo",
		"1http://h", "http", "http:", "https:",
		"httpx://h", "hTTpx://h", "http+s://h", "http.s://h", "http://h",
		// Authority: userinfo и порт.
		"http://:80", "http://:/", "http://@h/", "http://u@@h/", "http://u s@h/",
		"http://u%20s@h/", "http://user:p@ss@h/", "http://u:%zz@h/",
		"http://a:b:80/", "http://a:b/", "http://h:99999999999999999999/", "http://h:/",
		"http://:@h/", "http://u:@h/", "http://:p@h/", "http://u%3A@h/",
		"http://u%zz@h/", "http://u[p]@h/", "http://h:80:90/", "http://h::/",
		// Percent-escapes в host.
		"http://%41/", "http://%25/", "http://%c3%a9/", "http://%zz/", "http://%2/",
		"http://пример.рф/", "http://høst/", "http://%80/", "http://%FF/",
		"http://%2525/", "http://h%2F/", "http://h%5C/",
		// Скобки и IPv6-литералы.
		"http://[::1]/", "http://[::1]:80/", "http://[::1]x", "http://[127.0.0.1]/",
		"http://[::ffff:1.2.3.4]/", "http://[fe80::1%25eth0]/", "http://[fe80::1%eth0]/",
		"http://[v1.x]/", "http://[::1", "http://a[b]c/", "http://[]/", "http://[%3A%3A1]/",
		"http://[::]/", "http://[:]/", "http://[:::]/", "http://[1::2:3:4:5:6:7]/",
		"http://[1:2:3:4:5:6:7]/", "http://[1:2:3:4:5:6:7:8]/",
		"http://[1:2:3:4:5:6:7:8:9]/", "http://[1:2:3:4:5:6:7:8::]/",
		"http://[1::2::3]/", "http://[1:::2]/", "http://[::1:]/", "http://[::1:]",
		"http://[::%25]/", "http://[::%25eth0]/", "http://[::1%25]/",
		"http://[::%2525]/", "http://[::1%25%20]/", "http://[::1%25eth 0]/",
		"http://[::1%25eth%200]/", "http://[::1%2580]/", "http://[::1%25%%]/",
		"http://[::ffff:1.2.3.04]/", "http://[::ffff:01.2.3.4]/", "http://[::ffff:256.1.1.1]/",
		"http://[::ffff:1.2.3]/", "http://[::ffff:1.2.3.4.5]/", "http://[::1.2.3.4]/",
		"http://[1.2.3.4]/", "http://[g::1]/", "http://[12345::]/", "http://[::1]x/",
		"http://[::1]:/", "http://[::1]:%38/", "http://[::1]extra/", "http://]x/",
		"http://[/", "http://[a]b]/", "http://[::1]//x", "http://[[::1]]/",
		// Path, query, fragment.
		"http://h/#", "http://h/#%zz", "http://h/?a=%zz", "http://h/a b",
		"http://h/a?b#c", "http://h/a#b?c", "http://h/%", "http://h/%2",
		"http://h/%00", "http://h/\\", "http://h?a?b", "http://h#f#f", "http://h?",
		"http://h/?", "http://h/p%20", "http://h/p%2f", "http://h/p%2F", "http://h/##",
		"http://h/#%", "http://h/?#", "http://h?a=%", "http://h????????", "http://h//",
		"http://h///p", "http://h/a#b#c", "http://h/#frag?query",
		// CTL и Unicode.
		"http://h/a\x7f", "http://h/#\x7f", "\x00http://h", "http://h/\x00",
		"http://h/путь", "http://h/😀", "\thttp://h ", "http://h\n",
		"http://h/a\tb", "http://h/\x1f", "http://h/?\x7f", "http://h/#\x00",
		// Границы длины.
		"https://example.com/" + strings.Repeat("a", MaxURLLength-len("https://example.com/")),
		"https://example.com/" + strings.Repeat("a", MaxURLLength),
		strings.Repeat(" ", 2049),
		strings.Repeat("a", 2049),
		strings.Repeat("a", 2048),
		"  https://h  ",
	}
	return append(corpus, urlFuzzSeeds()...)
}

// urlFuzzSeeds возвращает обязательные seeds fuzz-цели: они покрывают классы
// префиксов, authority, экранирования host, скобок, path/query/fragment,
// CTL/Unicode и границ длины.
func urlFuzzSeeds() []string {
	return []string{
		"HTTP://H", "hTTpS://h", "http:/h", "https:/host", "http:foo", "http:///path",
		"http://", "//h/p", "/path", "mailto:a@b", ":foo", "1http://h", "http",
		"http:", "https:",
		"http://:80", "http://:/", "http://@h/", "http://u@@h/", "http://u s@h/",
		"http://u%20s@h/", "http://user:p@ss@h/", "http://u:%zz@h/", "http://a:b:80/",
		"http://a:b/", "http://h:99999999999999999999/", "http://h:/",
		"http://%41/", "http://%25/", "http://%c3%a9/", "http://%zz/", "http://%2/",
		"http://пример.рф/", "http://høst/",
		"http://[::1]/", "http://[::1]:80/", "http://[::1]x", "http://[127.0.0.1]/",
		"http://[::ffff:1.2.3.4]/", "http://[fe80::1%25eth0]/", "http://[fe80::1%eth0]/",
		"http://[v1.x]/", "http://[::1", "http://a[b]c/", "http://[]/",
		"http://[%3A%3A1]/",
		"http://h/#", "http://h/#%zz", "http://h/?a=%zz", "http://h/a b",
		"http://h/a?b#c", "http://h/a#b?c", "http://h/%", "http://h/%2",
		"http://h/%00", "http://h/\\", "http://h?a?b", "http://h#f#f", "http://h?",
		"http://h/?",
		"http://h/a\x7f", "http://h/#\x7f", "\x00http://h", "http://h/\x00",
		"http://h/путь", "http://h/😀", "\thttp://h ", "http://h\n",
		"https://example.com/" + strings.Repeat("a", MaxURLLength-len("https://example.com/")),
		"https://example.com/" + strings.Repeat("a", MaxURLLength),
		strings.Repeat(" ", 2049),
		strings.Repeat("a", 2049),
	}
}

// TestValidateURLMatchesReference — табличный дифференциальный тест: новая
// реализация обязана совпадать с эталоном по паре (значение, класс ошибки).
func TestValidateURLMatchesReference(t *testing.T) {
	for i, raw := range urlCorpus() {
		t.Run(fmt.Sprintf("%03d", i), func(t *testing.T) {
			got, gotErr := ValidateURL(raw)
			want, wantErr := validateURLReference(raw)
			if (gotErr == nil) != (wantErr == nil) {
				t.Fatalf("ValidateURL(%q) ошибка = %v, эталон = %v", raw, gotErr, wantErr)
			}
			if gotErr != nil {
				if !errors.Is(gotErr, ErrInvalidURL) || !errors.Is(wantErr, ErrInvalidURL) {
					t.Fatalf("ValidateURL(%q) ошибки не ErrInvalidURL: %v / %v", raw, gotErr, wantErr)
				}
				return
			}
			if got != want {
				t.Fatalf("ValidateURL(%q) = %q, эталон = %q", raw, got, want)
			}
		})
	}
}

// TestValidateURLAllocations фиксирует цель задачи: валидация не аллоцирует
// ни на одном входе, включая отклонённые IPv6-литералы.
func TestValidateURLAllocations(t *testing.T) {
	for _, raw := range urlCorpus() {
		if allocs := testing.AllocsPerRun(10, func() {
			if _, err := ValidateURL(raw); err != nil && !errors.Is(err, ErrInvalidURL) {
				t.Errorf("ValidateURL(%q) неожиданная ошибка: %v", raw, err)
			}
		}); allocs != 0 {
			t.Errorf("ValidateURL(%q) аллокаций = %v, ожидалось 0", raw, allocs)
		}
	}
}

// TestValidIPv6MatchesNetip проверяет разбор IPv6-адресной части (без зоны)
// против netip на фиксированном и детерминированно сгенерированном корпусе.
func TestValidIPv6MatchesNetip(t *testing.T) {
	reference := func(s string) bool {
		addr, err := netip.ParseAddr(s)
		return err == nil && !addr.Is4()
	}
	corpus := []string{
		"", ":", "::", ":::", "::1", "1::", "1:", ":1", "1", "g", "::g", "g::1",
		"1:2:3:4:5:6:7:8", "1:2:3:4:5:6:7", "1:2:3:4:5:6:7:8:9",
		"1:2:3:4:5:6:7:8:", "1:2:3:4:5:6:7:8::", "1::2::3", "1:::2",
		"1.2.3.4", "1.2.3", "1.2.3.4.5", "01.2.3.4", "0.0.0.0", "255.255.255.255",
		"256.1.1.1", "::1.2.3.4", "::ffff:1.2.3.4", "1:2:3:4:5:6:1.2.3.4",
		"1:2:3:4:5:6::1.2.3.4", "1:2:3:4:5::1.2.3.4", "1::1.2.3.4",
		"::1.2.3.4.5", "::01.2.3.4", "::256.1.1.1", "::1.2.3", "ffff::1.2.3.4",
		"12345::", "::12345", "1:2:3:4:5:6:7:1.2.3.4",
	}
	random := rand.New(rand.NewSource(1))
	tokens := []string{
		"::", ":", "1", "12", "123", "1234", "12345", "0", "00", "ffff", "ABCD",
		"1.2.3.4", "01.2.3.4", "255.255.255.255", "256.1.1.1", "1.2.3", "g", "z",
	}
	for i := 0; i < 30000; i++ {
		var b strings.Builder
		n := random.Intn(8)
		for j := 0; j <= n; j++ {
			b.WriteString(tokens[random.Intn(len(tokens))])
		}
		corpus = append(corpus, b.String())
	}
	for _, raw := range corpus {
		if got, want := validIPv6(raw), reference(raw); got != want {
			t.Errorf("validIPv6(%q) = %v, netip = %v", raw, got, want)
		}
	}
}

// BenchmarkValidateURL измеряет аллокации на фиксированном корпусе брифа:
// каждый кейс обязан дать 0 allocs/op.
func BenchmarkValidateURL(b *testing.B) {
	cases := []struct {
		name string
		in   string
		ok   bool
	}{
		{"V1", "https://example.com/", true},
		{"V2", "https://example.com/very/long/path?x=1&y=%D0%BF#frag", true},
		{"V3", "HTTPS://Example.COM/Path", true},
		{"V4", "http://user:pass@example.com:8080/x", true},
		{"V5", "https://example.com/%D0%BF%D1%83%D1%82%D1%8C?q=%20", true},
		{"V6", "https://[2001:db8::1]:8443/x", true},
		{"N1", "", false},
		{"N2", "   ", false},
		{"N3", "not a link", false},
		{"N4", "ftp://example.com/", false},
		{"N5", "http://", false},
		{"N6", "https://example.com/%zz", false},
		{"N7", "https://example.com/" + strings.Repeat("a", MaxURLLength), false},
	}
	for _, tc := range cases {
		b.Run(tc.name, func(b *testing.B) {
			b.ReportAllocs()
			for i := 0; i < b.N; i++ {
				got, err := ValidateURL(tc.in)
				if err != nil {
					if tc.ok {
						b.Fatalf("ValidateURL(%q) ошибка: %v", tc.in, err)
					}
					continue
				}
				if !tc.ok || got != tc.in {
					b.Fatalf("ValidateURL(%q) = %q, ожидался успех", tc.in, got)
				}
			}
		})
	}
}

// FuzzValidateURLMatchesReference сравнивает новую реализацию с эталоном по
// паре (значение, класс ошибки): либо обе (строка, nil), либо обе ("", ErrInvalidURL).
func FuzzValidateURLMatchesReference(f *testing.F) {
	for _, seed := range urlFuzzSeeds() {
		f.Add(seed)
	}
	f.Fuzz(func(t *testing.T, raw string) {
		got, gotErr := ValidateURL(raw)
		want, wantErr := validateURLReference(raw)
		if (gotErr == nil) != (wantErr == nil) {
			t.Fatalf("ValidateURL(%q) ошибка = %v, эталон = %v", raw, gotErr, wantErr)
		}
		if gotErr != nil {
			if !errors.Is(gotErr, ErrInvalidURL) {
				t.Fatalf("ValidateURL(%q) ошибка не ErrInvalidURL: %v", raw, gotErr)
			}
			return
		}
		if got != want {
			t.Fatalf("ValidateURL(%q) = %q, эталон = %q", raw, got, want)
		}
	})
}
