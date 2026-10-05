"""Realism Picker: choose shader + resource packs + optional mods, refuse broken combos, launch Minecraft."""
import json, os, re, subprocess, sys, zipfile, pathlib, math

APPDATA = pathlib.Path(os.environ.get("APPDATA", "."))
GAME = APPDATA / ".minecraft-ultra"
MC_JAR = APPDATA / ".minecraft" / "versions" / "26.3" / "26.3.jar"
CORE_MODS = {"fabric-api", "sodium", "iris"}  # never toggleable
ALWAYS_THERE = ("minecraft", "java", "fabricloader", "fabric")  # dep ids satisfied by the loader / fabric-api
PACK_NEEDS = {  # resource-pack content -> mod id that renders it
    "ctm": (r"optifine/ctm/", "continuity", "connected textures"),
    "cem": (r"(optifine/cem/|/emf/)", "entity_model_features", "custom entity models"),
    "etf": (r"(optifine/(random|emissive|mob)/|/etf/)", "entity_texture_features", "random/emissive entity textures"),
}
LAUNCHER = r"shell:AppsFolder\Microsoft.4297127D64EC6_8wekyb3d8bbwe!Minecraft"
PRESET = {"shader": "ComplementaryUnbound", "packs": ["Patrix", "rotrBLOCKS", "FreshAnimations", "Fast Better Grass"],
          "mods": "all"}


# ---------- inspection (pure, read-only: zips are listed, never extracted) ----------
def fmt(v, hi=False):
    """pack.mcmeta format value -> (major, minor). Ints mean 'any minor' when used as an upper bound."""
    if isinstance(v, list): v = v[0] if len(v) == 1 else tuple(v)
    if isinstance(v, tuple): return (int(v[0]), int(v[1]) if len(v) > 1 else (math.inf if hi else 0))
    if isinstance(v, float): return (int(v), int(round((v - int(v)) * 10)))
    return (int(v), math.inf if hi else 0)


def pack_range(meta):
    p = meta["pack"]
    if "min_format" in p and "max_format" in p:
        return fmt(p["min_format"]), fmt(p["max_format"], hi=True)
    sf = p.get("supported_formats")
    if isinstance(sf, dict): return fmt(sf["min_inclusive"]), fmt(sf["max_inclusive"], hi=True)
    if isinstance(sf, list): return fmt(sf[0]), fmt(sf[1], hi=True)
    return fmt(p["pack_format"]), fmt(p["pack_format"], hi=True)


def inspect_pack(path):
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        try: rng = pack_range(json.loads(z.read("pack.mcmeta").decode("utf-8-sig"), strict=False))
        except Exception: rng = None
    return {"range": rng, "pbr": any(re.search(r"_[ns]\.png$", n) for n in names),
            "needs": {k for k, (rx, *_) in PACK_NEEDS.items() if any(re.search(rx, n) for n in names)}}


def inspect_shader(path):
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        props = "\n".join(z.read(n).decode("latin-1") for n in names if n.endswith("shaders/shaders.properties"))
    req = set()
    for m in re.finditer(r"iris\.features\.required\s*=\s*(.+)", props):
        req |= set(m.group(1).upper().split())
    return {"dh": any(re.search(r"shaders/(world-?\d+/)?dh_", n) for n in names), "requires": req}


def inspect_mod(path):
    with zipfile.ZipFile(path) as z:
        m = json.loads(z.read("fabric.mod.json").decode("utf-8-sig"), strict=False)
    return {"id": m["id"], "provides": set(m.get("provides", [])), "depends": set(m.get("depends", {}))}


def iris_features():
    jar = next(GAME.glob("mods/iris*.jar"), None)
    if not jar: return set()
    with zipfile.ZipFile(jar) as z:
        cls = next(n for n in z.namelist() if n.endswith("/FeatureFlags.class"))
        return {s.decode() for s in re.findall(rb"\b[A-Z][A-Z_]{2,}\b", z.read(cls))}


