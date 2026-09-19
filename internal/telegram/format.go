package telegram

import (
	"fmt"
	"html"
	"strings"

	"yt_dw/internal/shortener"
)

// Пользовательские тексты. Значения — часть контракта, менять нельзя.
const (
	fallbackText     = "Expected link of format: https://your/own?long&link"
	startText        = "Hi! Send me a link and I'll shorten it. Use /links to see the links I've given you."
	emptyLinksText   = "You have no short links yet."
	linksHeader      = "Your short links:"
	shortenPrefix    = "Your short link: "
	storageErrorText = "Could not save the link, please try again later."
)

// anchor оборачивает URL в кликабельный HTML-анкор, экранируя значение.
func anchor(u string) string {
	escaped := html.EscapeString(u)
	return "<a href=\"" + escaped + "\">" + escaped + "</a>"
}

// absoluteShortURL собирает абсолютную короткую ссылку из базы и кода.
func absoluteShortURL(base, code string) string {
	return strings.TrimRight(base, "/") + "/" + code
}

// formatShort форматирует ответ на успешное сокращение.
func formatShort(short string) string {
	return shortenPrefix + anchor(short)
}

// formatLinks форматирует персональный список коротких ссылок.
func formatLinks(base string, links []shortener.Link) string {
	if len(links) == 0 {
		return emptyLinksText
	}
	var b strings.Builder
	b.WriteString(linksHeader)
	for i, link := range links {
		fmt.Fprintf(&b, "\n%d. %s", i+1, anchor(absoluteShortURL(base, link.Code)))
	}
	return b.String()
}
