package pagetitle

import (
	"io"
	"strings"

	"golang.org/x/net/html"
	"golang.org/x/net/html/charset"
)

// extractTitle декодирует HTML из r по Content-Type (BOM, charset из заголовка,
// <meta charset>, эвристика HTML-спеки) и возвращает первый непустой кандидат:
// текст <title> или атрибут content тега <meta property="og:title"> — что выше
// по документу. Пустая строка — кандидатов нет или ответ не разобран.
func extractTitle(r io.Reader, contentType string) string {
	decoded, err := charset.NewReader(r, contentType)
	if err != nil {
		return ""
	}
	doc, err := html.Parse(decoded)
	if err != nil {
		return ""
	}
	return firstTitle(doc)
}

// firstTitle обходит дерево в порядке документа и возвращает первый непустой
// кандидат: <title> в HTML-пространстве имён или og:title.
func firstTitle(n *html.Node) string {
	if n.Type == html.ElementNode && n.Namespace == "" {
		switch {
		case n.Data == "title":
			if title := normalizeTitle(textContent(n, nil)); title != "" {
				return title
			}
		case n.Data == "meta":
			if isOGTitle(n) {
				if title := normalizeTitle(attrValue(n, "content")); title != "" {
					return title
				}
			}
		}
	}
	for child := n.FirstChild; child != nil; child = child.NextSibling {
		if title := firstTitle(child); title != "" {
			return title
		}
	}
	return ""
}

// isOGTitle сообщает, задаёт ли тег meta свойство og:title (значение атрибута
// property сравнивается без учёта регистра и краевых пробелов).
func isOGTitle(n *html.Node) bool {
	return strings.EqualFold(strings.TrimSpace(attrValue(n, "property")), "og:title")
}

// attrValue возвращает значение атрибута key с точным (разобранным парсером в
// нижний регистр) именем.
func attrValue(n *html.Node, key string) string {
	for _, a := range n.Attr {
		if a.Key == key {
			return a.Val
		}
	}
	return ""
}

// textContent склеивает текстовые узлы поддерева n, разделяя их пробелом.
func textContent(n *html.Node, b *strings.Builder) string {
	if b == nil {
		b = &strings.Builder{}
	}
	if n.Type == html.TextNode {
		if b.Len() > 0 {
			b.WriteByte(' ')
		}
		b.WriteString(n.Data)
	}
	for child := n.FirstChild; child != nil; child = child.NextSibling {
		textContent(child, b)
	}
	return b.String()
}

// normalizeTitle схлопывает любые последовательности пробельных символов в один
// пробел и срезает краевые пробелы.
func normalizeTitle(s string) string {
	return strings.Join(strings.Fields(s), " ")
}
