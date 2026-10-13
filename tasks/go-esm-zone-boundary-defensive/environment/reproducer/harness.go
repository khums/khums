// Package main implements the public reproducer. It builds and runs the
// candidate's own esmd server binary (built by run-all.sh from whatever
// source currently lives in /testbed) as a real subprocess, talks to it only
// over its public HTTP API (POST /transform, GET /+<hash>.<ext>, and package
// install/serve endpoints against a loopback-only mock npm registry this
// harness also starts), and inspects only the filesystem locations this
// harness itself controls (its own temp storage/work roots and a planted
// "victim" file outside them). It never imports any package from the
// candidate module and never reads candidate source directly.
//
// This binary only ever reports five aggregate family results (Cases A-E),
// each backed by one or two underlying checks. A more granular,
// verifier-only probe exists for grading but is not part of this public
// reproducer, its output, or this binary.
package main

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"encoding/json"
	"fmt"
	"io"
	"math/rand"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

const boundedTimeout = 8 * time.Second

type caseResult struct {
	id     string
	status string
	detail string
}

func freePort() (int, error) {
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return 0, err
	}
	defer l.Close()
	return l.Addr().(*net.TCPAddr).Port, nil
}

func waitReady(addr string, timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		c, err := net.DialTimeout("tcp", addr, 300*time.Millisecond)
		if err == nil {
			c.Close()
			return nil
		}
		time.Sleep(100 * time.Millisecond)
	}
	return fmt.Errorf("server did not become ready within %s", timeout)
}

func startServer(esmdBin, workRoot string, port int, npmRegistry string) (*exec.Cmd, string, error) {
	storageDir := filepath.Join(workRoot, "storage")
	workDir := filepath.Join(workRoot, "work")
	logDir := filepath.Join(workRoot, "log")
	for _, d := range []string{storageDir, workDir, logDir} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			return nil, "", err
		}
	}
	cfg := map[string]any{
		"port":    port,
		"workDir": workDir,
		"logDir":  logDir,
		"storage": map[string]string{"type": "fs", "endpoint": storageDir},
	}
	if npmRegistry != "" {
		cfg["npmRegistry"] = npmRegistry
	}
	cfgBytes, _ := json.Marshal(cfg)
	cfgPath := filepath.Join(workRoot, "config.json")
	if err := os.WriteFile(cfgPath, cfgBytes, 0o644); err != nil {
		return nil, "", err
	}
	cmd := exec.Command(esmdBin, "-config", cfgPath)
	cmd.Dir = workRoot
	cmd.Stdout = nil
	cmd.Stderr = nil
	if err := cmd.Start(); err != nil {
		return nil, "", err
	}
	addr := fmt.Sprintf("127.0.0.1:%d", port)
	if err := waitReady(addr, boundedTimeout); err != nil {
		_ = cmd.Process.Kill()
		return nil, "", err
	}
	return cmd, storageDir, nil
}

