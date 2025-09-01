# aurdist

`aurdist` is a simple tool for building and managing your own repository of AUR packages. It uses a containerized build system to ensure that packages are built in a clean and reproducible environment.

## Features

* Build AUR packages in a containerized Arch Linux environment.
* Manage a pacman repository of your own packages.
* Serve the repository over HTTP.
* Push the repository to a remote server.

## Usage

### Building a package

To build a package, run the `build_package` script with the name of the package as an argument:

```
./bin/build_package <package_name>
```

For example, to build the `cowsay` package, you would run:

```
./bin/build_package cowsay
```

This will build the package and add it to your local repository in the `repo/` directory.

### Pushing the repository to a remote server

To push the repository to a remote server, you first need to configure the remote server in the `config/aurdist.conf` file. Then, you can run the `push` script:

```
./bin/push
```

### Serving the repository

To serve the repository over HTTP, you can use the provided `docker-compose.yml` file:

```
# docker-compose up -d
```

This will start a web server on port 8080 that serves the contents of the `repo/` directory.

### Client configuration

To use your repository on a client machine, you need to add the following to your `/etc/pacman.conf` file:

```
[aurdist]
SigLevel = Optional TrustAll
Server = http://<your_server_ip>:8080
```

Replace `<your_server_ip>` with the IP address of the machine running the web server.

## Configuration

The configuration for `aurdist` is stored in the `config/aurdist.conf` file. The following variables can be configured:

* `REPO_NAME`: The name of the repository.
* `DB_NAME`: The name of the database file.
* `REMOTE_SERVER`: The address of the remote server to push the repository to.
* `REMOTE_DIR`: The directory on the remote server to push the repository to.
