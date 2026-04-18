Userland non-daemon SSH server that inherits the current user privileges and serves whatever that user can access, with shell, exec, SFTP, and TCP forwarding on POSIX systems.

## Novelty

This project is intentionally positioned between `telnetd`-style ad hoc sharing and a full system `sshd` deployment.

What is novel here is the combination of:

- userland operation with no system daemon setup
- current-user privilege inheritance instead of a separate service account model
- quick startup for temporary ad hoc sharing
- one small foreground server process
- shell, exec, SFTP, local forwarding, and reverse forwarding together
- real SSH transport and host keys instead of telnet

In other words, this is not trying to replace a hardened multi-user OpenSSH installation. It is trying to provide a quick, dirty, but still encrypted SSH sharing tool for scenarios where `telnetd` would historically have been used.

## Installation

```bash
pip install sshshare
```

## Usage

Start the server:

```bash
ssh-keygen -t ed25519 -f host_ed25519
sshserve --username alice --password secret --host 127.0.0.1 --port 2222 --host-key host_ed25519
```

Connect from another terminal with SFTP:

```bash
sftp -P 2222 alice@127.0.0.1
```

Open an interactive shell over SSH:

```bash
ssh -p 2222 alice@127.0.0.1
```

Run a remote command:

```bash
ssh -p 2222 alice@127.0.0.1 'pwd && ls'
```

Use local port forwarding through the server:

```bash
ssh -L 8080:127.0.0.1:80 -p 2222 alice@127.0.0.1
```

Use remote port forwarding through the server:

```bash
ssh -R 9090:127.0.0.1:90 -p 2222 alice@127.0.0.1
```

Use a passphrase-protected Ed25519 host key:

```bash
ssh-keygen -t ed25519 -f host_ed25519
sshserve --username alice --password secret --host 127.0.0.1 --port 2222 --host-key host_ed25519 --host-key-passphrase your-passphrase
```

Notes:

- The SFTP root is the real filesystem root `/`.
- Accessible files and directories are whatever the current OS user running `sshserve` can access.
- Shell sessions and exec commands inherit the current user privileges.
- Shell sessions and exec commands start in the current working directory of the `sshserve` process.
- Password authentication is supported and `--username` / `--password` are required.
- PTY shell access is supported on POSIX systems.
- Direct TCP forwarding and reverse TCP forwarding are supported.
- `--host-key` is required and must point to an Ed25519 private key file.
- This implementation is POSIX-only because process launching uses `ctypes-unicode-proclaunch` and PTY handling uses POSIX APIs.

## Contributing

Contributions are welcome! Please submit pull requests or open issues on the GitHub repository.

## License

This project is licensed under the [MIT License](LICENSE).
