package iclouddrive

import (
	"archive/zip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"net/http"
	"os"
	"path"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/rclone/rclone/fs"
)

// Only this bridge build enables the package representation cache.
const bridgeIWorkCacheEnv = "ICLOUD_BRIDGE_PACKAGE_CACHE"

type bridgePackage struct {
	file string
	size atomic.Int64
	mu   sync.Mutex
}

func bridgeCacheKey(value string) string {
	digest := sha256.Sum256([]byte(value))
	return hex.EncodeToString(digest[:])
}

func bridgeIWorkName(remote string) bool {
	switch strings.ToLower(path.Ext(remote)) {
	case ".pages", ".numbers", ".key":
		return true
	default:
		return false
	}
}

// prepareBridgePackage resolves the representation without downloading its body.
func (o *Object) prepareBridgePackage(ctx context.Context) error {
	base := os.Getenv(bridgeIWorkCacheEnv)
	if base == "" || !bridgeIWorkName(o.remote) {
		return nil
	}
	var resp *http.Response
	var packageToken bool
	err := o.fs.pacer.Call(func() (bool, error) {
		var err error
		_, packageToken, resp, err = o.fs.service.GetDownloadDetailsByDriveID(ctx, o.driveID)
		return shouldRetry(ctx, resp, err)
	})
	if err != nil {
		return fmt.Errorf("cannot resolve complete iWork document: %w", err)
	}
	if !packageToken {
		return nil // A flat .pages/.numbers/.key file has ordinary data-token semantics.
	}
	key := bridgeCacheKey(o.driveID + "\x00" + o.etag + "\x00" + o.modTime.Format(time.RFC3339Nano) + fmt.Sprint(o.size))
	o.bridgePackage = &bridgePackage{file: filepath.Join(base, bridgeCacheKey(o.fs.name), key + ".zip")}
	o.bridgePackage.size.Store(-1)
	if info, err := os.Stat(o.bridgePackage.file); err == nil && info.Mode().IsRegular() {
		archive, err := zip.OpenReader(o.bridgePackage.file)
		if err == nil {
			_ = archive.Close()
			o.bridgePackage.size.Store(info.Size())
		}
	}
	return nil
}

type bridgeContextReader struct {
	ctx context.Context
	in  io.Reader
}

func (r bridgeContextReader) Read(p []byte) (int, error) {
	if err := r.ctx.Err(); err != nil { return 0, err }
	return r.in.Read(p)
}

func bridgeCheckArchive(ctx context.Context, filename string) error {
	archive, err := zip.OpenReader(filename)
	if err != nil { return fmt.Errorf("iWork download is not a complete ZIP document: %w", err) }
	defer archive.Close()
	if len(archive.File) == 0 { return fmt.Errorf("iWork download contains no document entries") }
	for _, entry := range archive.File {
		if err := ctx.Err(); err != nil { return err }
		if entry.FileInfo().IsDir() { continue }
		reader, err := entry.Open()
		if err != nil { return fmt.Errorf("cannot verify iWork document entry: %w", err) }
		_, err = io.Copy(io.Discard, bridgeContextReader{ctx, reader})
		closeErr := reader.Close()
		if err == nil { err = closeErr }
		if err != nil { return fmt.Errorf("iWork document failed ZIP integrity verification: %w", err) }
	}
	return ctx.Err()
}

// cacheBridgePackage measures and verifies the bytes used for every later upload.
func (o *Object) cacheBridgePackage(ctx context.Context) error {
	state := o.bridgePackage
	state.mu.Lock()
	defer state.mu.Unlock()
	if err := ctx.Err(); err != nil { return err }
	if err := bridgeCheckArchive(ctx, state.file); err == nil {
		info, err := os.Stat(state.file)
		if err != nil { return err }
		state.size.Store(info.Size())
		_ = os.Chtimes(state.file, time.Now(), time.Now())
		return nil
	} else if ctx.Err() != nil { return ctx.Err() }

	directory := filepath.Dir(state.file)
	if err := os.MkdirAll(directory, 0o700); err != nil { return err }
	if err := os.Chmod(directory, 0o700); err != nil { return err }
	// Old document revisions are expendable; Open can recreate an evicted entry.
	if entries, err := os.ReadDir(directory); err == nil {
		for _, entry := range entries {
			if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".zip") { continue }
			if info, err := entry.Info(); err == nil && time.Since(info.ModTime()) > 7 * 24 * time.Hour {
				_ = os.Remove(filepath.Join(directory, entry.Name()))
			}
		}
	}
	partial, err := os.CreateTemp(directory, ".incoming-")
	if err != nil { return err }
	defer func() { _ = partial.Close(); _ = os.Remove(partial.Name()) }()
	if err := partial.Chmod(0o600); err != nil { return err }

	fs.Infof(o, "Preparing complete iWork download; measuring ZIP bytes before upload")
	var resp *http.Response
	err = o.fs.pacer.Call(func() (bool, error) {
		downloadURL, packageToken, response, err := o.fs.service.GetDownloadDetailsByDriveID(ctx, o.driveID)
		if err != nil { return shouldRetry(ctx, response, err) }
		if !packageToken { return false, fmt.Errorf("iWork document representation changed; retry with a fresh listing") }
		resp, err = o.fs.service.DownloadFile(ctx, downloadURL, nil)
		return shouldRetry(ctx, resp, err)
	})
	if err != nil { return err }
	defer resp.Body.Close()
	size, err := io.Copy(partial, bridgeContextReader{ctx, resp.Body})
	if err != nil { return fmt.Errorf("complete iWork download failed: %w", err) }
	if err = partial.Close(); err != nil { return err }
	if err = bridgeCheckArchive(ctx, partial.Name()); err != nil { return err }
	if err = os.Rename(partial.Name(), state.file); err != nil { return err }
	state.size.Store(size)
	fs.Infof(o, "Complete iWork download ready (%d ZIP bytes; %d bundle bytes)", size, o.size)
	return nil
}

type bridgeSectionReader struct {
	io.Reader
	io.Closer
}

func (o *Object) openBridgePackage(ctx context.Context, options []fs.OpenOption) (io.ReadCloser, error) {
	if err := o.cacheBridgePackage(ctx); err != nil { return nil, err }
	size := o.bridgePackage.size.Load()
	fs.FixRangeOption(options, size)
	offset, limit := int64(0), size
	for _, option := range options {
		switch option := option.(type) {
		case *fs.RangeOption:
			offset, limit = option.Decode(size)
		case *fs.SeekOption:
			offset, limit = option.Offset, -1
		}
	}
	if offset < 0 || offset > size { return nil, fmt.Errorf("invalid iWork ZIP range") }
	if limit < 0 || limit > size - offset { limit = size - offset }
	file, err := os.Open(o.bridgePackage.file)
	if err != nil { return nil, err }
	return &bridgeSectionReader{bridgeContextReader{ctx, io.NewSectionReader(file, offset, limit)}, file}, nil
}
