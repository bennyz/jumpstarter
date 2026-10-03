// Command reportstatus-holdproxy is a test-only gRPC proxy used by
// e2e/repro/report-status-fencing.sh.
//
// It sits between an exporter and the controller and forwards every RPC
// unchanged, except that it can hold one ReportStatus request until the
// exporter's deadline has passed and then deliver it on a context the exporter
// can no longer cancel. The controller then processes lease A's report after
// the exporter has retried, moved on, and reported for lease B: the ordering a
// slow, uncancelled ReportStatus handler produces. This models a transport
// assumption (delivery is not bounded by the caller's deadline); it does not
// alter controller behavior.
//
// Plain-HTTP control API on -admin, meant for curl:
//
//	POST /arm?status=AVAILABLE    hold the next report with that status
//	GET  /wait-held?timeout=60s   newest undecided hold: "id=N status=... lease=..."
//	GET  /wait-gone?id=N          block until the exporter gave up on hold N
//	POST /release?id=N            deliver hold N and print the controller's answer
//	POST /drop?id=N               discard hold N
//	GET  /log                     every ReportStatus seen and its outcome
package main

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"math/big"
	"net"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/keepalive"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protowire"
)

const (
	reportStatusMethod = "/jumpstarter.v1.ControllerService/ReportStatus"

	statusField       = 1
	releaseLeaseField = 3
	leaseNameField    = 4

	forwardTimeout = 30 * time.Second
	maxMsgSize     = 64 << 20
)

var statusNames = []string{
	"UNSPECIFIED", "OFFLINE", "AVAILABLE", "BEFORE_LEASE_HOOK", "LEASE_READY",
	"AFTER_LEASE_HOOK", "BEFORE_LEASE_HOOK_FAILED", "AFTER_LEASE_HOOK_FAILED",
}

func statusName(v int32) string {
	if v >= 0 && int(v) < len(statusNames) {
		return statusNames[v]
	}
	return strconv.Itoa(int(v))
}

func parseStatus(s string) (int32, bool) {
	s = strings.TrimPrefix(strings.ToUpper(s), "EXPORTER_STATUS_")
	for i, name := range statusNames {
		if name == s {
			return int32(i), true
		}
	}
	return 0, false
}

// frame carries a message as opaque bytes so RPCs are proxied without
// generated stubs.
type frame struct{ payload []byte }

type rawCodec struct{}

func (rawCodec) Marshal(v any) ([]byte, error) {
	f, ok := v.(*frame)
	if !ok {
		return nil, fmt.Errorf("holdproxy: cannot marshal %T", v)
	}
	return f.payload, nil
}

func (rawCodec) Unmarshal(data []byte, v any) error {
	f, ok := v.(*frame)
	if !ok {
		return fmt.Errorf("holdproxy: cannot unmarshal into %T", v)
	}
	f.payload = append([]byte(nil), data...)
	return nil
}

func (rawCodec) Name() string { return "proto" }

type reportInfo struct {
	status       int32
	leaseName    string
	hasLeaseName bool
	releaseLease bool
	stripped     bool
}

func (r reportInfo) String() string {
	lease := "<absent>"
	switch {
	case r.hasLeaseName && r.leaseName == "":
		lease = "<empty>"
	case r.hasLeaseName:
		lease = r.leaseName
	}
	if r.stripped {
		lease += "(stripped)"
	}
	return fmt.Sprintf("status=%s lease=%s release_lease=%t", statusName(r.status), lease, r.releaseLease)
}

