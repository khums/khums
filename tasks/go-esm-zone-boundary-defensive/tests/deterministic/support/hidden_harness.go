// Package main implements the hidden, verifier-only granular probe. It lives
// under tests/, which is never mounted into the candidate's own working
// session (only the public reproducer at /testbed/reproducer/ is). It is
// built and run only at grading time, directly from this source, against
// whatever esmd/esmcli binaries the grading harness builds from /testbed.
// It talks to the candidate's binaries only over their public HTTP API and
// inspects only filesystem locations it itself controls, exactly like the
// public reproducer -- the only difference from the public reproducer is
// that every individual case's PASS/FAIL/ERROR is reported here, instead of
// being folded into the public reproducer's five aggregate family lines.
//
// Privilege model: when started as root (the grading container), this
// harness stays root only to orchestrate -- create fixtures, run the mock
// registry, inspect the filesystem. Every candidate-derived binary (esmd,
// esmcli) is launched under the unprivileged candidate identity
// (ESM_CANDIDATE_USER, default "agent") with a minimal environment, so
// submitted code never runs with verifier credentials or secrets. Fixtures
// live under a root-owned sticky base in /tmp so the candidate can create its
// own entries (required for escape attempts to be observable) but cannot
// rename, replace, or plant inside anything the harness itself created.
package main

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	crand "crypto/rand"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"net"
	"net/http"
	"os"
	"os/exec"
	"os/user"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
)

const boundedTimeout = 8 * time.Second

// tempBase is the root-owned, sticky (01777) directory under which every
// fixture and escape target for this run lives. Set in main.
var tempBase string

// candidateCred is the unprivileged identity candidate binaries run as; nil
// when the harness itself is not root (it is then already unprivileged).
var candidateCred *syscall.Credential
var candidateHome string

func initCandidateIdentity() error {
	if os.Geteuid() != 0 {
		candidateHome = os.Getenv("HOME")
		return nil
	}
	name := os.Getenv("ESM_CANDIDATE_USER")
	if name == "" {
		name = "agent"
	}
	u, err := user.Lookup(name)
	if err != nil {
		return fmt.Errorf("candidate user %q not found: %w", name, err)
	}
	uid, err := strconv.Atoi(u.Uid)
	if err != nil {
		return err
	}
	gid, err := strconv.Atoi(u.Gid)
	if err != nil {
		return err
	}
	if uid == 0 || gid == 0 {
		return fmt.Errorf("candidate user %q is privileged; refusing to run candidate binaries as root", name)
	}
	candidateCred = &syscall.Credential{Uid: uint32(uid), Gid: uint32(gid), Groups: []uint32{}}
	candidateHome = u.HomeDir
	return nil
}

// candidateCommand builds an exec.Cmd for a candidate-derived binary: it runs
// as the candidate identity with a minimal environment (no verifier secrets).
func candidateCommand(bin string, args ...string) *exec.Cmd {
	cmd := exec.Command(bin, args...)
	cmd.Env = []string{
		"PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
		"HOME=" + candidateHome,
		"TMPDIR=" + tempBase,
		"LANG=C.UTF-8",
	}
	// Own process group, so killCandidate also reaps anything it spawned.
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if candidateCred != nil {
		cmd.SysProcAttr.Credential = candidateCred
	}
	return cmd
}

func killCandidate(cmd *exec.Cmd) {
	if cmd.Process == nil {
		return
	}
	_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
	_ = cmd.Process.Kill()
}

// giveToCandidate hands a harness-created tree to the candidate identity.
// Called only after the harness has finished writing into it; the harness
// never writes through candidate-owned paths afterwards.
func giveToCandidate(root string) error {
	if candidateCred == nil {
		return nil
	}
	uid, gid := int(candidateCred.Uid), int(candidateCred.Gid)
	return filepath.WalkDir(root, func(p string, _ fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		return os.Lchown(p, uid, gid)
	})
}