func postTransform(base, zoneID, code string) (status int, body []byte, err error) {
	reqBody, _ := json.Marshal(map[string]string{"code": code, "lang": "js"})
	req, err := http.NewRequest("POST", base+"/transform", bytes.NewReader(reqBody))
	if err != nil {
		return 0, nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	if zoneID != "" {
		req.Header.Set("X-Zone-Id", zoneID)
	}
	client := &http.Client{Timeout: boundedTimeout}
	resp, err := client.Do(req)
	if err != nil {
		return 0, nil, err
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, b, nil
}

func getTransform(base, zoneID, hash, ext string) (status int, body []byte, err error) {
	req, err := http.NewRequest("GET", fmt.Sprintf("%s/+%s.%s", base, hash, ext), nil)
	if err != nil {
		return 0, nil, err
	}
	if zoneID != "" {
		req.Header.Set("X-Zone-Id", zoneID)
	}
	client := &http.Client{Timeout: boundedTimeout}
	resp, err := client.Do(req)
	if err != nil {
		return 0, nil, err
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, b, nil
}

// findNewFile polls dir for any regular file to appear (any name), up to timeout.
func findNewFile(dir string, timeout time.Duration) (string, bool) {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		entries, err := os.ReadDir(dir)
		if err == nil {
			for _, e := range entries {
				if !e.IsDir() {
					return filepath.Join(dir, e.Name()), true
				}
			}
		}
		time.Sleep(150 * time.Millisecond)
	}
	return "", false
}

func traversalPrefix(depth int) string {
	return strings.Repeat("../", depth)
}

func randSuffix() string {
	rand.Seed(time.Now().UnixNano())
	return fmt.Sprintf("%d-%d", os.Getpid(), rand.Intn(1_000_000))
}

// Case A: X-Zone-Id carrying a crafted escape payload on the POST /transform
// (write) path must not result in any file created outside the storage root.
func caseA(base string) caseResult {
	suffix := randSuffix()
	escapeDir := filepath.Join(os.TempDir(), "esm-poc-a-"+suffix)
	target := filepath.Join(escapeDir, "modules", "transform")
	zoneID := traversalPrefix(15) + strings.TrimPrefix(escapeDir, string(os.PathSeparator))

	// Malicious input may be safely rejected outright (any non-200 status)
	// or safely neutralized while the request still succeeds; either is an
	// acceptable remediation. The only required property is that the
	// escape never happens.
	_, _, err := postTransform(base, zoneID, "export const pocA = "+suffix+";")
	if err != nil {
		return caseResult{"A", "ERROR", "request failed: " + err.Error()}
	}
	if _, found := findNewFile(target, 3*time.Second); found {
		return caseResult{"A", "FAIL", "required property violated"}
	}
	return caseResult{"A", "PASS", "required property held"}
}

// Case B: X-Zone-Id carrying a crafted escape payload on the GET
// /+<hash>.<ext> (read) path must not allow retrieval of a file planted
// outside the storage root.
func caseB(base string) caseResult {
	suffix := randSuffix()
	secretDir := filepath.Join(os.TempDir(), "esm-secret-b-"+suffix)
	secretSubdir := filepath.Join(secretDir, "modules", "transform")
	if err := os.MkdirAll(secretSubdir, 0o755); err != nil {
		return caseResult{"B", "ERROR", "could not prepare victim fixture: " + err.Error()}
	}
	hash := strings.Repeat("a", 40)
	marker := "SECRET-" + suffix
	if err := os.WriteFile(filepath.Join(secretSubdir, hash+".mjs"), []byte(marker), 0o644); err != nil {
		return caseResult{"B", "ERROR", "could not write victim fixture: " + err.Error()}
	}
	zoneID := traversalPrefix(15) + strings.TrimPrefix(secretDir, string(os.PathSeparator))

	status, body, err := getTransform(base, zoneID, hash, "mjs")
	if err != nil {
		return caseResult{"B", "ERROR", "request failed: " + err.Error()}
	}
	if status == 200 && strings.Contains(string(body), marker) {
		return caseResult{"B", "FAIL", "required property violated"}
	}
	return caseResult{"B", "PASS", "required property held"}
}

// Case C (safe control): a syntactically valid domain X-Zone-Id must still
// succeed and be stored under that zone's own prefix.
func caseC(base, storageDir string) caseResult {
	suffix := randSuffix()
	zoneID := "zone-" + suffix + ".example.com"
	status, _, err := postTransform(base, zoneID, "export const pocC = "+suffix+";")
	if err != nil {
		return caseResult{"C", "ERROR", "request failed: " + err.Error()}
	}
	if status != 200 {
		return caseResult{"C", "FAIL", "required property violated"}
	}
	target := filepath.Join(storageDir, zoneID, "modules", "transform")
	if _, found := findNewFile(target, 3*time.Second); !found {
		return caseResult{"C", "FAIL", "required property violated"}
	}
	return caseResult{"C", "PASS", "required property held"}
}

// Case D (safe control): a request with no X-Zone-Id header at all must
// still succeed under the unzoned prefix.
func caseD(base string) caseResult {
	suffix := randSuffix()
	status, _, err := postTransform(base, "", "export const pocD = "+suffix+";")
	if err != nil {
		return caseResult{"D", "ERROR", "request failed: " + err.Error()}
	}
	if status != 200 {
		return caseResult{"D", "FAIL", "required property violated"}
	}
	return caseResult{"D", "PASS", "required property held"}
}

// buildTarball builds a gzipped tar with the given entries (name -> content).
// Entry names are written exactly as given, including any "../" segments --
// callers control the tar layer directly to test entry-name handling.
func buildTarball(entries map[string]string) []byte {
	var buf bytes.Buffer
	gw := gzip.NewWriter(&buf)
	tw := tar.NewWriter(gw)
	for name, content := range entries {
		_ = tw.WriteHeader(&tar.Header{Name: name, Mode: 0o644, Size: int64(len(content)), Typeflag: tar.TypeReg})
		_, _ = tw.Write([]byte(content))
	}
	_ = tw.Close()
	_ = gw.Close()
	return buf.Bytes()
}

// startMockRegistry starts a loopback-only npm-registry-shaped HTTP server
// serving exactly the packages named in tarballs (package name -> tarball
// bytes). It never leaves the machine and needs no real network access.
func startMockRegistry(tarballs map[string][]byte) (baseURL string, err error) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return "", err
	}
	base := fmt.Sprintf("http://127.0.0.1:%d/", ln.Addr().(*net.TCPAddr).Port)
	mux := http.NewServeMux()
	for name, tb := range tarballs {
		name, tb := name, tb
		tarballURL := base + name + "/-/" + name + "-1.0.0.tgz"
		mux.HandleFunc("/"+name, func(w http.ResponseWriter, r *http.Request) {
			meta := map[string]any{
				"name":      name,
				"dist-tags": map[string]string{"latest": "1.0.0"},
				"versions": map[string]any{
					"1.0.0": map[string]any{
						"name": name, "version": "1.0.0",
						"dist": map[string]string{"tarball": tarballURL},
					},
				},
			}
			_ = json.NewEncoder(w).Encode(meta)
		})
		mux.HandleFunc("/"+name+"/-/"+name+"-1.0.0.tgz", func(w http.ResponseWriter, r *http.Request) {
			_, _ = w.Write(tb)
		})
	}
	go func() { _ = http.Serve(ln, mux) }()
	return base, nil
}