// inspectReport decodes the ReportStatusRequest fields this proxy acts on and,
// when strip is set, drops lease_name so the controller sees a pre-fencing
// exporter.
func inspectReport(b []byte, strip bool) (reportInfo, []byte, error) {
	var info reportInfo
	out := make([]byte, 0, len(b))
	for len(b) > 0 {
		num, typ, n := protowire.ConsumeTag(b)
		if n < 0 {
			return info, nil, protowire.ParseError(n)
		}
		m := protowire.ConsumeFieldValue(num, typ, b[n:])
		if m < 0 {
			return info, nil, protowire.ParseError(m)
		}
		field, value := b[:n+m], b[n:n+m]
		b = b[n+m:]
		switch {
		case num == statusField && typ == protowire.VarintType:
			v, _ := protowire.ConsumeVarint(value)
			info.status = int32(v)
		case num == releaseLeaseField && typ == protowire.VarintType:
			v, _ := protowire.ConsumeVarint(value)
			info.releaseLease = v != 0
		case num == leaseNameField && typ == protowire.BytesType:
			v, _ := protowire.ConsumeBytes(value)
			info.leaseName, info.hasLeaseName = string(v), true
			if strip {
				info.stripped = true
				continue
			}
		}
		out = append(out, field...)
	}
	return info, out, nil
}

type hold struct {
	id       int
	info     reportInfo
	decided  bool
	gone     bool
	goneCode codes.Code
	done     bool
	result   string
	decision chan bool
	finished chan struct{}
}

type proxy struct {
	upstream *grpc.ClientConn
	strip    bool

	mu      sync.Mutex
	armed   *int32
	holds   map[int]*hold
	nextID  int
	history []string
	changed chan struct{}
}

func newProxy(upstream *grpc.ClientConn, strip bool) *proxy {
	return &proxy{
		upstream: upstream,
		strip:    strip,
		holds:    map[int]*hold{},
		changed:  make(chan struct{}),
	}
}

// recordLocked appends to the event log and wakes admin waiters.
func (p *proxy) recordLocked(format string, args ...any) {
	line := time.Now().Format("15:04:05.000") + " " + fmt.Sprintf(format, args...)
	p.history = append(p.history, line)
	log.Print(line)
	close(p.changed)
	p.changed = make(chan struct{})
}

func (p *proxy) handle(_ any, ss grpc.ServerStream) error {
	method, ok := grpc.MethodFromServerStream(ss)
	if !ok {
		return status.Error(codes.Internal, "holdproxy: missing method")
	}
	if method == reportStatusMethod {
		return p.reportStatus(ss)
	}
	return p.forward(ss, method)
}

func outgoingMD(ctx context.Context) metadata.MD {
	in, _ := metadata.FromIncomingContext(ctx)
	out := metadata.MD{}
	for k, v := range in {
		if strings.HasPrefix(k, ":") || strings.HasPrefix(k, "grpc-") {
			continue
		}
		switch k {
		case "content-type", "user-agent", "te":
			continue
		}
		out[k] = v
	}
	return out
}

func describe(err error) string {
	if err == nil {
		return "OK"
	}
	st := status.Convert(err)
	return fmt.Sprintf("%s: %s", st.Code(), st.Message())
}

func (p *proxy) reportStatus(ss grpc.ServerStream) error {
	req := &frame{}
	if err := ss.RecvMsg(req); err != nil {
		return err
	}
	info, payload, err := inspectReport(req.payload, p.strip)
	if err != nil {
		return status.Errorf(codes.InvalidArgument, "holdproxy: malformed ReportStatusRequest: %v", err)
	}
	req.payload = payload
	md := outgoingMD(ss.Context())

	if h := p.tryHold(info); h != nil {
		return p.deliverLate(ss, h, req, md)
	}

	var header, trailer metadata.MD
	resp := &frame{}
	ctx := metadata.NewOutgoingContext(ss.Context(), md)
	err = p.upstream.Invoke(ctx, reportStatusMethod, req, resp,
		grpc.ForceCodec(rawCodec{}), grpc.Header(&header), grpc.Trailer(&trailer))
	p.mu.Lock()
	p.recordLocked("pass %s -> %s", info, describe(err))
	p.mu.Unlock()
	ss.SetTrailer(trailer)
	if err != nil {
		return err
	}
	if err := ss.SetHeader(header); err != nil {
		return err
	}
	return ss.SendMsg(resp)
}

func (p *proxy) tryHold(info reportInfo) *hold {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.armed == nil || *p.armed != info.status {
		return nil
	}
	p.armed = nil
	p.nextID++
	h := &hold{
		id:       p.nextID,
		info:     info,
		decision: make(chan bool, 1),
		finished: make(chan struct{}),
	}
	p.holds[h.id] = h
	p.recordLocked("hold id=%d %s", h.id, info)
	return h
}

