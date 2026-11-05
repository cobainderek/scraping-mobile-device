#!/usr/bin/env python3
"""Painel de Inventário de Dispositivos USB.

Extrai o número de série (S/N) e outras informações de smartphones Android
conectados via USB, mesmo com tela quebrada, usando três métodos independentes:
  1. lsusb -v   (descritores do barramento USB)
  2. adb devices (+ getprop quando autorizado)
  3. fastboot devices (+ getvar)

Um monitor leve do sysfs avisa na hora em que um celular é plugado.
"""

import csv
import json
import queue
import re
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk

# ----------------------------------------------------------------------------
# Constantes
# ----------------------------------------------------------------------------

# Vendor IDs (hex, minúsculo) de fabricantes comuns de smartphones Android.
VENDORS = {
    "04e8": "Samsung",
    "1004": "LG",
    "22b8": "Motorola",
    "2717": "Xiaomi",
    "0b05": "Asus",
    "18d1": "Google",
    "12d1": "Huawei",
    "2a70": "OnePlus",
    "0fce": "Sony",
    "0bb4": "HTC",
    "22d9": "Oppo",
    "2d95": "Vivo",
    "19d2": "ZTE",
    "17ef": "Lenovo",
    "2e04": "Nokia/HMD",
    "1bbb": "Alcatel/TCL",
}

COLUMNS = ("fabricante", "modelo", "serial", "usb_id", "metodo", "status")
HEADINGS = {
    "fabricante": "Fabricante",
    "modelo": "Modelo Detectado",
    "serial": "Número de Série (S/N)",
    "usb_id": "ID USB",
    "metodo": "Método de Captura",
    "status": "Status",
}
WIDTHS = {"fabricante": 120, "modelo": 210, "serial": 210, "usb_id": 110, "metodo": 220, "status": 220}

STORE_PATH = Path.home() / ".local" / "share" / "inventario_usb" / "inventario.json"
SYSFS_USB = Path("/sys/bus/usb/devices")

CMD_TIMEOUT = 20  # segundos
WATCH_INTERVAL = 1.0  # segundos entre leituras do sysfs

COLOR_IDLE = "#6b7280"
COLOR_BUSY = "#f59e0b"
COLOR_OK = "#22c55e"
COLOR_ERR = "#ef4444"

# Propriedades do Android lidas via `adb shell getprop` (só com aparelho autorizado).
ADB_PROPS = (
    ("ro.product.brand", "Marca"),
    ("ro.product.model", "Modelo (sistema)"),
    ("ro.product.name", "Nome comercial"),
    ("ro.product.device", "Codinome"),
    ("ro.build.version.release", "Versão do Android"),
    ("ro.build.version.sdk", "API (SDK)"),
    ("ro.build.version.security_patch", "Patch de segurança"),
    ("ro.build.display.id", "Build"),
    ("ro.product.cpu.abi", "Arquitetura"),
    ("ro.hardware", "Hardware"),
    ("ro.boot.hardware.sku", "SKU"),
    ("ro.csc.sales_code", "Região (CSC)"),
    ("gsm.version.baseband", "Baseband"),
    ("gsm.operator.alpha", "Operadora"),
    ("ro.boot.verifiedbootstate", "Verified boot"),
    ("ro.crypto.state", "Criptografia"),
)

USB_CLASS_NAMES = {6: "MTP/PTP (transferência de arquivos)", 8: "Armazenamento em massa",
                   2: "Modem/CDC", 10: "CDC Data", 224: "Wireless/RNDIS (tethering)"}


# ----------------------------------------------------------------------------
# Backend: métodos de captura
# ----------------------------------------------------------------------------

class ToolMissing(Exception):
    """Ferramenta de sistema não encontrada no PATH."""


def run_cmd(args, timeout=CMD_TIMEOUT):
    """Executa um comando e devolve (returncode, stdout, stderr).

    Levanta ToolMissing se o binário não existir.
    """
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, errors="replace"
        )
    except FileNotFoundError as exc:
        raise ToolMissing(args[0]) from exc
    return proc.returncode, proc.stdout, proc.stderr


