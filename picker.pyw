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
    "sky": (r"optifine/sky/", "fabricskyboxes", "OptiFine custom skies"),  # nothing for 26.3 renders these
}
LAUNCHER = r"shell:AppsFolder\Microsoft.4297127D64EC6_8wekyb3d8bbwe!Minecraft"
BUILTIN_PRESETS = pathlib.Path(__file__).with_name("presets.json")
USER_PRESETS = GAME / "my_presets.json"


# ---------- presets: {"name", "shader", "packs" (top wins), "mods_off"}; entries are Modrinth slugs or filenames ----------
def load_presets():
    builtin = [{**p, "builtin": True} for p in json.loads(BUILTIN_PRESETS.read_text(encoding="utf-8"))]
    try: user = json.loads(USER_PRESETS.read_text(encoding="utf-8"))
    except (OSError, ValueError): user = []
    return builtin + [p for p in user if isinstance(p, dict) and p.get("name")]


def save_user_presets(presets):
    mine = [{k: v for k, v in p.items() if k != "builtin"} for p in presets if not p.get("builtin")]
    USER_PRESETS.write_text(json.dumps(mine, indent=2), encoding="utf-8")


def resolve_preset(preset, installed, shader_files, pack_files):
    """Map slugs to the files setup.py installed. Returns (shader file or None, pack files, missing names)."""
    to_file = lambda x: installed.get(x, x)
    shader = preset.get("shader") and to_file(preset["shader"])
    packs = [to_file(p) for p in preset.get("packs", [])]
    missing = ([shader] if shader and shader not in shader_files else []) + [p for p in packs if p not in pack_files]
    return (shader if shader in shader_files else None), [p for p in packs if p in pack_files], missing


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
    from tkinter import ttk, messagebox, simpledialog

    if not (GAME / "mods").exists():
        messagebox.showerror("Realism Picker", "Run setup.py first."); return
    game_fmt, feats = game_format(), iris_features()
    shaders = {f.name: {**inspect_shader(f), "name": f.stem} for f in sorted((GAME / "shaderpacks").glob("*.zip"), key=lambda p: p.name.lower())}
    packs = {f.name: inspect_pack(f) for f in sorted((GAME / "resourcepacks").glob("*.zip"), key=lambda p: p.name.lower())}
    mod_files = sorted(f.name.removesuffix(".disabled") for f in (GAME / "mods").iterdir() if f.name.endswith((".jar", ".jar.disabled")))
    mods = {f: inspect_mod(GAME / "mods" / (f if (GAME / "mods" / f).exists() else f + ".disabled")) for f in mod_files}

    try: installed = json.loads((GAME / "installed.json").read_text())
    except (OSError, ValueError): installed = {}
    presets = load_presets()

    root = tk.Tk(); root.title("Realism Picker — Minecraft 26.3"); root.geometry("1180x640")
    cols = ttk.Frame(root, padding=10); cols.pack(fill="both", expand=True)

    # shader packs (presets)
    prf = ttk.LabelFrame(cols, text="Shader packs", padding=6); prf.grid(row=0, column=0, sticky="nsew", padx=4)
    pr_list = tk.Listbox(prf, exportselection=False, width=24, height=12); pr_list.pack(fill="x")
    about = ttk.Label(prf, wraplength=190, justify="left", foreground="#555"); about.pack(fill="x", pady=6)

    def redraw_presets(sel=0):
        pr_list.delete(0, "end")
        for p in presets: pr_list.insert("end", ("★ " if p.get("builtin") else "   ") + p["name"])
        pr_list.selection_set(sel); pr_list.see(sel)

    # shaders
    sf = ttk.LabelFrame(cols, text="Shader (one)", padding=6); sf.grid(row=0, column=1, sticky="nsew", padx=4)
    shader_names = ["(none)"] + list(shaders)
    sh_list = tk.Listbox(sf, exportselection=False, width=38, height=24); sh_list.pack(fill="both", expand=True)
    for n in shader_names: sh_list.insert("end", n.removesuffix(".zip") + ("" if n == "(none)" or shaders[n]["dh"] else "   [no DH]"))

    # packs (ordered, top = highest priority)
    pf = ttk.LabelFrame(cols, text="Resource packs (top wins)", padding=6); pf.grid(row=0, column=2, sticky="nsew", padx=4)
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
    mf = ttk.LabelFrame(cols, text="Mods", padding=6); mf.grid(row=0, column=3, sticky="nsew", padx=4)
    mod_on = {}
    for f in mod_files:
        v = tk.BooleanVar(value=(GAME / "mods" / f).exists()); mod_on[f] = v
        cb = ttk.Checkbutton(mf, text=mods[f]["id"], variable=v); cb.pack(anchor="w")
        if mods[f]["id"] in CORE_MODS: v.set(True); cb.state(["disabled"])
    cols.columnconfigure((1, 2), weight=1); cols.rowconfigure(0, weight=1)

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

    def current_preset():
        i = pr_list.curselection()
        return presets[i[0]] if i else None

    def use_preset(_=None):
        pr = current_preset()
        if not pr: return
        about.config(text=pr.get("about", "Your saved pack."))
        shader, sel, missing = resolve_preset(pr, installed, shaders, packs)
        s = shader_names.index(shader) if shader else 0
        sh_list.selection_clear(0, "end"); sh_list.selection_set(s); sh_list.see(s)
        for p in packs: chosen[p].set(p in sel)
        order.sort(key=lambda p: sel.index(p) if p in sel else len(sel))
        off = set(pr.get("mods_off", []))
        for f in mod_files: mod_on[f].set(mods[f]["id"] in CORE_MODS or mods[f]["id"] not in off)
        redraw(); validate()
        if missing: status.insert("1.0", "✖ Not installed (run setup.py): " + ", ".join(missing) + "\n")

    def save_preset():
        if validate():
            messagebox.showerror("Refused", "Only working combinations can be saved:\n\n" + status.get("1.0", "end")); return
        name = simpledialog.askstring("Save shader pack", "Name for this shader pack:", parent=root)
        if not name or not name.strip(): return
        name = name.strip()
        old = next((i for i, p in enumerate(presets) if p["name"].lower() == name.lower()), None)
        if old is not None and presets[old].get("builtin"):
            messagebox.showerror("Save shader pack", f"'{name}' is a built-in pack; pick another name."); return
        if old is not None and not messagebox.askyesno("Save shader pack", f"Replace your pack '{name}'?"): return
        s, _, sel_packs, en = selection()
        new = {"name": name, "shader": None if s == "(none)" else s, "packs": sel_packs,
               "mods_off": sorted(mods[f]["id"] for f in mod_files if f not in en)}
        if old is None: presets.append(new)
        else: presets[old] = new
        save_user_presets(presets); redraw_presets(presets.index(new)); about.config(text="Your saved pack.")

    def delete_preset():
        pr = current_preset()
        if not pr or pr.get("builtin"):
            messagebox.showinfo("Delete", "Built-in packs can't be deleted."); return
        if messagebox.askyesno("Delete", f"Delete '{pr['name']}'?"):
            presets.remove(pr); save_user_presets(presets); redraw_presets(); use_preset()

    pb = ttk.Frame(prf); pb.pack(fill="x")
    ttk.Button(pb, text="Save current…", command=save_preset).pack(side="left")
    ttk.Button(pb, text="Delete", command=delete_preset).pack(side="left", padx=4)
    pr_list.bind("<<ListboxSelect>>", use_preset)

    bar = ttk.Frame(root, padding=10); bar.pack(fill="x")
    ttk.Button(bar, text="Check", command=validate).pack(side="left")
    ttk.Button(bar, text="Save only", command=lambda: go(False)).pack(side="right")
    ttk.Button(bar, text="Save & Launch", command=lambda: go(True)).pack(side="right", padx=6)
    sh_list.bind("<<ListboxSelect>>", validate)
    for v in mod_on.values(): v.trace_add("write", lambda *_: validate())
    redraw_presets(); use_preset()
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
    inst = {"bsl": "BSL.zip", "fa": "FA.zip"}
    assert resolve_preset({"shader": "bsl", "packs": ["fa", "My.zip", "gone"]}, inst, {"BSL.zip"}, {"FA.zip", "My.zip"}) \
        == ("BSL.zip", ["FA.zip", "My.zip"], ["gone"])
    assert resolve_preset({"shader": None, "packs": []}, inst, set(), set()) == (None, [], [])
    assert json.loads(BUILTIN_PRESETS.read_text(encoding="utf-8"))[0]["name"] == "Ultra Realism"
    print("selftest ok")


if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else gui()
