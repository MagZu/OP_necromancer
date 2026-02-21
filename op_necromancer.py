#!/usr/bin/env python3
"""
OP Necromancer — bring C3 back from the dead

A GUI wizard that resurrects a Comma 3 by:
  1. Locating the device on your network (SSH)
  2. Verifying sunnypilot 0.10.1 is installed (the required base)
  3. Validating your chosen openpilot fork (must be 0.10.2)
  4. Cloning the fork + applying the C3 compatibility patch

Requirements: Python 3.8+, tkinter (stdlib), ssh/scp in PATH
"""

from __future__ import annotations

import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext
import tkinter as tk

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent.resolve()
C3_PATCH   = SCRIPT_DIR / "c3_patch.py"
PATCH_DIR  = SCRIPT_DIR / "patch_files"

CONFIG_FILE = Path.home() / ".op_necromancer_config.json"

DEFAULT_USER  = "comma"
PREREQ_VER    = "0.10.1"
PREREQ_REMOTE = "sunnypilot/sunnypilot"
SUPPORTED_VER = "0.10.2"

# ── Dark necromancer colour palette ───────────────────────────────────────────
BG    = "#1a1025"   # near-black purple background
HDR   = "#0e0818"   # darker header strip
CARD  = "#2a1a3e"   # input field background
SEP   = "#4a2c6e"   # divider lines
FG    = "#e0d6f0"   # main text (slightly purple-white)
MUTED = "#7c6a9a"   # dimmed / secondary text
ACC   = "#c084fc"   # lavender purple — active elements
OK    = "#86efac"   # green — success
ERR   = "#f87171"   # red — error
WARN  = "#fb923c"   # orange — warning


# ═══════════════════════════════════════════════════════════════════════════════
# Network / SSH helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _ssh_opts(key: str, timeout: int = 10) -> list[str]:
    return [
        "-i", key,
        "-o", "StrictHostKeyChecking=no",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={timeout}",
        "-o", "ServerAliveInterval=30",
    ]


def _probe_port22(host: str, timeout: float = 2.5) -> str | None:
    """Return resolved IP if port 22 is reachable, else None."""
    try:
        infos = socket.getaddrinfo(host, 22, socket.AF_INET, socket.SOCK_STREAM)
        ip = infos[0][4][0]
        with socket.create_connection((ip, 22), timeout=timeout):
            return ip
    except Exception:
        return None


def discover_c3(timeout: float = 3.0) -> str | None:
    """Probe known Comma 3 addresses on port 22. Returns first found IP."""
    candidates = ["comma.local", "192.168.0.30", "192.168.43.1", "10.0.0.2"]
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(_probe_port22, h, timeout): h for h in candidates}
        for fut in as_completed(futs, timeout=timeout + 1):
            try:
                result = fut.result()
                if result:
                    return result
            except Exception:
                pass
    return None


def _load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text())
    except Exception:
        return {}


def _save_config(data: dict) -> None:
    try:
        # Merge with existing so we never lose keys we don't touch
        existing = _load_config()
        existing.update(data)
        CONFIG_FILE.write_text(json.dumps(existing, indent=2))
    except Exception:
        pass  # config is best-effort, never fatal


def ssh_test(host: str, user: str, key: str) -> tuple[bool, str | None]:
    """Returns (True, None) on success or (False, error_msg) on failure."""
    try:
        r = subprocess.run(
            ["ssh"] + _ssh_opts(key, 10) + [f"{user}@{host}", "echo ok"],
            capture_output=True, text=True, timeout=15,
        )
        if "ok" in r.stdout:
            return True, None
        return False, r.stderr.strip() or "No response from device"
    except subprocess.TimeoutExpired:
        return False, "Connection timed out"
    except FileNotFoundError:
        return False, "'ssh' command not found — install OpenSSH"
    except Exception as e:
        return False, str(e)


def check_prereqs(host: str, user: str, key: str) -> dict:
    """
    SSH to the device and check sunnypilot 0.10.1 + C3 patch status.
    Returns a dict with keys: error?, origin, log_line, is_sunnypilot,
                              version_ok, c3patch_installed
    """
    cmd = (
        "if [ ! -d /data/openpilot ]; then echo NO_DIR; exit 0; fi; "
        "cd /data/openpilot && "
        'echo "ORIGIN=$(git remote get-url origin 2>/dev/null)" && '
        'echo "LOG=$(git log --oneline -1 2>/dev/null)" && '
        'echo "PATCH=$(test -d /data/c3_backup && echo INSTALLED || echo CLEAN)"'
    )
    try:
        r = subprocess.run(
            ["ssh"] + _ssh_opts(key) + [f"{user}@{host}", cmd],
            capture_output=True, text=True, timeout=25,
        )
    except subprocess.TimeoutExpired:
        return {"error": "SSH timed out during inspection"}
    except Exception as e:
        return {"error": str(e)}

    out = r.stdout
    if "NO_DIR" in out:
        return {"error": "/data/openpilot not found — is sunnypilot installed on the C3?"}

    origin = log_line = patch_raw = ""
    for line in out.splitlines():
        if line.startswith("ORIGIN="):
            origin = line[7:].strip()
        elif line.startswith("LOG="):
            log_line = line[4:].strip()
        elif line.startswith("PATCH="):
            patch_raw = line[6:].strip()

    return {
        "origin":           origin,
        "log_line":         log_line,
        "is_sunnypilot":    PREREQ_REMOTE in origin,
        "version_ok":       PREREQ_VER in log_line,
        "c3patch_installed": patch_raw == "INSTALLED",
    }


def validate_repo(url: str, branch: str) -> tuple[bool, str]:
    """
    Fetch release.md from the GitHub repo on the given branch and confirm
    that SUPPORTED_VER (0.10.2) appears in it.
    Returns (ok, message).
    """
    url = url.strip()
    m = re.match(r"https?://github\.com/([^/]+)/([^/\s]+?)(?:\.git)?/?$", url)
    if not m:
        return False, "Enter a GitHub URL in the form  https://github.com/owner/repo"

    owner, repo = m.group(1), m.group(2)

    for fname in ("release.md", "RELEASE.md", "RELEASES.md"):
        raw = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{fname}"
        try:
            req = urllib.request.Request(raw, headers={"User-Agent": "op-necromancer/1.0"})
            with urllib.request.urlopen(req, timeout=12) as resp:
                content = resp.read().decode("utf-8", errors="replace")
            if SUPPORTED_VER in content:
                return True, f"✓  The grimoire speaks of version {SUPPORTED_VER} — spell is compatible."
            versions = re.findall(r"0\.\d+\.\d+", content)
            ver_str = versions[0] if versions else "unknown"
            return False, (
                f"This grimoire contains a version {ver_str} spell — incompatible.\n"
                f"OP Necromancer currently only supports openpilot {SUPPORTED_VER}."
            )
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue   # try next filename variant
            return False, f"The ancient archives returned HTTP {e.code} — check your URL."
        except Exception as e:
            return False, f"Could not consult the archives: {e}"

    return False, f"No release.md found in {owner}/{repo} at branch '{branch}'"