def make_row(fabricante, modelo, serial, usb_id, metodo, status, detalhes=None):
    return {"fabricante": fabricante, "modelo": modelo, "serial": serial, "usb_id": usb_id,
            "metodo": metodo, "status": status, "detalhes": detalhes or {}}


def _field(block, name):
    m = re.search(rf"(?m)^\s*{name}\s+(\S.*)$", block)
    return m.group(1).strip() if m else ""


def _str_field(block, name):
    """Campos tipo 'iSerial   3 ABC123' -> 'ABC123' (ignora o índice)."""
    m = re.search(rf"(?m)^\s*{name}\s+\d+\s*(.*)$", block)
    return m.group(1).strip() if m else ""


def scan_lsusb(log):
    """Método 1: lê descritores USB via `lsusb -v`."""
    rows = []
    log("[lsusb] Executando 'lsusb -v' (pode levar alguns segundos)...")
    try:
        _, out, _ = run_cmd(["lsusb", "-v"], timeout=60)
    except ToolMissing:
        log("[lsusb] ERRO: 'lsusb' não encontrado. Instale: sudo apt install usbutils")
        return rows
    except subprocess.TimeoutExpired:
        log("[lsusb] ERRO: tempo esgotado ao executar lsusb -v.")
        return rows

    # Cada dispositivo começa com "Bus 001 Device 004: ID 04e8:6860 Samsung ..."
    blocks = re.split(r"(?m)^(?=Bus \d{3} Device \d{3}:)", out)
    for block in blocks:
        head = re.match(r"Bus (\d{3}) Device (\d{3}): ID ([0-9a-f]{4}):([0-9a-f]{4})(.*)", block)
        if not head:
            continue
        bus, dev, vid, pid, rest = head.groups()
        vid, pid = vid.lower(), pid.lower()
        if vid not in VENDORS:
            continue

        serial = _str_field(block, "iSerial") or None
        model = _str_field(block, "iProduct") or rest.strip() or f"USB {vid}:{pid}"
        maker_usb = _str_field(block, "iManufacturer")

        # Modo atual do aparelho, deduzido das interfaces expostas.
        modes = []
        for cls, sub, proto in re.findall(
            r"bInterfaceClass\s+(\d+).*?\n\s*bInterfaceSubClass\s+(\d+).*?\n\s*bInterfaceProtocol\s+(\d+)",
            block, re.S,
        ):
            cls, sub, proto = int(cls), int(sub), int(proto)
            if cls == 255 and sub == 66 and proto == 1:
                modes.append("ADB (depuração USB ativa)")
            elif cls in USB_CLASS_NAMES:
                modes.append(USB_CLASS_NAMES[cls])
        modes = list(dict.fromkeys(modes))

        det = {
            "Fabricante (descritor USB)": maker_usb,
            "Produto (descritor USB)": model,
            "Versão USB": _field(block, "bcdUSB"),
            "Revisão do dispositivo": _field(block, "bcdDevice"),
            "Consumo máx. (USB)": _field(block, "MaxPower"),
            "Posição no barramento": f"Bus {bus} / Device {dev}",
            "Modo USB atual": ", ".join(modes) or "não identificado",
        }
        det = {k: v for k, v in det.items() if v}

        if serial:
            status = "Lido (hardware)"
            log(f"[lsusb] {VENDORS[vid]} {model} (Bus {bus}/Dev {dev}) -> S/N {serial}")
        else:
            status = "Sem iSerial"
            log(f"[lsusb] {VENDORS[vid]} {model} (Bus {bus}/Dev {dev}): "
                f"iSerial ausente ou ilegível (verifique a regra udev).")
        rows.append(make_row(VENDORS[vid], model, serial or "—", f"{vid}:{pid}",
                             "lsusb (USB nativo)", status, det))

    if not rows:
        log("[lsusb] Nenhum smartphone de fabricante conhecido encontrado.")
    return rows


