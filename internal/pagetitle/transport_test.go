package pagetitle

import (
	"context"
	"net"
	"net/netip"
	"testing"
	"time"
)

func TestIsPublicAddr(t *testing.T) {
	tests := []struct {
		addr string
		want bool
	}{
		{addr: "8.8.8.8", want: true},
		{addr: "1.1.1.1", want: true},
		{addr: "2606:4700:4700::1111", want: true},
		{addr: "100.63.255.255", want: true},
		{addr: "100.128.0.0", want: true},
		{addr: "198.17.255.255", want: true},
		{addr: "127.0.0.1", want: false},
		{addr: "127.255.255.254", want: false},
		{addr: "10.0.0.1", want: false},
		{addr: "172.16.0.1", want: false},
		{addr: "192.168.1.1", want: false},
		{addr: "169.254.169.254", want: false},
		{addr: "100.64.0.1", want: false},
		{addr: "100.127.255.254", want: false},
		{addr: "192.0.0.1", want: false},
		{addr: "192.0.0.255", want: false},
		{addr: "198.18.0.1", want: false},
		{addr: "198.19.255.254", want: false},
		{addr: "240.0.0.1", want: false},
		{addr: "255.255.255.255", want: false},
		{addr: "224.0.0.1", want: false},
		{addr: "0.0.0.0", want: false},
		{addr: "::1", want: false},
		{addr: "::", want: false},
		{addr: "fe80::1", want: false},
		{addr: "fc00::1", want: false},
		{addr: "ff02::1", want: false},
		{addr: "::ffff:10.0.0.1", want: false},
		{addr: "::ffff:127.0.0.1", want: false},
	}

	for _, tt := range tests {
		t.Run(tt.addr, func(t *testing.T) {
			if got := isPublicAddr(netip.MustParseAddr(tt.addr)); got != tt.want {
				t.Errorf("isPublicAddr(%s) = %v, ожидалось %v", tt.addr, got, tt.want)
			}
		})
	}

	t.Run("невалидный адрес", func(t *testing.T) {
		if isPublicAddr(netip.Addr{}) {
			t.Error("isPublicAddr(нулевой адрес) = true, ожидалось false")
		}
	})
}

func TestTransportBlocksLoopback(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("net.Listen: %v", err)
	}
	defer func() {
		if err := listener.Close(); err != nil {
			t.Errorf("listener.Close: %v", err)
		}
	}()

	transport := newTransport()
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	conn, err := transport.DialContext(ctx, "tcp", listener.Addr().String())
	if err == nil {
		if cerr := conn.Close(); cerr != nil {
			t.Errorf("conn.Close: %v", cerr)
		}
		t.Fatal("dial к loopback не заблокирован транспортом с SSRF-гардом")
	}
}