// installViaRawFetch requests a package's main entry through the ?raw path
// (installs/extracts the package via the mock registry without invoking the
// full esbuild bundling pipeline, which needs esm.sh's own build-time helper
// packages from the real registry and would not work fully offline).
func installViaRawFetch(base, pkgName string) (status int, body []byte, err error) {
	req, err := http.NewRequest("GET", fmt.Sprintf("%s/%s@1.0.0/index.js?raw", base, pkgName), nil)
	if err != nil {
		return 0, nil, err
	}
	client := &http.Client{Timeout: boundedTimeout}
	resp, err := client.Do(req)
	if err != nil {
		return 0, nil, err
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, b, nil
}

// Case G: a package tarball entry name carrying a crafted escape payload
// must not result in any file created outside the package's install
// directory when the package is installed. A hostile archive may be safely
// rejected outright (any non-200 status) or safely accepted with only the
// malicious entry skipped; either is an acceptable remediation. The only
// required property is that the escape never happens.
func caseG(base, escapeDir string) caseResult {
	_, _, err := installViaRawFetch(base, "pocg")
	if err != nil {
		return caseResult{"G", "ERROR", "request failed: " + err.Error()}
	}
	if _, found := findNewFile(escapeDir, 3*time.Second); found {
		return caseResult{"G", "FAIL", "required property violated"}
	}
	return caseResult{"G", "PASS", "required property held"}
}

// Case H (safe control): a well-formed package tarball must still install
// and be servable normally.
func caseH(base string) caseResult {
	status, body, err := installViaRawFetch(base, "poch")
	if err != nil {
		return caseResult{"H", "ERROR", "request failed: " + err.Error()}
	}
	if status != 200 || !strings.Contains(string(body), "ok") {
		return caseResult{"H", "FAIL", "required property violated"}
	}
	return caseResult{"H", "PASS", "required property held"}
}

// startCliServer starts the candidate's own root-module (CLI) binary in
// "serve" mode against appDir. This is a separate program from esmd, built
// from a different package (main, at the module root); flags must precede
// the positional app-dir argument for this CLI's own argument parser.
func startCliServer(esmcliBin, appDir string, port int) (*exec.Cmd, error) {
	cmd := exec.Command(esmcliBin, "serve", "--port", fmt.Sprintf("%d", port), appDir)
	cmd.Dir = appDir
	cmd.Stdout = nil
	cmd.Stderr = nil
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	addr := fmt.Sprintf("127.0.0.1:%d", port)
	if err := waitReady(addr, boundedTimeout); err != nil {
		_ = cmd.Process.Kill()
		return nil, err
	}
	return cmd, nil
}

// rawGet issues a GET request for exactly the given request-target string,
// via http.NewRequest (which does not collapse "../" dot-segments in the
// path, unlike some HTTP clients' default behavior).
func rawGet(base, requestPath string) (status int, body []byte, err error) {
	req, err := http.NewRequest("GET", base+requestPath, nil)
	if err != nil {
		return 0, nil, err
	}
	client := &http.Client{Timeout: boundedTimeout}
	resp, err := client.Do(req)
	if err != nil {
		return 0, nil, err
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, b, nil
}

// caseUnavailable reports ERROR for a case that needs the CLI binary when
// that binary failed to build (a separate program from esmd; not required
// for cases A-D, G, H).
func caseUnavailable(id string) caseResult {
	return caseResult{id, "ERROR", "candidate CLI binary (root module) did not build; case skipped"}
}

// Case I: a raw request path carrying a crafted escape payload against the
// CLI's static/app file server must not disclose file content located
// outside the configured app directory.
func caseI(esmcliBin string) caseResult {
	suffix := randSuffix()
	appDir := filepath.Join(os.TempDir(), "esm-poc-i-app-"+suffix)
	victimDir := filepath.Join(os.TempDir(), "esm-secret-i-"+suffix)
	if err := os.MkdirAll(appDir, 0o755); err != nil {
		return caseResult{"I", "ERROR", "could not prepare app fixture: " + err.Error()}
	}
	if err := os.MkdirAll(victimDir, 0o755); err != nil {
		return caseResult{"I", "ERROR", "could not prepare victim fixture: " + err.Error()}
	}
	if err := os.WriteFile(filepath.Join(appDir, "index.html"), []byte("<h1>ok</h1>"), 0o644); err != nil {
		return caseResult{"I", "ERROR", "could not write app fixture: " + err.Error()}
	}
	marker := "SECRET-" + suffix
	if err := os.WriteFile(filepath.Join(victimDir, "secret.txt"), []byte(marker), 0o644); err != nil {
		return caseResult{"I", "ERROR", "could not write victim fixture: " + err.Error()}
	}

	port, err := freePort()
	if err != nil {
		return caseResult{"I", "ERROR", "could not allocate a port: " + err.Error()}
	}
	cmd, err := startCliServer(esmcliBin, appDir, port)
	if err != nil {
		return caseResult{"I", "ERROR", "CLI server did not start: " + err.Error()}
	}
	defer func() {
		if cmd.Process != nil {
			_ = cmd.Process.Kill()
			_, _ = cmd.Process.Wait()
		}
	}()

	base := fmt.Sprintf("http://127.0.0.1:%d", port)
	depth := strings.Count(appDir, string(os.PathSeparator)) + 4
	requestPath := "/" + traversalPrefix(depth) + strings.TrimPrefix(victimDir, string(os.PathSeparator)) + "/secret.txt"
	status, body, err := rawGet(base, requestPath)
	if err != nil {
		return caseResult{"I", "ERROR", "request failed: " + err.Error()}
	}
	if status == 200 && strings.Contains(string(body), marker) {
		return caseResult{"I", "FAIL", "required property violated"}
	}
	return caseResult{"I", "PASS", "required property held"}
}

// Case J (safe control): a well-formed request for a file that genuinely
// exists inside the app directory must still be served normally.
func caseJ(esmcliBin string) caseResult {
	suffix := randSuffix()
	appDir := filepath.Join(os.TempDir(), "esm-poc-j-app-"+suffix)
	if err := os.MkdirAll(appDir, 0o755); err != nil {
		return caseResult{"J", "ERROR", "could not prepare app fixture: " + err.Error()}
	}
	marker := "OK-" + suffix
	if err := os.WriteFile(filepath.Join(appDir, "index.html"), []byte(marker), 0o644); err != nil {
		return caseResult{"J", "ERROR", "could not write app fixture: " + err.Error()}
	}

	port, err := freePort()
	if err != nil {
		return caseResult{"J", "ERROR", "could not allocate a port: " + err.Error()}
	}
	cmd, err := startCliServer(esmcliBin, appDir, port)
	if err != nil {
		return caseResult{"J", "ERROR", "CLI server did not start: " + err.Error()}
	}
	defer func() {
		if cmd.Process != nil {
			_ = cmd.Process.Kill()
			_, _ = cmd.Process.Wait()
		}
	}()

	base := fmt.Sprintf("http://127.0.0.1:%d", port)
	status, body, err := rawGet(base, "/")
	if err != nil {
		return caseResult{"J", "ERROR", "request failed: " + err.Error()}
	}
	if status != 200 || !strings.Contains(string(body), marker) {
		return caseResult{"J", "FAIL", "required property violated"}
	}
	return caseResult{"J", "PASS", "required property held"}
}

// launchToken is injected at image build time (the same value as the
// launcher's); without it the harness refuses to run, so it can only be
// started through /testbed/reproducer/run-all.sh.
var launchToken string

// run returns the exit code instead of calling os.Exit, so deferred cleanup
// (killing candidate servers, removing fixtures) always runs.
func run() int {
	if launchToken == "" || os.Getenv("ESM_REPRO_LAUNCH") != launchToken {
		fmt.Fprintln(os.Stderr, "this program is started by /testbed/reproducer/run-all.sh; run that instead")
		return 2
	}
	// Scrub the token before any candidate program is started, so the
	// candidate's own code never inherits it.
	os.Unsetenv("ESM_REPRO_LAUNCH")

	if len(os.Args) != 3 {
		fmt.Fprintln(os.Stderr, "usage: harness <esmd-binary-path> <esmcli-binary-path-or-empty>")
		return 2
	}
	esmdBin := os.Args[1]
	esmcliBin := os.Args[2]

	workRoot, err := os.MkdirTemp("", "esm-repro-")
	if err != nil {
		fmt.Println("ERROR\tcould not create work dir: " + err.Error())
		return 3
	}
	defer os.RemoveAll(workRoot)

	port, err := freePort()
	if err != nil {
		fmt.Println("ERROR\tcould not allocate a port: " + err.Error())
		return 3
	}

	// Case G/H fixtures: a mock npm registry (loopback only) must exist before
	// esmd starts, since esmd's npmRegistry is fixed at process startup.
	gSuffix := randSuffix()
	gEscapeDir := filepath.Join(os.TempDir(), "esm-poc-g-"+gSuffix)
	pocgTarball := buildTarball(map[string]string{
		"package/index.js":     "export const ok = 1;",
		"package/package.json": `{"name":"pocg","version":"1.0.0","main":"index.js"}`,
		"/" + traversalPrefix(15) + strings.TrimPrefix(gEscapeDir, string(os.PathSeparator)) + "/pwned.js": "export const pwned = true;",
	})
	pochTarball := buildTarball(map[string]string{
		"package/index.js":     "export const ok = true;",
		"package/package.json": `{"name":"poch","version":"1.0.0","main":"index.js"}`,
	})
	registryBase, err := startMockRegistry(map[string][]byte{"pocg": pocgTarball, "poch": pochTarball})
	if err != nil {
		fmt.Println("ERROR\tmock registry did not start: " + err.Error())
		return 3
	}

	cmd, storageDir, err := startServer(esmdBin, workRoot, port, registryBase)
	if err != nil {
		fmt.Println("ERROR\tserver did not start: " + err.Error())
		return 3
	}
	defer func() {
		if cmd.Process != nil {
			_ = cmd.Process.Kill()
			_, _ = cmd.Process.Wait()
		}
	}()

	base := fmt.Sprintf("http://127.0.0.1:%d", port)

	resA := caseA(base)
	resB := caseB(base)
	resC := caseC(base, storageDir)
	resD := caseD(base)
	resG := caseG(base, gEscapeDir)
	resH := caseH(base)

	var resI, resJ caseResult
	if esmcliBin != "" {
		resI = caseI(esmcliBin)
		resJ = caseJ(esmcliBin)
	} else {
		resI = caseUnavailable("I")
		resJ = caseUnavailable("J")
	}

	families := []struct {
		id      string
		results []caseResult
	}{
		{"A", []caseResult{resA, resH}},
		{"B", []caseResult{resG, resD}},
		{"C", []caseResult{resI, resC}},
		{"D", []caseResult{resB, resJ}},
		{"E", []caseResult{resA, resG, resI}},
	}

	overall := 0
	for _, fam := range families {
		status := "PASS"
		hasFail := false
		hasError := false
		passed := 0
		for _, r := range fam.results {
			if r.status == "PASS" {
				passed++
			} else if r.status == "FAIL" {
				hasFail = true
			} else if r.status == "ERROR" {
				hasError = true
			}
		}
		if hasFail {
			status = "FAIL"
		} else if hasError {
			status = "ERROR"
		}
		fmt.Printf("Case %s: %s (%d/%d)\n", fam.id, status, passed, len(fam.results))
		if status != "PASS" {
			overall = 1
		}
	}
	return overall
}

func main() {
	os.Exit(run())
}
