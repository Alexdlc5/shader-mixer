"""Shader Mixer: choose a shader + resource packs + optional mods, refuse broken combos, launch Minecraft."""
import json, os, re, subprocess, sys, zipfile, pathlib, math

APPDATA = pathlib.Path(os.environ.get("APPDATA", "."))
GAME = APPDATA / ".shader-mixer"
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
        d["profiles"]["shader-mixer"]["lastUsed"] = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        lp.write_text(json.dumps(d, indent=2))
    except Exception:
        pass
    subprocess.Popen([os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "explorer.exe"), LAUNCHER])  # nosec B603 - fixed args


# ---------- GUI ----------
BG, CARD, INK, MUTED, LINE, SOFT = "#FAF7F2", "#FFFFFF", "#2F2A35", "#9A93A2", "#EDE7DE", "#F3EEE7"
ACCENT = {  # pastel fill, deeper ink of the same hue
    "lavender": ("#E9E3FA", "#6B5CA5"), "sky": ("#DFECFA", "#3D6E9E"), "peach": ("#FCE6DA", "#B5643C"),
    "mint": ("#DDF2E6", "#3E8A62"), "rose": ("#F9E0E5", "#A8445E"), "butter": ("#FBF1D3", "#8A6D1E"),
}
FONT = "Segoe UI"


