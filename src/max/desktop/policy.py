"""Desktop command policy: what the local control server will and won't run.

Three fixed tiers, decided here and nowhere else:

``safe``     read-only, workspace-bounded inspection. Runs immediately.
``approval`` anything that changes state. Runs only after a human grants a
             one-shot approval bound to the exact argv (``approvals.py``).
``denied``   privilege escalation, host/system alteration, sandbox escape and
             bulk network egress. Refused even with an approval.

The tier never depends on *who* asked: model output cannot talk a command
into a different tier, and a grant issued for one argv never authorises a
different one. Every path argument must stay inside the configured workspace,
so the boundary is a filesystem fact rather than a promise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_ARGS = 32
MAX_ARG_LEN = 500
MAX_TOTAL_LEN = 2000

# Same belt-and-braces rule the coding worker uses: argv is passed without a
# shell, but anything that *looks* like it wants a shell is refused outright.
_SHELL_METACHARS = re.compile(r"[;&|`$><\n\r\\]|\$\(")

# Privilege escalation, host/system alteration, sandbox escape, bulk egress.
# These are refused regardless of approval.
DENIED_PROGRAMS = frozenset(
    {
        # privilege escalation
        "sudo", "su", "doas", "pkexec", "sudoedit", "visudo", "passwd",
        "chpasswd", "chsh", "newgrp",
        # host lifecycle / services
        "shutdown", "reboot", "poweroff", "halt", "init", "telinit",
        "systemctl", "service", "initctl", "update-rc.d",
        # disks, mounts, irreversible destruction
        "mount", "umount", "fdisk", "parted", "mkfs", "mkswap", "dd",
        "shred", "wipefs", "blkid",
        # permission / link tricks that defeat the path boundary
        "chmod", "chown", "chgrp", "chattr", "lsattr", "ln", "link",
        # scheduled and background execution
        "crontab", "at", "batch", "anacron",
        # killing arbitrary processes
        "kill", "killall", "pkill", "pkillall",
        # container / privileged runtimes
        "docker", "podman", "nerdctl", "kubectl", "lxc", "nsenter", "unshare",
        # network egress and remote shells
        "ssh", "scp", "sftp", "ftp", "telnet", "rlogin", "rcp",
        "nc", "netcat", "ncat", "socat", "tftp", "nmap", "arping",
        "curl", "wget", "aria2c", "axel",
        # package managers reach the network and execute foreign code
        "pip", "pip3", "easy_install", "npm", "npx", "yarn", "pnpm",
        "gem", "go", "cargo", "apt", "apt-get", "dpkg", "yum", "dnf",
        "snap", "brew",
        # firewall / user administration
        "iptables", "nft", "ufw", "firewall-cmd",
        "useradd", "userdel", "usermod", "groupadd", "groupdel",
        # chaining helpers hand control back to a shell
        "xargs", "env", "setsid", "nohup", "screen", "tmux",
        # a shell is the escape hatch that defeats every rule above
        "sh", "bash", "zsh", "fish", "dash", "ksh", "csh", "tcsh", "ash", "busybox",
    }
)

# git subcommands that move data off the host or rewrite history.
DENIED_GIT_SUBCOMMANDS = frozenset(
    {"push", "pull", "fetch", "clone", "remote", "submodule",
     "request-pull", "send-email", "request-pull"}
)

# argv heads that are read-only and workspace-bounded. A command is ``safe``
# only when it starts with one of these exact prefixes.
SAFE_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("ls",), ("pwd",), ("echo",), ("cat",), ("head",), ("tail",), ("wc",),
    ("stat",), ("file",), ("du",), ("df",), ("which",), ("uname",),
    ("date",), ("whoami",), ("id",), ("tree",), ("grep",), ("rg",),
    ("sleep",),
    ("find",), ("sort",), ("uniq",), ("basename",), ("dirname",),
    ("realpath",), ("cut",), ("tr",), ("diff",), ("md5sum",), ("sha256sum",),
    ("python", "--version"), ("python", "-V"),
    ("python3", "--version"), ("python3", "-V"),
    ("python", "-m", "pytest"), ("python3", "-m", "pytest"),
    ("python", "-m", "unittest"), ("python3", "-m", "unittest"),
    ("git", "status"), ("git", "diff"), ("git", "log"), ("git", "show"),
    ("git", "ls-files"), ("git", "rev-parse"),
    ("git", "shortlog"), ("git", "blame"),
)

# ``find`` is safe only without the flags that mutate or execute.
_FIND_DANGEROUS = ("-delete", "-exec", "-execdir", "-ok", "-okdir",
                   "-fls", "-fprintf", "-fprint")

_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


@dataclass(frozen=True)
class Verdict:
    tier: str  # "safe" | "approval" | "denied"
    reason: str
    argv: tuple[str, ...] = ()


def _bad_path_token(token: str) -> str | None:
    """Reason ``token`` is not an in-workspace relative path, or ``None``."""
    if not token:
        return None
    if token.startswith("~"):
        return "home-relative paths are not permitted"
    if token.startswith("/") or _DRIVE.match(token):
        return "absolute paths are not permitted — only workspace-relative paths"
    normalised = token.replace("\\", "/")
    if any(part == ".." for part in normalised.split("/")):
        return "path traversal ('..') is not permitted"
    return None


def _path_reason(arg: str) -> str | None:
    """Path checks for a whole argument, including ``--flag=/abs/path``."""
    reason = _bad_path_token(arg)
    if reason:
        return reason
    if "=" in arg:
        value = arg.split("=", 1)[1]
        reason = _bad_path_token(value)
        if reason:
            return reason
    return None


def _basename(program: str) -> str:
    return program.replace("\\", "/").rsplit("/", 1)[-1]


def classify(command) -> Verdict:
    """Assign a fixed risk tier to an argv list. Never raises on bad input."""
    if isinstance(command, str):
        return Verdict("denied", "command must be an array of arguments, not a string")
    if not isinstance(command, (list, tuple)):
        return Verdict("denied", "command must be an array of arguments")
    if not command:
        return Verdict("denied", "empty command")
    if len(command) > MAX_ARGS:
        return Verdict("denied", f"too many arguments (limit {MAX_ARGS})")

    argv: list[str] = []
    for item in command:
        if not isinstance(item, str) or not item:
            return Verdict("denied", "every argument must be a non-empty string")
        if len(item) > MAX_ARG_LEN:
            return Verdict("denied", f"argument longer than {MAX_ARG_LEN} characters")
        argv.append(item)
    if sum(len(a) for a in argv) > MAX_TOTAL_LEN:
        return Verdict("denied", f"command longer than {MAX_TOTAL_LEN} characters")

    for arg in argv:
        if _SHELL_METACHARS.search(arg):
            return Verdict("denied", "shell metacharacters are not permitted")
        reason = _path_reason(arg)
        if reason:
            return Verdict("denied", reason)

    program = _basename(argv[0])
    if program in DENIED_PROGRAMS:
        return Verdict("denied", f"'{program}' is never permitted from desktop control")

    lowered = [a.lower() for a in argv]
    if program.startswith("python") and "-c" in lowered[:2]:
        return Verdict("denied", "inline python (-c) is never permitted")
    if program.startswith("python") and "-m" in lowered and \
            lowered[lowered.index("-m") + 1:lowered.index("-m") + 2] in (["pip"], ["ensurepip"]):
        return Verdict("denied", "python package installation is never permitted")

    if program == "git" and len(argv) > 1:
        sub = _basename(argv[1]).lower()
        if sub in DENIED_GIT_SUBCOMMANDS:
            return Verdict("denied", f"'git {sub}' moves data off the host or rewrites history")

    if program == "rm":
        flags = "".join(a[1:] for a in argv[1:] if a.startswith("-") and not a.startswith("--"))
        if "r" in flags and "f" in flags:
            return Verdict("denied", "'rm -rf' style recursive deletion is never permitted")
        if any(a in (".", "*", "/*") for a in argv[1:]):
            return Verdict("denied", "'rm' on a wildcard or the workspace root is never permitted")

    if program == "find":
        for arg in argv[1:]:
            if any(arg == bad or arg.startswith(bad + "=") for bad in _FIND_DANGEROUS):
                return Verdict(
                    "denied",
                    f"'find {arg.split('=')[0]}' mutates or executes — not permitted as read-only",
                )

    for prefix in SAFE_PREFIXES:
        head = [_basename(p) for p in prefix]
        if [program] + [_basename(a) for a in argv[1:len(head)]] == head:
            return Verdict("safe", "read-only command inside the workspace", tuple(argv))

    return Verdict(
        "approval",
        "this command changes state and needs human approval",
        tuple(argv),
    )
