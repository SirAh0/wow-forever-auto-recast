# fish

Listens to system audio for a reference sound (an audio "fingerprint") and presses a key when it hears it.

`fish_listener.py` turns a reference WAV into a log-band spectrogram, captures live audio from the default output device (WASAPI loopback, i.e. "what you hear") or a microphone, and compares the latest audio to the reference with a sliding Pearson correlation. When the score crosses a threshold, it sends a key press with `SendInput` using hardware scan codes.

The `vm/` folder holds PowerShell scripts that set up a Hyper-V virtual machine with GPU partitioning (GPU-P), for running a 3D game in a VM on Windows 11 Pro.

> **Warning:** Automating gameplay in online games usually breaks the game's Terms of Service and can get the account banned. Running it inside a VM does not change that. Use at your own risk.

## Contents

| File | Purpose |
|---|---|
| `fish_listener.py` | Audio matcher and key sender |
| `lure-catch.wav` | Default reference sound |
| `requirements.txt` | Python dependencies (`numpy`, `scipy`, `PyAudioWPatch`) |
| `vm/1-New-WowVM.ps1` | Creates a Hyper-V VM with a GPU partition |
| `vm/2-Copy-GpuDrivers.ps1` | Copies the host NVIDIA driver into the VM disk |
| `vm/3-Share-Folder.ps1` | (Optional) Shares a host folder with the VM |

## Requirements

