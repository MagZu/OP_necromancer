#!/usr/bin/env python3
"""
c3_patch.py — Comma 3 Compatibility Patch Script

Patches openpilot on a Comma 3 device to restore C3 hardware support:
  - AR0231 camera sensor driver (restored to camerad build chain)
  - Panda support: DEPRECATED_DEVICES, bcd detection, HEALTH_PACKET_VERSION=16
  - AGNOS auto-update prevention (blocks softbricking the C3)
  - Persistent boot-time service that survives openpilot auto-updates

The script stores backup copies of all C3-specific files in /data/c3_backup/
(outside the openpilot git tree) so c3_persist.service can restore them
and trigger a camerad rebuild whenever openpilot auto-updates.

Usage:
  python3 c3_patch.py                              # apply all patches + rebuild
  python3 c3_patch.py --check                      # only show patch status
  python3 c3_patch.py --no-rebuild                 # patch but skip scons rebuild
  python3 c3_patch.py --no-persist                 # skip persistence service install
  python3 c3_patch.py --host 10.0.0.5              # override device IP
  python3 c3_patch.py --op-dir /data/openpilot_new # patch a specific clone
  python3 c3_patch.py --key ~/.ssh/id_ed25519       # override SSH key path
"""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# ── Default configuration ────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent.resolve()
SSH_KEY    = BASE_DIR / "openpilot_ssh" / "op_ssh"
PATCH_DIR  = BASE_DIR / "patch_files"   # self-contained patch sources

DEFAULT_HOST = "192.168.0.30"
DEFAULT_USER = "comma"
DEFAULT_OP   = "/data/openpilot"
BACKUP_DIR   = "/data/c3_backup"   # lives outside git tree, survives updates


# ── SSH helpers ──────────────────────────────────────────────────────────────

def ssh_base_opts() -> list[str]:
    return [
        "-i", str(SSH_KEY),
        "-o", "StrictHostKeyChecking=no",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=30",
    ]