// writeFixtureExcl creates a new fixture file without following a symlink or
// reusing an existing entry, for the one fixture (H14) written into a
// candidate-owned directory.
func writeFixtureExcl(path string, data []byte) error {
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL|syscall.O_NOFOLLOW, 0o644)
	if err != nil {
		return err
	}
	if _, err := f.Write(data); err != nil {
		f.Close()
		return err
	}
	return f.Close()
}

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
		if err := giveToCandidate(d); err != nil {
			return nil, "", err
		}
	}
	// H14's out-of-root target (storage/..). The harness-owned parent keeps
	// the path stable; the candidate-owned leaf keeps a vulnerable write
	// observable exactly as in an unprivileged run.
	outside := filepath.Join(workRoot, "modules", "transform")
	if err := os.MkdirAll(outside, 0o755); err != nil {
		return nil, "", err
	}
	if err := giveToCandidate(outside); err != nil {
		return nil, "", err
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
	cmd := candidateCommand(esmdBin, "-config", cfgPath)
	cmd.Dir = workRoot
	cmd.Stdout = nil
	cmd.Stderr = nil
	if err := cmd.Start(); err != nil {
		return nil, "", err
	}
	addr := fmt.Sprintf("127.0.0.1:%d", port)
	if err := waitReady(addr, boundedTimeout); err != nil {
		killCandidate(cmd)
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

// listFileNames returns the names of regular files currently in dir (empty if
// dir does not exist yet), for differential checks against a shared directory.
func listFileNames(dir string) map[string]bool {
	names := map[string]bool{}
	if entries, err := os.ReadDir(dir); err == nil {
		for _, e := range entries {
			if !e.IsDir() {
				names[e.Name()] = true
			}
		}
	}
	return names
}

// findFileNotIn polls dir until a regular file appears whose name is not in
// before. Unlike findNewFile, it ignores files that already existed, so it is
// safe on directories shared with earlier cases in the same server run.
func findFileNotIn(dir string, before map[string]bool, timeout time.Duration) (string, bool) {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if entries, err := os.ReadDir(dir); err == nil {
			for _, e := range entries {
				if !e.IsDir() && !before[e.Name()] {
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

// randSuffix is unpredictable so a candidate process cannot pre-create a
// fixture path before the harness does.
func randSuffix() string {
	var b [8]byte
	if _, err := crand.Read(b[:]); err != nil {
		panic(err)
	}
	return fmt.Sprintf("%d-%d", os.Getpid(), binary.LittleEndian.Uint64(b[:])%1_000_000_000)
}

// Case A: X-Zone-Id carrying a crafted escape payload on the POST /transform
// (write) path must not result in any file created outside the storage root.
func caseA(base string) caseResult {
	suffix := randSuffix()
	escapeDir := filepath.Join(tempBase, "esm-poc-a-"+suffix)
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
	secretDir := filepath.Join(tempBase, "esm-secret-b-"+suffix)
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

// Case E (H01, safe control): a non-domain, non-traversal garbage X-Zone-Id
// is malformed input. instruction.md permits either safely rejecting it
// outright (a clean 4xx client-error response) or safely handling it (200,
// whether treated as absent or kept in its own confined namespace). Both are
// acceptable; what is NOT acceptable is a hard server error (5xx), which would
// indicate the malformed input was not handled at all.
func caseE(base string) caseResult {
	suffix := randSuffix()
	status, _, err := postTransform(base, "not a domain!! "+suffix, "export const pocE = "+suffix+";")
	if err != nil {
		return caseResult{"H01", "ERROR", "request failed: " + err.Error()}
	}
	if status >= 500 {
		return caseResult{"H01", "FAIL", fmt.Sprintf("malformed zone identifier caused a server error (status %d)", status)}
	}
	return caseResult{"H01", "PASS", "required property held"}
}

// Case F (regression control): a crafted escape payload prefixed with an
// innocuous domain-like segment must still be blocked - guards against a
// remediation that only rejects a header starting with "..".
func caseF(base string) caseResult {
	suffix := randSuffix()
	escapeDir := filepath.Join(tempBase, "esm-poc-f-"+suffix)
	target := filepath.Join(escapeDir, "modules", "transform")
	zoneID := "evilzone.example.com/" + traversalPrefix(15) + strings.TrimPrefix(escapeDir, string(os.PathSeparator))

	_, _, err := postTransform(base, zoneID, "export const pocF = "+suffix+";")
	if err != nil {
		return caseResult{"H02", "ERROR", "request failed: " + err.Error()}
	}
	if _, found := findNewFile(target, 3*time.Second); found {
		return caseResult{"H02", "FAIL", "required property violated"}
	}
	return caseResult{"H02", "PASS", "required property held"}
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
	if err := giveToCandidate(appDir); err != nil {
		return nil, err
	}
	cmd := candidateCommand(esmcliBin, "serve", "--port", fmt.Sprintf("%d", port), appDir)
	cmd.Dir = appDir
	cmd.Stdout = nil
	cmd.Stderr = nil
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	addr := fmt.Sprintf("127.0.0.1:%d", port)
	if err := waitReady(addr, boundedTimeout); err != nil {
		killCandidate(cmd)
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
// for cases A-H).
func caseUnavailable(id string) caseResult {
	return caseResult{id, "ERROR", "candidate CLI binary (root module) did not build; case skipped"}
}

// Scope boundary for the web-serving cases (I, J, H06-H09, H12, H13): the
// trust boundary under test is the client-supplied request path. Symlinks
// inside the configured app directory are operator-controlled content and are
// deliberately out of scope -- the client has no in-task way to create one
// (static serving is read-only, tar extraction skips non-regular entries, and
// zone storage writes regular files), upstream follows them by design, and
// legitimate setups depend on it (e.g. pnpm/npm-linked workspaces). No case
// plants a symlink, so a remediation that also refuses out-of-tree symlinks is
// neither required nor penalized.

// Case I: a raw request path carrying a crafted escape payload against the
// CLI's static/app file server must not disclose file content located
// outside the configured app directory.
func caseI(esmcliBin string) caseResult {
	suffix := randSuffix()
	appDir := filepath.Join(tempBase, "esm-poc-i-app-"+suffix)
	victimDir := filepath.Join(tempBase, "esm-secret-i-"+suffix)
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
			killCandidate(cmd)
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
	appDir := filepath.Join(tempBase, "esm-poc-j-app-"+suffix)
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
			killCandidate(cmd)
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

// Case K (regression control): a crafted escape payload prefixed with an
// innocuous same-app-looking segment must still be blocked - guards against
// a remediation that only rejects a request path starting with "..".
func caseK(esmcliBin string) caseResult {
	suffix := randSuffix()
	appDir := filepath.Join(tempBase, "esm-poc-k-app-"+suffix)
	victimDir := filepath.Join(tempBase, "esm-secret-k-"+suffix)
	if err := os.MkdirAll(appDir, 0o755); err != nil {
		return caseResult{"H06", "ERROR", "could not prepare app fixture: " + err.Error()}
	}
	if err := os.MkdirAll(victimDir, 0o755); err != nil {
		return caseResult{"H06", "ERROR", "could not prepare victim fixture: " + err.Error()}
	}
	if err := os.WriteFile(filepath.Join(appDir, "index.html"), []byte("<h1>ok</h1>"), 0o644); err != nil {
		return caseResult{"H06", "ERROR", "could not write app fixture: " + err.Error()}
	}
	marker := "SECRET-" + suffix
	if err := os.WriteFile(filepath.Join(victimDir, "secret.txt"), []byte(marker), 0o644); err != nil {
		return caseResult{"H06", "ERROR", "could not write victim fixture: " + err.Error()}
	}

	port, err := freePort()
	if err != nil {
		return caseResult{"H06", "ERROR", "could not allocate a port: " + err.Error()}
	}
	cmd, err := startCliServer(esmcliBin, appDir, port)
	if err != nil {
		return caseResult{"H06", "ERROR", "CLI server did not start: " + err.Error()}
	}
	defer func() {
		if cmd.Process != nil {
			killCandidate(cmd)
			_, _ = cmd.Process.Wait()
		}
	}()

	base := fmt.Sprintf("http://127.0.0.1:%d", port)
	depth := strings.Count(appDir, string(os.PathSeparator)) + 4
	requestPath := "/assets/" + traversalPrefix(depth) + strings.TrimPrefix(victimDir, string(os.PathSeparator)) + "/secret.txt"
	status, body, err := rawGet(base, requestPath)
	if err != nil {
		return caseResult{"H06", "ERROR", "request failed: " + err.Error()}
	}
	if status == 200 && strings.Contains(string(body), marker) {
		return caseResult{"H06", "FAIL", "required property violated"}
	}
	return caseResult{"H06", "PASS", "required property held"}
}

// Case L (safe control): a request path whose filename merely contains ".."
// as a substring (not a "../" path segment) must still be served normally -
// distinguishes a real confinement check from a coarse substring-match
// rejection of any path containing "..".
func caseL(esmcliBin string) caseResult {
	suffix := randSuffix()
	appDir := filepath.Join(tempBase, "esm-poc-l-app-"+suffix)
	subDir := filepath.Join(appDir, "version..1")
	if err := os.MkdirAll(subDir, 0o755); err != nil {
		return caseResult{"H07", "ERROR", "could not prepare app fixture: " + err.Error()}
	}
	marker := "OK-" + suffix
	if err := os.WriteFile(filepath.Join(appDir, "foo..bar.js"), []byte(marker), 0o644); err != nil {
		return caseResult{"H07", "ERROR", "could not write app fixture: " + err.Error()}
	}
	if err := os.WriteFile(filepath.Join(subDir, "file.js"), []byte(marker), 0o644); err != nil {
		return caseResult{"H07", "ERROR", "could not write app fixture: " + err.Error()}
	}

	port, err := freePort()
	if err != nil {
		return caseResult{"H07", "ERROR", "could not allocate a port: " + err.Error()}
	}
	cmd, err := startCliServer(esmcliBin, appDir, port)
	if err != nil {
		return caseResult{"H07", "ERROR", "CLI server did not start: " + err.Error()}
	}
	defer func() {
		if cmd.Process != nil {
			killCandidate(cmd)
			_, _ = cmd.Process.Wait()
		}
	}()

	base := fmt.Sprintf("http://127.0.0.1:%d", port)
	status1, body1, err := rawGet(base, "/foo..bar.js?raw")
	if err != nil {
		return caseResult{"H07", "ERROR", "request failed: " + err.Error()}
	}
	status2, body2, err := rawGet(base, "/version..1/file.js?raw")
	if err != nil {
		return caseResult{"H07", "ERROR", "request failed: " + err.Error()}
	}
	if status1 != 200 || !strings.Contains(string(body1), marker) ||
		status2 != 200 || !strings.Contains(string(body2), marker) {
		return caseResult{"H07", "FAIL", "required property violated"}
	}
	return caseResult{"H07", "PASS", "required property held"}
}

// Case M (safe control): a request path that lexically cancels itself out
// (a "../" segment immediately following a real subdirectory, net effect
// still inside the app directory) must still resolve and serve normally -
// distinguishes a real confinement check (which allows this) from a blanket
// rejection of any request path containing a "../" segment at all.
func caseM(esmcliBin string) caseResult {
	suffix := randSuffix()
	appDir := filepath.Join(tempBase, "esm-poc-m-app-"+suffix)
	subDir := filepath.Join(appDir, "dir")
	if err := os.MkdirAll(subDir, 0o755); err != nil {
		return caseResult{"H08", "ERROR", "could not prepare app fixture: " + err.Error()}
	}
	marker := "OK-" + suffix
	if err := os.WriteFile(filepath.Join(appDir, "index.html"), []byte(marker), 0o644); err != nil {
		return caseResult{"H08", "ERROR", "could not write app fixture: " + err.Error()}
	}

	port, err := freePort()
	if err != nil {
		return caseResult{"H08", "ERROR", "could not allocate a port: " + err.Error()}
	}
	cmd, err := startCliServer(esmcliBin, appDir, port)
	if err != nil {
		return caseResult{"H08", "ERROR", "CLI server did not start: " + err.Error()}
	}
	defer func() {
		if cmd.Process != nil {
			killCandidate(cmd)
			_, _ = cmd.Process.Wait()
		}
	}()

	base := fmt.Sprintf("http://127.0.0.1:%d", port)
	status, body, err := rawGet(base, "/dir/../index.html")
	if err != nil {
		return caseResult{"H08", "ERROR", "request failed: " + err.Error()}
	}
	if status != 200 || !strings.Contains(string(body), marker) {
		return caseResult{"H08", "FAIL", "required property violated"}
	}
	return caseResult{"H08", "PASS", "required property held"}
}

// Case N (isolation control): an artifact created under one valid zone must
// not be retrievable using a different valid zone, nor without any zone at
// all - confirms zone isolation, not just that traversal is blocked. Also
// confirms a garbage (non-domain) zone stores under the unzoned prefix, not
// under a directory literally named after the garbage value.
func caseN(base, storageDir string) caseResult {
	suffix := randSuffix()
	zoneA := "zone-a-" + suffix + ".example.com"
	zoneB := "zone-b-" + suffix + ".example.com"
	marker := "ZONE-A-SECRET-" + suffix

	status, _, err := postTransform(base, zoneA, "export const pocN = \""+marker+"\";")
	if err != nil {
		return caseResult{"H03", "ERROR", "request failed: " + err.Error()}
	}
	if status != 200 {
		return caseResult{"H03", "ERROR", fmt.Sprintf("unexpected status %d from well-formed transform request", status)}
	}

	target := filepath.Join(storageDir, zoneA, "modules", "transform")
	created, found := findNewFile(target, 3*time.Second)
	if !found {
		return caseResult{"H03", "FAIL", "a valid zone's artifact was not stored under that zone's own namespace"}
	}
	name := filepath.Base(created)
	ext := strings.TrimPrefix(filepath.Ext(name), ".")
	hash := strings.TrimSuffix(name, filepath.Ext(name))

	statusB, bodyB, err := getTransform(base, zoneB, hash, ext)
	if err != nil {
		return caseResult{"H03", "ERROR", "cross-zone request failed: " + err.Error()}
	}
	if statusB == 200 && strings.Contains(string(bodyB), marker) {
		return caseResult{"H03", "FAIL", "artifact stored under one valid zone was served to a different valid zone"}
	}

	statusU, bodyU, err := getTransform(base, "", hash, ext)
	if err != nil {
		return caseResult{"H03", "ERROR", "unzoned request failed: " + err.Error()}
	}
	if statusU == 200 && strings.Contains(string(bodyU), marker) {
		return caseResult{"H03", "FAIL", "artifact stored under a valid zone was served to a request carrying no zone"}
	}

	// How a harmless malformed (non-domain) zone is handled -- rejected,
	// treated as absent, or stored in its own confined namespace -- is not
	// constrained here: the task permits any safe handling. Escapes are
	// covered by A/H02/H14 and server errors by H01.
	return caseResult{"H03", "PASS", "cross-zone isolation held"}
}

// Case O (mixed-tarball control): a tarball containing both a malicious
// escaping entry and legitimate sibling entries. Two remediations are both
// acceptable: (a) per-entry rejection -- the malicious entry is skipped and
// the legitimate siblings still extract and serve correctly, or (b) an
// all-or-nothing rejection of the whole archive. What is NOT acceptable is
// accepting the archive (200) while failing to serve the legitimate
// sibling content -- that would mean the malicious entry corrupted or
// blocked unrelated, legitimate package content. Reuses Case G's own
// package (which already contains one malicious entry alongside legitimate
// ones); Case G separately confirms the escape itself never happens.
func caseO(base string) caseResult {
	status, body, err := installViaRawFetch(base, "pocg")
	if err != nil {
		return caseResult{"H04", "ERROR", "request failed: " + err.Error()}
	}
	if status == 200 && !strings.Contains(string(body), "ok") {
		return caseResult{"H04", "FAIL", "required property violated"}
	}
	return caseResult{"H04", "PASS", "required property held"}
}

// Case P (compatibility control): a tarball entry name that lexically
// cancels itself out (a "../" segment immediately following a real
// subdirectory) must still extract to its normalized, in-package location
// and be servable there - confirms confinement doesn't over-reject benign
// entries, distinct from Case G's pure-escape check.
func caseP(base string) caseResult {
	req, err := http.NewRequest("GET", base+"/pocp@1.0.0/lib/index.js?raw", nil)
	if err != nil {
		return caseResult{"H05", "ERROR", "request build failed: " + err.Error()}
	}
	client := &http.Client{Timeout: boundedTimeout}
	resp, err := client.Do(req)
	if err != nil {
		return caseResult{"H05", "ERROR", "request failed: " + err.Error()}
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != 200 || !strings.Contains(string(body), "ok") {
		return caseResult{"H05", "FAIL", "required property violated"}
	}
	return caseResult{"H05", "PASS", "required property held"}
}

// Case Q (safe control): a request path whose filename contains a literal
// backslash must still be served normally on this Linux task environment -
// backslash has no path-separator meaning here, so a remediation that
// blanket-rejects any backslash character in a request path is over-broad.
func caseQ(esmcliBin string) caseResult {
	suffix := randSuffix()
	appDir := filepath.Join(tempBase, "esm-poc-q-app-"+suffix)
	if err := os.MkdirAll(appDir, 0o755); err != nil {
		return caseResult{"H09", "ERROR", "could not prepare app fixture: " + err.Error()}
	}
	marker := "OK-" + suffix
	weirdName := "weird\\file-" + suffix + ".txt"
	if err := os.WriteFile(filepath.Join(appDir, weirdName), []byte(marker), 0o644); err != nil {
		return caseResult{"H09", "ERROR", "could not write app fixture: " + err.Error()}
	}

	port, err := freePort()
	if err != nil {
		return caseResult{"H09", "ERROR", "could not allocate a port: " + err.Error()}
	}
	cmd, err := startCliServer(esmcliBin, appDir, port)
	if err != nil {
		return caseResult{"H09", "ERROR", "CLI server did not start: " + err.Error()}
	}
	defer func() {
		if cmd.Process != nil {
			killCandidate(cmd)
			_, _ = cmd.Process.Wait()
		}
	}()

	base := fmt.Sprintf("http://127.0.0.1:%d", port)
	status, body, err := rawGet(base, "/"+weirdName)
	if err != nil {
		return caseResult{"H09", "ERROR", "request failed: " + err.Error()}
	}
	if status != 200 || !strings.Contains(string(body), marker) {
		return caseResult{"H09", "FAIL", "required property violated"}
	}
	return caseResult{"H09", "PASS", "required property held"}
}

var knownTempPrefixes = []string{"esm-poc-", "esm-secret-", "esm-repro-"}

func isKnownTempEntry(name string) bool {
	for _, p := range knownTempPrefixes {
		if strings.HasPrefix(name, p) {
			return true
		}
	}
	return false
}

// realTmpDir is the literal, conventional OS temp directory, deliberately
// not os.TempDir() (which honors $TMPDIR, set elsewhere in this environment
// to a per-agent directory). H10/H11 must catch a remediation that
// hardcodes a literal "/tmp/..." path for a debug/error artifact regardless
// of $TMPDIR, so they snapshot the literal path, not the redirected one.
const realTmpDir = "/tmp"

func snapshotTempDir() (map[string]bool, error) {
	entries, err := os.ReadDir(realTmpDir)
	if err != nil {
		return nil, err
	}
	m := make(map[string]bool, len(entries))
	for _, e := range entries {
		m[e.Name()] = true
	}
	return m, nil
}

func newUnknownTempEntries(before, after map[string]bool) []string {
	var strays []string
	for name := range after {
		if before[name] {
			continue
		}
		if isKnownTempEntry(name) {
			continue
		}
		strays = append(strays, name)
	}
	return strays
}

// Case H10: a rejected/blocked malicious package install must not leave any
// stray, unrelated artifact directly under the OS temp directory (e.g. a
// debug or error log written by an overbroad remediation) beyond this
// harness's own known fixture directories.
func caseH10(base string) caseResult {
	before, err := snapshotTempDir()
	if err != nil {
		return caseResult{"H10", "ERROR", "could not snapshot temp dir: " + err.Error()}
	}
	// A dedicated package name, not reused from any earlier case in this run,
	// so a first-install-only side effect cannot be masked by an install
	// cache hit from a prior case's install of the same package name. The
	// install may be safely accepted or safely rejected; either is fine --
	// only a stray side effect is checked here.
	_, _, err = installViaRawFetch(base, "poch10")
	if err != nil {
		return caseResult{"H10", "ERROR", "request failed: " + err.Error()}
	}
	time.Sleep(500 * time.Millisecond)
	after, err := snapshotTempDir()
	if err != nil {
		return caseResult{"H10", "ERROR", "could not re-snapshot temp dir: " + err.Error()}
	}
	if strays := newUnknownTempEntries(before, after); len(strays) > 0 {
		return caseResult{"H10", "FAIL", "required property violated"}
	}
	return caseResult{"H10", "PASS", "required property held"}
}

// Case H11: a blocked/rejected transform request must not leave any stray,
// unrelated artifact directly under the OS temp directory beyond this
// harness's own known fixture directories.
func caseH11(base string) caseResult {
	before, err := snapshotTempDir()
	if err != nil {
		return caseResult{"H11", "ERROR", "could not snapshot temp dir: " + err.Error()}
	}
	suffix := randSuffix()
	escapeDir := filepath.Join(tempBase, "esm-poc-h11-"+suffix)
	zoneID := traversalPrefix(15) + strings.TrimPrefix(escapeDir, string(os.PathSeparator))
	// The malicious request may be safely rejected or safely neutralized;
	// either is fine. Only the side effect is checked here.
	_, _, err = postTransform(base, zoneID, "export const pocH11 = "+suffix+";")
	if err != nil {
		return caseResult{"H11", "ERROR", "request failed: " + err.Error()}
	}
	time.Sleep(500 * time.Millisecond)
	after, err := snapshotTempDir()
	if err != nil {
		return caseResult{"H11", "ERROR", "could not re-snapshot temp dir: " + err.Error()}
	}
	if strays := newUnknownTempEntries(before, after); len(strays) > 0 {
		return caseResult{"H11", "FAIL", "required property violated"}
	}
	return caseResult{"H11", "PASS", "required property held"}
}

// Case H12: a lexical string-prefix relationship between two directory
// paths (e.g. "/tmp/x/app" is a string-prefix of "/tmp/x/app-secret") must
// not be treated as filesystem containment. A remediation that checks
// strings.HasPrefix(candidatePath, appRootPath) without also verifying a
// path-separator boundary would incorrectly treat a sibling directory whose
// name merely starts with the same string as being "inside" the app root.
func caseH12(esmcliBin string) caseResult {
	suffix := randSuffix()
	rootDir := filepath.Join(tempBase, "esm-poc-h12-"+suffix)
	appDir := filepath.Join(rootDir, "app")
	siblingDir := filepath.Join(rootDir, "app-secret")
	if err := os.MkdirAll(appDir, 0o755); err != nil {
		return caseResult{"H12", "ERROR", "could not prepare app fixture: " + err.Error()}
	}
	if err := os.MkdirAll(siblingDir, 0o755); err != nil {
		return caseResult{"H12", "ERROR", "could not prepare sibling fixture: " + err.Error()}
	}
	if err := os.WriteFile(filepath.Join(appDir, "index.html"), []byte("<h1>ok</h1>"), 0o644); err != nil {
		return caseResult{"H12", "ERROR", "could not write app fixture: " + err.Error()}
	}
	marker := "SECRET-" + suffix
	if err := os.WriteFile(filepath.Join(siblingDir, "secret.txt"), []byte(marker), 0o644); err != nil {
		return caseResult{"H12", "ERROR", "could not write sibling fixture: " + err.Error()}
	}

	port, err := freePort()
	if err != nil {
		return caseResult{"H12", "ERROR", "could not allocate a port: " + err.Error()}
	}
	cmd, err := startCliServer(esmcliBin, appDir, port)
	if err != nil {
		return caseResult{"H12", "ERROR", "CLI server did not start: " + err.Error()}
	}
	defer func() {
		if cmd.Process != nil {
			killCandidate(cmd)
			_, _ = cmd.Process.Wait()
		}
	}()

	base := fmt.Sprintf("http://127.0.0.1:%d", port)
	status, body, err := rawGet(base, "/../app-secret/secret.txt")
	if err != nil {
		return caseResult{"H12", "ERROR", "request failed: " + err.Error()}
	}
	if status == 200 && strings.Contains(string(body), marker) {
		return caseResult{"H12", "FAIL", "required property violated"}
	}
	return caseResult{"H12", "PASS", "required property held"}
}

// Case H13: a blocked/rejected web-serving request must not leave any
// stray, unrelated artifact directly under the OS temp directory beyond
// this harness's own known fixture directories. Complements H10 (package
// boundary) and H11 (zone boundary) by exercising the same invariant
// against the web-serving boundary.
func caseH13(esmcliBin string) caseResult {
	before, err := snapshotTempDir()
	if err != nil {
		return caseResult{"H13", "ERROR", "could not snapshot temp dir: " + err.Error()}
	}
	suffix := randSuffix()
	appDir := filepath.Join(tempBase, "esm-poc-h13-app-"+suffix)
	if err := os.MkdirAll(appDir, 0o755); err != nil {
		return caseResult{"H13", "ERROR", "could not prepare app fixture: " + err.Error()}
	}
	if err := os.WriteFile(filepath.Join(appDir, "index.html"), []byte("<h1>ok</h1>"), 0o644); err != nil {
		return caseResult{"H13", "ERROR", "could not write app fixture: " + err.Error()}
	}

	port, err := freePort()
	if err != nil {
		return caseResult{"H13", "ERROR", "could not allocate a port: " + err.Error()}
	}
	cmd, err := startCliServer(esmcliBin, appDir, port)
	if err != nil {
		return caseResult{"H13", "ERROR", "CLI server did not start: " + err.Error()}
	}
	defer func() {
		if cmd.Process != nil {
			killCandidate(cmd)
			_, _ = cmd.Process.Wait()
		}
	}()

	base := fmt.Sprintf("http://127.0.0.1:%d", port)
	depth := strings.Count(appDir, string(os.PathSeparator)) + 4
	victimDir := filepath.Join(tempBase, "esm-secret-h13-"+suffix)
	requestPath := "/" + traversalPrefix(depth) + strings.TrimPrefix(victimDir, string(os.PathSeparator)) + "/secret.txt"
	_, _, err = rawGet(base, requestPath)
	if err != nil {
		return caseResult{"H13", "ERROR", "request failed: " + err.Error()}
	}
	time.Sleep(500 * time.Millisecond)
	after, err := snapshotTempDir()
	if err != nil {
		return caseResult{"H13", "ERROR", "could not re-snapshot temp dir: " + err.Error()}
	}
	if strays := newUnknownTempEntries(before, after); len(strays) > 0 {
		return caseResult{"H13", "FAIL", "required property violated"}
	}
	return caseResult{"H13", "PASS", "required property held"}
}

// Case H14: a zone identifier made only of a dot segment ("..") must not
// escape the storage root on either the write or the read path. Every other
// zone payload in this suite contains a "/", so a character allowlist that
// permits '.' passes them all while still letting ".." resolve to the storage
// root's parent. Status-neutral: rejection or neutralization are both fine.
func caseH14(base, storageDir string) caseResult {
	suffix := randSuffix()
	outside := filepath.Join(filepath.Dir(storageDir), "modules", "transform")
	before := listFileNames(outside)
	if _, _, err := postTransform(base, "..", "export const pocH14 = \""+suffix+"\";"); err != nil {
		return caseResult{"H14", "ERROR", "write request failed: " + err.Error()}
	}
	if _, found := findFileNotIn(outside, before, 3*time.Second); found {
		return caseResult{"H14", "FAIL", "required property violated"}
	}

	hash := strings.Repeat("d", 40)
	marker := "SECRET-H14-" + suffix
	if err := writeFixtureExcl(filepath.Join(outside, hash+".mjs"), []byte(marker)); err != nil {
		return caseResult{"H14", "ERROR", "could not write victim fixture: " + err.Error()}
	}
	status, body, err := getTransform(base, "..", hash, "mjs")
	if err != nil {
		return caseResult{"H14", "ERROR", "read request failed: " + err.Error()}
	}
	if status == 200 && strings.Contains(string(body), marker) {
		return caseResult{"H14", "FAIL", "required property violated"}
	}
	return caseResult{"H14", "PASS", "required property held"}
}

// Case H15 (compatibility control): a package file whose name merely
// contains ".." as a substring (not a path segment) must still be served
// through the server's package path -- guards against a remediation that
// rejects every package subpath containing "..".
func caseH15(base string) caseResult {
	status, body, err := rawGet(base, "/pocdd@1.0.0/foo..bar.js?raw")
	if err != nil {
		return caseResult{"H15", "ERROR", "request failed: " + err.Error()}
	}
	if status != 200 || !strings.Contains(string(body), "dd-ok") {
		return caseResult{"H15", "FAIL", "required property violated"}
	}
	return caseResult{"H15", "PASS", "required property held"}
}

// run returns the exit code instead of calling os.Exit, so deferred cleanup
// (killing candidate servers, removing fixtures) always runs.
func run() int {
	if len(os.Args) != 3 {
		fmt.Fprintln(os.Stderr, "usage: hidden_harness <esmd-binary-path> <esmcli-binary-path-or-empty>")
		return 2
	}
	esmdBin := os.Args[1]
	esmcliBin := os.Args[2]

	if err := initCandidateIdentity(); err != nil {
		fmt.Println("ERROR\t" + err.Error())
		return 3
	}

	// Literal /tmp (sticky, root-owned), not $TMPDIR, which may point into
	// the candidate's own home directory. The "esm-repro-" prefix is one of
	// knownTempPrefixes, so H10/H11/H13 ignore it.
	var err error
	tempBase, err = os.MkdirTemp(realTmpDir, "esm-repro-fx-")
	if err == nil {
		err = os.Chmod(tempBase, 0o1777)
	}
	if err != nil {
		fmt.Println("ERROR\tcould not create fixture base: " + err.Error())
		return 3
	}
	defer os.RemoveAll(tempBase)

	// Sticky and world-writable like a normal temp dir, so esmd's working
	// directory stays writable while the harness's own entries stay fixed.
	workRoot := filepath.Join(tempBase, "esm-repro-work")
	err = os.Mkdir(workRoot, 0o755)
	if err == nil {
		err = os.Chmod(workRoot, 0o1777)
	}
	if err != nil {
		fmt.Println("ERROR\tcould not create work dir: " + err.Error())
		return 3
	}

	port, err := freePort()
	if err != nil {
		fmt.Println("ERROR\tcould not allocate a port: " + err.Error())
		return 3
	}

	// Case G/H fixtures: a mock npm registry (loopback only) must exist before
	// esmd starts, since esmd's npmRegistry is fixed at process startup.
	gSuffix := randSuffix()
	gEscapeDir := filepath.Join(tempBase, "esm-poc-g-"+gSuffix)
	pocgTarball := buildTarball(map[string]string{
		"package/index.js":     "export const ok = 1;",
		"package/package.json": `{"name":"pocg","version":"1.0.0","main":"index.js"}`,
		"/" + traversalPrefix(15) + strings.TrimPrefix(gEscapeDir, string(os.PathSeparator)) + "/pwned.js": "export const pwned = true;",
	})
	pochTarball := buildTarball(map[string]string{
		"package/index.js":     "export const ok = true;",
		"package/package.json": `{"name":"poch","version":"1.0.0","main":"index.js"}`,
	})
	pocpTarball := buildTarball(map[string]string{
		"package/dist/../lib/index.js": "export const ok = true;",
		"package/package.json":         `{"name":"pocp","version":"1.0.0","main":"index.js"}`,
	})
	h10Suffix := randSuffix()
	h10EscapeDir := filepath.Join(tempBase, "esm-poc-h10-"+h10Suffix)
	poch10Tarball := buildTarball(map[string]string{
		"package/index.js":     "export const ok = 1;",
		"package/package.json": `{"name":"poch10","version":"1.0.0","main":"index.js"}`,
		"/" + traversalPrefix(15) + strings.TrimPrefix(h10EscapeDir, string(os.PathSeparator)) + "/pwned.js": "export const pwned = true;",
	})
	pocddTarball := buildTarball(map[string]string{
		"package/index.js":     "export const ok = true;",
		"package/foo..bar.js":  "export const ok = \"dd-ok\";",
		"package/package.json": `{"name":"pocdd","version":"1.0.0","main":"index.js"}`,
	})
	registryBase, err := startMockRegistry(map[string][]byte{"pocg": pocgTarball, "poch": pochTarball, "pocp": pocpTarball, "poch10": poch10Tarball, "pocdd": pocddTarball})
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
			killCandidate(cmd)
			_, _ = cmd.Process.Wait()
		}
	}()

	base := fmt.Sprintf("http://127.0.0.1:%d", port)

	results := []caseResult{
		caseA(base),
		caseB(base),
		caseC(base, storageDir),
		caseD(base),
		caseE(base),
		caseF(base),
		caseG(base, gEscapeDir),
		caseH(base),
		caseN(base, storageDir),
		caseO(base),
		caseP(base),
		caseH10(base),
		caseH11(base),
		caseH14(base, storageDir),
		caseH15(base),
	}

	if esmcliBin != "" {
		results = append(results, caseI(esmcliBin), caseJ(esmcliBin), caseK(esmcliBin), caseL(esmcliBin), caseM(esmcliBin), caseQ(esmcliBin), caseH12(esmcliBin), caseH13(esmcliBin))
	} else {
		results = append(results, caseUnavailable("I"), caseUnavailable("J"), caseUnavailable("H06"), caseUnavailable("H07"), caseUnavailable("H08"), caseUnavailable("H09"), caseUnavailable("H12"), caseUnavailable("H13"))
	}

	overall := 0
	for _, r := range results {
		fmt.Printf("Case %s: %s (%s)\n", r.id, r.status, r.detail)
		if r.status != "PASS" {
			overall = 1
		}
	}
	return overall
}

func main() {
	os.Exit(run())
}
