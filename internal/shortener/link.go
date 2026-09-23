// Package shortener отвечает за валидацию ссылок, генерацию коротких кодов
// и персистентное хранение соответствий «код → URL».
package shortener

import (
	"crypto/rand"
	"errors"
	"fmt"
	"net/url"
	"strings"
	"time"
)

// Параметры коротких кодов и ограничения входных ссылок.
const (
	// CodeLength — длина короткого кода в символах.
	CodeLength = 7
	// MaxURLLength — максимальная длина принимаемой ссылки после TrimSpace.
	MaxURLLength = 2048
	// MaxTitleLength — предельная длина названия короткой ссылки, в рунах.
	MaxTitleLength = 32
	// codeAlphabet — base62-алфавит короткого кода.
	codeAlphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
	// rejectionLimit — граница rejection sampling: 256 - (256 % 62) = 248.
	rejectionLimit = 248
	// titleEllipsis — многоточие усечения (U+2026); усечённое название —
	// первые MaxTitleLength-1 рун плюс этот символ.
	titleEllipsis = "…"
)

// Ошибки пакета. Возвращаются через errors.Is.
var (
	// ErrInvalidURL — входная строка не является абсолютным http(s) URL.
	ErrInvalidURL = errors.New("некорректная ссылка")
	// ErrNotFound — ссылка с запрошенным кодом отсутствует.
	ErrNotFound = errors.New("ссылка не найдена")
	// ErrCodeExhausted — не удалось подобрать свободный код за отведённые попытки.
	ErrCodeExhausted = errors.New("не удалось сгенерировать уникальный код")
)

// Link описывает сохранённую короткую ссылку.
type Link struct {
	Code      string    `json:"code"`
	UserID    int64     `json:"user_id"`
	URL       string    `json:"url"`
	Title     string    `json:"title"` // "" — название не сохранено (загрузка идёт/сбой/его нет/строка до v2)
	CreatedAt time.Time `json:"created_at"`
}

// Domain возвращает имя хоста URL без порта и без ведущего "www." (без учёта
// регистра). "" — если URL не разобран, хост пуст или после среза осталась
// пустая строка. Примеры: "https://blog.example.com/x" → "blog.example.com";
// "http://www.example.com:8080/x" → "example.com";
// "https://WWW.Example.COM/x" → "Example.COM";
// "https://www2.example.com/x" → "www2.example.com";
// "http://:8080/x", "https://www./x" → "".
func Domain(rawURL string) string {
	u, err := url.Parse(rawURL)
	if err != nil {
		return ""
	}
	host := u.Hostname()
	if len(host) >= len("www.") && strings.EqualFold(host[:len("www.")], "www.") {
		host = host[len("www."):]
	}
	return host
}

// TruncateTitle усекает s до MaxTitleLength рун: длиннее — первые
// MaxTitleLength-1 рун плюс "…". Ровно MaxTitleLength рун и короче — без
// изменений; счёт по рунам, не по байтам.
func TruncateTitle(s string) string {
	runes := []rune(s)
	if len(runes) <= MaxTitleLength {
		return s
	}
	return string(runes[:MaxTitleLength-1]) + titleEllipsis
}

// TitleFor возвращает итоговое название: pageTitle (если после trim непусто),
// иначе Domain(rawURL), иначе rawURL; результат прогнан через TruncateTitle.
func TitleFor(pageTitle, rawURL string) string {
	if pageTitle = strings.TrimSpace(pageTitle); pageTitle != "" {
		return TruncateTitle(pageTitle)
	}
	if domain := Domain(rawURL); domain != "" {
		return TruncateTitle(domain)
	}
	return TruncateTitle(rawURL)
}

// DisplayTitle возвращает название ссылки для показа: Title; если пусто —
// Domain(URL); если и он пуст — URL; результат не длиннее MaxTitleLength рун.
func (l Link) DisplayTitle() string {
	if l.Title != "" {
		return TruncateTitle(l.Title)
	}
	if domain := Domain(l.URL); domain != "" {
		return TruncateTitle(domain)
	}
	return TruncateTitle(l.URL)
}

