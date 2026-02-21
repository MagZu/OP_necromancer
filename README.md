# ☠ OP Necromancer ☠
### *bring C3 back from the dead*

A GUI wizard that resurrects a **Comma 3** running sunnypilot for use with a **Tesla Model S 2014 (pre-Autopilot)**. It applies the C3 compatibility patch that makes openpilot 0.10.2 work on the C3's hardware and Tesla's legacy CAN bus.

---

## What it does

The ritual proceeds in four steps:

| Step | Name | What happens |
|------|------|--------------|
| I | **Locate** | Finds your C3 on the network (auto-discover or manual IP) |
| II | **Inspect** | SSHs into the C3 and verifies the required base install is present |
| III | **Grimoire** | Validates your chosen openpilot fork against the required version |
| IV | **Resurrect** | Stops the service, entombs the old install, clones the new fork, and applies the C3 compatibility patch |

After resurrection the C3 will have:
- Custom F4/DOS panda firmware (STM32F4 support + SAFETY_TESLA_LEGACY mode)
- AR0231 camera driver (C3 hardware)
- AGNOS auto-update blocked (prevents softbrick)
- A running `c3_persist` service that keeps the patch alive across reboots

---

## Before you begin — requirements

### 1. sunnypilot 0.10.1 must be installed on the C3

The ritual requires **sunnypilot 0.10.1** as the base install. This specific version sets up the openpilot directory structure and system dependencies that the patch builds on.

Install it through the C3's installer UI before running OP Necromancer. If you already have the C3 patch installed (a previous resurrection), you can skip straight to the Maintenance window.

### 2. SSH access to the C3

The C3 (comma user) must be reachable over SSH with a private key. The default comma SSH key is typically at `~/.ssh/id_rsa` or `~/.ssh/id_ed25519`.

To check:
```bash
ssh comma@<C3-IP> echo ok
```

If that works you're ready. If not, make sure your public key is added to the C3's `~/.ssh/authorized_keys`.

### 3. C3 and your computer on the same network

The C3 must be reachable from the machine running OP Necromancer. Common addresses:
- **USB tethering / phone hotspot:** `192.168.43.1`
- **Home network:** check your router for the C3's IP, or use `comma.local`
- **USB cable (direct):** `192.168.0.30`

OP Necromancer will attempt to auto-discover the C3 on all of these. You can also enter the IP manually.

### 4. Python 3.8+ with tkinter

tkinter is part of the Python standard library. On most Linux distros it may need a separate package:
```bash
# Debian / Ubuntu
sudo apt install python3-tk

# Arch
sudo pacman -S tk
```

On macOS and Windows, tkinter ships with the standard Python installer from python.org.

---

## Running the wizard

```bash
python3 op_necromancer.py
```

No pip dependencies — only Python stdlib is used.

---

## Dry run — scry without committing

Check the **"Dry run"** box on the Locate page before proceeding. In dry run mode the wizard walks through all four steps but makes **no changes** to the C3. Instead of cloning and patching, it reads the current state of the device and reports what it finds. Useful for inspecting a C3 before committing to the ritual.

---

## Maintenance — after the ritual is complete

If the C3 patch is already installed, the Inspect page will show a **Maintenance** button instead of blocking you on the sunnypilot 0.10.1 prereq. From there you can:

- **Verify patch wards** — checks that all five components of the patch are in place (`c3_persist` service enabled/active, backup directory, panda firmware, AGNOS update block)
- **Update to latest commit** — pulls the latest commit from the remote branch and reapplies the curse
- **Restore to specific commit** — shows a list of commits between your current version and the remote tip; select one to check out and reapply the patch

---

## Files

| File | Purpose |
|------|---------|
| `op_necromancer.py` | The GUI wizard — this is what you run |
| `c3_patch.py` | The patch engine — called by the wizard over SSH |
| `patch_files/` | Binary and script assets deployed to the C3 |
| `patch_files/panda.bin.signed` | Custom F4/DOS panda firmware |
| `patch_files/bootstub.panda.bin` | Panda bootloader stub |
| `patch_files/panda_init.py` | Patched Python panda library |
| `patch_files/pandad.py` | Patched pandad for F4/bxCAN compatibility |
| `patch_files/amplifier.py` | TICI audio amplifier config |
| `patch_files/ar0231.cc` | AR0231 camera driver (C3 hardware) |
| `patch_files/ar0231_registers.h` | Camera register definitions |

---

## Disclaimer

This tool modifies the software on your Comma 3 and is intended for hobbyist use. It is not affiliated with Comma.ai. Use at your own risk. The C3 curse, once cast, is not easily reversed — though a backup of the original sunnypilot install is preserved on-device.
