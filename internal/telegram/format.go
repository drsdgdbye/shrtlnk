package telegram

import (
	"fmt"
	"html"
	"strconv"
	"strings"

	"github.com/go-telegram/bot/models"

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

// Параметры пагинации списка ссылок.
const (
	// linksPageSize — число ссылок на одной странице /links.
	linksPageSize = 10
	// callbackPrefix — префикс callback_data кнопок пагинации.
	callbackPrefix = "links:"
)

// anchorWithText оборачивает URL в кликабельный HTML-анкор с заданным текстом,
// экранируя и адрес, и текст.
func anchorWithText(u, text string) string {
	return "<a href=\"" + html.EscapeString(u) + "\">" + html.EscapeString(text) + "</a>"
}

// anchor оборачивает URL в кликабельный HTML-анкор, экранируя значение.
func anchor(u string) string {
	return anchorWithText(u, u)
}

// absoluteShortURL собирает абсолютную короткую ссылку из базы и кода.
func absoluteShortURL(base, code string) string {
	return strings.TrimRight(base, "/") + "/" + code
}

// formatShort форматирует ответ на успешное сокращение.
func formatShort(short string) string {
	return shortenPrefix + anchor(short)
}

// linksPageCount возвращает число страниц списка, минимум 1.
func linksPageCount(total int) int {
	if total <= 0 {
		return 1
	}
	return (total + linksPageSize - 1) / linksPageSize
}

// formatLinksPage форматирует страницу page (0-based; вне диапазона прижимается
// к границе). Пустой список → emptyLinksText. Нумерация сквозная: строка i
// имеет номер i+1.
func formatLinksPage(base string, links []shortener.Link, page int) string {
	if len(links) == 0 {
		return emptyLinksText
	}
	page = clampPage(page, linksPageCount(len(links)))
	start := page * linksPageSize
	end := min(start+linksPageSize, len(links))

	var b strings.Builder
	b.WriteString(linksHeader)
	for i := start; i < end; i++ {
		fmt.Fprintf(&b, "\n%d. %s", i+1,
			anchorWithText(absoluteShortURL(base, links[i].Code), links[i].DisplayTitle()))
	}
	return b.String()
}

// linksKeyboard возвращает клавиатуру навигации: на первой странице — только
// ">" с links:<user>:<page+1>, на последней — только "<" с
// links:<user>:<page-1>, на средних — обе; nil, если страница одна (Q13) или
// список пуст.
func linksKeyboard(userID int64, page, pages int) models.ReplyMarkup {
	if pages <= 1 {
		return nil
	}
	page = clampPage(page, pages)

	row := make([]models.InlineKeyboardButton, 0, 2)
	if page > 0 {
		row = append(row, models.InlineKeyboardButton{
			Text:         "<",
			CallbackData: encodeLinksCallback(userID, page-1),
		})
	}
	if page < pages-1 {
		row = append(row, models.InlineKeyboardButton{
			Text:         ">",
			CallbackData: encodeLinksCallback(userID, page+1),
		})
	}
	if len(row) == 0 {
		return nil
	}
	return models.InlineKeyboardMarkup{InlineKeyboard: [][]models.InlineKeyboardButton{row}}
}

// clampPage прижимает страницу к диапазону [0, pages-1].
func clampPage(page, pages int) int {
	if page < 0 {
		return 0
	}
	if page >= pages {
		return pages - 1
	}
	return page
}

// encodeLinksCallback кодирует пользователя и целевую страницу в callback_data.
func encodeLinksCallback(userID int64, page int) string {
	return fmt.Sprintf("%s%d:%d", callbackPrefix, userID, page)
}

// parseLinksCallback разбирает callback_data формата "links:<user_id>:<page>".
// Отрицательная страница синтаксически допустима (обработчик отклоняет её по
// диапазону); мусор возвращает ok == false.
func parseLinksCallback(data string) (userID int64, page int, ok bool) {
	rest, found := strings.CutPrefix(data, callbackPrefix)
	if !found {
		return 0, 0, false
	}
	idPart, pagePart, found := strings.Cut(rest, ":")
	if !found {
		return 0, 0, false
	}
	userID, err := strconv.ParseInt(idPart, 10, 64)
	if err != nil {
		return 0, 0, false
	}
	page, err = strconv.Atoi(pagePart)
	if err != nil {
		return 0, 0, false
	}
	return userID, page, true
}