// ValidateURL обрезает пробелы, проверяет длину (1..MaxURLLength) и структуру
// абсолютного http(s)-URL, не аллоцируя память. Проверка воспроизводит
// принимающую логику url.Parse тулчейна go1.27.1 при go-директивой 1.26
// (urlstrictcolons по умолчанию включён): схема http/https без учёта регистра,
// непустой Host, корректные percent-escapes в userinfo, host, пути и фрагменте.
// Возвращает обрезанную строку как канонический URL.
func ValidateURL(raw string) (string, error) {
	trimmed := strings.TrimSpace(raw)
	if trimmed == "" || len(trimmed) > MaxURLLength {
		return "", ErrInvalidURL
	}
	if !validAbsoluteURL(trimmed) {
		return "", ErrInvalidURL
	}
	return trimmed, nil
}

// validAbsoluteURL сообщает, принимает ли url.Parse строку s как абсолютный
// http(s)-URL с непустым Host. Разбор идёт по байтам, промежуточные строки не
// создаются, поэтому вызов не аллоцирует.
func validAbsoluteURL(s string) bool {
	body := s
	if hash := strings.IndexByte(s, '#'); hash >= 0 {
		// url.Parse отрезает фрагмент до проверки CTL-байтов и разбирает его
		// отдельно: невалидный percent-escape во фрагменте отклоняет URL.
		if frag := s[hash+1:]; frag != "" && !validPercentEscapes(frag) {
			return false
		}
		body = s[:hash]
	}
	for i := 0; i < len(body); i++ {
		if b := body[i]; b < 0x20 || b == 0x7f {
			return false
		}
	}
	rest, ok := cutHTTPScheme(body)
	if !ok {
		return false
	}
	if q := strings.IndexByte(rest, '?'); q >= 0 {
		rest = rest[:q]
	}
	if len(rest) < 2 || rest[0] != '/' || rest[1] != '/' {
		return false
	}
	authority := rest[2:]
	path := ""
	if slash := strings.IndexByte(authority, '/'); slash >= 0 {
		authority, path = authority[:slash], authority[slash:]
	}
	if at := strings.LastIndexByte(authority, '@'); at >= 0 {
		if !validUserinfo(authority[:at]) {
			return false
		}
		authority = authority[at+1:]
	}
	if authority == "" || !validHost(authority) {
		return false
	}
	return path == "" || validPercentEscapes(path)
}

// cutHTTPScheme отделяет схему, если s начинается с http: или https: в любом
// регистре, и возвращает остаток. Регистр значим: url.Parse нормализует схему,
// поэтому HTTP://, HtTpS:// и подобные варианты эквивалентны нижнему регистру.
func cutHTTPScheme(s string) (rest string, ok bool) {
	switch {
	case len(s) >= 5 && equalFoldLower(s[:4], "http") && s[4] == ':':
		return s[5:], true
	case len(s) >= 6 && equalFoldLower(s[:5], "https") && s[5] == ':':
		return s[6:], true
	}
	return "", false
}

// equalFoldLower сравнивает s с lower без учёта регистра ASCII-букв; lower
// обязан быть в нижнем регистре, длины равны по построению вызова.
func equalFoldLower(s, lower string) bool {
	for i := 0; i < len(s); i++ {
		c := s[i]
		if 'A' <= c && c <= 'Z' {
			c += 'a' - 'A'
		}
		if c != lower[i] {
			return false
		}
	}
	return true
}

// validPercentEscapes проверяет, что каждый % в s сопровождается двумя
// шестнадцатеричными цифрами; именно так net/url валидирует userinfo, путь,
// фрагмент и зону, не ограничивая остальные байты.
func validPercentEscapes(s string) bool {
	for i := 0; i < len(s); i++ {
		if s[i] != '%' {
			continue
		}
		if i+2 >= len(s) || !isHexByte(s[i+1]) || !isHexByte(s[i+2]) {
			return false
		}
		i += 2
	}
	return true
}

// isHexByte сообщает, является ли b ASCII-цифрой шестнадцатеричного разряда.
func isHexByte(b byte) bool {
	return '0' <= b && b <= '9' || 'a' <= b && b <= 'f' || 'A' <= b && b <= 'F'
}