def game_format():
    with zipfile.ZipFile(MC_JAR) as z:
        pv = json.loads(z.read("version.json"))["pack_version"]
    return (pv["resource_major"], pv["resource_minor"])


# ---------- the rules ----------
def check(shader, packs, mods, game_fmt, iris_feats):
    """shader: info dict or None; packs: {name: info}; mods: {file: info} (enabled only).
    Returns (errors, warnings). Any error = refuse to launch."""
    err, warn = [], []
    ids = {m["id"] for m in mods.values()} | {p for m in mods.values() for p in m["provides"]}
    for f, m in mods.items():
        missing = [d for d in m["depends"] if d not in ids and not d.startswith(ALWAYS_THERE)]
        if missing: err.append(f"Mod {m['id']} needs {', '.join(missing)}, which is turned off.")
    for name, p in packs.items():
        if p["range"] is None:
            err.append(f"{name}: no readable pack.mcmeta, so the game will reject it.")
        elif not (p["range"][0] <= game_fmt <= p["range"][1]):
            err.append(f"{name}: made for pack format {p['range'][0][0]}–{p['range'][1][0]}, game needs {game_fmt[0]}.{game_fmt[1]}.")
        for need in p["needs"]:
            _, mod, what = PACK_NEEDS[need]
            if mod not in ids: err.append(f"{name} uses {what}; turn on the {mod} mod.")
        if p["pbr"] and not shader: warn.append(f"{name} has PBR maps that only show up with a shader on.")
    if shader:
        if "distanthorizons" in ids and not shader["dh"]:
            err.append(f"{shader['name']} has no Distant Horizons support; turn off Distant Horizons or pick another shader.")
        unsupported = shader["requires"] - iris_feats
        if unsupported:
            err.append(f"{shader['name']} needs Iris features this Iris lacks: {', '.join(sorted(unsupported))}.")
    return err, warn


# ---------- apply ----------
def set_option(lines, key, value):
    out = [l for l in lines if not l.startswith(key + ":")]
    return out + [f"{key}:{value}"]


def apply(shader_file, pack_files, enabled_mod_files, all_mod_files):
    for f in all_mod_files:  # Fabric only loads *.jar, so ".jar.disabled" turns a mod off
        on, off = GAME / "mods" / f, GAME / "mods" / (f + ".disabled")
        if f in enabled_mod_files and off.exists(): off.rename(on)
        if f not in enabled_mod_files and on.exists(): on.rename(off)
    (GAME / "config").mkdir(exist_ok=True)
    iris = GAME / "config" / "iris.properties"
    lines = iris.read_text().splitlines() if iris.exists() else []
    lines = [l for l in lines if not l.startswith(("shaderPack=", "enableShaders="))]
    lines += [f"enableShaders={'true' if shader_file else 'false'}"] + ([f"shaderPack={shader_file}"] if shader_file else [])
    iris.write_text("\n".join(lines) + "\n")
    opts = GAME / "options.txt"
    lines = opts.read_text().splitlines() if opts.exists() else []
    # options.txt lists packs lowest-priority first; the picker shows highest first.
    lines = set_option(lines, "resourcePacks", json.dumps(["vanilla"] + [f"file/{p}" for p in reversed(pack_files)]))
    lines = set_option(lines, "incompatibleResourcePacks", "[]")
    opts.write_text("\n".join(lines) + "\n")


def launch():
    lp = APPDATA / ".minecraft" / "launcher_profiles.json"
    try:  # make our profile the most recently used so the launcher preselects it
        import datetime
        d = json.loads(lp.read_text())
        d["profiles"]["mc-ultra-realism"]["lastUsed"] = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        lp.write_text(json.dumps(d, indent=2))
    except Exception:
        pass
    subprocess.Popen([os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "explorer.exe"), LAUNCHER])  # nosec B603 - fixed args