# ═══════════════════════════════════════════════════════════════════════════════
# Installation (runs in a background thread)
# ═══════════════════════════════════════════════════════════════════════════════

def run_install(
    host: str,
    user: str,
    key: str,
    repo_url: str,
    branch: str,
    log_cb,              # callable(str) — called from worker thread
    done_cb,             # callable(bool, str) — UI must schedule via after()
    dry_run: bool = False,
) -> None:
    """Launch the full installation (or dry-run scry) in a daemon thread."""

    def _run():
        def log(msg: str):
            log_cb(msg + "\n")

        dr = "  [DRY RUN] " if dry_run else "  "

        log("☠" * 30)
        if dry_run:
            log("  OP Necromancer — Scrying the Ritual (DRY RUN)")
            log("  No changes will be made to the C3.")
        else:
            log("  OP Necromancer — Resurrection Ritual")
        log(f"  C3 Host : {host}")
        log(f"  Grimoire: {repo_url}")
        log(f"  Branch  : {branch}")
        log("☠" * 30)

        ssh_base = ["ssh"] + _ssh_opts(key) + [f"{user}@{host}"]

        # ── 1. Silence the C3 ────────────────────────────────────────────────
        log("\n[1/4]  Silencing openpilot (stopping the service) ...")
        if dry_run:
            log(f"{dr}Would run: sudo systemctl stop comma.service")
        else:
            subprocess.run(
                ssh_base + ["sudo systemctl stop comma.service 2>/dev/null || true"],
                capture_output=True,
            )
            log("  Done — the C3 is quiet.")

        # ── 2. Entomb the old install ─────────────────────────────────────────
        log("\n[2/4]  Entombing the existing sunnypilot install ...")
        if dry_run:
            log(f"{dr}Would rename /data/openpilot  →  /data/sunnypilot_entombed_<timestamp>")
        else:
            r = subprocess.run(ssh_base + ["date +%Y%m%d_%H%M%S"],
                               capture_output=True, text=True)
            ts = r.stdout.strip() or "entombed"
            tomb_path = f"/data/sunnypilot_entombed_{ts}"
            rename_cmd = (
                f"if [ -d /data/openpilot ]; then "
                f"  mv /data/openpilot {tomb_path} && echo ENTOMBED; "
                f"else echo EMPTY; fi"
            )
            r = subprocess.run(ssh_base + [rename_cmd], capture_output=True, text=True)
            if "ENTOMBED" in r.stdout:
                log(f"  /data/openpilot  →  {tomb_path}")
            else:
                log("  No existing /data/openpilot found — skipping.")

        # ── 3. Channel the new spell (git clone) ─────────────────────────────
        log(f"\n[3/4]  Channeling the spell from the grimoire ...")
        log(f"  Cloning {repo_url}  (branch: {branch})")
        if dry_run:
            log(f"{dr}Would run: git clone --recurse-submodules -b {branch} {repo_url} /data/openpilot")
            log(f"{dr}Submodules would be initialised recursively after clone.")
        else:
            log("  This may take several minutes — do not interrupt the ritual.\n")
            clone_cmd = (
                f"cd /data && "
                f"git clone --recurse-submodules --progress "
                f"-b {branch} {repo_url} openpilot 2>&1"
            )
            proc = subprocess.Popen(
                ssh_base + [clone_cmd],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                log("  " + line.rstrip())
            proc.wait()
            if proc.returncode != 0:
                done_cb(False, "The channeling failed — git clone returned an error.\nCheck the ritual log for details.")
                return
            log("\n  ✓  Spell channeled successfully.")

        # ── 4. Cast the curse (apply patches) ────────────────────────────────
        if dry_run:
            log("\n[4/4]  Scrying the curse — checking current patch status ...")
        else:
            log("\n[4/4]  Casting the curse — applying C3 compatibility patches ...")

        if not C3_PATCH.exists():
            done_cb(False, f"c3_patch.py not found at {C3_PATCH}\nCannot proceed.")
            return

        if dry_run:
            # --check: read-only inspection of current patch state, no changes
            patch_args = ["--host", host, "--user", user, "--key", key, "--check"]
            log("  Reading the omens — inspecting current patch status on the C3 ...\n")
        else:
            patch_args = ["--host", host, "--user", user, "--key", key,
                          "--op-dir", "/data/openpilot"]

        patch_proc = subprocess.Popen(
            [sys.executable, str(C3_PATCH)] + patch_args,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
            cwd=str(C3_PATCH.parent),
        )
        assert patch_proc.stdout is not None
        for line in patch_proc.stdout:
            log(line.rstrip())
        patch_proc.wait()
        if patch_proc.returncode != 0 and not dry_run:
            done_cb(False, "The curse could not be completed — patch script failed.\nCheck the ritual log for details.")
            return

        if dry_run:
            log("\n" + "☠" * 30)
            log("  ☠  DRY RUN COMPLETE — The omens have been read.")
            log("  No changes were made to the C3.")
            log("  Uncheck 'dry run' and cast the curse to perform the real ritual.")
            log("☠" * 30)
            done_cb(True, "The omens have been read.\nNo changes were made to the C3.\n\nUncheck dry run and cast the curse to begin the real ritual.")
        else:
            log("\n" + "☠" * 30)
            log("  ✓  THE C3 HAS BEEN RESURRECTED!")
            log(f"  Reboot your C3 to complete the ritual:")
            log(f"    ssh comma@{host} 'sudo reboot'")
            log("☠" * 30)
            done_cb(True, "The C3 has been resurrected!\n\nReboot it now to complete the ritual.")

    threading.Thread(target=_run, daemon=True).start()


# ═══════════════════════════════════════════════════════════════════════════════
# GUI
# ═══════════════════════════════════════════════════════════════════════════════

STEP_NAMES = ["I · Locate", "II · Inspect", "III · Grimoire", "IV · Resurrect"]


class OPNecromancer(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("OP Necromancer")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.geometry("760x660")

        cfg = _load_config()

        self._step:           int  = 0
        self._host            = tk.StringVar(value=cfg.get("last_host", ""))
        self._key_path        = tk.StringVar(value=cfg.get("ssh_key") or self._default_key())
        self._repo_url        = tk.StringVar()
        self._branch          = tk.StringVar(value="nap-alpha")
        self._repo_validated: bool = False
        self._dry_run         = tk.BooleanVar(value=False)
        self._log_q:          queue.Queue[str] = queue.Queue()

        # Reset repo validation if URL or branch changes
        self._repo_url.trace_add("write", lambda *_: self._reset_repo_valid())
        self._branch.trace_add("write",   lambda *_: self._reset_repo_valid())

        self._build_ui()
        self._show_step(0)
        self.after(50, self._drain_log)

    # ── helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _default_key() -> str:
        ssh_dir = Path.home() / ".ssh"
        for name in ("id_ed25519", "id_rsa", "id_ecdsa", "id_dsa"):
            p = ssh_dir / name
            if p.exists():
                return str(p)
        return str(ssh_dir / "id_rsa")

    def _reset_repo_valid(self):
        self._repo_validated = False
        if self._step == 2:
            self._btn_next.configure(state="disabled")

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Header ────────────────────────────────────────────────────────────
        hdr = tk.Frame(self, bg=HDR, pady=10)
        hdr.pack(fill="x")
        tk.Label(hdr, text="☠  OP Necromancer  ☠",
                 bg=HDR, fg=ACC, font=("TkDefaultFont", 15, "bold")).pack()
        tk.Label(hdr, text="bring C3 back from the dead",
                 bg=HDR, fg=MUTED, font=("TkDefaultFont", 9, "italic")).pack()

        # ── Step indicator ────────────────────────────────────────────────────
        step_row = tk.Frame(self, bg=BG, pady=6)
        step_row.pack(fill="x", padx=16)
        self._step_lbls: list[tk.Label] = []
        for name in STEP_NAMES:
            lbl = tk.Label(step_row, text=name, bg=BG,
                           font=("TkDefaultFont", 10), padx=14, pady=3)
            lbl.pack(side="left")
            self._step_lbls.append(lbl)

        tk.Frame(self, bg=SEP, height=1).pack(fill="x")

        # ── Page container ────────────────────────────────────────────────────
        self._container = tk.Frame(self, bg=BG)
        self._container.pack(fill="both", expand=True, padx=24, pady=14)

        self._pages = [
            self._page_locate(self._container),
            self._page_inspect(self._container),
            self._page_grimoire(self._container),
            self._page_resurrect(self._container),
        ]

        # ── Navigation bar ────────────────────────────────────────────────────
        tk.Frame(self, bg=SEP, height=1).pack(fill="x")
        nav = tk.Frame(self, bg=HDR, pady=8)
        nav.pack(fill="x")
        self._btn_back = self._mk_btn(nav, "← Retreat", self._go_back,
                                      bg="#3a2a4e", fg=FG)
        self._btn_back.pack(side="left", padx=16)
        self._btn_next = self._mk_btn(nav, "Proceed →", self._go_next,
                                      bg=ACC, fg="#0e0818", bold=True)
        self._btn_next.pack(side="right", padx=16)

    def _mk_btn(self, parent, text, cmd, bg=ACC, fg="#0e0818", bold=False):
        font = ("TkDefaultFont", 10, "bold") if bold else ("TkDefaultFont", 10)
        return tk.Button(parent, text=text, command=cmd,
                         bg=bg, fg=fg, relief="flat",
                         font=font, padx=12, pady=4, cursor="hand2",
                         activebackground=bg, activeforeground=fg)

    def _show_step(self, step: int):
        self._step = step
        # Step bar styling
        for i, lbl in enumerate(self._step_lbls):
            if i < step:
                lbl.configure(fg=OK, font=("TkDefaultFont", 10))
            elif i == step:
                lbl.configure(fg=ACC, font=("TkDefaultFont", 10, "bold"))
            else:
                lbl.configure(fg=MUTED, font=("TkDefaultFont", 10))
        # Show only active page
        for i, page in enumerate(self._pages):
            if i == step:
                page.pack(fill="both", expand=True)
            else:
                page.pack_forget()
        # Nav button states
        self._btn_back.configure(state="normal" if step > 0 else "disabled")
        if step == 0:
            self._btn_next.configure(text="Proceed →", state="normal")
        elif step == 1:
            # Enabled only after prereqs pass
            self._btn_next.configure(text="Proceed →", state="disabled")
        elif step == 2:
            # Enabled only after grimoire validated
            self._btn_next.configure(
                text="Begin Ritual ▶",
                state="normal" if self._repo_validated else "disabled")
        else:
            # Install page — in-page button is used instead
            self._btn_next.configure(text="Begin Ritual ▶", state="disabled")
        # Populate install summary and toggle banners when entering step 3
        if step == 3:
            self._install_summary.configure(
                text=(
                    f"Target  : {self._host.get()}\n"
                    f"Grimoire: {self._repo_url.get()}\n"
                    f"Branch  : {self._branch.get()}"
                )
            )
            if self._dry_run.get():
                self._warn_banner.pack_forget()
                self._dryrun_banner.pack(fill="x", pady=(0, 8), before=self._install_summary)
                self._cast_btn.configure(text="scry the ritual (dry run)", bg="#1a3a5c", fg=ACC)
            else:
                self._dryrun_banner.pack_forget()
                self._warn_banner.pack(fill="x", pady=(0, 8), before=self._install_summary)
                self._cast_btn.configure(text="cast curse (start patching)", bg=ACC, fg="#0e0818")

    def _go_back(self):
        if self._step > 0:
            self._show_step(self._step - 1)

    def _go_next(self):
        if self._step == 0:
            self._do_connect()
        elif self._step == 1:
            self._show_step(2)
        elif self._step == 2:
            self._show_step(3)

    # ── Page I: Locate ────────────────────────────────────────────────────────

    def _page_locate(self, parent) -> tk.Frame:
        f = tk.Frame(parent, bg=BG)

        tk.Label(f, text="Locate the Fallen C3",
                 bg=BG, fg=FG, font=("TkDefaultFont", 14, "bold")).pack(anchor="w", pady=(0, 4))
        tk.Label(f, text="The necromancer must find the device before the ritual can begin.",
                 bg=BG, fg=MUTED, font=("TkDefaultFont", 9, "italic")).pack(anchor="w", pady=(0, 14))

        # IP address row
        r1 = tk.Frame(f, bg=BG)
        r1.pack(fill="x", pady=5)
        tk.Label(r1, text="C3 IP Address:", bg=BG, fg=FG,
                 width=20, anchor="w").pack(side="left")
        tk.Entry(r1, textvariable=self._host, bg=CARD, fg=FG,
                 insertbackground=FG, relief="flat",
                 font=("TkFixedFont", 11), width=20).pack(side="left", padx=4)
        self._mk_btn(r1, "Seek ☠", self._do_discover,
                     bg="#3a2a4e", fg=ACC).pack(side="left", padx=4)

        # SSH key row
        r2 = tk.Frame(f, bg=BG)
        r2.pack(fill="x", pady=5)
        tk.Label(r2, text="Necromancer's Key (SSH):", bg=BG, fg=FG,
                 width=20, anchor="w").pack(side="left")
        tk.Entry(r2, textvariable=self._key_path, bg=CARD, fg=FG,
                 insertbackground=FG, relief="flat",
                 font=("TkFixedFont", 10), width=36).pack(side="left", padx=4)
        self._mk_btn(r2, "Browse…", self._browse_key,
                     bg="#3a2a4e", fg=FG).pack(side="left", padx=4)

        tk.Label(f,
                 text="Provide the SSH private key that grants access to the C3 (comma user).",
                 bg=BG, fg=MUTED, font=("TkDefaultFont", 9)).pack(anchor="w", pady=(4, 0))

        self._conn_lbl = tk.Label(f, text="", bg=BG, fg=FG,
                                  font=("TkFixedFont", 10), anchor="w",
                                  justify="left", wraplength=680)
        self._conn_lbl.pack(anchor="w", pady=(16, 0))

        # Dry run toggle
        tk.Frame(f, bg=SEP, height=1).pack(fill="x", pady=(18, 8))
        tk.Checkbutton(
            f,
            text="☠  Dry run — scry the ritual without making any changes to the C3",
            variable=self._dry_run,
            bg=BG, fg=MUTED, selectcolor=CARD,
            activebackground=BG, activeforeground=ACC,
            font=("TkDefaultFont", 9),
        ).pack(anchor="w")
        return f

    def _do_discover(self):
        self._conn_lbl.configure(
            text="Searching the network for a fallen C3…", fg=MUTED)
        self._btn_next.configure(state="disabled")
        self.update_idletasks()

        def _worker():
            ip = discover_c3()
            self.after(0, lambda: self._on_discover(ip))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_discover(self, ip: str | None):
        if ip:
            self._host.set(ip)
            _save_config({"last_host": ip})
            self._conn_lbl.configure(
                text=f"☠  A Comma 3 stirs at {ip} — its soul awaits.", fg=OK)
        else:
            self._conn_lbl.configure(
                text="No C3 detected on the network.\nEnter its IP address manually to proceed.",
                fg=WARN)
        self._btn_next.configure(state="normal")

    def _browse_key(self):
        path = filedialog.askopenfilename(
            title="Select your SSH private key",
            initialdir=str(Path.home() / ".ssh"),
            filetypes=[("Private keys", "id_* *.pem *.key"), ("All files", "*.*")],
        )
        if path:
            self._key_path.set(path)
            _save_config({"ssh_key": path})

    def _do_connect(self):
        host = self._host.get().strip()
        key  = self._key_path.get().strip()
        if not host:
            self._conn_lbl.configure(
                text="The C3 has no known location — enter its IP address.", fg=ERR)
            return
        if not key or not Path(key).exists():
            self._conn_lbl.configure(
                text="Your key is missing — browse to your SSH private key file.", fg=ERR)
            return
        self._conn_lbl.configure(text="Forging the link to the C3…", fg=MUTED)
        self._btn_next.configure(state="disabled")
        self.update_idletasks()

        def _worker():
            ok, err = ssh_test(host, DEFAULT_USER, key)
            self.after(0, lambda: self._on_connect(ok, err))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_connect(self, ok: bool, err: str | None):
        if ok:
            _save_config({"last_host": self._host.get(), "ssh_key": self._key_path.get()})
            self._conn_lbl.configure(
                text=f"✓  The link is forged — C3 at {self._host.get()} responds.", fg=OK)
            self._show_step(1)
            self._run_prereqs()
        else:
            self._conn_lbl.configure(
                text=f"✗  The link failed: {err}", fg=ERR)
            self._btn_next.configure(state="normal")

    # ── Page II: Inspect ──────────────────────────────────────────────────────

    def _page_inspect(self, parent) -> tk.Frame:
        f = tk.Frame(parent, bg=BG)

        tk.Label(f, text="Inspect the C3",
                 bg=BG, fg=FG, font=("TkDefaultFont", 14, "bold")).pack(anchor="w", pady=(0, 4))
        tk.Label(f,
                 text=f"The C3 must be running sunnypilot {PREREQ_VER} before resurrection is possible.",
                 bg=BG, fg=MUTED, font=("TkDefaultFont", 9, "italic")).pack(anchor="w", pady=(0, 14))

        self._prereq_icons: dict[str, tk.Label] = {}
        checks = [
            ("ssh",     "SSH link to C3 is alive",                             True),
            ("dir",     "openpilot directory exists at /data/openpilot",        True),
            ("sp",      f"Remote origin is {PREREQ_REMOTE}",                   True),
            ("ver",     f"Version {PREREQ_VER} is installed (required base)",   True),
        ]
        for key, text, _required in checks:
            row = tk.Frame(f, bg=BG)
            row.pack(fill="x", pady=3)
            icon = tk.Label(row, text="○", bg=BG, fg=MUTED,
                            font=("TkFixedFont", 12), width=3)
            icon.pack(side="left")
            tk.Label(row, text=text, bg=BG, fg=FG,
                     font=("TkDefaultFont", 10)).pack(side="left")
            self._prereq_icons[key] = icon

        # Informational divider + C3 patch status (does not block progression)
        tk.Frame(f, bg=SEP, height=1).pack(fill="x", pady=(8, 6))
        patch_row = tk.Frame(f, bg=BG)
        patch_row.pack(fill="x", pady=2)
        self._patch_icon = tk.Label(patch_row, text="○", bg=BG, fg=MUTED,
                                    font=("TkFixedFont", 12), width=3)
        self._patch_icon.pack(side="left")
        tk.Label(patch_row, text="C3 compatibility patch already installed (/data/c3_backup exists)",
                 bg=BG, fg=MUTED, font=("TkDefaultFont", 10, "italic")).pack(side="left")

        self._prereq_detail = tk.Label(f, text="", bg=BG, fg=MUTED,
                                       font=("TkFixedFont", 9), justify="left",
                                       wraplength=680, anchor="w")
        self._prereq_detail.pack(anchor="w", pady=(14, 0))

        self._prereq_msg = tk.Label(f, text="", bg=BG, fg=FG,
                                    font=("TkDefaultFont", 10), justify="left",
                                    wraplength=680, anchor="w")
        self._prereq_msg.pack(anchor="w", pady=(8, 0))

        # Maintenance button — only visible when C3 patch is already installed
        self._maint_btn = self._mk_btn(
            f, "☠  Open Maintenance (patch already installed)  ☠",
            self._open_maintenance, bg="#1a2a0a", fg=OK,
        )
        # not packed until c3_patched is confirmed in _on_prereqs
        return f

    def _set_icon(self, key: str, state: str):
        lbl = self._prereq_icons[key]
        icons = {"ok": ("✓", OK), "fail": ("✗", ERR),
                 "spin": ("…", ACC), "pending": ("○", MUTED)}
        text, color = icons.get(state, ("○", MUTED))
        lbl.configure(text=text, fg=color)

    def _run_prereqs(self):
        for k in self._prereq_icons:
            self._set_icon(k, "pending")
        self._prereq_detail.configure(text="")
        self._prereq_msg.configure(text="")
        self._btn_next.configure(state="disabled")
        self._set_icon("ssh", "spin")
        self.update_idletasks()

        host = self._host.get().strip()
        key  = self._key_path.get().strip()

        def _worker():
            result = check_prereqs(host, DEFAULT_USER, key)
            self.after(0, lambda: self._on_prereqs(result))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_prereqs(self, result: dict):
        if "error" in result:
            self._set_icon("ssh", "fail")
            self._patch_icon.configure(text="○", fg=MUTED)
            self._prereq_msg.configure(
                text=f"The inspection failed:\n{result['error']}", fg=ERR)
            return

        self._set_icon("ssh", "ok")
        self._set_icon("dir", "ok" if result.get("origin") else "fail")
        is_sp       = result.get("is_sunnypilot", False)
        ver_ok      = result.get("version_ok", False)
        c3_patched  = result.get("c3patch_installed", False)
        self._set_icon("sp",  "ok" if is_sp  else "fail")
        self._set_icon("ver", "ok" if ver_ok else "fail")

        # C3 patch icon — informational only, does not block
        if c3_patched:
            self._patch_icon.configure(text="✓", fg=OK)
            # Pack before the status message so it's always visible regardless of message length
            self._maint_btn.pack(anchor="w", pady=(10, 0), before=self._prereq_msg)
        else:
            self._patch_icon.configure(text="—", fg=MUTED)
            self._maint_btn.pack_forget()

        self._prereq_detail.configure(
            text=(
                f"Origin : {result.get('origin') or '(empty)'}\n"
                f"Commit : {result.get('log_line') or '(empty)'}"
            )
        )

        if is_sp and ver_ok:
            already = "  (C3 patch already present — will be refreshed)" if c3_patched else ""
            self._prereq_msg.configure(
                text=f"✓  The C3 is ready for resurrection — all conditions are met.{already}",
                fg=OK)
            self._btn_next.configure(state="normal")
        elif c3_patched:
            # Already running the patched fork — prereq checks are for fresh installs only
            self._prereq_msg.configure(
                text=(
                    "ℹ  These prereqs are for fresh installs only (expect sunnypilot 0.10.1 as base).\n"
                    "Your C3 is already running the patched fork — use Maintenance above\n"
                    "to verify the patch service or update to the latest commit."
                ),
                fg=ACC,
            )
        else:
            issues = []
            if not is_sp:
                issues.append(
                    f"Origin is not {PREREQ_REMOTE}  "
                    f"(found: {result.get('origin') or 'empty'})")
            if not ver_ok:
                issues.append(
                    f"Version {PREREQ_VER} not found in latest commit  "
                    f"(found: {result.get('log_line') or 'empty'})")
            self._prereq_msg.configure(
                text=(
                    "✗  The C3 is not yet ready for resurrection:\n" +
                    "".join(f"\n  •  {i}" for i in issues) +
                    f"\n\nInstall sunnypilot {PREREQ_VER} on the C3 first, then return."
                ),
                fg=ERR,
            )

    # ── Maintenance window ────────────────────────────────────────────────────

    def _open_maintenance(self):
        host = self._host.get().strip()
        key  = self._key_path.get().strip()

        win = tk.Toplevel(self)
        win.title("C3 Maintenance ☠")
        win.configure(bg=BG)
        win.geometry("720x520")
        win.resizable(False, False)

        # Header
        hdr = tk.Frame(win, bg=HDR, pady=8)
        hdr.pack(fill="x")
        tk.Label(hdr, text="☠  C3 Maintenance",
                 bg=HDR, fg=ACC, font=("TkDefaultFont", 13, "bold")).pack()
        tk.Label(hdr, text=f"Target: {host}",
                 bg=HDR, fg=MUTED, font=("TkDefaultFont", 9)).pack()

        # Buttons
        btn_row = tk.Frame(win, bg=BG, pady=10)
        btn_row.pack(fill="x", padx=20)
        verify_btn   = self._mk_btn(btn_row, "🔍  Verify Patch Service",
                                    lambda: _do_verify(), bg="#3a2a4e", fg=ACC)
        verify_btn.pack(side="left", padx=(0, 8))
        update_btn   = self._mk_btn(btn_row, "⬆  Update to Latest",
                                    lambda: _do_update(), bg="#3a2a4e", fg=ACC)
        update_btn.pack(side="left", padx=(0, 8))
        specific_btn = self._mk_btn(btn_row, "📜  Specific Commit…",
                                    lambda: _do_specific_commit(), bg="#3a2a4e", fg=ACC)
        specific_btn.pack(side="left")

        tk.Frame(win, bg=SEP, height=1).pack(fill="x", padx=20)

        # Maintenance log
        maint_log = scrolledtext.ScrolledText(
            win, bg=HDR, fg=FG, font=("TkFixedFont", 9),
            relief="flat", wrap="word", state="disabled",
        )
        maint_log.pack(fill="both", expand=True, padx=20, pady=(8, 16))
        maint_log.tag_configure("ok",   foreground=OK)
        maint_log.tag_configure("err",  foreground=ERR)
        maint_log.tag_configure("acc",  foreground=ACC)
        maint_log.tag_configure("warn", foreground=WARN)

        log_q: queue.Queue[str] = queue.Queue()

        def _append(text: str):
            log_q.put(text)

        def _drain():
            try:
                while True:
                    line = log_q.get_nowait()
                    maint_log.configure(state="normal")
                    lower = line.lower()
                    if "✓" in line or "complete" in lower or "reapplied" in lower:
                        tag = "ok"
                    elif "✗" in line or "fail" in lower or "error" in lower:
                        tag = "err"
                    elif "⚠" in line or "missing" in lower or "warning" in lower:
                        tag = "warn"
                    elif line.startswith("☠") or line.startswith("["):
                        tag = "acc"
                    else:
                        tag = None
                    maint_log.insert("end", line, tag) if tag else maint_log.insert("end", line)
                    maint_log.see("end")
                    maint_log.configure(state="disabled")
            except queue.Empty:
                pass
            if win.winfo_exists():
                win.after(50, _drain)

        win.after(50, _drain)

        def _set_btns(state: str):
            win.after(0, lambda: verify_btn.configure(state=state))
            win.after(0, lambda: update_btn.configure(state=state))
            win.after(0, lambda: specific_btn.configure(state=state))

        # ── Shared: stop service → run cmd → reapply patch ────────────────────
        def _run_checkout_and_patch(step_label: str, remote_cmd: str):
            """Runs in a daemon thread. Stops service, runs remote_cmd, reapplies patch."""
            ssh_base = ["ssh"] + _ssh_opts(key) + [f"{DEFAULT_USER}@{host}"]

            _append("[1/3]  Silencing openpilot...\n")
            subprocess.run(
                ssh_base + ["sudo systemctl stop comma.service 2>/dev/null || true"],
                capture_output=True,
            )

            _append(f"\n[2/3]  {step_label}...\n")
            proc = subprocess.Popen(
                ssh_base + [remote_cmd],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                _append("  " + line.rstrip() + "\n")
            proc.wait()
            if proc.returncode != 0:
                _append("\n✗  Failed — check the log above.\n")
                _set_btns("normal")
                return

            _append("\n✓  Done.\n")
            _append("\n[3/3]  Reapplying the C3 curse...\n")
            patch_proc = subprocess.Popen(
                [sys.executable, str(C3_PATCH), "--host", host, "--key", key],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
                cwd=str(C3_PATCH.parent),
            )
            assert patch_proc.stdout is not None
            for line in patch_proc.stdout:
                _append(line.rstrip() + "\n")
            patch_proc.wait()

            if patch_proc.returncode != 0:
                _append("\n✗  Patch reapplication failed — check the log above.\n")
            else:
                _append("\n" + "☠" * 20 + "\n")
                _append("  ✓  Curse reapplied — reboot the C3 to activate.\n")
                _append("☠" * 20 + "\n")
                win.after(0, lambda: messagebox.showinfo(
                    "Complete ☠",
                    "Done — the curse has been reapplied.\n\nReboot the C3 to activate.",
                    parent=win,
                ))
            _set_btns("normal")

        # ── Verify ────────────────────────────────────────────────────────────
        def _do_verify():
            _set_btns("disabled")
            _append("☠  Consulting the spirits — checking patch wards...\n\n")

            def _worker():
                cmd = (
                    "echo ENABLED=$(systemctl is-enabled c3_persist 2>/dev/null); "
                    "echo ACTIVE=$(systemctl is-active c3_persist 2>/dev/null); "
                    "echo BACKUP=$(test -d /data/c3_backup && echo YES || echo NO); "
                    "echo PANDA=$(test -f /data/c3_backup/panda/panda.bin.signed && echo YES || echo NO); "
                    "echo AGNOS=$(test -f /data/no_agnos_update && echo YES || echo NO)"
                )
                try:
                    r = subprocess.run(
                        ["ssh"] + _ssh_opts(key) + [f"{DEFAULT_USER}@{host}", cmd],
                        capture_output=True, text=True, timeout=15,
                    )
                    vals: dict[str, str] = {}
                    for line in r.stdout.splitlines():
                        if "=" in line:
                            k, v = line.split("=", 1)
                            vals[k.strip()] = v.strip()
                    checks = [
                        ("c3_persist.service enabled", vals.get("ENABLED") == "enabled"),
                        ("c3_persist.service active",  vals.get("ACTIVE")  == "active"),
                        ("/data/c3_backup exists",     vals.get("BACKUP")  == "YES"),
                        ("F4 panda firmware backup",   vals.get("PANDA")   == "YES"),
                        ("AGNOS update blocked",       vals.get("AGNOS")   == "YES"),
                    ]
                    for label, ok in checks:
                        _append(f"  {'✓' if ok else '✗'}  {label}\n")
                    if all(ok for _, ok in checks):
                        _append("\n✓  All wards are in place — the C3 is fully bound.\n")
                    else:
                        _append("\n⚠  Some wards are absent — re-running the installation may help.\n")
                except Exception as e:
                    _append(f"\n✗  Inspection failed: {e}\n")
                finally:
                    _set_btns("normal")

            threading.Thread(target=_worker, daemon=True).start()

        # ── Update to latest ──────────────────────────────────────────────────
        def _do_update():
            _set_btns("disabled")
            _append("☠  Channeling the latest incantations — updating openpilot...\n\n")
            threading.Thread(
                target=_run_checkout_and_patch,
                args=(
                    "Pulling latest commits from origin",
                    "cd /data/openpilot && "
                    "git submodule foreach --recursive git reset --hard 2>/dev/null; "
                    "git submodule foreach --recursive git clean -fd 2>/dev/null; "
                    "git pull --rebase --recurse-submodules --progress 2>&1",
                ),
                daemon=True,
            ).start()

        # ── Specific commit ───────────────────────────────────────────────────
        def _do_specific_commit():
            _set_btns("disabled")
            _append("\n☠  Consulting the chronicles — fetching commit history...\n")

            def _worker():
                # Fetch and list commits from current HEAD up to the remote tip
                cmd = (
                    "cd /data/openpilot && "
                    "git fetch origin --quiet 2>/dev/null; "
                    "echo CURRENT=$(git rev-parse --short HEAD); "
                    'echo "CURRENTLINE=$(git log -1 --format="%h %s" HEAD)"; '
                    "REMOTE=$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' "
                    "  2>/dev/null || echo 'origin/nap-alpha'); "
                    'git log HEAD..$REMOTE --format="%h %s" 2>/dev/null | head -60'
                )
                try:
                    r = subprocess.run(
                        ["ssh"] + _ssh_opts(key) + [f"{DEFAULT_USER}@{host}", cmd],
                        capture_output=True, text=True, timeout=30,
                    )
                    current_hash = current_line = ""
                    newer: list[tuple[str, str]] = []
                    for line in r.stdout.splitlines():
                        if line.startswith("CURRENT="):
                            current_hash = line[8:].strip()
                        elif line.startswith("CURRENTLINE="):
                            current_line = line[12:].strip()
                        elif line and not line.startswith("CURRENT"):
                            parts = line.split(" ", 1)
                            if len(parts) == 2:
                                newer.append((parts[0], parts[1]))

                    n = len(newer)
                    if current_hash:
                        _append(f"  Current : {current_line}\n")
                        _append(f"  Found   : {n} newer commit{'s' if n != 1 else ''} available.\n")
                    else:
                        _append("  ✗  Could not read commit history.\n")
                    win.after(0, lambda: _open_commit_picker(newer, current_line, current_hash))
                except Exception as e:
                    _append(f"\n✗  Failed to fetch commit list: {e}\n")
                    _set_btns("normal")

            threading.Thread(target=_worker, daemon=True).start()

        def _open_commit_picker(
            newer: list[tuple[str, str]],
            current_line: str,
            current_hash: str,
        ):
            _set_btns("normal")  # re-enable main buttons while picker is open

            picker = tk.Toplevel(win)
            picker.title("☠  Restore to Specific Commit")
            picker.configure(bg=BG)
            picker.geometry("700x420")
            picker.resizable(False, False)
            picker.grab_set()

            hdr2 = tk.Frame(picker, bg=HDR, pady=8)
            hdr2.pack(fill="x")
            tk.Label(hdr2, text="☠  Restore to Specific Commit",
                     bg=HDR, fg=ACC, font=("TkDefaultFont", 12, "bold")).pack()
            tk.Label(hdr2,
                     text="Select a commit — it will be checked out and the C3 curse reapplied.",
                     bg=HDR, fg=MUTED, font=("TkDefaultFont", 9, "italic")).pack()

            # Commit listbox
            lf = tk.Frame(picker, bg=BG)
            lf.pack(fill="both", expand=True, padx=16, pady=10)
            sb = tk.Scrollbar(lf)
            sb.pack(side="right", fill="y")
            lb = tk.Listbox(
                lf, bg=CARD, fg=FG, font=("TkFixedFont", 9),
                selectbackground=ACC, selectforeground="#0e0818",
                relief="flat", yscrollcommand=sb.set, activestyle="none",
            )
            lb.pack(side="left", fill="both", expand=True)
            sb.configure(command=lb.yview)

            # Newer commits (newest at top), then divider, then current
            for h, msg in newer:
                lb.insert("end", f"  {h}  {msg}")
            if newer:
                lb.insert("end", "  " + "─" * 68)
            curr_idx = lb.size()
            lb.insert("end", f"  ► {current_line}   ← you are here")
            lb.itemconfigure(curr_idx, fg=OK, selectforeground=OK)
            if newer:
                lb.selection_set(0)

            status_text = (
                f"  {len(newer)} newer commit{'s' if len(newer) != 1 else ''} above your current version."
                if newer else "✓  You are already on the latest commit."
            )
            tk.Label(picker, text=status_text, bg=BG,
                     fg=MUTED if newer else OK,
                     font=("TkDefaultFont", 9)).pack(anchor="w", padx=16)

            # Nav
            nav2 = tk.Frame(picker, bg=HDR, pady=8)
            nav2.pack(fill="x", side="bottom")

            def _confirm():
                sel = lb.curselection()
                if not sel:
                    messagebox.showwarning("Nothing selected",
                                           "Select a commit from the list first.", parent=picker)
                    return
                idx = sel[0]
                if idx >= len(newer):
                    messagebox.showinfo("Already there",
                                        "That is your current commit — nothing to do.", parent=picker)
                    return
                chosen_hash, chosen_msg = newer[idx]
                if not messagebox.askyesno(
                    "Confirm Ritual ☠",
                    f"Restore the C3 to:\n\n  {chosen_hash}  {chosen_msg}\n\n"
                    "The C3 curse will be reapplied afterwards.\n\nProceed?",
                    parent=picker,
                ):
                    return
                picker.destroy()
                _set_btns("disabled")
                _append(f"\n☠  Restoring to commit {chosen_hash}...\n\n")
                threading.Thread(
                    target=_run_checkout_and_patch,
                    args=(
                        f"Checking out {chosen_hash}",
                        f"cd /data/openpilot && "
                        f"git fetch origin --quiet 2>/dev/null; "
                        f"git submodule foreach --recursive git reset --hard 2>/dev/null; "
                        f"git submodule foreach --recursive git clean -fd 2>/dev/null; "
                        f"git checkout {chosen_hash} 2>&1 && "
                        f"git submodule update --init --recursive --force --progress 2>&1",
                    ),
                    daemon=True,
                ).start()

            self._mk_btn(nav2, "Restore to Selected  ☠", _confirm, bold=True).pack(side="right", padx=16)
            self._mk_btn(nav2, "Cancel", picker.destroy, bg="#3a2a4e", fg=FG).pack(side="left", padx=16)

    # ── Page III: Grimoire ────────────────────────────────────────────────────

    def _page_grimoire(self, parent) -> tk.Frame:
        f = tk.Frame(parent, bg=BG)

        tk.Label(f, text="Select Your Grimoire (Repository)",
                 bg=BG, fg=FG, font=("TkDefaultFont", 14, "bold")).pack(anchor="w", pady=(0, 4))
        tk.Label(f,
                 text=f"The grimoire must contain an openpilot {SUPPORTED_VER} spell — other versions are not yet supported.",
                 bg=BG, fg=MUTED, font=("TkDefaultFont", 9, "italic")).pack(anchor="w", pady=(0, 14))

        # Repo URL
        r1 = tk.Frame(f, bg=BG)
        r1.pack(fill="x", pady=5)
        tk.Label(r1, text="GitHub Repo URL:", bg=BG, fg=FG,
                 width=18, anchor="w").pack(side="left")
        tk.Entry(r1, textvariable=self._repo_url, bg=CARD, fg=FG,
                 insertbackground=FG, relief="flat",
                 font=("TkFixedFont", 10), width=46).pack(side="left", padx=4)

        # Branch
        r2 = tk.Frame(f, bg=BG)
        r2.pack(fill="x", pady=5)
        tk.Label(r2, text="Branch / Incantation:", bg=BG, fg=FG,
                 width=18, anchor="w").pack(side="left")
        tk.Entry(r2, textvariable=self._branch, bg=CARD, fg=FG,
                 insertbackground=FG, relief="flat",
                 font=("TkFixedFont", 10), width=26).pack(side="left", padx=4)

        self._mk_btn(f, "Inspect Grimoire ☠", self._do_validate,
                     bg="#3a2a4e", fg=ACC).pack(anchor="w", pady=10)

        self._repo_status = tk.Label(f, text="", bg=BG, fg=FG,
                                     font=("TkDefaultFont", 10),
                                     justify="left", wraplength=680, anchor="w")
        self._repo_status.pack(anchor="w")
        return f

    def _do_validate(self):
        url    = self._repo_url.get().strip()
        branch = self._branch.get().strip()
        if not url:
            self._repo_status.configure(
                text="A grimoire without a URL cannot be read.", fg=ERR)
            return
        if not branch:
            self._repo_status.configure(
                text="Name the branch — every spell needs an incantation.", fg=ERR)
            return
        self._repo_status.configure(text="Consulting the ancient archives…", fg=MUTED)
        self._btn_next.configure(state="disabled")
        self._repo_validated = False
        self.update_idletasks()

        def _worker():
            ok, msg = validate_repo(url, branch)
            self.after(0, lambda: self._on_validate(ok, msg))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_validate(self, ok: bool, msg: str):
        self._repo_validated = ok
        self._repo_status.configure(text=msg, fg=OK if ok else ERR)
        self._btn_next.configure(state="normal" if ok else "disabled")

    # ── Page IV: Resurrect ────────────────────────────────────────────────────

    def _page_resurrect(self, parent) -> tk.Frame:
        f = tk.Frame(parent, bg=BG)

        # Warning banner (real ritual)
        self._warn_banner = tk.Frame(f, bg="#3a1500", pady=7, padx=10)
        tk.Label(
            self._warn_banner,
            text="⚠   THE RITUAL IS IN PROGRESS — DO NOT RESTART OR POWER OFF YOUR C3   ⚠",
            bg="#3a1500", fg=WARN, font=("TkDefaultFont", 10, "bold"),
        ).pack()

        # Dry run banner (scry mode — shown instead of warning above)
        self._dryrun_banner = tk.Frame(f, bg="#0d1a33", pady=7, padx=10)
        tk.Label(
            self._dryrun_banner,
            text="☠   DRY RUN — Scrying the ritual only. No changes will be made to the C3.   ☠",
            bg="#0d1a33", fg=ACC, font=("TkDefaultFont", 10, "bold"),
        ).pack()

        # Ritual summary (filled dynamically by _show_step)
        self._install_summary = tk.Label(f, text="", bg=BG, fg=MUTED,
                                         font=("TkFixedFont", 9), justify="left")
        self._install_summary.pack(anchor="w", pady=(0, 6))

        # Ritual log
        self._log = scrolledtext.ScrolledText(
            f, bg=HDR, fg=FG, font=("TkFixedFont", 9),
            relief="flat", wrap="word", height=15, state="disabled",
        )
        self._log.pack(fill="both", expand=True)
        self._log.tag_configure("ok",   foreground=OK)
        self._log.tag_configure("err",  foreground=ERR)
        self._log.tag_configure("warn", foreground=WARN)
        self._log.tag_configure("acc",  foreground=ACC)

        # Cast button
        self._cast_btn = tk.Button(
            f,
            text="cast curse (start patching)",
            command=self._do_install,
            bg=ACC, fg="#0e0818", relief="flat",
            font=("TkDefaultFont", 11, "bold"), pady=7, cursor="hand2",
            activebackground=ACC, activeforeground="#0e0818",
        )
        self._cast_btn.pack(pady=(8, 0))
        return f

    def _log_append(self, text: str):
        self._log.configure(state="normal")
        lower = text.lower()
        if "✓" in text or "resurrected" in lower or "complete" in lower:
            tag = "ok"
        elif "✗" in text or "error" in lower or "failed" in lower:
            tag = "err"
        elif "⚠" in text or "warning" in lower:
            tag = "warn"
        elif text.startswith("☠") or text.startswith("["):
            tag = "acc"
        else:
            tag = None
        self._log.insert("end", text, tag) if tag else self._log.insert("end", text)
        self._log.see("end")
        self._log.configure(state="disabled")

    def _drain_log(self):
        """Poll the log queue and push lines to the widget (main thread only)."""
        try:
            while True:
                line = self._log_q.get_nowait()
                self._log_append(line)
        except queue.Empty:
            pass
        self.after(50, self._drain_log)

    def _do_install(self):
        dry = self._dry_run.get()
        in_progress_text = "Reading the omens…" if dry else "The curse is being cast…"
        self._cast_btn.configure(state="disabled", text=in_progress_text)
        host   = self._host.get().strip()
        key    = self._key_path.get().strip()
        url    = self._repo_url.get().strip()
        branch = self._branch.get().strip()

        def log_cb(line: str):
            self._log_q.put(line)

        def done_cb(ok: bool, msg: str):
            self.after(0, lambda: self._on_install_done(ok, msg))

        run_install(host, DEFAULT_USER, key, url, branch, log_cb, done_cb, dry_run=dry)

    def _on_install_done(self, ok: bool, msg: str):
        dry = self._dry_run.get()
        if ok:
            if dry:
                self._cast_btn.configure(text="☠  Omens read — dry run complete  ☠",
                                         bg="#1a3a5c", fg=ACC, state="normal")
                messagebox.showinfo("Dry Run Complete ☠", msg)
            else:
                self._cast_btn.configure(text="☠  The C3 has risen  ☠",
                                         bg=OK, fg="#0e0818", state="disabled")
                self._btn_next.configure(text="✓  Done", state="normal",
                                         command=self.destroy)
                messagebox.showinfo("Resurrection Complete ☠", msg)
        else:
            retry_text = "scry the ritual (retry)" if dry else "cast curse (retry)"
            self._cast_btn.configure(text=retry_text, state="normal", bg=ERR, fg=FG)
            messagebox.showerror("The ritual failed ✗", msg)


# ═══════════════════════════════════════════════════════════════════════════════

def main():
    app = OPNecromancer()
    app.mainloop()


if __name__ == "__main__":
    main()
