#!/usr/bin/env python3
"""
CI builder for HATS packs.

Autonome (stdlib only), reprend la logique du PackBuilder HATSKitPro :
- components.json comme source
- skeleton.zip depuis HATSKitPro
- téléchargement/processus des assets avec processing_steps
- manifest.json + <pack>.txt
- intégration HATS-Tools + HATS-Installer-Payload + config.ini
"""

import json
import datetime
import hashlib
import tempfile
import zipfile
import shutil
import re
import os
from pathlib import Path
from urllib import request, error

# ---------------------------------------------------------------------------
# Constantes / chemins
# ---------------------------------------------------------------------------

COMPONENTS_FILE = Path("components.json")
LOCAL_MANIFEST_FILE = Path("last_manifest.json")
TOOLS_MANIFEST_FILE = Path("tools_manifest.json")
OUTPUT_DIR = Path("dist")

ATMOSPHERE_REPO = "Atmosphere-NX/Atmosphere"
HATS_TOOLS_REPO = "sthetix/HATS-Tools"
HATS_INSTALLER_PAYLOAD_REPO = "sthetix/HATS-Installer-Payload"
HATSKITPRO_SKELETON_URL = "https://raw.githubusercontent.com/sthetix/HATSKitPro/main/assets/skeleton.zip"

DOWNLOAD_RETRIES = 3  # retries téléchargements


# ---------------------------------------------------------------------------
# Utilitaires HTTP
# ---------------------------------------------------------------------------

def log(msg: str):
    print(msg, flush=True)


def _http_get(url: str, accept: str = "application/vnd.github+json", timeout: int = 30) -> bytes:
    headers = {
        "Accept": accept,
        "User-Agent": "HATS-CI-Builder",
    }
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = request.Request(url, headers=headers, method="GET")
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except error.HTTPError as e:
        body = e.read().decode(errors="ignore")
        raise RuntimeError(f"HTTP {e.code} for {url}: {body}") from e
    except Exception as e:
        raise RuntimeError(f"HTTP error for {url}: {e}") from e


def _http_get_json(url: str, timeout: int = 30):
    data = _http_get(url, accept="application/vnd.github+json", timeout=timeout)
    return json.loads(data.decode("utf-8"))


# ---------------------------------------------------------------------------
# Lecture des composants + sélection
# ---------------------------------------------------------------------------

def load_components() -> dict:
    if not COMPONENTS_FILE.exists():
        raise SystemExit(f"{COMPONENTS_FILE} not found")
    with open(COMPONENTS_FILE, encoding="utf-8") as f:
        return json.load(f)


def get_selected_components_ids(components: dict) -> list:
    """
    CI: ignorer le flag default, builder sur TOUS les composants.
    """
    return list(components.keys())


# ---------------------------------------------------------------------------
# Skeleton depuis HATSKitPro
# ---------------------------------------------------------------------------

def download_skeleton_zip(tmp_dir: Path) -> Path:
    """
    Télécharge skeleton.zip depuis HATSKitPro.
    """
    dest = tmp_dir / "skeleton.zip"
    log("▶ Downloading skeleton.zip from HATSKitPro...")
    data = _http_get(HATSKITPRO_SKELETON_URL, accept="application/octet-stream", timeout=60)
    with open(dest, "wb") as f:
        f.write(data)
    log(f"  ✅ skeleton.zip downloaded to {dest}")
    return dest


# ---------------------------------------------------------------------------
# Logique issue du builder original : asset configs, download, processing
# ---------------------------------------------------------------------------

def compute_content_hash_from_components(components: dict, selected_ids: list) -> str:
    """
    Equivalent à compute_content_hash du builder :
    hash sur comp_id + version (asset_info.version).
    """
    hasher = hashlib.sha1()
    for comp_id in sorted(selected_ids):
        version = components[comp_id].get("asset_info", {}).get("version", "N/A")
        hasher.update(f"{comp_id}:{version}".encode("utf-8"))
    return hasher.hexdigest()[:7]