def gui():
    import tkinter as tk
    from tkinter import messagebox, simpledialog

    if not (GAME / "mods").exists():
        messagebox.showerror("Shader Mixer", "Run install.bat (or setup.py) first."); return
    game_fmt, feats = game_format(), iris_features()
    shaders = {f.name: {**inspect_shader(f), "name": f.stem} for f in sorted((GAME / "shaderpacks").glob("*.zip"), key=lambda p: p.name.lower())}
    packs = {f.name: inspect_pack(f) for f in sorted((GAME / "resourcepacks").glob("*.zip"), key=lambda p: p.name.lower())}
    mod_files = sorted(f.name.removesuffix(".disabled") for f in (GAME / "mods").iterdir() if f.name.endswith((".jar", ".jar.disabled")))
    mods = {f: inspect_mod(GAME / "mods" / (f if (GAME / "mods" / f).exists() else f + ".disabled")) for f in mod_files}
    try: installed = json.loads((GAME / "installed.json").read_text())
    except (OSError, ValueError): installed = {}
    presets = load_presets()

    root = tk.Tk(); root.title("Shader Mixer"); root.geometry("1200x700"); root.minsize(980, 560)
    root.configure(bg=BG)
    root.option_add("*Font", (FONT, 10))

    def button(parent, text, cmd, tone=None):
        fill, ink = ACCENT[tone] if tone else (SOFT, INK)
        b = tk.Button(parent, text=text, command=cmd, bg=fill, fg=ink, activebackground=LINE, activeforeground=INK,
                      relief="flat", bd=0, padx=14, pady=6, cursor="hand2", font=(FONT, 10, "bold" if tone else "normal"),
                      highlightthickness=1, highlightbackground=LINE)
        b.bind("<Enter>", lambda _: b.config(bg=LINE)); b.bind("<Leave>", lambda _: b.config(bg=fill))
        return b

    def card(col, title, tone, hint=""):
        outer = tk.Frame(cols, bg=LINE, padx=1, pady=1); outer.grid(row=0, column=col, sticky="nsew", padx=6)
        inner = tk.Frame(outer, bg=CARD, padx=12, pady=12); inner.pack(fill="both", expand=True)
        head = tk.Frame(inner, bg=CARD); head.pack(fill="x", pady=(0, 8))
        fill, ink = ACCENT[tone]
        tk.Label(head, text=title, bg=fill, fg=ink, font=(FONT, 9, "bold"), padx=8, pady=2).pack(side="left")
        if hint: tk.Label(head, text=hint, bg=CARD, fg=MUTED, font=(FONT, 9)).pack(side="left", padx=8)
        return inner

    def listbox(parent, tone, **kw):
        return tk.Listbox(parent, exportselection=False, bd=0, highlightthickness=0, activestyle="none", bg=CARD, fg=INK,
                          selectbackground=ACCENT[tone][0], selectforeground=INK, font=(FONT, 10), **kw)

    # header
    top = tk.Frame(root, bg=BG, padx=22, pady=16); top.pack(fill="x")
    tk.Label(top, text="Shader Mixer", bg=BG, fg=INK, font=(FONT, 20, "bold")).pack(side="left")
    tk.Label(top, text="Minecraft 26.3  ·  pick a look, or mix your own", bg=BG, fg=MUTED, font=(FONT, 10)).pack(side="left", padx=14, pady=(8, 0))
    dots = tk.Frame(top, bg=BG); dots.pack(side="right", pady=(8, 0))
    for tone in ("lavender", "sky", "peach", "mint"):
        tk.Label(dots, text="●", bg=BG, fg=ACCENT[tone][1], font=(FONT, 8)).pack(side="left", padx=2)

    cols = tk.Frame(root, bg=BG, padx=16); cols.pack(fill="both", expand=True)

    # 1. shader packs (presets)
    c1 = card(0, "SHADER PACKS", "lavender")
    pr_list = listbox(c1, "lavender", width=22, height=9); pr_list.pack(fill="x")
    about = tk.Label(c1, bg=CARD, fg=MUTED, wraplength=190, justify="left", anchor="nw", font=(FONT, 9))
    about.pack(fill="both", expand=True, pady=10)
    pb = tk.Frame(c1, bg=CARD); pb.pack(fill="x")

    def redraw_presets(sel=0):
        pr_list.delete(0, "end")
        for p in presets: pr_list.insert("end", ("  ✦  " if p.get("builtin") else "  ·  ") + p["name"])
        pr_list.selection_set(sel); pr_list.see(sel)

    # 2. shader, with a filter box (there are ~190)
    c2 = card(1, "SHADER", "sky", "pick one")
    query = tk.StringVar()
    box = tk.Frame(c2, bg=BG, highlightthickness=1, highlightbackground=LINE); box.pack(fill="x", pady=(0, 8))
    tk.Label(box, text="search", bg=BG, fg=MUTED, font=(FONT, 9), padx=8).pack(side="left")
    tk.Entry(box, textvariable=query, bd=0, bg=BG, fg=INK, insertbackground=INK, font=(FONT, 10)).pack(fill="x", ipady=5, padx=(0, 8))
    sh_list = listbox(c2, "sky", width=36); sh_list.pack(fill="both", expand=True)
    all_shaders = ["(none)"] + list(shaders); visible = list(all_shaders); picked = {"shader": "(none)"}

    def redraw_shaders(*_):
        q = query.get().strip().lower()
        visible[:] = [n for n in all_shaders if q in n.lower()] or ["(none)"]
        sh_list.delete(0, "end")
        for i, n in enumerate(visible):
            sh_list.insert("end", "  " + n.removesuffix(".zip"))
            if n != "(none)" and not shaders[n]["dh"]: sh_list.itemconfig(i, fg=MUTED)
        if picked["shader"] in visible:
            i = visible.index(picked["shader"]); sh_list.selection_set(i); sh_list.see(i)

    def pick_shader(_=None):
        i = sh_list.curselection()
        if i: picked["shader"] = visible[i[0]]; validate()

    query.trace_add("write", redraw_shaders)
    tk.Label(c2, text="grey = no Distant Horizons support", bg=CARD, fg=MUTED, font=(FONT, 8)).pack(anchor="w", pady=(6, 0))

    # 3. resource packs, ordered (top wins)
    c3 = card(2, "RESOURCE PACKS", "peach", "top wins · double-click to toggle")
    order = list(packs); chosen = {p: False for p in packs}
    pk_list = listbox(c3, "peach", width=40); pk_list.pack(fill="both", expand=True)

    def redraw(sel=None):
        pk_list.delete(0, "end")
        for i, p in enumerate(order):
            pk_list.insert("end", f"  {'●' if chosen[p] else '○'}  {p.removesuffix('.zip')}" + ("   pbr" if packs[p]["pbr"] else ""))
            pk_list.itemconfig(i, fg=INK if chosen[p] else MUTED)
        if sel is not None: pk_list.selection_set(sel); pk_list.see(sel)

    def toggle(_=None):
        i = pk_list.curselection()
        if i: p = order[i[0]]; chosen[p] = not chosen[p]; redraw(i[0]); validate()

    def move(d):
        i = pk_list.curselection()
        if not i or not 0 <= i[0] + d < len(order): return
        a = i[0]; order[a], order[a + d] = order[a + d], order[a]; redraw(a + d); validate()

    pk_list.bind("<Double-Button-1>", toggle); pk_list.bind("<space>", toggle)
    bf = tk.Frame(c3, bg=CARD); bf.pack(fill="x", pady=(8, 0))
    button(bf, "Toggle", toggle).pack(side="left")
    button(bf, "↑", lambda: move(-1)).pack(side="left", padx=(6, 0))
    button(bf, "↓", lambda: move(1)).pack(side="left", padx=(6, 0))

    # 4. mods: click a row to switch it
    c4 = card(3, "MODS", "mint")
    mod_on, mod_rows = {f: (GAME / "mods" / f).exists() or mods[f]["id"] in CORE_MODS for f in mod_files}, {}

    def paint_mod(f):
        core = mods[f]["id"] in CORE_MODS
        mod_rows[f].config(text=("●  " if mod_on[f] else "○  ") + mods[f]["id"] + ("   required" if core else ""),
                           fg=MUTED if core else (ACCENT["mint"][1] if mod_on[f] else MUTED))

    def flip_mod(f):
        if mods[f]["id"] in CORE_MODS: return
        mod_on[f] = not mod_on[f]; paint_mod(f); validate()

    for f in mod_files:
        core = mods[f]["id"] in CORE_MODS
        mod_rows[f] = tk.Label(c4, bg=CARD, anchor="w", font=(FONT, 10), cursor="" if core else "hand2")
        mod_rows[f].pack(fill="x", pady=2); mod_rows[f].bind("<Button-1>", lambda _, f=f: flip_mod(f)); paint_mod(f)

    cols.columnconfigure((1, 2), weight=1); cols.rowconfigure(0, weight=1)

    # status pill + actions
    foot = tk.Frame(root, bg=BG, padx=22, pady=14); foot.pack(fill="x")
    status = tk.Label(foot, bg=BG, anchor="w", justify="left", font=(FONT, 10), padx=12, pady=8, wraplength=640)
    status.pack(side="left", fill="x", expand=True)
    msg = {"text": ""}

    def selection():
        s = picked["shader"]
        en = {f: mods[f] for f in mod_files if mod_on[f]}
        return s, (None if s == "(none)" else shaders[s]), [p for p in order if chosen[p]], en

    def say(lines, tone):
        msg["text"] = "\n".join(lines)
        status.config(text=msg["text"], bg=ACCENT[tone][0], fg=ACCENT[tone][1])

    def validate(_=None, extra=()):
        s, sh, sel_packs, en = selection()
        err, warn = check(sh, {p: packs[p] for p in sel_packs}, en, game_fmt, feats)
        err = list(extra) + err
        if err: say(["✕  " + e for e in err], "rose")
        elif warn: say(["!  " + w for w in warn], "butter")
        else: say(["✓  This mix will load."], "mint")
        return err

    def go(do_launch):
        if validate():
            messagebox.showerror("Not this mix", "This combination won't load correctly:\n\n" + msg["text"]); return
        s, _, sel_packs, en = selection()
        apply(None if s == "(none)" else s, sel_packs, set(en), mod_files)
        if do_launch:
            launch()
            messagebox.showinfo("Shader Mixer", "The Minecraft Launcher is opening.\nPick the 'Shader Mixer' profile and press Play.")
        else:
            messagebox.showinfo("Shader Mixer", "Saved. It applies the next time you press Play on 'Shader Mixer'.")

    def current_preset():
        i = pr_list.curselection()
        return presets[i[0]] if i else None

    def use_preset(_=None):
        pr = current_preset()
        if not pr: return
        about.config(text=pr.get("about", "One of your mixes."))
        shader, sel, missing = resolve_preset(pr, installed, shaders, packs)
        picked["shader"] = shader or "(none)"; query.set("")
        for p in packs: chosen[p] = p in sel
        order.sort(key=lambda p: sel.index(p) if p in sel else len(sel))
        off = set(pr.get("mods_off", []))
        for f in mod_files:
            mod_on[f] = mods[f]["id"] in CORE_MODS or mods[f]["id"] not in off; paint_mod(f)
        redraw(); redraw_shaders()
        validate(extra=[f"Not installed (run install.bat again): {', '.join(missing)}"] if missing else ())

    def save_preset():
        if validate():
            messagebox.showerror("Not this mix", "Only working combinations can be saved:\n\n" + msg["text"]); return
        name = simpledialog.askstring("Save mix", "Name this mix:", parent=root)
        if not name or not name.strip(): return
        name = name.strip()
        old = next((i for i, p in enumerate(presets) if p["name"].lower() == name.lower()), None)
        if old is not None and presets[old].get("builtin"):
            messagebox.showerror("Save mix", f"'{name}' is a built-in pack. Pick another name."); return
        if old is not None and not messagebox.askyesno("Save mix", f"Replace your mix '{name}'?"): return
        s, _, sel_packs, en = selection()
        new = {"name": name, "shader": None if s == "(none)" else s, "packs": sel_packs,
               "mods_off": sorted(mods[f]["id"] for f in mod_files if f not in en)}
        if old is None: presets.append(new)
        else: presets[old] = new
        save_user_presets(presets); redraw_presets(presets.index(new)); about.config(text="One of your mixes.")

    def delete_preset():
        pr = current_preset()
        if not pr or pr.get("builtin"):
            messagebox.showinfo("Delete", "Built-in packs can't be deleted."); return
        if messagebox.askyesno("Delete", f"Delete '{pr['name']}'?"):
            presets.remove(pr); save_user_presets(presets); redraw_presets(); use_preset()

    button(pb, "Save mix…", save_preset, "lavender").pack(side="left")
    button(pb, "Delete", delete_preset).pack(side="left", padx=(6, 0))
    button(foot, "Save & Launch", lambda: go(True), "lavender").pack(side="right")
    button(foot, "Save only", lambda: go(False)).pack(side="right", padx=8)

    pr_list.bind("<<ListboxSelect>>", use_preset)
    sh_list.bind("<<ListboxSelect>>", pick_shader)
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