def ssh_run(host: str, user: str, cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    full = ["ssh"] + ssh_base_opts() + [f"{user}@{host}", cmd]
    return subprocess.run(full, capture_output=True, text=True, check=check)


def scp_push(host: str, user: str, local: Path, remote: str) -> None:
    full = ["scp", "-q"] + ssh_base_opts() + [str(local), f"{user}@{host}:{remote}"]
    subprocess.run(full, check=True)


def device_python(host: str, user: str, script: str) -> str:
    """Upload and run a Python snippet on the device, return stdout."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(script)
        tmp = f.name
    try:
        scp_push(host, user, Path(tmp), "/tmp/_c3patch.py")
    finally:
        os.unlink(tmp)
    r = ssh_run(host, user, "python3 /tmp/_c3patch.py; rm -f /tmp/_c3patch.py")
    return r.stdout.strip()


# ── Step 1: connection ────────────────────────────────────────────────────────

def step_check_connection(host: str, user: str) -> None:
    print(f"\n[1/8] Testing SSH connection to {user}@{host} ...")
    try:
        SSH_KEY.chmod(0o600)
    except Exception:
        pass  # key may be owned by a different user or on a read-only fs
    r = ssh_run(host, user, "echo ok", check=False)
    if r.returncode != 0 or "ok" not in r.stdout:
        print(f"  ERROR: {r.stderr.strip() or 'no response'}")
        sys.exit(1)
    print("  OK")


# ── Step 2: detect openpilot ─────────────────────────────────────────────────

def step_detect_op(host: str, user: str) -> str:
    print("\n[2/8] Detecting openpilot installation ...")
    r = ssh_run(host, user,
                f"test -d {DEFAULT_OP} && echo {DEFAULT_OP} || "
                f"find /data -maxdepth 2 -name 'SConstruct' 2>/dev/null | head -1 | xargs dirname",
                check=False)
    op = r.stdout.strip() or DEFAULT_OP
    print(f"  openpilot at: {op}")
    return op


# ── Step 3: status check ─────────────────────────────────────────────────────

def step_check_status(host: str, user: str, op: str) -> dict:
    print("\n[3/8] Checking patch status ...")

    checks = {
        # Camera
        "camera: ar0231.cc":          f"test -f {op}/system/camerad/sensors/ar0231.cc && echo yes || echo no",
        "camera: ar0231_registers.h": f"test -f {op}/system/camerad/sensors/ar0231_registers.h && echo yes || echo no",
        "camera: sensor.h include":   f"grep -q 'ar0231_registers' {op}/system/camerad/sensors/sensor.h && echo yes || echo no",
        "camera: sensor.h class":     f"grep -q 'class AR0231' {op}/system/camerad/sensors/sensor.h && echo yes || echo no",
        "camera: spectra.cc":         f"grep -q 'new AR0231' {op}/system/camerad/cameras/spectra.cc && echo yes || echo no",
        "camera: SConscript":         f"grep -q 'ar0231.cc' {op}/system/camerad/SConscript && echo yes || echo no",
        # Panda
        "panda: backup dir":          f"test -d {BACKUP_DIR}/panda && echo yes || echo no",
        "panda: __init__ patched":    f"grep -q 'DEPRECATED_DEVICES' {op}/panda/python/__init__.py && echo yes || echo no",
        "panda: pandad.h patched":    f"grep -q 'DEPRECATED_PANDA_TYPES' {op}/selfdrive/pandad/pandad.h && echo yes || echo no",
        "panda: pandad.py patched":   f"grep -q 'DEPRECATED_DEVICES' {op}/selfdrive/pandad/pandad.py && echo yes || echo no",
        "panda: pandad.cc patched":   f"grep -q 'C3_SKIP_CANFD' {op}/selfdrive/pandad/pandad.cc && echo yes || echo no",
        # AGNOS
        "agnos: updated.py blocked":      f"grep -q 'C3_BLOCK_AGNOS' {op}/system/updated/updated.py && echo yes || echo no",
        "agnos: launch_chffrplus.sh":     f"grep -q 'C3_BLOCK_AGNOS' {op}/launch_chffrplus.sh && echo yes || echo no",
        "agnos: flag file":               f"test -f /data/no_agnos_update && echo yes || echo no",
        # AGNOS 12.8 compatibility (openpilot 0.10.2 on C3)
        "compat: amplifier.py tici":  f"grep -q '\"tici\"' {op}/openpilot/system/hardware/tici/amplifier.py && echo yes || echo no",
        "compat: jeepney":            "pip3 show jeepney >/dev/null 2>&1 && echo yes || echo no",
        "compat: kaitaistruct":       "pip3 show kaitaistruct >/dev/null 2>&1 && echo yes || echo no",
        # Persistence
        "persist: service installed": f"test -f /etc/systemd/system/c3_persist.service && echo yes || echo no",
    }

    status: dict[str, bool] = {}
    for label, cmd in checks.items():
        r = ssh_run(host, user, cmd, check=False)
        ok = r.stdout.strip() == "yes"
        status[label] = ok
        mark = "✓" if ok else "✗"
        print(f"  {mark}  {label}")

    return status


# ── Step 4a: camera patches ───────────────────────────────────────────────────

def step_patch_camera(host: str, user: str, op: str, status: dict) -> None:
    print("\n[4/8] Applying camera support patches ...")

    cam_backup = f"{BACKUP_DIR}/camera"
    ssh_run(host, user, f"mkdir -p {cam_backup}")

    # Push AR0231 files from sunnypilot and store to backup
    for fname in ("ar0231.cc", "ar0231_registers.h"):
        local = PATCH_DIR / fname
        op_dest = f"{op}/system/camerad/sensors/{fname}"
        bk_dest = f"{cam_backup}/{fname}"

        chk_key = f"camera: {fname.replace('_registers.h', '_registers.h').replace('.cc', '.cc')}"
        # map to status keys
        stat_key = "camera: ar0231.cc" if fname == "ar0231.cc" else "camera: ar0231_registers.h"

        if not status[stat_key]:
            print(f"  Copying {fname} ...")
            scp_push(host, user, local, op_dest)
        else:
            print(f"  {fname} already present")

        # Always refresh backup
        scp_push(host, user, local, bk_dest)

    # Patch sensor.h
    if not status["camera: sensor.h include"] or not status["camera: sensor.h class"]:
        print("  Patching sensor.h ...")
        out = device_python(host, user, f"""
import sys
path = "{op}/system/camerad/sensors/sensor.h"
bk   = "{cam_backup}/sensor.h.bk"
with open(path) as f:
    content = f.read()
changed = False

INC_MARKER = '#include "system/camerad/sensors/ox03c10_registers.h"'
AR_INC = '#include "system/camerad/sensors/ar0231_registers.h"'
if AR_INC not in content:
    content = content.replace(INC_MARKER, AR_INC + "\\n" + INC_MARKER)
    changed = True

CLASS_MARKER = "class OX03C10 : public SensorInfo {{"
AR0231_CLASS = '''class AR0231 : public SensorInfo {{
public:
  AR0231();
  std::vector<i2c_random_wr_payload> getExposureRegisters(int exposure_time, int new_exp_g, bool dc_gain_enabled) const override;
  float getExposureScore(float desired_ev, int exp_t, int exp_g_idx, float exp_gain, int gain_idx) const override;
  int getSlaveAddress(int port) const override;

private:
  mutable std::map<uint16_t, std::pair<int, int>> ar0231_register_lut;
}};

'''
if "class AR0231" not in content:
    content = content.replace(CLASS_MARKER, AR0231_CLASS + CLASS_MARKER)
    changed = True

if changed:
    with open(path, "w") as f:
        f.write(content)
    # save patched version as backup
    import shutil; shutil.copy(path, bk)
    print("sensor.h patched")
else:
    print("sensor.h already up-to-date")
""")
        print(f"  {out}")
    else:
        print("  sensor.h already patched")
        # Refresh backup anyway
        ssh_run(host, user,
                f"cp {op}/system/camerad/sensors/sensor.h {cam_backup}/sensor.h.bk")

    # Patch spectra.cc
    if not status["camera: spectra.cc"]:
        print("  Patching spectra.cc ...")
        out = device_python(host, user, f"""
import sys
path = "{op}/system/camerad/cameras/spectra.cc"
bk   = "{cam_backup}/spectra.cc.bk"
with open(path) as f:
    content = f.read()

if "new AR0231" in content:
    print("spectra.cc already patched")
    sys.exit(0)

# Try all known sensor orderings (varies across openpilot versions)
patterns = [
    # OX03C10 first (openpilot 0.10.2)
    ("if (!init_sensor_lambda(new OX03C10) &&\\n      !init_sensor_lambda(new OS04C10))",
     "if (!init_sensor_lambda(new AR0231) &&\\n      !init_sensor_lambda(new OX03C10) &&\\n      !init_sensor_lambda(new OS04C10))"),
    # OS04C10 first (earlier versions / sunnypilot)
    ("if (!init_sensor_lambda(new OS04C10) &&\\n      !init_sensor_lambda(new OX03C10))",
     "if (!init_sensor_lambda(new AR0231) &&\\n      !init_sensor_lambda(new OS04C10) &&\\n      !init_sensor_lambda(new OX03C10))"),
]
for old, new in patterns:
    if old in content:
        content = content.replace(old, new)
        with open(path, "w") as f:
            f.write(content)
        import shutil; shutil.copy(path, bk)
        print("spectra.cc patched")
        sys.exit(0)

print("ERROR: could not find sensor probe chain in spectra.cc")
sys.exit(1)
""")
        print(f"  {out}")
        if "ERROR" in out:
            print("  WARNING: spectra.cc patch failed — camera may not detect AR0231")
    else:
        print("  spectra.cc already patched")
        ssh_run(host, user,
                f"cp {op}/system/camerad/cameras/spectra.cc {cam_backup}/spectra.cc.bk")

    # Patch SConscript
    if not status["camera: SConscript"]:
        print("  Patching SConscript ...")
        out = device_python(host, user, f"""
import sys
path = "{op}/system/camerad/SConscript"
bk   = "{cam_backup}/SConscript.bk"
with open(path) as f:
    content = f.read()
if "ar0231.cc" in content:
    print("SConscript already patched")
    sys.exit(0)
OLD = "'sensors/ox03c10.cc'"
NEW = "'sensors/ar0231.cc', 'sensors/ox03c10.cc'"
if OLD not in content:
    print("ERROR: ox03c10.cc not found in SConscript")
    sys.exit(1)
content = content.replace(OLD, NEW)
with open(path, "w") as f:
    f.write(content)
import shutil; shutil.copy(path, bk)
print("SConscript patched")
""")
        print(f"  {out}")
    else:
        print("  SConscript already patched")
        ssh_run(host, user,
                f"cp {op}/system/camerad/SConscript {cam_backup}/SConscript.bk")


# ── Step 4b: panda patches ────────────────────────────────────────────────────

def step_patch_panda(host: str, user: str, op: str, status: dict) -> None:
    print("\n[5/8] Applying panda support patches ...")

    panda_backup = f"{BACKUP_DIR}/panda"
    ssh_run(host, user, f"mkdir -p {panda_backup}")

    # ── panda/__init__.py and pandad.py: copy wholesale from sunnypilot ─────────
    copy_files = [
        (
            PATCH_DIR / "panda_init.py",
            f"{panda_backup}/panda_init.py",
            f"{op}/panda/python/__init__.py",
            "panda: __init__ patched",
            "panda/__init__.py",
        ),
        (
            PATCH_DIR / "pandad.py",
            f"{panda_backup}/pandad_py.py",
            f"{op}/selfdrive/pandad/pandad.py",
            "panda: pandad.py patched",
            "pandad.py",
        ),
    ]

    for local, bk_dest, op_dest, stat_key, label in copy_files:
        scp_push(host, user, local, bk_dest)
        if not status[stat_key]:
            print(f"  Installing {label} (from sunnypilot) ...")
            ssh_run(host, user, f"cp {bk_dest} {op_dest}")
        else:
            print(f"  {label} already patched")

    # ── F4 panda firmware (panda.bin.signed / bootstub) ──────────────────────
    # Custom-compiled F4 firmware with SAFETY_TESLA_LEGACY (mode 36) support.
    # Compiled from openpilot panda source with STM32F4 target and health v16.
    # patch_files/panda.bin.signed is the authoritative source — always deploy it
    # so that pandad will reflash the panda with mode 36 support on next boot.
    fw_backup = f"{panda_backup}/panda.bin.signed"
    fw_dest   = f"{op}/panda/board/obj/panda.bin.signed"
    bs_backup = f"{panda_backup}/bootstub.panda.bin"
    bs_dest   = f"{op}/panda/board/obj/bootstub.panda.bin"

    ssh_run(host, user, f"mkdir -p $(dirname {fw_dest})")

    local_fw = PATCH_DIR / "panda.bin.signed"
    local_bs = PATCH_DIR / "bootstub.panda.bin"

    if local_fw.exists():
        print("  Installing F4 panda firmware (SAFETY_TESLA_LEGACY) from patch_files ...")
        ssh_run(host, user, f"test -f {fw_dest} && cp {fw_dest} {fw_backup} || true", check=False)
        scp_push(host, user, local_fw, fw_dest)
        scp_push(host, user, local_fw, fw_backup)
        print(f"  panda.bin.signed installed ({local_fw.stat().st_size} bytes, mode 36 included)")
    else:
        # Fallback: grab from old installs (may lack SAFETY_TESLA_LEGACY)
        print("  WARNING: patch_files/panda.bin.signed missing — falling back to old installs")
        fw_sources = [
            "/data/openpilot_sunnytinkla/panda/board/obj/panda.bin.signed",
            "/data/openpilot_old/panda/board/obj/panda.bin.signed",
            "/data/openpilot_tinkla/panda/board/obj/panda.bin.signed",
            "/data/openpilot_tinklaNord1/panda/board/obj/panda.bin.signed",
        ]
        for src in fw_sources:
            r = ssh_run(host, user, f"test -f {src} && echo yes || echo no", check=False)
            if r.stdout.strip() == "yes":
                ssh_run(host, user, f"cp {src} {fw_dest} && cp {src} {fw_backup}")
                print(f"  panda.bin.signed installed from fallback ({src}) — mode 36 may be absent")
                break
        else:
            print("  ERROR: panda.bin.signed not found anywhere — pandad will fail to flash panda")

    if local_bs.exists():
        print("  Installing F4 bootstub from patch_files ...")
        ssh_run(host, user, f"test -f {bs_dest} && cp {bs_dest} {bs_backup} || true", check=False)
        scp_push(host, user, local_bs, bs_dest)
        scp_push(host, user, local_bs, bs_backup)
        print("  bootstub.panda.bin installed")
    else:
        # Fallback for bootstub
        bs_sources = [
            "/data/openpilot_sunnytinkla/panda/board/obj/bootstub.panda.bin",
            "/data/openpilot_old/panda/board/obj/bootstub.panda.bin",
            "/data/openpilot_tinkla/panda/board/obj/bootstub.panda.bin",
            "/data/openpilot_tinklaNord1/panda/board/obj/bootstub.panda.bin",
        ]
        for src in bs_sources:
            r = ssh_run(host, user, f"test -f {src} && echo yes || echo no", check=False)
            if r.stdout.strip() == "yes":
                ssh_run(host, user, f"mkdir -p $(dirname {bs_dest}) && cp {src} {bs_dest} && cp {src} {bs_backup}")
                print(f"  bootstub.panda.bin installed from fallback ({src})")
                break
        else:
            print("  WARNING: bootstub.panda.bin not found — panda DFU recovery may fail")

    # ── pandad.h: targeted patch — only add DEPRECATED_PANDA_TYPES ───────────
    # Do NOT copy sunnypilot's pandad.h wholesale; its fetchCarParams() returns
    # vector<string> but openpilot 0.10.2's panda_safety.cc expects string →
    # compile error. We only need to inject the DEPRECATED_PANDA_TYPES list.
    if not status["panda: pandad.h patched"]:
        print("  Patching pandad.h (adding DEPRECATED_PANDA_TYPES) ...")
        out = device_python(host, user, f"""
import sys, shutil
path = "{op}/selfdrive/pandad/pandad.h"
bk   = "{panda_backup}/pandad_h.h"
with open(path) as f:
    content = f.read()
if "DEPRECATED_PANDA_TYPES" in content:
    print("pandad.h already patched")
    sys.exit(0)
OLD = "void pandad_main_thread(std::vector<std::string> serials);"
NEW = (OLD + "\\n\\n"
       "// deprecated devices (C3 uses BLACK_PANDA)\\n"
       "static const std::vector<cereal::PandaState::PandaType> DEPRECATED_PANDA_TYPES = {{\\n"
       "  cereal::PandaState::PandaType::WHITE_PANDA,\\n"
       "  cereal::PandaState::PandaType::GREY_PANDA,\\n"
       "  cereal::PandaState::PandaType::BLACK_PANDA,\\n"
       "  cereal::PandaState::PandaType::PEDAL,\\n"
       "  cereal::PandaState::PandaType::UNO,\\n"
       "  cereal::PandaState::PandaType::RED_PANDA_V2\\n"
       "}};")
if OLD not in content:
    print("ERROR: anchor not found in pandad.h")
    sys.exit(1)
content = content.replace(OLD, NEW)
with open(path, "w") as f:
    f.write(content)
shutil.copy(path, bk)
print("pandad.h patched")
""")
        print(f"  {out}")
    else:
        print("  pandad.h already patched")
        ssh_run(host, user,
                f"cp {op}/selfdrive/pandad/pandad.h {panda_backup}/pandad_h.h")

    # ── pandad.cc: fix BLACK_PANDA (F4) hang in control_read retry loop ──────
    # panda_comms.cc control_read() loops forever on LIBUSB_ERROR_PIPE (unsupported cmd).
    # If the sunnypilot F4 firmware doesn't support:
    #   0xe8 (set_can_fd_auto)  → connect() hangs
    #   0xc2 (get_can_state)    → send_panda_states() returns nullopt every time
    # Either way pandaStates never gets published → hardwared never sees ignition.
    if not status.get("panda: pandad.cc patched", False):
        print("  Patching pandad.cc (BLACK_PANDA CAN FD + get_can_state) ...")
        out = device_python(host, user, f"""
import sys, shutil
path = "{op}/selfdrive/pandad/pandad.cc"
bk   = "{panda_backup}/pandad_cc.cc"
with open(path) as f:
    content = f.read()
if "C3_SKIP_CANFD" in content:
    print("pandad.cc already patched")
    sys.exit(0)

# Patch 1: skip set_can_fd_auto for BLACK_PANDA — command 0xe8 hangs the retry loop
# if unsupported by the sunnypilot F4 firmware.
OLD1 = (
    '  for (int i = 0; i < PANDA_CAN_CNT; i++) {{\\n'
    '    panda->set_can_fd_auto(i, true);\\n'
    '  }}')
NEW1 = (
    '  // C3_SKIP_CANFD: skip CAN FD for deprecated pandas (BLACK_PANDA/F4)\\n'
    '  // set_can_fd_auto (0xe8) may not be supported by sunnypilot F4 firmware;\\n'
    '  // panda_comms.cc control_read loops forever on LIBUSB_ERROR_PIPE.\\n'
    '  bool is_can_fd_supported = panda->hw_type != cereal::PandaState::PandaType::UNKNOWN &&\\n'
    '                             panda->hw_type != cereal::PandaState::PandaType::BLACK_PANDA;\\n'
    '  for (int i = 0; i < PANDA_CAN_CNT; i++) {{\\n'
    '    if (is_can_fd_supported) panda->set_can_fd_auto(i, true);\\n'
    '  }}')
if OLD1 not in content:
    print("ERROR: CAN FD anchor not found in pandad.cc")
    sys.exit(1)
content = content.replace(OLD1, NEW1)

# Patch 2: handle get_can_state failure for BLACK_PANDA — command 0xc2 may be
# unsupported, causing control_read to loop forever; or it may return unexpected
# data. Use zeroed can_health instead of returning nullopt (which silently drops
# all pandaStates publishes and prevents ignition detection).
OLD2 = (
    '    std::array<can_health_t, PANDA_CAN_CNT> can_health{{}};\\n'
    '    for (uint32_t i = 0; i < PANDA_CAN_CNT; i++) {{\\n'
    '      auto can_health_opt = panda->get_can_state(i);\\n'
    '      if (!can_health_opt) {{\\n'
    '        return std::nullopt;\\n'
    '      }}\\n'
    '      can_health[i] = *can_health_opt;\\n'
    '    }}')
NEW2 = (
    '    // C3_CANSTATE: deprecated pandas (BLACK_PANDA/F4) may not support 0xc2\\n'
    '    bool is_deprecated = panda->hw_type == cereal::PandaState::PandaType::UNKNOWN ||\\n'
    '                         panda->hw_type == cereal::PandaState::PandaType::BLACK_PANDA;\\n'
    '    std::array<can_health_t, PANDA_CAN_CNT> can_health{{}};\\n'
    '    for (uint32_t i = 0; i < PANDA_CAN_CNT; i++) {{\\n'
    '      auto can_health_opt = panda->get_can_state(i);\\n'
    '      if (!can_health_opt) {{\\n'
    '        if (!is_deprecated) return std::nullopt;\\n'
    '        // use zeroed can_health — get_can_state unsupported on this panda\\n'
    '      }} else {{\\n'
    '        can_health[i] = *can_health_opt;\\n'
    '      }}\\n'
    '    }}')
if OLD2 not in content:
    print("ERROR: get_can_state anchor not found in pandad.cc")
    sys.exit(1)
content = content.replace(OLD2, NEW2)

with open(path, "w") as f:
    f.write(content)
shutil.copy(path, bk)
print("pandad.cc patched")
""")
        print(f"  {out}")
        if "ERROR" in out:
            print("  WARNING: pandad.cc patch failed — pandad may not publish pandaStates for BLACK_PANDA")
    else:
        print("  pandad.cc already patched")
        ssh_run(host, user,
                f"cp {op}/selfdrive/pandad/pandad.cc {panda_backup}/pandad_cc.cc")


# ── Step 5: AGNOS block ───────────────────────────────────────────────────────

def step_block_agnos(host: str, user: str, op: str, status: dict) -> None:
    print("\n[6/8] Blocking AGNOS auto-update ...")

    if not status["agnos: updated.py blocked"]:
        print("  Patching updated.py ...")
        out = device_python(host, user, f"""
import sys
path = "{op}/system/updated/updated.py"
with open(path) as f:
    content = f.read()
if "C3_BLOCK_AGNOS" in content:
    print("updated.py already patched")
    sys.exit(0)
OLD = "def handle_agnos_update() -> None:"
NEW = '''def handle_agnos_update() -> None:
  # C3_BLOCK_AGNOS: prevent AGNOS update which softbricks the Comma 3
  import os as _c3os
  if _c3os.path.exists("/data/no_agnos_update"):
    return'''
if OLD not in content:
    print("ERROR: handle_agnos_update() not found")
    sys.exit(1)
content = content.replace(OLD, NEW)
with open(path, "w") as f:
    f.write(content)
print("updated.py patched")
""")
        print(f"  {out}")
    else:
        print("  updated.py already patched")

    # Patch launch_chffrplus.sh — this is the REAL trigger: called at every boot
    # before openpilot starts, it compares /VERSION vs $AGNOS_VERSION and runs
    # the updater binary (shows on-screen prompt) if they differ.
    r = ssh_run(host, user,
                f"grep -q 'C3_BLOCK_AGNOS' {op}/launch_chffrplus.sh && echo yes || echo no",
                check=False)
    if r.stdout.strip() != "yes":
        print("  Patching launch_chffrplus.sh ...")
        out = device_python(host, user, f"""
import sys
path = "{op}/launch_chffrplus.sh"
with open(path) as f:
    content = f.read()
if "C3_BLOCK_AGNOS" in content:
    print("launch_chffrplus.sh already patched")
    sys.exit(0)
OLD = '  # Check if AGNOS update is required\\n  if [ $(< /VERSION) != "$AGNOS_VERSION" ]; then'
NEW = ('  # Check if AGNOS update is required\\n'
       '  # C3_BLOCK_AGNOS: skip if flag exists (prevents softbricking Comma 3)\\n'
       '  if [ -f /data/no_agnos_update ]; then\\n'
       '    echo "C3: AGNOS update blocked (on $(< /VERSION), target $AGNOS_VERSION)"\\n'
       '  elif [ $(< /VERSION) != "$AGNOS_VERSION" ]; then')
if OLD not in content:
    print("ERROR: AGNOS check pattern not found in launch_chffrplus.sh")
    sys.exit(1)
content = content.replace(OLD, NEW)
with open(path, "w") as f:
    f.write(content)
print("launch_chffrplus.sh patched")
""")
        print(f"  {out}")
    else:
        print("  launch_chffrplus.sh already patched")

    if not status["agnos: flag file"]:
        ssh_run(host, user, "touch /data/no_agnos_update")
        print("  Created /data/no_agnos_update")
    else:
        print("  /data/no_agnos_update already exists")


# ── Step 6b: AGNOS 12.8 compatibility patches ────────────────────────────────

def step_patch_compat(host: str, user: str, op: str, status: dict) -> None:
    """Fix openpilot 0.10.2 incompatibilities with AGNOS 12.8 / Comma 3."""
    print("\n[7/8] Applying AGNOS 12.8 / C3 compatibility patches ...")

    compat_backup = f"{BACKUP_DIR}/compat"
    ssh_run(host, user, f"mkdir -p {compat_backup}")

    # ── amplifier.py: add missing "tici" config ──────────────────────────────
    # openpilot 0.10.2 only has "tizi" (C3x); C3 reports get_device_type()="tici"
    # causing KeyError in build.py before scons can run.
    amp_dest = f"{op}/openpilot/system/hardware/tici/amplifier.py"
    amp_bk   = f"{compat_backup}/amplifier.py"
    local_amp = PATCH_DIR / "amplifier.py"

    # Always refresh backup from sunnypilot (it has both "tici" and "tizi")
    scp_push(host, user, local_amp, amp_bk)

    if not status["compat: amplifier.py tici"]:
        print("  Installing amplifier.py (from sunnypilot, adds tici config) ...")
        ssh_run(host, user, f"cp {amp_bk} {amp_dest}")
        print("  amplifier.py installed")
    else:
        print("  amplifier.py already has tici config")

    # ── Python packages missing on AGNOS 12.8 ───────────────────────────────
    missing_pkgs = []
    if not status["compat: jeepney"]:
        missing_pkgs.append("jeepney")
    if not status["compat: kaitaistruct"]:
        missing_pkgs.append("kaitaistruct")

    if missing_pkgs:
        pkgs_str = " ".join(missing_pkgs)
        print(f"  Installing missing pip packages: {pkgs_str} ...")
        r = ssh_run(host, user, f"pip3 install --break-system-packages {pkgs_str}", check=False)
        if r.returncode == 0:
            print(f"  Installed: {pkgs_str}")
        else:
            print(f"  WARNING: pip3 install failed (rc={r.returncode})")
            err = (r.stderr or r.stdout or "").strip()
            if err:
                print(f"    {err[:300]}")
    else:
        print("  jeepney and kaitaistruct already installed")


# ── Step 7: persistence service ───────────────────────────────────────────────

PERSIST_SCRIPT = r"""#!/usr/bin/env python3
# c3_persist.py — re-applies C3 patches after openpilot auto-updates
# Generated by c3_patch.py — do not edit manually
import os, shutil, subprocess, sys

OP_DIR     = "{op_dir}"
BACKUP_DIR = "/data/c3_backup"

def log(msg):
    print(f"[c3_persist] {msg}", flush=True)


# ── 1. AGNOS flag ─────────────────────────────────────────────────────────────
if not os.path.exists("/data/no_agnos_update"):
    open("/data/no_agnos_update", "w").close()
    log("Restored /data/no_agnos_update")


# ── 2. AGNOS block in updated.py and launch_chffrplus.sh ─────────────────────
updated_py = os.path.join(OP_DIR, "system/updated/updated.py")
if os.path.exists(updated_py):
    with open(updated_py) as f:
        content = f.read()
    if "C3_BLOCK_AGNOS" not in content:
        OLD = "def handle_agnos_update() -> None:"
        NEW = ("def handle_agnos_update() -> None:\n"
               "  # C3_BLOCK_AGNOS: prevent AGNOS update which softbricks the Comma 3\n"
               "  import os as _c3os\n"
               "  if _c3os.path.exists(\"/data/no_agnos_update\"):\n"
               "    return")
        if OLD in content:
            content = content.replace(OLD, NEW)
            with open(updated_py, "w") as f:
                f.write(content)
            log("Re-applied AGNOS block to updated.py")
        else:
            log("WARNING: handle_agnos_update not found in updated.py")
    else:
        log("updated.py AGNOS block intact")

# launch_chffrplus.sh — the boot-time AGNOS update trigger (shows on-screen prompt)
launch_sh = os.path.join(OP_DIR, "launch_chffrplus.sh")
if os.path.exists(launch_sh):
    with open(launch_sh) as f:
        content = f.read()
    if "C3_BLOCK_AGNOS" not in content:
        OLD = '  # Check if AGNOS update is required\n  if [ $(< /VERSION) != "$AGNOS_VERSION" ]; then'
        NEW = ('  # Check if AGNOS update is required\n'
               '  # C3_BLOCK_AGNOS: skip if flag exists (prevents softbricking Comma 3)\n'
               '  if [ -f /data/no_agnos_update ]; then\n'
               '    echo "C3: AGNOS update blocked (on $(< /VERSION), target $AGNOS_VERSION)"\n'
               '  elif [ $(< /VERSION) != "$AGNOS_VERSION" ]; then')
        if OLD in content:
            content = content.replace(OLD, NEW)
            with open(launch_sh, "w") as f:
                f.write(content)
            log("Re-applied AGNOS block to launch_chffrplus.sh")
        else:
            log("WARNING: AGNOS check pattern not found in launch_chffrplus.sh")
    else:
        log("launch_chffrplus.sh AGNOS block intact")


# ── 3. Amplifier (add "tici" config for C3) ──────────────────────────────────
amp_bk   = os.path.join(BACKUP_DIR, "compat/amplifier.py")
amp_dest = os.path.join(OP_DIR, "openpilot/system/hardware/tici/amplifier.py")
if os.path.exists(amp_bk):
    needs_amp = True
    if os.path.exists(amp_dest):
        with open(amp_dest) as f:
            needs_amp = '"tici"' not in f.read()
    if needs_amp:
        shutil.copy(amp_bk, amp_dest)
        log("Restored amplifier.py (tici config)")
    else:
        log("amplifier.py intact")
else:
    log("WARNING: amplifier.py backup missing — run c3_patch.py again")


# ── 5. F4 panda firmware ─────────────────────────────────────────────────────
for fw_name in ("panda.bin.signed", "bootstub.panda.bin"):
    bk   = os.path.join(BACKUP_DIR, "panda", fw_name)
    dest = os.path.join(OP_DIR, "panda/board/obj", fw_name)
    if not os.path.exists(bk):
        log(f"WARNING: panda firmware backup {fw_name} missing — run c3_patch.py again")
        continue
    if not os.path.exists(dest):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy(bk, dest)
        log(f"Restored {fw_name}")
    else:
        log(f"{fw_name} intact")


# ── 7. Panda Python files ─────────────────────────────────────────────────────
panda_backup = os.path.join(BACKUP_DIR, "panda")
panda_files = [
    (f"{panda_backup}/panda_init.py",  f"{OP_DIR}/panda/python/__init__.py",     "DEPRECATED_DEVICES"),
    (f"{panda_backup}/pandad_py.py",   f"{OP_DIR}/selfdrive/pandad/pandad.py",   "DEPRECATED_DEVICES"),
    (f"{panda_backup}/pandad_h.h",     f"{OP_DIR}/selfdrive/pandad/pandad.h",    "DEPRECATED_PANDA_TYPES"),
]
for bk, op_dest, marker in panda_files:
    if not os.path.exists(bk):
        log(f"WARNING: panda backup {bk} missing — run c3_patch.py again")
        continue
    if os.path.exists(op_dest):
        with open(op_dest) as f:
            if marker in f.read():
                log(f"{os.path.basename(op_dest)} intact")
                continue
    shutil.copy(bk, op_dest)
    log(f"Restored {os.path.basename(op_dest)}")


# ── 8. pandad.cc patch (BLACK_PANDA CAN FD + get_can_state fix) ──────────────
pandad_cc_bk   = os.path.join(BACKUP_DIR, "panda/pandad_cc.cc")
pandad_cc_dest = os.path.join(OP_DIR, "selfdrive/pandad/pandad.cc")
pandad_rebuild_needed = False
if os.path.exists(pandad_cc_bk):
    needs_patch = True
    if os.path.exists(pandad_cc_dest):
        with open(pandad_cc_dest) as f:
            needs_patch = "C3_SKIP_CANFD" not in f.read()
    if needs_patch:
        shutil.copy(pandad_cc_bk, pandad_cc_dest)
        log("Restored pandad.cc (BLACK_PANDA patch)")
        pandad_rebuild_needed = True
    else:
        log("pandad.cc intact")
else:
    log("WARNING: pandad.cc backup missing — run c3_patch.py again")

if pandad_rebuild_needed:
    log("Rebuilding pandad (this takes ~1 min) ...")
    # Try known scons locations on AGNOS
    scons_candidates = [
        "/usr/local/venv/bin/scons",
        "/usr/bin/scons",
        "/usr/local/bin/scons",
    ]
    scons_bin = next((s for s in scons_candidates if os.path.isfile(s)), None)
    if not scons_bin:
        import shutil as _shutil
        scons_bin = _shutil.which("scons")
    if not scons_bin:
        log("WARNING: scons not found — pandad not rebuilt (run c3_patch.py to rebuild)")
    else:
        try:
            # Delete binary + .o first so scons cache can't restore the old build
            for f in (os.path.join(OP_DIR, "selfdrive/pandad/pandad"),
                      os.path.join(OP_DIR, "selfdrive/pandad/pandad.o")):
                try:
                    os.remove(f)
                except FileNotFoundError:
                    pass
            result = subprocess.run(
                [scons_bin, "--cache-disable", "-j4", "selfdrive/pandad/pandad"],
                cwd=OP_DIR, capture_output=True, text=True, timeout=300
            )
            if result.returncode == 0:
                log("pandad rebuilt successfully")
            else:
                log(f"WARNING: pandad rebuild failed (rc={result.returncode})")
                log(result.stderr[-500:] if result.stderr else "(no output)")
        except Exception as e:
            log(f"WARNING: pandad rebuild error: {e}")


# ── 10. Camera source files ───────────────────────────────────────────────────
cam_backup = os.path.join(BACKUP_DIR, "camera")
camera_rebuild_needed = False

# AR0231 binary files (just copy, no rebuild needed for these)
for fname in ("ar0231.cc", "ar0231_registers.h"):
    bk   = os.path.join(cam_backup, fname)
    dest = os.path.join(OP_DIR, "system/camerad/sensors", fname)
    if not os.path.exists(bk):
        log(f"WARNING: camera backup {fname} missing — run c3_patch.py again")
        continue
    if not os.path.exists(dest):
        shutil.copy(bk, dest)
        log(f"Restored {fname}")
        camera_rebuild_needed = True

# Patched source files that need rebuild if overwritten
patched_sources = [
    (f"{cam_backup}/sensor.h.bk",    f"{OP_DIR}/system/camerad/sensors/sensor.h",       "ar0231_registers"),
    (f"{cam_backup}/spectra.cc.bk",  f"{OP_DIR}/system/camerad/cameras/spectra.cc",     "new AR0231"),
    (f"{cam_backup}/SConscript.bk",  f"{OP_DIR}/system/camerad/SConscript",             "ar0231.cc"),
]
for bk, op_dest, marker in patched_sources:
    if not os.path.exists(bk):
        log(f"WARNING: camera backup {os.path.basename(bk)} missing — run c3_patch.py again")
        continue
    if os.path.exists(op_dest):
        with open(op_dest) as f:
            if marker in f.read():
                log(f"{os.path.basename(op_dest)} intact")
                continue
    shutil.copy(bk, op_dest)
    log(f"Restored {os.path.basename(op_dest)}")
    camera_rebuild_needed = True


# ── 11. Rebuild camerad if source files were restored ────────────────────────
if camera_rebuild_needed:
    log("Camera source files were restored — rebuilding camerad (this takes a few minutes) ...")
    scons_candidates = [
        "/usr/local/venv/bin/scons",
        "/usr/bin/scons",
        "/usr/local/bin/scons",
    ]
    scons_bin = next((s for s in scons_candidates if os.path.isfile(s)), None)
    if not scons_bin:
        import shutil as _shutil
        scons_bin = _shutil.which("scons")
    if not scons_bin:
        log("WARNING: scons not found — camerad not rebuilt (run c3_patch.py to rebuild)")
    else:
        try:
            result = subprocess.run(
                [scons_bin, "-j4", "system/camerad/camerad"],
                cwd=OP_DIR, capture_output=True, text=True, timeout=900
            )
            if result.returncode == 0:
                log("camerad rebuilt successfully")
            else:
                log(f"WARNING: camerad rebuild failed (rc={result.returncode})")
                log(result.stderr[-500:] if result.stderr else "(no output)")
        except subprocess.TimeoutExpired:
            log("WARNING: camerad rebuild timed out after 15 minutes")
        except Exception as e:
            log(f"WARNING: camerad rebuild error: {e}")
else:
    log("Camera patches intact — no rebuild needed")

log("Done")
"""

PERSIST_SERVICE = """\
[Unit]
Description=Comma 3 compatibility patch (C3 hardware support)
After=local-fs.target
Before=openpilot.service comma.service

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /data/c3_persist.py
RemainAfterExit=yes
TimeoutStartSec=1200
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""


def step_install_persistence(host: str, user: str, op: str, status: dict) -> None:
    print("\n[8/8] Installing persistence service ...")

    script_content = PERSIST_SCRIPT.replace("{op_dir}", op)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(script_content)
        tmp_py = f.name
    with tempfile.NamedTemporaryFile(mode="w", suffix=".service", delete=False) as f:
        f.write(PERSIST_SERVICE)
        tmp_svc = f.name

    try:
        scp_push(host, user, Path(tmp_py), "/data/c3_persist.py")
        ssh_run(host, user, "chmod +x /data/c3_persist.py")
        print("  Deployed /data/c3_persist.py")

        if not status["persist: service installed"]:
            scp_push(host, user, Path(tmp_svc), "/tmp/c3_persist.service")
            ssh_run(host, user,
                    "sudo mount -o remount,rw / && "
                    "sudo mv /tmp/c3_persist.service /etc/systemd/system/c3_persist.service && "
                    "sudo systemctl daemon-reload && "
                    "sudo systemctl enable c3_persist.service ; "
                    "sudo mount -o remount,ro /")
            print("  Systemd service installed and enabled")
        else:
            ssh_run(host, user,
                    "sudo mount -o remount,rw / && "
                    "sudo systemctl daemon-reload ; "
                    "sudo mount -o remount,ro /")
            print("  Systemd service refreshed (already installed)")
    finally:
        os.unlink(tmp_py)
        os.unlink(tmp_svc)


# ── Rebuild ───────────────────────────────────────────────────────────────────

def _find_scons(host: str, user: str, op: str) -> str:
    """Return a scons invocation that works on the device, or empty string."""
    # 1. Known venv location on AGNOS (openpilot 0.10.x)
    r = ssh_run(host, user,
                "test -x /usr/local/venv/bin/scons && echo yes || echo no",
                check=False)
    if r.stdout.strip() == "yes":
        return "/usr/local/venv/bin/scons"

    # 2. scons binary in PATH
    r = ssh_run(host, user, "which scons", check=False)
    if r.returncode == 0:
        return r.stdout.strip()

    # 3. Search for scons in common venv/pip locations
    r = ssh_run(host, user,
                "find /usr/local /usr /data -maxdepth 8 -name 'scons' -type f 2>/dev/null | head -1",
                check=False)
    path = r.stdout.strip()
    if path:
        return path

    # 4. Try pip-installing, then retry
    print("  scons not found — trying pip3 install scons ...")
    ssh_run(host, user,
            "pip3 install scons --break-system-packages -q 2>/dev/null || "
            "pip3 install scons -q 2>/dev/null || true",
            check=False)
    r = ssh_run(host, user, "which scons 2>/dev/null || find /usr/local -name 'scons' 2>/dev/null | head -1",
                check=False)
    path = r.stdout.strip()
    if path:
        return path

    return ""


def _rebuild_target(host: str, user: str, op: str, target: str, label: str,
                    scons_cmd: str) -> None:
    """Run a scons build for one target; print result."""
    if not scons_cmd:
        print(f"  SKIP {label}: scons not available on device")
        print(f"  To build manually: ssh in, then:")
        print(f"    cd {op} && /usr/local/venv/bin/scons -j4 {target}")
        return

    print(f"  Building {label} ...")

    # Pre-steps to force a real recompile:
    # 1. Fix ownership (cereal/gen was compiled as root, comma can't write)
    # 2. Delete target binary + its .o — scons cache can restore old binaries
    #    even after source changes; --cache-disable prevents that
    target_dir = f"{op}/{target.rsplit('/', 1)[0]}"
    target_bin = f"{op}/{target}"
    target_base = target.rsplit("/", 1)[-1]
    target_o    = f"{target_dir}/{target_base}.o"
    ssh_run(host, user,
            f"sudo chown -R comma:comma {op}/cereal/gen/ {target_dir}/ 2>/dev/null || true",
            check=False)
    ssh_run(host, user,
            f"rm -f {target_bin} {target_o}",
            check=False)

    # --cache-disable: bypass scons content-addressed cache so it actually
    # recompiles from source instead of restoring the old binary
    r = ssh_run(host, user,
                f"cd {op} && {scons_cmd} --cache-disable -j$(nproc) {target}",
                check=False)
    output = (r.stdout + r.stderr).strip()
    if r.returncode == 0:
        lines = [l for l in output.splitlines() if l.strip()]
        print(f"  {label} OK — {lines[-1] if lines else 'done'}")
    else:
        print(f"  WARNING: {label} build failed (exit {r.returncode})")
        for line in output.splitlines()[-8:]:
            print(f"    {line}")


def step_rebuild(host: str, user: str, op: str) -> None:
    scons = _find_scons(host, user, op)
    print(f"\n[+] Rebuild — scons: {scons or 'NOT FOUND'}")

    # pandad first (quick, fixes ignition detection for BLACK_PANDA)
    _rebuild_target(host, user, op, "selfdrive/pandad/pandad", "pandad", scons)

    # camerad (slower, AR0231 camera support)
    print()
    _rebuild_target(host, user, op, "system/camerad/camerad", "camerad", scons)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Comma 3 openpilot compatibility patch script",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--host",       default=DEFAULT_HOST, help=f"C3 IP (default: {DEFAULT_HOST})")
    parser.add_argument("--user",       default=DEFAULT_USER, help=f"SSH user (default: {DEFAULT_USER})")
    parser.add_argument("--op-dir",     default=None,         help="Override openpilot path on device (e.g. /data/openpilot_new)")
    parser.add_argument("--key",        default=None,         help="Path to SSH private key (overrides built-in key)")
    parser.add_argument("--check",      action="store_true",  help="Show status only, do not modify")
    parser.add_argument("--no-rebuild", action="store_true",  help="Skip camerad rebuild")
    parser.add_argument("--no-persist", action="store_true",  help="Skip persistence service")
    parser.add_argument("--c3-panel",   action="store_true",  help="Install Necromancer in-car maintenance panel")
    args = parser.parse_args()

    host, user = args.host, args.user

    global SSH_KEY
    if args.key:
        SSH_KEY = Path(args.key).expanduser().resolve()

    print("=" * 62)
    print("  C3 Compatibility Patch Script")
    print(f"  Target : {user}@{host}")
    print(f"  SSH key: {SSH_KEY}")
    print("=" * 62)

    if not SSH_KEY.exists():
        print(f"\nERROR: SSH key not found at {SSH_KEY}")
        sys.exit(1)
    if not PATCH_DIR.exists():
        print(f"\nERROR: patch_files directory not found at {PATCH_DIR}")
        sys.exit(1)

    step_check_connection(host, user)
    op = args.op_dir if args.op_dir else step_detect_op(host, user)
    if args.op_dir:
        print(f"\n[--op-dir] Using specified openpilot path: {op}")
    status = step_check_status(host, user, op)

    if args.check:
        all_good = all(status.values())
        print(f"\n{'All patches applied.' if all_good else 'Some patches missing — run without --check to apply.'}")
        return

    ssh_run(host, user, f"mkdir -p {BACKUP_DIR}")

    step_patch_camera(host, user, op, status)
    step_patch_panda(host, user, op, status)
    step_block_agnos(host, user, op, status)
    step_patch_compat(host, user, op, status)

    if not args.no_persist:
        step_install_persistence(host, user, op, status)
    else:
        print("\n[8/8] Skipping persistence service (--no-persist)")

    if not args.no_rebuild:
        step_rebuild(host, user, op)
    else:
        print("\n[+]  Skipping rebuild (--no-rebuild)")

    if args.c3_panel:
        step_install_c3_panel(host, user, op)
    else:
        print("\n[+]  Skipping Necromancer C3 panel (--c3-panel not set)")

    print("\n" + "=" * 62)
    print("  All patches applied.")
    print("  Reboot the device to activate changes:")
    print(f"    ssh -i {SSH_KEY} {user}@{host} 'sudo reboot'")
    print("=" * 62)


