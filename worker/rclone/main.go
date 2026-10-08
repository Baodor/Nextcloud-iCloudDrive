// The bridge needs these backends and commands; Go links their ordinary rclone implementations.
package main

import (
	_ "github.com/rclone/rclone/backend/alias"
	_ "github.com/rclone/rclone/backend/iclouddrive"
	_ "github.com/rclone/rclone/backend/local"
	_ "github.com/rclone/rclone/backend/webdav"
	"github.com/rclone/rclone/cmd"
	_ "github.com/rclone/rclone/cmd/bisync"
	_ "github.com/rclone/rclone/cmd/cat"
	_ "github.com/rclone/rclone/cmd/config"
	_ "github.com/rclone/rclone/cmd/copy"
	_ "github.com/rclone/rclone/cmd/copyto"
	_ "github.com/rclone/rclone/cmd/lsjson"
	_ "github.com/rclone/rclone/cmd/rcd"
	_ "github.com/rclone/rclone/cmd/serve/webdav"
	_ "github.com/rclone/rclone/cmd/sync"
	_ "github.com/rclone/rclone/cmd/version"
)

func main() { cmd.Main() }