// hexValue возвращает значение hex-разряда; вызывается только после isHexByte.
func hexValue(b byte) byte {
	switch {
	case '0' <= b && b <= '9':
		return b - '0'
	case 'a' <= b && b <= 'f':
		return b - 'a' + 10
	default:
		return b - 'A' + 10
	}
}

// validUserinfo повторяет net/url.validUserinfo: допустимы ASCII-буквы, цифры
// и символы -._:~!$&'()*+,;=%@, а каждый % обязан начинать корректный escape
// (иначе unescape username/password вернёт ошибку).
func validUserinfo(s string) bool {
	for i := 0; i < len(s); i++ {
		b := s[i]
		switch {
		case 'A' <= b && b <= 'Z', 'a' <= b && b <= 'z', '0' <= b && b <= '9':
		case b == '-', b == '.', b == '_', b == ':', b == '~', b == '!', b == '$',
			b == '&', b == '\'', b == '(', b == ')', b == '*', b == '+', b == ',',
			b == ';', b == '=', b == '%', b == '@':
		default:
			return false
		}
	}
	return validPercentEscapes(s)
}

// validHost повторяет net/url.parseHost для схем http/https в строгом режиме
// (urlstrictcolons=1 при go-директивой 1.26): host — IPv6-литерал в скобках либо
// имя не более чем с одним ':', за которым идут только цифры. Percent-escapes
// ограничены правилами unescape(_, encodeHost).
func validHost(host string) bool {
	if open := strings.LastIndexByte(host, '['); open > 0 {
		return false
	} else if open == 0 {
		return validBracketHost(host)
	}
	if colon := strings.IndexByte(host, ':'); colon >= 0 {
		if strings.LastIndexByte(host, ':') != colon {
			return false
		}
		if !validOptionalPort(host[colon:]) {
			return false
		}
	}
	return validHostEscapes(host)
}

// validBracketHost разбирает IPv6-литерал "[addr[:port]]" с необязательной
// зоной "%25...". Зона проверяется отдельно, потому что её escape-правила
// отличаются от правил адресной части.
func validBracketHost(host string) bool {
	close := strings.LastIndexByte(host, ']')
	if close < 0 {
		return false
	}
	if !validOptionalPort(host[close+1:]) {
		return false
	}
	inner := host[1:close]
	if zone := strings.Index(inner, "%25"); zone >= 0 {
		if len(inner) == zone+3 {
			return false // после unescape зона окажется пустой, netip её отклонит
		}
		return validIPv6(inner[:zone]) && validZone(inner[zone:])
	}
	if strings.IndexByte(inner, '%') >= 0 {
		// Вне зоны host-escape либо невалиден, либо декодируется в байт >= 0x80
		// или '%', который не может быть частью IPv6-адреса.
		return false
	}
	return validIPv6(inner)
}

// validZone проверяет unescape(zone, encodeZone): каждый %-escape корректен,
// escape, отличный от %25, декодируется только в пробел или в байт, допустимый
// в host без экранирования; литеральные байты >= 0x80 разрешены.
func validZone(s string) bool {
	for i := 0; i < len(s); i++ {
		b := s[i]
		if b == '%' {
			if i+2 >= len(s) || !isHexByte(s[i+1]) || !isHexByte(s[i+2]) {
				return false
			}
			if s[i:i+3] != "%25" {
				if v := hexValue(s[i+1])<<4 | hexValue(s[i+2]); v != ' ' && hostShouldEscape(v) {
					return false
				}
			}
			i += 2
			continue
		}
		if b < 0x80 && hostShouldEscape(b) {
			return false
		}
	}
	return true
}

// validHostEscapes проверяет unescape(host, encodeHost): все %-escape корректны,
// escape с первым hex-разрядом меньше 8 разрешён только как %25; литеральные
// байты >= 0x80 допустимы, ASCII-байты — по hostShouldEscape.
func validHostEscapes(s string) bool {
	for i := 0; i < len(s); i++ {
		b := s[i]
		if b == '%' {
			if i+2 >= len(s) || !isHexByte(s[i+1]) || !isHexByte(s[i+2]) {
				return false
			}
			if hexValue(s[i+1]) < 8 && s[i:i+3] != "%25" {
				return false
			}
			i += 2
			continue
		}
		if b < 0x80 && hostShouldEscape(b) {
			return false
		}
	}
	return true
}

