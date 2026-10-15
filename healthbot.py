#!/usr/bin/env python3
"""healthbot: a live server-health dashboard in a single Discord message.

Every run (once a minute from a systemd timer) it checks the machine, edits one
webhook message in place with stat tiles plus a 24-hour graph image, and posts
a separate alert message whenever a check changes state.
"""
import argparse, io, json, os, re, socket, subprocess, sys, time, tomllib
import urllib.error, urllib.request
from datetime import datetime, timedelta

__version__ = "1.0.0"
UA = f"healthbot/{__version__}"

DEFAULTS = {
    "webhook_url": "",
    "healthchecks_url": "",
    "name": "",
    "state_dir": "/var/lib/healthbot",
    "checks": {
        "services": [],
        "mounts": ["/"],
        "network_interface": "auto",
        "drives": "auto",
        "failed_units": True,
        "docker": True,
    },
    "thresholds": {
        "disk_warn": 85, "disk_crit": 95,
        "wear_warn": 80, "wear_crit": 90,
        "drive_temp_warn": 70,
        "cpu_temp_warn": 85, "cpu_temp_crit": 95,
        "mem_avail_warn": 10, "mem_avail_crit": 5,
        "load_warn_per_core": 1.5,
    },
    "alerts": {"mention": "@here"},
    "pterodactyl": {
        "enabled": False,
        "mysql_command": ["mysql", "panel"],
        "timezone": "auto",
        "env_file": "/var/www/pterodactyl/.env",
    },
}

OK, WARN, CRIT = 0, 1, 2
ICON = {OK: "🟢", WARN: "🟡", CRIT: "🔴"}
TILE_ICON = {OK: "", WARN: "⚠️ ", CRIT: "🔴 "}
LINE_ICON = {OK: "✅", WARN: "⚠️", CRIT: "🔴"}
COLOR = {OK: 0x0CA30C, WARN: 0xFAB219, CRIT: 0xD03B3B}
HISTORY_SECS = 24 * 3600


