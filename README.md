# aurdist

`aurdist` is a simple tool for building and managing your own repository of AUR packages. It uses a containerized build system to ensure that packages are built in a clean and reproducible environment.

## Features
* Build AUR packages in a containerized Arch Linux environment.
* Manage a pacman repository of your own packages.

## Use
Pacman dependencies: `sudo pacman -Sy --noconfirm base-devel pacman-contrib git docker pigz docker-buildx rsync`

1. Clone repo on an Arch box w/ docker installed (ensure your user is in Docker group & has passwordless sudo)
2. Packages get built in `packages/` under the repo. If you'd like them to be rsync'd somewhere else, e.g. where nginx is expecting them, then do: `echo "PATH" > .where` (and ensure you've installed `rsync` from repos)
3. Run either `./build.sh <package_name>` or put a list of your favorite AUR packages into `targets.txt` and run `./build.sh` with no args
4. Put either your web server URL or your path to `packages/` into your `/etc/pacman.conf` (if it's a web server, then you can have a dedicated package building box!)

## Pacman Config Examples
Local folder:
```
[aurdist]
SigLevel = Never
Server = file:///home/you/aurdist/packages
```

HTTP: (assuming localhost, otherwise use remote IP)
```
[aurdist]
SigLevel = Never
Server = http://127.0.0.1/
```