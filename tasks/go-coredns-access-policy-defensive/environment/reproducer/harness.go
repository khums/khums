package main

import (
	"bytes"
	"context"
	"fmt"
	"io/fs"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/miekg/dns"
)

const testbed = "/testbed"

func main() {
	if len(os.Args) != 2 {
		fmt.Fprintln(os.Stderr, "usage: harness <case-id>")
		os.Exit(2)
	}
	id := strings.ToUpper(os.Args[1])
	cases := map[string]func() (bool, string){
		"A": caseA, "B": caseB, "C": caseC, "D": caseD, "E": caseE,
		"F": caseF, "G": caseG, "H": caseH, "I": caseI,
	}
	run, ok := cases[id]
	if !ok {
		fmt.Printf("%s: ERROR: unknown case id\n", id)
		return
	}
	pass, errMsg := run()
	switch {
	case errMsg != "":
		fmt.Printf("%s: ERROR: %s\n", id, errMsg)
	case pass:
		fmt.Printf("%s: PASS\n", id)
	default:
		fmt.Printf("%s: FAIL\n", id)
	}
}

// caseA: a name rewrite must not move a query into a blocked zone unseen.
func caseA() (bool, string) {
	return expectRcode(`
. {
    rewrite name exact allowed.pub. sec.int.
    acl sec.int. {
        block
    }
    hosts {
        1.2.3.4 sec.int.
        fallthrough
    }
    errors
}
`, "127.0.0.1", "allowed.pub.", dns.TypeA, dns.RcodeRefused)
}

// caseB: a type rewrite must not turn an allowed type into a blocked one unseen.
func caseB() (bool, string) {
	return expectRcode(`
. {
    rewrite type A AAAA
    acl . {
        block type AAAA
    }
    hosts {
        ::1 query.example.
        fallthrough
    }
    errors
}
`, "127.0.0.1", "query.example.", dns.TypeA, dns.RcodeRefused)
}

// caseC: an answer cached for an allowed client must not be served to a
// client the policy blocks.
func caseC() (bool, string) {
	srv, err := startCoredns(`
. {
    acl shared.example. {
        allow net 127.0.0.2/32
        block
    }
    cache 30
    hosts {
        192.0.2.10 item.shared.example.
        fallthrough
    }
    errors
}
`)
	if err != nil {
		return false, err.Error()
	}
	defer srv.stop()
	warm, err := query(srv.addr, "127.0.0.2", "item.shared.example.", dns.TypeA)
	if err != nil {
		return false, err.Error()
	}
	if warm.Rcode != dns.RcodeSuccess {
		return false, ""
	}
	blocked, err := query(srv.addr, "127.0.0.1", "item.shared.example.", dns.TypeA)
	if err != nil {
		return false, err.Error()
	}
	return blocked.Rcode == dns.RcodeRefused, ""
}

// caseD: search-path expansion must not resolve a name inside a blocked zone.
func caseD() (bool, string) {
	dir, err := os.MkdirTemp("", "harness-resolv-")
	if err != nil {
		return false, err.Error()
	}
	defer os.RemoveAll(dir)
	resolv := filepath.Join(dir, "resolv.conf")
	if err := os.WriteFile(resolv, []byte("search open.example closed.example\n"), 0o644); err != nil {
		return false, err.Error()
	}
	return expectRcode(`
. {
    acl closed.example. {
        block
    }
    autopath `+resolv+`
    hosts {
        192.0.2.20 vault.closed.example.
    }
}
`, "127.0.0.1", "vault.open.example.", dns.TypeA, dns.RcodeRefused)
}

