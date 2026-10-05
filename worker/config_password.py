"""Print only to rclone's private password-command pipe."""
import os
print(os.environ["RCLONE_CONFIG_PASS"])