# ---------- GUI ----------
def gui():
    import tkinter as tk
    from tkinter import ttk, messagebox

    if not (GAME / "mods").exists():
        messagebox.showerror("Realism Picker", "Run setup.py first."); return
    game_fmt, feats = game_format(), iris_features()
    shaders = {f.name: {**inspect_shader(f), "name": f.stem} for f in sorted((GAME / "shaderpacks").glob("*.zip"), key=lambda p: p.name.lower())}
    packs = {f.name: inspect_pack(f) for f in sorted((GAME / "resourcepacks").glob("*.zip"), key=lambda p: p.name.lower())}
    mod_files = sorted(f.name.removesuffix(".disabled") for f in (GAME / "mods").iterdir() if f.name.endswith((".jar", ".jar.disabled")))
    mods = {f: inspect_mod(GAME / "mods" / (f if (GAME / "mods" / f).exists() else f + ".disabled")) for f in mod_files}

    root = tk.Tk(); root.title("Realism Picker — Minecraft 26.3"); root.geometry("920x620")
    cols = ttk.Frame(root, padding=10); cols.pack(fill="both", expand=True)

    # shaders
    sf = ttk.LabelFrame(cols, text="Shader (one)", padding=6); sf.grid(row=0, column=0, sticky="nsew", padx=4)
    shader_names = ["(none)"] + list(shaders)
    sh_list = tk.Listbox(sf, exportselection=False, width=38, height=24); sh_list.pack(fill="both", expand=True)
    for n in shader_names: sh_list.insert("end", n.removesuffix(".zip") + ("" if n == "(none)" or shaders[n]["dh"] else "   [no DH]"))

    # packs (ordered, top = highest priority)
    pf = ttk.LabelFrame(cols, text="Resource packs (top wins)", padding=6); pf.grid(row=0, column=1, sticky="nsew", padx=4)
    order = list(packs); chosen = {p: tk.BooleanVar() for p in packs}
    pk_list = tk.Listbox(pf, exportselection=False, width=44, height=20); pk_list.pack(fill="both", expand=True)

    def redraw(sel=None):
        pk_list.delete(0, "end")
        for p in order:
            tag = " PBR" if packs[p]["pbr"] else ""
            pk_list.insert("end", f"{'☑' if chosen[p].get() else '☐'} {p.removesuffix('.zip')}{tag}")
        if sel is not None: pk_list.selection_set(sel)

    def toggle(_=None):
        i = pk_list.curselection()
        if i: chosen[order[i[0]]].set(not chosen[order[i[0]]].get()); redraw(i[0])

    def move(d):
        i = pk_list.curselection()
        if not i or not 0 <= i[0] + d < len(order): return
        a = i[0]; order[a], order[a + d] = order[a + d], order[a]; redraw(a + d)

    pk_list.bind("<Double-Button-1>", toggle); pk_list.bind("<space>", toggle)
    bf = ttk.Frame(pf); bf.pack(fill="x")
    ttk.Button(bf, text="Toggle", command=toggle).pack(side="left")
    ttk.Button(bf, text="▲", width=3, command=lambda: move(-1)).pack(side="left")
    ttk.Button(bf, text="▼", width=3, command=lambda: move(1)).pack(side="left")

    # mods
    mf = ttk.LabelFrame(cols, text="Mods", padding=6); mf.grid(row=0, column=2, sticky="nsew", padx=4)
    mod_on = {}
    for f in mod_files:
        v = tk.BooleanVar(value=(GAME / "mods" / f).exists()); mod_on[f] = v
        cb = ttk.Checkbutton(mf, text=mods[f]["id"], variable=v); cb.pack(anchor="w")
        if mods[f]["id"] in CORE_MODS: v.set(True); cb.state(["disabled"])
    cols.columnconfigure((0, 1), weight=1); cols.rowconfigure(0, weight=1)

    status = tk.Text(root, height=6, wrap="word"); status.pack(fill="x", padx=14)

    def selection():
        i = sh_list.curselection(); s = shader_names[i[0]] if i else "(none)"
        sh = None if s == "(none)" else shaders[s]
        sel_packs = [p for p in order if chosen[p].get()]
        en = {f: mods[f] for f in mod_files if mod_on[f].get()}
        return s, sh, sel_packs, en

    def validate(_=None):
        s, sh, sel_packs, en = selection()
        err, warn = check(sh, {p: packs[p] for p in sel_packs}, en, game_fmt, feats)
        status.delete("1.0", "end")
        status.insert("end", "\n".join(["✖ " + e for e in err] + ["⚠ " + w for w in warn]) or "✔ This combination will load.")
        return err

    def go(do_launch):
        if validate():
            messagebox.showerror("Refused", "This combination won't load correctly:\n\n" + status.get("1.0", "end")); return
        s, _, sel_packs, en = selection()
        apply(None if s == "(none)" else s, sel_packs, set(en), mod_files)
        if do_launch:
            launch()
            messagebox.showinfo("Realism Picker", "Launcher opening — pick 'Ultra Realism (shaders)' and press Play.")
        else:
            messagebox.showinfo("Realism Picker", "Saved. It applies next time you press Play on 'Ultra Realism (shaders)'.")

    def preset():
        s = next((i for i, n in enumerate(shader_names) if n.startswith(PRESET["shader"])), 0)
        sh_list.selection_clear(0, "end"); sh_list.selection_set(s); sh_list.see(s)
        for p in packs: chosen[p].set(any(p.startswith(x) for x in PRESET["packs"]))
        order.sort(key=lambda p: next((i for i, x in enumerate(PRESET["packs"]) if p.startswith(x)), 99))
        for f in mod_files: mod_on[f].set(True)
        redraw(); validate()

    bar = ttk.Frame(root, padding=10); bar.pack(fill="x")
    ttk.Button(bar, text="Ultra Realism preset", command=preset).pack(side="left")
    ttk.Button(bar, text="Check", command=validate).pack(side="left", padx=6)
    ttk.Button(bar, text="Save only", command=lambda: go(False)).pack(side="right")
    ttk.Button(bar, text="Save & Launch", command=lambda: go(True)).pack(side="right", padx=6)
    sh_list.bind("<<ListboxSelect>>", validate)
    for v in mod_on.values(): v.trace_add("write", lambda *_: validate())
    preset()
    root.mainloop()


