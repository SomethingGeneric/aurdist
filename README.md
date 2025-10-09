# aurdist

`aurdist` is a comprehensive Python tool for building and managing your own repository of AUR packages. It runs natively and automatically handles AUR package dependencies by pulling them with Pacman.

## Features
* **Single Python script** - Everything consolidated into `aurutil.py`
* **Automatic dependency resolution** - Detects and handles AUR package dependencies natively
* **Version checking** - Compares local packages with AUR versions
* **Cron-friendly** - Run with no arguments to check and rebuild outdated packages
* **Native building** - Builds packages directly on your system using Pacman
* **Repository management** - Automatically updates pacman repository database
* **Remote syncing** - Optional rsync to web server directories

## Installation
Pacman dependencies: `sudo pacman -Sy --noconfirm base-devel pacman-contrib git rsync curl jq python python-requests`

1. Clone repo on an Arch Linux system
2. Ensure your user has passwordless sudo access for package installation
3. Packages get built in `packages/` under the repo
4. If you'd like them to be rsync'd somewhere else, e.g. where nginx is expecting them, then do: `echo "PATH" > .where` (and ensure you've installed `rsync` from repos)

## Usage

The primary usage of this tool is through GitHub Actions, which automatically builds packages and serves them via my personal web server (aur.mattcompton.dev)

### Basic Usage
```bash
# Check all packages and rebuild outdated ones
python aurutil.py

# Build a specific AUR package
python aurutil.py google-chrome

# Build from a generic git URL
python aurutil.py https://github.com/SomethingGeneric/pkgbuild.linux.git

# Force build a package even if up to date
python aurutil.py -f google-chrome

# Check versions only (don't build)
python aurutil.py --check-only
```

### Package Management
Create a `targets.txt` file with package names (one per line) to specify which packages to track. You can use either AUR package names or generic git URLs (HTTP/HTTPS/SSH):
```
# AUR packages
google-chrome
slack-desktop
visual-studio-code-bin

# Generic git repositories (HTTP/HTTPS)
https://github.com/SomethingGeneric/pkgbuild.linux.git

# Generic git repositories (SSH)
git@github.com:user/custom-package.git
```

When using git URLs, the package name is automatically extracted from the repository name. For example, `https://github.com/user/pkgbuild.linux.git` will be built as package `pkgbuild.linux`.

**Version Checking for Git URLs:**
For git repository packages, the tool automatically clones the repository and parses the `pkgver` variable from the PKGBUILD file to compare with the locally built version. This ensures you're notified when updates are available in the git repository.

### SSH Configuration

Configure SSH settings for remote operations by creating a `ssh.toml` file:

```toml
[ssh]
# Remote destination in format user@host:path
user = "root@h.goober.cloud:/var/www/aur"

# SSH port (optional, defaults to 22)
port = 2022

# Additional SSH options (optional)
# strict_host_key_checking = "no"  # Default is "no"
# connect_timeout = 30
# server_alive_interval = 60
```

The SSH configuration is used for:
- Remote package version checking with `--remote-dest` flag
- Package syncing when using `.where` file
- All SSH operations automatically use the configured port and options

If no `ssh.toml` file exists, the tool falls back to default SSH behavior for backward compatibility.

## Dependency Resolution

The build system automatically handles AUR package dependencies natively:

- **Dependency Detection**: Parses PKGBUILD files to identify all dependencies
- **Repository Checking**: Checks if dependencies are available in official Arch repositories  
- **AUR Validation**: Uses the AUR RPC API to verify AUR package availability
- **Native Installation**: Installs official repo packages with Pacman, then builds AUR dependencies natively
- **Detailed Reporting**: Shows which dependencies are found where and any missing packages

## Pacman Config Examples

**Hosted:**
```
[aurdist]
SigLevel = Never
Server = https://aur.mattcompton.dev
```

**Local folder:**
```
[aurdist]
SigLevel = Never
Server = file:///home/you/aurdist/packages
```

**HTTP (self-hosted):**
```
[aurdist]
SigLevel = Never
Server = http://your-server.com/path/to/packages/
```
