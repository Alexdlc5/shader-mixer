"""One-time installer: Fabric + Sodium/Iris/DH + realism shaders & packs for the newest MC release.
Re-run any time to update everything to the latest 26.x builds."""
import json, os, sys, urllib.request, urllib.parse, datetime, pathlib, hashlib

sys.stdout.reconfigure(errors="replace")  # pack names carry emoji / § codes

MC = "26.3"
GAME = pathlib.Path(os.environ["APPDATA"]) / ".minecraft-ultra"
MCDIR = pathlib.Path(os.environ["APPDATA"]) / ".minecraft"
MODS = ["fabric-api", "sodium", "iris", "distanthorizons", "entitytexturefeatures",
        "entity-model-features", "continuity"]
SHADERS = ["complementary-unbound", "complementary-reimagined", "bliss-shader", "bsl-shaders",
           "noble", "sildurs-vibrant-shaders"]  # realism picks; every other 26.3 shader is added by all_shaders()
PACKS = ["patrix-32x", "rotrblocks", "default-hd-128x", "modernarch", "primes-hd-textures",
         "simplista", "fresh-animations", "fast-better-grass"]
MANIFEST = GAME / "installed.json"
INSTALLED = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}
UA = {"User-Agent": "Alexdlc5/mc-realism-picker/1.0"}


def get(url):
    if not url.startswith("https://"):
        raise ValueError(f"refusing non-HTTPS URL: {url}")
    return urllib.request.urlopen(  # nosec B310
urllib.request.Request(url, headers=UA), timeout=120).read()


def all_shaders():
    """Every Modrinth shader with a build tagged for this MC version, most downloaded first."""
    slugs, offset = list(SHADERS), 0
    while True:
        q = urllib.parse.urlencode({"limit": 100, "offset": offset, "index": "downloads",
                                    "facets": json.dumps([["project_type:shader"], [f"versions:{MC}"]])})
        hits = json.loads(get(f"https://api.modrinth.com/v2/search?{q}"))["hits"]
        slugs += [h["slug"] for h in hits if h["slug"] not in slugs]
        if len(hits) < 100: return slugs
        offset += 100


def fetch(slug, folder, loaders):
    q = urllib.parse.urlencode({"game_versions": json.dumps([MC]), "loaders": json.dumps(loaders)})
    versions = json.loads(get(f"https://api.modrinth.com/v2/project/{slug}/version?{q}"))
    if not versions:
        print(f"  !! {slug}: no {MC} build, skipped"); return
    v = next((v for v in versions if v["version_type"] == "release"), versions[0])  # stable beats alpha/beta
    f = next((f for f in v["files"] if f["primary"]), v["files"][0])
    # Trust boundary: names and URLs come from a remote API.
    if pathlib.Path(f["filename"]).name != f["filename"] or not f["filename"].endswith((".jar", ".zip")):
        print(f"  !! {slug}: suspicious filename {f['filename']!r}, skipped"); return
    if urllib.parse.urlparse(f["url"]).netloc != "cdn.modrinth.com":
        print(f"  !! {slug}: unexpected download host, skipped"); return
    dest = folder / f["filename"]
    prev = INSTALLED.get(slug)
    if prev and prev != f["filename"]:  # replace the older build this script installed earlier
        for p in (folder / prev, folder / (prev + ".disabled")):
            p.unlink(missing_ok=True)
    INSTALLED[slug] = f["filename"]
    if dest.exists() or dest.with_name(dest.name + ".disabled").exists():
        print(f"  ok {f['filename']}"); return
    print(f"  get {f['filename']} ({f['size'] >> 20} MB)")
    data = get(f["url"])
    if hashlib.sha512(data).hexdigest() != f["hashes"]["sha512"]:
        INSTALLED.pop(slug); print(f"  !! {slug}: SHA-512 mismatch, discarded"); return
    dest.write_bytes(data)


def main():
    for d in ("mods", "shaderpacks", "resourcepacks", "config"):
        (GAME / d).mkdir(parents=True, exist_ok=True)

    loader = next(l["version"] for l in json.loads(get("https://meta.fabricmc.net/v2/versions/loader")) if l["stable"])
    vid = f"fabric-loader-{loader}-{MC}"
    vdir = MCDIR / "versions" / vid
    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / f"{vid}.json").write_bytes(get(f"https://meta.fabricmc.net/v2/versions/loader/{MC}/{loader}/profile/json"))
    print("Fabric", vid)

    try:
        print("Mods:");    [fetch(s, GAME / "mods", ["fabric"]) for s in MODS]
        print("Shaders:"); [fetch(s, GAME / "shaderpacks", ["iris", "optifine"]) for s in all_shaders()]
        print("Packs:");   [fetch(s, GAME / "resourcepacks", ["minecraft"]) for s in PACKS]
    finally:
        MANIFEST.write_text(json.dumps(INSTALLED, indent=2))

    # Smooth-but-pretty defaults for an RTX 3080; DH supplies the far terrain so vanilla distance stays modest.
    opts = GAME / "options.txt"
    if not opts.exists():
        opts.write_text("renderDistance:12\nsimulationDistance:8\nmipmapLevels:4\nentityShadows:true\n"
                        "biomeBlendRadius:3\nenableVsync:true\nmaxFps:260\n")

    # Profile in the official launcher (launcher must be closed or it overwrites this file).
    lp_path = MCDIR / "launcher_profiles.json"
    lp = json.loads(lp_path.read_text())
    key = "mc-ultra-realism"
    lp["profiles"][key] = {
        **lp["profiles"].get(key, {}),
        "name": "Ultra Realism (shaders)", "type": "custom", "icon": "Grass",
        "lastVersionId": vid, "gameDir": str(GAME),
        "javaArgs": "-Xmx8G -Xms4G -XX:+UseZGC",
        "created": lp["profiles"].get(key, {}).get("created", datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")),
    }
    lp_path.write_text(json.dumps(lp, indent=2))
    print(f"\nDone. Launcher profile 'Ultra Realism (shaders)' -> {GAME}")


if __name__ == "__main__":
    main()