def step_install_c3_panel(host: str, user: str, op: str) -> None:
    """Install the Necromancer in-car maintenance panel into the openpilot settings UI."""
    print("\n[+]  Installing Necromancer C3 panel ...")

    c3_panel_dir = PATCH_DIR / "c3_panel"
    if not c3_panel_dir.exists():
        print(f"  ERROR: c3_panel directory not found at {c3_panel_dir}")
        return

    # Create scripts/necromancer/ directory on device
    ssh_run(host, user, f"mkdir -p {op}/scripts/necromancer")

    # Copy the settings panel
    scp_push(host, user, c3_panel_dir / "necromancer.py",
             f"{op}/selfdrive/ui/layouts/settings/necromancer.py")
    print("  necromancer.py  →  selfdrive/ui/layouts/settings/")

    # Copy the full-screen runner
    scp_push(host, user, c3_panel_dir / "necro_runner.py",
             f"{op}/scripts/necromancer/necro_runner.py")
    print("  necro_runner.py →  scripts/necromancer/")

    # Empty __init__.py so Python treats it as a package
    ssh_run(host, user, f"touch {op}/scripts/necromancer/__init__.py")

    # Patch settings.py to register the new panel (idempotent)
    settings_py = f"{op}/selfdrive/ui/layouts/settings/settings.py"
    patch_script = f"""
import pathlib
p = pathlib.Path({repr(settings_py)})
t = p.read_text()

IMPORT_LINE = "from openpilot.selfdrive.ui.layouts.settings.necromancer import NecromancerLayout"
ENUM_LINE   = "  NECROMANCER = 7"
PANEL_LINE  = "PanelType.NECROMANCER"

changed = False

if IMPORT_LINE not in t:
    t = t.replace(
        "from openpilot.selfdrive.ui.layouts.settings.nap import NAPLayout",
        "from openpilot.selfdrive.ui.layouts.settings.nap import NAPLayout\\n" + IMPORT_LINE,
    )
    changed = True

if ENUM_LINE not in t:
    t = t.replace("  DEVELOPER = 6", "  DEVELOPER = 6\\n  NECROMANCER = 7")
    changed = True

if PANEL_LINE not in t:
    t = t.replace(
        'PanelType.DEVELOPER: PanelInfo(tr_noop("Developer"), DeveloperLayout()),',
        'PanelType.DEVELOPER: PanelInfo(tr_noop("Developer"), DeveloperLayout()),\\n'
        '      PanelType.NECROMANCER: PanelInfo(tr_noop("Necro"), NecromancerLayout()),',
    )
    changed = True

# Purple sidebar button for Necromancer
SIDEBAR_OLD = '''      # Button styling
      is_selected = panel_type == self._current_panel
      text_color = TEXT_SELECTED if is_selected else TEXT_NORMAL'''
SIDEBAR_NEW = '''      # Button styling
      is_selected = panel_type == self._current_panel
      if panel_type == PanelType.NECROMANCER:
        text_color = rl.Color(192, 132, 252, 255) if is_selected else rl.Color(130, 80, 200, 255)
      else:
        text_color = TEXT_SELECTED if is_selected else TEXT_NORMAL'''
if SIDEBAR_OLD in t and SIDEBAR_NEW not in t:
    t = t.replace(SIDEBAR_OLD, SIDEBAR_NEW)
    changed = True

# Make sidebar nav button height dynamic so all panels fit on screen
NAV_OLD = '''    # Navigation buttons
    y = rect.y + 300
    for panel_type, panel_info in self._panels.items():
      button_rect = rl.Rectangle(rect.x + 50, y, rect.width - 150, NAV_BTN_HEIGHT)'''
NAV_NEW = '''    # Navigation buttons — height shrinks to fit all panels on screen
    _nav_h = min(NAV_BTN_HEIGHT, int((rect.height - 320) / max(len(self._panels), 1)))
    y = rect.y + 300
    for panel_type, panel_info in self._panels.items():
      button_rect = rl.Rectangle(rect.x + 50, y, rect.width - 150, _nav_h)'''
if NAV_OLD in t:
    t = t.replace(NAV_OLD, NAV_NEW)
    t = t.replace('      y += NAV_BTN_HEIGHT', '      y += _nav_h')
    changed = True

if changed:
    p.write_text(t)
    print("PATCHED")
else:
    print("ALREADY_PATCHED")
"""
    result = device_python(host, user, patch_script)
    if "PATCHED" in result:
        print("  settings.py patched — Necromancer tab added to settings UI.")
    elif "ALREADY_PATCHED" in result:
        print("  settings.py already has Necromancer tab.")
    else:
        print(f"  WARNING: settings.py patch result unclear: {result!r}")

    print("  ✓  Necromancer C3 panel installed.")


if __name__ == "__main__":
    main()
