package iclouddrive

import (
	"archive/zip"
	"bytes"
	"context"
	"crypto/sha1"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/rclone/rclone/backend/iclouddrive/api"
	"github.com/rclone/rclone/backend/webdav"
	"github.com/rclone/rclone/fs"
	"github.com/rclone/rclone/fs/config/configmap"
	"github.com/rclone/rclone/fs/operations"
	"github.com/rclone/rclone/lib/pacer"
)

func bridgeTestArchive(t *testing.T) []byte {
	t.Helper()
	var body bytes.Buffer
	writer := zip.NewWriter(&body)
	entry, err := writer.Create("Index/Document.iwa")
	if err != nil { t.Fatal(err) }
	if _, err = entry.Write(bytes.Repeat([]byte("complete document contents\n"), 1000)); err != nil { t.Fatal(err) }
	if err = writer.Close(); err != nil { t.Fatal(err) }
	return body.Bytes()
}

func bridgeTestCloud(t *testing.T, body []byte, packageToken bool) (*Fs, *atomic.Int64) {
	t.Helper()
	var downloads atomic.Int64
	var server *httptest.Server
	server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/download/by_id") {
			kind := "data_token"
			if packageToken { kind = "package_token" }
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(map[string]any{kind: map[string]string{"url": server.URL + "/payload"}})
			return
		}
		if r.URL.Path != "/payload" { http.NotFound(w, r); return }
		downloads.Add(1)
		w.Header().Set("Content-Type", "application/octet-stream")
		w.WriteHeader(http.StatusOK)
		w.(http.Flusher).Flush() // Apple package downloads need not supply Content-Length.
		_, _ = w.Write(body)
	}))
	t.Cleanup(server.Close)
	client, err := api.New("fixture@example.invalid", "unused", "unused", "unused", nil, nil, "fixture", "")
	if err != nil { t.Fatal(err) }
	if err = json.Unmarshal([]byte(fmt.Sprintf(`{"webservices":{"drivews":{"url":%q},"docws":{"url":%q}}}`, server.URL, server.URL)), &client.Session.AccountInfo); err != nil { t.Fatal(err) }
	service, err := client.DriveService()
	if err != nil { t.Fatal(err) }
	return &Fs{name: "icloud_fixture", service: service, features: &fs.Features{},
		pacer: fs.NewPacer(context.Background(), pacer.NewDefault(pacer.MinSleep(time.Millisecond)))}, &downloads
}

func bridgeTestObject(t *testing.T, cloud *Fs, name, version string, size int64) fs.Object {
	t.Helper()
	object, err := cloud.NewObjectFromDriveItem(context.Background(), name, &api.DriveItem{
		Type: "FILE", Size: size, Drivewsid: "FILE::com.apple.CloudDocs::fixture", Etag: version,
		DateModified: time.Date(2026, 10, 8, 12, 0, 0, 0, time.UTC),
	})
	if err != nil { t.Fatal(err) }
	return object
}

func TestBridgeIWorkMeasuresPackageWithoutTrustingBundleSize(t *testing.T) {
	t.Setenv(bridgeIWorkCacheEnv, t.TempDir())
	body := bridgeTestArchive(t)
	cloud, downloads := bridgeTestCloud(t, body, true)
	object := bridgeTestObject(t, cloud, "Report.pages", "v1", 26000)
	if object.Size() != -1 { t.Fatalf("uncached package size = %d; must be unknown", object.Size()) }
	if downloads.Load() != 0 { t.Fatal("listing/preview downloaded document bodies") }
	reader, err := object.Open(context.Background())
	if err != nil { t.Fatal(err) }
	got, err := io.ReadAll(reader)
	_ = reader.Close()
	if err != nil { t.Fatal(err) }
	if !bytes.Equal(got, body) || object.Size() != int64(len(body)) { t.Fatal("payload or measured size differs") }
	if downloads.Load() != 1 { t.Fatal("package should be fetched once") }
	listedAgain := bridgeTestObject(t, cloud, "Report.pages", "v1", 26000)
	if listedAgain.Size() != int64(len(body)) { t.Fatal("cached listing did not use ZIP size") }
	reader, err = listedAgain.Open(context.Background(), &fs.RangeOption{Start: -1, End: 15})
	if err != nil { t.Fatal(err) }
	got, err = io.ReadAll(reader)
	_ = reader.Close()
	if err != nil || !bytes.Equal(got, body[len(body)-15:]) { t.Fatal("range does not refer to ZIP bytes") }
	if downloads.Load() != 1 { t.Fatal("unchanged document was downloaded again") }
	changed := bridgeTestObject(t, cloud, "Report.pages", "v2", 26000)
	if changed.Size() != -1 { t.Fatal("changed Apple ETag reused an older representation") }
	reader, err = changed.Open(context.Background())
	if err != nil { t.Fatal(err) }
	_ = reader.Close()
	if downloads.Load() != 2 { t.Fatal("changed document did not refresh cache") }
}

func TestBridgeIWorkPlainDocumentKeepsOrdinaryMetadata(t *testing.T) {
	t.Setenv(bridgeIWorkCacheEnv, t.TempDir())
	body := []byte("a flat document is an ordinary Apple data token")
	cloud, downloads := bridgeTestCloud(t, body, false)
	object := bridgeTestObject(t, cloud, "Report.numbers", "v1", int64(len(body)))
	if object.Size() != int64(len(body)) { t.Fatal("flat file size changed") }
	reader, err := object.Open(context.Background())
	if err != nil { t.Fatal(err) }
	got, err := io.ReadAll(reader)
	_ = reader.Close()
	if err != nil || !bytes.Equal(got, body) || downloads.Load() != 1 { t.Fatal("flat file did not stream normally") }
}