def extract_firmware_version_from_body(release_body: str) -> str:
    """
    Version extraite de builder.extract_firmware_version_from_body.
    """
    if not release_body:
        return "N/A"

    pattern_1 = re.search(
        r"(?:support|support was added|HOS)\s*.*?(?:for|up to)\s*(\d+\.\d+\.\d+)",
        release_body,
        re.IGNORECASE,
    )
    if pattern_1:
        return pattern_1.group(1)

    match_2 = re.search(r"(?:HOS|firmware)\s*(\d+\.\d+\.\d+)", release_body, re.IGNORECASE)
    if match_2:
        return match_2.group(1)

    match_3 = re.search(r"supports\s*up\s*to\s*(\d+\.\d+\.\d+)", release_body, re.IGNORECASE)
    if match_3:
        return match_3.group(1)

    return "N/A"


def get_atmosphere_firmware_info(atmosphere_version: str) -> tuple[str, str]:
    """
    Rapproche de get_atmosphere_firmware_info (sans PAT GUI, on prend GITHUB_TOKEN).
    Retourne (firmware_version, content_hash_tag).
    """
    try:
        api_url = f"https://api.github.com/repos/{ATMOSPHERE_REPO}/releases?per_page=3"
        log("  Fetching Atmosphere release info for firmware support...")
        releases = _http_get_json(api_url, timeout=15)

        clean_version = atmosphere_version.lstrip("v")
        latest_known_fw = "N/A"
        target_fw = "N/A"
        content_hash = "N/A"

        for release in releases:
            tag = (release.get("tag_name") or "").lstrip("v")
            body = release.get("body", "")

            fw_found = extract_firmware_version_from_body(body)
            if fw_found != "N/A":
                latest_known_fw = fw_found
                current_fw = fw_found
            else:
                current_fw = latest_known_fw

            if tag == clean_version:
                target_fw = current_fw
                hash_match = re.search(r"-([a-f0-9]{7,})", release.get("tag_name", ""))
                if hash_match:
                    content_hash = hash_match.group(1)[:7]
                break

        if target_fw != "N/A":
            log(f"  ✅ Atmosphere {atmosphere_version} supports firmware up to {target_fw}")
        else:
            log(f"  ⚠️ Could not determine firmware support for Atmosphere {atmosphere_version}")

        return target_fw, content_hash
    except Exception as e:
        log(f"  ⚠️ Error fetching Atmosphere firmware info: {e}")
        return "N/A", "N/A"


def get_asset_configs(comp_data: dict) -> list:
    """
    Equivalent simplifié de _get_asset_configs : accepte soit un dict, soit une liste.
    Dans components.json de HATSKitPro, c’est généralement "asset_patterns".
    """
    configs = comp_data.get("asset_patterns") or comp_data.get("asset_info", {}).get("asset_patterns")
    if not configs:
        return []
    if isinstance(configs, dict):
        return [configs]
    return list(configs)


def download_file_with_retries(url: str, dest: Path, max_retries: int = DOWNLOAD_RETRIES):
    """
    Equivalent à _download_file_with_progress, mais sans GUI/progress.
    """
    for attempt in range(1, max_retries + 1):
        try:
            log(f"    Downloading {url} (attempt {attempt}/{max_retries})")
            data = _http_get(url, accept="application/octet-stream", timeout=60)
            with open(dest, "wb") as f:
                f.write(data)
            return
        except Exception as e:
            log(f"    ⚠️ Download attempt {attempt} failed: {e}")
            if attempt == max_retries:
                raise