def _adb_getprops(serial, log):
    """Lê propriedades e bateria de um aparelho ADB autorizado."""
    det = {}
    try:
        _, out, _ = run_cmd(["adb", "-s", serial, "shell", "getprop"], timeout=15)
    except (subprocess.TimeoutExpired, ToolMissing):
        log(f"[adb] {serial}: não foi possível ler getprop.")
        return det
    props = dict(re.findall(r"\[([^\]]+)\]: \[([^\]]*)\]", out))
    for key, label in ADB_PROPS:
        if props.get(key):
            det[label] = props[key]
    for key in ("ro.serialno", "ro.boot.serialno"):
        if props.get(key):
            det["Serial (sistema)"] = props[key]
            break
    try:
        _, out, _ = run_cmd(["adb", "-s", serial, "shell", "dumpsys", "battery"], timeout=10)
        m = re.search(r"level:\s*(\d+)", out)
        if m:
            det["Bateria"] = f"{m.group(1)}%"
    except (subprocess.TimeoutExpired, ToolMissing):
        pass
    return det


def scan_adb(log):
    """Método 2: `adb devices -l` (+ getprop se autorizado)."""
    rows = []
    log("[adb] Executando 'adb devices -l'...")
    try:
        _, out, err = run_cmd(["adb", "devices", "-l"])
    except ToolMissing:
        log("[adb] Aviso: ADB não instalado. Instale: sudo apt install android-tools-adb")
        return rows, False
    except subprocess.TimeoutExpired:
        log("[adb] ERRO: tempo esgotado ao executar adb.")
        return rows, True

    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith(("List of devices", "*")):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        if state not in ("device", "unauthorized"):
            log(f"[adb] {serial} em estado '{state}' (ignorado).")
            continue
        extras = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
        model = extras.get("model", "").replace("_", " ") or extras.get("product", "") or "Android"
        maker = extras.get("manufacturer", "") or "—"
        det = {"Estado ADB": state, "Caminho USB (ADB)": extras.get("usb", "")}
        if extras.get("device"):
            det["Codinome (ADB)"] = extras["device"]
        if state == "device":
            status = "Autorizado"
            det.update(_adb_getprops(serial, log))
            maker = det.get("Marca", maker)
            model = det.get("Modelo (sistema)", model)
        else:
            status = "Não autorizado"
            det["Obs."] = "Autorize a depuração USB no aparelho para ler mais dados."
            log(f"[adb] {serial}: não autorizado - só o serial está disponível por ADB.")
        det = {k: v for k, v in det.items() if v}
        log(f"[adb] {serial} ({state}) -> {model} ({len(det)} campo(s))")
        rows.append(make_row(maker, model, serial, "", "ADB", status, det))

    if not rows:
        log("[adb] Nenhum dispositivo reportado pelo ADB.")
    return rows, True


def scan_fastboot(log):
    """Método 3: `fastboot devices` + `getvar serialno` / `getvar all`."""
    rows = []
    log("[fastboot] Executando 'fastboot devices'...")
    try:
        _, out, _ = run_cmd(["fastboot", "devices"])
    except ToolMissing:
        log("[fastboot] Aviso: Fastboot não instalado. Instale: sudo apt install fastboot")
        return rows, False
    except subprocess.TimeoutExpired:
        log("[fastboot] ERRO: tempo esgotado ao executar fastboot.")
        return rows, True

    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[1] != "fastboot":
            continue
        serial = parts[0]
        status = "Bootloader"
        det = {}
        try:
            # fastboot escreve o resultado de getvar em stderr
            _, o, e = run_cmd(["fastboot", "-s", serial, "getvar", "serialno"], timeout=10)
            m = re.search(r"serialno:\s*(\S+)", o + "\n" + e)
            if m:
                if m.group(1) == serial:
                    status = "Bootloader (validado)"
                else:
                    log(f"[fastboot] getvar divergente: '{m.group(1)}' vs '{serial}'. Usando getvar.")
                    serial = m.group(1)
                    status = "Bootloader (getvar)"
            _, o, e = run_cmd(["fastboot", "-s", serial, "getvar", "all"], timeout=20)
            for k, v in re.findall(r"\(bootloader\)\s*([\w\-]+):\s*(.+)", o + "\n" + e):
                if k not in ("serialno",) and not k.startswith(("partition-", "is-logical")):
                    det[f"fastboot {k}"] = v.strip()
        except (subprocess.TimeoutExpired, ToolMissing):
            log(f"[fastboot] getvar falhou/expirou para {serial}; usando valor de 'devices'.")
        model = det.get("fastboot product", "Modo Fastboot")
        log(f"[fastboot] {serial} -> {status} ({len(det)} variável(is))")
        rows.append(make_row("—", model, serial, "", "Fastboot", status, det))

    if not rows:
        log("[fastboot] Nenhum dispositivo em modo bootloader.")
    return rows, True


