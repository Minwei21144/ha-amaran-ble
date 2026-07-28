param(
    [string]$RelayHub = "",
    [string]$AppDataDir = (Join-Path $env:APPDATA "amaran Desktop"),
    [string]$InstallDir = "",
    [string]$FixtureConfigPath = "",
    [string]$OutputPath = "",
    [string]$PythonExe = "",
    [switch]$NoPause
)

$ErrorActionPreference = "Stop"

function Resolve-Python {
    param([string]$Requested)

    if ($Requested -and (Test-Path -LiteralPath $Requested)) {
        return @((Resolve-Path -LiteralPath $Requested).Path)
    }

    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) {
        foreach ($version in @("-3.14", "-3.13", "-3.12", "-3.11", "-3")) {
            try {
                $output = & $py.Source $version --version 2>$null
                if ($LASTEXITCODE -eq 0 -and $output -match "Python") {
                    return @($py.Source, $version)
                }
            }
            catch {
            }
        }
    }

    foreach ($candidate in @("python.exe", "python3.exe")) {
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($cmd) {
            try {
                $output = & $cmd.Source --version 2>$null
                if ($LASTEXITCODE -eq 0 -and $output -match "Python") {
                    return @($cmd.Source)
                }
            }
            catch {
            }
        }
    }

    throw "Python was not found. Install Python, or pass -PythonExe C:\Path\to\python.exe."
}

function Resolve-FixtureConfig {
    param(
        [string]$Requested,
        [string]$RequestedInstallDir
    )

    if ($Requested) {
        if (-not (Test-Path -LiteralPath $Requested -PathType Leaf)) {
            throw "Amaran fixture capability file not found: $Requested"
        }
        return (Resolve-Path -LiteralPath $Requested).Path
    }

    $candidates = [System.Collections.Generic.List[string]]::new()
    if ($RequestedInstallDir) {
        $candidates.Add((Join-Path $RequestedInstallDir "resources\config\fixture_config.json"))
    }
    if (${env:ProgramFiles(x86)}) {
        $candidates.Add((Join-Path ${env:ProgramFiles(x86)} "amaran Desktop\resources\config\fixture_config.json"))
    }
    if ($env:ProgramFiles) {
        $candidates.Add((Join-Path $env:ProgramFiles "amaran Desktop\resources\config\fixture_config.json"))
    }
    if ($env:LOCALAPPDATA) {
        $candidates.Add((Join-Path $env:LOCALAPPDATA "Programs\amaran Desktop\resources\config\fixture_config.json"))
    }

    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }

    $searched = $candidates -join [Environment]::NewLine
    throw @"
Amaran Desktop fixture capability file was not found.
Searched:
$searched

Pass -InstallDir or -FixtureConfigPath if Amaran Desktop is installed elsewhere.
"@
}

$tmp = $null