def download_asset(comp_data: dict, temp_dir: Path, pattern: str, version: str) -> Path:
    """
    Equivalent fonctionnel de _download_asset (github_release / direct_url).
    """
    source_type = comp_data.get("source_type") or comp_data.get("sourcetype", "")
    repo = comp_data.get("repo", "")

    if source_type == "direct_url":
        if not repo:
            raise RuntimeError("direct_url component without repo (URL)")
        url = repo
        filename = os.path.basename(url.split("?")[0])
        dest = temp_dir / filename
        download_file_with_retries(url, dest)
        return dest

    # github_release
    if not repo:
        raise RuntimeError("github_release component without repo")

    api_url = f"https://api.github.com/repos/{repo}/releases?per_page=20"
    releases = _http_get_json(api_url, timeout=30)

    wanted_version = (version or "").lstrip("v")
    matched_asset_url = None
    matched_name = None

    for rel in releases:
        tag = (rel.get("tag_name") or "")
        clean_tag = tag.lstrip("v")

        if version not in ("N/A", "", None):
            if wanted_version not in clean_tag:
                continue

        assets = rel.get("assets", [])
        for asset in assets:
            name = asset.get("name", "")
            if not name:
                continue
            if re.fullmatch(pattern.replace("*", ".*"), name):
                matched_asset_url = asset.get("browser_download_url")
                matched_name = name
                break
        if matched_asset_url:
            break

    if not matched_asset_url:
        raise RuntimeError(f"No asset matching pattern '{pattern}' for repo {repo} version {version}")

    dest = temp_dir / matched_name
    download_file_with_retries(matched_asset_url, dest)
    return dest


def _normalize_rel_path(p: str) -> str:
    """
    Interpréter les chemins JSON comme relatifs à la racine du pack.
    - enlève un / initial éventuel
    """
    if not p:
        return ""
    return p.lstrip("/")