// caseE: generation is consistent: regenerating from the generator input in a
// full copy of the source tree reproduces the committed generated files.
func caseE() (bool, string) {
	tmp, err := os.MkdirTemp("", "harness-gen-")
	if err != nil {
		return false, "mkdtemp: " + err.Error()
	}
	defer os.RemoveAll(tmp)
	if err := copyTree(testbed, tmp); err != nil {
		return false, "copy source: " + err.Error()
	}
	generated := []string{"core/dnsserver/zdirectives.go", "core/plugin/zplugin.go"}
	for _, rel := range generated {
		// Regeneration must recreate these files inside this copy.
		os.Remove(filepath.Join(tmp, rel))
	}
	ctx, cancel := context.WithTimeout(context.Background(), 180*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, "go", "generate", "coredns.go")
	cmd.Dir = tmp
	cmd.Env = goEnv()
	if out, err := cmd.CombinedOutput(); err != nil {
		return false, "go generate failed: " + err.Error() + ": " + tail(out)
	}
	for _, rel := range generated {
		orig, err := os.ReadFile(filepath.Join(testbed, rel))
		if err != nil {
			return false, "read " + rel + ": " + err.Error()
		}
		regenerated, err := os.ReadFile(filepath.Join(tmp, rel))
		if err != nil || !bytes.Equal(orig, regenerated) {
			return false, ""
		}
	}
	return true, ""
}

// caseF: an allowed rewrite still answers.
func caseF() (bool, string) {
	return expectRcode(`
. {
    rewrite name exact from.example. to.example.
    hosts {
        1.2.3.4 to.example.
        fallthrough
    }
    errors
}
`, "127.0.0.1", "from.example.", dns.TypeA, dns.RcodeSuccess)
}

// caseG: an allowed client still receives cached answers.
func caseG() (bool, string) {
	srv, err := startCoredns(`
. {
    acl other.example. {
        block
    }
    cache 30
    hosts {
        1.2.3.4 cached.example.
        fallthrough
    }
    errors
}
`)
	if err != nil {
		return false, err.Error()
	}
	defer srv.stop()
	for i := 0; i < 2; i++ {
		r, err := query(srv.addr, "127.0.0.1", "cached.example.", dns.TypeA)
		if err != nil {
			return false, "query " + strconv.Itoa(i+1) + ": " + err.Error()
		}
		if r.Rcode != dns.RcodeSuccess || len(r.Answer) == 0 {
			return false, ""
		}
	}
	return true, ""
}

// caseH: an ordinary query with no policy still answers.
func caseH() (bool, string) {
	return expectRcode(`
. {
    hosts {
        1.2.3.4 normal.example.
        fallthrough
    }
    errors
}
`, "127.0.0.1", "normal.example.", dns.TypeA, dns.RcodeSuccess)
}

// caseI: the built server still provides every previously available plugin.
func caseI() (bool, string) {
	bin, cleanup, err := buildCoredns()
	if err != nil {
		return false, err.Error()
	}
	defer cleanup()
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, bin, "-plugins").Output()
	if err != nil {
		return false, "list plugins: " + err.Error()
	}
	for _, line := range strings.Split(string(out), "\n") {
		name := strings.TrimSpace(line)
		if name == "proxyproto" || strings.HasSuffix(name, ".proxyproto") {
			return true, ""
		}
	}
	return false, ""
}

// --- helpers ---

type server struct {
	addr string
	stop func()
}

func goEnv() []string {
	return append(os.Environ(), "CGO_ENABLED=0", "GOFLAGS=-mod=mod", "GOPROXY=off", "GOSUMDB=off")
}

func tail(out []byte) string {
	if len(out) > 1200 {
		out = out[len(out)-1200:]
	}
	return string(out)
}

// buildCoredns always compiles the current source; a pre-existing binary in
// the tree is never trusted, so results reflect the submitted source.
func buildCoredns() (string, func(), error) {
	dir, err := os.MkdirTemp("", "harness-bin-")
	if err != nil {
		return "", nil, err
	}
	cleanup := func() { os.RemoveAll(dir) }
	bin := filepath.Join(dir, "coredns")
	ctx, cancel := context.WithTimeout(context.Background(), 600*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, "go", "build", "-tags", "grpcnotrace", "-o", bin, ".")
	cmd.Dir = testbed
	cmd.Env = goEnv()
	if out, err := cmd.CombinedOutput(); err != nil {
		cleanup()
		return "", nil, fmt.Errorf("build coredns: %s: %s", err, tail(out))
	}
	return bin, cleanup, nil
}