// deliverLate parks the request until the admin API decides, then sends it
// upstream detached from the exporter's (by then expired) deadline.
func (p *proxy) deliverLate(ss grpc.ServerStream, h *hold, req *frame, md metadata.MD) error {
	go func() {
		<-ss.Context().Done()
		p.mu.Lock()
		defer p.mu.Unlock()
		if h.done {
			return
		}
		h.gone, h.goneCode = true, status.FromContextError(ss.Context().Err()).Code()
		p.recordLocked("hold id=%d exporter gave up: %s", h.id, h.goneCode)
	}()

	if forward := <-h.decision; !forward {
		p.finish(h, "dropped")
		return status.Error(codes.Aborted, "holdproxy: dropped")
	}
	ctx, cancel := context.WithTimeout(metadata.NewOutgoingContext(context.Background(), md), forwardTimeout)
	defer cancel()
	resp := &frame{}
	err := p.upstream.Invoke(ctx, reportStatusMethod, req, resp, grpc.ForceCodec(rawCodec{}))
	p.finish(h, describe(err))
	if err != nil {
		return err
	}
	return ss.SendMsg(resp)
}

func (p *proxy) finish(h *hold, result string) {
	p.mu.Lock()
	h.result, h.done = result, true
	p.recordLocked("late id=%d %s -> %s", h.id, h.info, result)
	p.mu.Unlock()
	close(h.finished)
}

func (p *proxy) forward(ss grpc.ServerStream, method string) error {
	ctx, cancel := context.WithCancel(ss.Context())
	defer cancel()
	ctx = metadata.NewOutgoingContext(ctx, outgoingMD(ss.Context()))
	desc := &grpc.StreamDesc{ServerStreams: true, ClientStreams: true}
	cs, err := p.upstream.NewStream(ctx, desc, method, grpc.ForceCodec(rawCodec{}))
	if err != nil {
		return err
	}

	fromClient := make(chan error, 1)
	go func() {
		for {
			f := &frame{}
			if err := ss.RecvMsg(f); err != nil {
				if errors.Is(err, io.EOF) {
					fromClient <- cs.CloseSend()
				} else {
					fromClient <- err
				}
				return
			}
			if err := cs.SendMsg(f); err != nil {
				fromClient <- err
				return
			}
		}
	}()

	fromServer := make(chan error, 1)
	go func() {
		if md, err := cs.Header(); err == nil {
			if err := ss.SendHeader(md); err != nil {
				fromServer <- err
				return
			}
		}
		for {
			f := &frame{}
			if err := cs.RecvMsg(f); err != nil {
				fromServer <- err
				return
			}
			if err := ss.SendMsg(f); err != nil {
				fromServer <- err
				return
			}
		}
	}()

	clientDone := fromClient
	for {
		select {
		case err := <-clientDone:
			if err != nil && !errors.Is(err, io.EOF) {
				return err
			}
			clientDone = nil
		case err := <-fromServer:
			ss.SetTrailer(cs.Trailer())
			if errors.Is(err, io.EOF) {
				return nil
			}
			return err
		}
	}
}

func reply(w http.ResponseWriter, msg string) {
	_, _ = fmt.Fprintln(w, msg)
}

// waitFor blocks until cond, evaluated with p.mu held, reports done.
func (p *proxy) waitFor(r *http.Request, cond func() (string, bool)) (string, bool) {
	timeout := 2 * time.Minute
	if v := r.URL.Query().Get("timeout"); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			timeout = d
		}
	}
	timer := time.NewTimer(timeout)
	defer timer.Stop()
	for {
		p.mu.Lock()
		msg, done := cond()
		changed := p.changed
		p.mu.Unlock()
		if done {
			return msg, true
		}
		select {
		case <-changed:
		case <-timer.C:
			return "", false
		case <-r.Context().Done():
			return "", false
		}
	}
}