func TestBridgeIWorkInvalidAndTruncatedPackagesNeverBecomeUploadSources(t *testing.T) {
	valid := bridgeTestArchive(t)
	badCRC := bytes.Clone(valid)
	central := bytes.Index(badCRC, []byte{'P', 'K', 1, 2})
	if central < 0 { t.Fatal("test ZIP has no central directory") }
	badCRC[central+16] ^= 1 // Structurally valid archive with a wrong member checksum.
	for _, body := range [][]byte{[]byte("not a ZIP archive"), valid[:len(valid)-10], badCRC} {
		t.Run(fmt.Sprint(len(body)), func(t *testing.T) {
			cache := t.TempDir()
			t.Setenv(bridgeIWorkCacheEnv, cache)
			cloud, _ := bridgeTestCloud(t, body, true)
			object := bridgeTestObject(t, cloud, "Report.key", "v1", 26000)
			if reader, err := object.Open(context.Background()); err == nil { _ = reader.Close(); t.Fatal("invalid package accepted") }
			_ = filepath.Walk(cache, func(path string, info os.FileInfo, err error) error {
				if err == nil && !info.IsDir() { t.Errorf("partial cache file remains: %s", path) }
				return err
			})
		})
	}
}

func TestBridgeIWorkCancelledOpenDoesNotDownload(t *testing.T) {
	t.Setenv(bridgeIWorkCacheEnv, t.TempDir())
	cloud, downloads := bridgeTestCloud(t, bridgeTestArchive(t), true)
	object := bridgeTestObject(t, cloud, "Report.pages", "v1", 26000)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if reader, err := object.Open(ctx); err == nil { _ = reader.Close(); t.Fatal("cancelled open succeeded") }
	if downloads.Load() != 0 { t.Fatal("cancelled open fetched private data") }
}

func TestBridgeIWorkWebDAVReceivesMeasuredLengthAndCompleteBytes(t *testing.T) {
	body := bridgeTestArchive(t)
	for _, chunkSize := range []string{"0", "64"} {
		t.Run("chunk_size_"+chunkSize, func(t *testing.T) {
			t.Setenv(bridgeIWorkCacheEnv, t.TempDir())
			cloud, _ := bridgeTestCloud(t, body, true)
			object := bridgeTestObject(t, cloud, "Report.pages", "v1", 26000)
			var received []byte
			var length int64
			chunks := map[string][]byte{}
			var mu sync.Mutex
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				mu.Lock()
				defer mu.Unlock()
				switch r.Method {
				case "DELETE": w.WriteHeader(http.StatusNotFound)
				case "MKCOL": w.WriteHeader(http.StatusCreated)
				case "PUT":
					part, err := io.ReadAll(r.Body)
					if err != nil || int64(len(part)) != r.ContentLength {
						w.WriteHeader(http.StatusBadGateway)
						return
					}
					length += r.ContentLength
					if strings.Contains(r.URL.Path, "/dav/uploads/") { chunks[r.URL.Path] = part } else { received = part }
					w.Header().Set("X-OC-Mtime", "accepted")
					w.WriteHeader(http.StatusCreated)
				case "MOVE":
					names := make([]string, 0, len(chunks))
					for name := range chunks { names = append(names, name) }
					sort.Strings(names)
					for _, name := range names { received = append(received, chunks[name]...) }
					w.Header().Set("X-OC-Mtime", "accepted")
					w.WriteHeader(http.StatusCreated)
				case "PATCH":
					digest := sha1.Sum(received)
					w.Header().Set("OC-Checksum", fmt.Sprintf("SHA1:%x", digest))
					w.WriteHeader(http.StatusOK)
				case "PROPFIND":
					if received == nil { http.NotFound(w, r); return }
					w.Header().Set("Content-Type", "application/xml")
					w.WriteHeader(http.StatusMultiStatus)
					_, _ = fmt.Fprintf(w, `<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"><d:response><d:href>/remote.php/dav/files/fixture/Report.pages</d:href><d:propstat><d:prop><d:resourcetype/><d:getcontentlength>%d</d:getcontentlength><d:getlastmodified>Thu, 08 Oct 2026 12:00:00 GMT</d:getlastmodified></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>`, len(received))
				default: w.WriteHeader(http.StatusMethodNotAllowed)
				}
			}))
			defer server.Close()
			ctx := context.Background()
			destination, err := webdav.NewFs(ctx, "nextcloud_fixture", "", configmap.Simple{
				"url": server.URL + "/remote.php/dav/files/fixture/", "vendor": "nextcloud", "nextcloud_chunk_size": chunkSize})
			if err != nil { t.Fatal(err) }
			if _, err = operations.Copy(ctx, destination, nil, "Report.pages", object); err != nil { t.Fatal(err) }
			mu.Lock()
			defer mu.Unlock()
			if length != int64(len(body)) || !bytes.Equal(received, body) { t.Fatalf("upload length %d, received %d, expected %d", length, len(received), len(body)) }
			if chunkSize != "0" && len(chunks) < 2 { t.Fatal("Nextcloud chunked upload was not exercised") }
		})
	}
}