def scan_all(log):
    """Executa os três métodos de forma independente; falha de um não afeta os demais."""
    results = []
    adb_ok = fb_ok = True
    for name, fn in (("lsusb", scan_lsusb), ("adb", scan_adb), ("fastboot", scan_fastboot)):
        try:
            res = fn(log)
        except Exception as exc:  # isolamento entre métodos
            log(f"[{name}] ERRO inesperado: {exc!r}")
            continue
        if name == "lsusb":
            results += res
        else:
            rows, available = res
            results += rows
            if name == "adb":
                adb_ok = available
            else:
                fb_ok = available
    if not adb_ok and not fb_ok:
        log("Aviso: ADB/Fastboot não instalados. Varredura limitada ao lsusb")
    return results


# ----------------------------------------------------------------------------
# Monitor de conexão (sysfs, sem subprocess)
# ----------------------------------------------------------------------------

def _read(path):
    try:
        return path.read_text(errors="replace").strip()
    except OSError:
        return ""


def list_connected_phones():
    """Devolve {caminho: descrição} dos USBs de fabricantes de celular no sysfs."""
    found = {}
    try:
        entries = list(SYSFS_USB.iterdir())
    except OSError:
        return found
    for d in entries:
        if ":" in d.name:  # interfaces, não dispositivos
            continue
        vid = _read(d / "idVendor").lower()
        if vid not in VENDORS:
            continue
        pid = _read(d / "idProduct").lower()
        product = _read(d / "product") or f"{vid}:{pid}"
        serial = _read(d / "serial")
        found[d.name] = {"fabricante": VENDORS[vid], "produto": product, "serial": serial}
    return found


# ----------------------------------------------------------------------------
# Coleção persistente (acumula entre varreduras)
# ----------------------------------------------------------------------------

def _join_unique(a, b, sep):
    parts = [x for x in (a.split(sep) if a else []) + (b.split(sep) if b else []) if x]
    return sep.join(dict.fromkeys(parts))


def row_key(row):
    return row["serial"] if row["serial"] != "—" else f"sem-serial:{row['modelo']}"


def merge_rows(collection, rows):
    """Soma as linhas de uma varredura à coleção (chave: S/N). Devolve (novos, atualizados)."""
    new = upd = 0
    now = f"{datetime.now():%d/%m/%Y %H:%M:%S}"
    for row in rows:
        key = row_key(row)
        cur = collection.get(key)
        if cur is None:
            cur = dict(row)
            cur["detalhes"] = dict(row["detalhes"])
            cur["detalhes"]["Primeira leitura"] = now
            cur["detalhes"]["Última leitura"] = now
            collection[key] = cur
            new += 1
            continue
        upd += 1
        for f in ("fabricante", "usb_id"):
            if cur.get(f) in (None, "", "—") and row[f]:
                cur[f] = row[f]
        if cur["modelo"] in ("Android", "Modo Fastboot", "") and row["modelo"] not in ("Android", "Modo Fastboot"):
            cur["modelo"] = row["modelo"]
        cur["metodo"] = _join_unique(cur["metodo"], row["metodo"], " + ")
        cur["status"] = _join_unique(cur["status"], row["status"], " / ")
        cur.setdefault("detalhes", {}).update(row["detalhes"])
        cur["detalhes"]["Última leitura"] = now
    return new, upd


def load_collection():
    try:
        data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    for rec in data.values():  # compatível com versões antigas do arquivo
        rec.setdefault("usb_id", "")
        rec.setdefault("detalhes", {})
    return data


def save_collection(collection):
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STORE_PATH.write_text(json.dumps(collection, ensure_ascii=False, indent=2), encoding="utf-8")


