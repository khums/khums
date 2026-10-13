// Command launcher is the only supported entry point to the public
// reproducer (invoked by /testbed/reproducer/run-all.sh). It builds the
// candidate programs from /testbed itself and starts the sealed harness with
// a launch token baked into both binaries at image build time. It takes no
// arguments, so the harness can only ever run against freshly built /testbed
// source. Both binaries are root-owned and execute-only: neither the token nor
// the harness internals can be read out of them.
package main

import (
	"bufio"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
)

// launchToken is injected with -ldflags "-X main.launchToken=..." at image
// build time; the harness refuses to run without the matching value.
var launchToken string

const (
	goBin    = "/usr/local/go/bin/go"
	harness  = "/testbed/reproducer/harness"
	testbed  = "/testbed"
	logLines = 50
)

func build(out, pkg, logPath string) error {
	logFile, err := os.Create(logPath)
	if err != nil {
		return err
	}
	defer logFile.Close()
	cmd := exec.Command(goBin, "build", "-o", out, pkg)
	cmd.Dir = testbed
	cmd.Env = append(os.Environ(), "GOFLAGS=-mod=mod", "GOPROXY=off")
	cmd.Stdout = logFile
	cmd.Stderr = logFile
	return cmd.Run()
}

func printHead(w io.Writer, path string) {
	f, err := os.Open(path)
	if err != nil {
		return
	}
	defer f.Close()
	s := bufio.NewScanner(f)
	for n := 0; n < logLines && s.Scan(); n++ {
		fmt.Fprintln(w, s.Text())
	}
}

type countingWriter struct {
	w io.Writer
	n int
}

func (c *countingWriter) Write(p []byte) (int, error) {
	c.n += len(p)
	return c.w.Write(p)
}

func run() int {
	workDir, err := os.MkdirTemp("", "esm-repro-run-")
	if err != nil {
		fmt.Println("ERROR: cannot create workdir")
		return 3
	}
	defer os.RemoveAll(workDir)

	esmd := filepath.Join(workDir, "esmd")
	esmdLog := filepath.Join(workDir, "build-esmd.log")
	if err := build(esmd, "./server/esmd", esmdLog); err != nil {
		fmt.Println("ERROR: candidate server failed to build")
		printHead(os.Stderr, esmdLog)
		return 3
	}

	esmcli := filepath.Join(workDir, "esmcli")
	esmcliLog := filepath.Join(workDir, "build-esmcli.log")
	if err := build(esmcli, ".", esmcliLog); err != nil {
		fmt.Fprintln(os.Stderr, "NOTE: the CLI program (go build . in /testbed) failed to build; cases that depend on it report ERROR (or FAIL if another property they represent fails). Build output:")
		printHead(os.Stderr, esmcliLog)
		esmcli = ""
	}

	harnessErr := filepath.Join(workDir, "harness.err")
	errFile, err := os.Create(harnessErr)
	if err != nil {
		fmt.Println("ERROR: cannot create harness log")
		return 3
	}
	defer errFile.Close()

	out := &countingWriter{w: os.Stdout}
	cmd := exec.Command(harness, esmd, esmcli)
	cmd.Env = append(os.Environ(), "ESM_REPRO_LAUNCH="+launchToken)
	cmd.Stdout = out
	cmd.Stderr = errFile
	rc := 0
	if err := cmd.Run(); err != nil {
		if exitErr, ok := err.(*exec.ExitError); ok {
			rc = exitErr.ExitCode()
		} else {
			fmt.Println("ERROR: sealed harness could not be started")
			return 3
		}
	}
	if rc != 0 && out.n == 0 {
		fmt.Println("ERROR: harness produced no output")
		printHead(os.Stderr, harnessErr)
	}
	return rc
}

func main() {
	os.Exit(run())
}