try {
    if (-not $OutputPath) {
        $OutputPath = Join-Path $PSScriptRoot "amaran-ble-ha-config.json"
    }

    if (-not (Test-Path -LiteralPath $AppDataDir)) {
        throw "Amaran Desktop app data directory not found: $AppDataDir"
    }

    $python = Resolve-Python -Requested $PythonExe
    $fixtureConfig = Resolve-FixtureConfig -Requested $FixtureConfigPath -RequestedInstallDir $InstallDir

    $pythonCode = @'
import json
import os
import sqlite3
from pathlib import Path

appdata_dir = Path(os.environ["AMARAN_APPDATA_DIR"])
requested_relay_hub = os.environ.get("AMARAN_RELAY_HUB", "").strip()
output_path = Path(os.environ["AMARAN_OUTPUT_PATH"])
fixture_config_path = Path(os.environ["AMARAN_FIXTURE_CONFIG_PATH"])

def normalize_mac(value):
    raw = "".join(ch for ch in (value or "") if ch in "0123456789abcdefABCDEF")
    if len(raw) != 12:
        raise ValueError(f"Invalid MAC address: {value}")
    return ":".join(raw[i:i+2] for i in range(0, 12, 2)).upper()

def find_db():
    candidates = sorted(appdata_dir.glob("*_secure_id/amaran.db"))
    if not candidates:
        candidates = sorted(appdata_dir.glob("**/amaran.db"))
    if not candidates:
        raise FileNotFoundError(f"amaran.db not found under {appdata_dir}")
    return candidates[0]

def normalize_text(value):
    return " ".join(str(value or "").strip().lower().split())

def normalize_version(value):
    version = normalize_text(value)
    return version[1:] if version.startswith("v") else version

def parse_bool(value):
    if isinstance(value, bool):
        return value
    return normalize_text(value) in {"1", "true", "yes", "on"}

def parse_kelvin(value, field_name, profile_key):
    try:
        number = float(value)
    except (TypeError, ValueError) as err:
        raise ValueError(
            f"{profile_key} has invalid {field_name}: {value!r}"
        ) from err
    if number <= 0:
        raise ValueError(f"{profile_key} has invalid {field_name}: {value!r}")
    kelvin = number * 100 if number < 1000 else number
    return int(round(kelvin))

fixture_profiles = json.loads(fixture_config_path.read_text(encoding="utf-8-sig"))
if not isinstance(fixture_profiles, dict) or not fixture_profiles:
    raise ValueError(f"Invalid fixture capability file: {fixture_config_path}")

def resolve_profile(row):
    code = str(row.get("code") or "").strip().upper()
    hardware_version = normalize_version(row.get("control_hw_version"))
    fixture_name = str(row.get("name") or "").strip()

    candidates = [
        (key, profile)
        for key, profile in fixture_profiles.items()
        if str(profile.get("uuid") or "").strip().upper() == code
    ]

    if hardware_version:
        exact = [
            (key, profile)
            for key, profile in candidates
            if normalize_version(profile.get("hardware_version")) == hardware_version
        ]
        if len(exact) == 1:
            return code, exact[0][0], exact[0][1]

    if len(candidates) == 1:
        return code, candidates[0][0], candidates[0][1]

    if not candidates:
        normalized_name = normalize_text(fixture_name)
        name_matches = [
            (key, profile)
            for key, profile in fixture_profiles.items()
            if normalize_text(profile.get("name"))
            and normalize_text(profile.get("name")) in normalized_name
        ]
        product_codes = {
            str(profile.get("uuid") or "").strip().upper()
            for _, profile in name_matches
        }
        if len(product_codes) == 1:
            if hardware_version:
                exact = [
                    (key, profile)
                    for key, profile in name_matches
                    if normalize_version(profile.get("hardware_version")) == hardware_version
                ]
                if len(exact) == 1:
                    matched_code = str(exact[0][1].get("uuid") or "").strip().upper()
                    return matched_code, exact[0][0], exact[0][1]
            if len(name_matches) == 1:
                matched_code = str(name_matches[0][1].get("uuid") or "").strip().upper()
                return matched_code, name_matches[0][0], name_matches[0][1]

    detail = (
        f"name={fixture_name!r}, code={code or '<missing>'}, "
        f"control_hw_version={hardware_version or '<missing>'}"
    )
    if candidates:
        available = ", ".join(key for key, _ in candidates)
        raise ValueError(
            f"Unable to choose an exact official capability profile for {detail}. "
            f"Available profiles: {available}"
        )
    raise ValueError(f"No official capability profile found for {detail}")

db_path = find_db()
conn = sqlite3.connect(str(db_path))
conn.row_factory = sqlite3.Row

mesh = dict(conn.execute("select * from mesh limit 1").fetchone())
fixtures = [
    dict(row)
    for row in conn.execute(
        "select * from fixtures where state = 3 order by node_address"
    )
]

if not mesh.get("net_key") or not mesh.get("app_key"):
    raise ValueError("net_key/app_key not found in mesh table")
if not fixtures:
    raise ValueError("No active fixtures found in fixtures table")

lights = []
capability_summary = []
for row in fixtures:
    mac = normalize_mac(row.get("mac_address"))
    name = (row.get("name") or f"amaran {row.get('node_address')}").strip()
    code, profile_key, profile = resolve_profile(row)
    min_kelvin = parse_kelvin(profile.get("product_cct_min"), "product_cct_min", profile_key)
    max_kelvin = parse_kelvin(profile.get("product_cct_max"), "product_cct_max", profile_key)
    if min_kelvin > max_kelvin:
        raise ValueError(
            f"{profile_key} has an invalid CCT range: {min_kelvin}K..{max_kelvin}K"
        )
    supports_hs = parse_bool(profile.get("hsi_support"))
    supports_gm = parse_bool(profile.get("gm_support"))
    lights.append(
        {
            "name": name,
            "mac": mac,
            "mesh_address": int(row.get("node_address") or 0),
            "min_kelvin": min_kelvin,
            "max_kelvin": max_kelvin,
            "supports_hs": supports_hs,
            "supports_gm": supports_gm,
        }
    )
    capability_summary.append(
        {
            "name": name,
            "code": code,
            "profile": profile_key,
            "min_kelvin": min_kelvin,
            "max_kelvin": max_kelvin,
            "supports_hs": supports_hs,
            "supports_gm": supports_gm,
        }
    )

relay_hub = normalize_mac(requested_relay_hub) if requested_relay_hub else lights[0]["mac"]

config = {
    "net_key": str(mesh["net_key"]).upper(),
    "app_key": str(mesh["app_key"]).upper(),
    "relay_hub": relay_hub,
    "lights": lights,
}

output_path.parent.mkdir(parents=True, exist_ok=True)
output_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

print(f"DB: {db_path}")
print(f"Fixture capabilities: {fixture_config_path}")
print(f"Output: {output_path}")
if not requested_relay_hub:
    print(f"Relay hub auto-selected: {relay_hub}")
print("Detected fixture capabilities:")
for item in capability_summary:
    print(
        f"  - {item['name']}: code={item['code']}, profile={item['profile']}, "
        f"CCT={item['min_kelvin']}K..{item['max_kelvin']}K, "
        f"HS={item['supports_hs']}, GM={item['supports_gm']}"
    )
print("WARNING: The JSON below contains private Sidus Mesh keys. Do not share it publicly.")
print(json.dumps(config, ensure_ascii=False, indent=2))
'@

    $tmp = Join-Path $env:TEMP ("amaran_export_ha_config_{0}.py" -f ([guid]::NewGuid().ToString("N")))
    Set-Content -LiteralPath $tmp -Value $pythonCode -Encoding UTF8

    $env:AMARAN_APPDATA_DIR = (Resolve-Path -LiteralPath $AppDataDir).Path
    $env:AMARAN_RELAY_HUB = $RelayHub
    $env:AMARAN_OUTPUT_PATH = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputPath)
    $env:AMARAN_FIXTURE_CONFIG_PATH = $fixtureConfig

    if ($python.Count -gt 1) {
        $pythonArgs = @($python[1..($python.Count - 1)])
        & $python[0] @pythonArgs $tmp
    }
    else {
        & $python[0] $tmp
    }

    if ($LASTEXITCODE -ne 0) {
        throw "The exporter stopped with Python exit code $LASTEXITCODE."
    }
}
catch {
    Write-Host ""
    Write-Host "Export failed:" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    throw
}
finally {
    if ($tmp) {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    }

    if (-not $NoPause) {
        Write-Host ""
        [void](Read-Host "Press Enter to close this window")
    }
}
