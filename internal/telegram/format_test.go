package telegram

import (
	"fmt"
	"strings"
	"testing"

	"github.com/go-telegram/bot/models"

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

func TestAnchorWithText(t *testing.T) {
	got := anchorWithText("https://example.com/a?b=1&c=2", "Заголовок <b>")
	want := "<a href=\"https://example.com/a?b=1&amp;c=2\">Заголовок &lt;b&gt;</a>"
	if got != want {
		t.Errorf("anchorWithText = %q, ожидалось %q", got, want)
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

// pagesLinks возвращает n ссылок с предсказуемыми кодами и названиями.
func pagesLinks(n int) []shortener.Link {
	links := make([]shortener.Link, 0, n)
	for i := 1; i <= n; i++ {
		links = append(links, shortener.Link{
			Code:  fmt.Sprintf("code%04d", i),
			URL:   fmt.Sprintf("https://example.com/page/%d", i),
			Title: fmt.Sprintf("Название %d", i),
		})
	}
	return links
}

func TestLinksPageCount(t *testing.T) {
	tests := []struct {
		total int
		want  int
	}{
		{total: 0, want: 1},
		{total: 1, want: 1},
		{total: 10, want: 1},
		{total: 11, want: 2},
		{total: 20, want: 2},
		{total: 21, want: 3},
		{total: 23, want: 3},
	}
	for _, tt := range tests {
		t.Run(fmt.Sprintf("%d ссылок", tt.total), func(t *testing.T) {
			if got := linksPageCount(tt.total); got != tt.want {
				t.Errorf("linksPageCount(%d) = %d, ожидалось %d", tt.total, got, tt.want)
			}
		})
	}
}

func TestFormatLinksPage(t *testing.T) {
	const base = "https://shrt.example"

	t.Run("пустой список", func(t *testing.T) {
		if got := formatLinksPage(base, nil, 0); got != emptyLinksText {
			t.Errorf("formatLinksPage(пусто) = %q, ожидалось %q", got, emptyLinksText)
		}
	})

	t.Run("первая страница", func(t *testing.T) {
		links := pagesLinks(23)
		got := formatLinksPage(base, links, 0)
		if !strings.HasPrefix(got, linksHeader) {
			t.Errorf("страница не начинается с %q: %q", linksHeader, got)
		}
		if lines := strings.Count(got, "\n"); lines != linksPageSize {
			t.Errorf("строк на первой странице = %d, ожидалось %d", lines, linksPageSize)
		}
		if !strings.Contains(got, "\n1. ") || !strings.Contains(got, "\n10. ") {
			t.Errorf("нет глобальной нумерации 1..10: %q", got)
		}
		if strings.Contains(got, "\n11. ") {
			t.Errorf("первая страница содержит строку 11: %q", got)
		}
		want := fmt.Sprintf(`<a href="%s/code0001">Название 1</a>`, base)
		if !strings.Contains(got, want) {
			t.Errorf("нет анкора %q в %q", want, got)
		}
	})

	t.Run("последняя страница с глобальной нумерацией", func(t *testing.T) {
		links := pagesLinks(23)
		got := formatLinksPage(base, links, 2)
		for i := 21; i <= 23; i++ {
			want := fmt.Sprintf("\n%d. <a href=\"%s/code%04d\">Название %d</a>", i, base, i, i)
			if !strings.Contains(got, want) {
				t.Errorf("страница 2 не содержит %q: %q", want, got)
			}
		}
		if strings.Contains(got, "\n20. ") {
			t.Errorf("последняя страница содержит строку 20: %q", got)
		}
	})

	t.Run("страница вне диапазона прижимается", func(t *testing.T) {
		links := pagesLinks(23)
		if got, want := formatLinksPage(base, links, 99), formatLinksPage(base, links, 2); got != want {
			t.Errorf("страница 99 = %q, ожидалась последняя страница %q", got, want)
		}
		if got, want := formatLinksPage(base, links, -5), formatLinksPage(base, links, 0); got != want {
			t.Errorf("страница -5 = %q, ожидалась первая страница %q", got, want)
		}
	})
}

func TestLinksKeyboard(t *testing.T) {
	const userID = 7

	t.Run("первая страница из трёх", func(t *testing.T) {
		markup := linksKeyboard(userID, 0, 3)
		keyboard, ok := markup.(models.InlineKeyboardMarkup)
		if !ok {
			t.Fatalf("клавиатура = %#v, ожидался InlineKeyboardMarkup", markup)
		}
		if len(keyboard.InlineKeyboard) != 1 || len(keyboard.InlineKeyboard[0]) != 1 {
			t.Fatalf("кнопок = %#v, ожидалась одна кнопка", keyboard.InlineKeyboard)
		}
		button := keyboard.InlineKeyboard[0][0]
		if button.Text != ">" {
			t.Errorf("текст кнопки = %q, ожидался %q", button.Text, ">")
		}
		if want := encodeLinksCallback(userID, 1); button.CallbackData != want {
			t.Errorf("callback_data = %q, ожидалось %q", button.CallbackData, want)
		}
	})

	t.Run("средняя страница", func(t *testing.T) {
		markup := linksKeyboard(userID, 1, 3)
		keyboard, ok := markup.(models.InlineKeyboardMarkup)
		if !ok {
			t.Fatalf("клавиатура = %#v, ожидался InlineKeyboardMarkup", markup)
		}
		if len(keyboard.InlineKeyboard) != 1 || len(keyboard.InlineKeyboard[0]) != 2 {
			t.Fatalf("кнопок = %#v, ожидались две кнопки", keyboard.InlineKeyboard)
		}
		prev, next := keyboard.InlineKeyboard[0][0], keyboard.InlineKeyboard[0][1]
		if prev.Text != "<" || prev.CallbackData != encodeLinksCallback(userID, 0) {
			t.Errorf("кнопка назад = %+v, ожидались %q/%q", prev, "<", encodeLinksCallback(userID, 0))
		}
		if next.Text != ">" || next.CallbackData != encodeLinksCallback(userID, 2) {
			t.Errorf("кнопка вперёд = %+v, ожидались %q/%q", next, ">", encodeLinksCallback(userID, 2))
		}
	})

	t.Run("последняя страница", func(t *testing.T) {
		markup := linksKeyboard(userID, 2, 3)
		keyboard, ok := markup.(models.InlineKeyboardMarkup)
		if !ok {
			t.Fatalf("клавиатура = %#v, ожидался InlineKeyboardMarkup", markup)
		}
		if len(keyboard.InlineKeyboard) != 1 || len(keyboard.InlineKeyboard[0]) != 1 {
			t.Fatalf("кнопок = %#v, ожидалась одна кнопка", keyboard.InlineKeyboard)
		}
		button := keyboard.InlineKeyboard[0][0]
		if button.Text != "<" {
			t.Errorf("текст кнопки = %q, ожидался %q", button.Text, "<")
		}
		if want := encodeLinksCallback(userID, 1); button.CallbackData != want {
			t.Errorf("callback_data = %q, ожидалось %q", button.CallbackData, want)
		}
	})

	t.Run("одна страница", func(t *testing.T) {
		if markup := linksKeyboard(userID, 0, 1); markup != nil {
			t.Errorf("клавиатура одной страницы = %#v, ожидался nil", markup)
		}
	})

	t.Run("пустой список", func(t *testing.T) {
		if markup := linksKeyboard(userID, 0, linksPageCount(0)); markup != nil {
			t.Errorf("клавиатура пустого списка = %#v, ожидался nil", markup)
		}
	})
}

func TestLinksCallbackData(t *testing.T) {
	t.Run("обратимость", func(t *testing.T) {
		tests := []struct {
			userID int64
			page   int
		}{
			{userID: 1, page: 0},
			{userID: 7, page: 2},
			{userID: -42, page: -1},
			{userID: 9223372036854775807, page: 123456},
		}
		for _, tt := range tests {
			data := encodeLinksCallback(tt.userID, tt.page)
			gotUser, gotPage, ok := parseLinksCallback(data)
			if !ok {
				t.Errorf("parseLinksCallback(%q) не распознал данные", data)
				continue
			}
			if gotUser != tt.userID || gotPage != tt.page {
				t.Errorf("parseLinksCallback(%q) = (%d, %d), ожидалось (%d, %d)",
					data, gotUser, gotPage, tt.userID, tt.page)
			}
		}
	})

	t.Run("мусор отклоняется", func(t *testing.T) {
		bad := []string{
			"",
			"links:",
			"links:1",
			"links:1:",
			"links::0",
			"links:abc:0",
			"links:1:abc",
			"links:1:0:extra",
			"other:1:0",
			"1:0",
		}
		for _, data := range bad {
			if _, _, ok := parseLinksCallback(data); ok {
				t.Errorf("parseLinksCallback(%q) вернул ok, ожидалось отклонение", data)
			}
		}
	})
}