// hostShouldEscape повторяет shouldEscape(b, encodeHost): требует ли байт
// %-экранирования в host/zone. В ASCII запрещены управляющие байты, пробел и
// символы # % / ? @ \ ^ ` { | }; байты >= 0x80 в encoding-таблице net/url
// отсутствуют, поэтому тоже «требуют экранирования» — unescape пропускает их
// только при литеральном чтении, где проверка ограничена s[i] < 0x80.
func hostShouldEscape(b byte) bool {
	if b >= 0x80 {
		return true
	}
	switch b {
	case ' ', '#', '%', '/', '?', '@', '\\', '^', '`', '{', '|', '}':
		return true
	}
	return b < 0x20 || b == 0x7f
}

// validOptionalPort повторяет net/url.validOptionalPort: порт — пустая строка
// или ':' с цифрами; диапазон порта net/url не проверяет.
func validOptionalPort(port string) bool {
	if port == "" {
		return true
	}
	if port[0] != ':' {
		return false
	}
	for i := 1; i < len(port); i++ {
		if port[i] < '0' || port[i] > '9' {
			return false
		}
	}
	return true
}

// validIPv6 сообщает, разбирается ли s как IPv6-литерал без зоны; эквивалент
// успешного netip.ParseAddr(s) с Is4() == false, но без аллокаций. Допускаются
// до 8 hex-групп, одна "::" и встроенный IPv4 вместо последних двух групп.
func validIPv6(s string) bool {
	i := 0
	haveEllipsis := false
	if len(s) > 0 && s[0] == ':' {
		if len(s) < 2 || s[1] != ':' {
			return false
		}
		haveEllipsis = true
		i = 2
	}
	groups := 0
	for i < len(s) {
		start := i
		for i < len(s) && isHexByte(s[i]) {
			i++
		}
		if i == start || i-start > 4 {
			return false
		}
		if i < len(s) && s[i] == '.' {
			// Встроенный IPv4 занимает последние две группы: без "::" он
			// возможен только на месте групп 7-8, с "::" — при неполном наборе.
			total := groups + 2
			if total > 8 {
				return false
			}
			if haveEllipsis && total == 8 {
				return false
			}
			if !haveEllipsis && total != 8 {
				return false
			}
			return validIPv4(s[start:])
		}
		groups++
		if i == len(s) {
			break
		}
		if s[i] != ':' {
			return false
		}
		i++
		if i == len(s) {
			return false // одиночное ':' в конце
		}
		if s[i] == ':' {
			if haveEllipsis {
				return false
			}
			haveEllipsis = true
			i++
		}
	}
	if haveEllipsis {
		return groups < 8
	}
	return groups == 8
}

// validIPv4 повторяет разбор встроенного IPv4-хвоста netip: ровно четыре
// десятичных октета 0..255 без ведущих нулей.
func validIPv4(s string) bool {
	octets := 0
	i := 0
	for i < len(s) {
		start := i
		value := 0
		for i < len(s) && '0' <= s[i] && s[i] <= '9' {
			if i > start && value == 0 {
				return false
			}
			value = value*10 + int(s[i]-'0')
			if value > 255 {
				return false
			}
			i++
		}
		if i == start {
			return false
		}
		octets++
		if octets == 4 {
			return i == len(s)
		}
		if i == len(s) || s[i] != '.' {
			return false
		}
		i++
	}
	return false
}

// GenerateCode возвращает криптографически случайный код длины CodeLength из base62.
// Используется rejection sampling: байты >= 248 отбрасываются, чтобы модульное
// смещение не искажало распределение.
func GenerateCode() (string, error) {
	code := make([]byte, CodeLength)
	buf := make([]byte, CodeLength)
	for i := 0; i < CodeLength; {
		if _, err := rand.Read(buf); err != nil {
			return "", fmt.Errorf("генерация короткого кода: %w", err)
		}
		for _, b := range buf {
			if b >= rejectionLimit {
				continue
			}
			code[i] = codeAlphabet[int(b)%len(codeAlphabet)]
			i++
			if i == CodeLength {
				break
			}
		}
	}
	return string(code), nil
}
