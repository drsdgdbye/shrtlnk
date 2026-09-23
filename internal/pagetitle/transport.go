package pagetitle

import (
	"context"
	"fmt"
	"net"
	"net/http"
	"net/netip"
	"syscall"
)

// blockedPrefixes — непубличные IPv4-диапазоны, которые не покрываются
// стандартными классификаторами netip: CGNAT, "этот хост", бенчмарки,
// зарезервированное пространство.
var blockedPrefixes = []netip.Prefix{
	netip.MustParsePrefix("100.64.0.0/10"),
	netip.MustParsePrefix("192.0.0.0/24"),
	netip.MustParsePrefix("198.18.0.0/15"),
	netip.MustParsePrefix("240.0.0.0/4"),
}

// newTransport создаёт транспорт с блокировкой непубличных адресов на уровне
// соединения (проверка каждого dial, включая редиректы), без прокси из
// окружения и со штатной проверкой TLS-сертификата.
func newTransport() *http.Transport {
	dialer := &net.Dialer{ControlContext: checkPublicAddr}
	return &http.Transport{
		DialContext: dialer.DialContext,
		Proxy:       nil,
	}
}

// checkPublicAddr — ControlContext dialer'а: получает фактический адрес
// соединения после DNS-разрешения и отклоняет непубличные IP.
func checkPublicAddr(_ context.Context, network, address string, _ syscall.RawConn) error {
	switch network {
	case "tcp", "tcp4", "tcp6":
	default:
		return fmt.Errorf("сеть %q не поддерживается", network)
	}
	host, _, err := net.SplitHostPort(address)
	if err != nil {
		return fmt.Errorf("разбор адреса %q: %w", address, err)
	}
	addr, err := netip.ParseAddr(host)
	if err != nil {
		return fmt.Errorf("разбор IP %q: %w", host, err)
	}
	if !isPublicAddr(addr) {
		return fmt.Errorf("адрес %q не является публичным", host)
	}
	return nil
}

// isPublicAddr сообщает, допустим ли адрес для исходящего соединения:
// loopback, private, link-local, multicast, unspecified, IPv4-mapped (после
// Unmap) и зарезервированные диапазоны считаются непубличными.
func isPublicAddr(addr netip.Addr) bool {
	addr = addr.Unmap()
	if !addr.IsValid() ||
		addr.IsLoopback() ||
		addr.IsPrivate() ||
		addr.IsLinkLocalUnicast() ||
		addr.IsLinkLocalMulticast() ||
		addr.IsMulticast() ||
		addr.IsUnspecified() {
		return false
	}
	if !addr.Is4() {
		return true
	}
	for _, prefix := range blockedPrefixes {
		if prefix.Contains(addr) {
			return false
		}
	}
	return true
}