def selftest():
    g, feats = (97, 1), {"SSBO", "COMPUTE_SHADERS"}
    assert pack_range({"pack": {"min_format": [97.1], "max_format": [97.1]}}) == ((97, 1), (97, 1))
    assert pack_range({"pack": {"min_format": 84, "max_format": 999}})[1] == (999, math.inf)
    assert pack_range({"pack": {"pack_format": 15, "supported_formats": [15, 64]}})[1] == (64, math.inf)
    mod = lambda i, deps=(): {"id": i, "provides": set(), "depends": set(deps)}
    base = {"a": mod("fabric-api"), "b": mod("sodium"), "c": mod("iris", ["sodium", "fabricloader"])}
    ok = {"range": ((15, 0), (999, math.inf)), "pbr": True, "needs": set()}
    assert check({"name": "S", "dh": True, "requires": {"SSBO"}}, {"p": ok}, base, g, feats) == ([], [])
    assert check(None, {"p": ok}, base, g, feats)[1]                                     # PBR without shader warns
    assert check(None, {"p": {**ok, "range": ((15, 0), (64, math.inf))}}, base, g, feats)[0]   # too-old pack refused
    assert check(None, {"p": {**ok, "needs": {"cem"}}}, base, g, feats)[0]              # needs EMF
    dh = {**base, "d": mod("distanthorizons")}
    assert check({"name": "S", "dh": False, "requires": set()}, {}, dh, g, feats)[0]    # DH + non-DH shader
    assert check({"name": "S", "dh": True, "requires": {"NOPE"}}, {}, base, g, feats)[0]  # unknown Iris feature
    assert check(None, {}, {**base, "e": mod("entity_model_features", ["entity_texture_features"])}, g, feats)[0]
    print("selftest ok")


if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else gui()