# ----------------------------------------------------------------------------
# Interface gráfica
# ----------------------------------------------------------------------------

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.title("Painel de Inventário de Dispositivos USB")
        self.geometry("1280x860")
        self.minsize(1100, 760)

        self.log_queue = queue.Queue()
        self.result_queue = queue.Queue()
        self.plug_queue = queue.Queue()
        self.scanning = False
        self.connected = {}
        self.banner_override = None  # (texto, cor) do resultado da última varredura
        self.collection = load_collection()

        self._build_header()
        self._build_banner()
        self._build_controls()
        self._build_table()
        self._build_bottom()
        self._style_tree()

        self._refresh_table()
        self.after(100, self._poll_queues)
        threading.Thread(target=self._watch_usb, daemon=True).start()
        if self.collection:
            self.log(f"Coleção carregada: {len(self.collection)} aparelho(s) de {STORE_PATH}")
        self.log("Pronto. Conecte um aparelho: o painel avisa na hora.")

    # --- construção ---------------------------------------------------------

    def _build_header(self):
        frame = ctk.CTkFrame(self, fg_color="transparent")
        frame.pack(fill="x", padx=20, pady=(16, 6))
        ctk.CTkLabel(
            frame, text="Painel de Inventário de Dispositivos USB",
            font=ctk.CTkFont(size=30, weight="bold"),
        ).pack(side="left")
        status_box = ctk.CTkFrame(frame, fg_color="transparent")
        status_box.pack(side="right")
        self.status_dot = ctk.CTkLabel(status_box, text="●", text_color=COLOR_IDLE,
                                       font=ctk.CTkFont(size=28))
        self.status_dot.pack(side="left", padx=(0, 8))
        self.status_text = ctk.CTkLabel(status_box, text="Ocioso", font=ctk.CTkFont(size=18))
        self.status_text.pack(side="left")

    def _build_banner(self):
        self.banner = ctk.CTkFrame(self, fg_color="#374151", corner_radius=10, height=64)
        self.banner.pack(fill="x", padx=20, pady=6)
        self.banner.pack_propagate(False)
        self.banner_label = ctk.CTkLabel(
            self.banner, text="🔌  Aguardando aparelho... conecte o celular via USB",
            font=ctk.CTkFont(size=20, weight="bold"), anchor="w",
        )
        self.banner_label.pack(side="left", padx=18, fill="x", expand=True)

    def _build_controls(self):
        frame = ctk.CTkFrame(self, fg_color="transparent")
        frame.pack(fill="x", padx=20, pady=6)
        btn_font = ctk.CTkFont(size=18, weight="bold")
        self.btn_scan = ctk.CTkButton(
            frame, text="🔍  Escanear Dispositivos", height=56, font=btn_font,
            command=self.start_scan,
        )
        self.btn_scan.pack(side="left", padx=(0, 12))
        self.btn_export = ctk.CTkButton(
            frame, text="💾  Exportar Inventário (.CSV)", height=56, font=btn_font,
            fg_color="#16a34a", hover_color="#15803d",
            command=self.export_csv, state="disabled",
        )
        self.btn_export.pack(side="left", padx=(0, 12))
        self.btn_clear = ctk.CTkButton(
            frame, text="🗑  Limpar coleção", height=56, font=btn_font,
            fg_color="#7f1d1d", hover_color="#991b1b", command=self.clear_collection,
        )
        self.btn_clear.pack(side="left", padx=(0, 12))
        self.btn_manual = ctk.CTkButton(
            frame, text="➕  Adicionar manual", height=56, font=btn_font,
            fg_color="#6d28d9", hover_color="#5b21b6", command=self.add_manual,
        )
        self.btn_manual.pack(side="left")
        self.count_label = ctk.CTkLabel(frame, text="0 registro(s)", text_color="#9ca3af",
                                        font=ctk.CTkFont(size=18))
        self.count_label.pack(side="right")

    def _build_table(self):
        frame = ctk.CTkFrame(self)
        frame.pack(fill="both", expand=True, padx=20, pady=6)
        self.tree = ttk.Treeview(frame, columns=COLUMNS, show="headings", selectmode="browse")
        for col in COLUMNS:
            self.tree.heading(col, text=HEADINGS[col])
            self.tree.column(col, width=WIDTHS[col], anchor="w", stretch=True)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(4, 0), pady=4)
        scroll.pack(side="right", fill="y", pady=4, padx=(0, 4))
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

    def _build_bottom(self):
        frame = ctk.CTkFrame(self, fg_color="transparent")
        frame.pack(fill="x", padx=20, pady=(6, 16))
        frame.columnconfigure((0, 1), weight=1, uniform="b")

        left = ctk.CTkFrame(frame, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        ctk.CTkLabel(left, text="Detalhes do aparelho selecionado", anchor="w",
                     font=ctk.CTkFont(size=16, weight="bold")).pack(fill="x")
        self.detail_box = ctk.CTkTextbox(left, height=230, font=ctk.CTkFont(size=15))
        self.detail_box.pack(fill="both", expand=True)
        self._set_text(self.detail_box, "Clique em um aparelho da tabela para ver todas as informações.")

        right = ctk.CTkFrame(frame, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        ctk.CTkLabel(right, text="Log de atividade", anchor="w",
                     font=ctk.CTkFont(size=16, weight="bold")).pack(fill="x")
        self.log_box = ctk.CTkTextbox(right, height=230, font=ctk.CTkFont(family="monospace", size=14))
        self.log_box.pack(fill="both", expand=True)
        self.log_box.configure(state="disabled")

    def _style_tree(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Treeview", background="#2b2b2b", fieldbackground="#2b2b2b",
                        foreground="#e5e7eb", rowheight=44, borderwidth=0,
                        font=("TkDefaultFont", 14))
        style.configure("Treeview.Heading", background="#1f538d", foreground="white",
                        relief="flat", font=("TkDefaultFont", 14, "bold"), padding=8)
        style.map("Treeview", background=[("selected", "#1f538d")])
        style.map("Treeview.Heading", background=[("active", "#2563eb")])
        style.configure("Vertical.TScrollbar", arrowsize=20)
        self.tree.tag_configure("odd", background="#323232")

    # --- utilitários --------------------------------------------------------

    def log(self, msg):
        """Thread-safe: pode ser chamado da thread de varredura."""
        self.log_queue.put(f"[{datetime.now():%H:%M:%S}] {msg}")

    def _set_status(self, text, color):
        self.status_text.configure(text=text)
        self.status_dot.configure(text_color=color)

    @staticmethod
    def _set_text(box, text):
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("1.0", text)
        box.configure(state="disabled")

    def _poll_queues(self):
        try:
            while True:
                line = self.log_queue.get_nowait()
                self.log_box.configure(state="normal")
                self.log_box.insert("end", line + "\n")
                self.log_box.see("end")
                self.log_box.configure(state="disabled")
        except queue.Empty:
            pass
        try:
            rows = self.result_queue.get_nowait()
            self._finish_scan(rows)
        except queue.Empty:
            pass
        try:
            while True:
                self._update_connected(self.plug_queue.get_nowait())
        except queue.Empty:
            pass
        self.after(100, self._poll_queues)

    # --- monitor de conexão -------------------------------------------------

    def _watch_usb(self):
        """Thread: compara o sysfs a cada segundo e avisa quando algo muda."""
        last = None
        while True:
            current = list_connected_phones()
            if current != last:
                self.plug_queue.put(current)
                last = current
            threading.Event().wait(WATCH_INTERVAL)

    def _update_connected(self, current):
        for path, info in current.items():
            if path not in self.connected:
                self.log(f"Conectado: {info['fabricante']} {info['produto']}"
                         f"{' (S/N ' + info['serial'] + ')' if info['serial'] else ''}")
                if info["serial"] in self.collection:
                    self.log(f"AVISO: aparelho já existe na coleção (S/N {info['serial']}).")
        for path, info in self.connected.items():
            if path not in current:
                self.log(f"Desconectado: {info['fabricante']} {info['produto']}")
        if set(current) != set(self.connected):
            self.banner_override = None  # mudou o que está plugado: descarta o resultado antigo
        self.connected = current
        self._render_banner()

    def _render_banner(self):
        current = self.connected
        if self.banner_override:
            text, color = self.banner_override
            self.banner.configure(fg_color=color)
            self.banner_label.configure(text=text)
        elif current:
            names = ", ".join(f"{i['fabricante']} {i['produto']}" for i in current.values())
            dups = [i for i in current.values() if i["serial"] and i["serial"] in self.collection]
            if dups:
                self.banner.configure(fg_color="#b45309")
                self.banner_label.configure(
                    text=f"⚠️  JÁ EXISTE na coleção: {', '.join(i['serial'] for i in dups)}"
                         f"  -  escanear só atualiza os dados")
            else:
                self.banner.configure(fg_color="#15803d")
                self.banner_label.configure(
                    text=f"📱  {len(current)} aparelho(s) detectado(s): {names}  -  clique em Escanear")
        else:
            self.banner.configure(fg_color="#374151")
            self.banner_label.configure(text="🔌  Aguardando aparelho... conecte o celular via USB")
        if current:
            self.btn_scan.configure(fg_color="#16a34a", hover_color="#15803d")
        else:
            self.btn_scan.configure(fg_color=("#3a7ebf", "#1f538d"), hover_color=("#325882", "#14375e"))

    # --- ações --------------------------------------------------------------

    def start_scan(self):
        if self.scanning:
            return
        self.scanning = True
        self.btn_scan.configure(state="disabled", text="⏳  Escaneando...")
        self._set_status("Escaneando", COLOR_BUSY)
        self.log("=== Iniciando varredura ===")
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        try:
            rows = scan_all(self.log)
        except Exception as exc:
            self.log(f"ERRO fatal na varredura: {exc!r}")
            rows = None
        self.result_queue.put(rows)

    def _finish_scan(self, rows):
        self.scanning = False
        self.btn_scan.configure(state="normal", text="🔍  Escanear Dispositivos")
        if rows is None:
            self._set_status("Erro", COLOR_ERR)
            return
        dup_rows = {}
        for r in rows:
            if row_key(r) in self.collection:
                dup_rows[row_key(r)] = r
        for key, r in dup_rows.items():
            self.log(f"AVISO: aparelho já existe na coleção: {r['fabricante']} {r['modelo']} "
                     f"(S/N {r['serial']}) - dados atualizados.")
        new, upd = merge_rows(self.collection, rows)
        if dup_rows:
            self.banner_override = (
                f"⚠️  APARELHO JÁ EXISTE na coleção: {', '.join(r['serial'] for r in dup_rows.values())}"
                f"  -  dados atualizados", "#b45309")
        elif new:
            self.banner_override = (f"✅  {new} aparelho(s) novo(s) adicionado(s) à coleção", "#15803d")
        else:
            self.banner_override = None
        self._render_banner()
        try:
            save_collection(self.collection)
        except OSError as exc:
            self.log(f"ERRO ao salvar coleção: {exc}")
        self._refresh_table()
        self.log(f"=== Varredura concluída: {new} novo(s), {upd} já na coleção ===")
        self._set_status("Concluído" if rows else "Nenhum dispositivo",
                         COLOR_OK if rows else COLOR_IDLE)

    def _refresh_table(self):
        selected = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        for i, (key, rec) in enumerate(self.collection.items()):
            self.tree.insert("", "end", iid=key, values=[rec.get(c, "") for c in COLUMNS],
                             tags=("odd",) if i % 2 else ())
        if selected and self.tree.exists(selected[0]):
            self.tree.selection_set(selected[0])
        self.count_label.configure(text=f"{len(self.collection)} registro(s)")
        self.btn_export.configure(state="normal" if self.collection else "disabled")

    def _on_select(self, _event=None):
        sel = self.tree.selection()
        if not sel or sel[0] not in self.collection:
            return
        rec = self.collection[sel[0]]
        lines = [f"Fabricante: {rec['fabricante']}", f"Modelo: {rec['modelo']}",
                 f"Número de série: {rec['serial']}", f"ID USB: {rec.get('usb_id') or '—'}",
                 f"Métodos: {rec['metodo']}", f"Status: {rec['status']}", ""]
        lines += [f"{k}: {v}" for k, v in rec.get("detalhes", {}).items()]
        self._set_text(self.detail_box, "\n".join(lines))

    def add_manual(self):
        """Cadastra à mão um aparelho que o USB não consegue ler (ex.: S/N ou IMEI da NF)."""
        win = ctk.CTkToplevel(self)
        win.title("Adicionar aparelho manualmente")
        win.geometry("540x520")
        win.transient(self)
        entries = {}
        for label, key in (("Fabricante", "fabricante"), ("Modelo", "modelo"),
                           ("S/N ou IMEI *", "serial"), ("Observação (ex.: NF 1234)", "obs")):
            ctk.CTkLabel(win, text=label, anchor="w", font=ctk.CTkFont(size=16)).pack(
                fill="x", padx=24, pady=(14, 2))
            entries[key] = ctk.CTkEntry(win, height=42, font=ctk.CTkFont(size=16))
            entries[key].pack(fill="x", padx=24)
        error = ctk.CTkLabel(win, text="", text_color=COLOR_ERR, font=ctk.CTkFont(size=14))
        error.pack(pady=(10, 0))

        def save():
            vals = {k: e.get().strip() for k, e in entries.items()}
            if not vals["serial"]:
                error.configure(text="Informe o S/N ou IMEI.")
                return
            row = make_row(vals["fabricante"] or "—", vals["modelo"] or "Aparelho (manual)",
                           vals["serial"], "", "Cadastro manual", "Manual",
                           {"Observação": vals["obs"], "Origem": "Digitado pelo usuário"})
            row["detalhes"] = {k: v for k, v in row["detalhes"].items() if v}
            existed = row_key(row) in self.collection
            merge_rows(self.collection, [row])
            try:
                save_collection(self.collection)
            except OSError as exc:
                self.log(f"ERRO ao salvar coleção: {exc}")
            self._refresh_table()
            if existed:
                self.log(f"AVISO: aparelho já existe na coleção (S/N {row['serial']}) - dados atualizados.")
                self.banner_override = (f"⚠️  APARELHO JÁ EXISTE na coleção: {row['serial']}"
                                        f"  -  dados atualizados", "#b45309")
            else:
                self.log(f"Cadastro manual adicionado: {row['modelo']} (S/N {row['serial']})")
                self.banner_override = ("✅  1 aparelho adicionado manualmente à coleção", "#15803d")
            self._render_banner()
            win.destroy()

        ctk.CTkButton(win, text="Salvar na coleção", height=50, font=ctk.CTkFont(size=18, weight="bold"),
                      fg_color="#16a34a", hover_color="#15803d", command=save).pack(
            fill="x", padx=24, pady=16)
        win.after(200, lambda: (win.lift(), win.focus_force(), entries["serial"].focus()))

    def clear_collection(self):
        if not self.collection:
            return
        if not messagebox.askyesno("Limpar coleção",
                                   f"Apagar os {len(self.collection)} aparelhos da coleção?"):
            return
        self.collection.clear()
        try:
            save_collection(self.collection)
        except OSError as exc:
            self.log(f"ERRO ao salvar coleção: {exc}")
        self._refresh_table()
        self._set_text(self.detail_box, "Clique em um aparelho da tabela para ver todas as informações.")
        self.log("Coleção limpa.")

    def export_csv(self):
        if not self.collection:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV", "*.csv")],
            initialfile=f"inventario_{datetime.now():%Y%m%d_%H%M%S}.csv",
        )
        if not path:
            return
        try:
            # utf-8-sig para abrir corretamente acentos no Excel/LibreOffice
            with open(path, "w", newline="", encoding="utf-8-sig") as fh:
                writer = csv.writer(fh)
                writer.writerow([HEADINGS[c] for c in COLUMNS] + ["Detalhes"])
                for rec in self.collection.values():
                    det = "; ".join(f"{k}: {v}" for k, v in rec.get("detalhes", {}).items())
                    writer.writerow([rec.get(c, "") for c in COLUMNS] + [det])
            self.log(f"Inventário exportado: {path}")
        except OSError as exc:
            self.log(f"ERRO ao exportar CSV: {exc}")
            messagebox.showerror("Erro", f"Não foi possível salvar o arquivo:\n{exc}")


if __name__ == "__main__":
    App().mainloop()