- Windows 10/11 (WASAPI loopback and `SendInput` are Windows-only)
- Python 3.12 (64-bit). Newer versions may not have prebuilt `PyAudioWPatch` wheels.
- An audio output device. Inside a VM, see [Audio in a VM](#audio-in-a-vm).

## Install

### 1. Install Python

```powershell
winget install -e --id Python.Python.3.12
```

Open a **new** PowerShell window afterwards so `py` is on `PATH`.

### 2. Get the code

```powershell
git clone <repo-url> fish
cd fish
```

Or copy the folder onto the machine and `cd` into it.

### 3. Create a virtual environment and install dependencies

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
```

If `py` is not found, use Python's full path:

```powershell
& "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" -m venv .venv
```

### 4. Check that an audio device is found

```powershell
.\.venv\Scripts\python fish_listener.py --list-devices
```

## Usage

Run from the project folder (the default reference `lure-catch.wav` is resolved from the current directory).

```powershell
# Detect only, show live match score (no key presses)
.\.venv\Scripts\python fish_listener.py --dry-run -v

# Normal run, only press keys when the foreground window title matches
.\.venv\Scripts\python fish_listener.py --window "World of Warcraft" -v

# Use a different reference and threshold
.\.venv\Scripts\python fish_listener.py --ref other.wav --threshold 0.6 -v

# Record a new reference clip from the current output audio
.\.venv\Scripts\python fish_listener.py --record lure-catch.wav --seconds 4
```

Stop with **Ctrl+C**.

### Options

| Option | Default | Description |
|---|---|---|
| `--ref FILE` | `lure-catch.wav` | Reference WAV |
| `--threshold N` | `0.6` | Match score (0 to 1) needed to trigger |
| `--cooldown S` | `3` | Seconds to ignore matches after a trigger |
| `--key NAME` | `space` | Key to press (`space`, `enter`, `tab`, `esc`, `0`-`9`, `a`-`z`, `f1`-`f10`) |
| `--recast-delay S` | `0.5` | After a match, wait and press the key again (negative = don't) |
| `--recast-jitter S` | `1.5` | Random 0 to N seconds added to `--recast-delay` |
| `--timeout S` | `30` | Press the key if nothing matched for this long (0 = off) |
| `--timeout-jitter S` | `5` | Random 0 to N seconds added to `--timeout` |
| `--ref-seconds S` | `0.7` | Use only the first N seconds of the trimmed reference (0 = all) |
| `--trim-db DB` | `30` | Trim reference parts quieter than peak minus this many dB |
| `--delay-min S` / `--delay-max S` | `0` | Random delay before pressing |
| `--window TEXT` | | Only press when the foreground window title contains this text |
| `--device N` | | Capture device index (see `--list-devices`) |
| `--mic` | | Use the default microphone instead of loopback |
| `-v`, `--verbose` | | Show live match score |
| `--dry-run` | | Detect but don't press keys |
| `--list-devices` | | List capture devices and exit |
| `--record OUT.wav` | | Record a new reference clip and exit |
| `--seconds S` | `3` | Length for `--record` |

### Tuning

1. Run with `--dry-run -v` and watch the score while the sound plays.
2. Set `--threshold` a bit below the score you see on a real match, and above the score from background noise.
3. If it misfires, raise the threshold or increase `--ref-seconds`. If it reacts late, lower `--ref-seconds`.
4. If the in-game audio differs from the reference (volume, effects, a different machine), record a new reference with `--record`.

## Optional: Hyper-V VM with GPU partitioning

Runs the game and the listener in a Windows 11 VM with a share of the host GPU. Tested on Windows 11 Pro with an NVIDIA RTX 5070 Ti.

### Host requirements

- Windows 10/11 **Pro** (Hyper-V is not available on Home)
- A GPU listed by `Get-VMHostPartitionableGpu` (NVIDIA works best)
- A Windows 11 ISO, ideally the same build as the host
- About 160 GB free disk space for the VM

### Steps

All scripts run on the **host** in an **elevated** PowerShell, from the `vm` folder:

```powershell
cd vm
Set-ExecutionPolicy -Scope Process Bypass
```

**1. Enable Hyper-V** (skip if already enabled, then reboot):

```powershell
Enable-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V -All
```

**2. Create the VM.** Defaults: name `WoW`, 8 vCPU, 12 GB RAM, 160 GB dynamic disk, 50% GPU, stored in `D:\VMs`. Pass your ISO path:

```powershell
.\1-New-WowVM.ps1 -IsoPath 'D:\iso\Windows11.iso'
```

Other parameters: `-VMName`, `-VMRoot`, `-CpuCount`, `-MemoryBytes`, `-DiskBytes`, `-GpuPercent` (10 to 90).

**3. Install Windows in the VM.**

- Open Hyper-V Manager (`virtmgmt.msc`), right-click the VM, **Connect**, then **Start**.
- Click inside the window and press **Space** right away when "Press any key to boot from CD or DVD" appears. If you miss it, you'll see "The boot loader failed"; click **Restart now** and try again.
- Choose **I don't have a product key** and **Windows 11 Pro**. An unactivated VM works fine apart from a watermark.
- For a local account instead of a Microsoft account: **Set up for work or school**, then **Sign-in options**, then **Domain join instead**.
- **Don't install NVIDIA drivers inside the VM.**

**4. Copy the GPU driver into the VM.** Shut the VM down first (from inside Windows), then:

```powershell
.\2-Copy-GpuDrivers.ps1
```

Re-run this after every NVIDIA driver update on the host. Start the VM; Device Manager should list the host GPU under Display adapters.

**5. Display and audio.** The Hyper-V console window can't show GPU-rendered output. Install [Parsec](https://parsec.app) (with its virtual display driver) or Sunshine + Moonlight in the VM and connect from the host.

**6. (Optional) Share the project folder** with the VM instead of copying it:

```powershell
.\3-Share-Folder.ps1 -Path 'D:\dev\wow\fish'
```

Then inside the VM:

```powershell
net use Z: \\<HOST-NAME>\fish /user:<HOST-NAME>\<host-user> /persistent:yes
```

The host account needs a password. The firewall rule only opens SMB on the `vEthernet (Default Switch)` interface. To undo it on the host:

```powershell
Remove-SmbShare -Name fish -Force
Remove-NetFirewallRule -DisplayName "SMB from Hyper-V Default Switch"
```

Then follow [Install](#install) inside the VM.

### VM notes

- Checkpoints and saved state are disabled (GPU-P doesn't support them). Always shut the VM down normally.
- Each VM needs its own Windows license; the host's retail key doesn't cover it.

## Troubleshooting

| Problem | Fix |
|---|---|
| `py` is not recognized | Open a new PowerShell window after installing Python, or use the full path shown in [Install](#install) |
| `pip install` fails building `PyAudioWPatch` | Use Python 3.12 (64-bit) |
| `--list-devices` shows nothing | No audio output device. In a VM, connect via Parsec first |
| Score never gets high | Record a new reference with `--record` on the same machine |
| Key presses don't reach the game | Use `--window` to check the game has focus; run the terminal as admin if the game runs elevated |
| `2-Copy-GpuDrivers.ps1`: `Get-CimInstance : Shutting down` | WMI was busy; the script retries and falls back automatically. Re-run if it still fails |
| VM: "The boot loader failed" | The "press any key" prompt was missed; click **Restart now** and press Space right away |
| VM: no GPU in Device Manager | The VM must be off when running `2-Copy-GpuDrivers.ps1`; re-run it after host driver updates |