class Run:
    """Everything one run collects: check results, numeric metrics, config."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.th = cfg["thresholds"]
        self.results = []  # (key, level, label, detail)
        self.m = {}        # numeric readings for tiles and graphs
        self.drives = []   # dicts from check_drives
        self.raid = []     # (name, level, detail)

    def add(self, key, lvl, label, detail):
        self.results.append((key, lvl, label, detail))

    def level(self, prefix):
        return max([r[1] for r in self.results if r[0].startswith(prefix)] + [OK])

    def worst(self):
        return max([r[1] for r in self.results] + [OK])


# ---------------------------------------------------------------- helpers

def sh(*cmd, timeout=30):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def merge(base, over):
    out = dict(base)
    for k, v in over.items():
        out[k] = merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def load_config(path):
    cfg = DEFAULTS
    if path and os.path.exists(path):
        with open(path, "rb") as f:
            cfg = merge(DEFAULTS, tomllib.load(f))
    cfg = dict(cfg)
    cfg["webhook_url"] = os.environ.get("HEALTHBOT_WEBHOOK_URL", cfg["webhook_url"]).strip()
    cfg["name"] = cfg["name"] or socket.gethostname()
    return cfg


def fmt_bytes(b):
    return f"{b / 2**30:.0f} GiB" if b >= 2**30 else f"{b / 2**20:.0f} MiB"


def by_threshold(value, warn, crit):
    return CRIT if value >= crit else WARN if value >= warn else OK


# ---------------------------------------------------------------- checks

def check_raid(run):
    try:
        lines = open("/proc/mdstat").read().splitlines()
    except FileNotFoundError:
        return
    for i, line in enumerate(lines):
        if not line.startswith("md"):
            continue
        name = line.split()[0]
        status = lines[i + 1] if i + 1 < len(lines) else ""
        nxt = lines[i + 2] if i + 2 < len(lines) else ""
        bitmap = status[status.rfind("["):].strip() if "[" in status else "?"
        lvl, detail = (CRIT, f"DEGRADED {bitmap}") if "_" in bitmap else (OK, bitmap)
        if any(k in nxt for k in ("recovery", "resync", "reshape", "check")):
            lvl = max(lvl, WARN)
            detail += " · " + " ".join(nxt.split()[:4])
        if "inactive" in line:
            lvl, detail = CRIT, "INACTIVE"
        run.raid.append((name, lvl, detail))
        run.add(f"raid:{name}", lvl, name, detail)


def detect_drives():
    try:
        devices = json.loads(sh("smartctl", "--scan", "-j")).get("devices", [])
    except json.JSONDecodeError:
        return []
    return [d["name"] for d in devices if d.get("name")]


def check_drives(run):
    th, want = run.th, run.cfg["checks"]["drives"]
    names = detect_drives() if want == "auto" else want
    for dev in names:
        short = os.path.basename(dev)
        try:
            # -n standby: don't spin up sleeping hard drives just to read them.
            s = json.loads(sh("smartctl", "-a", "-j", "-n", "standby", dev))
        except json.JSONDecodeError:
            s = {}
        if any("STANDBY" in msg.get("string", "") for msg in s.get("smartctl", {}).get("messages", [])):
            run.drives.append({"name": short, "temp": None, "wear": None, "level": OK})
            run.add(f"drive:{short}", OK, short, "asleep (standby)")
            continue
        if "smart_status" not in s and "temperature" not in s:
            hint = "" if os.geteuid() == 0 else " (needs root)"
            run.add(f"drive:{short}", WARN, short, "SMART data unavailable" + hint)
            continue
        lvl, problems = OK, []
        if s.get("smart_status", {}).get("passed") is False:
            lvl = CRIT; problems.append("SMART self-assessment FAILED")
        temp = s.get("temperature", {}).get("current")
        wear = None
        nv = s.get("nvme_smart_health_information_log")
        if nv:
            wear = nv.get("percentage_used")
            if nv.get("critical_warning"):
                lvl = CRIT; problems.append(f"critical_warning={nv['critical_warning']}")
            if nv.get("media_errors"):
                lvl = CRIT; problems.append(f"media_errors={nv['media_errors']}")
            if nv.get("available_spare", 100) <= nv.get("available_spare_threshold", 0):
                lvl = CRIT; problems.append("spare exhausted")
        for attr in s.get("ata_smart_attributes", {}).get("table", []):
            if attr.get("id") in (5, 197, 198) and attr.get("raw", {}).get("value", 0) > 0:
                lvl = max(lvl, WARN); problems.append(f"{attr['name']}={attr['raw']['value']}")
        if wear is not None:
            lvl = max(lvl, by_threshold(wear, th["wear_warn"], th["wear_crit"]))
        if temp is not None and temp >= th["drive_temp_warn"]:
            lvl = max(lvl, WARN); problems.append("hot")
        parts = ([f"wear {wear}%"] if wear is not None else []) + ([f"{temp}°C"] if temp is not None else [])
        detail = " · ".join(parts + problems) or "ok"
        run.drives.append({"name": short, "temp": temp, "wear": wear, "level": lvl})
        if temp is not None:
            run.m[f"temp:{short}"] = temp
        run.add(f"drive:{short}", lvl, short, detail)


def check_mounts(run):
    th = run.th
    for mnt in run.cfg["checks"]["mounts"]:
        try:
            st = os.statvfs(mnt)
        except OSError:
            run.add(f"disk:{mnt}", WARN, mnt, "not mounted")
            continue
        pct = round(100 * (1 - st.f_bavail / st.f_blocks)) if st.f_blocks else 0
        run.m[f"disk:{mnt}"], run.m[f"free:{mnt}"] = pct, fmt_bytes(st.f_bavail * st.f_frsize)
        run.add(f"disk:{mnt}", by_threshold(pct, th["disk_warn"], th["disk_crit"]), mnt,
                f"{pct}% used · {run.m[f'free:{mnt}']} free")


def cpu_temp():
    """Best-effort CPU package temperature for AMD, Intel and ARM boards."""
    try:
        chips = json.loads(sh("sensors", "-j") or "{}")
    except json.JSONDecodeError:
        chips = {}
    prefer = [("k10temp", ("Tctl", "Tdie")), ("zenpower", ("Tdie", "Tctl")),
              ("coretemp", ("Package id 0",)), ("cpu_thermal", ("temp1",))]
    for prefix, feats in prefer:
        for chip, data in chips.items():
            if not chip.startswith(prefix):
                continue
            for feat in feats:
                for k, v in data.get(feat, {}).items():
                    if k.endswith("_input"):
                        return float(v)
    for zone in sorted(os.listdir("/sys/class/thermal")) if os.path.isdir("/sys/class/thermal") else []:
        try:
            kind = open(f"/sys/class/thermal/{zone}/type").read().strip()
            if kind in ("x86_pkg_temp", "cpu-thermal", "soc_thermal"):
                return int(open(f"/sys/class/thermal/{zone}/temp").read()) / 1000
        except OSError:
            continue
    return None


def check_system(run):
    th = run.th
    mem = {}
    for line in open("/proc/meminfo"):
        k, v = line.split(":")
        mem[k] = int(v.split()[0])
    avail_pct = 100 * mem["MemAvailable"] / mem["MemTotal"]
    used_g = (mem["MemTotal"] - mem["MemAvailable"]) / 2**20
    run.m.update(mem_pct=100 - avail_pct, mem_used_g=used_g, mem_total_g=mem["MemTotal"] / 2**20)
    mlvl = CRIT if avail_pct < th["mem_avail_crit"] else WARN if avail_pct < th["mem_avail_warn"] else OK
    run.add("sys:mem", mlvl, "Memory", f"{used_g:.1f}/{mem['MemTotal'] / 2**20:.0f} GiB")

    l1, l5, _ = os.getloadavg()
    cores = os.cpu_count() or 1
    run.m.update(load1=l1, load_pct=100 * l1 / cores)
    run.add("sys:load", WARN if l5 > cores * th["load_warn_per_core"] else OK, "Load",
            f"{l5:.2f} over 5 min on {cores} cores")

    t = cpu_temp()
    if t is not None:
        run.m["cpu_temp"] = t
        run.add("sys:cpu_temp", by_threshold(t, th["cpu_temp_warn"], th["cpu_temp_crit"]),
                "CPU temp", f"{t:.0f}°C")


def check_services(run):
    for svc in run.cfg["checks"]["services"]:
        state = sh("systemctl", "is-active", svc).strip() or "unknown"
        run.add(f"svc:{svc}", OK if state == "active" else CRIT, svc, state)
    if run.cfg["checks"]["failed_units"]:
        failed = [l.split()[0] for l in sh("systemctl", "--failed", "--no-legend", "--plain").splitlines()
                  if l.strip()]
        if failed:
            run.add("svc:failed", WARN, "failed units", ", ".join(failed)[:200])


def check_docker(run):
    if not run.cfg["checks"]["docker"]:
        return
    out = sh("docker", "ps", "-a", "--format", "{{.State}}")
    states = out.split()
    if out or states:
        run.m["containers_running"], run.m["containers_total"] = states.count("running"), len(states)


def ptero_timezone(cfg):
    from zoneinfo import ZoneInfo
    tz = cfg["timezone"]
    if tz == "auto":
        try:
            env = open(cfg["env_file"]).read()
            match = re.search(r"^APP_TIMEZONE=\"?([^\"\n]+)", env, re.M)
            tz = match.group(1).strip() if match else "UTC"
        except OSError:
            tz = "UTC"
    return ZoneInfo(tz)


def check_pterodactyl(run):
    cfg = run.cfg["pterodactyl"]
    if not cfg["enabled"]:
        return
    # Schedules with no tasks are never run by Pterodactyl, so they are skipped.
    q = """SELECT s.id, sv.name, s.next_run_at, s.last_run_at, s.is_processing
           FROM schedules s JOIN servers sv ON sv.id = s.server_id
           WHERE s.is_active = 1 AND EXISTS (SELECT 1 FROM tasks t WHERE t.schedule_id = s.id)"""
    try:
        r = subprocess.run([*cfg["mysql_command"], "-N", "-B", "-e", q],
                           capture_output=True, text=True, timeout=30)
        if r.returncode:
            raise RuntimeError(r.stderr.strip())
        tz = ptero_timezone(cfg)
    except Exception as e:
        run.add("ptero:schedules", WARN, "Schedules", f"query failed: {str(e)[:120]}")
        return
    now = datetime.now(tz).replace(tzinfo=None)
    parse = lambda v: None if v in ("NULL", "") else datetime.fromisoformat(v)
    rows = [l.split("\t") for l in r.stdout.splitlines() if l]
    bad = []
    for sid, server, next_run, last_run, processing in rows:
        nxt, last = parse(next_run), parse(last_run)
        if processing == "1" and last and now - last > timedelta(minutes=30):
            bad.append(f"#{sid} {server} (stuck)")
        elif nxt and now - nxt > timedelta(minutes=15):
            bad.append(f"#{sid} {server} (overdue)")
    if bad:
        run.add("ptero:schedules", CRIT, "Schedules", "; ".join(bad)[:300])
    else:
        run.add("ptero:schedules", OK, "Schedules", f"{len(rows)} active, all on time")


def default_interface():
    try:
        for line in open("/proc/net/route").read().splitlines()[1:]:
            f = line.split()
            if f[1] == "00000000":
                return f[0]
    except OSError:
        pass
    return None


def net_rates(run, state):
    """Average Mbit/s in/out since the previous run, from interface counters."""
    iface = run.cfg["checks"]["network_interface"]
    iface = default_interface() if iface == "auto" else iface
    for line in open("/proc/net/dev"):
        if iface and line.strip().startswith(iface + ":"):
            f = line.split(":", 1)[1].split()
            rx, tx = int(f[0]), int(f[8])
            break
    else:
        return
    now, prev = time.time(), state.get("net")
    state["net"] = [now, rx, tx, iface]
    if prev and len(prev) >= 3 and now > prev[0] and rx >= prev[1] and tx >= prev[2]:
        dt = now - prev[0]
        run.m["net_in"] = (rx - prev[1]) * 8 / dt / 1e6
        run.m["net_out"] = (tx - prev[2]) * 8 / dt / 1e6


def collect(cfg, state):
    run = Run(cfg)
    for check in (check_raid, check_drives, check_mounts, check_system,
                  check_services, check_docker, check_pterodactyl):
        try:
            check(run)
        except Exception as e:
            run.add(f"error:{check.__name__}", WARN, check.__name__, f"crashed: {e}"[:200])
    net_rates(run, state)
    return run


# ---------------------------------------------------------------- embed

def build_embed(run, now, uptime_s):
    """Stat tiles (three per row) for the headline numbers, then one line per
    health area that stays short when healthy and lists specifics when not."""
    m = run.m
    tile = lambda name, lvl, big, sub: {"name": TILE_ICON[lvl] + name,
                                        "value": f"**{big}**\n-# {sub}", "inline": True}
    days, rem = divmod(int(uptime_s), 86400)
    cpu_sub = f"load {m['load1']:.2f}" + (f" · {m['cpu_temp']:.0f}°C" if "cpu_temp" in m else "")
    tiles = [
        tile("CPU", max(run.level("sys:load"), run.level("sys:cpu_temp")), f"{m['load_pct']:.0f}%", cpu_sub),
        tile("Memory", run.level("sys:mem"),
             f"{m['mem_used_g']:.1f} / {m['mem_total_g']:.0f} GiB", f"{m['mem_pct']:.0f}% used"),
        tile("Uptime", OK, f"{days}d {rem // 3600}h", f"since <t:{int(now - uptime_s)}:d>"),
    ]

    if run.raid:
        lvl = run.level("raid:")
        tiles.append(tile("RAID", lvl, " ".join(f"{n} {'✓' if l == OK else '✗'}" for n, l, _ in sorted(run.raid)),
                          "mirrored, in sync" if lvl == OK
                          else "; ".join(f"{n} {d}" for n, l, d in run.raid if l != OK)))
    else:
        lvl = run.level("drive:")
        healthy = sum(1 for d in run.drives if d["level"] == OK)
        tiles.append(tile("Drives", lvl, f"{healthy} / {len(run.drives)} healthy" if run.drives else "—",
                          "SMART" if run.drives else "smartctl found no drives"))

    mounts = [mt for mt in run.cfg["checks"]["mounts"] if f"disk:{mt}" in m]
    if mounts:
        first, rest = mounts[0], mounts[1:]
        sub = f"{m[f'free:{first}']} free" + "".join(f" · {mt} {m[f'disk:{mt}']}%" for mt in rest[:2])
        tiles.append(tile(f"Disk {first}", run.level("disk:"), f"{m[f'disk:{first}']}%", sub))

    if "containers_total" in m:
        tiles.append(tile("Containers", OK, f"{m['containers_running']} running",
                          f"of {m['containers_total']} total"))
    elif "net_in" in m:
        tiles.append(tile("Network", OK, f"↓{m['net_in']:.1f} ↑{m['net_out']:.1f}", "Mbit/s"))

    def line(name, prefix, ok_text):
        bad = [(lbl, d) for k, lv, lbl, d in run.results if k.startswith(prefix) and lv != OK]
        text = ok_text if not bad else "; ".join(f"{lbl}: {d}" for lbl, d in bad)
        return f"{LINE_ICON[run.level(prefix)]} **{name}** — {text}"

    lines = []
    n_svc = len(run.cfg["checks"]["services"])
    if n_svc or run.level("svc:"):
        lines.append(line("Services", "svc:", f"all {n_svc} running"))
    if run.drives:
        wear = [f"{d['wear']}%" for d in run.drives if d["wear"] is not None]
        lines.append(line("Drives", "drive:", "healthy" + (f" · wear {' / '.join(wear)}" if wear else "")))
    for key, lvl, label, detail in run.results:
        if key.startswith(("ptero:", "error:")):
            lines.append(f"{LINE_ICON[lvl]} **{label}** — {detail}")

    worst = run.worst()
    n_bad = sum(1 for r in run.results if r[1] != OK)
    headline = {OK: "All systems healthy", WARN: f"{n_bad} warning{'s' * (n_bad > 1)}",
                CRIT: f"{n_bad} problem{'s' * (n_bad > 1)} detected"}[worst]
    fields = tiles + ([{"name": "​", "value": "\n".join(lines)[:1024], "inline": False}] if lines else [])
    return {
        "author": {"name": f"{run.cfg['name']} · server health"},
        "title": f"{ICON[worst]}  {headline}",
        "color": COLOR[worst],
        "description": f"Updated <t:{now}:R>",
        "fields": fields,
        "footer": {"text": "Refreshes every minute · if 'Updated' is stale, the server or bot is down"},
    }


# ---------------------------------------------------------------- graphics

# Dark chart surface and ink, plus three series hues validated for
# colour-vision deficiency on that surface.
SURFACE, INK, INK2, MUTED = "#1a1a19", "#f0efec", "#c3c2b7", "#898781"
GRID, BASELINE = "#2c2c2a", "#383835"
SERIES = ["#3987e5", "#d95926", "#199e70"]
STATUS_HEX = {OK: "#0ca30c", WARN: "#fab219", CRIT: "#d03b3b"}


def chart_sample(run):
    keys = ["load_pct", "mem_pct", "cpu_temp", "net_in", "net_out"]
    sample = {k: round(run.m[k], 2) for k in keys if k in run.m}
    sample.update({k: v for k, v in run.m.items() if k.startswith("temp:")})
    return sample


def series(history, key, bucket=300):
    """5-minute averages of one metric, with breaks across gaps (downtime)."""
    buckets = {}
    for row in history:
        if key in row:
            buckets.setdefault(row["t"] // bucket, []).append(row[key])
    ts, vs, last = [], [], None
    for b in sorted(buckets):
        if last is not None and b - last > 1:
            ts.append(datetime.fromtimestamp((last + 1) * bucket)); vs.append(float("nan"))
        ts.append(datetime.fromtimestamp(b * bucket + bucket / 2))
        vs.append(sum(buckets[b]) / len(buckets[b]))
        last = b
    return ts, vs


def temp_series(run, history):
    """CPU plus up to two drives as separate lines; more drives collapse to the hottest."""
    drive_keys = sorted({k for row in history for k in row if k.startswith("temp:")})
    out = [("cpu_temp", "CPU")] if any("cpu_temp" in r for r in history) else []
    if len(drive_keys) <= 2:
        return out + [(k, k[5:]) for k in drive_keys], history
    hist = [dict(r, temp_max=max(r[k] for k in drive_keys if k in r)) if any(k in r for k in drive_keys) else r
            for r in history]
    return out + [("temp_max", "hottest drive")], hist


def render_dashboard(run, history):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from matplotlib.ticker import FuncFormatter, MaxNLocator

    m, th = run.m, run.th
    plt.rcParams.update({
        "font.family": "DejaVu Sans", "font.size": 15,
        "text.color": INK, "axes.labelcolor": INK2,
        "xtick.color": MUTED, "ytick.color": MUTED,
        "xtick.labelsize": 13, "ytick.labelsize": 13,
    })
    fig = plt.figure(figsize=(10, 7.6), dpi=150, facecolor=SURFACE)
    gs = fig.add_gridspec(3, 2, height_ratios=[1, 1, 0.62], hspace=0.62, wspace=0.16,
                          left=0.06, right=0.94, top=0.855, bottom=0.04)
    now = datetime.now()
    t0 = now - timedelta(seconds=HISTORY_SECS)
    fig.text(0.06, 0.96, "Last 24 hours", fontsize=21, weight="bold", color=INK, va="center")
    fig.text(0.94, 0.96, f"{run.cfg['name']} · {now:%b %d %H:%M}", fontsize=14,
             color=MUTED, ha="right", va="center")

    def frame(ax, title, value):
        ax.set_facecolor(SURFACE)
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color(BASELINE)
        ax.grid(axis="y", color=GRID, linewidth=1)
        ax.set_axisbelow(True)
        ax.tick_params(length=0, pad=6)
        ax.set_xlim(t0, now + timedelta(minutes=25))  # room for the end marker
        ax.xaxis.set_major_locator(mdates.HourLocator(byhour=range(0, 24, 6)))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        ax.yaxis.set_major_locator(MaxNLocator(3, integer=True))
        ax.set_title(title, loc="left", fontsize=16, color=INK2, pad=12)
        ax.text(1.0, 1.04, value, transform=ax.transAxes, ha="right", va="bottom",
                fontsize=20, weight="bold", color=INK)

    def line(ax, hist, key, color, fill=True, label=None):
        ts, vs = series(hist, key)
        if not ts:
            return []
        ax.plot(ts, vs, color=color, linewidth=2, solid_joinstyle="round",
                solid_capstyle="round", label=label)
        if fill:
            ax.fill_between(ts, vs, 0, color=color, alpha=0.10, linewidth=0)
        ax.plot([ts[-1]], [vs[-1]], "o", ms=8, color=color, mec=SURFACE, mew=2)
        return [v for v in vs if v == v]

    def legend(ax):
        if not ax.get_legend_handles_labels()[0]:
            return
        leg = ax.legend(loc="upper left", bbox_to_anchor=(0, 1.0), ncol=3, frameon=False,
                        fontsize=12, handlelength=1.2, columnspacing=1.0, borderaxespad=0.2)
        for t in leg.get_texts():
            t.set_color(INK2)

    pct = FuncFormatter(lambda v, _: f"{v:.0f}%")

    ax = fig.add_subplot(gs[0, 0])
    frame(ax, "CPU load", f"{m['load_pct']:.0f}%")
    ax.set_ylim(0, max([100] + [v * 1.1 for v in line(ax, history, "load_pct", SERIES[0])]))
    ax.yaxis.set_major_formatter(pct)

    ax = fig.add_subplot(gs[0, 1])
    frame(ax, "Memory used", f"{m['mem_pct']:.0f}%")
    line(ax, history, "mem_pct", SERIES[0])
    ax.set_ylim(0, 100)
    ax.yaxis.set_major_formatter(pct)

    ax = fig.add_subplot(gs[1, 0])
    frame(ax, "Temperatures", f"CPU {m['cpu_temp']:.0f}°C" if "cpu_temp" in m else "")
    keys, hist = temp_series(run, history)
    vals = []
    for (key, name), color in zip(keys, SERIES):
        vals += line(ax, hist, key, color, fill=False, label=name)
    hi = max(vals + [60])
    ax.set_ylim(min(vals + [20]) - 5, hi + (hi - 20) * 0.45)  # headroom for the legend
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.0f}°"))
    legend(ax)

    ax = fig.add_subplot(gs[1, 1])
    rate = lambda v: f"{v:.1f}" if v < 10 else f"{v:.0f}"
    frame(ax, "Network", f"↓{rate(m['net_in'])} ↑{rate(m['net_out'])} Mb/s" if "net_in" in m else "")
    vals = []
    for (key, name), color in zip((("net_in", "in"), ("net_out", "out")), SERIES):
        vals += line(ax, history, key, color, fill=False, label=name)
    ax.set_ylim(0, max(vals + [1]) * 1.4)
    ax.yaxis.set_major_formatter(FuncFormatter(
        lambda v, _: "0" if v == 0 else f"{v / 1000:.1f}G" if v >= 1000 else f"{v:.0f}M"))
    legend(ax)

    def meters(ax, title, rows, empty):
        ax.set_facecolor(SURFACE)
        ax.axis("off")
        ax.set_xlim(0, 100)
        ax.set_ylim(max(len(rows), 2) - 0.4, -0.9)
        ax.text(0, -0.85, title, fontsize=16, color=INK2, va="bottom")
        if not rows:
            ax.text(0, 0.3, empty, fontsize=13, color=MUTED, va="center")
        for i, (name, value, lvl) in enumerate(rows):
            c = STATUS_HEX[lvl]
            ax.barh(i + 0.12, 100, height=0.26, color=c, alpha=0.18, linewidth=0)
            ax.barh(i + 0.12, max(value, 1), height=0.26, color=c, linewidth=0)
            ax.text(0, i - 0.12, name, fontsize=14, color=INK, va="bottom")
            ax.text(100, i - 0.12, f"{value:.0f}%", fontsize=14, color=INK, weight="bold",
                    va="bottom", ha="right")

    disk_rows = [(mt, m[f"disk:{mt}"], by_threshold(m[f"disk:{mt}"], th["disk_warn"], th["disk_crit"]))
                 for mt in run.cfg["checks"]["mounts"] if f"disk:{mt}" in m][:3]
    wear_rows = [(d["name"], d["wear"], by_threshold(d["wear"], th["wear_warn"], th["wear_crit"]))
                 for d in run.drives if d["wear"] is not None][:3]
    meters(fig.add_subplot(gs[2, 0]), "Disk usage", disk_rows, "no mounts configured")
    meters(fig.add_subplot(gs[2, 1]), "SSD wear (rated life used)", wear_rows,
           "no wear data (NVMe drives only)")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=SURFACE)
    plt.close(fig)
    return buf.getvalue()


def demo_history(run, hours=24):
    """Synthetic 24h history so --demo shows what full graphs look like."""
    import math, random
    rnd, now, out = random.Random(1), int(time.time()), []
    drives = [k for k in run.m if k.startswith("temp:")]
    for i in range(hours * 60):
        d = math.sin(i / (hours * 60) * 2 * math.pi)
        row = {"t": now - (hours * 60 - i) * 60,
               "load_pct": 14 + 8 * d + rnd.random() * 6, "mem_pct": 30 + 3 * d + rnd.random(),
               "cpu_temp": 52 + 6 * d + rnd.random() * 3,
               "net_in": 6 + 4 * d + rnd.random() * 3, "net_out": 20 + 10 * d + rnd.random() * 8}
        row.update({k: 33 + j + 2 * d for j, k in enumerate(drives)})
        out.append(row)
    return out


# ---------------------------------------------------------------- discord

def discord(method, url, payload, png=None):
    if png is None:
        data, ctype = json.dumps(payload).encode(), "application/json"
    else:
        # Multipart: payload_json plus one PNG referenced as attachment://health.png.
        # Listing only this attachment on an edit drops the previous image.
        payload = dict(payload, attachments=[{"id": 0, "filename": "health.png"}])
        boundary = "healthbot" + os.urandom(8).hex()
        data = b"".join([
            f'--{boundary}\r\nContent-Disposition: form-data; name="payload_json"\r\n'
            f"Content-Type: application/json\r\n\r\n".encode(), json.dumps(payload).encode(),
            f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="files[0]"; '
            f'filename="health.png"\r\nContent-Type: image/png\r\n\r\n'.encode(), png,
            f"\r\n--{boundary}--\r\n".encode(),
        ])
        ctype = f"multipart/form-data; boundary={boundary}"
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": ctype, "User-Agent": UA})
    for _ in range(3):
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                body = r.read()
                return r.status, (json.loads(body) if body else None)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(float(json.loads(e.read() or b"{}").get("retry_after", 2)))
                continue
            return e.code, None
        except urllib.error.URLError as e:
            print(f"discord: {e}", file=sys.stderr)
            return None, None
    return 429, None


def alert_messages(run, prev_levels):
    """New-message alerts for any check whose level changed since the last run."""
    worse, better = [], []
    for key, lvl, label, detail in run.results:
        old = prev_levels.get(key, OK)
        if lvl > old:
            worse.append(f"{ICON[lvl]} **{label}** — {detail}")
        elif lvl < old:
            better.append(f"{ICON[lvl]} **{label}** recovered — {detail}")
    current = {r[0] for r in run.results}
    better += [f"🟢 **{key.split(':', 1)[1]}** recovered" for key, lvl in prev_levels.items()
               if lvl != OK and key not in current]
    mention = run.cfg["alerts"]["mention"]
    if worse:
        return {"content": (f"{mention} " if mention else "") + "**Health alert**\n" + "\n".join(worse + better),
                "allowed_mentions": {"parse": ["everyone", "roles", "users"]}}
    if better:
        return {"content": "**Recovered**\n" + "\n".join(better), "allowed_mentions": {"parse": []}}
    return None


# ---------------------------------------------------------------- main

def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description="Live server-health dashboard in a Discord message.")
    ap.add_argument("-c", "--config", default="/etc/healthbot/config.toml")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the message and write health.png here; post and save nothing")
    ap.add_argument("--demo", action="store_true", help="with --dry-run: fill the graphs with sample data")
    ap.add_argument("--out", default="health.png", help="with --dry-run: where to write the image")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args()

    cfg = load_config(args.config)
    if not args.dry_run and not cfg["webhook_url"]:
        sys.exit(f"no webhook_url in {args.config} (or HEALTHBOT_WEBHOOK_URL)")
    state_file = os.path.join(cfg["state_dir"], "state.json")
    history_file = os.path.join(cfg["state_dir"], "history.json")

    def load(path, default):
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return default

    state = load(state_file, {})
    run = collect(cfg, state)
    now = int(time.time())
    uptime_s = float(open("/proc/uptime").read().split()[0])

    history = [r for r in load(history_file, []) if r["t"] > now - HISTORY_SECS]
    history.append({"t": now, **chart_sample(run)})
    if args.demo:
        history = demo_history(run)

    try:
        png = render_dashboard(run, history)
    except Exception as e:
        print(f"render failed: {e}", file=sys.stderr)
        png = None
    embed = build_embed(run, now, uptime_s)
    if png:
        embed["image"] = {"url": "attachment://health.png"}

    if args.dry_run:
        print(json.dumps({"embeds": [embed]}, indent=2, ensure_ascii=False))
        alert = alert_messages(run, state.get("levels", {}))
        if alert:
            print("\nalert that would be posted:\n" + alert["content"], file=sys.stderr)
        if png:
            try:
                with open(args.out, "wb") as f:
                    f.write(png)
                print(f"\nwrote {args.out}", file=sys.stderr)
            except OSError as e:
                print(f"\ncould not write image: {e} (try --out /tmp/health.png)", file=sys.stderr)
        return

    os.makedirs(cfg["state_dir"], exist_ok=True)
    write_json(history_file, history)

    # The dashboard: edit in place, re-post if the message was deleted.
    webhook = cfg["webhook_url"]
    msg_id = state.get("msg_id")
    status = None
    if msg_id:
        status, _ = discord("PATCH", f"{webhook}/messages/{msg_id}", {"embeds": [embed]}, png)
    if not msg_id or status == 404:
        status, msg = discord("POST", f"{webhook}?wait=true", {"embeds": [embed]}, png)
        if msg:
            state["msg_id"] = msg["id"]

    alert = alert_messages(run, state.get("levels", {}))
    if alert:
        discord("POST", webhook, alert)
    state["levels"] = {key: lvl for key, lvl, _, _ in run.results}
    write_json(state_file, state)

    # Optional external dead-man's switch: alerts you when these pings stop.
    if cfg["healthchecks_url"]:
        try:
            urllib.request.urlopen(urllib.request.Request(
                cfg["healthchecks_url"], headers={"User-Agent": UA}), timeout=10)
        except Exception:
            pass


if __name__ == "__main__":
    main()