func expectRcode(corefile, source, name string, qtype uint16, want int) (bool, string) {
	srv, err := startCoredns(corefile)
	if err != nil {
		return false, err.Error()
	}
	defer srv.stop()
	r, err := query(srv.addr, source, name, qtype)
	if err != nil {
		return false, err.Error()
	}
	return r.Rcode == want, ""
}

func startCoredns(corefile string) (*server, error) {
	bin, cleanup, err := buildCoredns()
	if err != nil {
		return nil, err
	}
	port, err := freePort()
	if err != nil {
		cleanup()
		return nil, err
	}
	dir, err := os.MkdirTemp("", "harness-coredns-")
	if err != nil {
		cleanup()
		return nil, err
	}
	cf := filepath.Join(dir, "Corefile")
	if err = os.WriteFile(cf, []byte(replaceZone(corefile, port)), 0o644); err != nil {
		os.RemoveAll(dir)
		cleanup()
		return nil, err
	}
	cmd := exec.Command(bin, "-conf", cf)
	cmd.Dir = dir
	if err = cmd.Start(); err != nil {
		os.RemoveAll(dir)
		cleanup()
		return nil, err
	}
	stop := func() {
		cmd.Process.Kill()
		cmd.Wait()
		os.RemoveAll(dir)
		cleanup()
	}
	addr := "127.0.0.1:" + strconv.Itoa(port)
	deadline := time.Now().Add(8 * time.Second)
	for time.Now().Before(deadline) {
		conn, cerr := net.DialTimeout("tcp", addr, 200*time.Millisecond)
		if cerr == nil {
			conn.Close()
			return &server{addr: addr, stop: stop}, nil
		}
		time.Sleep(100 * time.Millisecond)
	}
	stop()
	return nil, fmt.Errorf("coredns did not start on %s", addr)
}

func replaceZone(corefile string, port int) string {
	addr := ".:" + strconv.Itoa(port)
	lines := strings.Split(corefile, "\n")
	for i, l := range lines {
		if strings.TrimSpace(l) == ". {" {
			lines[i] = addr + " {\n    bind 127.0.0.1"
		}
	}
	return strings.Join(lines, "\n")
}

func query(addr, source, name string, qtype uint16) (*dns.Msg, error) {
	m := new(dns.Msg)
	m.SetQuestion(dns.Fqdn(name), qtype)
	m.RecursionDesired = false
	c := &dns.Client{Timeout: 3 * time.Second, Net: "udp",
		Dialer: &net.Dialer{Timeout: 3 * time.Second, LocalAddr: &net.UDPAddr{IP: net.ParseIP(source)}}}
	r, _, err := c.Exchange(m, addr)
	return r, err
}

func freePort() (int, error) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return 0, err
	}
	port := ln.Addr().(*net.TCPAddr).Port
	ln.Close()
	return port, nil
}

// copyTree copies the module tree needed by the generators: everything except
// the sealed reproducer, built binaries and VCS metadata. Unreadable entries
// are skipped rather than failing the copy.
func copyTree(src, dst string) error {
	return filepath.WalkDir(src, func(path string, d fs.DirEntry, err error) error {
		rel, _ := filepath.Rel(src, path)
		if err != nil {
			if d != nil && d.IsDir() {
				return filepath.SkipDir
			}
			return nil
		}
		if rel == "reproducer" || rel == ".git" || rel == "coredns" {
			if d.IsDir() {
				return filepath.SkipDir
			}
			return nil
		}
		target := filepath.Join(dst, rel)
		if d.IsDir() {
			return os.MkdirAll(target, 0o755)
		}
		if !d.Type().IsRegular() {
			return nil
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return nil
		}
		return os.WriteFile(target, data, 0o644)
	})
}
