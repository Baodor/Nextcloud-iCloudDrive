"""Apply the narrowly scoped bridge fix to the pinned rclone source tree."""
from pathlib import Path
import shutil
import sys

source = Path(sys.argv[1])
bundle = Path(__file__).parent


def replace(path, before, after, count=1):
    file = source / path
    text = file.read_text()
    if text.count(before) != count:
        raise SystemExit(f"Pinned rclone source differs at {path}; refusing an ambiguous patch.")
    file.write_text(text.replace(before, after))


replace("backend/iclouddrive/api/drive.go",
    'func (d *DriveService) GetDownloadURLByDriveID(ctx context.Context, id string) (string, *http.Response, error) {',
    '''func (d *DriveService) GetDownloadURLByDriveID(ctx context.Context, id string) (string, *http.Response, error) {
    downloadURL, _, response, err := d.GetDownloadDetailsByDriveID(ctx, id)
    return downloadURL, response, err
}

// GetDownloadDetailsByDriveID identifies the flat data or zipped package representation.
func (d *DriveService) GetDownloadDetailsByDriveID(ctx context.Context, id string) (string, bool, *http.Response, error) {''')
replace("backend/iclouddrive/api/drive.go", 'return "", resp, err\n\t}\n\n\tvar url string',
    'return "", false, resp, err\n\t}\n\n\tif filer == nil || (filer.DataToken == nil && filer.PackageToken == nil) {\n\t\treturn "", false, resp, fmt.Errorf("iCloud did not supply a document download token")\n\t}\n\n\tvar url string')
replace("backend/iclouddrive/api/drive.go", 'return url, resp, err\n}',
    'return url, filer.DataToken == nil, resp, err\n}')
replace("backend/iclouddrive/api/drive.go", '\t"context"\n', '\t"context"\n\t"fmt"\n')
replace("backend/iclouddrive/iclouddrive.go", '\tdownloadURL string\n}', '\tdownloadURL string\n\tbridgePackage *bridgePackage\n}')
replace("backend/iclouddrive/iclouddrive.go", '\treturn o, nil\n}\n\nfunc (f *Fs) readMetaData',
    '\tif err := o.prepareBridgePackage(ctx); err != nil {\n\t\treturn nil, err\n\t}\n\treturn o, nil\n}\n\nfunc (f *Fs) readMetaData')
replace("backend/iclouddrive/iclouddrive.go", 'func (o *Object) Open(ctx context.Context, options ...fs.OpenOption) (io.ReadCloser, error) {',
    'func (o *Object) Open(ctx context.Context, options ...fs.OpenOption) (io.ReadCloser, error) {\n\tif o.bridgePackage != nil {\n\t\treturn o.openBridgePackage(ctx, options)\n\t}')
replace("backend/iclouddrive/iclouddrive.go", 'func (o *Object) Size() int64 {\n\treturn o.size',
    'func (o *Object) Size() int64 {\n\tif o.bridgePackage != nil {\n\t\treturn o.bridgePackage.size.Load()\n\t}\n\treturn o.size')
replace("backend/iclouddrive/iclouddrive.go", '\to.size = item.Size\n', '\to.bridgePackage = nil\n\to.size = item.Size\n')
for name in ("bridge_iwork.go", "bridge_iwork_test.go"):
    shutil.copyfile(bundle / name, source / "backend/iclouddrive" / name)
shutil.copyfile(bundle / "main.go", source / "bridge_main.go")