def process_asset(asset_path: Path, comp_data: dict, staging_dir: Path, processing_steps: list) -> list:
    """
    Equivalent fonctionnel de _process_asset (sans GUI/log détaillé).
    Gère les actions principales de components.json.
    """
    processed_files = []

    for step in processing_steps:
        action = step.get("action")

        if action in ("unziptoroot", "unzip_to_root"):
            with zipfile.ZipFile(asset_path, "r") as z:
                z.extractall(staging_dir)
                for n in z.namelist():
                    processed_files.append(staging_dir / n)
            log("    - unzip_to_root completed")

        elif action == "unzip_to_path":
            raw_target = step.get("target_path") or step.get("targetpath", "")
            target_path = _normalize_rel_path(raw_target)
            if not target_path:
                continue
            with zipfile.ZipFile(asset_path, "r") as z:
                for member in z.namelist():
                    dest = staging_dir / target_path / member
                    if member.endswith("/"):
                        dest.mkdir(parents=True, exist_ok=True)
                    else:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        with z.open(member) as src, open(dest, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        processed_files.append(dest)
            log(f"    - unzip_to_path -> /{target_path}")

        elif action in ("unzipsubfoldertoroot", "unzip_subfolder_to_root"):
            subfolder = step.get("subfoldername") or step.get("subfolder_name")
            if not subfolder:
                continue
            with zipfile.ZipFile(asset_path, "r") as z:
                for member in z.namelist():
                    if member.startswith(subfolder.rstrip("/") + "/"):
                        rel = Path(member).relative_to(subfolder)
                        dest = staging_dir / rel
                        if member.endswith("/"):
                            dest.mkdir(parents=True, exist_ok=True)
                        else:
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            with z.open(member) as src, open(dest, "wb") as dst:
                                shutil.copyfileobj(src, dst)
                            processed_files.append(dest)
            log(f"    - unzip_subfolder_to_root '{subfolder}'")

        elif action in ("copyfile", "copy_file"):
            raw_target = step.get("target_path") or step.get("targetpath")
            if not raw_target:
                continue
            target_rel = _normalize_rel_path(raw_target)
            dest = staging_dir / target_rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(asset_path, dest)
            processed_files.append(dest)
            log(f"    - copy_file -> /{target_rel}")

        elif action == "find_and_copy":
            pattern = step.get("pattern")
            raw_target = step.get("target_path") or step.get("targetpath")
            if not pattern or not raw_target:
                continue
            target_rel = _normalize_rel_path(raw_target)
            with zipfile.ZipFile(asset_path, "r") as z:
                for member in z.namelist():
                    if re.fullmatch(pattern.replace("*", ".*"), os.path.basename(member)) and not member.endswith("/"):
                        dest = staging_dir / target_rel
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        with z.open(member) as src, open(dest, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        processed_files.append(dest)
                        break
            log(f"    - find_and_copy pattern={pattern} -> /{target_rel}")

        elif action == "find_and_rename":
            pattern = step.get("pattern")
            new_name = step.get("new_name")
            if not pattern or not new_name:
                continue
            target = None
            for p in staging_dir.rglob("*"):
                if p.is_file() and re.fullmatch(pattern.replace("*", ".*"), p.name):
                    target = p
                    break
            if target:
                new_path = target.with_name(new_name)
                target.rename(new_path)
                processed_files.append(new_path)
                log(f"    - find_and_rename {target.name} -> {new_name}")

        elif action in ("deletefile", "delete_file"):
            raw_target = step.get("path")
            if not raw_target:
                continue
            target_rel = _normalize_rel_path(raw_target)
            dest = staging_dir / target_rel
            if dest.exists():
                dest.unlink()
                log(f"    - delete_file /{target_rel}")

        else:
            log(f"    - Skipped unsupported action: {action}")

    return processed_files


# ---------------------------------------------------------------------------
# Cache outils (tools_manifest.json)
# ---------------------------------------------------------------------------

def load_tools_manifest() -> dict:
    if not TOOLS_MANIFEST_FILE.exists():
        return {}
    with open(TOOLS_MANIFEST_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_tools_manifest(data: dict):
    with open(TOOLS_MANIFEST_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def download_latest_release_asset(repo_full: str, prefer_zip: bool = True) -> Path:
    """
    Télécharge l'asset de la dernière release, avec cache basé sur tools_manifest.json.
    On n'appelle l'API GitHub que si aucune entrée ou fichier manquant.
    """
    tools_manifest = load_tools_manifest()
    entry = tools_manifest.get(repo_full, {})

    cached_path_str = entry.get("path")
    cached_tag = entry.get("tag")
    cache_path = Path(cached_path_str) if cached_path_str else None

    if cache_path and cache_path.exists() and cached_tag:
        log(f"  • Using cached asset for {repo_full} (tag {cached_tag}) at {cache_path}")
        return cache_path

    api_url = f"https://api.github.com/repos/{repo_full}/releases/latest"
    rel = _http_get_json(api_url, timeout=30)
    assets = rel.get("assets", [])
    if not assets:
        raise RuntimeError(f"No assets in latest release for {repo_full}")

    asset = None
    if prefer_zip:
        for a in assets:
            if a.get("name", "").endswith(".zip"):
                asset = a
                break
    if not asset:
        asset = assets[0]

    url = asset["browser_download_url"]
    name = asset["name"]
    tag = rel.get("tag_name", "")

    dest = Path(tempfile.gettempdir()) / name
    log(f"  • Downloading asset from {repo_full}: {name} (tag {tag})")
    data = _http_get(url, accept="application/octet-stream", timeout=60)
    with open(dest, "wb") as f:
        f.write(data)

    tools_manifest[repo_full] = {
        "tag": tag,
        "path": str(dest),
    }
    save_tools_manifest(tools_manifest)

    return dest


def integrate_hats_tools(staging_dir: Path):
    zpath = download_latest_release_asset(HATS_TOOLS_REPO, prefer_zip=True)
    with zipfile.ZipFile(zpath, "r") as z:
        z.extractall(staging_dir)
    log("  ✅ HATS-Tools extracted into pack")


def integrate_installer_payload(staging_dir: Path):
    zpath = download_latest_release_asset(HATS_INSTALLER_PAYLOAD_REPO, prefer_zip=False)

    if zpath.suffix == ".bin":
        tmp_bin = zpath
    else:
        with zipfile.ZipFile(zpath, "r") as z:
            bin_members = [m for m in z.namelist() if m.endswith(".bin")]
            if not bin_members:
                raise RuntimeError("No .bin payload found in HATS-Installer-Payload asset")
            member = bin_members[0]
            tmp_bin = Path(tempfile.gettempdir()) / Path(member).name
            with z.open(member) as src, open(tmp_bin, "wb") as dst:
                shutil.copyfileobj(src, dst)

    dest = staging_dir / "bootloader" / "payloads" / "hats-installer.bin"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(tmp_bin, dest)
    log(f"  ✅ Payload placed at {dest}")


def create_config_ini(staging_dir: Path, target_repo: str):
    """
    Génère un config.ini pour HATS Tools.
    pack_url = releases du repo cible (owner/repo), dans /config/hats-tool/config.ini.
    """
    repo = target_repo or os.getenv("GITHUB_REPOSITORY", "sthetix/HATS")
    pack_url = f"https://api.github.com/repos/{repo}/releases"

    cfg_path = staging_dir / "config" / "hats-tool" / "config.ini"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write(
            "[pack]\n"
            f"pack_url={pack_url}\n\n"
            "[installer]\n"
            "payload=/bootloader/payloads/hats-installer.bin\n"
            "staging_path=/hats-staging\n"
            "install_mode=overwrite\n\n"
            "[firmware]\n"
            "firmware_url=https://api.github.com/repos/sthetix/NXFW/releases\n"
        )
    log(f"  ✅ config.ini generated with pack_url={pack_url}")


# ---------------------------------------------------------------------------
# Manifest + metadata txt (fidèle au builder)
# ---------------------------------------------------------------------------

def load_last_manifest() -> dict:
    if not LOCAL_MANIFEST_FILE.exists():
        return {}
    with open(LOCAL_MANIFEST_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_last_manifest(manifest: dict):
    with open(LOCAL_MANIFEST_FILE, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)


def generate_metadata_txt(staging_dir: Path, manifest: dict, last_manifest: dict, final_base_name: str, build_comment: str = ""):
    """
    Reprend la génération du <base>.txt du builder (adaptée CI).
    """
    metadata_path = staging_dir / f"{final_base_name}.txt"
    last_build_components = last_manifest.get("components", {})

    with open(metadata_path, "w", encoding="utf-8") as f:
        f.write("# HATS Pack Summary\n\n")
        f.write(f"**Generated on:** {datetime.datetime.now(datetime.timezone.utc).strftime('%d-%m-%Y %H:%M:%S')} UTC  \n")
        f.write(f"**Builder Version:** {manifest.get('builder_version', 'CI')}-CI  \n")

        if manifest.get("content_hash") and manifest["content_hash"] != "N/A":
            f.write(f"**Content Hash:** {manifest['content_hash']}  \n")

        supported_fw = manifest.get("supported_firmware", "N/A")
        if supported_fw != "N/A":
            f.write(f"**Supported Firmware:** Up to {supported_fw}  \n")

        f.write("\n---\n\n")

        version_changes = []
        for comp_id, comp_data in manifest["components"].items():
            last_comp = last_build_components.get(comp_id)
            if last_comp and last_comp.get("version") != comp_data["version"]:
                version_changes.append(
                    f"- **{comp_data['name']}:** {last_comp.get('version')} → **{comp_data['version']}**"
                )

        if version_changes or build_comment:
            f.write("## CHANGELOG (What's New Since Last Build)\n\n")
            if build_comment:
                f.write(f"### Build Notes:\n{build_comment}\n\n")
            if version_changes:
                f.write("### Version Updates:\n")
                for change in version_changes:
                    f.write(change + "\n")
            f.write("\n---\n\n")

        f.write("## INCLUDED COMPONENTS\n")
        components_by_category = {}
        for comp_id, comp_data in manifest["components"].items():
            category = comp_data.get("category", "Uncategorized")
            components_by_category.setdefault(category, []).append(comp_data)

        for category, comps in sorted(components_by_category.items()):
            f.write(f"\n### {category.upper()}\n")
            for comp in sorted(comps, key=lambda x: x["name"]):
                repo_info = comp.get("repo", "")
                if repo_info:
                    f.write(f"- **{comp['name']}** ({comp['version']}) - {repo_info}\n")
                else:
                    f.write(f"- **{comp['name']}** ({comp['version']})\n")

        f.write("\n---\n\n")
        f.write("<sub>Generated with CI Builder</sub>\n")

    log(f"  ✅ {metadata_path.name} created.")


# ---------------------------------------------------------------------------
# Build principal (équivalent _worker_build_pack en mode CLI)
# ---------------------------------------------------------------------------

def build_ci_pack(build_comment: str = "", target_repo: str = "", channel: str = "release") -> Path:
    components = load_components()
    selected_ids = get_selected_components_ids(components)
    if not selected_ids:
        raise SystemExit("No components selected; check components.json")

    last_manifest = load_last_manifest()
    last_build_fw = last_manifest.get("supported_firmware", "N/A")
    last_components = last_manifest.get("components", {})

    now = datetime.datetime.now(datetime.timezone.utc)
    date_str = now.strftime("%d%m%Y")

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        staging_dir = temp_path / "staging"
        staging_dir.mkdir(parents=True, exist_ok=True)
        log(f"Created temporary staging area: {staging_dir}")

        # Skeleton
        try:
            skeleton_zip = download_skeleton_zip(temp_path)
            log("▶ Extracting skeleton.zip...")
            with zipfile.ZipFile(skeleton_zip, "r") as z:
                z.extractall(staging_dir)
            log("  ✅ Skeleton extracted successfully.")

            # Purger /switch issu du skeleton
            switch_dir = staging_dir / "switch"
            if switch_dir.exists():
                log("▶ Clearing /switch from skeleton...")
                for p in switch_dir.rglob("*"):
                    if p.is_file():
                        p.unlink()
                for d in sorted(switch_dir.rglob("*"), reverse=True):
                    if d.is_dir():
                        try:
                            d.rmdir()
                        except OSError:
                            pass
                log("  ✅ /switch cleared from skeleton.")

        except Exception as e:
            log(f"⚠️ WARNING: skeleton.zip issue: {e}")

        manifest = {
            "pack_name": "pending.zip",
            "build_date": now.isoformat(),
            "builder_version": "CI",
            "supported_firmware": last_build_fw,
            "content_hash": "pending",
            "components": {},
        }

        any_component_failed = False

        for comp_id in selected_ids:
            comp_data = components.get(comp_id)
            if not comp_data:
                log(f"❌ ERROR: Component '{comp_id}' not found in definitions. Skipping.")
                any_component_failed = True
                break

            manual_version = None
            if manual_version:
                version_to_build = manual_version
                log(f"\n▶ Processing: {comp_data['name']} ({version_to_build}) [MANUAL]")
            else:
                version_to_build = comp_data.get("asset_info", {}).get("version", "N/A")
                log(f"\n▶ Processing: {comp_data['name']} ({version_to_build})")

            asset_configs = get_asset_configs(comp_data)
            if not asset_configs:
                log("  ❌ No asset patterns defined. Skipping component.")
                any_component_failed = True
                break

            last_comp = last_components.get(comp_id)
            same_version = last_comp and last_comp.get("version") == version_to_build

            all_component_files = []
            component_failed = False

            if same_version:
                log("  🔁 Same version as last build: skipping GitHub API calls for assets.")
            else:
                for idx, asset_config in enumerate(asset_configs, 1):
                    asset_pattern = asset_config.get("pattern")
                    asset_processing_steps = asset_config.get("processing_steps", [])

                    if len(asset_configs) > 1:
                        log(f"  → Asset {idx}/{len(asset_configs)}: {asset_pattern}")

                    try:
                        asset_path = download_asset(comp_data, temp_path, pattern=asset_pattern, version=version_to_build)
                        if not asset_path:
                            log(f"  ❌ FAILED to download '{asset_pattern}'. Skipping this asset.")
                            component_failed = True
                            break
                    except Exception as e:
                        log(f"  ❌ FAILED during download of '{asset_pattern}': {e}")
                        component_failed = True
                        break

                    try:
                        processed_files = process_asset(asset_path, comp_data, staging_dir, asset_processing_steps)
                        if processed_files is None:
                            log(f"  ❌ FAILED during processing of '{asset_pattern}'.")
                            component_failed = True
                            break
                        all_component_files.extend(processed_files)
                    except Exception as e:
                        log(f"  ❌ FAILED during processing of '{asset_pattern}': {e}")
                        component_failed = True
                        break

            if component_failed:
                log(f"  ❌ Component '{comp_data['name']}' failed. Halting build.")
                any_component_failed = True
                break

            manifest["components"][comp_id] = {
                "name": comp_data["name"],
                "version": version_to_build,
                "category": comp_data.get("category", "Unknown"),
                "repo": comp_data.get("repo", ""),
                "files": [str(p.relative_to(staging_dir)).replace("\\", "/") for p in all_component_files],
            }

            if comp_id == "atmosphere":
                if same_version and last_manifest.get("supported_firmware"):
                    manifest["supported_firmware"] = last_manifest["supported_firmware"]
                    log(f"\n▶ Reusing cached Atmosphere firmware info: up to {manifest['supported_firmware']}")
                else:
                    log("\n▶ Scanning Atmosphere release for firmware support info...")
                    firmware_ver, _ = get_atmosphere_firmware_info(version_to_build)
                    manifest["supported_firmware"] = firmware_ver

        if any_component_failed:
            raise SystemExit("❌ Build failed because one or more components could not be processed.")

        # Hash de contenu
        log("\n▶ Computing content hash from downloaded versions...")
        content_hash = compute_content_hash_from_components(components, selected_ids)
        manifest["content_hash"] = content_hash
        log(f"  ✅ Content hash: {content_hash}")

        # Nom final : HATS-<date>-<hash>_<repoName>_<channel>.zip
        repo_name = ""
        if target_repo:
            repo_name = target_repo.split("/")[-1]
        else:
            gr = os.getenv("GITHUB_REPOSITORY", "")
            if gr:
                repo_name = gr.split("/")[-1]

        sep = "_"
        suffix_parts = []
        if repo_name:
            suffix_parts.append(repo_name)
        if channel:
            suffix_parts.append(channel)
        suffix = ""
        if suffix_parts:
            suffix = sep + sep.join(suffix_parts)

        final_base_name = f"HATS-{date_str}-{content_hash}{suffix}"
        final_pack_name = f"{final_base_name}.zip"
        manifest["pack_name"] = final_pack_name

        # Intégration HATS-Tools / Payload / config.ini
        log("\n▶ Integrating HATS-Tools...")
        integrate_hats_tools(staging_dir)

        log("▶ Integrating HATS-Installer-Payload...")
        integrate_installer_payload(staging_dir)

        log("▶ Generating config.ini...")
        create_config_ini(staging_dir, target_repo)

        # manifest.json (dans staging ET racine du zip)
        log("\n▶ Generating manifest.json...")
        manifest_path = staging_dir / "manifest.json"
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
        log("  ✅ Manifest created in staging.")

        # metadata txt
        log("▶ Generating metadata txt...")
        generate_metadata_txt(staging_dir, manifest, last_manifest, final_base_name, build_comment)

        # ZIP final (manifest.json aussi à la racine)
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        final_zip = OUTPUT_DIR / final_pack_name
        log("\n▶ Creating final ZIP archive...")
        with zipfile.ZipFile(final_zip, "w", zipfile.ZIP_DEFLATED) as zipf:
            zipf.writestr("manifest.json", json.dumps(manifest, indent=2))
            for file_path in staging_dir.rglob("*"):
                arcname = file_path.relative_to(staging_dir)
                zipf.write(file_path, arcname)
        log(f"  ✅ Pack saved to: {final_zip}")

        save_last_manifest(manifest)
        log("  ✅ Updated local manifest.json (last build reference).")

        return final_zip


# ---------------------------------------------------------------------------
# Entrée CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    comment = os.getenv("BUILD_COMMENT", "")
    target_repo = ""
    channel = "release"

    if len(sys.argv) > 1:
        target_repo = sys.argv[1]  # owner/repo pour pack_url
    if len(sys.argv) > 2:
        channel = sys.argv[2]      # canal: release/beta/dev...

    out = build_ci_pack(build_comment=comment, target_repo=target_repo, channel=channel)

    out = Path(out).resolve()
    # Logs humains
    print(f"\n=== CI PACK READY ===\n{out}")
    # Variables faciles à parser dans le workflow
    print(f"ZIP_PATH={out}")
    print(f"ZIP_NAME={out.name}")