func (p *proxy) lookup(w http.ResponseWriter, r *http.Request) (*hold, bool) {
	id, err := strconv.Atoi(r.URL.Query().Get("id"))
	if err != nil {
		http.Error(w, "id is required", http.StatusBadRequest)
		return nil, false
	}
	p.mu.Lock()
	h, ok := p.holds[id]
	p.mu.Unlock()
	if !ok {
		http.Error(w, "unknown hold", http.StatusNotFound)
	}
	return h, ok
}

func (p *proxy) handleArm(w http.ResponseWriter, r *http.Request) {
	st, ok := parseStatus(r.URL.Query().Get("status"))
	if !ok {
		http.Error(w, "unknown status", http.StatusBadRequest)
		return
	}
	p.mu.Lock()
	p.armed = &st
	p.recordLocked("armed status=%s", statusName(st))
	p.mu.Unlock()
	reply(w, "armed "+statusName(st))
}

func (p *proxy) handleWaitHeld(w http.ResponseWriter, r *http.Request) {
	msg, ok := p.waitFor(r, func() (string, bool) {
		for id := p.nextID; id > 0; id-- {
			if h := p.holds[id]; !h.decided {
				return fmt.Sprintf("id=%d %s", h.id, h.info), true
			}
		}
		return "", false
	})
	if !ok {
		http.Error(w, "no report held", http.StatusRequestTimeout)
		return
	}
	reply(w, msg)
}

func (p *proxy) handleWaitGone(w http.ResponseWriter, r *http.Request) {
	h, ok := p.lookup(w, r)
	if !ok {
		return
	}
	msg, ok := p.waitFor(r, func() (string, bool) { return h.goneCode.String(), h.gone })
	if !ok {
		http.Error(w, "exporter still waiting", http.StatusRequestTimeout)
		return
	}
	reply(w, msg)
}

func (p *proxy) handleDecision(forward bool) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		h, ok := p.lookup(w, r)
		if !ok {
			return
		}
		p.mu.Lock()
		if h.decided {
			p.mu.Unlock()
			http.Error(w, "already decided", http.StatusConflict)
			return
		}
		h.decided = true
		p.mu.Unlock()
		h.decision <- forward
		select {
		case <-h.finished:
			reply(w, h.result)
		case <-time.After(forwardTimeout + 5*time.Second):
			http.Error(w, "timed out waiting for the controller", http.StatusGatewayTimeout)
		}
	}
}

func (p *proxy) handleLog(w http.ResponseWriter, _ *http.Request) {
	p.mu.Lock()
	lines := append([]string(nil), p.history...)
	p.mu.Unlock()
	reply(w, strings.Join(lines, "\n"))
}

func (p *proxy) adminMux() *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) { reply(w, "ok") })
	mux.HandleFunc("POST /arm", p.handleArm)
	mux.HandleFunc("GET /wait-held", p.handleWaitHeld)
	mux.HandleFunc("GET /wait-gone", p.handleWaitGone)
	mux.HandleFunc("POST /release", p.handleDecision(true))
	mux.HandleFunc("POST /drop", p.handleDecision(false))
	mux.HandleFunc("GET /log", p.handleLog)
	return mux
}

// serverTLS issues a throwaway CA and a loopback leaf, and writes the CA so the
// exporter can trust the proxy without disabling verification.
func serverTLS(caOut string) (*tls.Config, error) {
	now := time.Now()
	caKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, err
	}
	caTmpl := &x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: "reportstatus-holdproxy CA"},
		NotBefore:             now.Add(-time.Hour),
		NotAfter:              now.Add(7 * 24 * time.Hour),
		IsCA:                  true,
		BasicConstraintsValid: true,
		KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature,
	}
	caDER, err := x509.CreateCertificate(rand.Reader, caTmpl, caTmpl, &caKey.PublicKey, caKey)
	if err != nil {
		return nil, err
	}
	caCert, err := x509.ParseCertificate(caDER)
	if err != nil {
		return nil, err
	}
	leafKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, err
	}
	leafTmpl := &x509.Certificate{
		SerialNumber: big.NewInt(2),
		Subject:      pkix.Name{CommonName: "reportstatus-holdproxy"},
		NotBefore:    now.Add(-time.Hour),
		NotAfter:     now.Add(7 * 24 * time.Hour),
		KeyUsage:     x509.KeyUsageDigitalSignature,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
		DNSNames:     []string{"localhost"},
		IPAddresses:  []net.IP{net.IPv4(127, 0, 0, 1), net.IPv6loopback},
	}
	leafDER, err := x509.CreateCertificate(rand.Reader, leafTmpl, caCert, &leafKey.PublicKey, caKey)
	if err != nil {
		return nil, err
	}
	if caOut != "" {
		caPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: caDER})
		if err := os.WriteFile(caOut, caPEM, 0o644); err != nil {
			return nil, err
		}
	}
	return &tls.Config{
		Certificates: []tls.Certificate{{Certificate: [][]byte{leafDER, caDER}, PrivateKey: leafKey}},
		MinVersion:   tls.VersionTLS12,
	}, nil
}

func dialUpstream(target, caFile string, insecure bool) (*grpc.ClientConn, error) {
	host, _, err := net.SplitHostPort(target)
	if err != nil {
		return nil, err
	}
	cfg := &tls.Config{ServerName: host, MinVersion: tls.VersionTLS12, InsecureSkipVerify: insecure}
	if caFile != "" {
		caPEM, err := os.ReadFile(caFile)
		if err != nil {
			return nil, err
		}
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM(caPEM) {
			return nil, fmt.Errorf("no certificates in %s", caFile)
		}
		cfg.RootCAs = pool
	}
	return grpc.NewClient(target,
		grpc.WithTransportCredentials(credentials.NewTLS(cfg)),
		grpc.WithDefaultCallOptions(grpc.MaxCallRecvMsgSize(maxMsgSize)))
}

func main() {
	listen := flag.String("listen", "127.0.0.1:18082", "exporter-facing gRPC address")
	admin := flag.String("admin", "127.0.0.1:18090", "plain-HTTP control address")
	upstream := flag.String("upstream", "", "controller gRPC endpoint (host:port)")
	upstreamCA := flag.String("upstream-ca", "", "PEM bundle that verifies the controller")
	upstreamInsecure := flag.Bool("upstream-insecure", false, "skip verification of the controller certificate")
	caOut := flag.String("ca-out", "", "write the proxy CA certificate to this file")
	strip := flag.Bool("strip-lease-name", false, "drop lease_name from every ReportStatus (pre-fencing exporter)")
	flag.Parse()
	if *upstream == "" {
		log.Fatal("-upstream is required")
	}

	conn, err := dialUpstream(*upstream, *upstreamCA, *upstreamInsecure)
	if err != nil {
		log.Fatalf("upstream: %v", err)
	}
	tlsCfg, err := serverTLS(*caOut)
	if err != nil {
		log.Fatalf("tls: %v", err)
	}
	p := newProxy(conn, *strip)
	srv := grpc.NewServer(
		grpc.Creds(credentials.NewTLS(tlsCfg)),
		grpc.ForceServerCodec(rawCodec{}),
		grpc.UnknownServiceHandler(p.handle),
		grpc.MaxRecvMsgSize(maxMsgSize),
		// Exporters ping every 20s; the default policy would GOAWAY them.
		grpc.KeepaliveEnforcementPolicy(keepalive.EnforcementPolicy{MinTime: 5 * time.Second, PermitWithoutStream: true}),
	)

	lis, err := net.Listen("tcp", *listen)
	if err != nil {
		log.Fatal(err)
	}
	adminLis, err := net.Listen("tcp", *admin)
	if err != nil {
		log.Fatal(err)
	}
	adminSrv := &http.Server{Handler: p.adminMux(), ReadHeaderTimeout: 10 * time.Second}
	go func() { log.Fatal(adminSrv.Serve(adminLis)) }()

	log.Printf("proxying %s -> %s (admin %s, strip-lease-name=%t)", *listen, *upstream, *admin, *strip)
	if err := srv.Serve(lis); err != nil {
		log.Fatal(err)
	}
}
